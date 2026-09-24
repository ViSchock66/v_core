"""Prueba qué modelos del catálogo NIM responden de verdad con credenciales reales.

Distingue tres estados:
  - vivo      : responde 200 con contenido
  - retirado  : 410 Gone (el modelo fue dado de baja por NVIDIA)
  - sin acceso: 404 (existe el nombre pero la cuenta no lo tiene habilitado)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

import httpx  # noqa: E402

KEY = None
for name in ("NVIDIA_API_KEY", "NVIDIA_KEY_MAIN", "NVIDIA_KEY_FALLBACK", "NVIDIA_KEY_AUX"):
    import os
    v = os.getenv(name, "").strip()
    if v:
        KEY = v
        break

BASE = "https://integrate.api.nvidia.com/v1"


def listar() -> list[str]:
    r = httpx.get(f"{BASE}/models", headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
    return sorted(m["id"] for m in r.json().get("data", []))


def probar(modelo: str) -> tuple[str, str]:
    try:
        r = httpx.post(
            f"{BASE}/chat/completions",
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            json={"model": modelo, "messages": [{"role": "user", "content": "di ok"}],
                  "max_tokens": 8, "temperature": 0},
            timeout=60,
        )
        if r.status_code == 200:
            data = r.json()
            contenido = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
            return "vivo", f"responde: {contenido[:30]!r}"
        if r.status_code == 410:
            return "retirado", "410 Gone"
        if r.status_code == 404:
            return "sin_acceso", "404 sin acceso para esta cuenta"
        return "error", f"HTTP {r.status_code}: {r.text[:80]}"
    except Exception as e:
        return "error", f"{type(e).__name__}: {str(e)[:80]}"


def main() -> int:
    if not KEY:
        print("sin key")
        return 1
    modelos = listar()
    print(f"modelos listados para la cuenta: {len(modelos)}\n")

    # Candidatos: los que el proyecto usa o podría usar para roles de código.
    candidatos = [
        "z-ai/glm-5.3", "z-ai/glm-5.3-flash", "moonshotai/kimi-k3",
        "deepseek-ai/deepseek-v4.1-flash", "nvidia/nemotron-3.5-lightning-30b-a3b",
        "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b",
        "nvidia/nemotron-nano-3-30b-a3b", "openai/gpt-oss-20b",
        "nvidia/llama-3.1-nemotron-ultra-253b-v1", "nvidia/llama-3.1-nemotron-70b-instruct",
        "mistralai/codestral-22b-instruct-v0.1", "mistralai/mistral-large-2-instruct",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    ]
    candidatos = [c for c in candidatos if c in modelos] + [c for c in candidatos if c not in modelos]

    resultados: dict[str, dict] = {}
    print(f"{'ESTADO':12s} {'MODELO':52s} DETALLE")
    print("-" * 118)
    for m in candidatos:
        estado, detalle = probar(m)
        resultados[m] = {"estado": estado, "detalle": detalle}
        print(f"{estado:12s} {m:52s} {detalle}")
        time.sleep(0.4)

    Path(".audit_shots/model_health.json").write_text(
        json.dumps({"total_catalogo": len(modelos), "resultados": resultados},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    vivos = [m for m, r in resultados.items() if r["estado"] == "vivo"]
    print("-" * 118)
    print(f"VIVOS ({len(vivos)}): {vivos}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
