# V-CORE — Comparativa con harnesses de código (2026-09-23)

> Documento de insumo para `docs/PLAN_REFACTOR_V2.md`. Responde una pregunta
> concreta: **¿qué tomamos y qué no de los harnesses del estado del arte?**
>
> Regla de este documento: cada afirmación tiene fuente. Lo que no pude verificar
> está marcado como tal. No hay arquitectura inventada.

## 0. Método y alcance

Fuentes primarias descargadas y leídas (no snippets):

| Fuente | Qué es | Tamaño |
|---|---|---|
| `openedclaude/claude-reviews-claude` → `architecture/11-compact-system.md` | Análisis del sistema de compactación de Claude Code, con constantes y números de línea | 336 líneas |
| `johnzfitch/claude-wiki` → `auto-compact-deep-dive.md` | Ingeniería inversa del auto-compact (binario 2.1.75) | 449 líneas |
| `openai/codex` → issue #40095 | Bug de umbral de compactación, con mediciones reales | 542 líneas |
| `openai/codex` → issue #11805 | Clamp al 90% que anula la config del usuario | — |
| `code.claude.com/docs/en/sub-agents.md` | Doc oficial de subagentes | 117 KB |
| `MohamedAbdallah-14/Wazir` → `research/codebase-understanding/repo-maps.md` | Estudio de 18 técnicas de repo map | 257 líneas |
| `Aider-AI/aider` → `aider/repomap.py` | Código fuente real del repo map | 27 KB |
| `pi.dev/docs/latest/how-pi-works` | Doc oficial de arquitectura de Pi | — |
| `az9713/pi-vs-claude-code` → `COMPARISON.md` | Comparación directa, 243 líneas de tablas | 39 KB |

**Limitación honesta:** el egress de red estuvo bloqueado al inicio de la sesión
(por una escalada de sandbox que no pedí). Se resolvió, pero **la investigación
previa se perdió**: los subagentes que la harían murieron sin escribir nada
(ver §6). Lo de acá es la investigación rehecha directamente.

---

## 1. Claude Code — el sistema de compactación es una arquitectura de 3 niveles

Es, con diferencia, **lo más avanzado que encontré**, y lo más transferible.
Fuente: `11-compact-system.md`.

### Los tres niveles

| Nivel | Mecanismo | Disparador | Compresión | Costo |
|---|---|---|---|---|
| **MicroCompact** (531 líneas) | Vacía resultados de tools viejos | Cada turno | ~10–50K tokens | Ninguno |
| **Session Memory Compact** (631 líneas) | Reemplaza mensajes viejos por memoria pre-construida | Umbral de auto-compact | ~60–80% | Sin llamada LLM |
| **Full Compact** (1.706 líneas) | El LLM resume toda la conversación | Auto o `/compact` | ~80–95% | 1 llamada API |

### El detalle que nos importa

MicroCompact ataca **exactamente** los tools de alto volumen, y **preserva** los
que no son reproducibles:

```typescript
const COMPACTABLE_TOOLS = new Set([
  FILE_READ_TOOL_NAME, ...SHELL_TOOL_NAMES, GREP_TOOL_NAME,
  GLOB_TOOL_NAME, WEB_SEARCH_TOOL_NAME, WEB_FETCH_TOOL_NAME,
  FILE_EDIT_TOOL_NAME, FILE_WRITE_TOOL_NAME,
])
// Los resultados de AgentTool y de tools MCP se PRESERVAN.
```

**Esto contrasta de forma brutal con V-Core.** Nosotros truncamos *todos* los
resultados a 800 caracteres *siempre* (`enlil.py:752`). Claude Code **conserva el
resultado completo y vacía solo los viejos**. Nosotros tenemos lo peor de ambos
mundos: el modelo nunca ve el contenido íntegro **y** no tenemos política de
expiración.

### Umbrales exactos

```typescript
effectiveWindow = contextWindow - Math.min(maxOutputTokens, 20_000)
threshold       = effectiveWindow - 13_000        // headroom de seguridad
warning         = effectiveWindow - 20_000
blockingLimit   = effectiveWindow - 3_000         // exige /compact manual
```

