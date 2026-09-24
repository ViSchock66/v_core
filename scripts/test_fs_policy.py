"""Verifica que el arreglo de path traversal no rompa las operaciones legitimas."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

from system.mcp_manager import MCPManager


async def main() -> int:
    m = MCPManager()
    await m.initialize()

    print("--- deja pasar lo legitimo ---")
    casos = [
        ("write_file", {"path": "workspace/_policy_probe.txt", "content": "ok"}),
        ("read_file", {"path": "workspace/_policy_probe.txt"}),
        ("patch_file", {"path": "workspace/_policy_probe.txt", "old": "ok", "new": "ok2"}),
        ("list_files", {"directory": "workspace"}),
    ]
    fallos = 0
    for tool, args in casos:
        r = await m.call(tool, args)
        bad = r.startswith("DENEGADO") or r.startswith("Error")
        fallos += bad
        print(f"  {'FALLA' if bad else 'ok   '} {tool:11s} -> {r[:80]}")

    print("\n--- sigue bloqueando lo malo ---")
    malos = [
        ("write_file", {"path": "../_escape_probe.txt", "content": "no"}),
        ("patch_file", {"path": "../AGENTS.md", "old": "a", "new": "b"}),
        ("list_files", {"directory": "../../../Windows"}),
        ("read_file", {"path": "C:/Windows/win.ini"}),
    ]
    for tool, args in malos:
        r = await m.call(tool, args)
        blocked = r.startswith("DENEGADO")
        fallos += (not blocked)
        print(f"  {'ok   ' if blocked else 'FALLA'} {tool:11s} -> {r[:80]}")

    print("\n--- verificacion en disco ---")
    escaped = Path("../_escape_probe.txt").resolve()
    print("  archivo dentro del workspace:", Path("workspace/_policy_probe.txt").exists())
    print("  archivo escapado (debe ser False):", escaped.exists())
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
