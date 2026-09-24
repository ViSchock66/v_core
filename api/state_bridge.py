"""
state_bridge.py
----------------
Puente entre los archivos/DB que ya existen (VCORE_STATE.json, vcore.db,
model_routing.yaml, gate_rules.yaml) y los endpoints de FastAPI.

No reinventa el estado: lee/escribe los mismos archivos que usan los
scripts .ps1 y los agentes. Este modulo es deliberadamente "tonto" -
toda decision de seguridad vive en gate.py, no aqui.

NUEVO v0.4: atomic_write() — protocolo de 7 pasos para integridad de estado.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import yaml

# BASE_DIR = raíz del repo (este archivo vive en api/)
BASE_DIR = Path(__file__).resolve().parent.parent

STATE_PATH = BASE_DIR / "VCORE_STATE.json"
AGENTS_PATH = BASE_DIR / "agents.yaml"
DB_PATH = BASE_DIR / "vcore.db"


# ----------------------------------------------------------------------
# VCORE_STATE.json
# ----------------------------------------------------------------------

# Campos requeridos por Capa 1 — siempre presentes en /state
_STATE_DEFAULTS: dict[str, Any] = {
    "session_id": 0,
    "proyecto_activo": "",
    "proyectos_registrados": [],
    "modelos_en_vram": [],
    "ultima_accion_real": "",
    "agentes_disponibles": [],
    "agentes_pendientes": [],
    "mcp_activos": [],
    "scripts_operativos": [],
    "orchestrator_backend": "cloud",
    "tokens_sesion": 0,
}


def read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return dict(_STATE_DEFAULTS)
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
    # Garantizar campos Capa 1 aunque el JSON sea de version anterior
    for key, default in _STATE_DEFAULTS.items():
        if key not in state:
            state[key] = default
    return state


def write_state(state: dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def update_ultima_accion(descripcion: str) -> None:
    """Actualiza VCORE_STATE.json con la ultima accion REAL (verificada
    por el Gate), no lo que un agente "declaro" sin tool call."""
    state = read_state()
    state["ultima_accion_real"] = descripcion
    write_state(state)


# ----------------------------------------------------------------------
# Agentes — leídos de model_routing.yaml (agents.yaml es legacy)
# ----------------------------------------------------------------------
def read_agents() -> list[dict[str, Any]]:
    if not AGENTS_PATH.exists():
        return []
    with open(AGENTS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("agents", [])


# ----------------------------------------------------------------------
# vcore.db - tablas approvals y gate_log
# ----------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_approvals_table() -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                resolved_at REAL,
                agent_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                params_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                result_json TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def create_approval(agent_id: str, tool: str, params: dict[str, Any], reason: str) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO approvals (created_at, agent_id, tool, params_json, reason, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (time.time(), agent_id, tool, json.dumps(params), reason),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_approvals(status: str | None = "pending") -> list[dict[str, Any]]:
    conn = _conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM approvals ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_approval(approval_id: int) -> dict[str, Any] | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve_approval(approval_id: int, status: str, result: Any = None) -> None:
    conn = _conn()
    try:
        conn.execute(
            "UPDATE approvals SET status = ?, resolved_at = ?, result_json = ? WHERE id = ?",
            (status, time.time(), json.dumps(result) if result is not None else None, approval_id),
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------
# gate_log (leido por gate.py, expuesto via /log)
# ----------------------------------------------------------------------
def read_gate_log(limit: int = 50) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                agent_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                nivel TEXT NOT NULL,
                auto_approved INTEGER NOT NULL,
                reason TEXT NOT NULL,
                paths_checked TEXT
            )
            """
        )
        rows = conn.execute(
            "SELECT * FROM gate_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def read_llm_usage(limit: int = 50) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            'SELECT * FROM llm_usage_log ORDER BY timestamp DESC LIMIT ?',
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []  # tabla aun no existe
    finally:
        conn.close()


# =============================================================================
# ATOMIC STATE UPDATE — TD-01
# =============================================================================

def _sha256_file(filepath: Path) -> str:
    """Calcula SHA-256 de un archivo."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_agent_state_hashes() -> dict[str, str]:
    """Lee los hashes registrados en agent_state."""
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT file_hashes FROM agent_state ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row["file_hashes"]:
            return json.loads(row["file_hashes"])
    except Exception:
        pass
    return {}


def _update_agent_state_hashes(new_hashes: dict[str, str]) -> None:
    """Actualiza los hashes en agent_state dentro de una transaccion."""
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO agent_state (agent_name, snapshot, file_hashes, timestamp)
               VALUES (?, ?, ?, ?)""",
            ("SYSTEM", "{}", json.dumps(new_hashes), time.time())
        )
        conn.commit()
    finally:
        conn.close()


