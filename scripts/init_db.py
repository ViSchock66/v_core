"""
scripts/init_db.py
===================
Inicializa vcore.db con el schema completo v1.5.

Idempotente: todo usa CREATE TABLE IF NOT EXISTS. Para columnas nuevas en
tablas que ya existian (agent_memory ganando quality_score/user_feedback/
task_id en BLOQUE 3), se usa ALTER TABLE ADD COLUMN con manejo de excepcion,
porque SQLite no soporta "ADD COLUMN IF NOT EXISTS" nativamente.

Tablas legacy v4.2 (sessions, iterations, chat_history): se conservan tal
cual. Ningun modulo v0.3 las lee ni las escribe -- son candidatas a retiro
en una limpieza futura explicita, no se borran aqui sin marcarlas primero.

approvals / gate_log: ya se crean on-demand dentro de api/state_bridge.py
(CONGELADO). Se declaran tambien aqui para que este script sea la fuente
unica del schema completo; el CREATE TABLE IF NOT EXISTS de state_bridge.py
queda como fallback defensivo, no como autoridad de schema.

Fuente de verdad del schema actual: VCORE_ARCHITECTURE.md.
"""

import os
import sqlite3
from pathlib import Path

# Mismo patron que el resto del codigo (llm_client.py, state_bridge.py):
# override por env var, default relativo a la raiz del repo -- nunca un
# path absoluto hardcodeado de una sola maquina.
DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "vcore.db")
DB_PATH = os.getenv("VCORE_DB_PATH", DEFAULT_DB_PATH)


def _add_column_if_missing(cur: sqlite3.Cursor, table: str, column: str, coltype: str) -> None:
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        print(f"  [migracion] {table}.{column} agregada")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def init_db(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    cur = conn.cursor()

    # ------------------------------------------------------------------
    # Legacy v4.2 -- conservadas, no leidas por v0.3 (ver docstring)
    # ------------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        project TEXT,
        tier INTEGER,
        budget_tokens INTEGER,
        status TEXT DEFAULT 'open'
    )
    """)
    # v1.5.x: titulo de conversacion editable (PATCH /sessions/{id}).
    # api/main.py lo agrega lazy via _ensure_sessions_title_column() para que
    # funcione sin correr esta migracion; se declara aqui para que el schema
    # siga teniendo una sola fuente de verdad.
    _add_column_if_missing(cur, "sessions", "title", "TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS iterations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        timestamp TEXT NOT NULL,
        agent TEXT NOT NULL,
        action TEXT NOT NULL,
        prompt TEXT,
        output TEXT,
        tokens_used INTEGER DEFAULT 0,
        tier INTEGER,
        cost_usd REAL DEFAULT 0.0,
        result TEXT DEFAULT 'success',
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        tool_calls TEXT,
        session_id INTEGER
    )
    """)

    # ------------------------------------------------------------------
    # approvals / gate_log -- declaradas aqui como autoridad de schema
    # ------------------------------------------------------------------
    cur.execute("""
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
    """)

    cur.execute("""
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
    """)

    # ------------------------------------------------------------------
    # Tablas v0.3 (origen: VCORE_ARCHITECTURE_v1_1.md, historial de schema)
    # ------------------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_execution (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT,
        agent_name  TEXT,
        node_id     TEXT,
        action      TEXT,
        input_hash  TEXT,
        output_hash TEXT,
        tokens_used INTEGER,   -- legacy compat: total = tokens_in + tokens_out
        duration_s  REAL,
        status      TEXT,   -- SUCCESS | FAILED | TIMEOUT | SCHEMA_INVALID | DIFF_MATCH_ERROR | INTERRUPTED
        error_msg   TEXT,
        timestamp   REAL
    )
    """)
    # Extension v0.4 (origen: historial de schema, VCORE_ARCHITECTURE_v1_1.md)
    _add_column_if_missing(cur, "agent_execution", "tokens_in",      "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "agent_execution", "tokens_out",     "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "agent_execution", "provider_used",  "TEXT")
    _add_column_if_missing(cur, "agent_execution", "schema_gate_ok", "INTEGER")
    _add_column_if_missing(cur, "agent_execution", "retry_count",    "INTEGER DEFAULT 0")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_graphs (
        id          TEXT PRIMARY KEY,
        definition  TEXT,   -- JSON del grafo validado
        status      TEXT,   -- PLANNING | IN_PROGRESS | AWAITING_APPROVAL | DONE | FAILED_UNCLEAN_SHUTDOWN
        created_at  REAL,
        updated_at  REAL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_memory (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT,
        agent_name  TEXT,
        memory_type TEXT,   -- short | long | lesson | quality
        content     TEXT,
        created_at  REAL
    )
    """)
    # Extension BLOQUE 3 (v0.4 estrategico, Sec 2.5 Fase 1 -- self-improvement loop)
    _add_column_if_missing(cur, "agent_memory", "task_id",          "TEXT")
    _add_column_if_missing(cur, "agent_memory", "quality_score",    "REAL")
    _add_column_if_missing(cur, "agent_memory", "user_feedback",    "TEXT")
    # FIX F-10: columnas que curator.py escribe/lee y faltaban en el schema
    # tier: ciclo de vida (episodic -> working -> distilled)
    # usage_count: cuantas veces fue inyectada al contexto (prioridad en _get_lessons_by_tier)
    # promoted_from_id: referencia a la leccion episodica de origen (opcional)
    _add_column_if_missing(cur, "agent_memory", "tier",             "TEXT DEFAULT 'episodic'")
    _add_column_if_missing(cur, "agent_memory", "usage_count",      "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "agent_memory", "promoted_from_id", "INTEGER")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_state (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_name  TEXT,
        snapshot    TEXT,   -- JSON
        file_hashes TEXT,   -- JSON: {filepath: sha256_hash} -- Atomic State Update
        timestamp   REAL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rag_documents (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        filepath    TEXT UNIQUE,
        content_hash TEXT,
        indexed_at  REAL,
        chunk_ids   TEXT    -- JSON: lista de IDs en ChromaDB
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS llm_usage_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        provider        TEXT,
        model           TEXT,
        tokens_in       INTEGER,
        tokens_out      INTEGER,
        estimated_cost  REAL,
        timestamp       REAL,
        agent_name      TEXT,
        task_id         TEXT,
        circuit_state   TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS circuit_breaker_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        provider        TEXT,
        transition      TEXT,   -- CLOSED->OPEN | OPEN->HALF-OPEN | HALF-OPEN->CLOSED
        trigger_reason  TEXT,
        error_rate      REAL,
        timestamp       REAL
    )
    """)

    conn.commit()
    conn.close()
    print(f"V-CORE DB inicializada correctamente (schema v0.3) en: {db_path}")


if __name__ == "__main__":
    init_db()
