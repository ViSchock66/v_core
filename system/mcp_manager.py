"""
system/mcp_manager.py
=====================
MCP Manager — implementación real del protocolo MCP para V-CORE.

Carga servidores desde mcp_config.json:
  - type: "local" → servidores in-process (vcore-fs, vcore-shell, vcore-audit)
  - type: "stdio" → servidores externos via stdio_client
  - type: "http"  → servidores externos via streamablehttp_client

Usa tipos estándar MCP (mcp.types.Tool, TextContent) para definición de tools
y resultados. La interfaz que ve ENLIL no cambia — get_tools_catalog() y call().

Migración desde system/mcp_client.py (custom dispatcher, 160 LOC).
"""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from typing import Any

# ── Tipos MCP estándar ──────────────────────────────────────────────────
from mcp.types import Tool, TextContent

# ── Config ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE_DIR / "mcp_config.json"


# =========================================================================
# LOCAL SERVER — in-process, zero-overhead, MCP types
# =========================================================================

class LocalMCPServer:
    """Servidor MCP que corre en el mismo proceso — sin subprocess, sin memory streams.

    Expone tools con tipos MCP estándar (Tool, TextContent) y se registra
    via register_tool(). Los handlers reciben **kwargs y retornan str.
    """

    def __init__(self, name: str):
        self.name = name
        self._tool_map: dict[str, Tool] = {}
        self._handler_map: dict[str, callable] = {}

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: callable,
    ) -> None:
        """Registra una tool con su schema JSON y handler async/sync."""
        self._tool_map[name] = Tool(
            name=name,
            description=description,
            inputSchema=input_schema,
        )
        self._handler_map[name] = handler

    async def list_tools(self) -> list[Tool]:
        return list(self._tool_map.values())

    async def call_tool(self, name: str, arguments: dict) -> list[TextContent]:
        handler = self._handler_map.get(name)
        if not handler:
            return [TextContent(type="text", text=f"Tool '{name}' not found on server '{self.name}'")]
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(**arguments)
            else:
                result = handler(**arguments)
            return [TextContent(type="text", text=str(result))]
        except Exception as e:
            traceback.print_exc()
            return [TextContent(type="text", text=f"Error executing '{name}': {e}")]


# =========================================================================
# EXTERNAL SERVER — real MCP stdio/HTTP client
# =========================================================================

