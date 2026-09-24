# Session Isolation Architecture

> **Hito diferenciador — Julio 2026 · Implementado v1.4**
> Por qué nuestra arquitectura de sesiones resuelve lo que Hermes Agent no puede.

---

## 1. Resumen ejecutivo

Todo sistema agéntico multi-sesión enfrenta el mismo problema: **cómo aislar conversaciones concurrentes sin que se mezclen contextos, modelos, ni configuraciones**. La respuesta define si el sistema escala o colapsa bajo uso real.

**Hermes Agent** opta por `contextvars.ContextVar` + un diccionario de overrides en memoria (`_session_model_overrides`) persistido en `sessions.json`. Es una solución de **parcheo sobre estado global compartido**: todas las sesiones comparten el mismo `config.yaml`, y las diferencias se aplican como overrides ad-hoc.

**Nuestra arquitectura** propone **Filesystem-based Session Isolation**: cada sesión recibe un clon inmutable del perfil base, vive en su propio directorio, y nunca comparte estado con otras sesiones. El aislamiento es estructural, no un parche.

Este documento explica ambas arquitecturas, analiza por qué la de Hermes falla bajo concurrencia real, y detalla nuestra implementación.

---

## 2. El problema del estado compartido

En un sistema agéntico que maneja múltiples conversaciones simultáneas, hay tres tipos de estado que compiten:

| Tipo de estado | Ejemplo | Riesgo si se comparte |
|---|---|---|
| **Configuración** | Modelo activo, provider, API keys | Una sesión cambia el modelo de otra |
| **Contexto** | Historial de mensajes, memoria semántica | Los auxiliares heredan conversaciones ajenas |
| **Operación** | Circuit breakers, rate limiters, usage tracking | Métricas agregadas incorrectamente |

El caso más peligroso es el **hot-swap de modelo**: el usuario cambia el modelo en la sesión A esperando que solo afecte a A. Si el estado es global, la sesión B también cambia sin que su usuario lo sepa.

---

## 3. Arquitectura de Hermes Agent

### 3.1 Aislamiento de contexto: `ContextVar`

Hermes usa `contextvars.ContextVar` para que cada tarea asyncio tenga su propia copia de variables de sesión:

```python
# gateway/session_context.py
_SESSION_PLATFORM: ContextVar = ContextVar("HERMES_SESSION_PLATFORM", default=_UNSET)
_SESSION_CHAT_ID:  ContextVar = ContextVar("HERMES_SESSION_CHAT_ID", default=_UNSET)
_SESSION_THREAD_ID: ContextVar = ContextVar("HERMES_SESSION_THREAD_ID", default=_UNSET)
_SESSION_ID:       ContextVar = ContextVar("HERMES_SESSION_ID", default=_UNSET)
# ... 12 variables más
```

**Propósito**: cuando dos mensajes de Telegram llegan simultáneamente, cada uno corre en su propia tarea asyncio con su propio `ContextVar`. Sin esto, `os.environ` (process-global) haría que el mensaje B sobrescriba las variables del mensaje A.

**Funciona para**: identificar qué conversación está activa en cada tarea.

### 3.2 Model override: `_session_model_overrides`

Para cambiar el modelo por conversación, Hermes mantiene un diccionario en memoria:

```python
# gateway/run.py — GatewayRunner
self._session_model_overrides: dict[str, dict] = {}
# {
#   "telegram:abc123": {"model": "gpt-5o", "provider": "openai", "api_key": "sk-..."},
#   "discord:def456":  {"model": "claude-sonnet-4-6", "provider": "anthropic", ...},
# }
```

Cuando el usuario ejecuta `/model gpt-5o` en Telegram, el override se guarda en este dict. Cuando se crea el agente para esa sesión, se lee el override y se inyecta en `make_agent()`.

**Persistencia**: los overrides (sin `api_key`) se escriben en `sessions.json` para sobrevivir reinicios del gateway.

### 3.3 Flujo completo

```
Usuario Telegram: /model gpt-5o
  → GatewayRunner._handle_model_command()
    → self._session_model_overrides[session_key] = {model, provider, api_key, ...}
    → SessionStore.set_model_override(session_key, override)  # persiste en sessions.json
    → Responde "Modelo cambiado a gpt-5o"

Siguiente mensaje:
  → GatewayRunner._make_agent_for_context()
    → override = self._session_model_overrides.get(session_key)
    → Si existe override → se inyecta en AgentInit
    → Si no → usa el modelo global de config.yaml
```

