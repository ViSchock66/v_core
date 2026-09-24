# V-CORE 2.0 — Plan de refactor y fuente única de verdad

> **Este documento manda.** Si algo acá contradice a un comentario de código, un
> informe de subagente o a `ANALISIS_REFACTOR_FRONTEND.md`, gana este documento
> hasta que se actualice con evidencia nueva.
>
> Regla de oro: **ningún bloque se cierra sin evidencia de ejecución**, y para
> todo lo que se ve en pantalla la evidencia es **una captura real del navegador
> con el backend vivo** — no un test verde, no un `build` exitoso.

- **Fecha de apertura:** 2026-09-23
- **Estado del sistema:** backend vivo en `:8000`, `version 2.0.0`
- **Fase actual:** F0 — cimientos (planificación cerrada, sin código nuevo aún)

---

## 0. Por qué existe este documento

El intento anterior de refactor se degradó por una razón medible, no por falta de
talento: **se escribió un frontend entero sin mirar la pantalla**. Evidencia del
transcript de esa sesión (8 turnos):

| Métrica | Valor |
|---|---|
| Escrituras de archivo (`write`/`edit`) | **193** |
| Comandos `pwsh` | 183 |
| Verificaciones en navegador | **0** |
| Errores de LLM / retries | 0 / 0 |

Cero errores de modelo: no fue una alucinación de fallback. Fue trabajo a ciegas.
El resultado es un frontend que **compila, no lanza ni un error de consola
(`pageErrors: 0`) y no muestra nada útil**. Eso es peor que un crash, porque un
crash se nota.

Consecuencia de método, que es la parte que importa: **cada bloque de este plan
termina en una captura**, y el bloque no se marca cerrado sin ella.

---

## 1. Objetivo y no-objetivos

### 1.1 Objetivo

Un **harness de código agéntico** presentable en entrevista de trabajo, con
identidad propia de V-Core, donde se pueda **ver** la ingeniería que el sistema
ya tiene: tool loop, permisos determinísticos, aprobaciones humanas, circuit
breakers, event sourcing, workspaces.

### 1.2 Decisiones cerradas con el PM (2026-09-23)

| Decisión | Resuelto |
|---|---|
| Identidad visual | **Romper con el frontend viejo.** Rediseño real tipo Codex. |
| Jerarquía del producto | **Harness de código tipo Codex, manteniendo la identidad V-Core**: chat denso como superficie principal, cósmico/sumerio como lenguaje visual, no un clon de la paleta actual. |
| Alcance del backend | **Lo que haga falta**, pero planificado sobre lo que existe en el repo (verificado, no supuesto). |

### 1.3 No-objetivos (explícitos, para no diluir el esfuerzo)

- **No** clonar la estética del frontend viejo. Ya se hizo y es la causa directa
  del problema actual (§2.2).
- **No** microservicios, Postgres ni Docker. SQLite sobra para uso local, y
  agregar infraestructura para impresionar es contraproducente.
- **No** adoptar CopilotKit / LangGraph frontend: taparían justamente la
  ingeniería que se quiere mostrar.
- **No** reescribir `api/llm_client.py`. Es lo más sólido del repo.
- **No** cargar las 49 sesiones basura en el sidebar.
- **No** tocar `Frontend/` (el viejo) salvo para archivarlo en el commit que
  valide su reemplazo.

---

## 2. Estado real verificado (no planificado)

Todo lo de esta sección lo verifiqué hoy contra el sistema vivo.

### 2.1 Lo que funciona de verdad (se conserva y se muestra)

| Capacidad | Evidencia |
|---|---|
| **Gate de permisos** | `test_gate_v2.py`: 18/18, cobertura de las **8 tools reales, ninguna sin cobertura**. |
| **Gate cableado al loop** | `test_gate_wiring.py`: **0 fallas**; permit/ask/deny verificados; ejecuciones reales contadas (2 de 2 esperadas). |
| **Aprobaciones HITL** | Broker con `asyncio.Future`; stats reales `requested:3, approved:1, rejected:1, timed_out:1`. |
| **Path traversal bloqueado** | `../../../Windows` → denegado; `archivo escapado (debe ser False): False`. |
| **Event sourcing** | `agent_events` persistido; 64 eventos reales en el hilo `sess_210_c69a40a5` (`text.delta:34`, `reasoning.delta:27`, `tool.end:1`, `run.start/end`). |
| **Protocolo de eventos v1** | `test_event_protocol.py`: `events=56 runs=1`, numerado y reconectable. |
| **Embeddings** | `/embeddings/status` → tier activo **NIM `nvidia/nemotron-3-embed-1b`, 2048 dims, `ok:true`**. |
| **Circuit breakers** | 15 breakers en CLOSED; `/llm/circuit-status` responde. |
| **Backend** | 56 rutas; `/health` → `{"status":"ok","version":"2.0.0"}`. |
| **Frontend nuevo compila** | `web_dist/` construido; React monta (`rootChildren: 1`, 115 botones, 0 errores). |

