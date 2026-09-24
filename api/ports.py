"""
api/ports.py
============
Puerto canonico del backend de V-CORE — fuente unica.

Por que existe
--------------
El proyecto tenia el puerto partido: `start_vcore.bat`, `start_clean.sh`,
`vcore.py` (CLI), `enlil.py`, `system/mcp_servers/*`, `visual_auditor.py` y
`watchdog.py` apuntaban a 8000, mientras el README y `AGENTS.md` decian 8001.
Consecuencia real y verificada: el servidor levantado en 8001 no era
alcanzable por el CLI (`WinError 10061`) ni por la tool `background_task` de
ENLIL, que hacia urlopen a :8000 sin capturar el error.

Regla
-----
Todo el codigo nuevo debe construir URLs con `api_base()`. No escribir
"127.0.0.1:8000" ni ":8001" a mano en ningun modulo.

Precedencia
-----------
1. Variable de entorno VCORE_PORT (o VCORE_API para la URL completa).
2. DEFAULT_PORT.

DEFAULT_PORT es 8000 porque es el valor historico del CLI, los MCP servers y
los scripts de arranque; cambiar el default rompe a quien ya lo tenga
automatizado. El puerto se unifica, no se elige de cero.
"""

from __future__ import annotations

import os

DEFAULT_PORT = 8000
DEFAULT_HOST = "127.0.0.1"


def port() -> int:
    """Puerto del backend, resuelto desde el entorno."""
    raw = os.environ.get("VCORE_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    # Compatibilidad: si alguien definio VCORE_API con un puerto, respetarlo.
    api = os.environ.get("VCORE_API", "")
    if ":" in api:
        try:
            return int(api.rsplit(":", 1)[1].split("/")[0])
        except (ValueError, IndexError):
            pass
    return DEFAULT_PORT


def host() -> str:
    """Host del backend, resuelto desde el entorno."""
    return os.environ.get("VCORE_HOST", DEFAULT_HOST)


def api_base() -> str:
    """URL base del backend, sin slash final. Ej: http://127.0.0.1:8000"""
    return f"http://{host()}:{port()}"


def url(path: str) -> str:
    """URL absoluta a un endpoint. `path` puede venir con o sin slash."""
    if not path.startswith("/"):
        path = "/" + path
    return api_base() + path