class ExternalMCPServer:
    """Servidor MCP externo — conecta via stdio_client o streamablehttp_client."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self._cleanup = None  # callable para cerrar la conexión
        self._session = None
        self._tools: list[Tool] = []

    async def connect(self) -> None:
        if "command" in self.config:
            from mcp.client.stdio import stdio_client, StdioServerParameters

            params = StdioServerParameters(
                command=self.config["command"],
                args=self.config.get("args", []),
            )
            # stdio_client retorna un async context manager
            ctx = stdio_client(params)
            read_stream, write_stream = await ctx.__aenter__()
            self._cleanup = lambda: ctx.__aexit__(None, None, None)

        elif "url" in self.config:
            from mcp.client.streamable_http import streamablehttp_client

            ctx = streamablehttp_client(
                self.config["url"],
                headers=self.config.get("headers", {}),
            )
            read_stream, write_stream = await ctx.__aenter__()
            self._cleanup = lambda: ctx.__aexit__(None, None, None)

        else:
            raise ValueError(
                f"MCP server '{self.name}': must have 'command' (stdio) or 'url' (HTTP)"
            )

        from mcp.client.session import ClientSession

        self._session = ClientSession(read_stream, write_stream)
        await self._session.initialize()

        # Discover tools
        result = await self._session.list_tools()
        self._tools = list(result.tools)
        print(f"[MCPManager] External server '{self.name}': {len(self._tools)} tools discovered")

    async def list_tools(self) -> list[Tool]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict) -> list[TextContent]:
        if not self._session:
            return [TextContent(type="text", text=f"Server '{self.name}' not connected")]
        try:
            result = await self._session.call_tool(name, arguments)
            return list(result.content)
        except Exception as e:
            return [TextContent(type="text", text=f"MCP call '{name}' failed: {e}")]


# =========================================================================
# MCP MANAGER — punto central de tools
# =========================================================================

class MCPManager:
    """Manager central MCP para V-CORE.

    Reemplaza system/mcp_client.py::MCPClient. Misma interfaz (get_tools_catalog,
    call), pero backed por el protocolo MCP real con tipos estándar.

    Uso:
        manager = MCPManager()
        await manager.initialize()

        # ENLIL usa esto para el system prompt
        tools = manager.get_tools_catalog()

        # ENLIL dispatcha tools así
        result = await manager.call("read_file", {"path": "agents/ENLIL/enlil.py"})
    """

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or DEFAULT_CONFIG
        self._servers: dict[str, LocalMCPServer | ExternalMCPServer] = {}
        self._tool_index: dict[str, str] = {}  # tool_name → server_name

    # ── Init ──────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Carga mcp_config.json, conecta servers externos, registra locales."""
        config = {}
        if self.config_path.exists():
            config = json.loads(self.config_path.read_text(encoding="utf-8"))

        servers_config = config.get("mcpServers", {})

        for server_name, server_cfg in servers_config.items():
            # Skip disabled servers
            if server_cfg.get("enabled") is False:
                print(f"[MCPManager] Server '{server_name}' is disabled — skipping")
                continue

            server_type = server_cfg.get("type", "stdio")

            if server_type == "local":
                await self._init_local(server_name, server_cfg)
            else:
                await self._init_external(server_name, server_cfg)

        # Siempre registrar servers locales built-in de V-CORE
        await self._register_builtin_servers()

        self._rebuild_index()

    async def _init_local(self, name: str, cfg: dict) -> None:
        """Servidores locales se registran via _register_builtin_servers()."""
        pass  # Los built-in se registran en _register_builtin_servers

    async def _init_external(self, name: str, cfg: dict) -> None:
        """Conecta a un servidor MCP externo (stdio o HTTP)."""
        connect_timeout = cfg.get("connect_timeout", 30)
        try:
            server = ExternalMCPServer(name, cfg)
            await asyncio.wait_for(server.connect(), timeout=connect_timeout)
            self._servers[name] = server
            tools_count = len(await server.list_tools())
            print(f"[MCPManager] Connected to '{name}': {tools_count} tools")
        except asyncio.TimeoutError:
            print(f"[MCPManager] WARN: timeout ({connect_timeout}s) connecting to '{name}' — skipping")
        except asyncio.CancelledError:
            print(f"[MCPManager] WARN: '{name}' connection cancelled — skipping")
            raise  # re-raise to let the caller handle the cancel properly
        except Exception as e:
            print(f"[MCPManager] WARN: failed to connect '{name}': {type(e).__name__}: {e}")

    async def _register_builtin_servers(self) -> None:
        """Registra los 3 servidores locales de V-CORE (fs, shell, audit)."""
        from system.mcp_servers.vcore_fs_mcp import create_fs_server
        from system.mcp_servers.vcore_shell_mcp import create_shell_server
        from system.mcp_servers.vcore_audit_mcp import create_audit_server

        self._servers["vcore-fs"] = create_fs_server()
        self._servers["vcore-shell"] = create_shell_server()
        self._servers["vcore-audit"] = create_audit_server()

        print(f"[MCPManager] Registered 3 built-in local servers: vcore-fs, vcore-shell, vcore-audit")

    def _rebuild_index(self) -> None:
        """Reconstruye tool_name → server_name para dispatch rápido."""
        self._tool_index.clear()
        for srv_name, server in self._servers.items():
            # list_tools() is async but tools are cached after register
            # For local servers, we can access _tool_map directly
            if isinstance(server, LocalMCPServer):
                for tool_name in server._tool_map:
                    self._tool_index[tool_name] = srv_name
            else:
                for tool in server._tools:
                    self._tool_index[tool.name] = srv_name

    # ── Public API (misma interfaz que el viejo MCPClient) ────────────

    async def get_tools_catalog(self) -> list[dict]:
        """Retorna tools en formato OpenAI-compatible (para system prompt de ENLIL).

        Cada tool: {name, description, parameters: {type: "object", properties: {...}}}
        """
        catalog = []
        for server in self._servers.values():
            tools = await server.list_tools()
            for tool in tools:
                catalog.append(self._tool_to_openai(tool))
        return catalog

    @staticmethod
    def _tool_to_openai(tool: Tool) -> dict:
        """Convierte Tool MCP a formato OpenAI function calling."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }

    def get_valid_tool_names(self) -> set[str]:
        """Set de nombres de tools disponibles (para validación en agent loop)."""
        return set(self._tool_index.keys())

    async def call(self, tool_name: str, params: dict) -> str:
        """Dispatch unificado — misma firma que el viejo MCPClient.call().

        Resuelve la tool al server correcto y retorna resultado como string.
        """
        # Resolver aliases (compatibilidad con viejo MCPClient)
        resolved = self._resolve_alias(tool_name)
        server_name = self._tool_index.get(resolved)

        if not server_name:
            available = sorted(self._tool_index.keys())
            return (
                f"Herramienta desconocida: '{tool_name}'. "
                f"Disponibles: {available}"
            )

        server = self._servers[server_name]
        try:
            result = await server.call_tool(resolved, params)
            # Extraer texto de los TextContent
            return "\n".join(
                item.text for item in result
                if hasattr(item, "text") and item.text
            )
        except Exception as e:
            return f"Error ejecutando {tool_name}: {e}"

    @staticmethod
    def _resolve_alias(name: str) -> str:
        """Normaliza aliases legacy (mismos que el viejo MCPClient)."""
        aliases = {
            "leer": "read_file",
            "read": "read_file",
            "escribir": "write_file",
            "crear_archivo": "write_file",
            "patch": "patch_file",
            "editar": "patch_file",
            "exec": "execute_command",
            "shell": "execute_command",
            "run": "execute_command",
            "list": "list_files",
            "ls": "list_files",
            "dir": "list_files",
        }
        return aliases.get(name, name)

    # ── Status / debug ────────────────────────────────────────────────

    def status(self) -> dict:
        """Retorna estado de todos los servers para /agents/status."""
        servers_status = {}
        for name, srv in self._servers.items():
            if isinstance(srv, LocalMCPServer):
                servers_status[name] = {
                    "type": "local",
                    "tools": len(srv._tool_map),
                    "connected": True,
                }
            else:
                servers_status[name] = {
                    "type": "external",
                    "tools": len(srv._tools),
                    "connected": srv._session is not None,
                }
        return {
            "total_servers": len(self._servers),
            "total_tools": len(self._tool_index),
            "servers": servers_status,
        }