Esta tabla es el material de entrevista. Nada de esto se toca sin motivo.

### 2.2 La causa raíz del desastre visual: se replicó el diseño viejo

**Medición, no opinión:**

- `web/src/styles/index.css` usa **65 veces `--accent`** y **4 veces
  `--surf-chat`**: es el **mismo sistema de tokens** de `Frontend/style.css`.
- El comentario del propio archivo lo confiesa (`index.css:5`): *"Hereda los
  tokens del frontend anterior (que estaban bien resueltos...)"*.
- El layout de 3 columnas (sidebar · chat · panel) se declaró "el correcto" en
  `ANALISIS_REFACTOR_FRONTEND.md` §4 y se conservó.
- La decisión se tomó en el plan anterior: §4 punto 9 y §5.2.

**Consecuencia:** se cambió el motor (vanilla → React+Vite+TS, que es
arquitectura y está bien) y se conservó la **capa visual entera**. El resultado
se siente como un no-cambio porque, visualmente, lo es.

Además, el frontend **nuevo se sirve desde `/`** (verificado: el HTML servido
referencia `index-TG2vjSeq.js`, build de 20:14, posterior al `Frontend/` viejo
de las 02:01). O sea: **el usuario estaba mirando el nuevo y le pareció el
viejo.** Ese es el diagnóstico central de todo este plan.

### 2.3 La causa raíz funcional: la sesión no tiene identidad persistida

**Esto es arquitectura de datos, y es el bug que rompe el dibujado de sesiones.**

```sql
-- Esquema real de la tabla `sessions` (verificado con PRAGMA table_info):
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, project TEXT, tier INTEGER,
    budget_tokens INTEGER, status TEXT DEFAULT 'open', title TEXT
)   -- ← NO EXISTE la columna session_dir
```

Pero `GET /sessions` **devuelve** `session_dir` en el JSON, porque lo **deriva
al vuelo** en `main.py:1066`:

```python
d["session_dir"] = _resolve_session_dir(d["id"])

def _resolve_session_dir(session_id: int) -> str | None:
    import glob as _glob
    matches = sorted(_glob.glob(str(SESSIONS_DIR / f"sess_{session_id}_*")))
    return Path(matches[-1]).name if matches else None
```

**El vínculo entre una sesión (`id=210`) y su conversación
(`thread_id='sess_210_c69a40a5'`) no es un dato: es un `glob` sobre el sistema de
archivos.** Ninguna tabla lo guarda.

Consecuencias demostrables:

1. Si el directorio en disco se borra o mueve, **la conversación desaparece de
   la UI aunque sus 64 eventos sigan intactos en SQLite**.
2. Hay un `thread_id = 'default'` con **35 eventos**: sesiones que corrieron sin
   `session_dir` y **mezclaron conversaciones distintas en un mismo log**.
3. Los títulos son `'Nueva conversación'` en masa: 49 sesiones en el sidebar, sin
   preview, sin agrupación, sin paginación.

**Corrección importante a un diagnóstico intermedio mío:** el cliente **no** está
mal en este punto. `Sidebar.tsx:169` y `chat.ts:207` usan `session_dir`
correctamente. El defecto está en el **backend**, que nunca persistió la
relación. El frontend nuevo quedó escrito asumiendo un contrato que la base no
cumple — y como el `glob` funciona hoy por coincidencia (los directorios
`sess_190_*`…`sess_210_*` existen en disco), el bug es **latente y silencioso**.

### 2.4 Otras verdades que mienten

| Síntoma | Evidencia | Naturaleza |
|---|---|---|
| Rol de recuperación apunta a proveedor muerto | `/system/model` → `retriever: ollama/nomic-embed-text`, pero `/embeddings/status` dice Ollama `"ok":false` | Backend (config) |
| Rutas del contrato nuevo ausentes | `/threads` (listado) → **404**; `/policy/stats` → **404**. Solo existen `/threads/{key}/events` y `/summary` | Backend (contrato incompleto) |
| Dos frontends vivos | `main.py:1714` monta `web_dist/` y si no `Frontend/`. El viejo **nunca se destruyó** pese a ser el objetivo declarado | Backend (fallback) |
| Sidebar sin señal | 49 items `Sesión NNN`, ninguno legible; bolitas de estado grises aunque la sesión tenga 64 eventos | Frontend + backend |

### 2.5 La prueba visual (lo que nunca se hizo)

Captura `.audit_shots/v2_01_initial.png`, build servido por FastAPI, 1440×900:

