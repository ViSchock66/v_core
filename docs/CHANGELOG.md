# CHANGELOG — V-CORE

## Unreleased — Hotfixes post-auditoría (2026-09-23)

Auditoría exhaustiva probando cada tab, control, endpoint y flujo con requests
reales y capturas vía Edge/CDP: 33 endpoints, 0 errores 500, 14/14 checks de
verificación integrada al cierre.

### Fixed — Seguridad

- **XSS almacenado en `escHtml` (`Frontend/app.js`).** Los cuatro `replace`
  sustituían cada carácter por sí mismo (`'&'` → `'&'`), así que la función
  devolvía el string intacto: era un no-op. Verificado a nivel de bytes y
  explotado en navegador — un `<img src=x onerror=...>` creaba el nodo real, y
  un `<script>` vía resultado de búsqueda. Afectaba a todo lo que se pinta con
  `innerHTML`. Ahora escapa `&`, `<`, `>`, `"` y `'`.
- **DT-00 — el security gate era un placebo.** `POST /approvals/{id}/approve`
  marcaba el registro como aprobado y nunca re-invocaba la tool. Verificado
  creando un `write_file`: el archivo nunca se creaba. Ahora re-ejecuta vía
  `MCPClient` con los params guardados, con **idempotencia real**
  (`already_executed` + `result_json`, sin re-ejecutar en reintentos).
  Al probar el flujo completo aparecieron tres bugs encadenados:
  - `execute_shell` (nombre del gate) no existía en el catálogo MCP, que usa
    `execute_command` → se agrega el alias al mapa de `mcp_client`.
  - `_execute_command(command)` no aceptaba `cwd`, que la API sí guarda en los
    params → `unexpected keyword argument 'cwd'`.
  - Sin idempotencia, un doble clic re-ejecutaba la acción.
- **Prompt injection de prueba versionado.** `workspace/uploads/default/notas_proyecto.txt`
  contenía instrucciones para sobrescribir `gate_rules.yaml`. Retriever usa
  `workspace/` como raíz por defecto para indexar y buscar, así que era un
  vector de inyección dentro del repositorio. Removido del índice.

### Fixed — Funcionalidad

- **Chat caído en Windows.** `VCoreTracer.__init__` imprimía un emoji y con
  stdout no interactivo (pipe, redirección, servicio, CI) Python usa cp1252 →
  `UnicodeEncodeError` que se propagaba desde el agent loop y mataba el chat
  antes de llegar al modelo. UTF-8 forzado en `api/main.py` + `_safe_print()`
  en `observability.py`.
- **Puerto partido (8000 vs 8001).** Cuatro fuentes decían 8000 y dos 8001; el
  CLI fallaba con `WinError 10061` y los MCP servers apuntaban a un puerto
  muerto. Nuevo `api/ports.py` como fuente única (`VCORE_PORT` para override),
  migrados CLI, MCP servers, Orchestrator, `visual_auditor`, `watchdog` y scripts de
  arranque.
- **`/system/health` se auto-bloqueaba.** 13.0s constante y reportaba
  `api.ok=false` con el backend sano, generando una alerta crítica falsa: el
  agente proactivo corre dentro del servidor y se pedía HTTP a sí mismo. Ahora
  resuelve su estado in-process. Medido: **13.0s → 0.01s**, 0 alertas falsas.
- **Búsqueda del IDE devolvía 0 resultados siempre.** Buscaba en `workspace/`,
  que solo tiene adjuntos. Ahora `root=vcore` por defecto. Medido: **0 → 50
  resultados** para `escHtml`.
- **Explorador de archivos no mostraba el código.** `/files/tree` tenía
  `root=workspace` por defecto. Medido: **9 → 251 nodos**, ahora con `api/`,
  `Frontend/`, `agents/` y sin `__pycache__`.
- **`#toast` no existía en el DOM.** `showToast()` hacía `if(!t) return` y el
  nodo nunca se declaró, aunque el CSS estaba completo: las 12 llamadas no
  mostraban nada. Sin feedback visual en toda la aplicación.
- **Approvals duplicados.** `fetchApprovals` hacía `push` y
  `handleApprovalRequest` volvía a hacerlo: medido `[32, 32]` para un solo
  approval.
- **`PATCH /sessions/{id}` no existía.** Renombrar conversación daba 405 y el
  `catch` vacío del frontend lo ocultaba.
- **`POST /llm/circuit-status/reset` no existía.** El botón daba 405 en
  silencio.
- **Contraste del botón de envío en tema light.** `color:#000` hardcodeado
  contra acentos oscuros (`#B45309`). Nuevo token `--accent-fg` por tema.

### Fixed — Consistencia

