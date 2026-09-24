# Curator — Context Manager / Memoria de Trabajo
> Inyeccion de contexto + historial de trabajo + lecciones aprendidas

## Identidad

- **Nombre:** Curator
- **Rol:** Capa de persistencia — memoria semantica (ChromaDB), memoria de lecciones (SQLite `agent_memory`), historial de sesiones. **No es un agente con modelo LLM asignado** (corregido DT-20, 8 Jul 2026). El role `curator` en `model_routing.yaml` que asigna `mistralai/mistral-small-4-119b-2603` es un remanente de una version anterior — Curator no hace llamadas a la API NIM.
- **Embeddings:** importa de `api/embed.py` unificado (3-tier fallback: Ollama → NIM → hash SHA-256)
- **Vector DB:** ChromaDB en `chroma_db/` (collection `curator_memory`)
- **Naturaleza:** solo lectura + escritura a SQLite/ChromaDB. No ejecuta archivos ni comandos.

## Responsabilidades

1. Inyectar contexto del proyecto ANTES de que Planner/Orchestrator actúen
2. Token budgeting por prioridad (arquitectura activa > lecciones recientes > historial reciente > lecciones antiguas)
3. Resúmenes estructurados de archivos (no lectura completa)
4. Registrar lecciones (rol ex-ENHEDUANA)
5. Estimar tamaño del repositorio para presupuesto adaptivo de tokens

## Metodos

| Metodo | Descripcion | Input | Output |
|---|---|---|---|
| `inject_project_context()` | Contexto completo del proyecto activo | — | `ProjectContext` |
| `get_architecture()` | Resumen del documento de arquitectura | — | `str` |
| `get_recent_work(limit=5)` | Ultimas N sesiones de `agent_memory` | limit: int | `list[dict]` |
| `summarize_file(filepath)` | Resumen de archivo sin leerlo completo | filepath: str | `str` con fragmentos |
| `record_lesson(lesson)` | Guarda leccion en `agent_memory` tipo "lesson" | lesson: str | `int` (id) |
| `record_quality(task_id, score, feedback)` | Guarda quality_score + user_feedback por task_id | task_id, score, feedback | `int` (id) |
| `estimate_repo_size()` | Calcula tamano del repo activo en KB | — | `int` |

## Reglas Absolutas

- Ningun nodo del Task Graph que no sea "context" ejecuta sin que Curator haya corrido primero
- Curator solo lee — nunca escribe archivos ni ejecuta comandos
- Token budget se calcula por prioridad: si hay overflow, se descartan lecciones antiguas primero
- `estimate_repo_size()` usa `os.scandir` — sin LLM, sin ChromaDB

## Agentes que dependen de Curator

| Agente | Que recibe de Curator |
|---|---|
| Orchestrator | Contexto del proyecto para clasificar y auditar |
| Planner | Fragmentos de archivo con lineas de contexto para `str_replace` |
| Retriever | Mapa del proyecto y lecciones previas para busqueda semantica |