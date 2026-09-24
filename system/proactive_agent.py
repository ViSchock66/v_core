#!/usr/bin/env python3
"""
system/proactive_agent.py
==========================
Agente proactivo de V-CORE — detecta inactividad, monitorea salud del sistema,
y sugiere tareas automáticamente.

Responsabilidades:
  1. Monitorear inactividad del usuario (última acción)
  2. Chequear salud del sistema (Ollama, API, DB, disco)
  3. Generar sugerencias automáticas basadas en estado
  4. Reportar alertas cuando circuit breakers están abiertos

Uso:
    from system.proactive_agent import ProactiveAgent
    agent = ProactiveAgent()
    suggestions = agent.check()
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from api.ports import url

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = BASE_DIR / "VCORE_STATE.json"
LOG_PATH = BASE_DIR / "proactive_log.jsonl"

# Umbrales de inactividad (segundos)
INACTIVITY_WARN = 120       # 2 min → sugerencia suave
INACTIVITY_ALERT = 300      # 5 min → sugerencia fuerte
INACTIVITY_CRITICAL = 600   # 10 min → sugerencia crítica

# Checks de salud.
# Ollama es externo: se consulta por HTTP. "api" y "frontend" apuntan al
# propio backend, y cuando este codigo corre dentro del server se resuelven
# in-process (ver _running_inside_server) para no auto-bloquearse.
HEALTH_CHECKS = {
    "ollama": {"url": "http://127.0.0.1:11434/api/tags", "label": "Ollama"},
    "api": {"url": url("/health"), "label": "API"},
    "frontend": {"url": url("/"), "label": "Frontend"},
}


def _read_state() -> dict:
    """Lee VCORE_STATE.json."""
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_state(state: dict):
    """Escribe VCORE_STATE.json."""
    try:
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _check_url(url: str, timeout: int = 5) -> dict:
    """Checkea si una URL responde."""
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=timeout)
        return {"status": resp.status, "ok": 200 <= resp.status < 400}
    except Exception as e:
        return {"status": 0, "ok": False, "error": str(e)[:80]}


def _running_inside_server() -> bool:
    """True si este proceso ES el backend (uvicorn ya arranco la app).

    El agente proactivo corre dentro del proceso del servidor, y ahi
    preguntarse por HTTP "¿esta vivo el servidor?" es un auto-bloqueo: con
    un solo worker, la peticion que el server se hace a si mismo no se
    atiende hasta que termina el handler actual, asi que se agota el timeout
    y el chequeo reporta ok=False con el backend perfectamente sano.
    Se detecta por el modulo cargado, no por una variable que haya que setear.
    """
    return "api.main" in sys.modules


def _check_self_health() -> dict:
    """Estado del propio backend sin salir por la red."""
    return {"status": 200, "ok": True, "detail": "in-process"}


def _get_circuit_status_inproc() -> dict:
    """Circuit breakers leidos del router en memoria (sin HTTP)."""
    from api.llm_client import get_router
    return get_router().get_circuit_status()


def _check_disk() -> dict:
    """Checkea espacio en disco."""
    try:
        import shutil
        usage = shutil.disk_usage(BASE_DIR)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        pct_free = (usage.free / usage.total) * 100
        return {
            "free_gb": round(free_gb, 1),
            "total_gb": round(total_gb, 1),
            "pct_free": round(pct_free, 1),
            "ok": pct_free > 10,
        }
    except Exception as e:
        return {"error": str(e)[:80], "ok": True}  # Failsafe: no alertar si no se puede medir


def _get_inactivity_since(state: dict) -> float:
    """Retorna segundos desde la última acción real."""
    last_action = state.get("ultima_accion_real", "")
    # Parsear timestamp si existe en el string
    # Fallback: asumir que fue hace 0s si no hay data
    if not last_action:
        return 0
    # Intentar extraer timestamp de ultima_accion_real
    # Formato: "Orchestrator respondió consulta simple | task_id=..."
    # No tiene timestamp explícito, usamos fecha de actualización del state
    state_updated = state.get("fecha_actualizacion", "")
    if state_updated:
        try:
            dt = datetime.strptime(state_updated, "%Y-%m-%dT%H:%M:%S%z" if "T" in state_updated else "%Y-%m-%dT%H:%M:%S")
            return time.time() - dt.timestamp()
        except (ValueError, TypeError):
            pass
    # Si no se puede determinar, retornar 0
    return 0


def _log_proactive(action: str, data: dict):
    """Loggea acciones proactivas en JSONL."""
    try:
        LOG_PATH.parent.mkdir(exist_ok=True)
        entry = {
            "timestamp": time.time(),
            "action": action,
            **data,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


class ProactiveAgent:
    """
    Agente proactivo de V-CORE.
    Monitorea salud del sistema y genera sugerencias automáticas.
    """

    def __init__(self):
        self._last_check = 0.0
        self._min_check_interval = 30  # segundos entre chequeos
    
    def check(self) -> dict:
        """
        Ejecuta chequeo completo del sistema.
        
        Returns:
            dict con:
            - health: estado de servicios (ollama, api, frontend, disco)
            - inactivity: segundos de inactividad
            - suggestions: lista de sugerencias automáticas
            - alerts: alertas críticas
            - circuit_breakers: estado de circuit breakers
        """
        now = time.time()
        if now - self._last_check < self._min_check_interval:
            return {"status": "cached", "last_check": self._last_check}
        
        self._last_check = now
        state = _read_state()
        
        # ── Health checks ──
        # Los servicios EXTERNOS (ollama) se chequean por HTTP. El propio
        # backend y su frontend NO: preguntarse a si mismo por HTTP se
        # bloquea hasta el timeout y reportaba ok=False con el server sano
        # (13s de latencia y una alerta critica falsa).
        inproc = _running_inside_server()
        health = {}
        for name, cfg in HEALTH_CHECKS.items():
            if inproc and name in ("api", "frontend"):
                health[name] = _check_self_health()
            else:
                health[name] = _check_url(cfg["url"])
        health["disk"] = _check_disk()
        
        # ── Inactivity ──
        inactivity = _get_inactivity_since(state)
        
        # ── Circuit breakers ──
        circuit_breakers = {}
        if inproc:
            try:
                circuit_breakers = _get_circuit_status_inproc()
            except Exception as e:
                circuit_breakers = {"error": f"router no disponible: {type(e).__name__}"}
        else:
            try:
                req = urllib.request.Request(
                    url("/llm/circuit-status"),
                    method="GET",
                )
                resp = urllib.request.urlopen(req, timeout=3)
                circuit_breakers = json.loads(resp.read().decode())
            except Exception:
                circuit_breakers = {"error": "API no disponible"}
        
        # ── Sugerencias automáticas ──
        suggestions = []
        alerts = []
        
        # Basadas en salud
        if not health.get("ollama", {}).get("ok", True):
            alerts.append({
                "severity": "warning",
                "message": "Ollama no responde (embeddings locales no disponibles).",
                "action": "Verifica que Ollama esté corriendo en http://127.0.0.1:11434. Los embeddings usarán fallback NIM → hash.",
            })
        
        if not health.get("api", {}).get("ok", True):
            alerts.append({
                "severity": "critical",
                "message": "API backend no responde.",
                "action": "Reinicia con: `python vcore.py serve`",
            })
        
        if not health.get("disk", {}).get("ok", True):
            alerts.append({
                "severity": "warning",
                "message": f"Disco bajo: {health['disk'].get('free_gb', '?')}GB libres",
                "action": "Libera espacio en disco",
            })
        
        # Basadas en circuit breakers
        for provider, status in circuit_breakers.items():
            if isinstance(status, dict) and status.get("state") == "OPEN":
                alerts.append({
                    "severity": "warning",
                    "message": f"Circuit breaker {provider} está OPEN",
                    "action": "Revisa logs del provider y resetea con /llm/reset-circuit",
                })
        
        # Basadas en inactividad
        if inactivity > INACTIVITY_CRITICAL:
            suggestions.append({
                "priority": "high",
                "type": "health_check",
                "message": "Llevas más de 10 min sin interactuar. ¿Quieres que ejecute una auditoría completa del sistema?",
                "command": "/loop audit full system",
            })
        elif inactivity > INACTIVITY_ALERT:
            suggestions.append({
                "priority": "medium",
                "type": "maintenance",
                "message": "¿Quieres que revise el estado del proyecto y sugiera mejoras?",
                "command": "/audit",
            })
        elif inactivity > INACTIVITY_WARN:
            suggestions.append({
                "priority": "low",
                "type": "tip",
                "message": "Disponible: Polish Loop para auto-mejora iterativa. Usa /loop para comenzar.",
                "command": "/help",
            })
        
        # Basadas en estado del sistema
        pending_approvals = state.get("agentes_pendientes", [])
        if pending_approvals:
            suggestions.append({
                "priority": "high",
                "type": "approval",
                "message": f"Tienes {len(pending_approvals)} aprobaciones pendientes.",
                "command": None,
            })
        
        # Siempre: sugerencia de mantenimiento preventivo
        if len(suggestions) < 2:
            suggestions.append({
                "priority": "low",
                "type": "tip",
                "message": "Usa /viz para un screenshot rápido del frontend o /help para ver todos los comandos.",
                "command": "/help",
            })
        
        result = {
            "status": "ok",
            "timestamp": now,
            "health": {
                service: {
                    "ok": info.get("ok", False),
                    "label": HEALTH_CHECKS.get(service, {}).get("label", service),
                    "detail": info.get("status", info.get("free_gb", info.get("error", ""))),
                }
                for service, info in health.items()
            },
            "inactivity_seconds": int(inactivity),
            "suggestions": suggestions[:3],
            "alerts": alerts[:3],
            "circuit_breakers": circuit_breakers,
        }
        
        # Loggear
        _log_proactive("check", {
            "alerts": len(alerts),
            "suggestions": len(suggestions),
            "inactivity": int(inactivity),
        })
        
        return result
    
    def get_health_summary(self) -> str:
        """Retorna un resumen legible de salud del sistema."""
        result = self.check()
        
        lines = ["## 🏥 Salud del Sistema\n"]
        
        # Servicios
        lines.append("### Servicios")
        for service, info in result.get("health", {}).items():
            icon = "✅" if info.get("ok") else "❌"
            detail = info.get("detail", "")
            lines.append(f"  {icon} {info.get('label', service)} ({detail})")
        lines.append("")
        
        # Inactividad
        inact = result.get("inactivity_seconds", 0)
        if inact > 0:
            mins = inact // 60
            secs = inact % 60
            lines.append(f"### ⏱ Inactividad: {mins}m {secs}s")
        lines.append("")
        
        # Alertas
        alerts = result.get("alerts", [])
        if alerts:
            lines.append("### ⚠ Alertas")
            for a in alerts:
                icon = "🔴" if a.get("severity") == "critical" else "🟡"
                lines.append(f"  {icon} {a['message']}")
            lines.append("")
        
        # Sugerencias
        suggestions = result.get("suggestions", [])
        if suggestions:
            lines.append("### 💡 Sugerencias")
            for s in suggestions:
                priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(s.get("priority", "low"), "💡")
                lines.append(f"  {priority_icon} {s['message']}")
            lines.append("")
        
        return "\n".join(lines)


# =============================================================================
# Async wrapper para uso en API / agent loop
# =============================================================================

async def check_system_health() -> dict:
    """Wrapper async para check síncrono."""
    import asyncio
    agent = ProactiveAgent()
    return await asyncio.to_thread(agent.check)


async def get_health_summary() -> str:
    """Wrapper async para resumen de salud."""
    import asyncio
    agent = ProactiveAgent()
    return await asyncio.to_thread(agent.get_health_summary)