Para un modelo de 200K: ventana efectiva 180K, **compacta a 167K (~93%)**.
Se puede ajustar con `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (recomiendan 85) y
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` — y el override de ventana es un **techo**, no
un piso: `Math.min(nativo, override)`, nunca extiende.

### Preservación post-compactación

```typescript
POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_TOKEN_BUDGET         = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE  = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET  = 25_000
```

Tras compactar **re-inyecta los últimos 5 archivos leídos** y las skills invocadas,
para que el modelo no tenga que releerlos. Es el detalle que hace que la
compactación no se sienta como amnesia.

### El prompt de resumen son 9 secciones

1. Petición e intención primaria · 2. Conceptos técnicos clave · 3. Archivos y
secciones de código **con snippets completos** · 4. Errores y arreglos · 5.
Resolución de problemas · 6. **Todos los mensajes del usuario** (crítico para la
intención) · 7. Tareas pendientes · 8. Trabajo actual · 9. Próximo paso opcional

Usa un bloque `<analysis>` que se **elimina** antes de inyectar el resumen:
cadena de pensamiento para mejorar la calidad, sin costo de tokens posterior.

### Un circuit breaker para la compactación

```typescript
const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
// BQ 2026-03-10: 1.279 sesiones tuvieron 50+ fallos consecutivos,
// desperdiciando ~250K llamadas API/día a nivel global
```

Y guardas de recursión: *"no compactes al compactador"*.

### Invariantes de API que hay que respetar

`adjustIndexToPreserveAPIInvariants()` (80+ líneas) garantiza que al cortar el
historial: (1) **todo `tool_result` conservado tenga su `tool_use` precedente**, y
(2) los bloques de pensamiento no queden huérfanos. **Esto es una trampa real y
nosotros vamos a caer en ella** si compactamos sin respetarla: cortar por índice
rompe pares tool_use/tool_result y la API devuelve error.

---

## 2. Codex — compactación con número medido y un bug documentado

Fuente: issues #40095 y #11805 de `openai/codex`. **Estos son datos duros con
mediciones del propio reportante.**

### El umbral real: 94.74%, no 90%

```
ventana usable reportada:        258.400 tokens
límite de auto-compact por defecto: 244.800  = 90% de 272.000 (ventana CRUDA)
90% de la ventana usable sería:    232.560
244.800 / 258.400 = 94,7368%       ← el bug
```

**El costo medido del desajuste:** *"el hilo maduro hizo 35 llamadas de muestreo
con entrada igual o superior a 232.560 antes de la compactación estándar. Esas
llamadas procesaron **8.376.048 tokens de entrada** en el hueco creado por el
desajuste del umbral."*

Y el efecto de compactar bien: *"la entrada media por llamada cayó **70,3% y
76,4%**"* tras dos compactaciones. Cada compactación remota tardó **~85 y ~104
segundos**.

### Lo que Codex hace con el historial al compactar

> *"Los historiales de reemplazo contenían **cinco mensajes retenidos y un ítem de
> compactación opaco, sin llamadas de tool ni salidas de tool en crudo**."*

**Codex descarta todas las llamadas y salidas de tools al compactar.** Es una
decisión agresiva: pierde la trazabilidad de lo que el agente hizo. Nuestro
event sourcing (que persiste cada evento en `agent_events`) es **superior** en
este punto — pero nosotros no lo usamos para reconstruir contexto.

### Configuración y clamp

```
effective_auto_compact_limit = min(user_config_limit, context_window * 90%)
```

La clave de config es `model_auto_compact_token_limit`. En v0.100.0 metieron el
clamp duro y anularon el valor del usuario — el issue lo describe como
*"removed a critical user capability"*. Existe `SUMMARIZATION_PROMPT` y un test
`auto_compact_clamps_config_limit_to_context_window` en
`codex-rs/core/tests/suite/compact.rs`.

**Lección de diseño para nosotros:** un umbral fijo de porcentaje es peor que un
umbral derivado de la **ventana efectiva**. Y el override del usuario debe poder
**bajar** el umbral libremente, nunca ser clamped en silencio.

---

## 3. Aider — el repo map, con números

