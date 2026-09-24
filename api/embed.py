"""
api/embed.py
============
Capa unificada de embeddings para SHAMASH y NISABA.

La versión NO se escribe acá: la fuente única es VCORE_STATE.json leída por
api/version.py (ver docs/DOCUMENTATION.md). Un número hardcodeado en un
docstring queda obsoleto en el siguiente bump.

3-tier fallback:
  1. Ollama local (nomic-embed-text) — $0, privado, 768 dims
  2. NVIDIA NIM (nemotron-3-embed-1b) — cloud, requiere API key, 2048 dims
  3. Hash determinístico SHA-256 expandido — 768 dims — último recurso

Historial de bugs corregidos (2026-09-23):
  - El tier 2 apuntaba a `nvidia/nv-embedqa-e5-v5`, retirado por NVIDIA el
    2026-08-25 (respuesta 410 Gone). La capa caía al tier 3 sin avisar.
  - `_embed_fallback` declaraba 768 dimensiones pero devolvía 32 (un SHA-256
    son 32 bytes y nunca se expandía). Consecuencia real y verificada: la
    colección `nem0_memory` quedó con vectores de 32 dims mientras
    `shamash_memory` tenía 768 — la memoria partida en dos espacios vectoriales
    incompatibles, con búsquedas semánticas silenciosamente degradadas.
  - El tier 2 no leía las 4 keys del credential pool de `llm_client.py`.

Uso:
    from api.embed import embed, embed_async, embed_with_source

    vec = embed("texto a embeber")               # sync  (SHAMASH)
    vec = await embed_async("texto a embeber")   # async (NISABA)
    vec, src = embed_with_source("texto")        # + procedencia

IMPORTANTE: dos vectores de tiers distintos NO son comparables. Nunca mezclar
espacios vectoriales en una misma colección. `embed_with_source()` existe para
que quien persiste pueda guardar y verificar la procedencia.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx

# ── Configuración ──────────────────────────────────────────────────

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
# `nvidia/nv-embedqa-e5-v5` fue retirado (410 Gone, 2026-08-25). El reemplazo
# vivo se verifico contra GET /v1/models + POST /v1/embeddings: devuelve 2048
# dimensiones.
NIM_EMBED_MODEL = os.getenv("NIM_EMBED_MODEL", "nvidia/nemotron-3-embed-1b")
NIM_EMBED_URL = "https://integrate.api.nvidia.com/v1/embeddings"
FALLBACK_DIMS = 768
OLLAMA_TIMEOUT = 10.0
NIM_TIMEOUT = 15.0

# Estado de proceso para la infraestructura vectorial compartida.
_DIMS_CACHE: int | None = None
_CHROMA_CLIENT = None


def _nim_api_key() -> str:
    """Resuelve la key de NIM.

    Primero el entorno; si no, el credential pool de `api/llm_client.py`, que
    tiene 4 keys rotativas. Antes esta capa solo miraba `NVIDIA_API_KEY` y
    quedaba ciega si el pool era la fuente real.
    """
    key = os.getenv("NVIDIA_API_KEY", "").strip()
    if key:
        return key
    for env_name in ("NVIDIA_KEY_MAIN", "NVIDIA_KEY_FALLBACK", "NVIDIA_KEY_AUX"):
        key = os.getenv(env_name, "").strip()
        if key:
            return key
    try:
        from dotenv import load_dotenv

        load_dotenv()
        key = os.getenv("NVIDIA_API_KEY", "").strip()
        if key:
            return key
        for env_name in ("NVIDIA_KEY_MAIN", "NVIDIA_KEY_FALLBACK", "NVIDIA_KEY_AUX"):
            key = os.getenv(env_name, "").strip()
            if key:
                return key
    except Exception:
        pass
    return ""


# ── Tier 1: Ollama local ──────────────────────────────────────────

def _embed_ollama(text: str) -> list[float] | None:
    """Intenta embedding via Ollama local (nomic-embed-text)."""
    try:
        resp = httpx.post(
            f"{OLLAMA_HOST}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        emb = resp.json().get("embedding", [])
        if emb:
            return emb
    except Exception:
        pass
    return None


async def _embed_ollama_async(text: str) -> list[float] | None:
    """Versión async — embedding via Ollama local."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OLLAMA_HOST}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text},
                timeout=OLLAMA_TIMEOUT,
            )
            resp.raise_for_status()
            emb = resp.json().get("embedding", [])
            if emb:
                return emb
    except Exception:
        pass
    return None


