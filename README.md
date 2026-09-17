# Backend agregador multi-nube

API pequeña que consolida el estado y las métricas de AWS, GCP y Azure para el
agente de voz SRE (reto ElevenLabs). El agente le habla a esta API en vez de
hablarle a cada nube por separado — así "multi-nube" no depende de credenciales
reales de AWS/Azure/GCP, sino de un agregador que tú controlas.

Probado localmente: `/health`, `/status`, `/metrics`, `/simulate` y `/reset`
responden correctamente (ver más abajo).

## Endpoints

| Método | Ruta | Para qué |
| --- | --- | --- |
| GET | `/health` | ping — confírmalo antes de grabar |
| GET | `/status` | estado de las 3 nubes → tool `get_cloud_status` |
| GET | `/metrics` | error rate / latencia → tool `get_error_metrics` |
| POST | `/simulate` | fuerza un estado/métrica (para armar el incidente antes de grabar) |
| POST | `/reset` | vuelve todo a "OK" (para repetir tomas) |

## Correr en local

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Prueba rápida:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/status
curl http://localhost:8000/metrics
```

Simular el incidente de la demo (AWS degradado en us-east-1) justo antes de grabar:

```bash
curl -X POST http://localhost:8000/simulate -H "Content-Type: application/json" \
  -d '{"provider":"aws","status":"degraded","detail":"Elevated error rates on EC2",
       "error_rate_pct":4.8,"p99_latency_ms":820}'
```

Volver a la línea base entre tomas:

```bash
curl -X POST http://localhost:8000/reset
```

## Desplegar (elige uno — todos tienen nivel gratuito y HTTPS público, que es lo que necesitas para las webhook tools de ElevenLabs)

### Render
1. Sube esta carpeta a un repo de GitHub.
2. En render.com → New → Web Service → conecta el repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Copia la URL pública que te da Render (ej. `https://tu-backend.onrender.com`).

### Railway
1. railway.app → New Project → Deploy from GitHub repo.
2. Railway detecta Python solo; si te pide start command usa la misma de arriba.
3. Copia el dominio público que te asigna.

### Fly.io
```bash
fly launch   # detecta el proyecto Python, genera el Dockerfile si lo pides
fly deploy
```

Nota sobre los free tiers: Render y Railway "duermen" el servicio tras un rato
sin tráfico — la primera llamada después de dormir tarda unos segundos.
Haz un `curl /health` un par de minutos antes de grabar para despertarlo.

## Conectar con el agente en ElevenLabs

En la pestaña *Tools* del agente, crea dos webhook tools apuntando a tu URL
desplegada:

- `get_cloud_status` → `GET https://tu-backend.onrender.com/status`
- `get_error_metrics` → `GET https://tu-backend.onrender.com/metrics`

Ninguna de las dos necesita parámetros ni autenticación — son GET públicos de
solo lectura, pensados para que el agente los llame directo.

## Nota de despliegue real (Render)

Desplegado en: https://sre-voice-agent-backend.onrender.com

Render usa Python 3.14 por defecto para servicios nuevos, y `pydantic-core`
todavía no publica wheel precompilado para esa versión (intenta compilar
desde código fuente con Rust/maturin y falla por sistema de archivos de
solo lectura). Solución: en el servicio, pestaña **Environment**, agrega la
variable `PYTHON_VERSION=3.11.9` y vuelve a desplegar. El archivo
`runtime.txt` ya NO es el mecanismo soportado por Render — usa la variable
de entorno o un archivo `.python-version`.
