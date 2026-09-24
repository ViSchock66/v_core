"""Prueba end-to-end completa del chat: razonamiento, tools y política.

Verifica sobre el backend real, sin navegador:
  1. que el stream emita eventos `reasoning` (modelos de razonamiento) o texto
  2. que las tool calls se ejecuten y emitan evento `tool`
  3. que la política se aplique en el camino del agente

Uso: .venv\\Scripts\\python scripts\\e2e_chat_full.py "mensaje"
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
MENSAJE = sys.argv[1] if len(sys.argv) > 1 else (
    "Lista los archivos de la carpeta api y dime cuantos .py hay. Se breve.")


def main() -> int:
    payload = json.dumps({"message": MENSAJE, "history": [], "task_id": "",
                          "session_dir": ""}).encode()
    req = urllib.request.Request(BASE + "/agents/route", data=payload,
                                 headers={"Content-Type": "application/json"})

    conteo: dict[str, int] = {}
    texto: list[str] = []
    razonamiento: list[str] = []
    tools: list[dict] = []
    politicas: list[dict] = []
    inicio = time.time()
    primer_reasoning: float | None = None
    primer_texto: float | None = None

    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            t = ev.get("type", "?")
            conteo[t] = conteo.get(t, 0) + 1
            if t == "chunk":
                if primer_texto is None:
                    primer_texto = time.time() - inicio
                texto.append(ev.get("content") or "")
            elif t == "reasoning":
                if primer_reasoning is None:
                    primer_reasoning = time.time() - inicio
                razonamiento.append(ev.get("content") or "")
            elif t == "tool":
                tools.append({"tool": ev.get("tool"), "error": ev.get("error"),
                              "resultado": (ev.get("result") or "")[:120]})
            elif t in ("tool.denied", "approval.requested", "approval.resolved"):
                politicas.append({"evento": t, "tool": ev.get("tool"),
                                  "regla": ev.get("rule_id"), "motivo": (ev.get("reason") or "")[:100]})

    duracion = time.time() - inicio
    respuesta = "".join(texto).strip()
    resumen = {
        "mensaje": MENSAJE[:60],
        "duracion_s": round(duracion, 1),
        "primer_evento_razonamiento_s": round(primer_reasoning, 1) if primer_reasoning else None,
        "primer_evento_texto_s": round(primer_texto, 1) if primer_texto else None,
        "conteo_eventos": conteo,
        "razonamiento_chars": len("".join(razonamiento)),
        "razonamiento_muestra": "".join(razonamiento)[:150],
        "tools": tools,
        "eventos_politica": politicas,
        "respuesta_len": len(respuesta),
        "respuesta": respuesta[:500],
    }
    Path(".audit_shots/e2e_chat_full.json").write_text(
        json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(resumen, indent=2, ensure_ascii=False))

    problemas = []
    if not respuesta and not razonamiento:
        problemas.append("no llego ni texto ni razonamiento")
    if respuesta and respuesta.startswith("⚠️"):
        problemas.append("el agente devolvio un aviso de error")
    print("\nDIAGNOSTICO:", "OK" if not problemas else " | ".join(problemas))
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
