# V-CORE — Arquitectura vigente

> Este documento describe estructura estable. Para configuración o estado actual,
> consultar las fuentes vivas indicadas abajo. No usar conteos de líneas, nombres
> de modelos activos ni resultados de pruebas como arquitectura.

## Fuentes de verdad

| Tema | Fuente autoritativa |
|---|---|
| Versión | `VCORE_STATE.json`, leído por `api/version.py` |
| Routing, roles y presets | `model_routing.yaml` |
| Configuración del template de sesión | `sessions/default/model_routing.yaml` |
| Estado runtime | Endpoints `/health`, `/system/model` y `/system/health` |
| Comportamiento | Código ejecutable y pruebas recientes |

El modelo lead es estado mutable por hot-swap. Nunca se documenta por valor en
prosa: consultar `model_routing.yaml` o `GET /system/model`.

## Capas del sistema

```text
Frontend (Vanilla JS)
  -> FastAPI (`api/main.py`)
     -> Orchestrator: orquestación y streaming SSE
     -> Planner: planificación, aplicación y verificación de cambios
     -> Curator: contexto y persistencia de memoria
     -> Retriever: búsqueda, indexación e impacto
     -> LLMRouter: providers, presets, fallback, rate limiting y circuit breaker
     -> MCPManager: catálogo y ejecución de herramientas
     -> SQLite / ChromaDB: sesiones, uso, aprobaciones y memoria
```

### Frontend

`web/` contiene la interfaz (Vite + TypeScript; el build se genera en
`web_dist/`). Consume las rutas HTTP/SSE de la API, muestra conversaciones,
herramientas, modelo activo y controles de sesión. El frontend v1 (Vanilla JS)
vive archivado en `docs/_archive/frontend_v1/`. Los detalles de UX o de un
refactor son históricos y viven fuera de este documento.

### API y routers

`api/main.py` es la entrada FastAPI. Expone las familias de sistema, modelos,
agentes, sesiones, grafo de tareas, observabilidad, aprobaciones, estado y git.
Los routers de archivos, shell y búsqueda viven en módulos separados de `api/`.

No se mantiene aquí un número de endpoints: cambia al registrar routers.

### Agentes

| Componente | Responsabilidad |
|---|---|
| Orchestrator | Punto de entrada de conversación, routing, tool loop y streaming. |
| Planner | Planificación, aplicación y verificación de cambios de código. |
| Curator | Contexto y memoria persistente con SQLite y ChromaDB; no tiene modelo LLM propio. |
| Retriever | Indexación, recuperación semántica, filesystem e impact mapping. |

Los documentos de cada agente detallan su contrato. La configuración de sus
modelos se consulta siempre en `model_routing.yaml`.

### Routing y proveedores

`api/llm_client.py` carga `model_routing.yaml`, aplica presets por modelo y
administra proveedores, fallback chains, rate limiting, circuit breaker y
hot-swap. NVIDIA NIM es el proveedor principal; las claves se resuelven desde
el entorno, no desde documentación.

El embedding unificado vive en `api/embed.py` con fallback Ollama -> NIM -> hash
SHA-256. Esto evita que la memoria tenga una dependencia externa obligatoria.

### Herramientas

Orchestrator integra `system/mcp_manager.py`. Este módulo carga `mcp_config.json`,
descubre herramientas externas cuando están disponibles y registra servidores
locales. `system/mcp_client.py` y algunos servidores antiguos permanecen por
compatibilidad; no son la interfaz preferida para trabajo nuevo.

### Persistencia y sesiones

- SQLite guarda estado operativo, sesiones, uso y aprobaciones.
- ChromaDB es opcional para recuperación semántica.
- Cada sesión usa un directorio en `sessions/`; `sessions/default/` es la
  plantilla de configuración.
- La API protege el intercambio de router por sesión con locks de hilo.

El diseño detallado está en `docs/SESSION_ISOLATION_ARCHITECTURE.md`.

## Límites y decisiones

- `gate.py`, `gate_rules.yaml`, `.env`, `api/llm_client.py` y
  `model_routing.yaml` son zonas protegidas.
- Los cambios de infraestructura se verifican contra código y, cuando aplique,
  contra el servidor vivo.
- Tauri es una exploración futura, no parte del runtime actual. Ver
  `docs/ROADMAP_TAURI.md`.
- Las decisiones, deuda y trabajo pendiente viven solamente en
  `VCORE_ROADMAP.md` después de la consolidación documental.

## Documentación

La jerarquía, el alcance de cada documento y las reglas para evitar fuentes
duplicadas están en `docs/DOCUMENTATION.md`.
