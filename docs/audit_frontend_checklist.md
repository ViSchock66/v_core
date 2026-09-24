# Auditoría Frontend V-CORE — Checklist Temporal
> 2026-07-08 · En vivo, no especulativa

## 🔧 Setup
- [ ] Levantar servidor V-CORE en :8000
- [ ] Abrir frontend en browser
- [ ] Verificar health endpoint

## 🧱 Estructura y Layout
- [ ] Page load: todos los elementos presentes (sidebar, activity bar, main, rpanel, input)
- [ ] Activity bar: 4 tabs (chat, files, git, search) + 4 popovers (approval, usage, observability, theme)
- [ ] Sidebar: chat list, new conversation btn, header con logo y versión
- [ ] Topbar: sidebar toggle, status dot, right panel toggle, CB alert
- [ ] Input area: toolbar (attach, voice, mention), textarea, model selector, temperature, send

## 🔄 Flujo de Chat
- [ ] Enviar mensaje simple → respuesta streaming
- [ ] Thinking blocks: expandir/colapsar, no desaparecen al terminar
- [ ] Markdown rendering: código, listas, links
- [ ] Typing indicator: 3 puntos visibles durante streaming
- [ ] Autoscroll: sigue el contenido nuevo, scroll pill aparece al hacer scroll up
- [ ] Nueva conversación: limpia mensajes, crea entrada en sidebar
- [ ] Cambiar entre conversaciones: historial persiste

## 🛠️ Tool Calls y Artefactos (Foco crítico)
- [ ] Pedir a Orchestrator que genere código (ej: "crea un archivo demo.py con X")
- [ ] Verificar que el tool call se muestra en el chat (bloque structured, no texto crudo)
- [ ] Verificar que el archivo aparece como artefacto en right panel
- [ ] Right panel: toggle abre/cierra correctamente
- [ ] Artefacto: se muestra el contenido del archivo, no solo el nombre
- [ ] Artefacto: botón de copiar funciona
- [ ] Artefacto: se puede descargar
- [ ] Artefacto: aparece en lista de artefactos del sidebar

## 🎨 Right Panel / Artefactos (El problema reportado)
- [ ] Diagnosticar por qué los artefactos muestran solo texto, no el archivo
- [ ] Evaluar diseño actual vs Claude-style artifact cards
- [ ] Determinar si vale la pena modernizar o mantener vanilla JS

## 🌓 Temas y UI
- [ ] Dark theme: todos los elementos visibles, contrastes OK
- [ ] Light Studio theme: switch funciona, todos los elementos visibles
- [ ] Animaciones y transiciones: no hay flickering ni jumps

## 🔐 Session Isolation
- [ ] Cambiar de sesión: hot-swap no contamina estado global
- [ ] Modelo cambia por sesión: verificar con GET /system/model
- [ ] Nueva sesión: estado limpio, sin mensajes previos

## 🧪 Edge Cases
- [ ] Servidor caído: connection banner aparece, reconexión funciona
- [ ] Input vacío: send no hace nada
- [ ] Mensaje muy largo: no rompe layout
- [ ] Respuesta con error: se muestra bloque de error, no crashea
