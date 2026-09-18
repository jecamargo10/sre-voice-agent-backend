"""
Backend agregador multi-nube — SRE Voice Agent (ElevenLabs take-home)

Consolida el estado y las metricas de AWS, GCP y Azure en un solo lugar,
para que el agente de voz (o cualquier otro cliente) los consulte con
una sola llamada HTTP en vez de hablar con cada nube por separado.

Endpoints:
  GET  /             -> info basica de la API
  GET  /health        -> liveness check (usalo antes de grabar el demo)
  GET  /status        -> estado actual de las tres nubes
  GET  /metrics       -> metricas de error/latencia de las tres nubes
  POST /simulate      -> fuerza un estado/metrica para una nube (control del demo)
  POST /reset          -> vuelve todo a la linea base "todo OK"
  POST /escalate       -> crea un ticket de escalamiento en Zendesk (proxy, tool create_escalation)

Ejecutar local:
  pip install -r requirements.txt
  uvicorn main:app --reload --port 8000

Deploy: ver README.md (Render / Railway / Fly.io).

Variables de entorno (Render -> Environment):
  ZENDESK_SUBDOMAIN     ej. "alquimicos"
  ZENDESK_CLIENT_ID     "Unique identifier" del OAuth client (debe ser tipo CONFIDENTIAL)
  ZENDESK_CLIENT_SECRET Client Secret de ese mismo OAuth client
  ZENDESK_SCOPE         opcional, default "tickets:read tickets:write"

  Con estas 4 variables el backend saca su propio access token via el grant
  "client_credentials" (machine-to-machine, sin navegador ni login humano) y lo
  cachea en memoria. Si Zendesk lo rechaza (expirado/revocado), pide uno nuevo
  automaticamente y reintenta una vez -- no hay token manual que se venza ni
  flujo OAuth que repetir a mano.

  Requisito en Zendesk: el OAuth client debe ser "Confidential" (no "Public"),
  porque client_credentials no esta permitido para clients Publicos.
"""

import os
import threading
import time
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="Backend agregador multi-nube",
    description="Consolida estado y metricas de AWS, GCP y Azure para el agente SRE de voz.",
    version="1.0.0",
)

# CORS abierto: este backend solo lo llaman las tools del agente (webhook tools)
# y tu propio navegador/curl durante las pruebas. No expone nada sensible.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

Provider = Literal["aws", "gcp", "azure"]
StatusValue = Literal["ok", "degraded", "down"]

# --- Estado en memoria -------------------------------------------------
# Linea base: todo operando con normalidad. Usa /simulate para inyectar
# el incidente justo antes de grabar (o durante la llamada de prueba).

BASELINE_STATUS = {
    "aws": {"status": "ok", "region": "us-east-1", "detail": "All systems operational"},
    "gcp": {"status": "ok", "region": "us-central1", "detail": "All systems operational"},
    "azure": {"status": "ok", "region": "eastus2", "detail": "All systems operational"},
}

BASELINE_METRICS = {
    "aws": {"error_rate_pct": 0.1, "p99_latency_ms": 180},
    "gcp": {"error_rate_pct": 0.1, "p99_latency_ms": 150},
    "azure": {"error_rate_pct": 0.1, "p99_latency_ms": 165},
}

state = {
    "status": {k: v.copy() for k, v in BASELINE_STATUS.items()},
    "metrics": {k: v.copy() for k, v in BASELINE_METRICS.items()},
    "updated_at": datetime.now(timezone.utc).isoformat(),
}


class SimulateRequest(BaseModel):
    provider: Provider
    status: Optional[StatusValue] = None
    region: Optional[str] = None
    detail: Optional[str] = None
    error_rate_pct: Optional[float] = None
    p99_latency_ms: Optional[int] = None


class EscalateRequest(BaseModel):
    subject: str
    body: str
    priority: Optional[Literal["low", "normal", "high", "urgent"]] = "normal"
    # ID de la conversacion de ElevenLabs (system__conversation_id), pasado como
    # Dynamic variable por la plataforma -- NUNCA lo decide el modelo. Con esto el
    # backend puede des-duplicar aunque el agente llame esta tool mas de una vez
    # para la misma llamada.
    conversation_id: Optional[str] = None


ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "")
ZENDESK_CLIENT_ID = os.environ.get("ZENDESK_CLIENT_ID", "")
ZENDESK_CLIENT_SECRET = os.environ.get("ZENDESK_CLIENT_SECRET", "")
ZENDESK_SCOPE = os.environ.get("ZENDESK_SCOPE", "tickets:read tickets:write")

# Cache en memoria del access token (client_credentials): se pide uno nuevo
# solo cuando no hay token cacheado, cuando ya paso su expiracion, o cuando
# Zendesk lo rechaza en caliente (401) durante una llamada real.
_token_cache = {"access_token": None, "expires_at": 0.0}
_token_lock = threading.Lock()

# Des-duplicacion de escalamientos: llave = conversation_id de ElevenLabs.
# Si el agente llama create_escalation mas de una vez para la MISMA llamada
# (reintento, confusion del modelo, lo que sea) esto evita abrir un segundo
# ticket real en Zendesk -- la garantia vive aqui, no en que el prompt "se
# acuerde" de que ya escalo. Se limpia en cada /reset y vive solo en memoria
# (se resetea si el proceso se reinicia; suficiente para este demo).
_escalations_by_conversation = {}
_escalation_lock = threading.Lock()


