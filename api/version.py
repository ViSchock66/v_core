"""
api/version.py
==============
Fuente única de verdad para la versión de V-CORE.

Todos los módulos importan de aquí. Nadie hardcodea una versión.
VCORE_STATE.json es el storage autoritativo — este módulo es el lector.
"""

from pathlib import Path
import json

_STATE_PATH = Path(__file__).resolve().parent.parent / "VCORE_STATE.json"

def read() -> str:
    """Lee la versión desde VCORE_STATE.json. Fallback: '0.0.0'."""
    try:
        if _STATE_PATH.exists():
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("version", "0.0.0")
    except Exception:
        pass
    return "0.0.0"

def bump(new_version: str) -> str:
    """Escribe una nueva versión en VCORE_STATE.json. Retorna la versión nueva."""
    try:
        state = {}
        if _STATE_PATH.exists():
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        state["version"] = new_version
        from datetime import datetime, timezone
        state["fecha_actualizacion"] = datetime.now(timezone.utc).isoformat()
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        return new_version
    except Exception as e:
        print(f"[version] Error bumping to {new_version}: {e}")
        return read()

# Singleton — se lee una vez al importar
VERSION = read()
