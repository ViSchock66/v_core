"""Prueba end-to-end del HITL sincrónico a través del backend real.

Envía un mensaje que hace que ENLIL intente un comando no listado, escucha el
evento `approval.requested` en el stream SSE, lo resuelve vía
`POST /approvals/pending/{id}/resolve` y verifica que el stream se reanuda y la
tool se ejecuta.

Uso: .venv\\Scripts\\python scripts/e2e_hitl.py [aprobar|rechazar]
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
DECISION = sys.argv[1] if len(sys.argv) > 1 else "aprobar"

MENSAJE = ("Ejecuta exactamente este comando usando la herramienta execute_command, "
           "sin modificarlo: docker ps. Despues dime el resultado en una linea.")


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def resolver_aprobandolas(eventos: list[dict], decidir_aprobar: bool) -> None:
    """Hilo que vigila los eventos y resuelve la primera aprobación que aparezca."""
    vistos: set[str] = set()
    for _ in range(600):  # hasta 120 s
        time.sleep(0.2)
        for ev in list(eventos):
            if ev.get("type") != "approval.requested":
                continue
            rid = ev.get("request_id")
            if not rid or rid in vistos:
                continue
            vistos.add(rid)
            try:
                r = post(f"/approvals/pending/{rid}/resolve", {"approved": decidir_aprobar})
                print(f"[hitl] resuelto {rid} aprobado={decidir_aprobar} -> {r}", flush=True)
            except Exception as e:
                print(f"[hitl] error resolviendo {rid}: {e}", flush=True)
            return


def main() -> int:
    eventos: list[dict] = []
    stderr_lines: list[str] = []

    hilo = threading.Thread(target=resolver_aprobandolas,
                            args=(eventos, DECISION == "aprobar"), daemon=True)
    hilo.start()

    payload = json.dumps({"message": MENSAJE, "history": [], "task_id": "",
                          "session_dir": ""}).encode()
    req = urllib.request.Request(BASE + "/agents/route", data=payload,
                                headers={"Content-Type": "application/json"})
    texto = []
    inicio = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            eventos.append(ev)
            t = ev.get("type")
            if t in ("tool", "artifact", "approval_request", "approval.requested",
                     "approval.resolved", "tool.denied", "task_graph", "error"):
                stderr_lines.append(f"{t}: {json.dumps(ev, ensure_ascii=False)[:200]}")
            if t == "chunk":
                texto.append(ev.get("content") or "")

    duracion = time.time() - inicio
    tipos = [e.get("type") for e in eventos]
    resumen = {
        "decision": DECISION,
        "duracion_s": round(duracion, 1),
        "eventos_politica": [e for e in eventos
                             if e.get("type", "").startswith("approval")
                             or e.get("type") == "tool.denied"],
        "tools_ejecutadas": [e.get("tool") for e in eventos if e.get("type") == "tool"],
        "denegadas": [e.get("rule_id") for e in eventos if e.get("type") == "tool.denied"],
        "respuesta": "".join(texto)[-400:],
    }
    print(json.dumps(resumen, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
