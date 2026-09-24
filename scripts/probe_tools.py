"""Prueba de capacidad: ¿qué modelos vivos soportan tool calling nativo?

Es la capacidad crítica de V-CORE: sin `tool_calls` el agente no puede ejecutar
nada y cae al parser de texto, que es menos confiable.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()
from openai import AsyncOpenAI  # noqa: E402

KEY = os.getenv("NVIDIA_API_KEY", "").strip()

# Tool mínima con el nombre exacto del catálogo real, para que la prueba sea
# representativa del caso de uso de V-CORE.
TOOL = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "Lista archivos de un directorio del proyecto.",
        "parameters": {
            "type": "object",
            "properties": {"directory": {"type": "string"}},
            "required": ["directory"],
        },
    },
}

CANDIDATOS = [
    "z-ai/glm-5.3",
    "moonshotai/kimi-k3",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
]


async def probar(modelo: str) -> dict:
    client = AsyncOpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=KEY,
                         timeout=120)
    try:
        resp = await client.chat.completions.create(
            model=modelo,
            messages=[{"role": "user",
                       "content": "Necesito ver que hay en el directorio docs. Usa la herramienta."}],
            tools=[TOOL],
            tool_choice="auto",
            max_tokens=600,
            temperature=0,
        )
        msg = resp.choices[0].message
        tcs = getattr(msg, "tool_calls", None)
        llamadas = []
        for tc in (tcs or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {"_raw": (tc.function.arguments or "")[:60]}
            llamadas.append({"name": tc.function.name, "args": args})
        return {
            "modelo": modelo,
            "ok": bool(llamadas),
            "tool_calls": llamadas,
            "content": (msg.content or "")[:80],
            "finish": resp.choices[0].finish_reason,
        }
    except Exception as e:
        return {"modelo": modelo, "ok": False, "error": f"{type(e).__name__}: {str(e)[:110]}"}


async def main() -> int:
    print(f"{'TOOL CALL':10s} {'MODELO':46s} RESULTADO")
    print("-" * 118)
    resultados = []
    for m in CANDIDATOS:
        r = await probar(m)
        resultados.append(r)
        if r.get("ok"):
            detalle = json.dumps(r["tool_calls"], ensure_ascii=False)[:60]
        else:
            detalle = r.get("error") or f"respondio texto: {r.get('content','')[:40]!r}"
        print(f"{'SI' if r.get('ok') else 'NO':10s} {m:46s} {detalle}")

    Path(".audit_shots/tool_calling.json").write_text(
        json.dumps(resultados, indent=2, ensure_ascii=False), encoding="utf-8")
    apoyan = [r["modelo"] for r in resultados if r.get("ok")]
    print("-" * 118)
    print(f"con tool calling: {apoyan}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