- **0 errores de página** y **0 errores de consola** — todo "funciona".
- `rootChildren: 1`, 115 botones, 58 íconos SVG, `monaco: undefined`.
- Sidebar: **49 sesiones** todas `Sesión NNN`; `hilosVisibles: []` (ningún
  `sess_NNN_hash` en pantalla); estados grises salvo la activa.
- Área central: **~800 px de vacío** con "V—CORE / Creá una conversación para
  empezar".
- Panel derecho: **380 px vacíos**.
- Lo que sí está bien: badge `v2.0.0` con punto verde, selector `glm-5.3` **con
  `rol: lead` visible** (esto mejoró respecto del viejo), 6 acentos, toggle de
  tema.

**Diagnóstico:** tres columnas y **ninguna tiene contenido**. El layout de 3
columnas que el plan anterior declaró "el correcto" es precisamente lo que
produce este vacío.

---

## 3. Qué se salva y qué se desecha

### 3.1 Se salva (ingeniería demostrable — el material de entrevista)

1. `gate.py` + `gate_rules.yaml` + `gate_log` — permisos determinísticos.
2. `api/policy.py` + `api/approval_broker.py` — HITL con `asyncio.Future`.
3. `api/llm_client.py` — circuit breaker, rate limiter, fallback chains,
   credential pool, hot-swap. **Zona protegida.**
4. `api/events.py` + `agent_events` — event sourcing con `seq` y replay.
5. `system/mcp_manager.py` + los 8 tools `*_mcp.py`.
6. `system/task_graph_engine.py` — DAG con persistencia y resume.
7. `system/observability.py` + `traces.jsonl`.
8. `agents/Curator/memory.py` + `agents/Retriever/retriever.py` (con el fix de dims).
9. `api/version.py` + `VCORE_STATE.json` — fuente única de versión.
10. `web/src/lib/reduce.ts` + `protocol.ts` + `api.ts` — **el reducer único
    compartido entre vivo y replay es una buena decisión y se conserva.**
11. Los tests del trabajo previo (`test_gate_v2`, `test_gate_wiring`,
    `test_event_protocol`, `test_fs_policy`, `test_policy_commands`).

### 3.2 Se desecha

1. **La capa visual heredada**: tokens clonados de `Frontend/style.css`, layout
   de 3 columnas fijas, sidebar de 237 px con pared de `Sesión NNN`.
2. **`Frontend/` completo** (app.js, index.html, style.css, marked.min.js) → se
   **archiva** en el commit que valide el reemplazo, y se elimina el fallback de
   `main.py:1714`.
3. **El esquema `sessions` sin `session_dir`** → migración real (ver F1).
4. **El hilo `'default'` como cajón de sastre** (35 eventos mezclados).
5. **Los 3 documentos del refactor fantasma** (`ROADMAP_FRONTEND.md`,
   `FUNCIONES_PERDIDAS.md`, `AUDITORIA_FUNCIONES.md`): se consolidan acá y se
   archivan.
6. **`docs/ANALISIS_REFACTOR_FRONTEND.md`** como plan vigente: pasa a ser
   **insumo histórico**. Su §5.2 (stack) y §5.1 (event sourcing) se mantienen
   vigentes; su §4 punto 9 (heredar tokens) queda **revocado**.

---

## 4. Principios de ingeniería (no negociables)

1. **La UI no inventa estado.** Todo lo que se ve sale de reducir el log de
   eventos. Ya está resuelto en `reduce.ts`; se conserva.
2. **Ningún dato inventado.** Se va cualquier contador estimado (`chars/4`) o
   estado "ok" hardcodeado. Un número falso es peor que ningún número.
3. **Una sola fuente de verdad por dato.** Ya se hizo con la versión
   (`version.py`); ahora con la identidad de sesión.
4. **Verificación visual obligatoria.** Captura del navegador con backend vivo.
5. **Un bloque = un commit**, con su evidencia en el mensaje.
6. **Read-only por defecto.** Las tools del agente operan dentro del workspace
   de la sesión; el perímetro lo valida el gate, no el prompt.

---

## 5. Bloques de trabajo

Cada bloque declara su **criterio de aceptación** (verificable) y su **commit**.
El orden importa: F1 desbloquea todo lo demás.

### F0 — Cimientos documentales *(este documento)*

- [x] Verificar el estado real del sistema (backend, DB, navegador).
- [x] Cerrar decisiones de identidad visual, jerarquía y alcance con el PM.
- [x] Escribir este plan como fuente única de verdad.
- **Aceptación:** este archivo existe, está commiteado y el PM lo aprobó.
- **Commit:** `docs: plan de refactor v2 como fuente unica de verdad`

### F1 — Identidad de sesión persistida *(bloqueante de todo)*

El bug de §2.3. Sin esto, ninguna UI puede dibujar conversaciones de forma
confiable.

