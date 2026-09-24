"""
system/mcp_servers/vcore_fs_mcp.py
====================================
MCP Server: vcore-fs — implementación real con tipos MCP estándar.

Tools: read_file, write_file, patch_file, list_files, search_code

Migrado desde system/mcp_client.py::VCoreFSServer (MCPServer custom).
Usa LocalMCPServer + mcp.types.Tool / TextContent.
"""

from __future__ import annotations

import os
import tempfile
import shutil
import subprocess
from pathlib import Path

from system.mcp_manager import LocalMCPServer

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SEARCH_DIRS = ["agents", "web", "system", "api"]


def create_fs_server() -> LocalMCPServer:
    """Factory que crea y registra el server vcore-fs con todas sus tools."""
    srv = LocalMCPServer("vcore-fs")

    # ── read_file ──────────────────────────────────────────────────
    srv.register_tool(
        name="read_file",
        description=(
            "Lee un archivo del proyecto. Usa offset y limit para archivos grandes "
            "(ej: offset=980, limit=30 para ver una función específica). "
            "Si path es un directorio, lista su contenido."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta del archivo (relativa a la raíz del proyecto)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Línea donde empezar (opcional, default 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Máximo de líneas a leer (opcional, default 100)",
                },
            },
            "required": ["path"],
        },
        handler=_read_file,
    )

    # ── write_file ─────────────────────────────────────────────────
    srv.register_tool(
        name="write_file",
        description=(
            "Escribe o sobreescribe un archivo completo. "
            "Crea directorios intermedios si no existen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta del archivo (relativa a la raíz del proyecto)",
                },
                "content": {
                    "type": "string",
                    "description": "Contenido completo a escribir",
                },
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
    )

    # ── patch_file ─────────────────────────────────────────────────
    srv.register_tool(
        name="patch_file",
        description=(
            "Edita un archivo existente con find-and-replace exacto. "
            "Busca 'old' y lo reemplaza con 'new'. "
            "IMPORTANTE: 'old' debe ser texto EXACTO del archivo — "
            "cópialo con read_file primero."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta del archivo",
                },
                "old": {
                    "type": "string",
                    "description": "Texto exacto a buscar (cópialo del archivo)",
                },
                "new": {
                    "type": "string",
                    "description": "Texto de reemplazo",
                },
            },
            "required": ["path", "old", "new"],
        },
        handler=_patch_file,
    )

    # ── list_files ─────────────────────────────────────────────────
    srv.register_tool(
        name="list_files",
        description="Lista archivos y directorios en una ruta dada.",
        input_schema={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directorio a listar (relativo a la raíz, default '.')",
                },
            },
        },
        handler=_list_files,
    )

    # ── search_code ────────────────────────────────────────────────
    srv.register_tool(
        name="search_code",
        description=(
            "Busca texto en el código del proyecto (grep recursivo). "
            "Busca en agents/, web/, system/, api/. "
            "Úsala antes de patch_file para encontrar la función exacta."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Texto a buscar (string literal o regex básico)",
                },
            },
            "required": ["query"],
        },
        handler=_search_code,
    )

    return srv


# =========================================================================
# Handlers (misma lógica que los viejos, adaptados a async MCP)
# =========================================================================

def _resolve(path_str: str) -> Path:
    """Resuelve un path contra BASE_DIR y **verifica que quede dentro del repo**.

    Antes esta función era `p if p.is_absolute() else BASE_DIR / p`: no colapsaba
    `..` ni resolvía symlinks, y no comprobaba nada. Consecuencia verificada
    (explotada el 2026-09-23):

        read_file("../../../Windows/System32/drivers/etc/hosts")

    devolvía el contenido real de un archivo del sistema operativo. Como el gate
    no se consulta en el bucle del agente, la cadena de escape estaba completa:
    el modelo podía leer y escribir cualquier archivo del equipo.

    Esto es defensa en profundidad, no la política: la política decide *si* una
    acción se permite, y esta función garantiza que el primitivo de filesystem
    **no pueda** salirse del root ni aunque la política falle o alguien olvide
    consultarla. La resolución se hace con `resolve()`, así que symlinks y `..`
    se colapsan antes de comparar (evita el bypass trivial por enlace).
    """
    p = Path(path_str)
    if not p.is_absolute():
        p = BASE_DIR / p
    resolved = p.resolve()

    root = BASE_DIR.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise PermissionError(
            f"Path fuera del root permitido: '{path_str}' resuelve a '{resolved}', "
            f"que no está dentro de '{root}'."
        )
    return resolved


def _safe_resolve(path_str: str) -> tuple[Path | None, str | None]:
    """`_resolve` sin excepción: devuelve (path, error_legible).

    Los handlers necesitan responder al modelo con un mensaje en vez de
    propagar una excepción: el agente debe poder leer que la acción fue
    denegada y explicárselo al usuario, no recibir un stack trace.
    """
    try:
        return _resolve(path_str), None
    except PermissionError as e:
        return None, f"DENEGADO por política de rutas: {e}"
    except Exception as e:
        return None, f"Error resolviendo '{path_str}': {e}"


