# ROADMAP — V-CORE: Migración Tauri v2 + React

> **Fecha inicio**: 2026-07-06  
> **Última actualización**: 2026-07-06  
> **Estado**: Planificación — fase de entrevista completada

---

## Decisiones de arquitectura (fijadas en entrevista)

| Decisión | Elección | Justificación |
|---|---|---|
| **Stack frontend** | React 19 + Vite + TypeScript | Máxima iteración (HMR), type safety, ecosistema |
| **Component system** | shadcn/ui (Radix + Tailwind) | Accesibilidad nativa, composición copy-paste |
| **State management** | Zustand | Liviano, no re-renders innecesarios, ideal para streaming |
| **Editor de código** | Monaco Editor (se mantiene) | Carga local en Tauri → sin penalty de bandwidth |
| **Backend** | Sidecar — FastAPI intacto como proceso hijo | 0 rewrite, debug instantáneo, reusa 4000+ líneas |
| **Shell** | Tauri v2 — Rust thin layer | Ventana nativa, IPC, sidecar management |
| **Diseño** | DTCG tokens (design-fundamentals) + registro product-ui | Escala sin duplicar CSS, vocabulario semántico |
| **Migración** | Incremental — por fases, no big bang | Feedback rápido, riesgo acotado |

---

## Fases

### Fase 0 — Toolchain (día 1, ~1h)

- [ ] Instalar Rust: `rustup-init.exe` → stable-x86_64-pc-windows-msvc
- [ ] Verificar: `rustc --version`, `cargo --version`
- [ ] Instalar Tauri CLI: `cargo install tauri-cli --version "^2"`
- [ ] Verificar WebView2 runtime (Windows 10+ ya lo incluye)
- [ ] Crear scaffold: `npm create tauri-app@latest vcore-desktop -- --template react-ts`
- [ ] Verificar: `cargo tauri dev` lanza ventana vacía

### Fase 1 — Shell + Sidecar (día 1, ~2-3h)

- [ ] Configurar `tauri.conf.json`: título "V-CORE", tamaño 1400×900, min 900×600
- [ ] Declarar sidecar: uvicorn como proceso hijo con `tauri::process::Command`
- [ ] `main.rs`: lanzar sidecar al iniciar, matar al cerrar (graceful shutdown)
- [ ] Health check: frontend espera señal de backend listo antes de mostrar UI
- [ ] Configurar CORS/localhost para fetch desde webview a sidecar
- [ ] Verificar: ventana nativa + backend responde en `/health`
- [ ] **Hito**: app desktop funcional con frontend vacío y backend vivo

### Fase 2 — Sidebar + Activity Bar (día 1-2, ~4h)

- [ ] Migrar DTCG tokens de `tokens.json` a `tokens.css` (CSS custom properties)
- [ ] Implementar `ActivityBar` con iconos SVG inline (Chat, Files, Git, Search)
- [ ] Implementar botones inferiores (Approval, Usage, Observability, Theme)
- [ ] Tooltips custom con `act-tooltip`
- [ ] `SidebarShell` con header "V—CORE 1.1 / Orchestrator · agente activo"
- [ ] `ConvList` — cargar sesiones desde `GET /sessions`
- [ ] `NewConvButton` — `POST /sessions` + navegar a nueva sesión
- [ ] `FileTree` — `GET /files/tree` → render recursivo con iconos por extensión
- [ ] `GitPanel` — `GET /git/status` → branch, staged files, diff preview
- [ ] `SearchPanel` — `GET /search/workspace?q=...`
- [ ] Cambio de vista: `showSb` → Zustand `activeView` store
- [ ] Sidebar resize drag handle
- [ ] Verificar: alternar entre Chat/Files/Git/Search sin errores de consola

### Fase 3 — Chat Core (día 2-3, ~6h)

