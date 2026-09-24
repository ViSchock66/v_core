#!/usr/bin/env python3
"""
V-Core MCP Bridge — expone los MCP servers internos de V-Core (vcore-fs, vcore-shell, vcore-audit)
como un servidor MCP estándar (stdio) que Hermes puede conectar.

Arquitectura:
  Hermes (MCP client)  <stdio>  vcore_mcp_bridge.py  <in-process>  V-Core MCPClient + 3 servers

Esto permite a Hermes usar: visual_audit, search_code, read_file, write_file, patch_file,
list_files, execute_command, background_task — sin duplicar lógica SQL ni reinventar ruedas.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Asegurar que V-Core esté en el path (raíz del repo, desde este archivo)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from system.mcp_manager import MCPManager


class VCoreMCPBridge:
    """
    Bridge que adapta MCPClient interno de V-Core al protocolo MCP stdio estándar.
    """

    def __init__(self):
        self.manager = MCPManager()
        self._initialized = False

    async def _ensure_init(self):
        """Inicializa el MCP Manager (lazy)."""
        if self._initialized:
            return
        await self.manager.initialize()
        self._initialized = True

    async def handle_request(self, request: dict) -> dict:
        """Procesa un request MCP y retorna response."""
        await self._ensure_init()
        
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params", {})

        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "vcore-mcp-bridge", "version": "1.0.0"},
                    },
                }

            elif method == "tools/list":
                tools_mcp = await self.manager.list_tools()
                tools = []
                for tool in tools_mcp:
                    tools.append({
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema,
                    })
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

            elif method == "tools/call":
                tool_name = params.get("name")
                tool_params = params.get("arguments", {})
                result = await self.manager.call(tool_name, tool_params)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": result}]},
                }

            elif method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
            }

    async def run_stdio(self):
        """Loop principal stdio MCP - Windows compatible."""
        import sys
        import os
        
        # Use direct stdin/stdout reading/writing on Windows
        loop = asyncio.get_event_loop()
        
        # Create a synchronous reader/writer for Windows
        def read_line():
            line = sys.stdin.readline()
            if not line:
                return None
            return line.strip()
        
        def write_response(response):
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        
        try:
            while True:
                # Read in thread pool to avoid blocking
                line = await loop.run_in_executor(None, read_line)
                if line is None:
                    break
                if not line:
                    continue
                    
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue
                    
                response = await self.handle_request(request)
                await loop.run_in_executor(None, write_response, response)
                
        except (KeyboardInterrupt, EOFError):
            pass


async def main():
    bridge = VCoreMCPBridge()
    await bridge.run_stdio()


if __name__ == "__main__":
    asyncio.run(main())