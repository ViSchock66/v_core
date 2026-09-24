"""
system/mcp_servers/vcore_audit_mcp.py
=======================================
MCP Server: vcore-audit — implementación real con tipos MCP estándar.

Tools: visual_audit, web_search
"""

from __future__ import annotations

import asyncio
import traceback
import sys
from pathlib import Path

from system.mcp_manager import LocalMCPServer

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def create_audit_server() -> LocalMCPServer:
    """Factory que crea y registra el server vcore-audit."""
    srv = LocalMCPServer("vcore-audit")

    srv.register_tool(
        name="visual_audit",
        description=(
            "Audita visualmente el frontend: abre la UI con Playwright, toma un "
            "screenshot, verifica que los elementos clave existan y sean visibles, "
            "prueba interacciones (abrir/cerrar paneles) y captura errores de consola. "
            "Devuelve bugs estructurales reales. Usalo cuando el usuario pida revisar "
            "como se ve la interfaz o reporte un problema visual."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "instructions": {
                    "type": "string",
                    "description": "Qué revisar en particular (opcional)",
                },
                "analyze": {
                    "type": "boolean",
                    "description": "Si True, además del DOM envía el screenshot a un modelo de visión",
                },
            },
            "required": [],
        },
        handler=_visual_audit,
    )

    srv.register_tool(
        name="web_search",
        description="Busca en internet usando DuckDuckGo Instant Answer.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Términos de búsqueda",
                },
            },
            "required": ["query"],
        },
        handler=_web_search,
    )

    return srv


# =========================================================================
# Handlers
# =========================================================================

async def _visual_audit(instructions: str = "", analyze: bool = True) -> str:
    """Auditoría visual del frontend.

    La tool estaba ANUNCIADA en el prompt del agente (`enlil.py` la lista como
    acción disponible) pero no registrada en el catálogo MCP: `mcp.call`
    respondía "herramienta desconocida". Tres referencias muertas en total —
    el catálogo, `TaskGraphEngine._run_visual_audit_sync` y
    `TaskGraphEngine._run_visual_audit` — mientras la implementación real
    (`system/visual_auditor.py`) quedaba huérfana. Ahora está cableada de punta
    a punta.
    """
    from system.task_graph_engine import TaskGraphEngine

    try:
        engine = TaskGraphEngine()
        return await engine._run_visual_audit(instructions or "", bool(analyze))
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return f"visual_audit falló: {type(e).__name__}: {e}"


async def _web_search(query: str) -> str:
    """Búsqueda web via TaskGraphEngine."""
    query = query.strip()
    if not query:
        return "Error: query vacía"

    try:
        from system.task_graph_engine import TaskGraphEngine

        engine = TaskGraphEngine()
        return await engine._web_search(query)
    except Exception as e:
        return f"web_search falló: {e}"
