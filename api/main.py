"""
api/main.py
-----------
Backend FastAPI de V-CORE.

Arquitectura:
    uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload

El puerto canonico vive en api/ports.py (override con VCORE_PORT). El CLI,
los MCP servers y ENLIL lo resuelven desde ahi para no volver a divergir.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

# Windows: cuando stdout no es una consola interactiva (pipe, redireccion a
# archivo, servicio, CI) Python lo abre con la codificacion local (cp1252) y
# cualquier print() con emoji (⚠, ✅) revienta con UnicodeEncodeError. Ese
# error se propagaba desde VCoreTracer.__init__ dentro del agent loop de
# ENLIL y mataba el chat antes de llegar al modelo. Forzar UTF-8 en los dos
# streams elimina la clase entera de bug. errors="replace" garantiza que
# nunca mas un print pueda tumbar una request.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
del _stream

# Cargar .env antes que cualquier otro import que use variables de entorno
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api import state_bridge as sb
from api.llm_client import get_router


# ---------------------------------------------------------------------------
# Session Isolation — filesystem-based (v1.4)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
SESSIONS_DIR = BASE_DIR / "sessions"
DEFAULT_SESSION = SESSIONS_DIR / "default"
ARCHIVE_DIR = SESSIONS_DIR / "_archive"


def _ensure_default_session() -> None:
    """Crea `sessions/default/` si no existe. Plantilla de configuración de sesión.

    BUG CORREGIDO (2026-09-23): la versión anterior, cuando `sessions/default/`
    ya existía, comparaba el modelo lead del template con el del `model_routing.yaml`
    global y —si diferían— **copiaba el template SOBRE el global**:

        if d_lead and g_lead and d_lead != g_lead:
            shutil.copy2(default_yaml, global_yaml)

    El efecto real era que cada arranque del servidor revertía silenciosamente
    cualquier cambio hecho en `model_routing.yaml`. Cualquier edición del routing
    (hot-swap, edición manual, actualización de modelos) se perdía al reiniciar.
    Además `shutil.copy2` preserva el mtime, así que el archivo sobrescrito
    aparentaba no haber sido tocado: el síntoma era confuso y difícil de rastrear.

    La dirección correcta es la inversa: **el global es la fuente de verdad** y el
    template se sincroniza desde él, no al revés. Sincronizar el template es
    barato y no destructivo, porque las sesiones nuevas se clonan de ahí.
    """
    if DEFAULT_SESSION.exists():
        # El global manda: propagar al template para que las sesiones nuevas
        # hereden la configuración vigente.
        global_yaml = BASE_DIR / "model_routing.yaml"
        default_yaml = DEFAULT_SESSION / "model_routing.yaml"
        if global_yaml.exists():
            try:
                shutil.copy2(global_yaml, default_yaml)
            except Exception:
                pass
        return
    DEFAULT_SESSION.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    # Copiar archivos de config desde raíz
    for src_name in ["model_routing.yaml", "gate_rules.yaml"]:
        src = BASE_DIR / src_name
        if src.exists():
            shutil.copy2(src, DEFAULT_SESSION / src_name)

    # state.json inicial
    from api.version import VERSION
    state = {
        "version": VERSION,
        "session_id": "default",
        "proyecto_activo": "V-CORE",
        "fecha_actualizacion": datetime.now(timezone.utc).isoformat(),
    }
    (DEFAULT_SESSION / "state.json").write_text(json.dumps(state, indent=2))


def _clone_session(session_id: str) -> Path:
    """Clona sessions/default/ → sessions/<session_id>/ y prepara su workspace.

    Estructura resultante de cada sesión:

        sessions/sess_205_ab12cd34/
          state.json            estado de la sesión (lo lee el state_bridge)
          model_routing.yaml    router aislado por sesión
          gate_rules.yaml       política aislada por sesión
          work/                 ← WORKSPACE: donde el agente escribe de verdad
          artifacts/            artefactos producidos (fuera del workspace, para
                                que no ensucien el árbol de trabajo)
          meta.json             metadatos: cwd, modelo lead, timestamps

    Sin `work/` y `artifacts/`, `sessions/<id>/` era un perfil de configuración,
    no un espacio de trabajo: el agente escribía en la raíz del repo porque no
    tenía otro lugar. El aislamiento fuerte (git worktree por sesión) se apoya
    sobre este directorio.
    """
    _ensure_default_session()
    dest = SESSIONS_DIR / session_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(DEFAULT_SESSION, dest)
    # Actualizar state.json con el session_id real
    state_path = dest / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        state["session_id"] = session_id
        state["fecha_actualizacion"] = datetime.now(timezone.utc).isoformat()
        state_path.write_text(json.dumps(state, indent=2))

    prepared = _ensure_session_workspace(dest)
    if prepared.get("work_dir"):
        try:
            state = json.loads(state_path.read_text())
            state["workspace"] = prepared["work_dir"]
            state_path.write_text(json.dumps(state, indent=2))
        except Exception:
            pass
    return dest


def _ensure_session_workspace(session_path: Path) -> dict[str, Any]:
    """Crea (idempotente) `work/`, `artifacts/` y `meta.json` de una sesión.

    Se separa de `_clone_session` para poder aplicarlo también a las sesiones
    que ya existen y fueron creadas antes de que el workspace existiera.
    """
    if not session_path.exists():
        return {}
    work_dir = session_path / "work"
    artifacts_dir = session_path / "artifacts"
    meta_path = session_path / "meta.json"
    for d in (work_dir, artifacts_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    meta.setdefault("session_dir", session_path.name)
    meta["work_dir"] = str(work_dir)
    meta["artifacts_dir"] = str(artifacts_dir)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    meta.setdefault("created_at", meta["updated_at"])
    try:
        # El modelo lead activo se consulta, no se hardcodea.
        meta["lead_model"] = (get_router().config.get("roles", {})
                              .get("enlil_lead", {}).get("model"))
    except Exception:
        pass
    try:
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return {"work_dir": str(work_dir), "artifacts_dir": str(artifacts_dir), "meta": meta}


def _archive_session(session_id: str) -> bool:
    """Mueve sessions/<id>/ → sessions/_archive/<id>/."""
    src = SESSIONS_DIR / session_id
    if not src.exists():
        return False
    dest = ARCHIVE_DIR / session_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(src), str(dest))
    return True


def _get_session_config_path(session_id: str | None) -> Path:
    """Retorna el path al model_routing.yaml de una sesión."""
    if session_id:
        session_dir = SESSIONS_DIR / session_id
        if session_dir.exists():
            cfg = session_dir / "model_routing.yaml"
            if cfg.exists():
                return cfg
    # Fallback al default
    _ensure_default_session()
    return DEFAULT_SESSION / "model_routing.yaml"


def _get_session_router(session_id: str | None = None):
    """Retorna un LLMRouter para la sesión especificada."""
    config_path = str(_get_session_config_path(session_id))
    # Forzar nuevo router con la config de la sesión
    from api.llm_client import LLMRouter
    return LLMRouter(config_path=config_path)


# ---------------------------------------------------------------------------
# /git/status — Git status endpoint
# ---------------------------------------------------------------------------

# Leer versión desde fuente única (api/version.py)
from api.version import VERSION as _V

_V = _V

app = FastAPI(
    title="V-CORE API",
    version=_V,
    description=f"Backend V-CORE v{_V} — agentes, task graphs, approvals, LLM usage, hot-swap.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:4173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# /system/health — Proactive agent health check
# ---------------------------------------------------------------------------

@app.get("/system/health")
async def system_health():
    """Health check completo del sistema + sugerencias proactivas."""
    from system.proactive_agent import ProactiveAgent
    agent = ProactiveAgent()
    return agent.check()


@app.get("/system/health/summary")
async def system_health_summary():
    """Resumen legible de salud del sistema."""
    from system.proactive_agent import get_health_summary
    summary = await get_health_summary()
    return {"summary": summary}


@app.post("/system/suggest")
async def system_suggest():
    """Fuerza una sugerencia proactiva inmediata (llamado desde ENLIL)."""
    from system.proactive_agent import ProactiveAgent
    agent = ProactiveAgent()
    result = agent.check()
    suggestions = result.get("suggestions", [])
    alerts = result.get("alerts", [])
    return {
        "suggestions": suggestions[:2],
        "alerts": alerts[:2],
        "inactivity": result.get("inactivity_seconds", 0),
    }


# ---------------------------------------------------------------------------
# /system/model — Hot-swap de modelos en caliente
# ---------------------------------------------------------------------------

@app.get("/system/model")
async def get_active_models():
    """Retorna los modelos activos de cada rol + estado de circuit breakers."""
    from api.llm_client import get_router
    router = get_router()
    return router.get_active_models()


@app.get("/system/model/available")
async def get_available_models(provider: str = "nvidia"):
    """Lista modelos disponibles para selección en el frontend."""
    from api.llm_client import get_router
    router = get_router()
    return {"provider": provider, "models": router.get_available_models(provider)}


class ModelSwapRequest(BaseModel):
    role: str
    model: str
    provider: str | None = None
    temperature: float | None = None


@app.post("/system/model")
async def hot_swap_model(body: ModelSwapRequest):
    """
    Cambia el modelo de un rol en caliente.
    Resetea circuit breaker automáticamente. Persiste en YAML + STATE.
    El siguiente request al backend ya usa el modelo nuevo.
    """
    from api.llm_client import get_router
    router = get_router()
    try:
        result = router.hot_swap(role=body.role, model=body.model, provider=body.provider, temperature=body.temperature)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Hot-swap failed: {str(e)}")


@app.post("/sessions/{session_dir}/model")
async def session_hot_swap_model(session_dir: str, body: ModelSwapRequest):
    """
    Hot-swap de modelo para UNA sesión específica (v1.4).
    Escribe en sessions/<session_dir>/model_routing.yaml.
    No afecta otras sesiones ni el default global.
    """
    config_path = _get_session_config_path(session_dir)
    if not config_path.exists():
        raise HTTPException(status_code=404, detail=f"Sesión '{session_dir}' no encontrada")

    from api.llm_client import LLMRouter
    router = LLMRouter(config_path=str(config_path))
    try:
        result = router.hot_swap(role=body.role, model=body.model, provider=body.provider, temperature=body.temperature)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Session hot-swap failed: {str(e)}")


@app.on_event("startup")
async def startup() -> None:
    _ensure_default_session()
    sb.read_gate_log(limit=1)  # inicializa tabla gate_log si no existe
    # Sincronizar VCORE_STATE.json desde model_routing.yaml (fuente de verdad para modelos)
    try:
        from api.llm_client import get_router
        router = get_router()
        state = sb.read_state()
        roles = router.config.get("roles", {})
        role_to_key = {
            "enlil_lead": "enlil_lead_model",
            "enlil_council": "enlil_council_model",
            "enlil_escalation": "enlil_escalation_model",
            "enki_plan": "enki_plan_model",
            "enki_apply": "enki_apply_model",
            "shamash": "shamash_model",
        }
        for role, key in role_to_key.items():
            if role in roles:
                state[key] = roles[role].get("model", state.get(key, ""))
        state["fecha_actualizacion"] = datetime.now(timezone.utc).isoformat()
        sb.write_state(state)
    except Exception as e:
        print(f"[V-CORE] State sync skip: {e}")
    # Recovery de grafos colgados
    try:
        from system.task_graph_engine import TaskGraphEngine
        engine = TaskGraphEngine()
        recovered = engine.startup_recovery()
        if recovered:
            print(f"[V-CORE] Recovery: {len(recovered)} tareas recuperadas")
    except Exception as e:
        print(f"[V-CORE] Recovery skip: {e}")
    print(f"[V-CORE] API v{_V} iniciada")


# ---------------------------------------------------------------------------
# Modelos de request
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    message: str
    history: list[dict] = []
    task_id: str = ""
    conversation_id: str = ""
    session_dir: str = ""  # v1.4: directorio de sesión aislada
    attachments: list[dict] = []  # Pipe 2: archivos adjuntos [{path, type, name}]
    command: str = ""  # H-04: Comando CLI (/loop, /audit, /fix, etc.)


class StateUpdateRequest(BaseModel):
    ultima_accion_real: str | None = None
    proyecto_activo: str | None = None
    enlil_backend: str | None = None


class ApprovalRequest(BaseModel):
    agent_id: str
    tool: str
    params: dict[str, Any]
    reason: str


class GraphExecuteRequest(BaseModel):
    auto_approve: bool = False


# ---------------------------------------------------------------------------
# /health  ← MOVIDO AL INICIO, antes de cualquier mount
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok", "version": _V}


# ---------------------------------------------------------------------------
# /agents — ENLIL entry point
# ---------------------------------------------------------------------------


@app.get("/agents/{name}/capabilities")
async def agent_capabilities(name: str) -> dict[str, Any]:
    """Capacidades de un agente especifico."""
    caps = {
        "ENLIL": {
            "description": "Orquestador central. Routing, clasificacion, auditoria, council.",
            "status": "operativo",
            "methods": ["route", "classify", "council_decide", "get_status"],
        },
        "SHAMASH": {
            "description": "Context Manager. Inyeccion de contexto, lecciones, resumen de archivos.",
            "status": "operativo",
            "methods": [
                "inject_project_context", "get_architecture", "get_recent_work",
                "summarize_file", "record_lesson", "record_quality", "estimate_repo_size",
            ],
        },
        "ENKI": {
            "description": "Programador. Plan -> Apply -> Verify.",
            "status": "rudimentario",
            "methods": ["plan_diff", "apply_diff", "shadow_verify"],
        },
        "NISABA": {
            "description": "RAG + Filesystem + Impact Mapping.",
            "status": "rudimentario",
            "methods": ["search", "index_incremental", "get_file_tree", "get_impact_map"],
        },
    }
    agent = caps.get(name.upper())
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agente '{name}' no encontrado")
    return {"agent": name.upper(), **agent}


# E-05: Endpoint stub eliminado — usar POST /agents/route para interactuar con agentes

# Session Isolation: locks por sesión para evitar race conditions
import threading
_session_locks: dict[str, threading.Lock] = {}


def _get_session_lock(session_dir: str) -> threading.Lock:
    """Retorna (o crea) un lock threading para la sesión especificada."""
    if session_dir not in _session_locks:
        _session_locks[session_dir] = threading.Lock()
    return _session_locks[session_dir]


@app.post("/agents/route")
async def agents_route(body: ChatMessage) -> StreamingResponse:
    """
    Entry point principal. Recibe mensaje del usuario y responde via SSE.
    ENLIL clasifica y responde directo (simple) o genera y ejecuta Task Graph (complejo).
    
    Si session_dir está presente (v1.4), usa el model_routing.yaml de esa sesión
    y adquiere un lock por sesión para evitar race conditions entre requests concurrentes.
    """
    from agents.ENLIL.enlil import ENLIL
    
    # Session Isolation: lock + inyectar router de sesión si corresponde
    session_lock = _get_session_lock(body.session_dir) if body.session_dir else None
    saved_router = None
    
    async def event_stream() -> AsyncGenerator[str, None]:
        nonlocal saved_router

        # Event sourcing: cada run produce un log append-only de eventos tipados.
        # Se persisten para que el cliente pueda reconstruir la conversación
        # (tool cards, artefactos, aprobaciones) y reconectar desde un `seq`.
        from api.events import (
            AgentEvent, EventType, RunRecorder, canonical_from_internal,
        )
        run_id = body.task_id or f"run_{uuid.uuid4().hex[:12]}"
        thread_id = body.session_dir or "default"
        recorder = RunRecorder(run_id=run_id, thread_id=thread_id)

        def _emit(kind: str, payload: dict) -> str:
            """Numera, persiste y serializa un evento en el formato del cliente.

            Dos formatos a la vez, a propósito:

            - **Canónico** (persistido y expuesto por `/threads/{id}/events`): es
              el contrato del protocolo. El payload que se guarda NO repite
              `type`, porque el tipo ya vive en el envelope.
            - **Legacy** (lo que viaja por SSE): incluye `type` dentro del objeto
              porque es lo que el frontend actual lee. Cuando el frontend nuevo
              esté validado, esta rama se puede borrar sin tocar el log.
            """
            ev = recorder.record(AgentEvent(type=kind, data=dict(payload)))
            legacy = dict(payload)
            legacy["type"] = kind
            legacy["seq"] = ev.seq
            legacy["id"] = ev.id
            legacy["run_id"] = ev.run_id
            legacy["thread_id"] = ev.thread_id
            return f"data: {json.dumps(legacy, ensure_ascii=False)}\n\n"

        # Adquirir lock de sesión antes de tocar el router
        if session_lock is not None:
            session_lock.acquire()

        try:
            # Session Isolation: inyectar router de sesión
            if body.session_dir:
                from api.llm_client import get_router, set_router, LLMRouter
                session_cfg = _get_session_config_path(body.session_dir)
                saved_router = get_router()  # preservar singleton
                set_router(LLMRouter(config_path=str(session_cfg)))

            yield _emit(EventType.RUN_START, {
                "run_id": run_id,
                "thread_id": thread_id,
                "model": (get_router().config.get("roles", {})
                          .get("enlil_lead", {}).get("model")),
                "message_preview": (body.message or "")[:200],
            })

            enlil = ENLIL()
            try:
                async for chunk in enlil.route(
                    message=body.message,
                    history=body.history,
                    task_id=body.task_id,
                    attachments=body.attachments,
                    command=body.command,
                    session_dir=body.session_dir,
                ):
                    # Se traduce el evento interno al protocolo canónico para
                    # persistirlo, y se emite el payload plano para el cliente.
                    canonical = canonical_from_internal(chunk)
                    if canonical is not None:
                        recorder.record(canonical)

                    if isinstance(chunk, dict) and "__event__" in chunk:
                        event_type = chunk.pop("__event__")
                        payload = {"type": event_type, **chunk}
                    else:
                        payload = {"type": "chunk", "content": chunk}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                yield _emit(EventType.RUN_END, {"reason": "completed"})

            except Exception as e:
                import traceback
                traceback.print_exc()
                yield _emit(EventType.RUN_ERROR, {
                    "reason": "exception", "detail": f"{type(e).__name__}: {str(e)[:200]}",
                })
                yield _emit(EventType.RUN_END, {"reason": "error"})
            finally:
                # Restaurar router global
                if saved_router is not None:
                    set_router(saved_router)
                recorder.close()
        finally:
            if session_lock is not None:
                session_lock.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/agents/status")
async def agents_status() -> dict[str, Any]:
    """Estado de ENLIL y circuit breakers de todos los providers."""
    from agents.ENLIL.enlil import ENLIL
    enlil = ENLIL()
    return enlil.get_status()


@app.post("/agents/council")
async def agents_council(body: ChatMessage) -> dict[str, Any]:
    """
    Council mode: dispara Lead + Council en paralelo.
    Retorna consenso o ambas respuestas si divergen.
    """
    from agents.ENLIL.enlil import ENLIL
    enlil = ENLIL()
    return await enlil.council_decide(
        question=body.message,
        task_id=body.task_id,
    )


# ---------------------------------------------------------------------------
# /graph — Task Graphs (M2)
# ---------------------------------------------------------------------------

@app.get("/graph/{task_id}/status")
async def graph_status(task_id: str) -> dict[str, Any]:
    """Estado del Task Graph por task_id — busca en task_graphs y approvals."""
    # Primero buscar en task_graphs
    try:
        import sqlite3
        from system.task_graph_engine import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT status, definition_json, created_at, updated_at FROM task_graphs WHERE id = ?",
            (task_id,)
        )
        row = cur.fetchone()
        if row:
            defn = json.loads(row["definition_json"]) if row["definition_json"] else {}
            # B-03: Consultar agent_execution para estado por nodo
            nodes = []
            cur = conn.cursor()
            cur.execute(
                "SELECT node_id, agent_name, action, status, error_msg, duration_s, tokens_used, tokens_in, tokens_out "
                "FROM agent_execution WHERE task_id = ? ORDER BY timestamp ASC",
                (task_id,)
            )
            for ex_row in cur.fetchall():
                frontend_status = {
                    "SUCCESS": "done",
                    "FAILED": "error",
                    "RUNNING": "running",
                    "AWAITING_APPROVAL": "pending",
                    "SKIPPED": "done",
                }.get(ex_row["status"], "pending")
                nodes.append({
                    "id": ex_row["node_id"],
                    "agent": ex_row["agent_name"],
                    "action": ex_row["action"],
                    "status": frontend_status,
                    "duration_ms": int(ex_row["duration_s"] * 1000) if ex_row["duration_s"] else 0,
                    "tokens_used": ex_row["tokens_used"] or 0,
                    "token_budget": 1000,
                    "error": ex_row["error_msg"],
                })
            conn.close()
            return {
                "task_id": task_id,
                "source": "task_graphs",
                "status": row["status"],
                "task_type": defn.get("task_type"),
                "description": defn.get("description"),
                "node_count": len(defn.get("nodes", [])),
                "nodes": nodes,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
    except Exception:
        pass

    # Fallback a approvals
    approvals = sb.list_approvals(status=None)
    for a in approvals:
        params = json.loads(a.get("params_json", "{}"))
        if params.get("task_id") == task_id:
            return {
                "task_id": task_id,
                "source": "approvals",
                "approval_id": a["id"],
                "status": a["status"],
                "task_type": params.get("task_type"),
                "description": params.get("description"),
                "node_count": params.get("node_count"),
            }
    raise HTTPException(status_code=404, detail=f"Task Graph '{task_id}' no encontrado")


@app.post("/graph/{task_id}/execute")
async def graph_execute(task_id: str, body: GraphExecuteRequest) -> StreamingResponse:
    """
    Ejecuta un Task Graph directamente (para reanudar o disparar manualmente).
    """
    from system.task_graph_engine import TaskGraphEngine
    engine = TaskGraphEngine()

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            definition, _ = engine._load_graph_definition(task_id)
            if not definition:
                error = json.dumps({"type": "error", "content": f"Grafo {task_id} no encontrado"})
                yield f"data: {error}\n\n"
                return

            NL = "\n"
            yield f"data: {json.dumps({'type': 'chunk', 'content': f'🚀 Reanudando Task Graph {task_id}...{NL}'})}\n\n"

            result = await engine.execute_graph(
                task_id=task_id,
                graph_definition=definition,
                auto_approve=body.auto_approve,
            )

            for node in result.nodes:
                if node.status == "PENDING":
                    continue
                status_icon = "✅" if node.status == "SUCCESS" else "❌" if node.status == "FAILED" else "🔐"
                content = f"{status_icon} {node.id}: {node.agent}/{node.status}"
                yield f"data: {json.dumps({'type': 'chunk', 'content': content + NL})}\n\n"

            if result.success:
                yield f"data: {json.dumps({'type': 'chunk', 'content': '✅ Task Graph completado' + NL})}\n\n"
            elif result.awaiting_approval:
                yield f"data: {json.dumps({'type': 'chunk', 'content': f'🔐 Esperando aprobación en nodo {result.approval_node_id}' + NL})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'chunk', 'content': f'❌ Error: {result.error}' + NL})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            error_data = json.dumps({"type": "error", "content": str(e)})
            yield f"data: {error_data}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/graph/{task_id}/approve")
async def graph_approve(task_id: str) -> dict[str, Any]:
    """
    Aprueba un Task Graph pausado en AWAITING_APPROVAL y lo reanuda.
    """
    # Resolver approval asociado
    approvals = sb.list_approvals(status="pending")
    approval_id = None
    for a in approvals:
        params = json.loads(a.get("params_json", "{}"))
        if params.get("task_id") == task_id:
            approval_id = a["id"]
            break

    if approval_id:
        sb.resolve_approval(approval_id, status="approved")
        sb.update_ultima_accion(f"[APROBADO] Task Graph {task_id} | approval_id={approval_id}")

    # Reanudar ejecucion
    from system.task_graph_engine import TaskGraphEngine
    engine = TaskGraphEngine()
    result = await engine.resume_graph(task_id)

    return {
        "task_id": task_id,
        "approval_id": approval_id,
        "status": "approved_and_resumed",
        "graph_status": "DONE" if result.success else "FAILED" if not result.awaiting_approval else "AWAITING_APPROVAL",
        "nodes_executed": len([n for n in result.nodes if n.status != "PENDING"]),
        "total_nodes": len(result.nodes),
        "error": result.error,
    }


@app.post("/graph/{task_id}/reject")
async def graph_reject(task_id: str) -> dict[str, Any]:
    """
    Rechaza un Task Graph pausado en AWAITING_APPROVAL.
    """
    approvals = sb.list_approvals(status="pending")
    approval_id = None
    for a in approvals:
        params = json.loads(a.get("params_json", "{}"))
        if params.get("task_id") == task_id:
            approval_id = a["id"]
            break

    if approval_id:
        sb.resolve_approval(approval_id, status="rejected")
        sb.update_ultima_accion(f"[RECHAZADO] Task Graph {task_id} | approval_id={approval_id}")

    # Actualizar estado del grafo
    try:
        import sqlite3
        from system.task_graph_engine import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE task_graphs SET status = ?, updated_at = ? WHERE id = ?",
            ("REJECTED", time.time(), task_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return {
        "task_id": task_id,
        "approval_id": approval_id,
        "status": "rejected",
    }


# ---------------------------------------------------------------------------
# /llm — Control de LLM y circuit breakers
# ---------------------------------------------------------------------------

@app.get("/llm/circuit-status")
async def llm_circuit_status() -> dict[str, Any]:
    """Estado de todos los circuit breakers — para UsagePanel.jsx."""
    router = get_router()
    return router.get_circuit_status()


@app.post("/llm/circuit-status/reset")
async def llm_circuit_reset() -> dict[str, Any]:
    """Fuerza todos los circuit breakers a CLOSED.

    Lo llama el botón de reset del frontend (resetCircuitBreakers() en app.js).
    reset_circuit_breakers() opera sobre el singleton _router_instance, así que
    hay que forzar get_router() primero para garantizar que exista.
    """
    from api.llm_client import reset_circuit_breakers
    router = get_router()
    reset_circuit_breakers()
    return {"reset": True, "circuit_status": router.get_circuit_status()}


@app.get("/llm/usage")
async def llm_usage(limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    """Ultimas N llamadas LLM con tokens y costo estimado."""
    return sb.read_llm_usage(limit=limit)


@app.get("/llm/providers")
async def llm_providers() -> dict[str, Any]:
    """Roles configurados y sus providers — desde model_routing.yaml."""
    router = get_router()
    roles = {}
    for role, config in router.config.get("roles", {}).items():
        roles[role] = {
            "provider": config["provider"],
            "model": config["model"],
        }
    return {"roles": roles, "circuit_status": router.get_circuit_status()}


# ---------------------------------------------------------------------------
# /embeddings — estado de la capa vectorial (SHAMASH + NISABA)
# ---------------------------------------------------------------------------

@app.get("/embeddings/status")
def embeddings_status() -> dict[str, Any]:
    """Estado de la infraestructura de embeddings y de los índices vectoriales.

    Existe porque la capa fallaba en silencio: el tier 2 apuntaba a un modelo
    retirado por NVIDIA y el tier 3 declaraba 768 dimensiones devolviendo 32,
    así que la memoria y el RAG quedaron en espacios vectoriales incompatibles
    sin un solo error visible. La UI necesita poder mostrar esto.
    """
    from api.embed import CHROMA_DIR, collection_name, probe

    from agents.SHAMASH.memory import nem0

    status: dict[str, Any] = {"probe": probe(), "chroma_dir": str(CHROMA_DIR)}

    try:
        status["memory"] = nem0.stats()
    except Exception as e:
        status["memory"] = {"error": str(e)[:200]}

    try:
        from agents.NISABA.nisaba import COLLECTION_BASE as _NISABA_BASE
        from agents.NISABA.nisaba import NISABA

        col = NISABA().collection
        status["knowledge"] = {
            "collection": collection_name(_NISABA_BASE),
            "docs": col.count(),
            "dims": nem0._dims(),
        }
    except Exception as e:
        status["knowledge"] = {"error": str(e)[:200]}

    return status


@app.post("/embeddings/rebuild")
def embeddings_rebuild(target: str = Query(default="memory")) -> dict[str, Any]:
    """Reconstruye un índice vectorial desde su fuente de verdad.

    `target=memory` re-embebe los recuerdos desde SQLite (que guarda el texto
    completo). Es seguro e idempotente: los vectores son datos derivados.
    """
    if target == "memory":
        from agents.SHAMASH.memory import nem0

        return nem0.rebuild_index()
    raise HTTPException(status_code=400, detail=f"target no soportado: {target}")


# ---------------------------------------------------------------------------
# /observability — B3: Panel de observabilidad (traces + session summary)
# ---------------------------------------------------------------------------

@app.get("/observability/traces")
async def observability_traces(limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    """Últimas N trazas del log local JSONL."""
    from system.observability import get_tracer
    tracer = get_tracer()
    return tracer.get_recent_traces(limit)


@app.get("/observability/session/{task_id}")
async def observability_session(task_id: str) -> dict:
    """Resumen de sesión para un task_id dado."""
    from system.observability import get_tracer
    tracer = get_tracer()
    return tracer.get_session_summary(task_id)


@app.get("/observability/status")
async def observability_status() -> dict:
    """Estado del tracer: Langfuse activo/inactivo, tamaño del log local."""
    from system.observability import get_tracer
    tracer = get_tracer()
    traces = tracer.get_recent_traces(1)
    return {
        "langfuse_enabled": tracer.langfuse_enabled,
        "local_traces_count": len(tracer.get_recent_traces(500)),
        "last_trace_ts": traces[0]["ts"] if traces else None,
    }


# ---------------------------------------------------------------------------
# /state
# ---------------------------------------------------------------------------

@app.get("/state")
def get_state() -> dict[str, Any]:
    return sb.read_state()


@app.patch("/state")
def patch_state(body: StateUpdateRequest) -> dict[str, Any]:
    state = sb.read_state()
    if body.ultima_accion_real is not None:
        state["ultima_accion_real"] = body.ultima_accion_real
    if body.proyecto_activo is not None:
        state["proyecto_activo"] = body.proyecto_activo
    if body.enlil_backend is not None:
        state["enlil_backend"] = body.enlil_backend
    sb.write_state(state)
    return state


# ---------------------------------------------------------------------------
# /agents (legacy — lista de agentes del yaml)
# ---------------------------------------------------------------------------

@app.get("/agents")
def get_agents() -> list[dict[str, Any]]:
    return sb.read_agents()


# ---------------------------------------------------------------------------
# /sessions — persistencia de conversaciones
# ---------------------------------------------------------------------------

_sessions_title_ready = False


def _ensure_sessions_title_column() -> None:
    """Migracion lazy e idempotente: sessions.title.

    SQLite no soporta "ADD COLUMN IF NOT EXISTS", asi que se intenta una vez
    por proceso y se ignora el error de columna duplicada. Mismo patron que
    scripts/init_db.py::_add_column_if_missing. Necesario para que
    PATCH /sessions/{id} (renombrar conversacion) tenga donde persistir.
    """
    global _sessions_title_ready
    if _sessions_title_ready:
        return
    import sqlite3
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent / "vcore.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    finally:
        conn.close()
    _sessions_title_ready = True


@app.get("/sessions")
def get_sessions() -> list[dict[str, Any]]:
    """Lista de sesiones con su directorio aislado.

    Incluye `session_dir` porque es el identificador que el frontend necesita
    para el aislamiento por sesión: sin él, `/agents/route` no puede cargar el
    router ni el workspace de la sesión, y todas las conversaciones comparten la
    configuración global. El backend ya lo generaba al crear; no lo exponía.
    """
    import sqlite3
    from pathlib import Path
    _ensure_sessions_title_column()
    db = Path(__file__).resolve().parent.parent / "vcore.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, timestamp, project, status, title FROM sessions ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["session_dir"] = _resolve_session_dir(d["id"])
        out.append(d)
    return out


def _resolve_session_dir(session_id: int) -> str | None:
    """Devuelve el directorio aislado de una sesión, o None si no existe.

    El directorio se crea al crear la sesión (`sess_<id>_<hex8>`); para las
    sesiones históricas se busca por patrón. Devolver None es información útil:
    la UI puede mostrarlo como "sin workspace" en vez de fingir que existe.
    """
    import glob as _glob
    matches = sorted(_glob.glob(str(SESSIONS_DIR / f"sess_{session_id}_*")))
    return Path(matches[-1]).name if matches else None


def _session_key_to_id(session_key: str) -> int | None:
    """Traduce el identificador de sesión de la URL a su `id` numérico.

    Acepta las dos formas porque conviven en el sistema y en las URLs del
    frontend: el `id` numérico (`205`) y el nombre del directorio de sesión
    (`sess_205_a1b2c3d4`). Antes solo aceptaba `int` y un cliente que mandara el
    nombre del directorio recibía un 422 opaco en vez del historial.
    """
    key = (session_key or "").strip()
    if not key:
        return None
    if key.isdigit():
        return int(key)
    # Forma `sess_<id>_<hex>`
    parts = key.split("_")
    if len(parts) >= 2 and parts[0] == "sess" and parts[1].isdigit():
        return int(parts[1])
    return None


@app.post("/sessions")
def create_session() -> dict[str, Any]:
    import sqlite3, time, uuid
    from pathlib import Path
    _ensure_sessions_title_column()
    db = Path(__file__).resolve().parent.parent / "vcore.db"
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO sessions (timestamp, project, status, title) VALUES (?, ?, ?, ?)",
                 (str(time.time()), "V-CORE", "open", "Nueva conversación"))
    conn.commit()
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    # Session Isolation: clonar directorio
    session_dir_id = f"sess_{sid}_{uuid.uuid4().hex[:8]}"
    _clone_session(session_dir_id)

    return {"id": sid, "session_dir": session_dir_id, "title": "Nueva conversación", "agent": "ENLIL"}


@app.patch("/sessions/{session_key}")
def rename_session(session_key: str, body: dict[str, Any]) -> dict[str, Any]:
    """Renombra una conversacion. Lo llama renameConv() del frontend."""
    session_id = _session_key_to_id(session_key)
    if session_id is None:
        raise HTTPException(status_code=422,
                            detail=f"Identificador de sesión inválido: '{session_key}'")
    import sqlite3
    from pathlib import Path
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title vacío")
    _ensure_sessions_title_column()
    db = Path(__file__).resolve().parent.parent / "vcore.db"
    conn = sqlite3.connect(str(db))
    cur = conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
    conn.commit()
    updated = cur.rowcount
    conn.close()
    if not updated:
        raise HTTPException(status_code=404, detail=f"sesión {session_id} no existe")
    return {"renamed": True, "session_id": session_id,
            "session_dir": _resolve_session_dir(session_id), "title": title}


@app.delete("/sessions/{session_key}")
def delete_session(session_key: str) -> dict[str, Any]:
    """Elimina una sesión y su historial de chat."""
    session_id = _session_key_to_id(session_key)
    if session_id is None:
        raise HTTPException(status_code=422,
                            detail=f"Identificador de sesión inválido: '{session_key}'")
    import sqlite3
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent / "vcore.db"
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM chat_history WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()

    # Session Isolation: archivar directorios que matcheen sess_{id}_*
    import glob as _glob
    pattern = str(SESSIONS_DIR / f"sess_{session_id}_*")
    for match in _glob.glob(pattern):
        dir_name = Path(match).name
        _archive_session(dir_name)

    return {"deleted": True, "session_id": session_id}


@app.get("/sessions/{session_key}/messages")
def get_session_messages(session_key: str) -> dict[str, Any]:
    """Carga el historial de chat de una sesión.

    `session_key` acepta el `id` numérico o el nombre del directorio de sesión
    (`sess_205_ab12cd34`), que es lo que el frontend maneja de forma natural.
    """
    session_id = _session_key_to_id(session_key)
    if session_id is None:
        raise HTTPException(
            status_code=422,
            detail=f"Identificador de sesión inválido: '{session_key}'. "
                   f"Se espera un id numérico o el nombre del directorio (sess_<id>_<hex>).",
        )
    import sqlite3
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent / "vcore.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content, timestamp FROM chat_history "
        "WHERE session_id = ? ORDER BY timestamp ASC LIMIT 100",
        (session_id,)
    ).fetchall()
    conn.close()
    return {
        "session_id": session_id,
        "session_dir": _resolve_session_dir(session_id),
        "messages": [dict(r) for r in rows],
    }


@app.post("/sessions/{session_key}/messages")
def save_session_message(session_key: str, body: dict[str, Any]) -> dict[str, Any]:
    """Guarda un turno de chat (user o assistant) en el historial."""
    session_id = _session_key_to_id(session_key)
    if session_id is None:
        raise HTTPException(status_code=422,
                            detail=f"Identificador de sesión inválido: '{session_key}'")
    import sqlite3, time
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent / "vcore.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO chat_history (timestamp, role, content, session_id) VALUES (?, ?, ?, ?)",
        (time.time(), body.get("role"), body.get("content"), session_id)
    )
    conn.commit()
    conn.close()
    return {"saved": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# /approvals
# ---------------------------------------------------------------------------

@app.get("/approvals")
def get_approvals(
    status: str | None = Query(default="pending")
) -> list[dict[str, Any]]:
    filter_status = None if status == "all" else status
    return sb.list_approvals(status=filter_status)


@app.post("/approvals")
def post_approval(body: ApprovalRequest) -> dict[str, Any]:
    approval_id = sb.create_approval(
        agent_id=body.agent_id,
        tool=body.tool,
        params=body.params,
        reason=body.reason,
    )
    return {"id": approval_id, "status": "pending"}


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: int) -> dict[str, Any]:
    """Aprueba una accion pendiente Y LA EJECUTA.

    Fix de DT-00: antes este endpoint solo marcaba el registro como
    "approved" y nunca re-invocaba la tool. El Security Gate existia para que
    las acciones de riesgo pasaran por aprobacion humana, pero aprobar no
    ejecutaba nada: la feature de seguridad era un placebo de punta a punta
    (verificado creando un write_file y comprobando que el archivo nunca se
    creaba).

    Idempotencia (requisito explicito de DT-00): si el endpoint se reintenta
    -- timeout de red, doble click, retry del frontend -- NO debe volver a
    ejecutar la accion. Si el approval ya tiene result_json persistido, se
    devuelve ese resultado sin re-ejecutar.
    """
    row = sb.get_approval(approval_id)
    if not row:
        raise HTTPException(status_code=404, detail="Approval no encontrado")

    # Idempotencia: ya resuelto y con resultado -> devolver lo guardado.
    if row["status"] == "approved" and row.get("result_json"):
        try:
            return {
                "id": approval_id,
                "status": "approved",
                "already_executed": True,
                "result": json.loads(row["result_json"]),
            }
        except (ValueError, TypeError):
            return {"id": approval_id, "status": "approved", "already_executed": True}

    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Ya resuelto: {row['status']}")

    sb.resolve_approval(approval_id, status="approved")
    sb.update_ultima_accion(
        f"[APROBADO] {row['agent_id']} -> {row['tool']} | {row['reason']}"
    )

    # ── Re-ejecutar la tool aprobada con los params originales ──
    execution = _execute_approved_tool(row)
    sb.resolve_approval(approval_id, status="approved", result=execution)

    return {
        "id": approval_id,
        "status": "approved",
        "executed": execution.get("ok", False),
        "result": execution,
    }


def _execute_approved_tool(row: dict[str, Any]) -> dict[str, Any]:
    """Ejecuta la tool de un approval ya aprobado, con sus params guardados.

    Usa el mismo MCPClient que habria ejecutado la accion si el gate le
    hubiera dado Nivel A directamente. Nunca lanza: devuelve el resultado o
    el error, para que la aprobacion quede registrada con su desenlace (DT-00
    pide que un fallo post-aprobacion no revierta la aprobacion, pero si sea
    visible).
    """
    tool = row.get("tool") or ""
    try:
        params = json.loads(row.get("params_json") or "{}")
        if not isinstance(params, dict):
            params = {}
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": f"params_json inválido: {e}"}

    if not tool:
        return {"ok": False, "error": "approval sin tool"}

    # El gate clasifica con sus propios nombres (gate_rules.yaml), que no
    # siempre coinciden con los del catalogo MCP. Sin este mapeo, un approval
    # de shell se guardaba como "execute_shell" y al re-ejecutar respondia
    # "Herramienta desconocida: 'execute_shell'". Se mantiene el nombre del
    # gate (es la regla de seguridad) y se traduce solo al despachar.
    TOOL_ALIASES = {
        "execute_shell": "execute_command",
        "shell": "execute_command",
        "write": "write_file",
        "read": "read_file",
        "patch": "patch_file",
    }
    tool = TOOL_ALIASES.get(tool, tool)

    try:
        from agents.ENLIL.enlil import ENLIL
        import asyncio

        engine = ENLIL()

        async def _run() -> str:
            await engine._ensure_mcp()
            return await engine.mcp.call(tool, params)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Ya estamos dentro de un event loop: correr en un hilo aparte
            # para no anidar loops (asyncio.run fallaria).
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(lambda: asyncio.run(_run())).result(timeout=120)
        else:
            result = asyncio.run(_run())

        try:
            sb.update_ultima_accion(f"[EJECUTADO] {tool} | approval #{row['id']}")
        except Exception:
            pass
        return {"ok": True, "tool": tool, "output": str(result)[:2000]}

    except Exception as e:
        return {"ok": False, "tool": tool, "error": f"{type(e).__name__}: {e}"}


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: int) -> dict[str, Any]:
    row = sb.get_approval(approval_id)
    if not row:
        raise HTTPException(status_code=404, detail="Approval no encontrado")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Ya resuelto: {row['status']}")
    sb.resolve_approval(approval_id, status="rejected")
    sb.update_ultima_accion(
        f"[RECHAZADO] {row['agent_id']} -> {row['tool']} | {row['reason']}"
    )
    return {"id": approval_id, "status": "rejected"}


# ---------------------------------------------------------------------------
# /policy — estado y recarga de la política de permisos
# ---------------------------------------------------------------------------

@app.post("/system/reload-gate")
def reload_gate() -> dict[str, Any]:
    """Recarga la política de permisos desde gate_rules.yaml.

    Antes recargaba solo las instancias de `files_api` y `shell_api`, mientras
    `search_api`, `enki` y `nisaba` (y el agente) seguían con la política vieja
    en memoria: el endpoint respondía "reloaded" sin cambiar el comportamiento
    efectivo. Ahora hay una sola instancia (api/policy.py) y la recarga es real.
    """
    from api.policy import reload_policy
    return {"status": "reloaded", **reload_policy()}


@app.get("/policy/rules")
def policy_rules() -> dict[str, Any]:
    """Política vigente: catálogo de tools, allowlist de comandos y perímetro."""
    from api.policy import RULES_PATH, get_gate
    gate = get_gate()
    return {
        "rules_path": str(RULES_PATH),
        "workspace_dir": str(gate.workspace_dir),
        "allowed_roots": [str(p) for p in gate.allowed_roots],
        "tools": gate.tools,
        "command_params": gate.command_params,
        "command_allowlist": gate.command_allowlist,
        "perimeter": gate.rules.get("perimeter", {}),
    }


@app.get("/sessions/{session_key}/workspace")
def get_session_workspace(session_key: str) -> dict[str, Any]:
    """Workspace real de una sesión: rutas, contenido y estado.

    Es la superficie que necesita el frontend para dejar de ser decorativo en
    materia de workspaces: qué directorio usa el agente, qué produjo y cuánto hay.
    Crea la estructura si la sesión es anterior a que el workspace existiera.
    """
    session_id = _session_key_to_id(session_key)
    if session_id is None:
        raise HTTPException(status_code=422,
                            detail=f"Identificador de sesión inválido: '{session_key}'")

    session_dir = _resolve_session_dir(session_id)
    if not session_dir:
        return {"session_id": session_id, "session_dir": None, "exists": False,
                "detail": "La sesión no tiene directorio aislado"}

    path = SESSIONS_DIR / session_dir
    prepared = _ensure_session_workspace(path)
    work_dir = Path(prepared.get("work_dir", path / "work"))
    artifacts_dir = Path(prepared.get("artifacts_dir", path / "artifacts"))

    def _list(d: Path, limit: int = 50) -> list[dict[str, Any]]:
        if not d.exists():
            return []
        items = []
        try:
            for e in sorted(d.iterdir())[:limit]:
                items.append({
                    "name": e.name,
                    "type": "directory" if e.is_dir() else "file",
                    "size": e.stat().st_size if e.is_file() else None,
                })
        except Exception:
            pass
        return items

    return {
        "session_id": session_id,
        "session_dir": session_dir,
        "exists": True,
        "work_dir": str(work_dir),
        "artifacts_dir": str(artifacts_dir),
        "work_files": _list(work_dir),
        "artifacts": _list(artifacts_dir),
        "meta": prepared.get("meta", {}),
    }


# ---------------------------------------------------------------------------
# /threads — log de eventos (event sourcing) para replay y reconexión
# ---------------------------------------------------------------------------

@app.get("/threads/{thread_key}/events")
def thread_events(
    thread_key: str,
    after: int = Query(default=0, ge=0, description="Último seq que ya tiene el cliente"),
    limit: int = Query(default=2000, ge=1, le=10000),
) -> dict[str, Any]:
    """Log de eventos de una conversación, desde `after` exclusivo.

    Es lo que permite dos cosas que antes eran imposibles:

    1. **Replay fiel**: la UI reconstruye la conversación completa (tool cards,
       artefactos, aprobaciones y razonamiento) desde el log, no desde el texto.
       Antes esa información no se persistía en ningún lado.
    2. **Reconexión**: si el stream se corta, el cliente pide `?after=<último seq>`
       y recibe exactamente lo que le falta, sin duplicar ni perder eventos.
    """
    from api.events import EventStore
    store = EventStore()
    events = store.read_thread(thread_key, after_seq=after, limit=limit)
    return {
        "thread_id": thread_key,
        "after": after,
        "count": len(events),
        "next_after": events[-1]["seq"] if events else after,
        "events": events,
    }


@app.get("/threads/{thread_key}/summary")
def thread_summary(thread_key: str) -> dict[str, Any]:
    """Resumen de una conversación: cuántos eventos y de qué tipo, sin traerlos."""
    from api.events import EventStore
    return EventStore().summarize_thread(thread_key)


@app.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=2000, ge=1, le=10000),
) -> dict[str, Any]:
    """Log de eventos de un run puntual (una ejecución del agente)."""
    from api.events import EventStore
    events = EventStore().read_run(run_id, after_seq=after, limit=limit)
    return {
        "run_id": run_id,
        "after": after,
        "count": len(events),
        "next_after": events[-1]["seq"] if events else after,
        "events": events,
    }


# ---------------------------------------------------------------------------
# /approvals — HITL sincrónico sobre el stream del agente
# ---------------------------------------------------------------------------

class ApprovalResolveRequest(BaseModel):
    approved: bool


@app.get("/approvals/pending")
def approvals_pending() -> dict[str, Any]:
    """Aprobaciones esperando decisión humana en este proceso.

    Distinto de `GET /approvals` (que lee la tabla `approvals` del flujo
    asíncrono): estas están bloqueando un stream abierto ahora mismo.
    """
    from api.approval_broker import get_broker
    broker = get_broker()
    return {"pending": broker.pending(), "stats": broker.stats}


@app.post("/approvals/pending/{request_id}/resolve")
def approvals_resolve(request_id: str, body: ApprovalResolveRequest) -> dict[str, Any]:
    """Resuelve una aprobación pendiente y **reanuda el stream del agente**.

    Idempotente: si el `request_id` ya fue resuelto (doble click, reintento de
    red, replay), devuelve `resolved: false` sin ejecutar nada dos veces.
    """
    from api.approval_broker import get_broker
    resolved = get_broker().resolve(request_id, approved=body.approved)
    return {
        "request_id": request_id,
        "resolved": resolved,
        "approved": body.approved,
        "detail": None if resolved else "ya resuelto, inexistente o vencido",
    }


# ---------------------------------------------------------------------------
# /system/reload-gate (alias histórico)
# ---------------------------------------------------------------------------
# /log
# ---------------------------------------------------------------------------

@app.get("/log")
def get_log(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
    return sb.read_gate_log(limit=limit)


# ---------------------------------------------------------------------------
# Routers heredados (files, shell, search)
# ---------------------------------------------------------------------------

from api.files_api import router as files_router
from api.shell_api import router as shell_router
from api.search_api import router as search_router

app.include_router(files_router)
app.include_router(shell_router)
app.include_router(search_router)


# ---------------------------------------------------------------------------
# Background Tasks — ejecución larga con notificación al terminar
# ---------------------------------------------------------------------------

import asyncio as _asyncio_bg, uuid as _uuid, threading as _threading

# In-memory task store (dict: task_id → {status, result, created_at})
_bg_tasks: dict = {}
_bg_lock = _threading.Lock()

def _run_bg_task(task_id: str, command: str, workdir: str):
    """Ejecuta un comando shell en thread separado y guarda el resultado."""
    import subprocess
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=300, cwd=workdir or str(Path(__file__).resolve().parent.parent)
        )
        with _bg_lock:
            _bg_tasks[task_id] = {
                "status": "completed" if proc.returncode == 0 else "failed",
                "exit_code": proc.returncode,
                "stdout": proc.stdout[:2000],
                "stderr": proc.stderr[:1000],
                "completed_at": time.time(),
            }
    except subprocess.TimeoutExpired:
        with _bg_lock:
            _bg_tasks[task_id] = {"status": "timeout", "error": "Timeout 300s"}
    except Exception as e:
        with _bg_lock:
            _bg_tasks[task_id] = {"status": "error", "error": str(e)}


@app.post("/tasks/background")
async def create_background_task(request: dict):
    """Lanza una tarea en background y retorna task_id inmediatamente."""
    command = request.get("command", "")
    label = request.get("label", command[:60])
    workdir = request.get("workdir", "")
    if not command.strip():
        raise HTTPException(400, "command es requerido")
    
    task_id = _uuid.uuid4().hex[:12]
    with _bg_lock:
        _bg_tasks[task_id] = {"status": "running", "label": label, "created_at": time.time()}
    
    # Ejecutar en thread para no bloquear el event loop
    _threading.Thread(target=_run_bg_task, args=(task_id, command, workdir), daemon=True).start()
    
    return {"task_id": task_id, "status": "running", "label": label}


@app.get("/tasks/pending")
async def list_recent_tasks():
    """Lista tareas recientes (últimas 20) para polling del frontend."""
    with _bg_lock:
        items = [{"task_id": tid, **data} for tid, data in list(_bg_tasks.items())[-20:]]
    return {"tasks": items}


@app.get("/tasks/{task_id}")
async def get_task_status(task_id: str):
    """Consulta el estado de una tarea background."""
    with _bg_lock:
        task = _bg_tasks.get(task_id)
    if not task:
        raise HTTPException(404, f"Tarea {task_id} no encontrada")
    return {"task_id": task_id, **task}


# ---------------------------------------------------------------------------
# Git status endpoint (read-only, sin approval) — alimenta el Git panel
# ---------------------------------------------------------------------------

@app.get("/git/status")
async def git_status(path: str = Query(".", description="Path relativo al repo")):
    """Retorna branch, status, y diff del repo git. Solo lectura, sin approval."""
    import subprocess, shlex
    base = Path(__file__).resolve().parent.parent
    workdir = str(base / path) if path != "." else str(base)
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=workdir, text=True, timeout=5
        ).strip()
    except Exception:
        branch = "unknown"
    
    try:
        status_raw = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=workdir, text=True, timeout=5
        )
        files = []
        for line in status_raw.split("\n"):
            if not line.strip():
                continue
            st = line[:2].strip()
            fp = line[3:].strip()
            files.append({"status": st, "path": fp})
    except Exception:
        files = []
    
    try:
        diff_raw = subprocess.check_output(
            ["git", "diff", "--stat"], cwd=workdir, text=True, timeout=5
        ).strip()
    except Exception:
        diff_raw = ""
    
    return {
        "branch": branch,
        "files": files,
        "changed_count": len([f for f in files if f["status"] not in ("??",)]),
        "untracked_count": len([f for f in files if f["status"] == "??"]),
        "diff_stat": diff_raw,
        "clean": len(files) == 0,
    }


# ---------------------------------------------------------------------------
# Static files (frontend)  ← SIEMPRE AL FINAL — catch-all para SPA
# ---------------------------------------------------------------------------
#
# Se sirve el build nuevo (`web_dist/`) si existe, y si no el frontend anterior
# (`Frontend/`). El orden importa: permite tener los dos vivos durante el
# refactor y cambiar de uno a otro sin tocar código, solo construyendo o
# borrando el directorio. Cuando el nuevo esté validado, `Frontend/` se archiva
# y esta rama queda como única.

_base = os.path.dirname(__file__)
for _candidate, _name in (("web_dist", "frontend-nuevo"), ("Frontend", "frontend-v1")):
    _dir = os.path.normpath(os.path.join(_base, "..", _candidate))
    if os.path.exists(os.path.join(_dir, "index.html")):
        app.mount("/", StaticFiles(directory=_dir, html=True), name=_name)
        print(f"[V-CORE] Frontend servido desde {_candidate}/")
        break
else:
    print("[V-CORE] Sin frontend: no hay web_dist/index.html ni Frontend/index.html")