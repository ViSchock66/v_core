"""
system/observability.py
=======================
VCoreTracer — observabilidad unificada de V-CORE (B3).

Emite traces a Langfuse por cada evento relevante del sistema:
  - Iteraciones del agent loop de Orchestrator (tool usada, tokens, duración)
  - Diffs de Planner (archivo, outcome, user_corrected flag)
  - Screenshots del Visual Auditor (findings count, instrucción)

Diseño:
  - Si Langfuse no está configurado, todas las llamadas son no-ops silenciosos.
    El sistema NUNCA falla por ausencia de observabilidad.
  - Configuración via variables de entorno o VCORE_STATE.json.
  - También persiste un log local JSONL como fallback (siempre activo).
  - Thread-safe: usa locks para escritura al log local.
  - Singleton: VCoreTracer() devuelve siempre la misma instancia.

Configuración (.env o variables de entorno):
    LANGFUSE_PUBLIC_KEY=pk-lf-...
    LANGFUSE_SECRET_KEY=sk-lf-...
    LANGFUSE_HOST=https://cloud.langfuse.com   # o instancia local

Uso:
    from system.observability import get_tracer

    tracer = get_tracer()

    # En agent loop
    tracer.trace_agent_loop(
        task_id="abc123",
        iteration=1,
        tool="read_file",
        result="OK — 80 líneas",
        tokens=320,
        duration_ms=450,
    )

    # En Planner
    tracer.trace_diff(
        task_id="abc123",
        file="web/src/App.tsx",
        outcome="applied",
        user_corrected=False,
    )

    # En Visual Auditor
    tracer.trace_visual_audit(
        task_id="abc123",
        findings_count=3,
        screenshot_path="screenshots/example.png",
        instruction="Verifica que el botón send funcione",
    )
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
TRACE_LOG_PATH = BASE_DIR / "traces.jsonl"


def _safe_print(msg: str) -> None:
    """print() que nunca puede tumbar la observabilidad.

    Contrato del modulo: "El sistema NUNCA falla por ausencia de
    observabilidad". El status incluye emoji (⚠ / ✅) y en Windows con stdout
    no interactivo (cp1252) eso lanzaba UnicodeEncodeError desde
    VCoreTracer.__init__, que corre dentro del agent loop de Orchestrator y mataba
    el chat antes de llegar al modelo. api/main.py ya fuerza UTF-8 en los
    streams del server; esta guarda cubre cualquier otro entry point
    (scripts, CLI, imports directos) que no pase por ahi.
    """
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(msg.encode(enc, "replace").decode(enc, "replace"))
        except Exception:
            pass
    except Exception:
        pass

# =============================================================================
# LANGFUSE CLIENT — wrapper con graceful degradation
# =============================================================================

class _LangfuseClient:
    """
    Wrapper sobre el SDK de Langfuse.
    Si el SDK no está instalado o las credenciales no están configuradas,
    todas las llamadas son no-ops silenciosos.
    """

    def __init__(self):
        self._client = None
        self._enabled = False
        self._init_error: str | None = None
        self._try_init()

    def _try_init(self) -> None:
        """Intenta inicializar el cliente Langfuse. Falla silenciosamente."""
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

        if not public_key or not secret_key:
            self._init_error = "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY no configuradas"
            return

        try:
            from langfuse import Langfuse  # type: ignore
            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
            )
            self._enabled = True
            print(f"[VCoreTracer] Langfuse conectado → {host}")
        except ImportError:
            self._init_error = "langfuse no instalado (pip install langfuse)"
        except Exception as e:
            self._init_error = f"Error al conectar Langfuse: {e}"

    @property
    def enabled(self) -> bool:
        return self._enabled

    def trace(self, name: str, metadata: dict, input_data: Any = None, output_data: Any = None) -> str | None:
        """
        Crea un trace en Langfuse.
        Retorna el trace_id o None si Langfuse no está disponible.
        """
        if not self._enabled or not self._client:
            return None
        try:
            trace = self._client.trace(
                name=name,
                metadata=metadata,
                input=input_data,
                output=output_data,
            )
            return trace.id
        except Exception as e:
            print(f"[VCoreTracer] Error emitiendo trace a Langfuse: {e}")
            return None

    def span(self, trace_id: str, name: str, metadata: dict,
             input_data: Any = None, output_data: Any = None,
             start_time: float | None = None, end_time: float | None = None) -> None:
        """Crea un span dentro de un trace existente."""
        if not self._enabled or not self._client:
            return
        try:
            self._client.span(
                trace_id=trace_id,
                name=name,
                metadata=metadata,
                input=input_data,
                output=output_data,
                start_time=start_time,
                end_time=end_time,
            )
        except Exception as e:
            print(f"[VCoreTracer] Error emitiendo span a Langfuse: {e}")

    def flush(self) -> None:
        """Fuerza el envío de eventos pendientes."""
        if self._enabled and self._client:
            try:
                self._client.flush()
            except Exception:
                pass


# =============================================================================
# LOCAL JSONL LOGGER — siempre activo, independiente de Langfuse
# =============================================================================

class _LocalLogger:
    """
    Logger local JSONL como fallback garantizado.
    Siempre escribe, incluso si Langfuse no está disponible.
    Thread-safe via lock.
    """

    def __init__(self, path: Path = TRACE_LOG_PATH):
        self._path = path
        self._lock = threading.Lock()

    def write(self, event_type: str, data: dict) -> None:
        entry = {
            "ts": time.time(),
            "event": event_type,
            **data,
        }
        try:
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            print(f"[VCoreTracer] Error escribiendo log local: {e}")

    def tail(self, n: int = 50) -> list[dict]:
        """Retorna las últimas N entradas del log."""
        try:
            if not self._path.exists():
                return []
            lines = self._path.read_text(encoding="utf-8").strip().splitlines()
            recent = lines[-n:] if len(lines) > n else lines
            result = []
            for line in reversed(recent):
                try:
                    result.append(json.loads(line))
                except Exception:
                    pass
            return result
        except Exception:
            return []

    def clear(self) -> None:
        """Limpia el log local."""
        try:
            self._path.write_text("", encoding="utf-8")
        except Exception:
            pass


# =============================================================================
# VCORE TRACER — interfaz pública
# =============================================================================

class VCoreTracer:
    """
    Tracer unificado de V-CORE.
    Emite a Langfuse (si disponible) + log local JSONL (siempre).

    Uso como singleton via get_tracer().
    """

    def __init__(self):
        self._langfuse = _LangfuseClient()
        self._local = _LocalLogger()
        self._session_traces: dict[str, str] = {}  # task_id → langfuse trace_id

        status = "✅ Langfuse activo" if self._langfuse.enabled else f"⚠ Langfuse inactivo ({self._langfuse._init_error})"
        _safe_print(f"[VCoreTracer] Iniciado. {status}. Log local: {TRACE_LOG_PATH}")

    # ─────────────────────────────────────────────────────────────────
    # AGENT LOOP TRACES
    # ─────────────────────────────────────────────────────────────────

    def trace_agent_loop(
        self,
        task_id: str,
        iteration: int,
        tool: str | None,
        result: str | None,
        tokens: int = 0,
        duration_ms: int = 0,
        model: str = "",
        is_final: bool = False,
    ) -> None:
        """
        Emite un trace por cada iteración del agent loop de Orchestrator.

        Args:
            task_id: ID de la tarea (uuid)
            iteration: Número de iteración (0-based)
            tool: Nombre de la tool usada, o None si fue respuesta final
            result: Resultado truncado de la tool, o None
            tokens: Tokens consumidos en esta iteración
            duration_ms: Duración de la iteración en ms
            model: Modelo LLM usado
            is_final: True si es la respuesta final (no tool call)
        """
        data = {
            "task_id": task_id,
            "iteration": iteration,
            "tool": tool,
            "tokens": tokens,
            "duration_ms": duration_ms,
            "model": model,
            "is_final": is_final,
            "result_preview": (result or "")[:200],
        }

        # Log local siempre
        self._local.write("agent_loop", data)

        # Langfuse: crear trace al inicio de la tarea (iter 0), span en iteraciones siguientes
        if self._langfuse.enabled:
            if iteration == 0 and task_id not in self._session_traces:
                trace_id = self._langfuse.trace(
                    name=f"orchestrator_task_{task_id[:8]}",
                    metadata={"task_id": task_id, "model": model},
                    input_data={"message": result},
                )
                if trace_id:
                    self._session_traces[task_id] = trace_id
            else:
                trace_id = self._session_traces.get(task_id)
                if trace_id:
                    self._langfuse.span(
                        trace_id=trace_id,
                        name=f"iter_{iteration}_{tool or 'response'}",
                        metadata=data,
                        input_data={"tool": tool},
                        output_data={"result": (result or "")[:500]},
                    )

    # ─────────────────────────────────────────────────────────────────
    # Planner DIFF TRACES
    # ─────────────────────────────────────────────────────────────────

    def trace_diff(
        self,
        task_id: str,
        file: str,
        outcome: str,           # "applied" | "failed" | "skipped"
        user_corrected: bool = False,
        compiled: bool | None = None,
        tests_passed: bool | None = None,
        diff_size_chars: int = 0,
    ) -> None:
        """
        Emite un trace por cada diff aplicado por Planner.

        Args:
            task_id: ID de la tarea
            file: Archivo modificado
            outcome: "applied", "failed", o "skipped"
            user_corrected: True si el usuario tuvo que corregir manualmente después
            compiled: True/False si se verificó compilación, None si no aplica
            tests_passed: True/False si pasaron tests, None si no aplica
            diff_size_chars: Tamaño del diff en caracteres
        """
        data = {
            "task_id": task_id,
            "file": file,
            "outcome": outcome,
            "user_corrected": user_corrected,
            "compiled": compiled,
            "tests_passed": tests_passed,
            "diff_size_chars": diff_size_chars,
        }

        self._local.write("planner_diff", data)

        if self._langfuse.enabled:
            trace_id = self._session_traces.get(task_id)
            if trace_id:
                self._langfuse.span(
                    trace_id=trace_id,
                    name=f"planner_diff_{Path(file).name}",
                    metadata=data,
                    input_data={"file": file},
                    output_data={"outcome": outcome, "user_corrected": user_corrected},
                )
            else:
                # Diff sin task_id previo (ej: aplicado fuera del loop)
                self._langfuse.trace(
                    name=f"planner_diff_{Path(file).name}",
                    metadata=data,
                    input_data={"file": file},
                    output_data={"outcome": outcome},
                )

    # ─────────────────────────────────────────────────────────────────
    # VISUAL AUDITOR TRACES
    # ─────────────────────────────────────────────────────────────────

    def trace_visual_audit(
        self,
        task_id: str,
        findings_count: int,
        screenshot_path: str = "",
        instruction: str = "",
        duration_ms: int = 0,
        console_errors_count: int = 0,
        dom_missing: list[str] | None = None,
    ) -> None:
        """
        Emite un trace por cada ejecución del Visual Auditor.

        Args:
            task_id: ID de la tarea
            findings_count: Número de hallazgos reportados
            screenshot_path: Ruta al screenshot generado
            instruction: Instrucción enviada al auditor
            duration_ms: Duración del audit en ms
            console_errors_count: Errores de consola detectados
            dom_missing: Lista de IDs DOM faltantes
        """
        data = {
            "task_id": task_id,
            "findings_count": findings_count,
            "screenshot_path": screenshot_path,
            "instruction_preview": instruction[:200],
            "duration_ms": duration_ms,
            "console_errors_count": console_errors_count,
            "dom_missing": dom_missing or [],
        }

        self._local.write("visual_audit", data)

        if self._langfuse.enabled:
            trace_id = self._session_traces.get(task_id)
            name = f"visual_audit_{task_id[:8]}"
            if trace_id:
                self._langfuse.span(
                    trace_id=trace_id,
                    name=name,
                    metadata=data,
                    input_data={"instruction": instruction},
                    output_data={
                        "findings_count": findings_count,
                        "screenshot": screenshot_path,
                    },
                )
            else:
                self._langfuse.trace(
                    name=name,
                    metadata=data,
                    input_data={"instruction": instruction},
                    output_data={"findings_count": findings_count},
                )

    # ─────────────────────────────────────────────────────────────────
    # UTILS
    # ─────────────────────────────────────────────────────────────────

    def get_recent_traces(self, n: int = 50) -> list[dict]:
        """Retorna las últimas N entradas del log local."""
        return self._local.tail(n)

    def get_session_summary(self, task_id: str) -> dict:
        """
        Genera un resumen de la sesión para un task_id dado.
        Útil para el panel de observabilidad del frontend.
        """
        traces = self.get_recent_traces(200)
        session = [t for t in traces if t.get("task_id") == task_id]

        tool_calls = [t for t in session if t.get("event") == "agent_loop" and t.get("tool")]
        diffs = [t for t in session if t.get("event") == "planner_diff"]
        audits = [t for t in session if t.get("event") == "visual_audit"]

        total_tokens = sum(t.get("tokens", 0) for t in session if t.get("event") == "agent_loop")
        total_duration = sum(t.get("duration_ms", 0) for t in session)

        tool_freq: dict[str, int] = {}
        for t in tool_calls:
            tool = t.get("tool", "unknown")
            tool_freq[tool] = tool_freq.get(tool, 0) + 1

        return {
            "task_id": task_id,
            "iterations": len([t for t in session if t.get("event") == "agent_loop"]),
            "tool_calls": len(tool_calls),
            "tool_frequency": tool_freq,
            "diffs_applied": len([d for d in diffs if d.get("outcome") == "applied"]),
            "diffs_failed": len([d for d in diffs if d.get("outcome") == "failed"]),
            "user_corrections": len([d for d in diffs if d.get("user_corrected")]),
            "visual_audits": len(audits),
            "total_tokens": total_tokens,
            "total_duration_ms": total_duration,
            "langfuse_active": self._langfuse.enabled,
        }

    def flush(self) -> None:
        """Fuerza el flush de Langfuse (llamar al cerrar la sesión)."""
        self._langfuse.flush()

    @property
    def langfuse_enabled(self) -> bool:
        return self._langfuse.enabled


# =============================================================================
# SINGLETON
# =============================================================================

_tracer_instance: VCoreTracer | None = None
_tracer_lock = threading.Lock()


def get_tracer() -> VCoreTracer:
    """
    Retorna el singleton VCoreTracer.
    Thread-safe. Inicializa en el primer llamado.
    """
    global _tracer_instance
    if _tracer_instance is None:
        with _tracer_lock:
            if _tracer_instance is None:
                _tracer_instance = VCoreTracer()
    return _tracer_instance
