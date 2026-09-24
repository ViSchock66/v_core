# V-CORE — Roadmap activo

> Este es el único backlog activo del proyecto. Se actualiza con evidencia de
> código o pruebas; los documentos históricos no cambian el estado de un ítem.

## Cómo leer este roadmap

| Estado | Significado |
|---|---|
| `✅` | Resuelto y verificado con evidencia de ejecución. |
| `P0` | Bloquea confianza o uso seguro. |
| `P1` | Debe verificarse antes de depender del sistema sin supervisión. |
| `P2` | Mejora importante, pero no bloquea el flujo principal. |
| `Deferred` | Idea válida fuera del runtime actual. |

Un ítem solo se marca resuelto después de una prueba proporcional al riesgo.
“Verificado estáticamente” significa que se leyó el código; no sustituye una
prueba con el servidor vivo.

## ✅ Resuelto — Auditoría 2026-09-23

Sesión de auditoría exhaustiva: 33 endpoints, cada tab y control del frontend,
capturas vía Edge/CDP. Detalle completo en `docs/CHANGELOG.md`. Los ítems que
este roadmap listaba como P0/P1 y quedaron cerrados:

- **Approval flow ejecutable e idempotente** (era P0). `POST /approvals/{id}/approve`
  ahora re-invoca la tool con los params guardados vía `MCPClient`, con
  idempotencia real. Aceptación cumplida: write_file aprobado crea el archivo,
  el segundo approve devuelve `already_executed` sin re-ejecutar, y rechazar no
  ejecuta. De paso salieron tres bugs encadenados (alias `execute_shell`, `cwd`
  no aceptado, falta de idempotencia).
- **XSS almacenado en `escHtml`.** No era un ítem del roadmap; se descubrió en
  esta auditoría. La función de escape era un no-op. Verificado y corregido.
- **Puerto canónico.** Cuatro fuentes decían 8000 y dos 8001; el CLI y los MCP
  servers apuntaban a un puerto muerto. Fuente única en `api/ports.py`.
- **`/system/health`** se auto-bloqueaba (13s) y reportaba el backend caído.
  Ahora resuelve su estado in-process: 0.01s, sin alertas falsas.
- **Búsqueda y explorador de archivos** operaban sobre `workspace/` en vez del
  proyecto: 0 resultados de búsqueda y 9 nodos en el árbol. Corregido.
- **`hot_swap` rompía el 3-point config sync** y el selector de temperatura no
  existía en la UI pese a estar documentado.

## P0 — Correctitud y seguridad operativa

### ROTAR la GEMINI_API_KEY filtrada

**Evidencia:** el commit `f057245` ("v4.2-final") versionó `.env` con la clave
real. `4ec33ed` la removió del tracking, pero sigue alcanzable en el historial
desde `main` y `v0.3-dev`, y coincide con la que el sistema usa hoy.

**Objetivo:** rotar la clave en Google AI Studio y actualizar `.credentials.yaml`.
Opcionalmente reescribir el historial para purgar `f057245`.

**Bloquea:** hacer público el repositorio.

### Resiliencia ante fallo de proveedor

El comportamiento de DeepSeek V4 Pro y de los timeouts no se ha reproducido en
esta limpieza. Debe evaluarse con un fallo controlado antes de introducir un
fix específico.

**Aceptación:** timeout/fallo simulado no derriba la API, entrega un error
legible o rota según la política configurada y conserva la sesión.

## P1 — Confianza verificable

### Fallback chains y circuit breaker

El código trata HTTP 429 como transitorio y lo deja fuera del cálculo del
circuit breaker. Validar con pruebas controladas que esta política, el rate
limiter y los fallback chains se comporten como espera el operador.
(Verificado en esta auditoría que el reset funciona y devuelve los 15 breakers
en CLOSED; falta la prueba de degradación.)

**Aceptación:** probar cada eslabón relevante del chain, registrar proveedor
seleccionado, estado del circuit breaker y resultado visible para la sesión.

### Session isolation end-to-end

La implementación usa configuración por directorio y locks por sesión. Falta
una prueba concurrente vigente que demuestre que un hot-swap de una sesión no
afecta otra ni el template `sessions/default/`.
(En esta auditoría se verificó el 3-point sync de `temperature` y del modelo
lead; falta el caso concurrente multi-sesión.)

### Resistencia a prompt injection por contenido leído

Diseñar y ejecutar un caso controlado en que un archivo leído contenga una
instrucción maliciosa. La tarea legítima debe completarse sin ejecutar esa
instrucción ni alterar reglas protegidas.

**Contexto nuevo:** durante la auditoría se encontró un archivo de prueba con
exactamente esa inyección (`notas_proyecto.txt`, instrucciones para sobrescribir
`gate_rules.yaml`) versionado dentro de `workspace/`, que es la raíz por defecto
que Retriever usa para indexar. Se removió del repositorio, pero **el caso no se
probó**: no está verificado que el sistema resista la instrucción si ese
archivo se lee durante una tarea.

### Regresión funcional del frontend

El frontend fue reducido de forma considerable durante un refactor. Auditar
las funciones de producto contra el backend actual antes de declarar
recuperaciones o tests históricos como vigentes.
(Esta auditoría cubrió los 4 tabs, selector de modelo, temperatura, acentos,
temas, approvals, artefactos y toggles. Sigue pendiente la lista de funciones
perdidas de `docs/_archive/FUNCIONES_PERDIDAS.md`.)

## P2 — Evolución del producto

- Compresión persistente de contexto para conversaciones largas.
- Skills persistentes y aprendizaje entre sesiones.
- Delegación con contexto aislado.
- Scheduler para tareas recurrentes.
- Perfiles o multi-proyecto aislados.
- Registro e instalación de servidores MCP externos.
- Suite E2E reproducible que no dependa del gateway de Hermes.

## Deferred — Escritorio Tauri

Tauri es una posible capa de escritorio con FastAPI como sidecar. No forma
parte del runtime actual y no se inicia hasta que P0 y P1 tengan evidencia de
aceptación. El diseño de esa exploración está en `docs/ROADMAP_TAURI.md`.

## Criterio de salida para flujos personales

Antes de sustituir una tarea real asistida por otra herramienta, ejecutar ese
flujo en paralelo y confirmar: aprobación efectiva, recuperación ante fallo de
proveedor, aislamiento de sesión y trazabilidad del resultado.
