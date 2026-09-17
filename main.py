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
  ZENDESK_SUBDOMAIN   ej. "alquimicos"
  ZENDESK_TOKEN       el valor COMPLETO del header Authorization que Zendesk espera,
                      ej. "Bearer abc123..." (token OAuth) o "Basic base64(email/token:API_TOKEN)"
                      Este backend nunca lo expone: lo lee del entorno y lo reenvia a Zendesk.
                      Si el token expira, se actualiza SOLO aqui (en Render) -- ElevenLabs
                      no se toca para nada, porque apunta siempre a este mismo endpoint.
"""

import os
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


ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "")
ZENDESK_TOKEN = os.environ.get("ZENDESK_TOKEN", "")  # valor completo del header Authorization


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
    El agente de ElevenLabs le pega a ESTE endpoint (sin credenciales de Zendesk),
    y es este backend el que agrega el Authorization real antes de reenviar a Zendesk.
    Asi, si el token de Zendesk expira, se renueva SOLO en la variable de entorno
    ZENDESK_TOKEN de Render -- no hay que tocar nada en ElevenLabs.
    """
    if not ZENDESK_SUBDOMAIN or not ZENDESK_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Backend mal configurado: falta ZENDESK_SUBDOMAIN o ZENDESK_TOKEN en las variables de entorno.",
        )

    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets.json"
    payload = {
        "ticket": {
            "subject": req.subject,
            "comment": {"body": req.body},
            "priority": req.priority,
        }
    }

    try:
        resp = httpx.post(
            url,
            headers={"Authorization": ZENDESK_TOKEN, "Content-Type": "application/json"},
            json=payload,
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar a Zendesk: {exc}")

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Zendesk respondio {resp.status_code}: {resp.text}")

    data = resp.json()
    ticket = data.get("ticket", {})
    return {"ok": True, "ticket_id": ticket.get("id"), "url": ticket.get("url")}
