# V-Core — Project Context for Agents

**Project root**: raíz del repo (este archivo vive en la raíz)
**Stack**: FastAPI + Vanilla JS + SQLite + NVIDIA NIM (multi-provider con circuit breaker, rate limiter, fallback chains y credential pool de 4 keys)
**Backend entry**: `api/main.py` (uvicorn; el puerto canónico vive en `api/ports.py` — default 8000, override con `VCORE_PORT`. Rutas y routers son estado de código, no se cuentan aquí)
**Frontend**: `web/` — Vite + TypeScript (build a `web_dist/`, ignorado). El v1 (Vanilla JS, temas Dark + Light Studio) vive archivado en `docs/_archive/frontend_v1/`. No documentar conteos de líneas: cambian con cada refactor.
**Model routing**: `model_routing.yaml` — hot-swap en caliente. El lead activo es **estado mutable por hot-swap**: consultar `model_routing.yaml` → `orchestrator_lead.model` o `GET /system/model`. Council, escalation, Planner y visual audit se consultan en el mismo YAML.
**Embeddings**: `api/embed.py` unificado — 3-tier fallback (Ollama → NIM → hash SHA-256). Sin dependencia externa obligatoria.
**Curator**: capa de persistencia (SQLite + ChromaDB), **no es un agente con modelo LLM asignado**. No existe un rol `curator` en `model_routing.yaml`.

**Arquitectura**: `VCORE_ARCHITECTURE.md` · **Roadmap activo**: `VCORE_ROADMAP.md`
**Versión**: `VCORE_STATE.json` → `api/version.py` (fuente única)
**Jerarquía documental**: `docs/DOCUMENTATION.md`. Solo los documentos marcados como vigentes sirven de contexto operativo.

## Primera acción obligatoria

Carga la skill `/vcore` antes de tocar cualquier archivo del proyecto. La instalación disponible se encuentra en el catálogo local de skills de tu harness (ruta relativa al catálogo: `software-development/vcore-app-config`); si tu entorno no la tiene, pídesela al PM.
Toda la arquitectura, pitfalls, comandos y patrones de bugs están ahí.

## Reglas de conducta (aplican desde el primer turno)

1. **Lee antes de editar** — siempre inspecciona el archivo antes de cualquier cambio.
2. **No digas "funciona" sin evidencia** — verifica con curl, grep, node --check o Playwright.
3. **Honestidad brutal** — si algo falla o no sabes, dilo. No adornes resultados.
4. **No hagas el trabajo de Orchestrator** — solo arregla infraestructura (timeouts, paths, prompts, permisos). Orchestrator aplica los fixes.
5. **Confirmación por bloques** — presenta 3-4 tareas, espera "vamos", ejecuta, repite.
6. **No modifiques `.md` files** sin pedido explícito del PM.
7. **No hagas git push** sin orden explícita.
8. **Verificación post-cambio**: `python -m py_compile <archivo>` (Python) y/o `npm --prefix web run typecheck` (frontend `web/`) después de cada edición.

## Archivos inmunes (no tocar sin council mode)

`gate.py`, `.env`, `gate_rules.yaml` (solo allowlist es modificable), `llm_client.py`, `model_routing.yaml`

## Comandos rápidos

```powershell
# Puerto canónico (fuente única: api/ports.py)
python -c "from api.ports import port; print(port())"

# Matar uvicorn viejo
netstat -ano | findstr :8000

# Iniciar backend
.venv\Scripts\uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload

# Verificar
curl http://127.0.0.1:8000/health
python -m compileall -q api agents system scripts
npm --prefix web run typecheck
python scripts/audit.py
```