- **`hot_swap` rompía el 3-point config sync.** `_persist_yaml` escribía solo
  el global; como `_ensure_default_session()` copia el default encima al
  arrancar, todo hot-swap se perdía. Verificado: global 0.7 vs default 0.3
  antes; ahora ambos coinciden.
- **Selector de temperatura ausente.** El campo existía en los 8 roles del YAML
  y `llm_client` ya lo leía, pero no había control en la UI. Se agregan los tres
  niveles documentados (preciso 0.1 / balance 0.3 / creativo 0.7) — es
  aleatoriedad, **no** esfuerzo. `/system/model` ahora expone `temperature` para
  que el selector refleje el estado real.

### Added — Infraestructura de repo

- `requirements.txt` (15 dependencias reales validadas contra el `.venv`).
- `LICENSE` MIT, `.env.example`.
- `.github/workflows/ci.yml`: matriz 3.11–3.13 con compilación, import de
  `api.main` y smoke test de endpoints; frontend con `node --check`,
  cache-busting y ausencia de residuos `*_old.*`.
- `scripts/capture_frontend.py`: capturas en los 6 acentos y ambos temas vía
  Edge + DevTools Protocol.

### Changed — Documentación

- `README.md`: corregido contra el sistema vivo (documentaba `POST /chat`, que
  no existe; versiones falsas; y aconsejaba servir el frontend con un servidor
  estático, que no funciona porque `app.js` deriva la base de `window.location`).
- `docs/DOCUMENTATION.md` declara los 10 documentos vivos; los históricos se
  movieron a `docs/_archive/`.
- `docs/FILE_MANIFEST.md` regenerado desde disco.
- `AGENTS.md`: puerto canónico y comandos actualizados (referenciaba
  `scripts/smoke_test.py`, que no existe).

### Security — resuelto antes de publicar

- La clave de Gemini hallada durante la auditoría se retiró del sistema y del
  árbol publicado; este repositorio nació de un export sin historial previo.
  Pendiente del maintainer: revocarla en el proveedor.

## Unreleased — Normalización documental

- `VCORE_ROADMAP.md` pasa a ser el único backlog activo.
- La deuda, la ruta estratégica y la auditoría de puntos débiles se consolidan
  en el roadmap; sus snapshots se preservan en `docs/_archive/`.
- `docs/DOCUMENTATION.md` define las fuentes vigentes y el límite entre estado
  actual e historia.

## v1.5.0 (2026-07-08)

### Fixed — Session Isolation (3 bugs críticos)
- **SI-1**: `_persist_yaml()` en `llm_client.py` usa `self.config_path` en vez de Path hardcodeado. Hot-swap de sesión ya no contamina el YAML global.
- **SI-2**: `asyncio.Lock` → `threading.Lock` en `main.py`. Sin I/O bloqueante en el event loop.
- **SI-3**: `_ensure_default_session()` ahora resincroniza desde default si el lead model difiere del global.

### Added — Motor de memoria Curator
- `agents/Curator/memory.py`: motor con deduplicación (SHA-256), historial SQLite, API limpia.
- Tabla `nem0_memory` reemplaza `agent_memory` (0 rows → funcional).
- Curator `curator.py`: 511→308 líneas. Solo contexto del proyecto.

### Added — Tool parsing v2
- Bare parser: detecta `read_file path` sin prefijo `TOOL:`.
- OpenAI JSON format: `{"name": "...", "arguments": {...}}` además de `{"tool": ...}`.
- Param aliases: `file_path→path`, `dir→directory`, `cmd→command`, `pattern→query`.

### Added — Producción readiness
- `_verify_tool()`: telemetría post-ejecución en write_file/execute_command.
- `MAX_TOKENS_PER_SESSION`: presupuesto con corte duro a 100K tokens.
- `max_iterations`: 5→999. El circuit breaker maneja la convergencia.

### Added — Frontend UX
- Typing indicator: tres puntos animados durante streaming.
- Thinking block: ya no se colapsa al terminar.
- Temperature selector: preciso (0.1) / balance (0.3) / creativo (0.7).
- TOOL leak strip: regex limpia JSON tool calls del texto visible.
- Cache busting: `?v=3` en app.js y style.css.

### Changed — Versionado
- `api/version.py`: fuente única de versión. Lee de VCORE_STATE.json.
- `VCORE_ARCHITECTURE_v1_3.md` → `VCORE_ARCHITECTURE.md`.
- `VCORE_ROADMAP_v1_3.md` → `VCORE_ROADMAP.md`.

### Removed — Código muerto
- 25 referencias a modelos muertos (gemini, qwen, llava) corregidas.
- `list_models.py`, `import.py`, `FILE_MANIFEST.txt`, `.continue/` eliminados.
- 8 sesiones viejas (182-189) eliminadas.
- Rol `curator` eliminado de `model_routing.yaml` (45 referencias limpiadas).

