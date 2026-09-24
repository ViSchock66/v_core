"""
api/events.py
=============
Protocolo de eventos de V-CORE — envelope tipado, versionado y persistible.

Por qué existe
--------------
El backend ya emitía eventos por SSE, pero de forma ad-hoc:

    {"type": "chunk", "content": "..."}
    {"type": "tool", "tool": "read_file", "args": {...}}
    {"type": "artifact", ...}

Sin `id`, sin secuencia, sin timestamp, sin versión y sin `run_id`. Consecuencias
reales y verificadas:

  - El frontend no puede **reconectar** y pedir "lo que me falta": no hay noción
    de posición en el stream.
  - No puede **reconstruir** una conversación: los eventos nunca se persistieron,
    así que al recargar la página las tool cards, los artefactos y las
    aprobaciones desaparecían. El historial guardaba solo el texto final.
  - No hay forma de saber a qué **run** pertenece un evento, así que dos envíos
    concurrentes se mezclan.

El diseño es *event sourcing*: cada run produce un log append-only de eventos
tipados, y la UI es una función de ese log — `UI = reduce(events)`. El mismo
`reduce` alimenta el vivo (SSE) y el replay (historial), así que no puede
desincronizarse del backend.

Envelope
--------
    {
      "v": 1,                     # versión del protocolo
      "seq": 42,                  # posición monotónica dentro del run
      "id": "evt_...",            # identificador único
      "ts": 1790194795.25,        # timestamp epoch
      "run_id": "run_...",        # ejecución del agente
      "thread_id": "205",         # conversación (sesión)
      "type": "tool.end",         # tipo tipado
      "data": { ... }             # payload específico del tipo
    }

`seq` es lo que habilita la reconexión: `GET /threads/{id}/events?after=41`
devuelve exactamente lo que falta.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

PROTOCOL_VERSION = 1

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "vcore.db"


# ---------------------------------------------------------------------------
# Catálogo de tipos
# ---------------------------------------------------------------------------

class EventType:
    """Tipos canónicos del protocolo.

    Los nombres siguen la convención `dominio.accion` para que el cliente pueda
    despachar por prefijo y para que agregar tipos no rompa consumidores viejos
    (un cliente que no conoce `tool.delta` simplemente lo ignora).
    """

    RUN_START = "run.start"
    RUN_END = "run.end"
    RUN_ERROR = "run.error"

    TEXT_DELTA = "text.delta"
    REASONING_DELTA = "reasoning.delta"

    TOOL_START = "tool.start"
    TOOL_END = "tool.end"
    TOOL_DENIED = "tool.denied"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"

    ARTIFACT_CREATED = "artifact.created"
    GRAPH_SNAPSHOT = "graph.snapshot"

    CIRCUIT_STATE = "circuit.state"
    USAGE_UPDATE = "usage.update"


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

@dataclass
class AgentEvent:
    """Un evento del protocolo. `seq` lo asigna el `RunRecorder`, no el emisor."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    v: int = PROTOCOL_VERSION
    seq: int = 0
    id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    ts: float = field(default_factory=time.time)
    run_id: str = ""
    thread_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "seq": self.seq,
            "id": self.id,
            "ts": self.ts,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "type": self.type,
            "data": self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Traducción desde los eventos internos de ENLIL
# ---------------------------------------------------------------------------

# ENLIL emite dicts con la clave `__event__` (o strings sueltos para texto).
# Este mapa traduce esos nombres internos al protocolo público. La traducción
# vive acá, en el borde, y no en cada tool: es lo que permite cambiar el
# formato interno sin tocar el contrato con el cliente.
_INTERNAL_TO_CANONICAL: dict[str, str] = {
    "tool": EventType.TOOL_END,
    "artifact": EventType.ARTIFACT_CREATED,
    "reasoning": EventType.REASONING_DELTA,
    "task_graph": EventType.GRAPH_SNAPSHOT,
    "approval_request": EventType.APPROVAL_REQUESTED,
    "approval.requested": EventType.APPROVAL_REQUESTED,
    "approval.resolved": EventType.APPROVAL_RESOLVED,
    "tool.denied": EventType.TOOL_DENIED,
    "circuit_breaker": EventType.CIRCUIT_STATE,
    "error": EventType.RUN_ERROR,
    "done": EventType.RUN_END,
}