- `ALTER TABLE sessions` + migración: agregar `session_dir TEXT`, `updated_at`,
  `last_message_preview`, `event_count`; backfill de las filas históricas
  reusando el `glob` **una sola vez**.
- `_resolve_session_dir()` deja de adivinar: lee la columna. Se conserva el
  `glob` solo como *fallback* de migración, marcado como tal.
- Escribir `session_dir` al crear la sesión (`main.py:1117` ya genera
  `sess_{id}_{hex8}`: ese valor va a la base, no solo al disco).
- Backfill de los eventos huérfanos de `thread_id='default'`: se aíslan o se
  marcan, nunca se mezclan con una sesión real.
- Endpoint `GET /threads` (listado) para que el sidebar no dependa de `/sessions`
  + adivinanza, **o** se documenta que `/sessions` es la única fuente y devuelve
  `session_dir` de la base.
- **Aceptación:** crear sesión → enviar mensaje → recargar la página → **la
  conversación reaparece**; y `PRAGMA table_info(sessions)` muestra
  `session_dir`. Captura antes/después.
- **Commit:** `fix(sesiones): persistir session_dir y eliminar la resolucion por glob`

### F1.5 — Motor honesto *(catálogo único + prompt versionado)*

Tres representaciones duplicadas de la misma verdad. Detalle y fuentes en
`docs/COMPARATIVA_HARNESSES.md` §8.

- **Un solo catálogo de tools**, derivado de `gate_rules.yaml` (canónico: **22
  tools**) y de `MCPManager.get_tools_catalog()` (dinámico). Se elimina
  `_TOOL_DEFS` hardcodeado (`orchestrator.py:502-512`), que expone **8** y es el que se
  usa. Hoy `_OPENAI_TOOLS` se calcula y **no se consume**.
- **El catálogo deja de duplicarse como texto** en el system prompt
  (`orchestrator.py:529-537`).
- **Prompt operacional a archivo versionado**, separado de la persona. Hoy
  `_IDENTITY_LOCK` + operacional = **~5.224 tokens** fijos por iteración, en
  español y con rutas muertas (`Frontend/app.js`, `:539-542`). Objetivo: ~1.500
  tokens sin perder reglas operativas.
- **Eliminar `_execute_tool_legacy`** (~300 líneas, `orchestrator.py:1133`).
- **Techo de iteraciones sensato**: `max_iterations=999` → derivado del
  presupuesto real.
- **Aceptación:** `_TOOL_DEFS` con 0 ocurrencias; el catálogo expuesto al modelo
  coincide en número con `gate_rules.yaml`; el prompt operacional vive en un
  archivo; run real que ejecute una tool.
- **Commit:** `refactor(motor): catalogo unico de tools y prompt versionado`

### F1.6 — Compactación de contexto *(la fuga más grande)*

Hoy no existe (`compact` → 0 ocurrencias) y el historial se corta duro con
`history[-10:]` (`orchestrator.py:572,1276`). Diseño tomado de Claude Code (3 niveles) y
Pi (lossless), adaptado a nuestro event log.

- **Nivel 1 — vaciar resultados de tools viejos** (equivalente a MicroCompact):
  conservar los **N más recientes** completos, vaciar los anteriores. **Es la
  corrección directa del `result[:800]`** (`orchestrator.py:752,837,899,959`): hoy el
  modelo nunca ve un archivo entero. Claude Code hace lo contrario a nosotros:
  conserva completo y vacía solo lo viejo.
- **Nivel 2 — resumen del historial** con umbral derivado de la **ventana
  efectiva**, no un porcentaje fijo: `effective = context_window -
  min(max_output, 20_000)`, umbral `effective - 13_000`. El bug de Codex (compacta
  a 94,7% en vez de 90%, costando 8,37M tokens de entrada en el hueco) es la
  advertencia de por qué no usar la ventana cruda.
- **Preservar tool_use/tool_result emparejados** al cortar. Cortar por índice
  rompe pares y la API devuelve error; Claude Code dedica 80+ líneas a esto.
- **Re-inyectar los últimos archivos leídos** tras compactar (Claude Code: 5
  archivos, presupuesto 50K, 5K por archivo) para no inducir amnesia.
- **Circuit breaker de compactación**: tras N fallos consecutivos, parar en esa
  sesión (Claude Code usa 3; su nota interna reporta 1.279 sesiones con 50+ fallos
  desperdiciando ~250K llamadas API/día).
- **Compresión lossless**: las entradas originales permanecen en el event log
  (Pi lo hace en el árbol de sesión). Compactamos el **contexto**, no el
  **registro** — a diferencia de Codex, que descarta llamadas y salidas de tools.
- **Aceptación:** test que llene la ventana y demuestre que (a) se dispara en el
  umbral derivado, (b) los pares tool_use/tool_result quedan íntegros, (c) el
  event log sigue completo después, y (d) una conversación larga sigue respondiendo
  con coherencia. Captura de la UI con el estado visible.