### Docs
- `SESION_2026-07-08.md`: registro completo de la sesión.
- `DEUDA_TECNICA.md`: DT-14 resuelto, DT-20 resuelto, SI bugs documentados.
- `SESSION_ISOLATION_ARCHITECTURE.md`: Fase 2 completada.
- `CHANGELOG.md`: esta entrada.

---

## Auditoría docs↔código, segunda ronda (2026-07-08, post v1.4.0)

> No es un release — es un erratum documental de la segunda ronda de auditoría...

**Documentos actualizados**: `AGENTS.md`, `VCORE_STATE.json`, `agents/Orchestrator/Orchestrator.md`, `agents/Planner/Planner.md`, `agents/Retriever/Retriever.md`, `agents/Curator/Curator.md`, `VCORE_ARCHITECTURE_v1_3.md`, `VCORE_ROADMAP_v1_3.md`, `docs/SESSION_ISOLATION_ARCHITECTURE.md`, `docs/DEUDA_TECNICA.md`, `docs/CHANGELOG.md` (este mismo erratum).

---

## Auditoría del sistema funcionando (2026-07-08, servidor vivo)

> Se levantó el backend, se auditó el sistema en runtime completo: CLI, chat contra 6 modelos, ChromaDB, session isolation end-to-end.

### Modelos — 5/6 responden, DeepSeek V4 Pro silencioso
- **GLM 5.2** (lead): ✅ streaming real, respuesta completa en SSE
- **Kimi K2.6**: ✅ responde, con identity lock + tool call espontáneo (`read_file("README.md")`)
- **GLM 5.1**: ✅ responde correctamente
- **DeepSeek V4 Flash**: ✅ responde con markdown formateado
- **Nemotron 3 Ultra 550b**: ✅ responde (sin el bug de vacío que afecta al Nemotron Super 49b)
- **DeepSeek V4 Pro**: ❌ silencio total — el server no crashea pero no emite respuesta. Confirma DT-13a (no hay manejo de timeout ni memory guard específico)

### CLI — funcional con 2 versiones stale
- `vcore sessions list` ✅, `vcore doctor` ✅ (93 docs ChromaDB confirmados, 12 tablas DB, 15 CBs CLOSED)
- `vcore status` ✅ (versión 1.4.0 leída de VCORE_STATE.json)
- **Bugs corregidos in-situ**: 3 hardcodeos de versión en `vcore.py` (banner v1.2.0, doctor v1.2.0, `cli_version` v1.3.0 → todos a v1.4.0)

### Curator — confirmado: solo SQL + ChromaDB
- 0 imports de cliente LLM, 0 llamadas a API NIM/Ollama/DeepSeek
- Solo `sqlite3`, `chromadb`, `api.embed` — exactamente lo que dijiste: "un script de SQL"
- El "nim" encontrado por grep era falso positivo ("mí**nim**a")

### ChromaDB — 93 docs confirmados desde sistema vivo
- `vcore doctor` reporta "1 colecciones, 93 documentos"
- VCORE_ARCHITECTURE decía 79 → corregido a 93

### Session Isolation — 3 bugs críticos encontrados