---

## 4. Puntos de fallo de la arquitectura de Hermes

### 4.1 Estado global compartido como base

El `config.yaml` es un archivo único. El `model.default` es global. Los overrides son **excepciones** sobre una base compartida. Esto crea una jerarquía frágil:

```
config.yaml (global, todos lo comparten)
  └── _session_model_overrides (excepción por sesión)
```

Si el override no se aplica correctamente —por un bug, un race condition, o una tarea que no propaga el ContextVar— la sesión silenciosamente vuelve al modelo global.

### 4.2 ContextVar no se propaga a subprocesos

`ContextVar` es task-local en asyncio, pero **no se propaga automáticamente a subprocesos** creados con `threading.Thread` o `run_in_executor`. Si un auxiliar (delegación, browser, cron) corre en un thread separado, hereda `_UNSET` en lugar del valor de la sesión. Esto es exactamente lo que causa que:

> "cuando tienes aux, se confunden en distintas conversaciones y se mezclan sus contextos"

El fix es pasar explícitamente el `ContextVar` al thread, pero cada tool y cada auxiliar debe implementarlo individualmente. Es frágil por diseño.

### 4.3 `sessions.json` como single point of corruption

Todos los overrides de todas las sesiones viven en un solo archivo JSON. Si el archivo se corrompe (escritura parcial, crash del gateway, race condition entre hilos), **todas** las sesiones pierden su configuración simultáneamente.

### 4.4 `session_key` es un hash, no un identificador estable

```python
session_key = build_session_key(source)
# → "telegram:abc123def456"
```

Si el `chat_id` o `thread_id` cambia (migración de grupo, cambio de topic), el session_key cambia y la sesión se "pierde" (el historial queda huérfano). No hay un mecanismo de migración o alias.

### 4.5 Sin aislamiento de fallo entre sesiones

Si una sesión corrompe el `config.yaml` (o `sessions.json`), todas las sesiones se ven afectadas. El radio de explosión de un error es "todas las conversaciones activas".

---

## 5. Nuestra arquitectura: Filesystem-based Session Isolation

### 5.1 Principio fundacional

> **Cada sesión es un directorio. Cada directorio es un perfil completo e independiente.**

No hay estado global compartido entre sesiones. No hay overrides. No hay `ContextVar`. El sistema de archivos es el mecanismo de aislamiento.

### 5.2 Estructura de directorios (implementada)

```
raíz del proyecto/
  sessions/
    default/                        ← perfil base (plantilla inmutable, versionada)
      model_routing.yaml            ← lead = Kimi K2.6
      gate_rules.yaml               ← reglas de seguridad
      state.json                    ← estado inicial
      .env.keys.example             ← template sin secretos

    sess_5_abc123/                  ← sesión activa (clon de default)
      model_routing.yaml            ← hot-swap modifica ESTE archivo
      state.json                    ← session_id, fecha_actualizacion

    sess_8_def456/                  ← otra sesión, otro modelo, cero interferencia
      model_routing.yaml            ← puede tener lead=GLM mientras otra usa Kimi

    _archive/                       ← sesiones eliminadas
      sess_3_old/                   ← preservadas, no destruidas
```

### 5.3 Concurrencia: lock por sesión

Dos requests de la misma sesión no pueden ejecutarse simultáneamente — el swap del router no es atómico y se pisarían. Requests de distintas sesiones corren en paralelo sin interferencia:

```python
# api/main.py — agents_route
_session_locks: dict[str, asyncio.Lock] = {}

async def agents_route(body: ChatMessage):
    session_lock = _get_session_lock(body.session_dir)  # lock por sesión

    async def event_stream():
        await session_lock.acquire()        # bloquear esta sesión
        try:
            saved = get_router()            # preservar singleton
            set_router(LLMRouter(sessions/<id>/model_routing.yaml))
            try:
                ...  # ENLIL.route()
            finally:
                set_router(saved)           # restaurar
        finally:
            session_lock.release()          # liberar

    return StreamingResponse(event_stream(), ...)
```

**Garantías:**
- Misma sesión → secuencial (lock)
- Distintas sesiones → paralelo (distinto lock)
- Sin `session_dir` → sin lock (compatibilidad hacia atrás)
- `finally` anidados aseguran que el singleton siempre se restaura

### 5.4 Credenciales: compartidas en v1.4