# ── Tier 2: NVIDIA NIM cloud ──────────────────────────────────────

def _nim_payload(text: str) -> dict:
    return {
        "input": [text],
        "model": NIM_EMBED_MODEL,
        "encoding_format": "float",
        "input_type": "query",
    }


def _embed_nim(text: str) -> list[float] | None:
    """Intenta embedding via NVIDIA NIM (nemotron-3-embed-1b, 2048 dims)."""
    nim_key = _nim_api_key()
    if not nim_key:
        return None
    try:
        resp = httpx.post(
            NIM_EMBED_URL,
            headers={
                "Authorization": f"Bearer {nim_key}",
                "Content-Type": "application/json",
            },
            json=_nim_payload(text),
            timeout=NIM_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            emb = data.get("data", [{}])[0].get("embedding", [])
            if emb:
                return emb
    except Exception:
        pass
    return None


async def _embed_nim_async(text: str) -> list[float] | None:
    """Versión async — embedding via NVIDIA NIM."""
    nim_key = _nim_api_key()
    if not nim_key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                NIM_EMBED_URL,
                headers={
                    "Authorization": f"Bearer {nim_key}",
                    "Content-Type": "application/json",
                },
                json=_nim_payload(text),
                timeout=NIM_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                emb = data.get("data", [{}])[0].get("embedding", [])
                if emb:
                    return emb
    except Exception:
        pass
    return None


# ── Tier 3: Hash fallback ─────────────────────────────────────────

def _embed_fallback(text: str, dims: int = FALLBACK_DIMS) -> list[float]:
    """Vector determinístico de `dims` dimensiones derivado del texto.

    El SHA-256 del texto son 32 bytes: si se devolvían tal cual, el vector
    tenía 32 dimensiones y no las 768 declaradas. Se expande encadenando
    hashes con contador (patrón de expansión tipo KDF) hasta cubrir `dims`, y
    se normaliza a [0, 1). Es determinístico pero NO semántico: dos textos
    distintos producen vectores casi ortogonales, así que sirve para no romper
    el sistema, no para recuperar por significado.
    """
    if dims <= 0:
        return []
    out: list[float] = []
    counter = 0
    while len(out) < dims:
        digest = hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest()
        out.extend(b / 255.0 for b in digest)
        counter += 1
    return out[:dims]


# ── API pública ───────────────────────────────────────────────────

def _spec(tier: str, provider: str, model: str, dims: int) -> dict:
    return {"tier": tier, "provider": provider, "model": model, "dims": dims}


def embed_with_source(text: str) -> tuple[list[float], dict]:
    """Embedding + procedencia (tier, provider, modelo, dimensiones).

    Quien persista vectores debe guardar la procedencia: mezclar espacios
    vectoriales en una colección es la causa raíz del bug de 32 vs 768 dims.
    """
    result = _embed_ollama(text)
    if result:
        return result, _spec("ollama", "ollama", EMBED_MODEL, len(result))

    result = _embed_nim(text)
    if result:
        return result, _spec("nim", "nvidia", NIM_EMBED_MODEL, len(result))

    vec = _embed_fallback(text)
    return vec, _spec("fallback", "local", "sha256-expandido", len(vec))


async def embed_with_source_async(text: str) -> tuple[list[float], dict]:
    """Versión async de `embed_with_source`."""
    result = await _embed_ollama_async(text)
    if result:
        return result, _spec("ollama", "ollama", EMBED_MODEL, len(result))

    result = await _embed_nim_async(text)
    if result:
        return result, _spec("nim", "nvidia", NIM_EMBED_MODEL, len(result))

    vec = _embed_fallback(text)
    return vec, _spec("fallback", "local", "sha256-expandido", len(vec))


def embed(text: str) -> list[float]:
    """
    Genera embedding con 3-tier fallback (sync).
    Ollama → NIM → hash determinístico.
    Siempre retorna un vector no vacío.
    """
    return embed_with_source(text)[0]


async def embed_async(text: str) -> list[float]:
    """
    Genera embedding con 3-tier fallback (async).
    Ollama → NIM → hash determinístico.
    Siempre retorna un vector no vacío.
    """
    return (await embed_with_source_async(text))[0]


def probe() -> dict:
    """Diagnóstico de disponibilidad de cada tier (una llamada por tier).

    Pensado para el endpoint de estado: responde "¿qué está sirviendo los
    embeddings ahora mismo y con cuántas dimensiones?" sin adivinar.
    """
    tiers: list[dict] = []

    try:
        v = _embed_ollama("probe")
        tiers.append({"tier": "ollama", "model": EMBED_MODEL, "ok": bool(v),
                      "dims": len(v) if v else 0})
    except Exception as e:
        tiers.append({"tier": "ollama", "model": EMBED_MODEL, "ok": False,
                      "dims": 0, "error": str(e)[:120]})

    try:
        v = _embed_nim("probe")
        tiers.append({"tier": "nim", "model": NIM_EMBED_MODEL, "ok": bool(v),
                      "dims": len(v) if v else 0})
    except Exception as e:
        tiers.append({"tier": "nim", "model": NIM_EMBED_MODEL, "ok": False,
                      "dims": 0, "error": str(e)[:120]})

    fb = _embed_fallback("probe")
    tiers.append({"tier": "fallback", "model": "sha256-expandido", "ok": True,
                  "dims": len(fb)})

    active = next((t for t in tiers if t["ok"]), tiers[-1])
    return {"active": active, "tiers": tiers, "has_nim_key": bool(_nim_api_key())}


# ── Infraestructura vectorial compartida ──────────────────────────
#
# Vive acá (y no en cada agente) porque SHAMASH y NISABA escriben en el mismo
# almacén Chroma y deben coincidir en ruta, nombre de colección y espacio
# vectorial. Antes cada uno definía los suyos: SHAMASH usaba `chroma_db/` y
# NISABA `knowledge/.chromadb/` — un directorio que no existía, así que
# ChromaDB lo creaba vacío en silencio y el RAG quedaba desconectado de todos
# los embeddings ya calculados.

CHROMA_DIR = Path(__file__).resolve().parent.parent / "chroma_db"


def embedding_dims() -> int:
    """Dimensiones del tier de embedding activo (con cache por proceso)."""
    global _DIMS_CACHE
    if _DIMS_CACHE is None:
        try:
            _DIMS_CACHE = int(embed_with_source("dims probe")[1]["dims"])
        except Exception:
            _DIMS_CACHE = 0
    return _DIMS_CACHE


def reset_dims_cache() -> None:
    """Invalida el cache de dimensiones (si cambia el tier disponible)."""
    global _DIMS_CACHE
    _DIMS_CACHE = None


def collection_name(base: str) -> str:
    """Nombre de colección versionado por ancho de vector.

    Las dimensiones son parte de la identidad de la colección: incluir el
    ancho en el nombre hace imposible volver a mezclar espacios vectoriales,
    que fue el bug de origen (`nem0_memory` con 32 dims y `shamash_memory`
    con 768, ambas vivas a la vez).
    """
    dims = embedding_dims()
    return f"{base}_{dims}" if dims else base


def chroma_client():
    """Cliente ChromaDB persistente compartido (o None si no está disponible)."""
    global _CHROMA_CLIENT
    if _CHROMA_CLIENT is None:
        try:
            import chromadb
            CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            _CHROMA_CLIENT = chromadb.PersistentClient(path=str(CHROMA_DIR))
        except Exception:
            _CHROMA_CLIENT = False
    return _CHROMA_CLIENT if _CHROMA_CLIENT is not False else None
