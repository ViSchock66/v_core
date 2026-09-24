"""
agents/Curator/memory.py
=========================
Motor de memoria de Curator — deduplicación, búsqueda semántica, historial.

Principios:
- Privado: cero llamadas externas. Embeddings via Ollama local.
- Durable: SQLite para historial + ChromaDB para búsqueda semántica.
- Dedup: hash SHA-256 del contenido antes de insertar.
- API limpia: add() / search() / update() / delete() / history().
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from api.embed import (
    CHROMA_DIR,
    chroma_client,
    embed,
    embed_with_source,
    embedding_dims,
)

# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).resolve().parent.parent.parent
DB_PATH     = BASE_DIR / "vcore.db"
# Almacén Chroma compartido con Retriever (ver api/embed.py): una sola ruta, un
# solo cliente. Tener dos rutas fue la causa de que la memoria y el RAG
# vivieran en bases distintas.
CHROMA_PATH = CHROMA_DIR

# Base del nombre de colección. El nombre efectivo agrega el ancho del vector
# (ej. `nem0_memory_2048`): las dimensiones del embedding son parte de la
# identidad de la colección, no un detalle de implementación.
COLLECTION_BASE = "nem0_memory"

# ── Schemas ────────────────────────────────────────────────────────

class MemoryEntry(BaseModel):
    id: str                # hash SHA-256 único
    content: str           # texto original
    metadata: dict = {}    # {file, task_id, agent, outcome, ...}
    created_at: float = 0.0
    updated_at: float = 0.0
    access_count: int = 0

class SearchResult(BaseModel):
    id: str
    content: str
    metadata: dict = {}
    score: float = 0.0     # distancia coseno (más bajo = más relevante)

# ── Engine ─────────────────────────────────────────────────────────

class CuratorMemory:
    """Motor de memoria con deduplicación y búsqueda semántica."""

    def __init__(self):
        self.last_error: str = ""
        self._init_sqlite()

    # ── SQLite helpers ──────────────────────────────────────────

    def _init_sqlite(self) -> None:
        """Crea tabla nem0_memory si no existe."""
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nem0_memory (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                access_count INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_nem0_created
            ON nem0_memory(created_at DESC)
        """)
        conn.commit()
        conn.close()

    def _hash(self, content: str) -> str:
        """Hash SHA-256 del contenido para deduplicación."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    # ── ChromaDB helpers ────────────────────────────────────────

    def _get_chroma(self):
        """Cliente ChromaDB compartido (ver `api/embed.py`).

        El almacén, la ruta y el cliente son compartidos con Retriever: antes cada
        agente construía el suyo con rutas distintas, así que podían escribir en
        bases diferentes sin que nadie lo notara.
        """
        return chroma_client()

    def _get_collection(self):
        """Retorna la colección de la dimensionalidad activa (o la crea)."""
        client = self._get_chroma()
        if client is None:
            return None
        return self._get_or_create(client, self.collection_name())

    # ── Embeddings ──────────────────────────────────────────────

    def _embed(self, text: str) -> list[float]:
        """Embedding via la capa unificada `api.embed` (Ollama → NIM → hash).

        El fallback local de este método era un vector de 384 dimensiones
        hardcodeado, incompatible con cualquier otro tier: en vez de mantener
        una tercera definición de dimensionalidad, todo pasa por `api.embed`,
        que ya garantiza un vector no vacío.
        """
        return embed(text)

    @staticmethod
    def _embed_with_spec(text: str) -> tuple[list[float], dict]:
        return embed_with_source(text)

    # ── Compatibilidad de colecciones ───────────────────────────

    def collection_name(self) -> str:
        """Nombre de colección Chroma, versionado por dimensiones.

        El bug de origen: la colección se llamaba siempre `nem0_memory` sin
        importar el espacio vectorial. Cuando el tier activo cambió (Ollama
        768 → hash de 32 dims), Chroma quedó anclada a las dimensiones del
        primer insert y **toda** operación posterior falló con
        "Collection expecting embedding with dimension of 32, got 2048",
        atrapada por un `except` que solo imprimía. Incluir el ancho del
        vector en el nombre hace imposible volver a mezclar espacios.
        """
        dims = self._dims()
        return f"{COLLECTION_BASE}_{dims}" if dims else COLLECTION_BASE

    def _dims(self) -> int:
        """Dimensiones del tier de embedding activo."""
        return embedding_dims()

    def _get_collection(self):
        """Retorna (o crea) la colección de la dimensionalidad activa."""
        client = self._get_chroma()
        if client is None:
            return None
        return self._get_or_create(client, self.collection_name())

    def _get_or_create(self, client, name: str):
        try:
            return client.get_or_create_collection(name)
        except Exception as e:
            self.last_error = f"ChromaDB no pudo abrir '{name}': {e}"
            return None

    def rebuild_index(self) -> dict:
        """Reconstruye el índice vectorial desde SQLite (fuente de verdad).

        SQLite guarda el contenido completo de cada recuerdo; Chroma solo
        guarda el vector derivado. Por eso recrear el índice es seguro y
        reproducible: se re-embeben los textos y se crea la colección en la
        dimensionalidad actual. Se usa cuando la colección existente quedó
        anclada a un espacio vectorial viejo.

        Devuelve {ok, collection, dims, indexed, failed, previous}.
        """
        client = self._get_chroma()
        if client is None:
            return {"ok": False, "error": "ChromaDB no disponible", "indexed": 0,
                    "failed": 0, "dims": 0, "collection": None, "previous": []}

        previous = [c.name for c in client.list_collections()]
        name = self.collection_name()
        dims = self._dims()

        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT id, content, metadata_json FROM nem0_memory"
        ).fetchall()
        conn.close()

        # Borrar la colección destino para no dejar vectores del espacio viejo.
        try:
            client.delete_collection(name)
        except Exception:
            pass
        try:
            col = client.create_collection(name)
        except Exception:
            col = self._get_or_create(client, name)
        if col is None:
            return {"ok": False, "error": self.last_error or "no se pudo crear la colección",
                    "indexed": 0, "failed": 0, "dims": dims,
                    "collection": name, "previous": previous}

        indexed = failed = 0
        for mem_id, content, meta_json in rows:
            try:
                meta = json.loads(meta_json) if meta_json else {}
                if not isinstance(meta, dict):
                    meta = {}
                # Chroma rechaza valores no primitivos en metadata.
                meta = {k: v for k, v in meta.items()
                        if isinstance(v, (str, int, float, bool))}
                col.add(ids=[mem_id], documents=[content],
                        embeddings=[self._embed(content)], metadatas=[meta])
                indexed += 1
            except Exception as e:
                failed += 1
                self.last_error = f"fallo indexando {mem_id}: {e}"
        return {"ok": failed == 0, "collection": name, "dims": dims,
                "indexed": indexed, "failed": failed, "previous": previous}

    def stats(self) -> dict:
        """Estadísticas de la memoria (SQLite + estado del índice vectorial)."""
        conn = sqlite3.connect(str(DB_PATH))
        total = conn.execute("SELECT COUNT(*) FROM nem0_memory").fetchone()[0]
        total_accesses = conn.execute("SELECT SUM(access_count) FROM nem0_memory").fetchone()[0] or 0
        conn.close()

        client = self._get_chroma()
        col = self._get_collection()
        chroma_count = 0
        if col is not None:
            try:
                chroma_count = col.count()
            except Exception:
                chroma_count = 0

        indexed = col is not None
        return {
            "total_entries": total,
            "total_accesses": total_accesses,
            "chromadb_docs": chroma_count,
            "avg_accesses": round(total_accesses / max(total, 1), 1),
            "collection": self.collection_name() if indexed else None,
            "dims": self._dims(),
            "index_ok": indexed,
            "index_lag": max(0, total - chroma_count),
            "collections": [c.name for c in client.list_collections()] if client else [],
            "last_error": self.last_error,
        }

    # ── Public API ──────────────────────────────────────────────

    def add(
        self,
        content: str,
        metadata: Optional[dict] = None,
        user_id: str = "default",
    ) -> str:
        """
        Guarda un recuerdo con deduplicación.
        Si el contenido ya existe (mismo hash), actualiza access_count y timestamp.
        Retorna el ID del recuerdo.
        """
        mem_id = self._hash(content)
        now = time.time()
        meta = metadata or {}
        meta["user_id"] = user_id

        # 1. SQLite — upsert con dedup
        conn = sqlite3.connect(str(DB_PATH))
        existing = conn.execute(
            "SELECT id, access_count FROM nem0_memory WHERE id = ?", (mem_id,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE nem0_memory SET updated_at = ?, access_count = access_count + 1, metadata_json = ? WHERE id = ?",
                (now, json.dumps(meta), mem_id),
            )
        else:
            conn.execute(
                "INSERT INTO nem0_memory (id, content, metadata_json, created_at, updated_at, access_count) VALUES (?, ?, ?, ?, ?, 1)",
                (mem_id, content, json.dumps(meta), now, now),
            )
        conn.commit()
        conn.close()

        # 2. ChromaDB — solo insertar si es nuevo (no actualizar vectores en duplicados)
        if not existing:
            col = self._get_collection()
            if col is not None:
                try:
                    emb = self._embed(content)
                    col.add(
                        ids=[mem_id],
                        documents=[content],
                        embeddings=[emb],
                        metadatas=[meta],
                    )
                except Exception as e:
                    self.last_error = f"ChromaDB add: {e}"
                    print(f"[NEM0] ChromaDB add error: {e}")

        return mem_id

    def search(
        self,
        query: str,
        k: int = 5,
        user_id: Optional[str] = None,
    ) -> list[SearchResult]:
        """
        Búsqueda semántica + relleno cronológico por SQLite.

        Contrato explícito (antes era ambiguo y engañoso): `score` es distancia
        coseno solo para los resultados de ChromaDB. Los que vienen del relleno
        SQLite llevan `score=None` y `metadata["match"]="recency"`, para que el
        consumidor pueda distinguir un match semántico real de un relleno.
        """
        results = []

        # 1. ChromaDB — búsqueda semántica
        col = self._get_collection()
        if col is not None:
            try:
                emb = self._embed(query)
                where = {"user_id": user_id} if user_id else None
                chroma_results = col.query(
                    query_embeddings=[emb],
                    n_results=min(k, 20),
                    where=where,
                )
                ids = chroma_results.get("ids", [[]])[0]
                distances = chroma_results.get("distances", [[]])[0]
                documents = chroma_results.get("documents", [[]])[0]
                metadatas = chroma_results.get("metadatas", [[]])[0]

                for i, mid in enumerate(ids):
                    results.append(SearchResult(
                        id=mid,
                        content=documents[i] if i < len(documents) else "",
                        metadata=metadatas[i] if i < len(metadatas) else {},
                        score=distances[i] if i < len(distances) else 1.0,
                    ))
            except Exception as e:
                self.last_error = f"ChromaDB search: {e}"
                print(f"[NEM0] ChromaDB search error: {e}")

        # 2. Relleno SQLite — si la búsqueda semántica no alcanzó k resultados.
        # NO es un fallback semántico: son los recuerdos más recientes. Se marcan
        # como tales para no hacer pasar recencia por relevancia.
        if len(results) < k:
            conn = sqlite3.connect(str(DB_PATH))
            existing_ids = {r.id for r in results}
            sql_results = conn.execute(
                "SELECT id, content, metadata_json, created_at, updated_at, access_count FROM nem0_memory ORDER BY updated_at DESC LIMIT ?",
                (k * 2,),
            ).fetchall()
            conn.close()

            for row in sql_results:
                if row[0] in existing_ids:
                    continue
                try:
                    meta = json.loads(row[2])
                except Exception:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                if user_id and meta.get("user_id") != user_id:
                    continue
                meta = {**meta, "match": "recency"}
                results.append(SearchResult(
                    id=row[0],
                    content=row[1],
                    metadata=meta,
                    score=0.0,
                ))
                if len(results) >= k:
                    break

        # 3. Actualizar access_count
        if results:
            conn = sqlite3.connect(str(DB_PATH))
            for r in results[:k]:
                conn.execute(
                    "UPDATE nem0_memory SET access_count = access_count + 1 WHERE id = ?",
                    (r.id,),
                )
            conn.commit()
            conn.close()

        return results[:k]

    def update(self, mem_id: str, content: str, metadata: Optional[dict] = None) -> bool:
        """Actualiza un recuerdo existente."""
        conn = sqlite3.connect(str(DB_PATH))
        existing = conn.execute("SELECT id FROM nem0_memory WHERE id = ?", (mem_id,)).fetchone()
        if not existing:
            conn.close()
            return False

        now = time.time()
        meta = metadata or {}
        conn.execute(
            "UPDATE nem0_memory SET content = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
            (content, json.dumps(meta), now, mem_id),
        )
        conn.commit()
        conn.close()

        # Actualizar ChromaDB
        col = self._get_collection()
        if col is not None:
            try:
                emb = self._embed(content)
                col.update(ids=[mem_id], documents=[content], embeddings=[emb], metadatas=[meta])
            except Exception:
                pass

        return True

    def delete(self, mem_id: str) -> bool:
        """Elimina un recuerdo de ambas DBs."""
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("DELETE FROM nem0_memory WHERE id = ?", (mem_id,))
        deleted = conn.total_changes > 0
        conn.commit()
        conn.close()

        if deleted:
            col = self._get_collection()
            if col is not None:
                try:
                    col.delete(ids=[mem_id])
                except Exception:
                    pass

        return deleted

    def history(self, limit: int = 20, user_id: Optional[str] = None) -> list[MemoryEntry]:
        """Historial cronológico de recuerdos."""
        conn = sqlite3.connect(str(DB_PATH))
        if user_id:
            rows = conn.execute(
                "SELECT id, content, metadata_json, created_at, updated_at, access_count FROM nem0_memory WHERE json_extract(metadata_json, '$.user_id') = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, content, metadata_json, created_at, updated_at, access_count FROM nem0_memory ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()

        results = []
        for row in rows:
            try:
                meta = json.loads(row[2])
            except Exception:
                meta = {}
            results.append(MemoryEntry(
                id=row[0],
                content=row[1],
                metadata=meta,
                created_at=row[3],
                updated_at=row[4],
                access_count=row[5],
            ))
        return results

    def count(self) -> int:
        """Número total de recuerdos."""
        conn = sqlite3.connect(str(DB_PATH))
        count = conn.execute("SELECT COUNT(*) FROM nem0_memory").fetchone()[0]
        conn.close()
        return count

# Singleton — misma instancia en todo el sistema
nem0 = CuratorMemory()
