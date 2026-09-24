#!/usr/bin/env bash
# Arranca el backend de V-CORE con PYTHONPATH limpio (evita que se cuele el
# venv de otro entorno).
#
# El puerto sale de VCORE_PORT; si no esta definido, usa el default de
# api/ports.py. Asi el CLI, los MCP servers y Orchestrator apuntan al mismo lugar.
set -e
cd "$(dirname "$0")"
export PYTHONPATH=""
PORT="${VCORE_PORT:-8000}"
echo "[V-CORE] Iniciando backend en http://127.0.0.1:${PORT}"
exec .venv/Scripts/python.exe -m uvicorn api.main:app \
  --host 127.0.0.1 --port "$PORT" "$@"
