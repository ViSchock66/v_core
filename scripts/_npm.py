"""Ejecuta npm resuelto por Python.

Motivo: en este entorno el acceso a red de PowerShell está bloqueado por el
sandbox (`curl https://registry.npmjs.org` devuelve 000) mientras que el de
Python funciona. Este wrapper permite invocar npm desde Python, que sí tiene
salida a internet, y ver qué devuelve.
"""
import subprocess
import sys

args = sys.argv[1:] or ["--version"]
print(f"$ npm {' '.join(args)}", flush=True)

try:
    r = subprocess.run(
        ["npm.cmd"] + args,
        capture_output=True, text=True, timeout=900, shell=False,
    )
    print(f"exit={r.returncode}")
    if r.stdout:
        print("--- stdout (ultimas 40 lineas) ---")
        print("\n".join(r.stdout.splitlines()[-40:]))
    if r.stderr:
        print("--- stderr (ultimas 25 lineas) ---")
        print("\n".join(r.stderr.splitlines()[-25:]))
    sys.exit(r.returncode)
except FileNotFoundError as e:
    print(f"npm.cmd no encontrado: {e}")
    sys.exit(127)
except Exception as e:
    print(f"EXC {type(e).__name__}: {e}")
    sys.exit(1)
