"""
agents/Retriever/retriever.py
=======================
Retriever — RAG, Búsqueda, Filesystem, Impact Mapping — V-CORE v1.4

Responsabilidades:
  - Busqueda semantica en codigo/docs via ChromaDB
  - Indexacion incremental por hash de archivo
  - Filesystem tree con metadatos (rol ex-NINSUN)
  - Impact Mapping efimero por tarea ("si cambio X, que se rompe?")
  - Tamano del repo para Curator (token budget adaptivo)

Embeddings: unificados via api/embed.py (3-tier: Ollama → NIM → hash)
Vector DB: ChromaDB persistente en chroma_db/

Uso:
    from agents.Retriever.retriever import Retriever
    retriever = Retriever()
    results = await retriever.search("query de busqueda")
    mapa = retriever.get_impact_map("api/main.py")
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import chromadb

from api.embed import (
    CHROMA_DIR,
    chroma_client,
    collection_name,
    embed_async,
    embed_with_source,
    embedding_dims,
)


# =============================================================================
# CONSTANTES
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # V-CORE/
DB_PATH = BASE_DIR / "vcore.db"
# Almacén Chroma compartido con Curator (ver api/embed.py). Antes Retriever apuntaba
# a `knowledge/.chromadb/`, un directorio que no existía: ChromaDB lo creaba
# vacío y el RAG operaba sobre una base distinta a la de los embeddings reales.
CHROMA_PATH = CHROMA_DIR
WORKSPACE_DIR = BASE_DIR / "workspace"

# Nombre base de la colección. El efectivo agrega las dimensiones activas.
COLLECTION_BASE = "vcore_knowledge"

# Extensiones de codigo que Retriever indexa
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".yaml", ".yml", ".json", ".md",
    ".html", ".css", ".cpp", ".c", ".h", ".hpp", ".ps1",
    ".sql", ".txt", ".cfg", ".ini", ".toml", ".xml", ".svg",
}

# Patrones para detectar imports/dependencias por lenguaje
IMPORT_PATTERNS = {
    ".py": [
        re.compile(r"^import\s+(\S+)", re.MULTILINE),
        re.compile(r"^from\s+(\S+)\s+import", re.MULTILINE),
    ],
    ".js": [
        re.compile(r"require\(['\"](.+?)['\"]\)"),
        re.compile(r"import\s+.*?\s+from\s+['\"](.+?)['\"]"),
    ],
    ".ts": [
        re.compile(r"import\s+.*?\s+from\s+['\"](.+?)['\"]"),
        re.compile(r"import\s+['\"](.+?)['\"]"),
    ],
}


# =============================================================================
# Retriever
# =============================================================================

class Retriever:
    """
    RAG + Filesystem + Impact Mapping.
    Solo lectura + indexacion. Nunca modifica archivos de codigo.
    """

    def __init__(self):
        self.agent_name = "Retriever"
        self._client: Optional[chromadb.PersistentClient] = None
        self._collection = None

    # ------------------------------------------------------------------
    # ChromaDB client (lazy init)
    # ------------------------------------------------------------------

    @property
    def client(self) -> chromadb.PersistentClient:
        if self._client is None:
            shared = chroma_client()
            if shared is not None:
                self._client = shared
            else:
                CHROMA_PATH.mkdir(parents=True, exist_ok=True)
                self._client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        return self._client

    @property
    def collection(self):
        if self._collection is None:
            self._collection = self.client.get_or_create_collection(
                name=collection_name(COLLECTION_BASE),
            )
        return self._collection

    # ------------------------------------------------------------------
    # 1. BUSQUEDA SEMANTICA
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        n: int = 5,
        filter_metadata: Optional[dict] = None,
    ) -> list[dict[str, Any]]:
        """
        Busqueda semantica en ChromaDB.
        Usa embeddings generados por nomic-embed-text via Ollama.

        Args:
            query: Texto de busqueda
            n: Numero maximo de resultados
            filter_metadata: Filtro opcional por metadatos (ej: {"filename": "*.py"})

        Returns:
            Lista de dicts con {id, content, metadata, distance}
        """
        try:
            # Generar embedding del query usando Ollama
            query_embedding = await embed_async(query)

            kwargs = {
                "query_embeddings": [query_embedding],
                "n_results": min(n, 50),
            }
            if filter_metadata:
                kwargs["where"] = filter_metadata

            results = self.collection.query(**kwargs)

            if not results["ids"] or not results["ids"][0]:
                return []

            output = []
            for i in range(len(results["ids"][0])):
                output.append({
                    "id": results["ids"][0][i],
                    "content": results["documents"][0][i][:1000] if results["documents"] else "",
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "distance": results["distances"][0][i] if results["distances"] else 0.0,
                })

            return output

        except Exception as e:
            print(f"[Retriever] Error en busqueda: {e}")
            return []

    # ------------------------------------------------------------------
    # 2. INDEXACION INCREMENTAL
    # ------------------------------------------------------------------

    def index_file(self, filepath: str) -> bool:
        """
        Indexa un archivo si su hash cambio (incremental).
        Retorna True si se indexo, False si no cambio o fallo.

        El archivo se divide en chunks de tamano fijo para busqueda
        semantica mas precisa.
        """
        # --- Gap 3: Validar path con gate antes de leer ---
        from gate import Gate
        gate = Gate("gate_rules.yaml")
        decision = gate.evaluate("read_file", {"path": filepath}, agent_id="Retriever")
        if not decision.auto_approved:
            print(f"[Retriever] Gate bloqueó indexación: {decision.reason}")
            return False
        # --- fin Gap 3 ---

        path = Path(filepath)
        if not path.exists() or not path.is_file():
            return False

        ext = path.suffix.lower()
        if ext not in CODE_EXTENSIONS:
            return False

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                return False

            content_hash = self._hash_content(content)
            doc_id = self._normalize_path(path)

            # Verificar hash actual vs almacenado
            stored_hash = self._get_stored_hash(doc_id)
            if stored_hash == content_hash:
                return False  # No cambio — omitir

            # Dividir en chunks y upsert
            chunks = self._chunk_text(content, doc_id)
            chunk_ids = []
            chunk_docs = []
            chunk_metas = []

            for i, chunk in enumerate(chunks):
                chunk_id = f"{doc_id}#chunk{i}"
                chunk_ids.append(chunk_id)
                chunk_docs.append(chunk)
                chunk_metas.append({
                    "source": doc_id,
                    "filename": path.name,
                    "extension": ext,
                    "chunk": i,
                    "total_chunks": len(chunks),
                    "hash": content_hash,
                    "indexed_at": time.time(),
                })

            if chunk_ids:
                # Los embeddings se calculan EXPLÍCITAMENTE con `api.embed`.
                # Antes el upsert omitía `embeddings`, así que ChromaDB usaba su
                # modelo por defecto (all-MiniLM-L6-v2, 384 dims) mientras la
                # búsqueda mandaba un vector de `embed_async` (768 o 2048). Dos
                # espacios vectoriales distintos en la misma colección: el RAG
                # indexaba con un embedder y buscaba con otro.
                embeddings = [
                    embed_with_source(chunk)[0]
                    for chunk in chunk_docs
                ]
                self.collection.upsert(
                    ids=chunk_ids,
                    documents=chunk_docs,
                    embeddings=embeddings,
                    metadatas=chunk_metas,
                )

            # Actualizar hash en rag_documents
            self._update_stored_hash(doc_id, content_hash, chunk_ids)

            return True

        except Exception as e:
            print(f"[Retriever] Error indexando {filepath}: {e}")
            return False

    def index_project(self, root: str = "") -> dict[str, int]:
        """
        Indexa todo un proyecto incrementalmente.
        Solo archivos que cambiaron desde la ultima indexacion se reprocesan.

        Args:
            root: Path del proyecto a indexar (default: V-CORE raiz)

        Returns:
            Dict con stats: {indexed, skipped, errors}
        """
        root_path = Path(root) if root else BASE_DIR
        stats = {"indexed": 0, "skipped": 0, "errors": 0}

        if not root_path.exists():
            return stats

        for entry in root_path.rglob("*"):
            if entry.is_file() and entry.suffix.lower() in CODE_EXTENSIONS:
                # Saltar __pycache__, .venv, .chromadb, .git
                rel = entry.relative_to(BASE_DIR)
                parts = rel.parts
                skip_dirs = {"__pycache__", ".venv", ".chromadb", ".git", "node_modules"}
                if any(p in skip_dirs for p in parts):
                    stats["skipped"] += 1
                    continue

                try:
                    if self.index_file(str(entry)):
                        stats["indexed"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception:
                    stats["errors"] += 1

        return stats

    # ------------------------------------------------------------------
    # 3. FILESYSTEM TREE
    # ------------------------------------------------------------------

    def get_file_tree(
        self,
        root: str = "",
        max_depth: int = 4,
        show_hidden: bool = False,
    ) -> dict[str, Any]:
        """
        Arbol de directorios con metadatos (tamano, tipo, hash).
        Rol ex-NINSUN.

        Args:
            root: Path raiz (default: workspace/)
            max_depth: Profundidad maxima del arbol
            show_hidden: Incluir archivos ocultos

        Returns:
            Dict con estructura de arbol
        """
        # --- Gap 3: Validar root con gate antes de leer ---
        from gate import Gate
        gate = Gate("gate_rules.yaml")
        root_path = Path(root) if root else WORKSPACE_DIR
        decision = gate.evaluate("read_file", {"path": str(root_path)}, agent_id="Retriever")
        if not decision.auto_approved:
            return {"name": root_path.name, "type": "directory", "children": [], "error": f"Gate bloqueó: {decision.reason}"}
        # --- fin Gap 3 ---

        if not root_path.exists():
            return {"name": root_path.name, "type": "directory", "children": []}

        return self._build_tree(root_path, max_depth=max_depth, show_hidden=show_hidden)

    # ------------------------------------------------------------------
    # 4. IMPACT MAPPING
    # ------------------------------------------------------------------

    def get_dependency_graph(self, filepath: str) -> dict[str, Any]:
        """
        Grafo de dependencias EFIMERO para un archivo.
        Escanea imports/references locales para construir un mapa
        de "que archivos importan a este" y "a cuales importa este".

        No se persiste — es por tarea.

        Args:
            filepath: Path del archivo a analizar

        Returns:
            Dict con {imports: [...], imported_by: [...]}
        """
        path = Path(filepath)
        if not path.exists():
            return {"imports": [], "imported_by": [], "error": "Archivo no encontrado"}

        ext = path.suffix.lower()
        patterns = IMPORT_PATTERNS.get(ext, [])

        if not patterns:
            return {"imports": [], "imported_by": [], "note": "Sin patrones para esta extension"}

        try:
            content = path.read_text(encoding="utf-8", errors="replace")

            # Extraer imports del archivo
            imports = set()
            for pattern in patterns:
                for match in pattern.finditer(content):
                    module = match.group(1).strip()
                    # Solo imports locales (relativos o del proyecto)
                    if module.startswith(".") or module.startswith(("agents", "api", "scripts")):
                        imports.add(module)

            # Buscar que archivos importan a este
            filename = path.name
            imported_by = self._find_importers(filename, path.parent)

            return {
                "file": filepath,
                "imports": sorted(imports),
                "imported_by": sorted(imported_by),
            }

        except Exception as e:
            return {"imports": [], "imported_by": [], "error": str(e)}

    def get_impact_map(self, filepath: str) -> dict[str, Any]:
        """
        Mapa de impacto: "si cambio X, que hay que revisar?"
        Combina dependencias directas + busqueda semantica de referencias.

        Args:
            filepath: Path del archivo a modificar

        Returns:
            Dict con {file, impact_chain, risk_level, related_by_content}
        """
        path = Path(filepath)
        if not path.exists():
            return {"file": filepath, "impact_chain": [], "risk_level": "unknown"}

        deps = self.get_dependency_graph(filepath)

        # Determinar nivel de riesgo basado en cantidad de dependencias
        total_deps = len(deps.get("imports", [])) + len(deps.get("imported_by", []))
        if total_deps == 0:
            risk = "low"
        elif total_deps <= 3:
            risk = "medium"
        else:
            risk = "high"

        # Busqueda semantica por nombre de archivo para encontrar
        # referencias contextuales no detectadas por patrones de import
        try:
            similar = self.collection.query(
                query_texts=[path.name],
                n_results=5,
            )
            related = []
            if similar["ids"] and similar["ids"][0]:
                for i in range(len(similar["ids"][0])):
                    src = similar["metadatas"][0][i].get("source", "")
                    if src and src != str(path):
                        related.append(src)
        except Exception:
            related = []

        return {
            "file": filepath,
            "impact_chain": {
                "imports": deps.get("imports", []),
                "imported_by": deps.get("imported_by", []),
            },
            "risk_level": risk,
            "total_dependencies": total_deps,
            "related_by_content": list(set(related))[:10],
        }

    # ------------------------------------------------------------------
    # 5. REPO SIZE (para Curator)
    # ------------------------------------------------------------------

    def get_repo_size_kb(
        self,
        root: str = "",
        code_only: bool = True,
    ) -> int:
        """
        Tamano total del repositorio en KB.
        Cuando Retriever existe, Curator delega a este metodo.

        Args:
            root: Path a medir (default: V-CORE raiz)
            code_only: Solo archivos de codigo (True) o todos (False)

        Returns:
            Tamano en KB
        """
        root_path = Path(root) if root else BASE_DIR
        total_bytes = 0

        try:
            for entry in root_path.rglob("*"):
                if not entry.is_file():
                    continue
                rel = entry.relative_to(BASE_DIR)
                parts = rel.parts
                skip_dirs = {"__pycache__", ".venv", ".chromadb", ".git", "node_modules"}
                if any(p in skip_dirs for p in parts):
                    continue
                if entry.name.startswith("."):
                    continue
                if code_only and entry.suffix.lower() not in CODE_EXTENSIONS:
                    continue
                total_bytes += entry.stat().st_size

            return total_bytes // 1024

        except Exception:
            return 0

    # ------------------------------------------------------------------
    # 6. WEB SEARCH (opcional)
    # ------------------------------------------------------------------

    async def web_search(
        self,
        query: str,
        max_results: int = 5,
    ) -> list[dict[str, str]]:
        """
        Busqueda web opcional.
        Requiere configuracion explicita — no se activa por defecto.

        Por ahora retorna lista vacia. La implementacion real depende
        del proveedor de busqueda web configurado.
        """
        # Placeholder: en el futuro se puede integrar con
        # DuckDuckGo, SerpAPI, o Google Custom Search
        print(f"[Retriever] web_search solicitada pero no configurada: '{query}'")
        return []

    # ------------------------------------------------------------------
    # METODOS INTERNOS
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _normalize_path(path: Path) -> str:
        """Normaliza path a forward slashes para consistencia."""
        return str(path).replace("\\", "/")

    def _get_stored_hash(self, doc_id: str) -> str:
        """Lee el hash almacenado en rag_documents para un doc_id."""
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute(
                "SELECT content_hash FROM rag_documents WHERE filepath = ?",
                (doc_id,),
            ).fetchone()
            conn.close()
            return row[0] if row else ""
        except Exception:
            return ""

    def _update_stored_hash(
        self,
        doc_id: str,
        content_hash: str,
        chunk_ids: list[str],
    ) -> None:
        """Actualiza el hash en rag_documents (upsert)."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT INTO rag_documents (filepath, content_hash, indexed_at, chunk_ids)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    indexed_at = excluded.indexed_at,
                    chunk_ids = excluded.chunk_ids
            """, (
                doc_id,
                content_hash,
                time.time(),
                json.dumps(chunk_ids),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[Retriever] Error actualizando hash: {e}")

    @staticmethod
    def _chunk_text(text: str, doc_id: str, chunk_size: int = 500) -> list[str]:
        """
        Divide texto en chunks de tamano fijo (por lineas).
        Intenta cortar en limites de parrafo cuando es posible.
        """
        lines = text.split("\n")
        chunks = []
        current_chunk = []
        current_size = 0

        for line in lines:
            line_size = len(line) // 4  # estimacion tokens
            if current_size + line_size > chunk_size and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_size = 0
            current_chunk.append(line)
            current_size += line_size

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks if chunks else [text[:2000]]

    def _build_tree(
        self,
        path: Path,
        max_depth: int,
        show_hidden: bool,
        current_depth: int = 0,
    ) -> dict[str, Any]:
        """Construye arbol de directorios recursivo."""
        if current_depth >= max_depth:
            return {
                "name": path.name,
                "type": "directory",
                "path": str(path),
                "children": None,
            }

        try:
            children = []
            if path.is_dir():
                for entry in sorted(path.iterdir()):
                    if not show_hidden and entry.name.startswith("."):
                        continue
                    if entry.name in ("__pycache__", ".venv", "node_modules"):
                        continue

                    if entry.is_dir():
                        children.append(
                            self._build_tree(
                                entry, max_depth, show_hidden, current_depth + 1
                            )
                        )
                    else:
                        ext = entry.suffix.lower()
                        children.append({
                            "name": entry.name,
                            "type": "file",
                            "path": str(entry),
                            "size": entry.stat().st_size,
                            "extension": ext,
                            "indexed": ext in CODE_EXTENSIONS,
                        })

            return {
                "name": path.name,
                "type": "directory",
                "path": str(path),
                "children": children if children else None,
            }

        except PermissionError:
            return {
                "name": path.name,
                "type": "directory",
                "path": str(path),
                "children": None,
            }

    def _find_importers(self, filename: str, search_dir: Path) -> set[str]:
        """
        Busca que archivos en search_dir importan/referencian a filename.
        """
        importers = set()
        for ext, patterns in IMPORT_PATTERNS.items():
            for filepath in search_dir.rglob(f"*{ext}"):
                if filepath.name == filename:
                    continue
                try:
                    content = filepath.read_text(encoding="utf-8", errors="replace")
                    for pattern in patterns:
                        for match in pattern.finditer(content):
                            ref = match.group(1)
                            # Si el import contiene el nombre del archivo (sin extension)
                            if filename.replace(".", "") in ref.replace(".", ""):
                                importers.add(str(filepath))
                except Exception:
                    continue
        return importers

    # ------------------------------------------------------------------
    # INTEGRACION CON Curator
    # ------------------------------------------------------------------

    @staticmethod
    def get_collection_stats() -> dict[str, Any]:
        """Estadisticas de la coleccion ChromaDB."""
        try:
            client = chromadb.PersistentClient(path=str(CHROMA_PATH))
            collection = client.get_or_create_collection(name=COLLECTION_NAME)
            count = collection.count()
            return {
                "total_documents": count,
                "collection": COLLECTION_NAME,
                "chroma_path": str(CHROMA_PATH),
            }
        except Exception as e:
            return {
                "total_documents": 0,
                "error": str(e),
            }