Fuente: `repo-maps.md` (estudio de 18 técnicas) + `aider/repomap.py`.

- Aider envía un **mapa del repo entero** con la lista de archivos y los
  **símbolos clave** de cada uno (cabeceras de clase, firmas de función).
- `--map-tokens` default: **1.024 tokens**. Usa **búsqueda binaria** para el mayor
  subconjunto de tags rankeados que entra en el presupuesto, con **15% de tolerancia**.
- Ranking: **PageRank sobre un grafo de dependencias** (archivo = nodo, aristas =
  referencias cruzadas de símbolos), con **personalización**: los archivos en el
  chat actual pesan más, así el mapa prioriza lo relevante a la tarea.
- Extracción con **tree-sitter** (reemplazó a ctags porque extrae definiciones
  **y referencias**), con queries `tags.scm` por lenguaje, **130+ lenguajes**.
- Se **reconstruye en cada request**; caché por `mtime` para no re-parsear.

### La cifra que justifica todo

> El enfoque de repo map de Aider logra **4,3–6,5% de utilización de la ventana de
> contexto**, contra **54–70%** de los agentes que usan estrategias de búsqueda
> iterativa.

Y en SWE-bench: el mapa con tree-sitter **mejoró el rendimiento** frente al de
ctags, *"demostrando que mejor selección de contexto mejora directamente la
precisión de las ediciones"*.

### El espectro completo de representación de código

| Nivel | Enfoque | Costo de tokens | Herramientas |
|---|---|---|---|
| 1 | Árbol de archivos | Muy bajo | `tree` |
| 2 | Empaquetado completo | Muy alto | Repomix, Gitingest |
| 3 | Empaquetado comprimido (firmas) | Medio | Repomix `--compress` |
| 4 | **Mapa de símbolos rankeado** | **Bajo** | **Aider, RepoMapper** |
| 5 | RAG de embeddings | Variable | Cursor, Roo Code |
| 6 | Grafo de conocimiento persistente | Pre-computado | Augment, Cody |

**Ganancia clave:** un mapa a nivel de símbolos ocupa **5–10% del código original**
y captura **~90% de lo que el LLM necesita** para entender la arquitectura.
El repo map funciona como **tabla de contenidos**: no reemplaza leer archivos, le
dice al modelo **cuáles** leer.

---

## 4. Pi — minimalismo deliberado, y una postura de seguridad que nos favorece

Fuente: doc oficial `how-pi-works` + `COMPARISON.md`.

### Arquitectura real

- **Una sesión es un árbol**: mensajes y eventos forman un árbol; cada camino es
  una rama; la rama activa provee el historial. Persistencia en **JSONL**, cada
  entrada con `id` y referencia al padre.
- **Compaction inserta una entrada de resumen que reemplaza los mensajes viejos en
  requests posteriores — pero las entradas originales permanecen en el árbol.**
  Esto es el "context folding": es **lossless**, se puede volver atrás.
- **Abort detiene el run y devuelve los mensajes en cola al editor.** Pi tiene
  cancelación real.
- **Skills se cargan a demanda** (progressive disclosure), no en el prompt base.
- **Extensiones en TypeScript in-process**: 25 eventos, incluyendo
  `session_before_compact` (puede **reemplazar por completo** la compactación) y
  `context` (copia profunda de los mensajes, se puede filtrar y podar).

### Su postura de seguridad — y por qué nos conviene

> *"YOLO por defecto — sin permisos, sin sandbox. **'La seguridad en los agentes de
> código es mayormente teatro; si puede escribir y ejecutar código, se acabó el
> juego.'**"*

Y en la doc oficial: *"Las tools habilitadas usan los permisos del sistema
operativo del proceso Pi."*

**Esto valida que nuestro gate es un diferencial genuino.** Pi no tiene capa de
política: confía en los permisos del SO. Nosotros tenemos 22 tools catalogadas,
niveles A/B, evaluación de riesgo, allowlist de raíces y aprobación humana
bloqueante. **Eso no es teatro: es la diferencia entre un agente que puede
exfiltrar `C:\Windows\hosts` y uno que devuelve `DENEGADO por política de rutas`.**

### Sistema prompt y tools

