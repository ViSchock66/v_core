"""
agents/SHAMASH/shamash.py
=========================
SHAMASH v3 — Capa de Contexto para V-CORE. Motor de memoria delegado a NEM0.

La persistencia (store/query/historial) la maneja system/nem0.
SHAMASH se queda con lo que solo él puede hacer: inyectar contexto del proyecto.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

# ── Motor de memoria SHAMASH ────────────────────────────────────
from agents.SHAMASH.memory import nem0

# =============================================================================
# SCHEMAS
# =============================================================================

class FileEntry(BaseModel):
    path: str
    size: int
    modified: float


class Lesson(BaseModel):
    id: int = 0
    content: str
    created_at: float = 0.0


class ProjectContext(BaseModel):
    project_name: str
    architecture_summary: str
    active_files: list[FileEntry] = []
    recent_lessons: list[Lesson] = []
    impact_map: Optional[dict] = None
    token_budget_used: int = 0
    token_budget_total: int = 0


# =============================================================================
# CONSTANTES
# =============================================================================

BASE_DIR      = Path(__file__).resolve().parent.parent.parent
DB_PATH       = BASE_DIR / "vcore.db"
STATE_PATH    = BASE_DIR / "VCORE_STATE.json"
CHROMA_PATH   = BASE_DIR / "chroma_db"
ARCHITECTURE_PATH = BASE_DIR / "VCORE_ARCHITECTURE.md"
WORKSPACE_DIR = BASE_DIR / "workspace"

OLLAMA_HOST        = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL        = "nomic-embed-text"   # ya disponible en Ollama
CHROMA_COLLECTION  = "shamash_memory"

DEFAULT_TOKEN_BUDGET_TOTAL = 32768


from api.embed import embed

# =============================================================================
# SHAMASH v2
# =============================================================================

class SHAMASH:
    """
    Context Manager de V-CORE.
    Memoria delegada a NEM0 (system/nem0). Solo lectura de archivos.
    """

    def __init__(self):
        self.agent_name = "SHAMASH"

    # ── API de memoria (delegada a NEM0) ─────────────────────────

    def store(self, content: str, metadata: Optional[dict] = None) -> str:
        """Guardar recuerdo con deduplicación. Delega en NEM0."""
        if not content or not content.strip():
            return ""
        return nem0.add(content=content, metadata=metadata or {})

    def query(self, question: str, k: int = 5) -> list[Lesson]:
        """Búsqueda semántica. Delega en NEM0."""
        results = nem0.search(query=question, k=k)
        return [
            Lesson(id=0, content=r.content, created_at=time.time())
            for r in results
        ]

    def record_lesson(self, lesson: str, task_id: str = "") -> str:
        """Compatibilidad con ENLIL agent loop. Delega en NEM0."""
        return self.store(content=lesson, metadata={"outcome": "lesson", "task_id": task_id})

    def record_quality(self, content: str, quality_score: float = 0.0,
                       user_feedback: str = "", task_id: str = "") -> str:
        """Registra feedback de calidad. Delega en NEM0."""
        return self.store(content=content, metadata={
            "outcome": "quality", "quality_score": str(quality_score),
            "user_feedback": user_feedback, "task_id": task_id,
        })

    def get_recent_work(self, limit: int = 5) -> list[dict]:
        """Últimas lecciones desde NEM0."""
        entries = nem0.history(limit=limit)
        return [{"id": e.id, "content": e.content, "metadata": e.metadata,
                 "created_at": e.created_at} for e in entries]

    # ── Contexto del proyecto (propio de SHAMASH) ─────────────────

    def inject_project_context(self) -> ProjectContext:
        """
        Construye el contexto completo del proyecto activo.
        B2: usa query() semántico en lugar de _get_lessons_by_tier().
        """
        state         = self._read_state()
        project_name  = state.get("proyecto_activo", "V-CORE")
        architecture  = self.get_architecture()
        active_files  = self._list_active_files()

        # B2: retrieval semántico — pregunta por contexto relevante al proyecto activo
        lessons = self.query(f"contexto proyecto {project_name} lecciones recientes", k=5)

        budget_used = self._estimate_context_tokens(architecture, active_files, lessons)

        return ProjectContext(
            project_name=project_name,
            architecture_summary=architecture,
            active_files=active_files,
            recent_lessons=lessons,
            token_budget_used=budget_used,
            token_budget_total=DEFAULT_TOKEN_BUDGET_TOTAL,
        )

    def get_architecture(self) -> str:
        if ARCHITECTURE_PATH.exists():
            try:
                lines = ARCHITECTURE_PATH.read_text(encoding="utf-8").split("\n")
                summary_lines = []
                capture = False
                for line in lines[:200]:
                    if line.startswith("## 1.") or line.startswith("## 3.") or \
                       line.startswith("## 4.") or line.startswith("## 15."):
                        capture = True
                    if line.startswith("## ") and capture and \
                       not any(line.startswith(f"## {n}.") for n in ["1","3","4","15"]):
                        break
                    if capture:
                        summary_lines.append(line)
                return "\n".join(summary_lines) or "Documento de arquitectura disponible."
            except Exception:
                from api.version import VERSION
                return f"V-CORE v{VERSION} — Sistema agéntico personal de producción."
        from api.version import VERSION
        return f"V-CORE v{VERSION} — Sistema agéntico personal de producción."

    def get_recent_work(self, limit: int = 5) -> list[dict]:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, session_id, agent_name, memory_type, content,
                          created_at, task_id, quality_score, user_feedback
                   FROM agent_memory
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def summarize_file(self, filepath: str) -> str:
        path = Path(filepath)
        if not path.exists() or not path.is_file():
            return f"Archivo no encontrado: {filepath}"
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            lines   = content.split("\n")
            total   = len(lines)
            ext     = path.suffix.lower()

            sections = []
            sections.append(f"# {path.name} ({total} líneas, {ext})")
            sections.append("## Inicio:")
            sections.append("\n".join(f"{i+1:4d}: {l}" for i, l in enumerate(lines[:30])))

            if total > 40:
                sections.append("## Final:")
                sections.append("\n".join(f"{total-10+i:4d}: {l}" for i, l in enumerate(lines[-10:])))

            defs = []
            for i, line in enumerate(lines):
                stripped = line.strip()
                if ext == ".py" and (stripped.startswith("def ") or stripped.startswith("class ") or
                                     stripped.startswith("async def ")):
                    defs.append(f"  L{i+1}: {stripped[:80]}")
                elif ext in (".js", ".ts") and ("function " in stripped or
                             stripped.startswith("const ") or stripped.startswith("class ")):
                    defs.append(f"  L{i+1}: {stripped[:80]}")

            if defs:
                sections.append(f"## Definiciones ({len(defs)}):")
                sections.append("\n".join(defs[:30]))

            return "\n".join(sections)
        except Exception as e:
            return f"Error leyendo {filepath}: {e}"

    def estimate_repo_size(self) -> int:
        total_bytes = 0
        code_extensions = {
            ".py", ".js", ".ts", ".yaml", ".yml", ".json", ".md",
            ".html", ".css", ".cpp", ".c", ".h", ".hpp", ".ps1",
            ".sql", ".txt", ".cfg", ".ini", ".toml",
        }
        try:
            for entry in os.scandir(BASE_DIR):
                if entry.name.startswith(".") or entry.name in ("__pycache__", ".venv", "chroma_db"):
                    continue
                if entry.is_dir():
                    total_bytes += self._dir_size(entry.path, code_extensions)
                elif entry.is_file():
                    if Path(entry.name).suffix.lower() in code_extensions:
                        total_bytes += entry.stat().st_size
            return total_bytes // 1024
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _read_state(self) -> dict[str, Any]:
        if STATE_PATH.exists():
            try:
                return json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"proyecto_activo": "V-CORE", "agentes_disponibles": []}

    def _list_active_files(self) -> list[FileEntry]:
        files = []
        scan_dirs = [
            (WORKSPACE_DIR,        "workspace"),
            (BASE_DIR / "scripts", "scripts"),
            (BASE_DIR / "agents",  "agents"),
            (BASE_DIR / "Frontend","frontend"),
            (BASE_DIR / "api",     "api"),
            (BASE_DIR / "system",  "system"),
        ]
        for scan_dir, label in scan_dirs:
            if not scan_dir.exists():
                continue
            try:
                for entry in os.scandir(scan_dir):
                    if entry.name.startswith("."):
                        continue
                    if entry.is_file():
                        files.append(FileEntry(
                            path=str(entry.path),
                            size=entry.stat().st_size,
                            modified=entry.stat().st_mtime,
                        ))
                    elif entry.is_dir() and label == "agents":
                        try:
                            for sub in os.scandir(entry.path):
                                if sub.is_file() and sub.name.endswith(".py"):
                                    files.append(FileEntry(
                                        path=str(sub.path),
                                        size=sub.stat().st_size,
                                        modified=sub.stat().st_mtime,
                                    ))
                        except Exception:
                            pass
            except Exception:
                pass
        return sorted(files, key=lambda f: f.path)

    @staticmethod
    def _dir_size(dirpath: str, extensions: set[str]) -> int:
        """Suma recursiva de bytes de archivos con extensión de código.

        Este helper estaba escrito pero **fuera de cualquier método**: quedaba
        como código inalcanzable después de un `return` dentro de
        `_list_active_files()`. `estimate_repo_size()` lo llamaba como
        `self._dir_size(...)` y lanzaba AttributeError en cada invocación
        (silenciado por el `except Exception: return 0`), así que el tamaño del
        repo siempre reportaba 0 KB.
        """
        total = 0
        try:
            for entry in os.scandir(dirpath):
                if entry.name.startswith(".") or entry.name in ("__pycache__", ".venv", "chroma_db"):
                    continue
                if entry.is_dir():
                    total += SHAMASH._dir_size(entry.path, extensions)
                elif entry.is_file():
                    if Path(entry.name).suffix.lower() in extensions:
                        total += entry.stat().st_size
        except PermissionError:
            pass
        return total

    @staticmethod
    def _estimate_context_tokens(
        architecture: str,
        active_files: list[FileEntry],
        lessons: list[Lesson],
    ) -> int:
        total_chars  = len(architecture)
        total_chars += sum(len(f.path) + 20 for f in active_files[:50])
        total_chars += sum(len(l.content) for l in lessons)
        return total_chars // 4