- **Commit:** `feat(contexto): compactacion por niveles con umbral derivado`

### F1.7 — Cancelación real de runs

`interrupt` → 0 ocurrencias. El cliente tiene `AbortController`, pero el backend
**no puede detener un run en curso**. Los cuatro harnesses comparados lo tienen.

- Endpoint de cancelación por `run_id` que detiene el agent loop y persiste
  `run.cancelled`.
- El estado del run debe quedar **honesto**: cancelado, nunca "completado".
- **Aceptación:** lanzar un run, cancelarlo a mitad, verificar que el loop se
  detiene, que el evento queda persistido y que la UI lo muestra como cancelado.
- **Commit:** `feat(runs): cancelacion real de runs en curso`

### F1.8 — Permisos: postura, escalada y estado visible

**Origen:** la crítica del PM al gate ("pide permiso hasta para leer un doc") se
midió y **resultó correcta**. 29 operaciones típicas de un harness de código:

```
permit  18 (62,1%)   ask  7 (24,1%)   deny  4 (13,8%)
```

El problema no es que el gate exista: es que **interrumpe de más y explica de
menos**. Y el `gate_rules.yaml:103` ya lo había diagnosticado:

> *"…habría dejado al agente pidiendo permiso hasta para listar un directorio:
> fricción inútil que entrena al usuario a aprobar sin leer."*

El arreglo anterior quedó a medias: se completó el catálogo de tools para que no
cayeran en fail-safe, pero **no se agregó el mecanismo que resuelve la fricción de
raíz**.

#### Modelo de tres niveles (tomado de DSH, verificado)

| Nivel | Qué es | Quién decide |
|---|---|---|
| **L1 Postura** | `read-only` → `workspace-write` (default) → `full-access` | **El humano, al lanzar.** Nunca el agente. |
| **L2 Operación** | `permit` / `ask` / `deny` | El agente propone, la política resuelve |
| **L3 Concesión** | una vez · por sesión · siempre para esta tool | **El humano** |

**Invariante:** la escalada existe y es parte del diseño; lo que no existe es la
**autoconcesión**. El agente no cambia su propia postura.

#### Lo que hay que construir

1. **Postura de sesión como fila de composición** (L1). `danger-full-access` se
   elige al lanzar, no se activa en caliente desde el agente. Es el primer caso de
   uso real del kernel Cordis (§ F4.5): `minimal` / `code` / `full` son **YAML**,
   no reescrituras.
2. **Escalada con justificación** (L2). `approval.requested` gana un campo
   `justification` que el agente redacta. Hoy la tarjeta muestra *qué* (tool +
   params) y **nunca *por qué*** — un humano no puede decidir informado.
3. **Concesiones con alcance** (L3). Sin "permitir siempre esta tool", el humano
   clickea 50 veces y aprende a aprobar sin leer. La fricción no se arregla con
   menos control, se arregla con **control mejor diseñado**.
4. **Estado siempre anunciado** (el punto que habilita auditar). **Todo estado de
   espera declara: qué espera, por qué, y qué lo desbloquea.** Sin esto, una
   pantalla en blanco es ambigua: no se puede distinguir "esperando aprobación,
   todo bien" de "colgado para siempre". Eventos mínimos: `run.state`
   (`thinking` | `awaiting_approval` | `blocked` | `denied` | `cancelled`), cada
   uno con `reason` y `unblocked_by`.
   - **Consecuencia directa:** la auditoría visual se vuelve automatizable. En vez
     de mirar una captura y adivinar si el vacío es normal, se puede **afirmar**
     sobre el estado (`awaiting_approval` + `unblocked_by: "approve req_x"`) y
     detectar un deadlock por ausencia del evento.
5. **Los dos bugs medidos:**
   - `list_files(".")` → `ask` por `path.outside_agent_allowlist`. **Listar la
     raíz del proyecto no puede pedir permiso.** Las tools `read_only` no deben
     pasar por la allowlist de agente, o la raíz debe estar en ella.
   - **`web/` no está en la allowlist de Planner.** El agente coder **no puede
     escribir el frontend nuevo**. Tampoco están `docs/` ni `tests/`. Hay que
     revisar la allowlist completa contra la estructura real del repo (y sacar
     `Frontend/`, que se archiva).

#### Aceptación

- **Fricción medida, antes y después:** el mismo script de 29 operaciones debe
  mostrar **0 `ask` en lectura y navegación**, y `ask` solo en lo que
  legítimamente lo merece (`git reset --hard`, `pip install`, `curl`, `python -c`).
- **`list_files(".")` → `permit`**, verificado por script.
- **Planner puede escribir en `web/`**, verificado por script.
- **Escalada con justificación:** un run que pida algo fuera de allowlist muestra
  en la UI la justificación redactada por el agente, y las tres opciones de
  concesión.
