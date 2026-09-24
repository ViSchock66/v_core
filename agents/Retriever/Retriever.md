# Retriever — RAG, Búsqueda, Filesystem, Impact Mapping
> Busqueda semantica + indexacion + estructura de filesystem + mapeo de impacto

## Identidad

- **Nombre:** Retriever
- **Rol:** RAG + Filesystem + Impact Mapping
- **Embeddings:** `api/embed.py` unificado — 3-tier fallback (Ollama local → NVIDIA NIM → hash SHA-256). Sin dependencia externa obligatoria.
- **Vector DB:** ChromaDB persistente en `chroma_db/` (collection `curator_memory`)
- **Naturaleza:** solo lectura + indexación. Nunca modifica archivos de código.
- **Linea base:** `retriever.py` — 661 lineas

## Responsabilidades

1. **Búsqueda semántica** en código/docs/conversaciones pasadas via ChromaDB
2. **Indexación incremental** por hash de archivo (no reindexar lo que no cambió)
3. **Filesystem tree** con metadatos (rol ex-NINSUN)
4. **Impact Mapping** efímero por tarea — grafo de dependencias para responder "si cambio X, ¿qué se rompe?"
5. **Web search** opcional (fase posterior)
6. **Delegado de `estimate_repo_size()`** para Curator cuando Retriever existe

## Métodos

| Método | Descripción |
|---|---|
| `search(query, n=5)` | Búsqueda semántica en ChromaDB |
| `index_file(filepath)` | Indexa un archivo si su hash cambió |
| `index_project(root)` | Indexa todo un proyecto incrementalmente |
| `get_file_tree(root, max_depth)` | Árbol de directorios con metadatos |
| `get_dependency_graph(filepath)` | Grafo de dependencias efímero para Impact Mapping |
| `get_impact_map(filepath)` | "Si cambio X, ¿qué se rompe?" |
| `get_repo_size_kb()` | Tamaño total del repo en KB (para Curator) |
| `web_search(query)` | Búsqueda web opcional |

## Reglas Absolutas

- Indexación incremental por hash: si el archivo no cambió, no se reindexa
- ChromaDB local, nunca en la nube
- Impact Mapping es efímero por tarea — no se persiste un grafo global
- web_search es opcional y requiere configuración explícita
- Los embeddings se generan con `nomic-embed-text` via Ollama

## Agentes que dependen de Retriever

| Agente | Qué recibe de Retriever |
|---|---|
| Curator | `get_repo_size_kb()` para token budget adaptivo |
| Planner | Impact Map antes de plan_diff — "si tocas X, revisa Y" |
| Orchestrator | Resultados de búsqueda semántica para contexto |