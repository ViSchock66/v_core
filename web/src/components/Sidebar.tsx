import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, sessions as sessionsApi, type AvailableModel, type Session } from '@/lib/api'
import { useChat, startNewSession } from '@/state/chat'
import { ACCENTS, useTheme, type Accent } from '@/state/theme'
import { Button, Chip, Icon, ICONS, cx } from '@/components/ui'

/** Los roles que tienen modelo propio y son cambiables desde la UI. */
const EDITABLE_ROLES: Array<{ id: string; label: string; hint: string }> = [
  { id: 'orchestrator_lead', label: 'Lead', hint: 'Orquestación y conversación' },
  { id: 'orchestrator_council', label: 'Council', hint: 'Segunda opinión en paralelo' },
  { id: 'orchestrator_escalation', label: 'Escalation', hint: 'Casos complejos' },
  { id: 'planner_plan', label: 'Planner plan', hint: 'Planificación de cambios' },
  { id: 'planner_apply', label: 'Planner apply', hint: 'Aplicación de diffs' },
]

/**
 * Selector de modelos.
 *
 * El frontend anterior mostraba el nombre del modelo sin decir de qué **rol**
 * era, permitía cambiar solo `orchestrator_lead`, y pedía el catálogo con
 * `provider=nvidia` hardcodeado. Acá se muestra rol + modelo + estado del
 * circuit breaker, y se puede cambiar cualquier rol con modelo propio.
 *
 * El hot-swap es real: `POST /system/model` reescribe el YAML y el backend usa
 * el modelo nuevo en la siguiente llamada, sin reiniciar.
 */
function ModelPicker() {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const qc = useQueryClient()

  const models = useQuery({ queryKey: ['models'], queryFn: api.models, refetchInterval: 20000 })
  const catalog = useQuery({
    queryKey: ['models-available'],
    queryFn: () => api.availableModels(),
    enabled: open,
    staleTime: 60000,
  })

  const lead = models.data?.orchestrator_lead

  async function swap(role: string, modelId: string) {
    setBusy(role)
    try {
      await api.swapModel(role, modelId)
      await qc.invalidateQueries({ queryKey: ['models'] })
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="relative px-2 pb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded-md border border-b-default bg-surf-card px-2 py-1.5 text-left hover:bg-surf-hover"
      >
        <span
          className={cx(
            'h-1.5 w-1.5 shrink-0 rounded-full',
            lead?.circuit_breaker === 'OPEN' ? 'bg-danger' : 'bg-success',
          )}
        />
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-mono text-[11px] text-t-primary">
            {lead?.model?.split('/').pop() ?? 'cargando…'}
          </span>
          <span className="text-[9px] text-t-tertiary">rol: lead</span>
        </span>
        <Icon path={ICONS.chevronDown} size={12} />
      </button>

      {open && (
        <div className="absolute right-2 left-2 z-30 mt-1 rounded-md border border-b-strong bg-surf-panel p-2 shadow-lg">
          <div className="mb-2 text-[10px] text-t-tertiary">Modelo por rol (hot-swap)</div>

          {EDITABLE_ROLES.map((role) => {
            const current = models.data?.[role.id]
            return (
              <div key={role.id} className="mb-2">
                <div className="mb-1 flex items-center gap-1.5">
                  <span className="text-[10px] text-t-secondary">{role.label}</span>
                  {current?.circuit_breaker === 'OPEN' && <Chip tone="danger">cb abierto</Chip>}
                  <span className="ml-auto font-mono text-[9px] text-t-tertiary">
                    {current?.model?.split('/').pop() ?? '—'}
                  </span>
                </div>
                <select
                  value={current?.model ?? ''}
                  disabled={busy === role.id || !catalog.data}
                  onChange={(e) => void swap(role.id, e.target.value)}
                  className="w-full rounded-sm border border-b-default bg-surf-input px-1.5 py-1 font-mono text-[10px] text-t-primary"
                  title={role.hint}
                >
                  {!catalog.data && <option value={current?.model ?? ''}>cargando catálogo…</option>}
                  {catalog.data?.models.map((m: AvailableModel) => (
                    <option key={m.id} value={m.id}>
                      {m.name}
                      {m.reasoning ? ' ◆' : ''}
                    </option>
                  ))}
                </select>
              </div>
            )
          })}

          <div className="mt-2 border-t border-b-subtle pt-2 text-[9px] text-t-tertiary">
            ◆ indica modelo de razonamiento: emite cadena de pensamiento en un canal aparte.
          </div>
        </div>
      )}
    </div>
  )
}

function SessionRow({ s, active }: { s: Session; active: boolean }) {
  const qc = useQueryClient()
  const selectThread = useChat((st) => st.selectThread)
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(s.title ?? '')

  const key = s.session_dir ?? String(s.id)
  const label = s.title && s.title !== 'Nueva conversación' ? s.title : `Sesión ${s.id}`
  const hasEvents = s.session_dir != null

  async function save() {
    setEditing(false)
    const next = title.trim()
    if (!next || next === s.title) return
    await sessionsApi.rename(key, next)
    await qc.invalidateQueries({ queryKey: ['sessions'] })
  }

  async function remove() {
    await sessionsApi.remove(key)
    await qc.invalidateQueries({ queryKey: ['sessions'] })
    if (active) selectThread(null)
  }

  return (
    <div
      className={cx(
        'group flex items-center gap-2 rounded-md px-2 py-1.5',
        active ? 'bg-accent-dim' : 'hover:bg-surf-hover',
      )}
    >
      <span
        className={cx('h-1.5 w-1.5 shrink-0 rounded-full', hasEvents ? 'bg-accent' : 'bg-t-tertiary')}
        title={hasEvents ? 'con workspace aislado' : 'sin directorio de sesión'}
      />
      {editing ? (
        <input
          autoFocus
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onBlur={() => void save()}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void save()
            if (e.key === 'Escape') setEditing(false)
          }}
          className="min-w-0 flex-1 rounded-sm border border-b-strong bg-surf-input px-1 py-0.5 text-[11px] text-t-primary"
        />
      ) : (
        <button
          type="button"
          onClick={() => selectThread(s.session_dir ?? String(s.id))}
          onDoubleClick={() => setEditing(true)}
          className="min-w-0 flex-1 truncate text-left text-[11px] text-t-secondary hover:text-t-primary"
          title={`${label} — ${key}`}
        >
          {label}
        </button>
      )}
      <button
        type="button"
        onClick={() => void remove()}
        className="hidden text-t-tertiary hover:text-danger group-hover:block"
        title="Eliminar sesión"
        aria-label={`Eliminar ${label}`}
      >
        <Icon path={ICONS.x} size={11} />
      </button>
    </div>
  )
}