def _read_file(path: str, offset: int = 0, limit: int = 100) -> str:
    p, err = _safe_resolve(path)
    if err:
        return err
    if not p.exists():
        return f"Error: no encontrado: {p}"

    if p.is_dir():
        return _list_files(directory=str(p))

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error: no se pudo leer {p}: {e}"

    lines = content.split("\n")
    total = len(lines)
    offset = int(offset or 0)
    limit = int(limit or 100)

    if offset > 0:
        lines = lines[offset - 1:]
    if limit and len(lines) > limit:
        lines = lines[:limit]

    start = offset + 1 if offset else 1
    end = start + len(lines) - 1
    rel = p.relative_to(BASE_DIR) if p.is_relative_to(BASE_DIR) else p
    return f"Archivo: {rel} (líneas {start}-{end} de {total}):\n" + "\n".join(lines)


def _write_file(path: str, content: str) -> str:
    p, err = _safe_resolve(path)
    if err:
        return err
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=p.parent, delete=False
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        shutil.move(tmp_path, p)
    except Exception as e:
        return f"Error: no se pudo escribir {p}: {e}"

    rel = p.relative_to(BASE_DIR) if p.is_relative_to(BASE_DIR) else p
    return f"Archivo escrito: {rel} ({len(content)} chars)"


def _patch_file(path: str, old: str, new: str) -> str:
    p, err = _safe_resolve(path)
    if err:
        return err
    if not p.exists():
        return f"Error: archivo no encontrado: {p}"
    if not old:
        return "Error: 'old' no puede estar vacío"

    try:
        content = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error: no se pudo leer {p}: {e}"

    if old not in content:
        preview = content[:120].replace("\n", "↵")
        return (
            f"Error: texto 'old' no encontrado en {p}.\n"
            f"Primeras 120 chars del archivo: {preview}\n"
            f"Usa read_file con offset/limit para copiar el texto exacto."
        )

    content = content.replace(old, new, 1)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=p.parent
        ) as tmp:
            tmp.write(content)
            temp_path = Path(tmp.name)
        os.replace(temp_path, p)
    except Exception as e:
        return f"Error: no se pudo escribir {p}: {e}"

    rel = p.relative_to(BASE_DIR) if p.is_relative_to(BASE_DIR) else p
    return f"Archivo parcheado: {rel} (reemplazo exitoso)"


def _list_files(directory: str = ".") -> str:
    p, err = _safe_resolve(directory)
    if err:
        return err
    if not p.exists():
        return f"Directorio no encontrado: {p}"
    if not p.is_dir():
        return f"No es un directorio: {p}"

    try:
        items = sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name))
    except Exception as e:
        return f"Error: no se pudo listar {p}: {e}"

    lines = []
    for entry in items[:60]:
        tipo = "📁" if entry.is_dir() else "📄"
        size_str = ""
        if entry.is_file():
            s = entry.stat().st_size
            if s >= 1_000_000: size_str = f" ({s/1_000_000:.1f} MB)"
            elif s >= 1_000: size_str = f" ({s/1_000:.1f} KB)"
            else: size_str = f" ({s} B)"
        lines.append(f"  {tipo} {entry.name}{size_str}")

    rel = p.relative_to(BASE_DIR) if p.is_relative_to(BASE_DIR) else p
    header = f"{rel}/ — {len(items)} elementos:"
    if len(items) > 60:
        header += f" [mostrando primeros 60]"
    return header + "\n" + "\n".join(lines)


def _search_code(query: str) -> str:
    query = query.strip()
    if not query:
        return "Error: query vacía"

    search_paths = [
        str(BASE_DIR / d) for d in SEARCH_DIRS if (BASE_DIR / d).exists()
    ]
    if not search_paths:
        return "Error: no se encontraron directorios de código"

    try:
        result = subprocess.run(
            [
                "grep", "-rn",
                "--include=*.py", "--include=*.js", "--include=*.html",
                "--include=*.css", "--include=*.yaml",
                query, *search_paths,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(BASE_DIR),
        )
        output = result.stdout[:2500]
        if not output:
            return f"Sin resultados para: '{query}'"

        lines = output.strip().split("\n")
        clean = []
        for line in lines:
            for d in SEARCH_DIRS:
                full = str(BASE_DIR / d)
                if line.startswith(full):
                    line = line.replace(full, d, 1)
                    break
            clean.append(line)

        return f"Resultados para '{query}' ({len(clean)} líneas):\n" + "\n".join(clean)
    except subprocess.TimeoutExpired:
        return "Error: search_code excedió timeout de 10s"
    except Exception as e:
        return f"Error en search_code: {e}"