| Dimensión | Claude Code | Pi |
|---|---|---|
| System prompt | **~10.000+ tokens** | **~200 tokens** |
| Tools por defecto | 10+ | 4 (`read`, `write`, `edit`, `bash`) |
| Permisos | 5 modos, deny-first, sandbox | **ninguno** |
| Subagentes | Task tool, 7 paralelos | no nativos (extensión) |
| Sesión | lineal | **árbol JSONL con fork** |
| Hooks | 14 eventos (shell) | **25 eventos (TS in-process)** |
| Proveedores | Claude únicamente | **20+, 324 modelos** |

Dato relevante para nosotros: **nuestro prompt son ~5.224 tokens** — la mitad que
Claude Code, 26 veces más que Pi. No estamos en un extremo absurdo, pero está muy
por encima de lo que Pi demuestra que es suficiente.

---

## 5. La tabla comparativa

**Confianza:** V-Core = **alta** (leí el código y medí). Externos = **alta** para
citas directas de fuentes, **media** donde la fuente es de terceros.

| Capacidad | V-Core | Codex | Claude Code | Aider | Pi |
|---|---|---|---|---|---|
| Compactación | ❌ **no existe** | ✅ 94.7% | ✅ **3 niveles** | ⚠️ vía mapa | ✅ lossless |
| Preserva resultados de tool | ❌ **corta a 800 chars** | ❌ los descarta | ✅ **vacía solo viejos** | — | ✅ |
| Repo map / ranking | ❌ | ❌ | ⚠️ | ✅ **PageRank** | ❌ |
| Permisos determinísticos | ✅ **22 tools + riesgo** | ✅ sandbox SO | ✅ deny-first | ❌ | ❌ **YOLO** |
| Aprobación humana | ✅ **bloqueante** | ✅ | ✅ | ⚠️ | ❌ |
| Sandbox de SO | ❌ | ✅ | ✅ | ❌ | ❌ |
| Subagentes aislados | ❌ | ✅ | ✅ **3 niveles máx** | ❌ | ❌ |
| Cancelación de run | ❌ | ✅ | ✅ | ✅ | ✅ |
| Sesión ramificable | ⚠️ event log | ❌ | ⚠️ fork | ❌ | ✅ **árbol** |
| Event sourcing + replay | ✅ **`seq`** | ❌ | ⚠️ | ❌ | ✅ árbol |
| Circuit breaker | ✅ **15 + credential pool** | ❌ | ✅ (para compact) | ❌ | ❌ |
| Task graph DAG | ✅ **87 grafos** | ❌ | ❌ | ❌ | ❌ |
| Verificación post-edición | ✅ **por lenguaje** | ⚠️ | ⚠️ | ✅ test/lint | ⚠️ |
| Aislamiento por worktree | ⚠️ (plan F5) | ❌ | ✅ `isolation: worktree` | ❌ | ❌ |

### Lo que dice la tabla

**V-Core tiene tres cosas que ningún otro tiene:** circuit breaker por proveedor
con credential pool, task graph con DAG persistido, y gate con niveles de riesgo.
**Pi no tiene permisos. Aider no tiene sandbox. Codex y Claude Code no tienen
grafo de tareas ni breakers de proveedor.**

**A V-Core le falta una sola familia de capacidades: gestión de sesión.** Y es
justo la que todos los demás tienen (compactación, cancelación, subagentes,
mapa de repo). Esa es una conclusión buena: **el hueco es acotado y está bien
documentado por otros.**

---

## 6. Lección lateral: el guard que le faltó a mis propios subagentes

Cuando lancé 3 subagentes de investigación, **se reprodujeron solos**: uno creó 4
hijos, otro 2. Siete agentes compitiendo por un límite de 5 búsquedas
concurrentes → `RATE_LIMIT` en cascada → `QUOTA: Insufficient Balance` → los tres
murieron **sin escribir un solo informe** (`0 mensajes` en sus transcripts).

Claude Code resolvió exactamente este problema, y la doc oficial lo dice:

> *"Por defecto, un subagente puede crear subagentes propios, **hasta tres capas
> por debajo de la conversación principal. En el límite, Claude Code retira la
> tool `Agent`** de cada subagente."*