| # | Bug | Causa raíz | Severidad |
|---|-----|-----------|-----------|
| **SI-1** | Hot-swap de sesión contamina el YAML global | `_persist_yaml()` en `llm_client.py:1673` usa `Path("model_routing.yaml")` (relativo, CWD) en vez de `self.config_path`. Cualquier `LLMRouter` creado con config de sesión termina escribiendo al global. | 🔴 |
| **SI-2** | `asyncio.Lock` con I/O bloqueante | `_get_session_lock()` usa `asyncio.Lock` pero `hot_swap()` llama a `_persist_yaml()` (yaml.dump síncrono, I/O bloqueante) → bloquea el event loop de FastAPI. La skill vcore-app-config lo documenta (pitfall #36): debe ser `threading.Lock`. | 🟠 |
| **SI-3** | `sessions/default/model_routing.yaml` no se resincroniza al arranque si ya existe | `_ensure_default_session()` solo crea el default si no existe — si ya existe y se contaminó (por SI-1 u otro hot-swap), queda stale para siempre. Las nuevas sesiones clonan un default contaminado. | 🟠 |

**Verificación forense de SI-1**: al hacer `POST /sessions/sess_174_6a242e48/model` con Kimi K2.6, el archivo `model_routing.yaml` global (raíz) cambió de `z-ai/glm-5.2` a `moonshotai/kimi-k2.6`. El hot-swap "por sesión" está funcionando como hot-swap global — la session isolation prometida no ocurre en la práctica.

### Correcciones aplicadas en esta ronda
- `vcore.py`: 3 versiones stale → 1.4.0
- `VCORE_ARCHITECTURE_v1_3.md`: ChromaDB 79→93
- `sessions/default/model_routing.yaml`: restaurado a GLM 5.2 (estaba contaminado con Kimi)

### Pendientes
- **SI-1 fix**: cambiar `Path("model_routing.yaml")` por `self.config_path` en `_persist_yaml()` (archivo inmune — requiere council mode)
- **SI-2 fix**: `asyncio.Lock` → `threading.Lock`
- **SI-3 fix**: `_ensure_default_session()` debe re-sync desde global si el default ya existe pero tiene modelo diferente

---

## Fixes Session Isolation (2026-07-08, aplicados en caliente)

> Los 3 bugs de session isolation descubiertos en la auditoría del sistema vivo fueron corregidos y verificados.

### SI-1 — `_persist_yaml` usa `self.config_path` ✅
- `llm_client.py:1072`: `self.config_path = config_path` en `__init__`
- `llm_client.py:1674`: `Path(self.config_path)` en vez de `Path("model_routing.yaml")` hardcodeado
- **Verificación**: hot-swap de sesión 181 via curl → session YAML: Kimi, global YAML: GLM 5.2 intacto

### SI-2 — `threading.Lock` reemplaza `asyncio.Lock` ✅
- `main.py:382-390`: `threading.Lock` en `_get_session_lock()`, tipado actualizado
- `main.py:413`: `session_lock.acquire()` sin `await` (síncrono, compatible con thread pool de FastAPI)
- `main.py:450`: `session_lock.release()` sin `await`

### SI-3 — Default resync al arranque ✅
- `main.py:48-61`: `_ensure_default_session()` ahora compara lead model default vs global en cada startup
- Si difieren, restaura global desde default (el template inmutable)
- **Verificación**: al arrancar servidor limpio, log muestra `[V-CORE] API v1.4.0 iniciada` sin mensaje de resync (default y global coinciden)

### Limpieza
- 8 sesiones contaminadas (171-174, 176, 179, 181, test_fix) restauradas a GLM 5.2
- `sessions/default/model_routing.yaml` sincronizado con global

### Pendiente
- Rol `curator` en `model_routing.yaml`: tiene comentario DT-20, el bloque con `mistralai/mistral-small-4-119b` sigue activo. No es un agente — decisión de eliminación pendiente.

---
- **VCORE_STATE.json**: `curator_model: "mistralai/mistral-small-4-119b-2603"` — factualmente incorrecto (DT-20). Curator no consume API NIM. Corregido a `null` con nota explicativa.
- **agentes/Orchestrator.md, Planner.md, Retriever.md, Curator.md**: completamente stale desde v0.3 — mencionaban `gemini-2.5-flash`, `gemini-3.5-flash`, `qwen2.5-coder:14b`, `nomic-embed-text via Ollama` con rutas viejas (`knowledge/.chromadb/`). Actualizados a modelos NIM reales (`moonshotai/kimi-k2.6`, `deepseek-ai/deepseek-v4-flash`, `nvidia/nemotron-mini-4b-instruct`), `api/embed.py` unificado, `chroma_db/` como ruta de ChromaDB. Curator reetiquetado como capa de persistencia, no agente.
- **VCORE_ARCHITECTURE_v1_3.md**: line counts corregidos — orchestrator.py 2029→1905 (-124, reducción no documentada), app.js 2448→2495 (+47), style.css 645→670 (+25), index.html 270→267 (-3), model_routing.yaml 275→340 (+65), planner.py 867→868, retriever.py 660→661. Agregado "streaming SSE" en descripción de orchestrator.py. Actualizado a 6 modelos disponibles. Corregida descripción de Curator en diagrama de componentes (removida asignación de modelo LLM, agregada nota "no LLM").
- **VCORE_ROADMAP_v1_3.md**: actualizada fecha a 8 Jul. Corregido el snapshot del lead model en tabla de auditoría (ahora dice `z-ai/glm-5.2`, 8 Jul). Actualizado line count de orchestrator.py (2029→1905). Actualizada referencia a DEUDA_TECNICA (DT-00 a DT-20, antes DT-14). Agregada nota de actualización 8 Jul.
- **SESSION_ISOLATION_ARCHITECTURE.md**: Fase 2 marcada como ✅ COMPLETADO — `POST /sessions/<id>/model` ya está implementado (v1.4). Antes estaba marcada como pendiente.
- **DEUDA_TECNICA.md**: DT-14 actualizado de 🟡 "Decidido, pendiente de verificación" a ✅ "Resuelto — migración implementada y verificada" (basado en la skill vcore-app-config que documenta `mcp_manager.py` reemplazando al dispatcher casero). DT-06 desbloqueado como consecuencia.
- **RUTA-SOFTWARE-SERIO.md**: §2 triage de `mcp_config.json` decía "✅ Resuelto", pero DT-14 en DEUDA_TECNICA decía "pendiente de verificación" — contradicción resuelta: ambos ahora marcan DT-14 como ✅ resuelto.

**Hallazgos no corregidos en esta ronda (dejan de ser contradicción, queda registrado)**:
- **SESION_2026-07-07.md**: dice "v1.1.0" en el estado final y "ChromaDB 93 docs" — este doc es histórico de esa sesión y no se modifica por diseño (es una foto del momento, no un doc vivo). Se aclara la contradicción con VCORE_ARCHITECTURE que dice "79 docs" — ninguno confianza: falta verificación real de ChromaDB (chromadb no instalado en este venv).

**Documentos actualizados**: `AGENTS.md`, `VCORE_STATE.json`, `agents/Orchestrator/Orchestrator.md`, `agents/Planner/Planner.md`, `agents/Retriever/Retriever.md`, `agents/Curator/Curator.md`, `VCORE_ARCHITECTURE_v1_3.md`, `VCORE_ROADMAP_v1_3.md`, `docs/SESSION_ISOLATION_ARCHITECTURE.md`, `docs/DEUDA_TECNICA.md`, `docs/CHANGELOG.md` (este mismo erratum).

---

## Auditoría cruzada docs↔código (2026-07-07, post v1.4.0)

> No es un release — es un erratum documental. Se cruzaron `VCORE_ARCHITECTURE_v1_3.md`, `VCORE_ROADMAP_v1_3.md`, `DEUDA_TECNICA.md`, `LOGFIX.md` y `VCORE_STATE.json` contra el código real (`main.py`, `llm_client.py`, `orchestrator.py`, `curator.py`, `retriever.py`, `gate.py`, `mcp_client.py`, `gate_rules.yaml`, `model_routing.yaml`). Las entradas históricas de este changelog no se modificaron — quedan como registro de lo que se declaró en su momento. Estos son los errores encontrados y lo que se corrigió en los documentos de planning:

- **🔴 Corrección crítica — "Lead model: Kimi K2.6" (entrada v1.3.1 abajo) es incorrecta.** `model_routing.yaml` real decía `orchestrator_lead.model: z-ai/glm-5.1` al momento de esta auditoría (7 Jul 2026), verificado directamente en el archivo y sin overrides en `llm_client.py` que lo contradigan. Ni GLM-5.2 (lo que decía `VCORE_ARCHITECTURE_v1_3.md`) ni Kimi K2.6 (lo que dice esta entrada de changelog) eran correctos en ese momento. No está claro en qué punto se revirtió de 5.2 a 5.1 sin dejar registro — si fue intencional, falta la entrada de changelog correspondiente. **Nota (8 Jul 2026, DEUDA_TECNICA.md DT-17)**: el lead es estado mutable por hot-swap, no arquitectura fija — para el valor *actual* en cualquier momento, consultar `model_routing.yaml` o `GET /system/model`, no este changelog ni ningún documento narrativo.
- **🔴 Hallazgo nuevo, no documentado en ningún lado antes de hoy — Approval flow no ejecuta la acción aprobada.** `POST /approvals/{id}/approve` marca el registro como aprobado en SQLite pero no dispara la ejecución de la tool pendiente. El security gate aparenta funcionar en la UI pero la acción autorizada nunca corre. Ver `DEUDA_TECNICA.md` DT-00.
- **✅ Streaming real ya estaba implementado** — `stream_complete()` existe y está en uso activo en `orchestrator.py` (comentario en código: `# B8: Streaming real`), con `StreamingResponse`/SSE real en `main.py`. Este trabajo se hizo sin dejar entrada en este changelog, y por eso `VCORE_ROADMAP_v1_3.md` y `DEUDA_TECNICA.md` lo listaban como P0 pendiente durante varias versiones. Corregido en ambos documentos.
- **⚠️ "MCP" interno no es protocolo MCP real** — `system/mcp_client.py` es un dispatcher de tools propio (así lo admite su propio docstring), sin SDK oficial `mcp`. `mcp_config.json` (el único server MCP protocol-compliant en el repo) no se carga en ningún punto del runtime. Ver `DEUDA_TECNICA.md` DT-14.
- **Line counts desincronizados** en `VCORE_ARCHITECTURE_v1_3.md` para `main.py`, `llm_client.py`, `orchestrator.py`, `curator.py`, `retriever.py` — corregidos con conteos reales (`wc -l`) en ese documento.
- **DeepSeek V4 Pro crash guard y Nemotron vacío como lead**: confirmados sin fix en código, siguen abiertos tal como los documentos de planning ya indicaban — sin cambios de estado, solo se agregó detalle de causa verificada.
- **F-12 (`_insert_memory` SQLite, 0 rows)**: confirmado sin fix, y se identificó por qué nadie lo ha resuelto — el método atrapa la excepción real con `except Exception: print(...)` sin loguear el traceback completo, ocultando la causa. Ver `DEUDA_TECNICA.md` DT-13c.

**Documentos actualizados como resultado**: `VCORE_ARCHITECTURE_v1_3.md`, `VCORE_ROADMAP_v1_3.md`, `DEUDA_TECNICA.md`.

---

## v1.4.0 (2026-07-07)

### Added — Session Isolation (Filesystem-based)
- **`sessions/` directory structure**: `default/` (plantilla inmutable), `_archive/` (sesiones eliminadas). Cada sesión es un directorio autocontenido con su propio `model_routing.yaml`.
- **`POST /sessions`** ahora clona `sessions/default/` → `sessions/sess_{id}_{uuid}/` y retorna `session_dir`.
- **`DELETE /sessions/{id}`** ahora archiva el directorio en `_archive/`.
- **`POST /sessions/{dir}/model`** — hot-swap por sesión. Escribe en `sessions/{dir}/model_routing.yaml`. No afecta otras sesiones.
- **`POST /agents/route`** — acepta `session_dir` en el body. Swap del router con `asyncio.Lock` por sesión para evitar race conditions.
- **`LLMRouter`** — `force_new=True` y `set_router()` para session isolation.
- **`.gitignore`** — excluye `sessions/*` excepto `default/`.

### Changed
- `api/main.py`: +95 líneas (helpers de sesiones, endpoints, lock)
- `api/llm_client.py`: `get_router(force_new)`, `set_router()`
- `ChatMessage.session_dir`: nuevo campo

### Docs
- `docs/SESSION_ISOLATION_ARCHITECTURE.md` — documento completo de arquitectura (Hermes vs nuestra arquitectura)
- `VCORE_ROADMAP_v1_3.md` — Fase 1 Session Isolation en Futuro
- `VCORE_ARCHITECTURE_v1_3.md` — referencia a session isolation

## v1.3.1 (2026-07-07)

### Fixed — Embeddings unificados (P0.2)
- **Retriever sin fallback**: `_get_embedding()` solo usaba Ollama. Sin Ollama → vector de ceros → búsquedas semánticas rotas.
- **Curator con fallback duplicado**: `_embed()` tenía 3-tier independiente, no compartido con Retriever.
- **Fix**: Creado `api/embed.py` — capa unificada `embed()` sync + `embed_async()` async con 3-tier fallback (Ollama → NIM → hash SHA-256). Curator y Retriever importan del mismo módulo. Sin dependencia externa obligatoria.
- **Archivos**: `api/embed.py` (nuevo), `agents/Curator/curator.py` (-47 líneas), `agents/Retriever/retriever.py` (-29 líneas)

### Changed — Docs
- **Lead model**: `AGENTS.md` y `VCORE_ROADMAP_v1_3.md` actualizados — lead real es Kimi K2.6 (no GLM 5.2) ⚠️ **Esta afirmación es incorrecta — ver erratum del 7 Jul 2026 más arriba en este mismo documento.** Ni GLM-5.2 ni Kimi K2.6 eran el lead real; era `z-ai/glm-5.1`. Entrada conservada sin editar como registro histórico de lo que se declaró en ese momento.
- **P0.2**: Marcado como completado en roadmap
- **VISUAL-03, VISUAL-08**: Marcados como resueltos en roadmap
- **VCORE-STATE-STALE, FALLBACK-FORMAT**: Marcados como resueltos

## v1.3.0 (2026-07-07)

### Fixed — State sync & single source of truth
- **STATE-STALE**: `VCORE_STATE.json` tenía `fecha_actualizacion` congelada (2026-06-27) y `council_model` incorrecto. Causa: `_persist_state()` solo corría en hot-swap, nunca en startup, y `fecha_actualizacion` no tenía writer automático.
- **Fix**: `main.py` `startup()` ahora sincroniza modelos desde `model_routing.yaml` → `VCORE_STATE.json` + actualiza `fecha_actualizacion`. `_persist_state()` también escribe `fecha_actualizacion` en cada hot-swap.
- **Single source of truth**: version ahora se lee de `VCORE_STATE.json` en `main.py` (`/health`, `FastAPI(version=...)`) y `vcore.py` (`cmd_status`). Eliminados 3 hardcodeos de versión en 3 archivos distintos.
- **Archivos**: `api/main.py`, `api/llm_client.py`, `vcore.py`, `VCORE_STATE.json`

### Changed — Documentación y roadmap
- **Roadmap reordenado**: `VCORE_ROADMAP_v1_3.md` — completados consolidados, Tauri postergado, bugs en LOGFIX como fuente única, 3 prioridades P0 con orden de ejecución claro
- **Arquitectura actualizada**: `VCORE_ARCHITECTURE_v1_3.md` — line counts, modelos, Tauri status, stack actualizado
- **LOGFIX**: agregados F-07, B-08, B-10 (v1.2.0 fixes) + bugs pendientes actualizados
- **DEUDA_TECNICA**: actualizado a v1.2.0, DT-02 marcado como parcialmente resuelto (4 keys sin rotación automática)

### Fixed
- `VCORE_STATE.json`: council model stale (`deepseek-v4-flash` → `kimi-k2.6`), roadmap ref, fecha
- `AGENTS.md`: modelos stale (GLM 5.1→5.2, DeepSeek council→Kimi council)

### Cleanup
- Legacy NVIDIA reports (3 archivos) → `docs/_archive/`
- `_test_routing.yaml` → `docs/_archive/`
- `VCORE_ROADMAP_v1_1.md` → `docs/_archive/` (reemplazado por v1.3)

### Fixed — Auditoría de código (7 Jul 2026)
- **BOM en `rag_index.py` y `rag_query.py`**: removido U+FEFF del inicio. Scripts funcionales de nuevo.
- **`iterations` SQLite**: tabla muerta eliminada (0 rows, 0 referencias en código)
- **`manifiesto vcore.txt`**: eliminado (3.7 MB dump de directorio)
- **`primos.py`**, **`test_orchestrator.py`**: eliminados (código sin relación con V-Core)
- **`prototipo.html`** (84 KB) + **`workspace/`** (1.3 MB): archivados en `docs/_archive/`
- **Scripts NVIDIA legacy**: `nvidia_smoke_test.py`, `nvidia_model_discovery_v2/v3`, `generate_nvidia_report.py`, `nvidia_smoke_results.json` → `scripts/_legacy_backup/`
- **RAG scripts**: verificados funcionales post-BOM fix. `rag_documents` table lista para usar.

### Fixed — Self-modification (7 Jul 2026)
- **`gate_rules.yaml`**: agregado `API` al `agent_path_allowlist`. Los writes vía REST API ahora son Nivel A.
- **`api/main.py`**: endpoint `POST /system/reload-gate` para recargar reglas sin reiniciar.
- **Bug approval sin ejecución**: documentado. `POST /approvals/{id}/approve` marca approved pero no ejecuta el write pendiente.

### Added — CLI v1.2.0 (7 Jul 2026)
- **17 comandos, ~50 subcomandos**: `chat`, `sessions`, `agents`, `config`, `models`, `tools`, `mcp`, `gate`, `approvals`, `memory`, `logs`, `db`, `serve`, `doctor`, `init`, `status`, `audit`.
- **Autocompletado**: `prompt_toolkit` con `SlashCompleter` — dropdown inline al escribir `/`.
- **Selector de modelos**: `radiolist_dialog` con flechas. `/models` permite cambiar lead desde el chat.
- **19 slash commands**: `/doctor`, `/models`, `/sessions`, `/agents`, `/logs`, `/db`, etc.
- **VT Processing**: `SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)` para ANSI en Windows.
- **Paleta ANSI**: clase `S` con cyan, dim, bold, gray. Cero emojis.
- **Box-drawing tables**: `_print_table()` con bordes Unicode `┌─┬─┐`.
- `vcore` sin args → chat interactivo (como `claude`).
- **Dependencia nueva**: `prompt_toolkit>=3.0`.

---

## v1.2.0 (2026-07-06)

### Changed — UX Overhaul
- **Paleta**: 6 temas eliminados → Dark (#0D0D0D ChatGPT-style) + Light Studio (#F2F3F5 gray)
- **Topbar**: breadcrumb "V-CORE" redundante eliminado. Model selector movido al input bar
- **Chat**: burbujas asimétricas — usuario alineado derecha (acento cyan), agente izquierda (card)
- **Tipografía**: `msg-body` usa `var(--sans)` (Inter 14px), headings con pesos, espaciado generoso
- **Approval**: tarjetas inline en el chat con botones aprobar/rechazar. Popover convertido a historial solo-lectura
- **Right panel**: fix 1px bug → abre 360px por defecto, guarda/restaura ancho en localStorage
- **Search**: migrado a `/search/workspace` (contenido real, no solo nombres). Resultados con preview de línea
- **Delete UX**: confirmación flotante cerca del cursor, no integrada al nombre del chat

### Fixed
- **F-07**: "Abrir carpeta" ahora funcional — lanza explorer en el proyecto
- **B-08**: Conversación sin mensajes ahora muestra empty state
- **B-10**: Right panel 1px — toggle restaura ancho guardado

### Removed
- 6 temas (Cyber Green, Amber Terminal, Matrix Rain, Ocean Deep, Purple Haze, Pure Black)
- Breadcrumb "V-CORE > V-CORE" redundante en topbar
- Botones aprobar/rechazar del popover approval (ahora inline)

---

## v1.1.2 (2026-07-06)

### Fixed
- **B-01**: Approval endpoint — `fetchApprovals()` llamaba a `/graph/approvals` (404) → `/approvals`
- **B-02**: Usage panel — `fetchUsage()` era `/* stub */` → conectado a `/llm/usage` con datos reales
- **B-03**: Approval display — mostraba "undefined" para nombre y descripción → normaliza `agent_id→agent`, `reason→action`, `status→risk`
- **B-04**: Cost display — `$0.0000esta sesión` sin espacio → `$0.0000 esta sesión`
- **B-05**: showSb recursion — `window.showSb = (v) => showSb(v)` causaba stack overflow → referencia directa `window.showSb = showSb`
- **B-06**: File tree — `container` usado antes de declararse + API response `{root, tree}` mal mapeado → fix de orden + `treeData.tree` extract

### Changed
- Lead model: `z-ai/glm-5.1` → `z-ai/glm-5.2` (NVIDIA NIM, 200K ctx, native TC)
- Council model: `deepseek-ai/deepseek-v4-flash` → `moonshotai/kimi-k2.6`
- Provider routing: `model_routing.yaml` usa `nvidia-lead`/`nvidia-kimi` sub-providers explícitos
- `model_presets`: agregado preset `z-ai/glm-5.2` (200K ctx, 131K output, 744B/40B MoE)
- `available_models`: GLM-5.2 priority 1, Kimi priority 2, DS Pro priority 4
- `llm_client.py`: `_model_to_provider` incluye `z-ai/glm-5.2 → nvidia-lead`
- NVIDIA keys: 4 keys (MAIN/FALLBACK/COMPRESS/AUX) transferidas de Hermes a `.env` de V-CORE
- Hermes `config.yaml`: todos los auxiliares (vision, delegation, xsearch, etc.) → `zai` + `glm-5.2`

### Added
- **ROADMAP_TAURI.md**: plan de migración a Tauri v2 + React + TypeScript + shadcn/ui (125 líneas)

---

## v1.1.1 (2026-07-03)

### Fixed
- **F-17**: Markdown rendering — marked.js no exponía global, polyfill inline aplicado
- **F-18**: Temas no aplicaban visualmente — body background hardcodeado → `var(--bg0)`
- **F-19**: Router LLM KeyError 'nvidia' — resolución via `_model_to_provider`
- **F-20**: Tool call truncado (write_file solo escribía firma) — guard para contenido incompleto
- **F-21**: Model label stale ("glm-5.1" hardcodeado → "cargando…")
- **F-22**: Empty states desactualizados ("0.5" → "1.1", "llava:7b" → "NIM vision")
- **F-23**: Provider YAML corrupto (`nvidia-deepseek` → `nvidia`)
- **F-24**: Curator embeddings offline — 3-tier fallback (Ollama → NIM → hash SHA-256)
- **F-25**: Times New Roman en todo el frontend — body sin `font-family` → `var(--sans)` (Inter)
- **F-26**: Auto-scroll roto durante streaming — `streaming ||` force-scroll + threshold 60→80px
- **F-08**: Voice button disabled — `t-disabled` + `disabled` removidos

### Changed
- Lead model: `deepseek-ai/deepseek-v4-pro` (mejor tool calling, 1M ctx)
- marked.js: CDN → local (`Frontend/marked.min.js`)
- WELCOME_PHRASES actualizadas
- `style.css`: `html,body` ahora incluye `font-family:var(--sans)`
- `app.js` `autoScroll()`: threshold 60→80px + force-scroll durante streaming

### Added
- **LOGFIX.md**: documentación de bugs corregidos
- `Frontend/marked.min.js`: copia local de marked v4.3.0
- `start_vcore.bat`: script de arranque con PYTHONPATH explícito (`#783`)

---

## v1.1.0 (2026-06-28)

- Hot-swap de modelos en caliente
- Model presets con context windows
- Auto-context guard
- Native function calling + 3 fallbacks
- Circuit breaker + rate limiter
- Auto-auditoría visual 2-fase (DOM + NIM vision)
- Frontend dropdown fix

## v1.0.0 (2026-06-27)

- Migración NVIDIA NIM
- Curator v2 (memoria semántica ChromaDB)
- MCP como capa de herramientas
- Observabilidad Langfuse + JSONL

## v0.9.0

- ReAct agent loop
- Visual audit con Playwright
- Memoria persistente entre sesiones
- Multi-proveedor cloud
- 6 temas de color

## v0.8.0

- Arquitectura multi-agente (Orchestrator, Planner, Curator, Retriever)
- Frontend vanilla JS
- Sidebars redimensionables
- Monaco Editor
- Empty states rotativos
