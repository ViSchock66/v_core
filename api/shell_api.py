"""
shell_api.py
-----------
Shell Execution API para V-CORE (v4.2).

Endpoints REST para operaciones de shell:
- POST /shell/execute — ejecutar comando (siempre Nivel B, requiere approval)
- GET /shell/history — historial de comandos ejecutados

Todas las operaciones shell son always_b en Gate,
significa que SIEMPRE requieren aprobación del usuario.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

# Gate validation
from gate import Gate
# State management
from api import state_bridge as sb

BASE_DIR = Path(__file__).resolve().parent.parent
GATE = Gate(BASE_DIR / "gate_rules.yaml", db_path=BASE_DIR / "vcore.db")

router = APIRouter(prefix="/shell", tags=["shell"])


# -----------------------------------------------------------------------
# Modelos
# -----------------------------------------------------------------------
class ShellExecuteRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    timeout: int = 30


class ShellExecuteResponse(BaseModel):
    status: str  # "approval_required", "executed", "approved"
    command: str
    approval_id: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    return_code: Optional[int] = None
    gate_decision: dict[str, Any]


# -----------------------------------------------------------------------
# Historial en memoria (BLOQUE 3 básico)
# TODO BLOQUE 4+: persistir en BD
# -----------------------------------------------------------------------
_shell_history: list[dict[str, Any]] = []


# -----------------------------------------------------------------------
# /shell/execute — POST execute command
# -----------------------------------------------------------------------
@router.post("/execute")
def execute_shell(body: ShellExecuteRequest) -> ShellExecuteResponse:
    """
    Ejecuta comando shell.
    always_b en Gate → SIEMPRE requiere aprobación.
    
    Workflow:
    1. Validar path de trabajo (cwd)
    2. Gate.evaluate() → siempre Nivel B (always_b)
    3. Crear approval
    4. Retornar approval_id (sin ejecutar aún)
    
    Cuando usuario aprueba en frontend → ENLIL ejecuta realmente.
    """
    try:
        # Validar cwd si se proporciona
        if body.cwd:
            cwd_path = Path(body.cwd)
            if not cwd_path.exists():
                raise HTTPException(
                    status_code=400,
                    detail=f"Directorio de trabajo no existe: {body.cwd}",
                )

        # Gate validation (always_b)
        decision = GATE.evaluate(
            "execute_shell",
            {"command": body.command},
            agent_id="API",
        )

        # Gate siempre rechaza execute_shell (always_b)
        if not decision.auto_approved:
            # Crear approval
            approval_id = sb.create_approval(
                agent_id="ENLIL",
                tool="execute_shell",
                params={
                    "command": body.command,
                    "cwd": body.cwd or str(BASE_DIR),
                },
                reason=f"Ejecución de comando shell (Nivel B): {body.command[:50]}",
            )

            return ShellExecuteResponse(
                status="approval_required",
                command=body.command,
                approval_id=approval_id,
                gate_decision={
                    "nivel": decision.nivel,
                    "auto_approved": False,
                    "reason": decision.reason,
                },
            )

        # Nunca debería llegar aquí (always_b siempre es Nivel B)
        raise HTTPException(
            status_code=500,
            detail="Lógica interna: execute_shell debería siempre ser Nivel B",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error preparando ejecución: {e}")


# -----------------------------------------------------------------------
# /shell/execute-approved — POST execute approved command
# -----------------------------------------------------------------------
@router.post("/execute-approved")
def execute_shell_approved(
    approval_id: int = Query(..., description="ID de aprobación"),
    body: ShellExecuteRequest = None,
) -> ShellExecuteResponse:
    """
    Ejecuta comando que fue aprobado.
    Llamado después de que usuario aprueba en Approvals panel.
    
    Seguridad: Se DEBE verificar que approval_id existe y está "approved".
    """
    if not body:
        raise HTTPException(status_code=400, detail="Request body requerido")

    try:
        # Verificar que approval existe y está approved
        approval = sb.get_approval(approval_id)
        if not approval:
            raise HTTPException(
                status_code=404,
                detail=f"Approval {approval_id} no encontrado",
            )

        if approval["status"] != "approved":
            raise HTTPException(
                status_code=409,
                detail=f"Approval debe estar en estado 'approved', está: {approval['status']}",
            )

        # Ejecutar comando
        cwd = body.cwd or str(BASE_DIR)
        result = subprocess.run(
            body.command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            timeout=body.timeout,
            text=True,
        )

        # Registrar en historial
        execution = {
            "timestamp": time.time(),
            "approval_id": approval_id,
            "command": body.command,
            "cwd": cwd,
            "return_code": result.returncode,
            "stdout": result.stdout[:500],  # Limitar salida
            "stderr": result.stderr[:500],
        }
        _shell_history.append(execution)

        # Actualizar state
        sb.update_ultima_accion(f"[SHELL EJECUTADO] {body.command}")

        return ShellExecuteResponse(
            status="executed",
            command=body.command,
            stdout=result.stdout,
            stderr=result.stderr,
            return_code=result.returncode,
            gate_decision={
                "nivel": "A",
                "auto_approved": True,
                "reason": "Aprobado por usuario",
            },
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=408,
            detail=f"Timeout ejecutando comando (>{body.timeout}s)",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error ejecutando comando: {e}")


# -----------------------------------------------------------------------
# /shell/history — GET command history
# -----------------------------------------------------------------------
@router.get("/history")
def get_shell_history(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """
    Retorna historial de comandos ejecutados.
    """
    return {
        "history": _shell_history[-limit:],
        "total": len(_shell_history),
        "note": "Historial en memoria (BLOQUE 3). Post-BLOQUE 3: persistir en BD.",
    }
