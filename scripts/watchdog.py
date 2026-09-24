#!/usr/bin/env python3
"""
watchdog.py — Mantiene uvicorn vivo. Si crashea, reinicia.

Uso:
    python scripts/watchdog.py [--port N] [--max-restarts N]

El puerto sale de api/ports.py (VCORE_PORT / default) para no divergir del
resto del sistema. El directorio de trabajo se deriva de la ubicacion de
este archivo, no de una ruta absoluta de una maquina concreta.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from api.ports import port as _default_port  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=_default_port())
parser.add_argument("--max-restarts", type=int, default=10)
args = parser.parse_args()

restarts = 0
while restarts < args.max_restarts:
    print(f"[watchdog] Iniciando uvicorn en puerto {args.port} (intento {restarts+1})")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app",
         "--host", "127.0.0.1", "--port", str(args.port)],
        cwd=str(ROOT),
    )
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("[watchdog] Detenido por usuario")
        proc.terminate()
        break

    restarts += 1
    print(f"[watchdog] uvicorn murió (código {proc.returncode}). Reiniciando en 3s...")
    time.sleep(3)

print(f"[watchdog] Límite de reinicios alcanzado ({args.max_restarts}). Saliendo.")