- **Estado visible y auditable:** un run detenido en aprobación **no** deja la UI
  en blanco; se puede leer el estado y qué lo desbloquea desde el DOM. Verificado
  con la sonda de Playwright, no a ojo.
- **Sin deadlocks silenciosos:** un test que verifique que **todo** estado de
  espera tiene un evento asociado; si un run espera sin anunciarlo, el test falla.

- **Commit:** `feat(permisos): postura de sesion, escalada justificada y estado visible`

### F4.5 — Kernel Cordis *(todo es un plugin)*

Se introduce **sobre lo que ya funciona**, después de que el sistema esté sano.
Detalle y fuentes en `docs/COMPARATIVA_HARNESSES.md` y en el análisis de Cordis
v4.0.1 (`@deepseek-ai/cordis`, vendorizado en DSH).

**Concepto:** cada capacidad es una fila en un `cordis.yml`. Cambiar lo que un
agente puede hacer = cambiar qué filas se componen. Nada más.

- **Kernel:** `Context` (contenedor de dependencias), `Fiber` (handle de
  propiedad), `provide`/`get`/`inject`, `effect` con disposer. **Desmontar un
  fiber revierte todo lo que registró** — eso es lo que hace posible
  activar/desactivar capacidades sin reiniciar.
- **Dos planos:** *host* (gate, aprobaciones, registries, router de proveedores,
  persistencia, breakers) y *preset* (tools, prompt, persona, política de
  compactación).
- **Lo primero que hay que construir es el validador de montaje**, porque es lo
  que más valor devuelve: compone el subárbol de verdad y rechaza (a) paquete que
  no resuelve, (b) config inválida, (c) **fila que nunca se activa porque nadie
  provee lo que inyecta**, (d) servicio publicado en el realm raíz.
  - **Habría cazado `visual_audit`**: anunciada en `_TOOL_DEFS` (`orchestrator.py:506`) y
    en el prompt (`:530`), y **no registrada en MCP**. Es exactamente el fallo (c).
  - También habría cazado el `retriever → ollama/nomic-embed-text` apuntando a un
    proveedor caído.
- **Perfiles** `minimal` / `code` / `full` como archivos de composición. La postura
  de F1.8 es una fila más.
- **Guard de profundidad de subagentes**: máximo N niveles, y **retirar la tool de
  delegación en el límite** (Claude Code usa 3). Sin esto, un multiagente se
  auto-amplifica y agota el presupuesto — verificado en carne propia (§7.7).
- **Aceptación:** el validador rechaza una composición con una fila que inyecta un
  servicio inexistente y otra con config inválida; los tres perfiles montan; el
  perfil `minimal` arranca sin breakers ni memoria y el sistema sigue respondiendo.
- **Commit:** `feat(kernel): composicion por plugins con validador de montaje`

### F2 — Identidad visual V-Core *(el bloque que responde a "se ve igual")*

Rediseño real. Rompe con los tokens heredados.

- **Romper la herencia:** tokens nuevos, no la paleta clonada. Lenguaje visual
  cósmico/sumerio **propio** (el proyecto ya tiene nombres: Orchestrator, Planner, Retriever,
  Curator; eso es identidad, y hoy no se ve en pantalla).
- **Cambiar el layout:** el chat es la superficie dominante. Los paneles de
  sistema/artefactos/workspace pasan a **drawers superpuestos**, no a una tercera
  columna fija que resta 380 px aunque esté vacía.
- **Tipografía y densidad** propias, no las del frontend anterior.
- **Estado vacío con contenido real:** el primer arranque debe mostrar qué es
  V-Core (agentes, permisos, estado del sistema), no 800 px de blanco.
- **Aceptación:** captura que **no** sea confundible con `Frontend/`; el PM
  confirma que "no se parece al viejo".
- **Commit:** `feat(ui): sistema de diseno propio e identidad visual v-core`

### F3 — Producto: el chat como harness

- Mensajes densos, streaming real, razonamiento colapsable.
- **Tool cards ricas:** nombre, argumentos, duración, resultado, error.
- **Approvals inline** con el riesgo y la regla visible (el gate ya los emite).
- **Artefactos:** visor CodeMirror, diff unificado, tabs, descarga, persistidos
  (no en `localStorage`).
- **Cancelación real** del run.
- **Aceptación:** flujo completo en pantalla: mensaje → tool call → approval →
  artefacto → recarga → **todo sigue ahí**. Capturas de cada paso.
- **Commit:** `feat(chat): harness de codigo con tool cards y approvals`

### F4 — Sesiones legibles

- Sidebar con títulos reales (preview del primer mensaje), agrupación por fecha,
  búsqueda y paginación. **Se acaba la pared de `Sesión NNN`.**