def _fetch_zendesk_token() -> str:
    """Pide un access token nuevo via client_credentials -- sin navegador, sin humano."""
    if not ZENDESK_SUBDOMAIN or not ZENDESK_CLIENT_ID or not ZENDESK_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail=(
                "Backend mal configurado: falta ZENDESK_SUBDOMAIN, ZENDESK_CLIENT_ID "
                "o ZENDESK_CLIENT_SECRET en las variables de entorno."
            ),
        )
    try:
        resp = httpx.post(
            f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/oauth/tokens",
            headers={"Content-Type": "application/json"},
            json={
                "grant_type": "client_credentials",
                "client_id": ZENDESK_CLIENT_ID,
                "client_secret": ZENDESK_CLIENT_SECRET,
                "scope": ZENDESK_SCOPE,
            },
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar el endpoint de OAuth de Zendesk: {exc}")

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=(
                "Zendesk rechazo la solicitud de token (revisa que el OAuth client sea "
                f"'Confidential' y tenga el scope correcto): {resp.status_code} {resp.text}"
            ),
        )

    data = resp.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    with _token_lock:
        _token_cache["access_token"] = token
        # Renovamos 60s antes de la expiracion real, con margen de seguridad.
        _token_cache["expires_at"] = time.time() + max(expires_in - 60, 0)
    return token


def _get_zendesk_token(force_refresh: bool = False) -> str:
    with _token_lock:
        cached_ok = (
            not force_refresh
            and _token_cache["access_token"]
            and time.time() < _token_cache["expires_at"]
        )
        if cached_ok:
            return _token_cache["access_token"]
    return _fetch_zendesk_token()


@app.get("/")
def root():
    return {
        "service": "backend-agregador-multi-nube",
        "endpoints": ["/health", "/status", "/metrics", "/simulate (POST)", "/reset (POST)"],
        "updated_at": state["updated_at"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def get_status():
    """Estado consolidado de AWS, GCP y Azure. Tool: get_cloud_status."""
    return {"providers": state["status"], "updated_at": state["updated_at"]}


@app.get("/metrics")
def get_metrics():
    """Metricas de error/latencia de AWS, GCP y Azure. Tool: get_error_metrics."""
    return {"providers": state["metrics"], "updated_at": state["updated_at"]}


@app.post("/simulate")
def simulate(body: SimulateRequest):
    """
    Fuerza el estado/metricas de una nube para controlar el demo.
    Ejemplo (degradar AWS antes de grabar):

      curl -X POST http://localhost:8000/simulate -H "Content-Type: application/json" \\
        -d '{"provider":"aws","status":"degraded","detail":"Elevated error rates on EC2",
             "error_rate_pct":4.8,"p99_latency_ms":820}'
    """
    provider = body.provider
    if body.status is not None:
        state["status"][provider]["status"] = body.status
    if body.region is not None:
        state["status"][provider]["region"] = body.region
    if body.detail is not None:
        state["status"][provider]["detail"] = body.detail
    if body.error_rate_pct is not None:
        state["metrics"][provider]["error_rate_pct"] = body.error_rate_pct
    if body.p99_latency_ms is not None:
        state["metrics"][provider]["p99_latency_ms"] = body.p99_latency_ms

    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return {"ok": True, "provider": provider, "status": state["status"][provider], "metrics": state["metrics"][provider]}


@app.post("/reset")
def reset():
    """Vuelve las tres nubes a la linea base 'todo OK'. Usalo entre tomas."""
    state["status"] = {k: v.copy() for k, v in BASELINE_STATUS.items()}
    state["metrics"] = {k: v.copy() for k, v in BASELINE_METRICS.items()}
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with _escalation_lock:
        _escalations_by_conversation.clear()
    return {"ok": True, "status": state["status"], "metrics": state["metrics"]}


@app.get("/status/{provider}")
def get_status_one(provider: Provider):
    return state["status"][provider]


@app.get("/metrics/{provider}")
def get_metrics_one(provider: Provider):
    return state["metrics"][provider]


@app.post("/escalate")
def escalate(req: EscalateRequest):
    """
    Proxy hacia Zendesk: crea un ticket de escalamiento. Tool: create_escalation.
    El agente de ElevenLabs le pega a ESTE endpoint (sin credenciales de Zendesk).
    El backend obtiene su propio access token (client_credentials), lo cachea, y si
    Zendesk lo rechaza por expirado/invalido pide uno nuevo y reintenta una vez sola
    -- todo automatico, sin volver a pasar por el flujo OAuth de navegador.
    """
    if not ZENDESK_SUBDOMAIN:
        raise HTTPException(
            status_code=500,
            detail="Backend mal configurado: falta ZENDESK_SUBDOMAIN en las variables de entorno.",
        )

    # Si ya escalamos esta misma llamada (mismo conversation_id), devolvemos el
    # ticket que ya existe en vez de crear uno nuevo -- pase lo que pase del
    # lado del modelo.
    if req.conversation_id:
        with _escalation_lock:
            cached = _escalations_by_conversation.get(req.conversation_id)
        if cached is not None:
            return {**cached, "already_escalated": True}

    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets.json"
    payload = {
        "ticket": {
            "subject": req.subject,
            "comment": {"body": req.body},
            "priority": req.priority,
        }
    }

    def _post_with_token(bearer_token: str) -> httpx.Response:
        return httpx.post(
            url,
            headers={"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=10.0,
        )

    token = _get_zendesk_token()
    try:
        resp = _post_with_token(token)
        if resp.status_code == 401:
            # Token invalido/expirado antes de lo previsto: pide uno nuevo y reintenta UNA vez.
            token = _get_zendesk_token(force_refresh=True)
            resp = _post_with_token(token)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar a Zendesk: {exc}")

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Zendesk respondio {resp.status_code}: {resp.text}")

    data = resp.json()
    ticket = data.get("ticket", {})
    result = {"ok": True, "ticket_id": ticket.get("id"), "url": ticket.get("url")}

    if req.conversation_id:
        with _escalation_lock:
            _escalations_by_conversation[req.conversation_id] = result

    return result
