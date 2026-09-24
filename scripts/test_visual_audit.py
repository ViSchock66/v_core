"""Prueba de la tool visual_audit de punta a punta.

La llama por el camino real (mcp.call), que es como la invoca el agente.
Usa analyze=False para no gastar una llamada al modelo de visión.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

from system.mcp_manager import MCPManager


async def main() -> int:
    m = MCPManager()
    await m.initialize()

    print("visual_audit en el catalogo:", "visual_audit" in m._tool_index)
    print("invocando la tool (analyze=False)...\n")

    raw = await m.call("visual_audit", {"instructions": "revisar el layout general", "analyze": False})
    print(f"respuesta cruda ({len(raw)} chars):")
    print(raw[:400])
    print()

    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"FALLA: la respuesta no es JSON valido: {e}")
        return 1

    print("status      :", data.get("status"))
    print("app_state   :", data.get("app_state"))
    print("screenshot  :", data.get("screenshot"))
    print("bug_count   :", data.get("bug_count"))
    print("findings    :", data.get("findings"))
    print("console_err :", len(data.get("console_errors") or []))
    print("interacciones:", data.get("interaction_audit"))

    shot = data.get("screenshot")
    existe = bool(shot) and Path(shot).exists()
    print(f"\nscreenshot en disco: {existe}")
    problemas = []
    if data.get("status") != "completed":
        problemas.append(f"status={data.get('status')} error={data.get('error')}")
    if not existe:
        problemas.append("no se genero el screenshot")
    print("DIAGNOSTICO:", "OK" if not problemas else " | ".join(problemas))
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
