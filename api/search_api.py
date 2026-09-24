"""
search_api.py
-----------
Search API para V-CORE (v4.2).

Endpoints REST para búsqueda:
- GET /search/workspace — buscar en contenido de archivos (grep)
- GET /search/quick — búsqueda rápida solo nombres

Todas las operaciones son read_only → Nivel A.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

# Gate validation
from gate import Gate

BASE_DIR = Path(__file__).resolve().parent.parent
GATE = Gate(BASE_DIR / "gate_rules.yaml", db_path=BASE_DIR / "vcore.db")
WORKSPACE_DIR = Path(BASE_DIR / "workspace")

router = APIRouter(prefix="/search", tags=["search"])


# -----------------------------------------------------------------------
# /search/workspace — GET search in file contents (grep-like)
# -----------------------------------------------------------------------
@router.get("/workspace")
def search_workspace(
    query: str = Query(..., description="Patrón de búsqueda"),
    file_pattern: str = Query("*", description="Filtro de archivos (ej: *.py)"),
    case_sensitive: bool = Query(False, description="Búsqueda sensible a mayúsculas"),
    limit: int = Query(50, ge=1, le=500, description="Máximo de resultados"),
    root: str = Query("vcore", description="'vcore' (proyecto) o 'workspace' (adjuntos)"),
) -> dict[str, Any]:
    """
    Busca patrón en el contenido de archivos. read_only → Nivel A.

    El default era workspace/, que solo contiene los adjuntos subidos, así que
    la búsqueda del IDE devolvía 0 resultados para cualquier término de código
    (verificado con 'renderTree', 'def ', 'import', 'Orchestrator').
    """
    try:
        search_root = WORKSPACE_DIR if root == "workspace" else BASE_DIR

        # Gate validation (search_files)
        decision = GATE.evaluate(
            "search_files",
            {"path": str(search_root)},
            agent_id="API",
        )
        if not decision.auto_approved:
            raise HTTPException(
                status_code=403,
                detail=f"Gate rechazó búsqueda: {decision.reason}",
            )

        results = []
        flags = 0 if case_sensitive else re.IGNORECASE
        # Directorios que nunca aportan a una búsqueda de código.
        SKIP_DIRS = {".git", ".venv", ".venv_test", "__pycache__", "node_modules",
                     "chroma_db", ".audit_shots", "dist", "build", ".mypy_cache",
                     ".pytest_cache", ".ruff_cache"}

        try:
            regex = re.compile(query, flags)
        except re.error as e:
            raise HTTPException(
                status_code=400,
                detail=f"Patrón regex inválido: {e}",
            )

        # Buscar en archivos
        # SKIP_FILES: salidas que se regeneran solas y solo agregan ruido.
        SKIP_FILES = {"traces.jsonl", "proactive_log.jsonl", "package-lock.json",
                      "pnpm-lock.yaml", "vcore.db"}
        for filepath in search_root.rglob(file_pattern):
            if filepath.name.startswith("."):
                continue  # Skip hidden files
            if any(part in SKIP_DIRS for part in filepath.parts):
                continue  # Skip ruido (venv, caches, base de datos vectorial)
            if filepath.name in SKIP_FILES:
                continue  # Skip logs y lockfiles

            if filepath.is_file():
                try:
                    content = filepath.read_text(encoding="utf-8", errors="ignore")
                    
                    # Buscar líneas que coinciden
                    for line_no, line in enumerate(content.split("\n"), 1):
                        if regex.search(line):
                            results.append({
                                "file": str(filepath),
                                "line": line_no,
                                "content": line.strip()[:100],  # Limitar contenido
                                "matches": len(regex.findall(line)),
                            })
                            
                            if len(results) >= limit:
                                break

                except (UnicodeDecodeError, OSError):
                    pass  # Skip unreadable files

            if len(results) >= limit:
                break

        return {
            "query": query,
            "file_pattern": file_pattern,
            "case_sensitive": case_sensitive,
            "results": results,
            "total": len(results),
            "limited": len(results) >= limit,
            "gate_decision": {
                "nivel": decision.nivel,
                "auto_approved": True,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error buscando: {e}")


# -----------------------------------------------------------------------
# /search/quick — GET fast filename search
# -----------------------------------------------------------------------
@router.get("/quick")
def search_quick(
    query: str = Query(..., description="Patrón en nombre de archivo"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """
    Búsqueda rápida solo en nombres de archivo (sin leer contenido).
    read_only → Nivel A.
    """
    try:
        # Gate validation
        decision = GATE.evaluate(
            "search_files",
            {"path": str(WORKSPACE_DIR)},
            agent_id="API",
        )
        if not decision.auto_approved:
            raise HTTPException(
                status_code=403,
                detail=f"Gate rechazó búsqueda: {decision.reason}",
            )

        results = []
        flags = re.IGNORECASE

        try:
            regex = re.compile(query, flags)
        except re.error as e:
            raise HTTPException(
                status_code=400,
                detail=f"Patrón regex inválido: {e}",
            )

        # Buscar solo por nombre
        for filepath in WORKSPACE_DIR.rglob("*"):
            if filepath.name.startswith("."):
                continue

            if regex.search(filepath.name):
                results.append({
                    "path": str(filepath),
                    "name": filepath.name,
                    "type": "directory" if filepath.is_dir() else "file",
                })

                if len(results) >= limit:
                    break

        return {
            "query": query,
            "results": results,
            "total": len(results),
            "limited": len(results) >= limit,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en búsqueda rápida: {e}")
