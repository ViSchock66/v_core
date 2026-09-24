"""
api/approval_broker.py
======================
Aprobación humana **sincrónica** sobre el stream del agente.

Problema que resuelve
---------------------
Cuando la política decide `ask`, el agente tiene que *pausarse* y esperar una
decisión humana. Las dos formas incorrectas de hacerlo:

  1. Cortar el socket SSE y reconectar cuando el usuario responda: se pierde el
     estado del generador y el stream queda a medias.
  2. Devolver `ask` al cliente y hacer polling de la tabla `approvals`: el agente
     ya siguió de largo, así que el usuario aprueba algo que nunca se ejecuta, o
     se ejecuta dos veces.

La forma correcta es pausar el **generador** del loop ReAct, no la conexión: el
`yield` simplemente espera. Este módulo es el registro que conecta esa espera con
el endpoint REST que resuelve la aprobación.

Diseño
------
- `request()` crea un `asyncio.Future` por `request_id` y espera con timeout.
- `resolve()` lo completa desde otro contexto (el handler HTTP).
- **Timeout ⇒ rechazo** (fail-safe): nunca se ejecuta por vencimiento.
- **Idempotencia**: resolver dos veces no hace nada la segunda vez. Cubre doble
  click, reintento de red y replay del frontend.
- El broker es por proceso; las aprobaciones no sobreviven un reinicio, y eso es
  deliberado: una aprobación pendiente de un proceso muerto no debe ejecutarse.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional


DEFAULT_TIMEOUT_S = 300.0


@dataclass
class PendingApproval:
    request_id: str
    tool: str
    agent_id: str
    effect: str
    risk: str
    rule_id: str
    reason: str
    params: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    timeout_s: float = DEFAULT_TIMEOUT_S
    decision: Optional[dict] = None
    resolved: bool = False
    approved: Optional[bool] = None
    resolved_by: Optional[str] = None   # "user" | "timeout" | "cancelled"
    resolved_at: Optional[float] = None

    def public(self) -> dict[str, Any]:
        """Vista para la UI: sin los parámetros crudos (pueden tener secretos)."""
        return {
            "request_id": self.request_id,
            "tool": self.tool,
            "agent_id": self.agent_id,
            "effect": self.effect,
            "risk": self.risk,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "timeout_s": self.timeout_s,
            "resolved": self.resolved,
            "approved": self.approved,
            "resolved_by": self.resolved_by,
        }


class ApprovalBroker:
    """Registro de aprobaciones pendientes ligadas a un stream en curso."""

    def __init__(self, default_timeout_s: float = DEFAULT_TIMEOUT_S):
        self.default_timeout_s = default_timeout_s
        self._waiters: dict[str, asyncio.Future] = {}
        self._pending: dict[str, PendingApproval] = {}
        # Contadores para diagnóstico: la UI muestra tasa de aprobación.
        self.stats = {"requested": 0, "approved": 0, "rejected": 0,
                      "timed_out": 0, "cancelled": 0}

    # ── Ciclo de vida ────────────────────────────────────────────

    async def request(
        self,
        decision: dict[str, Any],
        params: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> bool:
        """Espera la decisión humana. Devuelve True si se aprueba.

        Fail-safe: timeout, cancelación o error ⇒ False (no se ejecuta).
        """
        request_id = str(decision.get("request_id") or "")
        if not request_id:
            # Sin identificador no hay forma de correlacionar la respuesta.
            return False

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters[request_id] = fut

        self._pending[request_id] = PendingApproval(
            request_id=request_id,
            tool=decision.get("tool", ""),
            agent_id=decision.get("agent_id", ""),
            effect=decision.get("effect", "ask"),
            risk=decision.get("risk", "medium"),
            rule_id=decision.get("rule_id", ""),
            reason=decision.get("reason", ""),
            params=params or {},
            timeout_s=timeout_s or self.default_timeout_s,
            decision=decision,
        )
        self.stats["requested"] += 1

        try:
            return await asyncio.wait_for(fut, timeout=timeout_s or self.default_timeout_s)
        except asyncio.TimeoutError:
            self._finish(request_id, approved=False, by="timeout")
            return False
        except asyncio.CancelledError:
            self._finish(request_id, approved=False, by="cancelled")
            raise
        finally:
            self._waiters.pop(request_id, None)

    def resolve(self, request_id: str, approved: bool, by: str = "user") -> bool:
        """Resuelve una aprobación pendiente.

        Idempotente: si el `request_id` ya fue resuelto o no existe, devuelve
        False y no altera nada. Esto es lo que impide que un doble click o un
        reintento ejecute la acción dos veces.
        """
        fut = self._waiters.get(request_id)
        if fut is None or fut.done():
            return False
        self._finish(request_id, approved=approved, by=by)
        fut.set_result(approved)
        return True

    def cancel(self, request_id: str) -> bool:
        """Cancela una aprobación pendiente (p. ej. el usuario detiene el run)."""
        return self.resolve(request_id, approved=False, by="cancelled")

    def cancel_all(self, reason: str = "shutdown") -> int:
        """Rechaza todas las pendientes. Se usa al apagar o al cancelar un run."""
        n = 0
        for rid in list(self._waiters.keys()):
            if self.resolve(rid, approved=False, by="cancelled"):
                n += 1
        return n

    # ── Consulta ─────────────────────────────────────────────────

    def pending(self) -> list[dict[str, Any]]:
        return [p.public() for p in self._pending.values() if not p.resolved]

    def get(self, request_id: str) -> Optional[dict[str, Any]]:
        p = self._pending.get(request_id)
        return p.public() if p else None

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        items = sorted(self._pending.values(), key=lambda p: p.created_at, reverse=True)
        return [p.public() for p in items[:limit]]

    # ── Internos ─────────────────────────────────────────────────

    def _finish(self, request_id: str, approved: bool, by: str) -> None:
        p = self._pending.get(request_id)
        if p is not None:
            p.resolved = True
            p.approved = approved
            p.resolved_by = by
            p.resolved_at = time.time()
        if by == "timeout":
            self.stats["timed_out"] += 1
        elif by == "cancelled":
            self.stats["cancelled"] += 1
        elif approved:
            self.stats["approved"] += 1
        else:
            self.stats["rejected"] += 1


# Singleton de proceso: el stream y el endpoint HTTP deben compartir instancia.
_broker: ApprovalBroker | None = None


def get_broker() -> ApprovalBroker:
    global _broker
    if _broker is None:
        _broker = ApprovalBroker()
    return _broker