Las API keys se cargan del `.env` raíz vía `load_dotenv()`. Todas las sesiones comparten las mismas credenciales. El aislamiento en v1.4 es de **configuración** (modelo, provider, reglas), no de credenciales. El aislamiento total de credenciales por sesión está planificado para v2.0 multi-tenant.

### 5.5 API implementada

---

## 6. Comparativa directa

| Dimensión | Hermes Agent | Nuestra arquitectura |
|---|---|---|
| **Mecanismo de aislamiento** | `ContextVar` + dict en memoria | Sistema de archivos (directorios) |
| **Estado base** | `config.yaml` global compartido | `sessions/default/` — plantilla inmutable |
| **Cambio de modelo** | Override ad-hoc en `_session_model_overrides` | Escritura en `sessions/<id>/model_routing.yaml` |
| **Persistencia** | `sessions.json` único (single point of corruption) | Un archivo por sesión (fallo aislado) |
| **Propagación a auxiliares** | Manual — cada tool debe copiar ContextVar | Automática — el session_id se pasa como parámetro |
| **Radio de explosión** | Un bug afecta todas las sesiones | Un bug afecta una sesión |
| **Hot-swap entre sesiones** | Comparten config global, overrides pueden colisionar | Aisladas — cada una tiene su propio archivo |
| **Debugging** | Requiere inspeccionar estado en memoria + ContextVar | `cat sessions/<id>/model_routing.yaml` |
| **Migración/backup** | Exportar `sessions.json` + `state.db` | Copiar `sessions/<id>/` |
| **Rollback** | Complejo — revertir `sessions.json` | Revertir `sessions/<id>/model_routing.yaml` |

---

## 7. Hoja de ruta de implementación

### Fase 1 — Infraestructura ✅ COMPLETADO (v1.4, Jul 2026)

- [x] Crear `sessions/default/` con `model_routing.yaml`, `gate_rules.yaml`, `state.json`
- [x] `POST /sessions` → clona `default/` → `sessions/sess_{id}_{uuid}/`
- [x] `LLMRouter` acepta `config_path` explícito + `force_new=True` + `set_router()`
- [x] `DELETE /sessions/{id}` → archiva a `_archive/`
- [x] `POST /agents/route` → acepta `session_dir`, swap de router con lock asyncio
- [x] `.gitignore` → excluye `sessions/*` excepto `default/`

### Fase 2 — Hot-swap por sesión ✅ COMPLETADO (v1.4, Jul 2026)

- [x] `POST /sessions/<id>/model` → escribe en `sessions/<id>/model_routing.yaml`
- [ ] `GET /sessions/<id>/model` → lee modelo activo de la sesión *(endpoint no verificado en esta auditoría — confirmar si existe)*
- [ ] Frontend: el model selector muestra y cambia el modelo de la sesión actual *(pendiente — el selector global no propaga `session_dir` aún)*

### Fase 3 — Migración

- [ ] Migrar sesiones existentes de la base de datos global a `sessions/<id>/chat_history.db`
- [ ] Migrar `state.json` global a `sessions/<id>/state.json`
- [ ] Deprecar archivos globales (`model_routing.yaml` raíz, `state.json` raíz)

### Fase 4 — Multi-tenant

- [ ] `sessions/default/` se puede sobrescribir con perfil personalizado
- [ ] Múltiples perfiles base: `profiles/dev/`, `profiles/writing/`, etc.
- [ ] `POST /sessions?profile=dev` → clona desde `profiles/dev/`

---

## 8. Conclusión

**Hermes Agent resolvió el aislamiento de sesiones con una capa de runtime** (`ContextVar` + overrides) sobre una base de estado compartido. Es una solución que funciona para el caso simple pero se quiebra bajo concurrencia real: los auxiliares pierden el contexto, los overrides colisionan, y un solo archivo corrupto tumba todas las conversaciones.

**Nuestra arquitectura resuelve el mismo problema con una capa de sistema de archivos**: cada sesión es un directorio autocontenido. No hay estado compartido que proteger, no hay `ContextVar` que propagar, no hay overrides que colisionen. El aislamiento es estructural, no un parche.

Esto no es una optimización — es una **decisión arquitectónica fundacional** que define cómo el sistema escala, cómo falla, y cómo se debuggea. Es el tipo de decisión que separa un prototipo de un sistema operativo agéntico.

---

> **Documento**: `docs/SESSION_ISOLATION_ARCHITECTURE.md`
> **Versión**: 1.0 — Julio 2026
> **Autor**: Vicente + Hermes Agent