- Estados honestos: "sin mensajes" vs "con N eventos".
- Archivado de las 49 sesiones basura (`sessions/_archive/`).
- **Aceptación:** captura del sidebar con títulos legibles y sin ruido.
- **Commit:** `feat(sesiones): sidebar legible con preview y agrupacion`

### F5 — Workspaces funcionales

Hoy `sessions/<id>/` es un perfil decorativo; `/sessions/{key}/workspace`
devuelve `work_files: []` y `artifacts: []`.

- Workspace real aislado por sesión (`work/` + `artifacts/`), con las tools
  resolviendo rutas relativas al workspace y el gate validando contra ese root.
- `git worktree` por sesión para aislamiento real y `git diff` gratis.
- UI: selector de workspace, árbol del workspace (**no del repo**), badge de
  cambios sin commitear, descarga de artefactos.
- **Aceptación:** dos sesiones trabajan el mismo repo sin pisarse; captura del
  árbol del workspace.
- **Commit:** `feat(workspace): workspace aislado por sesion con git worktree`

### F6 — El panel de sistema (la ingeniería, visible)

Es el bloque que convierte el repo en material de entrevista.

- Providers y **circuit breaker** en vivo (15 breakers, ya expuestos).
- **Tokens y costo** reales por modelo y sesión (`llm_usage_log`: 978 filas),
  sin estimaciones.
- **Trazas** por `task_id` (`traces.jsonl`).
- **Últimas decisiones del gate** con su regla y razón (`gate_log`: 1.685 filas).
- **Approvals pendientes** y su resolución.
- **Grafo de tareas** (87 grafos en `task_graphs`; hoy el motor más impresionante
  del repo **no se ve**).
- **Aceptación:** captura del panel con datos reales, no mocks.
- **Commit:** `feat(sistema): panel de observabilidad con datos reales`

### F7 — Cierre demostrable

- `Frontend/` viejo **archivado** (`docs/_archive/frontend_v1/`) y **eliminado el
  fallback dual** de `main.py:1714`.
- Suite E2E propia (Playwright) sobre el protocolo: streaming, tool call,
  artifact, approval, reload con replay fiel, cambio de modelo, cambio de
  workspace.
- README + diagrama de arquitectura + sección "cómo se ve en 30 segundos".
- Consolidar y archivar los 3 documentos del refactor fantasma.
- **Aceptación:** `Frontend/` ya no existe en la raíz; la suite E2E pasa;
  `web_dist/` es la única fuente servida.
- **Commit:** `chore(cierre): archivar frontend v1 y publicar suite e2e`

---

## 6. Tabla de estado (se actualiza al cerrar cada bloque)

| Bloque | Estado | Evidencia |
|---|---|---|
| F0 — Plan | ✅ Cerrado | `docs/PLAN_REFACTOR_V2.md` + `docs/COMPARATIVA_HARNESSES.md` (commit `2ac7d27` y siguiente) |
| F1 — Identidad de sesión | ⬜ Pendiente | — |
| F1.5 — Motor honesto | ⬜ Pendiente | — |
| F1.6 — Compactación | ⬜ Pendiente | — |
| F1.7 — Cancelación | ⬜ Pendiente | — |
| F1.8 — Permisos y estado visible | ⬜ Pendiente | Fricción medida: 24,1% de operaciones piden aprobación |
| F2 — Identidad visual | ⬜ Pendiente | — |
| F3 — Harness de chat | ⬜ Pendiente | — |
| F4 — Sesiones legibles | ⬜ Pendiente | — |
| F4.5 — Kernel Cordis | ⬜ Pendiente | — |
| F5 — Workspaces | ⬜ Pendiente | — |
| F6 — Panel de sistema | ⬜ Pendiente | — |
| F7 — Cierre | ⬜ Pendiente | — |

Leyenda: ⬜ pendiente · 🟡 en curso · ✅ cerrado con evidencia.

---

## 7. Riesgos y trampas conocidas

1. **El bug latente que se esconde.** El `glob` de `_resolve_session_dir`
   funciona hoy porque los directorios existen. Cualquier refactor que asuma que
   la relación id↔hilo es confiable **va a fallar en silencio**. Por eso F1 va
   primero.
2. **Confundir "compila" con "sirve".** El frontend actual tiene **0 errores de
   consola** y no sirve. Los tests verdes no son evidencia visual.
3. **Re-heredar el diseño sin darse cuenta.** Al tocar CSS es fácil volver a los
   tokens viejos porque están cómodos. F2 exige una captura comparativa.
4. **Verificación en navegador bajo el sandbox.** Playwright falla con
   `WinError 5` al crear sus named pipes. **Solución conocida:** ejecutarlo con
   `sandbox_permissions: danger-full-access` (ya validado hoy; es la única
   operación del proyecto que lo requiere).