def canonical_from_internal(chunk: Any) -> Optional[AgentEvent]:
    """Convierte un chunk interno de ENLIL en un `AgentEvent`.

    Devuelve None si el chunk no es un evento (p. ej. un string vacío).
    """
    if isinstance(chunk, dict):
        internal = chunk.get("__event__")
        if internal:
            data = {k: v for k, v in chunk.items() if k != "__event__"}
            return AgentEvent(type=_INTERNAL_TO_CANONICAL.get(internal, internal),
                              data=data)
        # Dict sin `__event__`: se trata como payload de texto
        if "content" in chunk:
            return AgentEvent(type=EventType.TEXT_DELTA,
                              data={"content": chunk.get("content") or ""})
        return AgentEvent(type="unknown", data=dict(chunk))

    if isinstance(chunk, str):
        if not chunk:
            return None
        return AgentEvent(type=EventType.TEXT_DELTA, data={"content": chunk})

    return None


# ---------------------------------------------------------------------------
# Persistencia (event sourcing)
# ---------------------------------------------------------------------------

class EventStore:
    """Log append-only de eventos en SQLite.

    La tabla `agent_events` es la fuente de verdad de lo que pasó en un run.
    `chat_history` sigue existiendo para el texto plano (compatibilidad con el
    endpoint de mensajes), pero el replay rico se hace desde acá.
    """

    # RLock y no Lock: `Record` nunca debe poder autobloquearse. Aunque el flush
    # ya se hace fuera de la seccion critica, el lock es reentrante como red de
    # seguridad para futuras llamadas anidadas.
    _lock = threading.RLock()
    _table_ready = False

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self._ensure_table()

    def _ensure_table(self) -> None:
        if EventStore._table_ready:
            return
        with EventStore._lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_events (
                        id TEXT PRIMARY KEY,
                        seq INTEGER NOT NULL,
                        run_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        type TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        data_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_thread "
                    "ON agent_events(thread_id, seq)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_run "
                    "ON agent_events(run_id, seq)"
                )
                conn.commit()
            finally:
                conn.close()
            EventStore._table_ready = True

    def append(self, events: Iterable[AgentEvent]) -> int:
        """Persiste eventos. Devuelve cuántos se escribieron."""
        rows = [
            (e.id, e.seq, e.run_id, e.thread_id, e.ts, e.type, e.v,
             json.dumps(e.data, ensure_ascii=False))
            for e in events
        ]
        if not rows:
            return 0
        with EventStore._lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO agent_events "
                    "(id, seq, run_id, thread_id, timestamp, type, version, data_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        return len(rows)

    def read_thread(self, thread_id: str, after_seq: int = 0,
                    limit: int = 5000) -> list[dict[str, Any]]:
        """Eventos de una conversación, desde `after_seq` inclusive.

        `after_seq` es lo que hace posible la reconexión del cliente: pide
        `after=<último seq que tiene>` y recibe exactamente lo que falta.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE thread_id = ? AND seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (str(thread_id), int(after_seq), limit),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_event(r) for r in rows]

    def read_run(self, run_id: str, after_seq: int = 0,
                 limit: int = 5000) -> list[dict[str, Any]]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE run_id = ? AND seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (run_id, int(after_seq), limit),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_event(r) for r in rows]

    def summarize_thread(self, thread_id: str) -> dict[str, Any]:
        """Resumen de una conversación: qué produjo, sin traer todos los eventos."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_events WHERE thread_id = ?",
                (str(thread_id),),
            ).fetchone()["n"]
            runs = conn.execute(
                "SELECT COUNT(DISTINCT run_id) AS n FROM agent_events WHERE thread_id = ?",
                (str(thread_id),),
            ).fetchone()["n"]
            by_type = conn.execute(
                "SELECT type, COUNT(*) AS n FROM agent_events WHERE thread_id = ? "
                "GROUP BY type ORDER BY n DESC",
                (str(thread_id),),
            ).fetchall()
            first = conn.execute(
                "SELECT MIN(timestamp) AS t FROM agent_events WHERE thread_id = ?",
                (str(thread_id),),
            ).fetchone()["t"]
            last = conn.execute(
                "SELECT MAX(timestamp) AS t FROM agent_events WHERE thread_id = ?",
                (str(thread_id),),
            ).fetchone()["t"]
        finally:
            conn.close()
        return {
            "thread_id": str(thread_id),
            "events": total,
            "runs": runs,
            "types": {r["type"]: r["n"] for r in by_type},
            "first_ts": first,
            "last_ts": last,
        }

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        try:
            data = json.loads(row["data_json"])
        except Exception:
            data = {}
        return {
            "v": row["version"],
            "seq": row["seq"],
            "id": row["id"],
            "ts": row["timestamp"],
            "run_id": row["run_id"],
            "thread_id": row["thread_id"],
            "type": row["type"],
            "data": data,
        }


