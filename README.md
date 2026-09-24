# V-CORE

**Versión 2.0.0** · LLM orchestrator multi-agente con routing por roles, circuit breaker, fallback chains y security gate determinístico.

[![CI](https://github.com/ViSchock66/v_core/actions/workflows/ci.yml/badge.svg)](https://github.com/ViSchock66/v_core/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.138-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## Qué es

V-CORE es un orquestador de modelos de lenguaje (LLM) tipo IDE:
un backend FastAPI que despacha conversaciones a 4 agentes con identidad propia,
rutea llamadas a múltiples proveedores NIM con fallback automático,
y pone un security gate delante de cualquier acción con side-effects
(escritura de archivos, ejecución de shell) para que pasen por aprobación
humana antes de ejecutarse.

No es un wrapper de OpenAI. No es un chatbot. Es infraestructura de
operaciones de LLM — mismo tipo de problemas que Site Reliability Engineering
aplica a servicios tradicionales, pero para modelos.

## Arquitectura

```
Frontend (Vite + TypeScript)
  → FastAPI (api/main.py)
     → Orchestrator: orquestación, routing y streaming SSE
     → Planner: planificación y aplicación de cambios de código
     → Curator: contexto y memoria persistente (SQLite + ChromaDB)
     → Retriever: indexación, filesystem e impact mapping
     → LLMRouter: providers, fallback, rate limiting, circuit breaker
     → Security Gate: clasificación de riesgo + approval flow
     → MCPManager: catálogo y ejecución de herramientas
```

Detalles de cada capa: [`VCORE_ARCHITECTURE.md`](VCORE_ARCHITECTURE.md).

## Características clave

- **Multi-provider con fallback real.** NVIDIA NIM como proveedor principal
  con 4 API keys rotando (credential pooling). Si una se agota o falla, el
  circuit breaker la aisla y el fallback chain toma el siguiente proveedor —
  sin intervención manual.
- **Circuit breaker con política por código HTTP.** 429 se trata como
  transitorio (no cuenta contra el breaker), 500/502/503/504 como persistente,
  401/403 como fatal. Política derivada de pruebas reales en NIM.
- **Routing por roles.** El modelo que planifica (Planner) no es el que
  orquesta la conversación (Orchestrator) ni el que toma decisiones de escalación
  (escalation tier). Hot-swap en caliente vía `model_routing.yaml`,
  sin reiniciar el server.
- **Model presets con quirks por modelo.** Cada modelo en
  `model_routing.yaml` tiene su propio preset: context window, latencia
  esperada, `tool_choice`, `max_tool_calls`, temperatura y "mañas"
  operacionales aprendidas. Ej — Kimi K2.6 con `tool_choice=auto` alucina;
  con `required` + `max_tool_calls=1` funciona. Esa clase de conocimiento
  no está en la doc oficial de los proveedores.
- **Security gate declarativo.** `gate.py` + `gate_rules.yaml` clasifican
  cada tool call en dos niveles (Nivel A auto-aprobado, Nivel B requiere
  aprobación humana). Un `agent_path_allowlist` controla qué agente puede
  escribir dónde. Fail-safe: agente sin entrada → Nivel B, no Nivel A.
- **Memoria semántica sin dependencia externa obligatoria.** `embed.py`
  con fallback de 3 niveles: Ollama → NIM → hash SHA-256. ChromaDB es
  opcional, no un hard dependency.
- **Auto-context guard.** Si la conversación excede el 80% del context
  window, trimma mensajes oldest-first manteniendo siempre un mínimo de 4.

## Estado del proyecto

Versión actual: **2.0.0** (fuente única: `VCORE_STATE.json` → `api/version.py`).

Roadmap activo: [`VCORE_ROADMAP.md`](VCORE_ROADMAP.md). Resumen de
prioridades:

- **P0**: approval flow ejecutable e idempotente + resiliencia ante fallo
  de proveedor — pendientes de prueba con servidor vivo.
- **P1**: validación E2E de session isolation, resistencia a prompt
  injection por contenido leído, regresión funcional del frontend.
- **P2**: compresión persistente de contexto, skills cross-session,
  delegación con contexto aislado, scheduler, multi-proyecto.
- **Deferred**: capa de escritorio con Tauri (no parte del runtime actual).

## Requisitos

- Python 3.11+ (el entorno de desarrollo corre sobre 3.14)
- Conexión a NVIDIA NIM (se espera `NVIDIA_KEY_MAIN` + claves de fallback
  en el entorno — ver `.env`)
- Opcional: Ollama local para embeddings y como capa de fallback

Stack de runtime (versiones verificadas contra el `.venv` de desarrollo):

| Dependencia | Versión usada | Para qué |
|---|---|---|
| FastAPI | 0.138 | API HTTP + SSE streaming |
| uvicorn | 0.49 | Servidor ASGI |
| ChromaDB | 1.5.9 | Recuperación semántica (opcional en runtime) |
| httpx | 0.28 | Cliente HTTP para NIM |
| pydantic | 2.13 | Validación |

La lista completa y con rangos está en [`requirements.txt`](requirements.txt).

## Inicio rápido

```bash
# 1. Clonar
git clone https://github.com/ViSchock66/v_core.git
cd v_core

# 2. Crear venv
python -m venv .venv
# En Windows:
.venv/Scripts/activate
# En Linux/macOS:
source .venv/bin/activate

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar credenciales (no se commitean)
cp .env.example .env        # y completar las claves
# Se espera al menos: NVIDIA_KEY_MAIN, y las de fallback
# (NVIDIA_KEY_FALLBACK, NVIDIA_KEY_COMPRESS, NVIDIA_KEY_AUX)

# 5. Compilar el frontend (genera web_dist/)
npm ci --prefix web
npm run build --prefix web

# 6. Arrancar — el backend sirve el frontend en la raíz
uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload

# 7. Abrir la interfaz
# http://127.0.0.1:8001/    <-- el frontend se sirve desde el propio backend
```

> **El frontend se sirve desde el backend.** La UI (build de `web/` en
> `web_dist/`) deriva la URL de la API de `window.location`, así que sólo
> funciona abierta en el mismo origen que expone los endpoints
> (`http://127.0.0.1:8001/`). Servir el build con un servidor estático en
> otro puerto **no funciona**: las llamadas a la API apuntarían al origen
> equivocado.

**Nota Windows (bash/MSYS):** el PYTHONPATH debe apuntar al venv de V-Core,
no al de otro entorno (p.ej. el de Hermes Agent):

```bash
PYTHONPATH=".venv/Lib/site-packages;." .venv/Scripts/uvicorn api.main:app \
  --host 127.0.0.1 --port 8001
```

Si `pydantic` falla con `_pydantic_core module not found`, es que el Python
cargó el venv equivocado. Verificar `which python`.

## Endpoints principales

| Ruta | Descripción |
|---|---|
| `GET /health` | Health check básico |
| `POST /agents/route` | Enviar mensaje — responde vía SSE (es el endpoint que usa el frontend) |
| `GET /system/model` | Modelo lead activo + configuración del routing |
| `GET /system/health` | Estado extendido del sistema |
| `POST /system/model` | Hot-swap de modelo por rol |
| `PATCH /sessions/{id}` | Renombrar una conversación |
| `GET /approvals` | Listar approvals pendientes |
| `POST /approvals/{id}/approve` | Aprobar una acción en espera |
| `POST /llm/circuit-status/reset` | Forzar todos los circuit breakers a CLOSED |

La lista completa de routers vive en `api/main.py` (cambia al registrar
nuevos routers, no la mantenemos en este README).

## Estructura del repo

```
V-Core/
├── api/                FastAPI entry + routers
│   ├── main.py         Entry point (uvicorn)
│   ├── llm_client.py   LLMRouter, providers, circuit breaker
│   ├── embed.py        Embeddings unificado (Ollama → NIM → SHA-256)
│   └── version.py      Fuente única de versión
├── agents/             Identidad de los 4 agentes
│   ├── Orchestrator/          Orquestación + streaming + tool loop
│   ├── Planner/           Planificación + aplicación de cambios
│   ├── Curator/        Persistencia (SQLite + ChromaDB) + memoria
│   └── Retriever/         Búsqueda, indexación, impact mapping
├── web/                UI (Vite + TypeScript; build → web_dist/, ignorado)
├── system/             MCP manager, observabilidad, proactive agent
├── gate.py             Security gate (zona protegida)
├── gate_rules.yaml     Reglas declarativas del gate
├── model_routing.yaml  Routing por roles + presets + fallback (zona protegida)
├── sessions/default/   Plantilla de configuración de sesión
├── scripts/            Diagnósticos, init DB, tests
├── docs/               Documentación vigente
├── VCORE_ARCHITECTURE.md
├── VCORE_ROADMAP.md
└── VCORE_STATE.json    Estado mutable (versión, agentes, modelos)
```

## Documentación

| Doc | Qué contiene |
|---|---|
| [`VCORE_ARCHITECTURE.md`](VCORE_ARCHITECTURE.md) | Estructura estable del sistema |
| [`VCORE_ROADMAP.md`](VCORE_ROADMAP.md) | Backlog activo con prioridades P0/P1/P2 |
| [`AGENTS.md`](AGENTS.md) | Contexto para agentes que trabajan en el repo |
| [`docs/DOCUMENTATION.md`](docs/DOCUMENTATION.md) | Jerarquía documental |
| [`docs/_archive/`](docs/_archive/) | Documentos históricos — registro, no operativo |

## Configuración

Toda la configuración operacional usa **3-point config sync** — tres
fuentes que deben concordar:

1. `model_routing.yaml` (global)
2. `sessions/default/model_routing.yaml` (plantilla de sesión — **fuente de
   verdad**; si los otros dos divergen, se resincronizan desde aquí)
3. `VCORE_STATE.json` (estado mutable: versión, modelo activo, agentes)

Para cambiar el modelo lead, editar `sessions/default/model_routing.yaml`
→ se propaga al global + state. No editar el global directamente.

**Archivos protegidos** (no tocar sin `council mode`):

- `gate.py`, `gate_rules.yaml` (excepto el allowlist)
- `.env`
- `api/llm_client.py`
- `model_routing.yaml`

## Desarrollo

```bash
# Backend
uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload

# Verificar
curl http://127.0.0.1:8001/health
python -m compileall -q api agents system scripts
npm --prefix web run typecheck

# Migración de schema (idempotente)
python scripts/init_db.py

# Matar server fantasma en el puerto (Windows)
netstat -ano | findstr :8001
taskkill //F //PID <pid>

# Bump de versión (toca solo VCORE_STATE.json)
python -c "from api.version import bump; bump('1.6.0')"
```

### Integración continua

`.github/workflows/ci.yml` corre en cada push y PR a `main` / `v0.3-dev`:

- **Backend** (matriz 3.11–3.13): compila todos los módulos, verifica que
  `api.main` importe y hace smoke test de los endpoints base con `TestClient`.
- **Frontend**: instala las dependencias de `web/` y corre el typecheck de
  TypeScript (`npm run typecheck`).

Todavía no hay suite de tests unitarios — está en el roadmap
([`VCORE_ROADMAP.md`](VCORE_ROADMAP.md)).

## Cómo se construyó

V-CORE se desarrolló con asistencia de agentes de IA orquestados por su autor.
Los documentos de [`docs/`](docs/) — el plan de refactor, el diseño del
subsistema de permisos, la comparativa con harnesses del estado del arte y la
auditoría de seguridad previa a esta publicación — muestran el flujo de
trabajo real: los agentes proponen y ejecutan, una persona decide y verifica.
El propio V-CORE (orquestador de LLMs) fue a la vez la herramienta y el
resultado del proceso.

## Licencia

MIT — ver [`LICENSE`](LICENSE).

## Maintainer

Vicente — [@ViSchock66](https://github.com/ViSchock66)