5. **`retriever` apunta a Ollama caído.** `/system/model` → `ollama/nomic-embed-text`
   mientras Ollama reporta `ok:false`. El retrieval funciona por el tier NIM,
   pero la config miente.
6. **Backend viejo corriendo.** El proceso actual sirve `2.0.0` y monta
   `web_dist/`. Antes de medir cualquier cambio hay que confirmar que el proceso
   vivo corresponde al código nuevo.
7. **Un multiagente se auto-amplifica y agota el presupuesto.** Verificado en
   carne propia el 2026-09-23: 3 subagentes de investigación **se reprodujeron
   solos** (uno creó 4 hijos, otro 2), 7 agentes compitieron por un límite de 5
   búsquedas concurrentes, entraron en `RATE_LIMIT` en cascada y murieron los tres
   por `QUOTA: Insufficient Balance` **sin escribir un solo informe**. Claude Code
   resuelve esto con **profundidad máxima de 3 niveles y retirando la tool de
   delegación en el límite**; hay que copiar ese guard si F3/F6 agregan
   subagentes.
8. **Un worker que no escribe su resultado incrementalmente pierde todo al morir.**
   Corolario del punto 7: los subagentes hicieron 8–17 búsquedas cada uno y no
   dejaron nada recuperable porque su único entregable era el mensaje final. Todo
   worker debe **escribir a un archivo a medida que avanza**, no al final.
9. **Bloqueo de red por escalada no pedida.** Al inicio de la sesión el egress de
   shell estaba bloqueado (`curl exit=35`, `IWR` con error TLS). **No era un
   límite duro del entorno: era el sandbox esperando una escalada que no se
   pidió.** Con `danger-full-access` la red funciona. Antes de declarar un
   bloqueo, reintentar la operación exacta pidiendo la escalada.
10. **El gate interrumpe de más y explica de menos.** Medido: **24,1%** de las
    operaciones típicas piden aprobación, incluida `list_files(".")` — listar la
    raíz del proyecto. La causa está en que la allowlist de agente de Planner lista
    subdirectorios pero no la raíz, y `web/` no está en absoluto (el agente coder
    no puede escribir el frontend nuevo). **Corolario de diseño:** un gate que
    interrumpe sin explicar entrena al humano a aprobar sin leer, lo cual es peor
    que no tener gate. Ver F1.8.
11. **El silencio es indistinguible del bloqueo.** Si el agente no anuncia su
    estado, una UI en blanco puede ser "esperando aprobación, todo bien" o
    "colgado para siempre" y **no hay forma de saberlo** — ni a ojo ni con una
    sonda automatizada. Una captura de pantalla vacía no es evidencia de nada.
    Por eso F1.8 exige que **todo** estado de espera declare qué espera, por qué y
    qué lo desbloquea. Es lo que convierte la auditoría visual en algo que se
    puede afirmar en vez de interpretar.

---

## 8. Protocolo de trabajo

1. Se presenta el bloque, el PM dice "vamos", se ejecuta, se verifica, se
   commitea, y **se actualiza §6 antes de pasar al siguiente**.
2. Toda afirmación de "funciona" va con su evidencia (comando, captura o test).
3. Si algo falla o no se sabe, se dice. Sin adornos.
4. Los archivos inmunes siguen siendo inmunes: `gate.py` (sin council mode),
   `.env`, `llm_client.py`, `model_routing.yaml`.
5. No se hace `git push` sin orden explícita.

---

## 9. Notas de honestidad

- **Verificado por mí hoy:** esquema de `sessions` y `agent_events`, los 404 de
  `/threads` y `/policy/stats`, el `/system/model` completo (8 roles), el
  `/embeddings/status`, la ausencia de `session_dir` en la tabla, el `glob` de
  `_resolve_session_dir`, la captura del navegador y los conteos del sidebar.
- **Ejecutado por mí hoy:** `test_gate_v2` (18/18), `test_gate_wiring` (0
  fallas), `test_event_protocol` (56 eventos), `test_fs_policy`,
  `test_policy_commands`. `test_visual_audit` **falla por el sandbox**
  (`WinError 5`), no por el código.
- **Tomado del trabajo previo, no re-verificado por mí en esta sesión:** los
  conteos de filas antiguas (`gate_log` 1.685, `llm_usage_log` 978,
  `task_graphs` 87) provienen de `ANALISIS_REFACTOR_FRONTEND.md`; los de
  `agent_events` (64 eventos) y los del gate los verifiqué yo.
- **Corregido en este documento:** un diagnóstico intermedio mío atribuyó el fallo
  del dibujado de sesiones al cliente (`id` numérico vs `session_dir`). **Era
  falso**: el cliente lo hace bien. La causa está en el backend (§2.3).