- [ ] `ChatArea` con `MessageScroller` (shadcn/ui pattern)
- [ ] `EmptyState` — mensajes rotativos desde el array existente
- [ ] `UserMessage` — avatar "VC", timestamp, contenido
- [ ] `AgentMessage` — avatar por agente (Orchestrator/Planner/Curator), badge de modelo
- [ ] Markdown rendering con `marked.js` + highlight.js (igual que ahora)
- [ ] `ReasoningBlock` — colapsable, texto monoespaciado
- [ ] `ToolCallBlock` — muestra tool call + resultado colapsable
- [ ] `ScrollPill` — badge de mensajes nuevos, scroll-to-bottom
- [ ] `InputBar` — textarea auto-resize, token counter, botón enviar
- [ ] `InputToolbar` — botones attach/voice/mention (conexión a backend)
- [ ] **Streaming**: EventSource → Zustand store → render incremental
- [ ] `Topbar` — breadcrumb, model selector dropdown, pipeline status dot
- [ ] `ModelSelector` — `GET /llm/providers` → dropdown con hot-swap
- [ ] `CircuitBreakerAlert` — toast cuando un provider entra en OPEN
- [ ] Verificar: conversación completa con GLM-5.2 vía streaming

### Fase 4 — Popovers + Paneles (día 3-4, ~4h)

- [ ] `UsagePanel` — `GET /llm/usage` → barras por modelo, costo total, circuit breakers
- [ ] `ApprovalQueue` — `GET /approvals` → lista con aprobar/rechazar, badge
- [ ] `ThemePicker` — 6 temas (Pure Black, Cyber Green, Amber, Matrix, Ocean, Purple)
- [ ] `Observability` — `GET /observability/status` → Langfuse + traces
- [ ] `RightPanel` — Monaco Editor con `showCode()`, `copyCode()`
- [ ] Panel resize drag handle
- [ ] Posicionamiento de popovers relativo al botón trigger
- [ ] Cerrar popovers con click fuera (actual `initPopovers`)
- [ ] Verificar: cada popover abre/cierra, datos reales, sin errores

### Fase 5 — Polish + Build (día 4, ~3h)

- [ ] Aplicar tokens DTCG a todos los componentes (revisión final)
- [ ] Animaciones: transiciones de sidebar, fade-in de mensajes, scroll suave
- [ ] `prefers-reduced-motion` respetado
- [ ] Keyboard navigation: Tab entre paneles, Escape cierra popovers
- [ ] Focus visible en todos los interactivos
- [ ] Responsive: sidebar colapsa en < 800px, input se adapta
- [ ] **Build standalone**: `cargo tauri build` → .msi/.exe
- [ ] Bundle sidecar: PyInstaller → uvicorn.exe incluido en recursos
- [ ] Test en máquina limpia (sin Python, sin Node)
- [ ] **Hito**: V-CORE como app nativa de Windows, doble click, cero dependencias

---

## Fuera de scope (v2 o backlog)

- Multiplataforma macOS/Linux (WebView2 → WKWebView quirks documentados)
- Rust rewrite del backend (innecesario con sidecar funcionando)
- Tests E2E con Playwright + Tauri
- CI/CD para builds automáticos
- Auto-updater (Tauri updater plugin)
- Sistema de plugins/extensions

---

## Notas técnicas de la entrevista

- **IPC latency**: el register product-ui advierte que `fetch API` vía IPC de Tauri impone latencia real en payloads grandes. Para `/files/tree` (60+ nodos) y respuestas de streaming, mantener buffer consciente.
- **WebView2 quirks**: en Windows heredamos Chromium → CSS moderno sin problemas. El riesgo documentado (WebGL/canvas pesado) no aplica a V-CORE (no usa canvas/WebGL).
- **Figma lesson**: no podemos migrar si el core depende de renderizado de píxel preciso cross-browser. V-CORE no depende de eso → seguro.
- **Tokens**: NUNCA bindear componentes a primitivos. Todo componente consume semánticos (`background`, `foreground`, `accent`, `border`). Cambiar tema = cambiar primitivos, el resto se propaga solo.
- **Tailwind + shadcn/ui**: los componentes shadcn se copian al repo, no se instalan como dependencia. Eso da control total sobre el markup y estilos.
- **Estado actual de V-CORE**: ver `VCORE_ARCHITECTURE.md` y `VCORE_STATE.json`. Este documento describe la migración planificada a Tauri, independiente de la versión actual del runtime.
