"""
api/policy.py
=============
Contexto de política compartido: un solo `Gate` para todo el proceso.

Por qué existe
--------------
`Gate` se instanciaba por separado en `api/files_api.py`, `api/shell_api.py`,
`api/search_api.py`, `agents/Planner/planner.py` y `agents/Retriever/retriever.py`. Consecuencia
real: `POST /system/reload-gate` recargaba reglas en una instancia y las demás
seguían con la política vieja en memoria — el endpoint "funcionaba" y no cambiaba
nada del comportamiento efectivo.

Acá vive la instancia única, el contexto del perímetro (workspace de la sesión,
sandbox) y la recarga atómica.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
RULES_PATH = BASE_DIR / "gate_rules.yaml"
DB_PATH = BASE_DIR / "vcore.db"

_lock = threading.RLock()
_gate = None
_rules_mtime: float | None = None


@dataclass
class PolicyContext:
    """Contexto de ejecución para una decisión de política.

    - `workspace`: raíz efectiva donde el agente opera. Es lo que define qué es
      "dentro del workspace" para las reglas de alcance.
    - `sandbox`: si la ejecución ocurre en un perímetro acotado (worktree
      desechable + recursos limitados). Relaja la *ejecución*, nunca el alcance.
    - `session_dir` / `run_id`: correlación para auditoría.
    """

    workspace: Path = BASE_DIR
    sandbox: bool = False
    session_dir: str = ""
    run_id: str = ""
    approval_timeout_s: float = 300.0
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "sandbox": self.sandbox,
            "session_dir": self.session_dir,
            "run_id": self.run_id,
            "approval_timeout_s": self.approval_timeout_s,
        }


def get_gate():
    """Retorna la instancia única de `Gate`, recargando si el YAML cambió.

    La recarga es automática por `mtime`: editar `gate_rules.yaml` surte efecto
    en la siguiente decisión, sin reiniciar el servidor y sin depender de que
    alguien llame al endpoint de reload. Es el comportamiento que un operador
    espera de un archivo de política.
    """
    global _gate, _rules_mtime
    with _lock:
        if _gate is None:
            from gate import Gate
            _gate = Gate(RULES_PATH, db_path=DB_PATH)
            _rules_mtime = _mtime()
            return _gate

        if _mtime() != _rules_mtime:
            try:
                _gate.reload()
                _rules_mtime = _mtime()
            except Exception:
                # Si la política nueva es inválida, se conserva la vigente: es
                # mejor operar con reglas viejas y conocidas que quedar sin
                # política por un error de sintaxis.
                pass
        return _gate


def reload_policy() -> dict[str, Any]:
    """Recarga forzada de la política. Lo llama `POST /system/reload-gate`."""
    global _rules_mtime
    with _lock:
        gate = get_gate()
        before = _rules_mtime
        gate.reload()
        _rules_mtime = _mtime()
        return {
            "reloaded": True,
            "rules_path": str(RULES_PATH),
            "mtime_before": before,
            "mtime_after": _rules_mtime,
            "tools": len(gate.tools),
        }


def _mtime() -> float | None:
    try:
        return RULES_PATH.stat().st_mtime
    except OSError:
        return None
