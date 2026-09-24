"""
files_api.py
-----------
File Management API para V-CORE (v4.2).

Endpoints REST para operaciones de archivos:
- GET /files/tree — estructura recursiva del workspace
- GET /files/read — leer contenido de archivo
- POST /files/write — escribir/editar archivo (con Gate)
- POST /files/create-directory — crear carpeta (con Gate)
- GET /files/search — buscar archivos por patrón

Todas las operaciones se validan contra Gate.
Operaciones Nivel B crean approval en lugar de ejecutar inmediatamente.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

# Gate validation
from gate import Gate
# State management
from api import state_bridge as sb

BASE_DIR = Path(__file__).resolve().parent.parent
GATE = Gate(BASE_DIR / "gate_rules.yaml", db_path=BASE_DIR / "vcore.db")
WORKSPACE_DIR = Path(BASE_DIR / "workspace")

router = APIRouter(prefix="/files", tags=["files"])


# -----------------------------------------------------------------------
# Modelos
# -----------------------------------------------------------------------
class FileReadRequest(BaseModel):
    path: str
    encoding: str = "utf-8"


class FileWriteRequest(BaseModel):
    path: str
    content: str
    create_if_missing: bool = True


class DirectoryCreateRequest(BaseModel):
    path: str


class FileSearchRequest(BaseModel):
    pattern: str
    root: str = "workspace"


class FileTreeNode(BaseModel):
    name: str
    type: str  # "file" o "directory"
    path: str
    size: Optional[int] = None
    children: Optional[list["FileTreeNode"]] = None


# -----------------------------------------------------------------------
# Utilidades
# -----------------------------------------------------------------------
def _safe_path(path: str) -> Path:
    """
    Convierte string path a Path, resolviendo a ruta absoluta.
    Prueba relativo a BASE_DIR primero, luego CWD.
    Falla si está fuera de allowed_roots.
    """
    try:
        p = Path(path)
        if not p.is_absolute():
            # Intentar resolver relativo a BASE_DIR
            p = (BASE_DIR / path).resolve()
        else:
            p = p.resolve()
        # Validar que esté dentro de allowed_roots
        for root_str in GATE.allowed_roots:
            if str(p).lower().startswith(str(root_str).lower()):
                return p
        raise ValueError(f"Path fuera de allowed_roots: {p}")
    except Exception as e:
        raise ValueError(f"Path inválido: {path} — {e}")


def _build_tree(dirpath: Path, max_depth: int = 3, current_depth: int = 0) -> FileTreeNode:
    """
    Construye árbol recursivo de directorios.
    Limita profundidad para evitar recursión infinita.
    """
    if current_depth >= max_depth:
        return FileTreeNode(
            name=dirpath.name,
            type="directory",
            path=str(dirpath),
            children=None,
        )

    try:
        children = []
        if dirpath.is_dir():
            # Directorios de build/caches: no aportan a un explorador de
            # archivos y multiplican los nodos (__pycache__ por cada modulo).
            SKIP_TREE_DIRS = {"__pycache__", "node_modules", ".git", ".venv",
                              ".venv_test", "chroma_db", ".audit_shots",
                              ".mypy_cache", ".pytest_cache", ".ruff_cache"}
            SKIP_TREE_EXTS = {".pyc", ".pyo", ".pyd"}
            for item in sorted(dirpath.iterdir()):
                if item.name.startswith("."):
                    continue  # Skip hidden files
                if item.is_dir():
                    if item.name in SKIP_TREE_DIRS:
                        continue
                    children.append(_build_tree(item, max_depth, current_depth + 1))
                else:
                    if item.suffix.lower() in SKIP_TREE_EXTS:
                        continue
                    children.append(
                        FileTreeNode(
                            name=item.name,
                            type="file",
                            path=str(item),
                            size=item.stat().st_size,
                        )
                    )
        return FileTreeNode(
            name=dirpath.name,
            type="directory",
            path=str(dirpath),
            children=children if children else None,
        )
    except Exception as e:
        return FileTreeNode(
            name=dirpath.name,
            type="directory",
            path=str(dirpath),
            children=None,
        )


# -----------------------------------------------------------------------
# /files/tree — GET directory structure
# -----------------------------------------------------------------------
@router.get("/tree")
def get_file_tree(root: str = "vcore", depth: int = 4) -> dict[str, Any]:
    """
    Retorna árbol de directorios.
    root: "vcore" → raíz del proyecto (default: es lo que el usuario espera
                    ver en un explorador de archivos del IDE)
          "workspace" → solo workspace/ (adjuntos y artefactos de sesión)
          <otro> → ruta validada por el gate

    Nota: el default era "workspace", que solo contiene los adjuntos subidos,
    así que el explorador de archivos mostraba únicamente las imágenes de
    prueba y nunca el código del proyecto.
    """
    try:
        if root == "workspace":
            target = WORKSPACE_DIR
        elif root == "vcore":
            target = BASE_DIR
        else:
            target = _safe_path(root)

        # Gate validation (read_only)
        decision = GATE.evaluate("directory_tree", {"path": str(target)}, agent_id="API")
        if not decision.auto_approved:
            raise HTTPException(
                status_code=403,
                detail=f"Gate rechazó acceso: {decision.reason}",
            )

        tree = _build_tree(target, max_depth=max(1, min(depth, 8)))
        return {
            "root": str(target),
            "tree": tree.dict(),
            "gate_decision": {
                "nivel": decision.nivel,
                "auto_approved": decision.auto_approved,
                "reason": decision.reason,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo árbol: {e}")


# -----------------------------------------------------------------------
# /files/read — GET file content
# -----------------------------------------------------------------------
@router.get("/read")
def read_file(path: str, encoding: str = "utf-8") -> dict[str, Any]:
    """
    Lee contenido de un archivo.
    Validado como read_only → siempre Nivel A.
    """
    try:
        target = _safe_path(path)

        # Gate validation (read_only)
        decision = GATE.evaluate("read_file", {"path": str(target)}, agent_id="API")
        if not decision.auto_approved:
            raise HTTPException(
                status_code=403,
                detail=f"Gate rechazó acceso: {decision.reason}",
            )

        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Archivo no existe: {target}")

        if target.is_dir():
            raise HTTPException(status_code=400, detail=f"Es un directorio, no un archivo: {target}")

        content = target.read_text(encoding=encoding)
        return {
            "path": str(target),
            "content": content,
            "size": len(content),
            "encoding": encoding,
            "gate_decision": {
                "nivel": decision.nivel,
                "auto_approved": decision.auto_approved,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"No se puede decodificar como {encoding}: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo archivo: {e}")


# -----------------------------------------------------------------------
# /files/write — POST write/create file
# -----------------------------------------------------------------------
@router.post("/write")
def write_file(body: FileWriteRequest) -> dict[str, Any]:
    """
    Escribe o edita archivo.
    write_scoped → Nivel A si está en workspace/, Nivel B si está fuera.
    
    Si Nivel B: crea approval en lugar de escribir.
    Si Nivel A: escribe directamente.
    """
    try:
        target = _safe_path(body.path)

        # Gate validation (write_scoped)
        decision = GATE.evaluate("write_file", {"path": str(target)}, agent_id="API")

        if not decision.auto_approved:
            # Nivel B: crear approval
            approval_id = sb.create_approval(
                agent_id="API",
                tool="write_file",
                params={"path": str(target), "content": body.content[:100] + "..."},
                reason=f"Escritura de archivo fuera de workspace: {target}",
            )
            return {
                "status": "approval_required",
                "approval_id": approval_id,
                "gate_decision": {
                    "nivel": decision.nivel,
                    "auto_approved": False,
                    "reason": decision.reason,
                },
                "path": str(target),
            }

        # Nivel A: escribir directamente
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content, encoding="utf-8")

        return {
            "status": "written",
            "path": str(target),
            "size": len(body.content),
            "gate_decision": {
                "nivel": decision.nivel,
                "auto_approved": True,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error escribiendo archivo: {e}")


# -----------------------------------------------------------------------
# /files/create-directory — POST create directory
# -----------------------------------------------------------------------
@router.post("/create-directory")
def create_directory(body: DirectoryCreateRequest) -> dict[str, Any]:
    """
    Crea directorio.
    write_scoped → Nivel A si está en workspace/, Nivel B si está fuera.
    """
    try:
        target = _safe_path(body.path)

        # Gate validation (create_directory)
        decision = GATE.evaluate("create_directory", {"path": str(target)}, agent_id="API")

        if not decision.auto_approved:
            # Nivel B: crear approval
            approval_id = sb.create_approval(
                agent_id="API",
                tool="create_directory",
                params={"path": str(target)},
                reason=f"Creación de directorio fuera de workspace: {target}",
            )
            return {
                "status": "approval_required",
                "approval_id": approval_id,
                "gate_decision": {
                    "nivel": decision.nivel,
                    "auto_approved": False,
                },
            }

        # Nivel A: crear directorio
        target.mkdir(parents=True, exist_ok=True)

        return {
            "status": "created",
            "path": str(target),
            "gate_decision": {
                "nivel": decision.nivel,
                "auto_approved": True,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creando directorio: {e}")


# -----------------------------------------------------------------------
# /files/search — GET search files
# -----------------------------------------------------------------------
@router.get("/search")
def search_files(
    pattern: str = Query(..., description="Patrón de búsqueda (*.py, test*)"),
    root: str = Query("workspace", description="Raíz de búsqueda"),
) -> dict[str, Any]:
    """
    Busca archivos por patrón glob.
    read_only → siempre Nivel A.
    """
    try:
        if root == "workspace":
            target = WORKSPACE_DIR
        elif root == "vcore":
            target = BASE_DIR
        else:
            target = _safe_path(root)

        # Gate validation (search_files)
        decision = GATE.evaluate("search_files", {"path": str(target)}, agent_id="API")
        if not decision.auto_approved:
            raise HTTPException(
                status_code=403,
                detail=f"Gate rechazó búsqueda: {decision.reason}",
            )

        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"No es directorio: {target}")

        # Buscar
        results = []
        for match in target.rglob(pattern):
            if not match.name.startswith("."):
                try:
                    results.append({
                        "path": str(match),
                        "size": match.stat().st_size if match.is_file() else None,
                        "type": "file" if match.is_file() else "directory",
                    })
                except:
                    pass

        return {
            "pattern": pattern,
            "root": str(target),
            "results": sorted(results, key=lambda x: x["path"]),
            "count": len(results),
            "gate_decision": {
                "nivel": decision.nivel,
                "auto_approved": True,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error buscando archivos: {e}")


# -----------------------------------------------------------------------
# /files/view — Servir archivos subidos (imagen, PDF, etc.)
# -----------------------------------------------------------------------
from fastapi.responses import FileResponse

@router.get("/view")
def view_file(path: str):
    """Sirve un archivo del workspace/uploads/ para preview en el chat."""
    target = _safe_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Archivo no existe: {path}")
    return FileResponse(str(target))


# -----------------------------------------------------------------------
# /files/upload — File upload pipeline (Pipe 1: carretera de archivos)
# -----------------------------------------------------------------------
from fastapi import UploadFile, File as FileParam

@router.post("/upload")
async def upload_file(
    file: UploadFile = FileParam(...),
    session_id: str = "default",
) -> dict[str, Any]:
    """
    Recibe un archivo (imagen, PDF, MD, código, etc.) y lo guarda en
    workspace/uploads/{session_id}/. Retorna metadata para adjuntar al chat.
    """
    import shutil, time

    upload_dir = BASE_DIR / "workspace" / "uploads" / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Sanitizar nombre
    safe_name = file.filename.replace("\\", "/").split("/")[-1]
    if not safe_name or safe_name.startswith("."):
        safe_name = f"upload_{int(time.time())}{Path(file.filename).suffix}"

    dest = upload_dir / safe_name
    # Evitar sobrescribir — agregar sufijo numérico
    counter = 1
    stem, suffix = dest.stem, dest.suffix
    while dest.exists():
        dest = upload_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    # Guardar archivo
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    # Detectar tipo
    ext = dest.suffix.lower()
    type_map = {
        ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
        ".webp": "image", ".svg": "image", ".bmp": "image",
        ".pdf": "document", ".md": "document", ".txt": "document",
        ".py": "code", ".js": "code", ".html": "code", ".css": "code",
        ".json": "data", ".yaml": "data", ".yml": "data", ".csv": "data",
    }
    file_type = type_map.get(ext, "file")

    size_kb = round(dest.stat().st_size / 1024, 1)

    return {
        "path": str(dest.relative_to(BASE_DIR)).replace("\\", "/"),
        "abs_path": str(dest),
        "name": safe_name,
        "type": file_type,
        "size_kb": size_kb,
        "ext": ext,
        "session_id": session_id,
        "uploaded_at": time.time(),
    }