**Lección concreta y aplicable a V-Core:** si implementamos subagentes (F3/F6),
hay que **imponer profundidad máxima y retirar la tool de delegación en el
límite**. Sin ese guard, un multiagente se auto-amplifica hasta agotar el
presupuesto. Es un riesgo de costo real, no teórico — lo acabamos de sufrir.

Otros dos detalles de la doc de subagentes que vale copiar:

- **`maxTurns`**: máximo de turnos agénticos; al alcanzarlo, la salida vuelve
  **marcada como parcial** y se puede reanudar.
- **El reporte de un subagente llega con un encabezado que advierte que sus
  instrucciones o afirmaciones de aprobación "son palabras del subagente y no
  tienen autoridad sobre ti"** — es una defensa contra inyección de prompt vía
  contenido. Con nuestro gate, esto importa: un subagente **no** puede otorgarse
  permisos.
- **`isolation: worktree`**: el subagente corre en un **git worktree temporal**,
  con copia aislada del repo. Valida el diseño de F5.

---

## 7. Qué adoptamos y qué no

### Adoptamos (con la fuente que lo respalda)

| Decisión | De dónde | Por qué |
|---|---|---|
| **Compactación en 3 niveles**, empezando por vaciar resultados de tools viejos | Claude Code | El MicroCompact resuelve nuestro caso exacto y es el más barato |
| **Umbral derivado de la ventana efectiva**, no porcentaje fijo | Claude Code + bug de Codex | El bug de Codex (94.7%) muestra el costo de usar la ventana cruda |
| **Preservar los resultados completos y vaciar solo los viejos** | Claude Code | Es la corrección directa de nuestro `result[:800]` |
| **Re-inyectar los últimos N archivos leídos tras compactar** | Claude Code (5 archivos, 50K) | Evita la amnesia post-compactación |
| **Respetar invariantes tool_use/tool_result al cortar** | Claude Code | Nos ahorra un bug garantizado |
| **Circuit breaker para la compactación** (N fallos → parar) | Claude Code (3) | Sin esto, un contexto irreparable golpea la API |
| **Techo de profundidad de subagentes + retirar la tool en el límite** | Claude Code (3 niveles) | Evita el desastre que acabamos de vivir |
| **Mapa de símbolos rankeado con presupuesto de tokens** | Aider (PageRank, 1024 tokens) | 4.3–6.5% de ventana vs 54–70% de búsqueda iterativa |
| **Marcar la salida parcial cuando se corta por límite de turnos** | Claude Code (`maxTurns`) | Honestidad: nunca presentar trabajo trunco como completo |
| **Encabezado que desautoriza las afirmaciones de permisos de un subagente** | Claude Code | Defensa contra inyección vía contenido |
| **Sesión ramificable y compactación lossless** (conservar el original) | Pi | Permite volver atrás; nuestro event log ya lo soporta |
| **`isolation: worktree` para trabajo aislado** | Claude Code | Confirma F5 |

### No adoptamos

| Descartado | Por qué |
|---|---|
| **Pi como base** | Su fuerte (ciclo de vida de sesión) es un bloque acotado; su debilidad (YOLO sin permisos) es nuestro diferencial. Adoptarlo cambia lo que nos hace distintos por lo que nos falta. |
| **Descartar tool calls al compactar** (como Codex) | Perdemos trazabilidad. Nuestro event log es superior: compactamos el **contexto**, no el **registro**. |
| **Repo map con embeddings / grafo persistente** (Cursor, Augment) | Nivel 5–6 del espectro: mucho costo de infraestructura para un proyecto local. El nivel 4 (Aider) da el 90% del valor. |
| **Sandbox de SO ahora** | Correcto en teoría, pero primero el workspace (F5). Ya está en el roadmap como Deferred. |
| **Prompt de ~200 tokens** (Pi) | Demasiado agresivo para un sistema multiagente con roles. Pero bajar de 5.224 a ~1.500 es un objetivo razonable. |

---

## 8. Lo que esto cambia en el plan

El bloque de motor deja de ser genérico: son **fugas medidas y contrastadas**.