# ---------------------------------------------------------------------------
# Recorder: asigna seq y persiste
# ---------------------------------------------------------------------------

class RunRecorder:
    """Asigna `seq` monotónico a cada evento de un run y los persiste.

    El `seq` se asigna en el orden en que el generador de ENLIL los produce, que
    es el mismo orden en que llegan al cliente. Eso es lo que hace que el replay
    desde el log reproduzca exactamente la misma secuencia que se vio en vivo.
    """

    def __init__(self, run_id: str, thread_id: str,
                 store: Optional[EventStore] = None, persist: bool = True):
        self.run_id = run_id
        self.thread_id = thread_id
        self.store = store if store is not None else EventStore()
        self.persist = persist
        self.seq = 0
        self.buffer: list[AgentEvent] = []
        self._flock = threading.Lock()

    def record(self, event: AgentEvent) -> AgentEvent:
        """Numera, sella y encola un evento. Devuelve el evento listo para emitir."""
        self.seq += 1
        event.seq = self.seq
        event.run_id = self.run_id
        event.thread_id = self.thread_id
        if not event.ts:
            event.ts = time.time()

        if not self.persist:
            return event

        # El buffer se toca SOLO bajo el lock, y el flush se decide aca pero se
        # ejecuta FUERA del lock. La version anterior llamaba a `flush()` desde
        # dentro del `with self._flock:` y `flush()` vuelve a tomar el mismo
        # lock: con `threading.Lock` (no reentrante) eso es un deadlock que
        # colgaba el proceso entero — se reproducia al instante porque
        # `run.end` fuerza flush en la primera emision del run.
        pendiente: list[AgentEvent] = []
        with self._flock:
            self.buffer.append(event)
            if len(self.buffer) >= 32 or event.type in (
                EventType.RUN_END, EventType.RUN_ERROR, EventType.TOOL_END,
                EventType.TOOL_DENIED, EventType.APPROVAL_REQUESTED,
                EventType.APPROVAL_RESOLVED, EventType.ARTIFACT_CREATED,
            ):
                pendiente, self.buffer = self.buffer, []

        if pendiente:
            self.store.append(pendiente)
        return event

    def flush(self) -> int:
        """Escribe lo pendiente. Se llama al cerrar el run y en eventos clave."""
        with self._flock:
            pending, self.buffer = self.buffer, []
        if pending:
            return self.store.append(pending)
        return 0

    def close(self) -> int:
        return self.flush()
