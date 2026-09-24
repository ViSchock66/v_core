"""
system/mcp_servers/vcore_shell_mcp.py
=======================================
MCP Server: vcore-shell — implementación real con tipos MCP estándar.

Tools: execute_command, background_task
"""

from __future__ import annotations

import json
import subprocess
import urllib.request as _ur
from pathlib import Path

from system.mcp_manager import LocalMCPServer
from api.ports import api_base

BASE_DIR = Path(__file__).resolve().parent.parent.parent
# Fuente unica del puerto (api/ports.py).
BACKEND_URL = api_base()


def create_shell_server() -> LocalMCPServer:
    """Factory que crea y registra el server vcore-shell."""
    srv = LocalMCPServer("vcore-shell")

    srv.register_tool(
        name="execute_command",
        description=(
            "Ejecuta un comando shell y retorna stdout + stderr. "
            "Timeout 30s. Úsalo para correr scripts, instalar deps, "
            "verificar procesos, o cualquier operación de sistema."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Comando shell a ejecutar",
                },
            },
            "required": ["command"],
        },
        handler=_execute_command,
    )

    srv.register_tool(
        name="background_task",
        description=(
            "Lanza un comando de larga duración en background y retorna "
            "inmediatamente con un task_id. El resultado se notifica al "
            "usuario cuando termina. Úsalo para builds, tests largos, "
            "o cualquier proceso que tome más de 10s."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Comando a ejecutar en background",
                },
                "label": {
                    "type": "string",
                    "description": "Nombre descriptivo de la tarea (opcional)",
                },
            },
            "required": ["command"],
        },
        handler=_background_task,
    )

    return srv


# =========================================================================
# Handlers
# =========================================================================

# `cwd` llega desde la API al crear el approval (/shell/execute guarda
# {"command":..., "cwd":...}), y el dispatcher pasa los params como kwargs.
# Sin aceptarlo, aprobar un comando fallaba con
# "_execute_command() got an unexpected keyword argument 'cwd'".
def _execute_command(command: str, cwd: str | None = None) -> str:
    command = command.strip()
    if not command:
        return "Error: comando vacío"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd or str(BASE_DIR),
        )
        stdout = result.stdout[:1500] if result.stdout else ""
        stderr = result.stderr[:500] if result.stderr else ""
        parts = [f"Exit: {result.returncode}"]
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return "Error: comando excedió timeout de 30s"
    except Exception as e:
        return f"Error ejecutando comando: {e}"


def _background_task(command: str, label: str = "") -> str:
    command = command.strip()
    label = label or command[:60]
    if not command:
        return "Error: comando vacío"

    try:
        req = _ur.Request(
            f"{BACKEND_URL}/tasks/background",
            data=json.dumps({"command": command, "label": label}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = _ur.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        task_id = data.get("task_id", "?")
        return (
            f"Tarea lanzada en background: {task_id}\n"
            f"Label: {label}\n"
            f"El resultado llegará como notificación cuando termine."
        )
    except Exception as e:
        return f"Error lanzando background task: {e}"