| Fuga de V-Core | Evidencia propia | Cómo lo resuelven ellos |
|---|---|---|
| Resultados cortados a 800 chars | `enlil.py:752,837,899,959` | Claude Code: completos, vacía solo viejos |
| Sin compactación | `compact` → 0 ocurrencias | Claude Code: 3 niveles; Pi: lossless |
| Historial truncado duro | `history[-10:]` (`:572`, `:1276`) | Claude Code: resumen de 9 secciones |
| Prompt fijo de ~5.224 tokens por iteración | Medido | Pi: ~200; Claude Code: ~10.000 con guardrails |
| Cuatro catálogos de tools, se usa el más pobre (8 de 22) | `_TOOL_DEFS` vs `gate_rules.yaml` | Claude Code: 10+ tools bien descritas |
| Sin cancelación | `interrupt` → 0 ocurrencias | Pi: abort devuelve la cola al editor |
| Sin invariancia tool_use/tool_result | No aplica (no compacta) | Claude Code: 80+ líneas dedicadas |
| Sin mapa de repo | No existe | Aider: PageRank + árbol de firmas |

Y una conclusión para la entrevista, que sale directo de esta comparación:

> **Los harnesses comerciales invirtieron en que la sesión larga funcione.
> V-Core invirtió en que el sistema no se caiga ni haga cosas peligrosas.
> La comparación muestra que ambos conjuntos son complementarios, y que el
> nuestro —permisos determinísticos, breakers por proveedor, grafo de tareas—
> es el que ninguno de los cuatro tiene.**

---

## 9. Lectura recomendada

- **Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents** —
  [arXiv 2609.00006](https://export.arxiv.org/pdf/2609.00006). Vocabulario formal
  del dominio; documenta incluso a OpenHands con gestión de memoria enchufable vía
  una clase `Condenser` abstracta. **Es el marco teórico para presentar V-Core.**
- [Aider: Building a better repository map](https://aider.chat/2023/10/22/repomap.html)
- [Claude Code: Explore the context window](https://code.claude.com/docs/en/context-window)

## 10. Fuentes citadas

- Claude Code — sistema de compactación: `openedclaude/claude-reviews-claude` → `architecture/11-compact-system.md`
- Claude Code — auto-compact (ingeniería inversa): `johnzfitch/claude-wiki` → `02-Claude-Code-CLI/auto-compact-deep-dive.md`
- Claude Code — subagentes: https://code.claude.com/docs/en/sub-agents
- Claude Code — ventana de contexto: https://code.claude.com/docs/en/context-window
- Codex — umbral 94.7%: https://github.com/openai/codex/issues/40095
- Codex — clamp al 90%: https://github.com/openai/codex/issues/11805
- Codex — subagentes: https://developers.openai.com/codex/subagents
- Aider — repo map (estudio de 18 técnicas): `MohamedAbdallah-14/Wazir` → `docs/research/codebase-understanding/repo-maps.md`
- Aider — fuente: `Aider-AI/aider` → `aider/repomap.py`
- Pi — arquitectura: https://pi.dev/docs/latest/how-pi-works
- Pi vs Claude Code: `az9713/pi-vs-claude-code` → `COMPARISON.md`

## 11. Notas de honestidad

- **Verificado por mí, ejecutando:** las métricas de V-Core (prompt de ~5.224
  tokens, `result[:800]`, `history[-10:]`, 0 ocurrencias de `compact`/`interrupt`,
  8 tools hardcodeadas contra 22 en `gate_rules.yaml`, 56 rutas del backend).
- **Leído en fuentes primarias:** todo lo de Claude Code, Codex, Aider y Pi de la
  §1–§4. Los números (167K, 94.7%, 1.024 tokens, 4.3–6.5%, 5 archivos, 50K) están
  citados de esas fuentes, no inferidos.
- **No verificado:** no ejecuté ninguno de los cuatro harnesses. Las cifras de
  rendimiento de terceros (SWE-bench, la comparación de Augment) provienen de las
  fuentes y **no las reproduje**.
- **Fallido y descartado:** la primera ronda de investigación por subagentes murió
  sin producir informes (§6). No se rescató nada de ella; este documento es
  trabajo rehecho desde cero.