export function Sidebar() {
  const activeThread = useChat((s) => s.activeThread)
  const selectThread = useChat((s) => s.selectThread)
  const theme = useTheme((s) => s.theme)
  const accent = useTheme((s) => s.accent)
  const setTheme = useTheme((s) => s.setTheme)
  const setAccent = useTheme((s) => s.setAccent)

  const [filter, setFilter] = useState('')
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: sessionsApi.list })

  // El backend devuelve las sesiones por id descendente. Se separan las que
  // tienen actividad real (eventos) de las vacías: en el frontend anterior el
  // sidebar eran 50 filas idénticas de "Nueva conversación".
  const list = (sessions.data ?? []).filter((s) => {
    if (!filter) return true
    const label = `${s.title ?? ''} ${s.session_dir ?? ''}`.toLowerCase()
    return label.includes(filter.toLowerCase())
  })

  return (
    <aside className="flex h-full min-h-0 w-60 shrink-0 flex-col border-r border-b-subtle bg-surf-sidebar">
      <div className="flex items-center gap-2 px-3 py-3">
        <span className="font-mono text-sm tracking-[0.25em] text-t-primary">V—CORE</span>
        <span className="ml-auto font-mono text-[9px] text-t-tertiary">2.0</span>
      </div>

      <div className="px-2 pb-2">
        <Button
          className="w-full justify-center"
          onClick={() => void startNewSession()}
          title="Nueva conversación"
        >
          <Icon path={ICONS.plus} size={12} />
          Nueva conversación
        </Button>
      </div>

      <ModelPicker />

      <div className="px-2 pb-1">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filtrar conversaciones…"
          className="w-full rounded-sm border border-b-subtle bg-surf-input px-2 py-1 text-[10px] text-t-primary placeholder:text-t-tertiary"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2">
        {sessions.isLoading && <div className="px-2 py-1 text-[10px] text-t-tertiary">cargando…</div>}
        {sessions.isError && (
          <div className="px-2 py-1 text-[10px] text-danger">
            no se pudo cargar: {(sessions.error as Error).message}
          </div>
        )}
        {list.map((s) => (
          <SessionRow
            key={s.id}
            s={s}
            active={activeThread === (s.session_dir ?? String(s.id))}
          />
        ))}
        {!sessions.isLoading && list.length === 0 && (
          <div className="px-2 py-1 text-[10px] text-t-tertiary">
            {filter ? 'sin coincidencias' : 'sin conversaciones'}
          </div>
        )}
      </div>

      <div className="border-t border-b-subtle p-2">
        <div className="mb-2 flex items-center gap-1.5">
          <span className="text-[10px] text-t-tertiary">Acento</span>
          <div className="ml-auto flex gap-1">
            {ACCENTS.map((a) => (
              <button
                key={a}
                type="button"
                onClick={() => setAccent(a as Accent)}
                title={a}
                aria-label={`Acento ${a}`}
                aria-pressed={accent === a}
                className={cx(
                  'h-3 w-3 rounded-full border',
                  accent === a ? 'border-t-secondary' : 'border-transparent',
                )}
                style={{ background: `var(--accent)`, opacity: accent === a ? 1 : 0.45 }}
              />
            ))}
          </div>
        </div>
        <Button
          className="w-full justify-center"
          onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
          title="Cambiar tema"
        >
          <Icon path={theme === 'dark' ? ICONS.sun : ICONS.moon} size={12} />
          {theme === 'dark' ? 'Tema claro' : 'Tema oscuro'}
        </Button>
        <button
          type="button"
          onClick={() => selectThread(null)}
          className="mt-1.5 w-full text-center text-[9px] text-t-tertiary hover:text-t-secondary"
        >
          {activeThread ? `hilo: ${activeThread}` : 'sin conversación activa'}
        </button>
      </div>
    </aside>
  )
}
