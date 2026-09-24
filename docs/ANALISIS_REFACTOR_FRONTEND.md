# V-CORE — Análisis para el refactor de frontend (2026-09-23)

> Documento de trabajo. No es doctrina: es el resultado de auditar el sistema vivo
> antes de rehacer el frontend. Cada afirmación tiene evidencia (comando, archivo:línea
> o captura). Lo que no pude verificar está marcado como tal.

## 0. Método

Evidencia recolectada hoy, con el backend en `:8000` (v1.5.0) y Chromium real:

| Herramienta | Qué produjo |
|---|---|
| `curl` a 8 endpoints | salud, catálogo de modelos, sessions, files/tree, llm/* |
| `scripts/audit_live.py` (nuevo) | errores de consola, errores HTTP, estado inicial, dropdown, file tree, visor |
| `scripts/e2e_chat_probe.py` (nuevo) | chat real por SSE: tools, artifacts, persistencia post-reload |
| `.audit_shots/*.png` | capturas del estado actual |
| lectura de código | Orchestrator, Planner, Retriever, Curator, gate, llm_client, task_graph, MCP |
| `sqlite3` sobre `vcore.db` | esquema y volumen real de datos |
| subagentes de auditoría | inventario agentivo + estado del arte (ver §7) |

---

## 1. Qué es V-CORE hoy (verificado)

Backend FastAPI con ~54 rutas y un motor agéntico que **funciona de verdad** en el camino
crítico. No es un wrapper de OpenAI: hay orquestación, tools, resiliencia y persistencia.

**Funciona (evidencia propia):**

- **Chat por SSE con tool loop ReAct.** Mandé 3 mensajes reales. Orchestrator emitió eventos
  `tool` y `artifact` correctamente; el archivo `workspace/prueba_artefacto.txt` se creó
  (~`write_file` confirmado: "Archivo escrito: workspace\prueba_artefacto.txt (9 chars)").
- **8 tools MCP reales**: `read_file`, `write_file`, `patch_file`, `list_files`,
  `search_code`, `execute_command`, `background_task`, `web_search`
  (`system/mcp_manager.py`, factories `*_mcp.py`).
- **Resiliencia de LLM**: circuit breaker de 3 estados por provider discriminando
  429/5xx/401, rate limiter RPM con backoff, fallback chains, credential pool de 4 keys
  NVIDIA, hot-swap en caliente. Todo expuesto por `/llm/circuit-status`, `/llm/providers`,
  `/system/model`, `/llm/usage`.
- **Gate determinístico bien escrito** (`gate.py`): reglas por tool, niveles A/B,
  allowlist de roots, log en `gate_log` (**1.685 filas reales**).
- **Approvals que sí ejecutan** al aprobar y son idempotentes (`main.py:993+`, fix DT-00).
- **Task Graph engine** con DAG, persistencia SQLite (**87 grafos** en `task_graphs`),
  resume y recovery al arranque.
- **Observabilidad** en `traces.jsonl` correlacionada por `task_id`, consumible por HTTP.
- **Memoria**: existe. `agents/Curator/memory.py` (SQLite `nem0_memory` + ChromaDB,
  dedup por SHA-256) **y** `agents/Curator/curator.py` (lecciones). 23 filas en
  `nem0_memory`. Está cableada en el agent loop (`orchestrator.py:436,667,744`).

**Datos reales acumulados** (`vcore.db`, 14 tablas): `agent_execution` 430,
`gate_log` 1685, `llm_usage_log` 978, `chat_history` 130, `task_graphs` 87,
`circuit_breaker_log` 47, `approvals` 41, `sessions` 56.

---

## 2. Diagnóstico del frontend actual: no es un problema de estética, es de arquitectura

`Frontend/` son 3 archivos (app.js 622 líneas, index.html 165, style.css) con estado
global de ~18 variables mutables y 85 funciones sueltas colgadas de `window`. Es
recuperable como *referencia*, no como base. Lo que rompe:

### 2.1 El estado de la sesión no cierra el círculo (el bug estructural)

- `POST /sessions` devuelve `{id, session_dir}` (`main.py:892`). El frontend **ignora
  `session_dir`** y manda `session_id` numérico en `/agents/route`, donde el modelo
  `ChatMessage` (`main.py:329-334`) **no tiene un campo `session_id`**: solo `session_dir`.
  Resultado: **el aislamiento por sesión está implementado en el backend y nunca se usa
  desde la UI**. Todas las conversaciones comparten el router global y el prompt global.
- `GET /sessions/{id}/messages` exige `int`. Si el frontend manda un id string
  (`sess_205_1bfb4378`), devuelve **422** (verificado con curl).
- **Evidencia del daño real:** después de 3 conversaciones completas, recargué la página →
  `{msgAi: 0, msgUser: 0, welcome: true}`. **La conversación desaparece por completo.**
  El sidebar lista 50 ítems y 54 de 56 sesiones se llaman "Nueva conversación" (solo 2
  títulos distintos en toda la base). Además **no existe scroll infinito ni filtro**: el
  usuario ve una pared de ruido.

### 2.2 El visor de artefactos depende de un CDN

- Monaco se carga desde `cdn.jsdelivr.net` (`index.html:12`) y `require.config` dentro de
  `showCode()` (`app.js:294`). El fallback declarado —highlight.js por CDN y un
  `highlightCode()` casero— también depende de red (`index.html:10-11`).
- **Verificado**: sin embargo sí renderiza cuando hay red (`.audit_shots/04`), con
  `monacoGlobal: true`. El problema es que **es una dependencia oculta de internet en una
  app que se supone local**, y el estado intermedio ("Cargando…" / panel vacío) no se
  comunica.
- El panel sólo abre **archivos de texto e imágenes**: `showCode()` no tiene ramas para
  PDF, binarios, diffs, ni para artefactos `kind: "audit"`. El test de artefacto mostró
  `monacoText: ""` durante el abrir (una carrera entre `openPanel()` y la creación del
  editor).
- La tarjeta de artefacto **duplica el final de la respuesta**: en `.audit_shots/05` el
  texto "Hecho. Archivo `workspace/prueba_artefacto.txt`…" aparece **dos veces**. Causa:
  `appendText` escribe en nodos de texto vivos y `endStream()` (`app.js:128-146`) inserta
  un `.md-rendered` con el texto completo, pero los nodos de texto ya existentes no
  siempre se eliminan (los que tienen contenido dentro de hijos no son nodos de texto
  hijos del body).

### 2.3 El selector de modelos funciona, pero no dice la verdad completa

- El dropdown **sí carga** (6 filas, modelo activo marcado con `--accent`), verificado
  con click real.
- Pero arranca con `available?provider=nvidia` hardcodeado (`app.js:479`) y pide cambiar
  **siempre `role: "orchestrator_lead"`** (`app.js:490`): no permite cambiar council, escalation
  ni Planner, que son roles con modelo propio en `model_routing.yaml`.
- El rol activo no se muestra: el topbar muestra `z-ai/glm-5.2` sin decir *de qué rol*.
- `renderModelDropdown` usa `m.id.split('/')` sin guarda: si el backend devolviera un
  string (el código intenta soportar ambos), **revienta con TypeError** dentro de un
  `.then()` sin `catch`.
- El spinner de tokens miente tres veces: `updateTokenInfo` está sobrecargada (una versión
  recibe `chars` y estima `chars/4` contra un límite fijo de 128.000; otra recibe un array
  de usage acumulado). En pantalla decía **"13.274 tokens"** cuando el contexto real son
  ~2.000. Un número inventado en la UI es peor que no tener número.

### 2.4 No hay workspace: es un adorno, y tienes razón

- `workspace/` sólo contiene `uploads/`. La raíz de trabajo de todas las sesiones es la
  raíz del repo: `GET /files/tree` devuelve la raíz del checkout completa (181 nodos en
  el DOM). El file tree es un explorador del repo, no del espacio de trabajo del agente.
- `sessions/<id>/` sólo guarda 4 archivos de configuración (state.json, dos YAML, un
  example). **No es un workspace, es un perfil.**
- En el DOM no existe **ningún** elemento de workspace (`workspaceEls: 0`).
- Consecuencia demostrable: en los tests, Orchestrator escribió `hello.txt` y `workspace/
  prueba_artefacto.txt` **en la raíz del proyecto**, ensuciando el repo del usuario.

### 2.5 El gate no protege el camino del agente (hallazgo grave, verificado por mí)

- `orchestrator.py:407` calcula `decision = self.gate.evaluate(tool="route", ...)` y **nunca usa
  `decision`**. No hay rama de bloqueo.
- `orchestrator.py:875-884`: `_execute_tool()` hace `await self.mcp.call(tool_name, params)`
  directo, sin pasar por el gate ni por approvals.
- Contraste: `api/files_api.py`, `api/shell_api.py`, `api/search_api.py` y
  `agents/Planner/planner.py:234` **sí** consultan el gate.
- **Consecuencia:** por la UI, un `write_file` o `execute_command` decidido por el modelo
  se ejecuta sin aprobación. Lo verifiqué indirectamente: en el e2e el archivo se creó sin
  que apareciera ninguna tarjeta de aprobación.
- El gate sigue siendo **ingeniería demostrable real**: reglas, niveles, allowlist, 1685
  decisiones logueadas. Lo que falta es *cablearlo* al loop. Es un bug de 10 líneas con
  impacto enorme en la historia del proyecto.

### 2.6 Placebos que restan credibilidad (lista corta)

- `visual_audit` está **triple roto**: anunciada en el catálogo, no registrada en MCP, y
  los métodos `_run_visual_audit` / `_run_visual_audit_sync` que la invocan **no existen**.
  La implementación real (`system/visual_auditor.py`) está huérfana. Hay 87 grafos pero la
  capacidad estrella está muerta.
- `Curator.get_recent_work()` lanza `NameError` (falta `import sqlite3`) y
  `estimate_repo_size()` lanza `AttributeError` (`self._dir_size` no existe).
- Retriever RAG probablemente roto por mismatch de dimensiones de embedding
  (docs sin embedding 384d vs query 768d).
- El flujo de chat normal **nunca genera Task Graph**: `route()` va directo a
  `_agent_loop`. El grafo solo se alcanza por `/fix` y `/loop`. O sea: el motor más
  impresionante del repo **no se ve desde la UI**.
- Código muerto: `system/mcp_client.py` + `vcore_{fs,shell,audit}.py` (los `*_mcp.py`
  son los vivos). `scripts/audit.py` apunta al puerto 8001 (canónico: 8000).
  `scripts/test_frontend.py` también apunta a 8001 → **la suite falla con
  `ERR_CONNECTION_REFUSED`** (verificado).
- Versionado roto: `VCORE_STATE.json` dice **1.5.0**, el commit `ead47da` dice **"v1.5.1"**,
  y `docs/CHANGELOG.md` tiene dos bloques **"Unreleased"**. Tres fuentes, tres respuestas.
- `docs/FUNCIONES_PERDIDAS.md` + `docs/AUDITORIA_FUNCIONES.md` + `docs/ROADMAP_FRONTEND.md`
  son tres documentos de un refactor anterior **nunca cerrado** (33 funciones perdidas,
  12 por reimplementar). Es deuda que se arrastra, no un plan vigente.

---

## 3. ¿Es problema de backend o de frontend el "dibujado de sesiones"?

**De los dos, pero el peso está en el frontend y en el contrato entre ambos.** Desglose:

| Síntoma | Culpable | Evidencia |
|---|---|---|
| La conversación desaparece al recargar | Frontend | no llama `loadConvHistory` al arrancar; `activeConvId` es `null` en `init()` |
| Mensajes no se guardan | Ambos | `saveMessage` sale temprano si `activeConvId` es null; y nunca se manda `session_dir` |
| 422 al cargar historial | Contrato de API | path param `int` vs id string de sesión |
| Duplicación del texto final | Frontend | `endStream()` inserta el markdown sin limpiar los nodos de texto ya pintados |
| Artefactos no se ven antes de recargar | ya funciona | 1 tarjeta renderizada correctamente en el e2e |
| Artefactos no se ven después de recargar | Frontend + API | no hay endpoint de artefactos; se guardan en `localStorage` y el historial no los trae |
| No hay grafo de tareas visible | Backend (flujo) + Frontend | el grafo no se genera en el chat normal y la UI solo lo pinta si llega el evento |

Conclusión: **no hay que "arreglar el backend" para que la UI funcione.** Hay que
redefinir el contrato (sesión ↔ directorio ↔ historial ↔ artefactos) y reconstruir el
cliente sobre ese contrato. El backend necesita tres arreglos puntuales (§6, bloque P0).

---

## 4. Qué se salva y qué se desecha

### Se salva (el valor real)

1. **`gate.py` + `gate_rules.yaml` + `gate_log`** — seguridad determinística. Se cablea al
   loop. Es de lo más defendible del repo.
2. **`api/llm_client.py`** — CB, rate limiter, fallback, credential pool, hot-swap,
   presets por modelo. Zona protegida: se expone, no se reescribe.
3. **`system/task_graph_engine.py`** — DAG con persistencia, resume y recovery.
4. **`system/mcp_manager.py`** + los 8 tools `*_mcp.py`. Se borra el wrapper muerto.
5. **`system/observability.py`** + `traces.jsonl` — sin UI no vale nada; con UI es oro.
6. **Approvals end-to-end** (crear → aprobar → ejecutar, idempotente).
7. **Curator memoria** (`memory.py`) y **Retriever retrieval** — arreglando los 3 bugs.
8. **`api/version.py` + `VCORE_STATE.json`** — el patrón de fuente única es correcto.
9. **La paleta y el sistema de tokens de `style.css`** — 2 temas × 6 acentos bien
   resueltos (dark `--surf-chat`, light con cyan `#007A99` para contraste). Se hereda
   como design tokens, no como hoja de estilos.
10. **El layout de 3 columnas** (sidebar · chat · panel de artefactos) — es el correcto.

### Se desecha

1. **`Frontend/app.js` completo.** 622 líneas de estado global mutable, `onclick=` inline
   en HTML generado por strings (`onclick="selectConv('...')"`), 24 funciones exportadas a
   `window` a mano, y ~150 `catch {}` vacíos que convierten cada error en silencio. No se
   puede testear ni razonar. Se queda como referencia de comportamiento en `docs/_archive/`.
2. **`Frontend/marked.min.js`** (vendored, 49 KB) y el `highlightCode()` casero de 8 líneas
   que hace regex sobre código — reemplazables por un renderer con syntax highlighting real.
3. **La dependencia de CDN** para Monaco/hljs/fuentes en una app local.
4. **El esquema `sessions` plano** (id autoint, sin relación con `session_dir`, sin
   `updated_at`, sin preview del último mensaje, sin conteo de artefactos). Se migra con
   `ALTER TABLE` + tabla de eventos.
5. **El protocolo SSE ad-hoc** `{"type": "chunk"}` sin `seq`, sin `ts`, sin `run_id`, sin
   `message_id`: imposible reconectar o reconstruir. Se reemplaza por un envelope tipado y
   versionado (§5).
6. **Todo el sistema de artefactos en `localStorage`** — se pierde al cambiar de navegador
   y no es consultable. Va a SQLite.
7. **Código muerto**: `mcp_client.py`, `vcore_{fs,shell,audit}.py`, `_execute_tool_legacy`
   (300+ líneas en `orchestrator.py`), `docs/ROADMAP_TAURI.md` como plan vigente.
8. **Los tres documentos del refactor fantasma** (`ROADMAP_FRONTEND.md`,
   `FUNCIONES_PERDIDAS.md`, `AUDITORIA_FUNCIONES.md`): se consolidan en uno y se archivan.
9. **`workspace/` como carpeta decorativa** con solo `uploads/`.

---

## 5. Arquitectura propuesta

### 5.1 Principio rector: event sourcing sobre el stream

El backend ya emite eventos; el error fue no tratarlos como **la** fuente de verdad. El
diseño: cada run agéntico produce un **log append-only de eventos tipados**. La UI es una
función pura de ese log: `UI = reduce(eventos)`. El mismo `reduce` alimenta el vivo (SSE)
y el replay (historial). Consecuencia: reabrir una conversación muestra exactamente las
mismas tool cards, artefactos y approvals que ocurrieron, sin código duplicado.

Es el patrón que usa el estado del arte (AG-UI, AI SDK v5) y es el argumento más fuerte
para una entrevista: **explica por qué la UI no puede desincronizarse del backend.**

```json
{ "v": 1, "seq": 42, "ts": 1790194795.25, "run_id": "…", "thread_id": "205",
  "message_id": "msg_7", "type": "tool.end",
  "data": { "call_id": "c1", "name": "write_file", "args": {...}, "result": "...", "duration_ms": 12 } }
```

Tipos mínimos: `run.start|end|error|cancelled`, `text.delta`, `reasoning.delta`,
`tool.start|end`, `artifact.created`, `approval.requested|resolved`, `graph.snapshot`,
`usage.update`, `circuit.state`.

**Persistencia**: tabla `events(thread_id, seq, ts, type, data_json)` + `snapshot` por
thread. El historial deja de ser "mensajes" y pasa a ser "log", que es lo que el sistema
realmente produce.

### 5.2 Stack

| Capa | Decisión | Razón |
|---|---|---|
| Build | **Vite + TypeScript** | tipado del envelope = la UI no puede inventar campos |
| UI | **React 19** | ecosistema de streaming/editor/virtualización más maduro |
| Estilos | **Tailwind v4 + design tokens heredados de `style.css`** | conserva los 2 temas × 6 acentos que ya están resueltos |
| Componentes | **shadcn/ui (Radix)** | accesibles, sin dependencia de un vendor |
| Estado | **Zustand** (store del log) + **TanStack Query** (REST) | el log es un store; lo demás es caché de servidor |
| Lista de mensajes | **@tanstack/react-virtual** | las sesiones largas son el caso normal |
| Editor | **CodeMirror 6** | 1/5 del peso de Monaco, tree-shakeable, sin CDN, buen soporte de solo-lectura |
| Markdown | **react-markdown + Shiki** | sin CDN, resaltado real, con sanitización |
| Transporte | **POST + SSE** (fetch stream) con `seq` y `after=` | ya existe SSE; reconexión sin cambiar de protocolo |

Descartado: **CopilotKit / LangGraph frontend** (acoplan el proyecto a su runtime y
esconden lo que justamente quieres mostrar), **Next.js** (no hay SSR ni rutas que lo
justifiquen), **vanilla** (el problema fue exactamente no tener estructura).

### 5.3 Workspaces funcionales

Hoy `sessions/<id>/` es un perfil. Propuesta: **un workspace real y aislado por sesión**,
con `git worktree`.

```
sessions/sess_206_a1b2c3d4/
  meta.json          # modelo lead por sesión, cwd, created_at
  config/            # model_routing.yaml, gate_rules.yaml (lo que ya existe)
  work/              # ← EL WORKSPACE: git worktree del repo (o dir vacío)
  artifacts/         # archivos producidos por el agente, versionados por evento
  events.jsonl       # log de eventos de la sesión
```

- `git worktree add sessions/<id>/work HEAD` da aislamiento real: dos sesiones pueden
  trabajar el mismo repo sin pisarse, y `git diff` por sesión sale gratis. Es la respuesta
  correcta a "para qué sirve un workspace".
- Todo tool de archivos resuelve rutas **relativas al workspace** de la sesión, y el gate
  valida contra ese root (no contra la raíz del proyecto).
- La UI expone: selector de workspace en el topbar, árbol de archivos del workspace (no
  del repo), badge de "cambios sin commitear", botón "abrir en el visor", y descarga de
  artefactos.

### 5.4 La UI: cinco superficies, no una

El frontend actual intenta ser IDE + chat + panel y no logra ninguna. Propuesta:

1. **Chat** — burbujas, streaming real, bloques de razonamiento colapsables, tool calls
   como tarjetas ricas (nombre, args, duración, resultado, error), approvals inline con
   riesgo visible, cancelación real (`POST /runs/{id}/cancel`).
2. **Artefactos** — visor con CodeMirror, diff unificado para cambios de archivo, tabs por
   artefacto, descarga, "copiar" y — clave — **persistidos en SQLite**, no en localStorage.
3. **Panel de sistema** — el que hoy no existe y es tu mejor material: estado de cada
   provider y circuit breaker, gasto de tokens por modelo y por sesión, trazas por
   `task_id`, últimas decisiones del gate (con su razón), approvals pendientes.
4. **Grafo de tareas** — visualización real del DAG (nodos, estados, dependencias) cuando
   hay grafo; hoy el motor se ejecuta a ciegas.
5. **Workspaces** — lo de §5.3.

Regla de honestidad visual: **ningún dato inventado** (se va el contador de tokens
estimado a `chars/4` y todo estado "ok" hardcodeado).

---

## 6. Plan por bloques

Los bloques se ejecutan **en orden**, con verificación y checkpoint al cerrar cada uno.
Nada se da por bueno sin evidencia (captura + endpoint + test).

### P0 — Verdades del backend (sin esto, la UI miente)

1. **Cablear el gate al agent loop** (`orchestrator.py`): `_execute_tool` consulta el gate;
   nivel B crea approval y **detiene** la ejecución hasta resolución. Cierra el agujero de
   §2.5. De paso, el evento `approval.requested` ya existe en el protocolo.
2. **Unificar sesión ↔ thread ↔ workspace**: `ChatMessage.session_dir` obligatorio,
   historial por `thread_id` (no `int`), y `GET /threads/{id}` con metadata + eventos.
3. **Arreglar los tres bugs de memoria/retrieval**: `import sqlite3` en `curator.py`,
   `_dir_size`, y dimensiones de embedding en Retriever (verificar contra Chroma real).
4. **Revivir o enterrar `visual_audit`**: importar `system/visual_auditor.py` en el
   catálogo MCP **o** borrar las 3 referencias muertas. No puede quedar a medias.
5. **Versionado**: una sola fuente (`VCORE_STATE.json`), bump a **2.0.0** por ser un
   cambio de contrato, y CHANGELOG con la entrada real. Cerrar los dos "Unreleased".
6. **Limpiar señal**: borrar código muerto, arreglar el puerto 8001 de los scripts, y
   consolidar los 3 documentos del refactor fantasma.

### P1 — Protocolo de eventos + persistencia

7. Definir el envelope tipado (§5.1) en `api/events.py`, con `seq` monotónico por run.
8. Persistir eventos en `events` + `snapshot`; endpoint `GET /threads/{id}/events?after=seq`.
9. Adaptar Orchestrator para emitir el envelope v1 (traducción en el borde, no en cada tool).

### P2 — Frontend nuevo desde cero

10. `web/` con Vite + TS + React + Tailwind; tokens de diseño portados desde `style.css`.
11. Store del log + `reduce()` único compartido entre vivo y replay.
12. Chat: streaming, razonamiento, tool cards, approvals, cancelación.
13. Artefactos: CodeMirror, diffs, tabs, persistencia, descarga.
14. Panel de sistema: providers, CB, usage, trazas, gate log, approvals.
15. Grafo de tareas con layout de DAG.
16. Workspaces: selector, árbol del workspace, git status por sesión.

### P3 — Cierre demostrable

17. El frontend nuevo reemplaza a `Frontend/` (el viejo se archiva, no se borra en silencio).
18. Suite E2E propia (Playwright) sobre el protocolo: streaming, tool call, artifact,
    approval, reload con replay fiel, cambio de modelo, cambio de workspace.
19. README + un diagrama de arquitectura + sección "cómo se ve en 30 segundos" para la
    entrevista.

---

## 7. Decisiones que se toman acá (y que conviene aprobar)

1. **Rehacer el frontend con React+Vite+TS en vez de reparar el vanilla.** Reparar el
   vanilla cuesta menos hoy y vuelve a romperse en 2 semanas: el problema no son los bugs,
   es que no existe una capa de estado ni un contrato de eventos.
2. **Event sourcing como espina dorsal**, no "guardar mensajes y armar la UI al vuelo".
3. **CodeMirror en vez de Monaco** — quita la dependencia de CDN y baja el peso.
4. **Gate cableado al loop, con approval bloqueante.** Es el cambio que más credibilidad
   devuelve por línea escrita.
5. **Workspace = git worktree por sesión** — aislamiento real y `git diff` gratis.
6. **Bump a 2.0.0** con contrato nuevo y CHANGELOG honesto.
7. **No borrar nada del frontend viejo hasta que el nuevo pase la suite E2E**; se archiva
   en `docs/_archive/frontend_v1/`.

---

## 8. Lo que NO haría (y por qué)

- **Microservicios o base de datos nueva (Postgres).** SQLite alcanza y sobra para uso
  local; agregar infraestructura para impresionar es contraproducente en entrevista.
- **Dockerizar el sandbox ahora.** Correcto en teoría (aislar código no confiable), pero
  agrega un problema operativo antes de que exista un workspace. Primero worktree, después
  sandbox si hace falta.
- **Adoptar CopilotKit/LangGraph como dependencia.** Taparía justamente la ingeniería que
  quieres mostrar (tool loop, approvals, gate).
- **Reescribir `llm_client.py`.** Es lo más sólido del repo y está marcado como zona
  protegida. Se expone en la UI, no se toca.
- **Cargar las 56 sesiones basura.** Se archivan de una vez (`sessions/_archive/`) y el
  sidebar arranca limpio, con búsqueda y agrupación por fecha.

---

## 9. Notas de honestidad

- El informe de "estado del arte" (subagente) verificó nombres de specs y versiones
  mayores contra fuentes; no verificó versiones patch ni benchmarks.
- El inventario agentivo (subagente) fue contrastado por mí en los puntos críticos:
  gate no cableado (`orchestrator.py:407,881`), memoria existente (`memory.py`, 23 filas),
  y ausencia de Task Graph en el chat normal. Los puntos que cito como verificados los leí
  en el código; los que no leí están atribuidos a la auditoría y marcados como tales.
- Las capturas en `.audit_shots/` son de hoy, con el sistema vivo. Sirven como "antes"
  para comparar contra el refactor.