class AtomicWriteResult:
    """Resultado de una operacion atomic_write."""
    def __init__(self, success: bool, filepath: str, old_hash: str, new_hash: str,
                 error: str = "", re_sync_required: bool = False):
        self.success = success
        self.filepath = filepath
        self.old_hash = old_hash
        self.new_hash = new_hash
        self.error = error
        self.re_sync_required = re_sync_required

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "filepath": self.filepath,
            "old_hash": self.old_hash,
            "new_hash": self.new_hash,
            "error": self.error,
            "re_sync_required": self.re_sync_required,
        }


def atomic_write(filepath: str, new_content: str, task_id: str = "") -> AtomicWriteResult:
    """
    Protocolo de escritura atomica con verificacion de hash.

    1. Leer hash actual del archivo en disco
    2. Verificar contra agent_state.file_hashes[filepath]
    3. Si divergen -> HALT: alguien modifico el archivo fuera del sistema
    4. Si coinciden -> escribir contenido nuevo
    5. Calcular hash del contenido nuevo
    6. Actualizar agent_state.file_hashes[filepath] en transaction
    7. Registrar en agent_execution

    Args:
        filepath: Path absoluto del archivo a escribir
        new_content: Contenido nuevo (string)
        task_id: ID de tarea para logging

    Returns:
        AtomicWriteResult con success/error y flags de re-sync
    """
    path = Path(filepath)

    # 1. Hash actual en disco
    if path.exists():
        disk_hash = _sha256_file(path)
    else:
        disk_hash = ""

    # 2. Hash registrado en agent_state
    registered_hashes = _read_agent_state_hashes()
    registered_hash = registered_hashes.get(filepath, "")

    # 3. Verificacion de divergencia
    if path.exists() and registered_hash and disk_hash != registered_hash:
        return AtomicWriteResult(
            success=False,
            filepath=filepath,
            old_hash=registered_hash,
            new_hash=disk_hash,
            error=(
                f"HASH MISMATCH: archivo modificado fuera del sistema. "
                f"registered={registered_hash[:16]}... disk={disk_hash[:16]}..."
            ),
            re_sync_required=True,
        )

    # 4. Escribir contenido nuevo (modo atomico: write temp + rename)
    try:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(new_content, encoding="utf-8")
        temp_path.replace(path)
    except Exception as e:
        return AtomicWriteResult(
            success=False,
            filepath=filepath,
            old_hash=registered_hash,
            new_hash="",
            error=f"WRITE ERROR: {e}",
            re_sync_required=False,
        )

    # 5. Calcular hash del nuevo contenido
    new_hash = _sha256_file(path)

    # 6. Actualizar agent_state en transaccion
    updated_hashes = dict(registered_hashes)
    updated_hashes[filepath] = new_hash
    _update_agent_state_hashes(updated_hashes)

    # 7. Registrar en agent_execution
    try:
        conn = _conn()
        conn.execute(
            """INSERT INTO agent_execution
               (task_id, agent_name, node_id, action, input_hash, output_hash,
                tokens_in, tokens_out, tokens_used, duration_s, status,
                provider_used, schema_gate_ok, retry_count, error_msg, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                "SYSTEM",
                "atomic_write",
                "atomic_write",
                registered_hash,
                new_hash,
                0, 0, 0,
                0.0,
                "SUCCESS",
                "local",
                1,
                0,
                "",
                time.time(),
            )
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # logging no bloquea la operacion

    return AtomicWriteResult(
        success=True,
        filepath=filepath,
        old_hash=registered_hash,
        new_hash=new_hash,
        error="",
        re_sync_required=False,
    )
