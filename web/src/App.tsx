import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Composer } from '@/components/Composer'
import { RunList } from '@/components/RunList'
import { Sidebar } from '@/components/Sidebar'
import { ArtifactViewer } from '@/components/ArtifactViewer'
import { SystemPanel } from '@/components/SystemPanel'
import { WorkspacePanel } from '@/components/WorkspacePanel'
import { useChat, restoreActiveThread } from '@/state/chat'
import { useApplyTheme } from '@/state/theme'
import { Button, Chip, Icon, ICONS, cx } from '@/components/ui'
import type { Artifact } from '@/lib/reduce'

const PANELS = [
  { id: 'artifacts', label: 'Artefactos', icon: ICONS.code },
  { id: 'system', label: 'Sistema', icon: ICONS.activity },
  { id: 'workspace', label: 'Workspace', icon: ICONS.folder },
] as const

function ConnectionBadge() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 10000 })
  const ok = health.isSuccess
  return (
    <span className="flex items-center gap-1.5" title={ok ? 'backend conectado' : 'backend sin responder'}>
      <span className={cx('h-1.5 w-1.5 rounded-full', ok ? 'bg-success' : 'bg-danger')} />
      <span className="font-mono text-[10px] text-t-tertiary">
        {ok ? `v${health.data?.version ?? ''}` : 'sin conexión'}
      </span>
    </span>
  )
}

export default function App() {
  useApplyTheme()

  // Restaura el hilo que se estaba viendo antes de recargar. Dispara el replay
  // desde el log de eventos, que es lo que hace que la conversación reaparezca
  // completa en vez de vacía.
  useEffect(() => {
    void restoreActiveThread()
  }, [])

  const sidebarOpen = useChat((s) => s.sidebarOpen)
  const toggleSidebar = useChat((s) => s.toggleSidebar)
  const panel = useChat((s) => s.panel)
  const setPanel = useChat((s) => s.setPanel)
  const activeThread = useChat((s) => s.activeThread)
  const chat = useChat((s) => s.chat)
  const streaming = useChat((s) => s.streaming)
  const error = useChat((s) => s.error)
  const dismissError = useChat((s) => s.dismissError)
  const send = useChat((s) => s.send)
  const cancel = useChat((s) => s.cancel)
  const setOpenArtifact = useChat((s) => s.openArtifact)

  const models = useQuery({ queryKey: ['models'], queryFn: api.models, refetchInterval: 20000 })
  const lead = models.data?.enlil_lead

  const thread = activeThread ? chat.threads[activeThread] : null
  const runs = thread?.runs ?? []

  function onSend(text: string) {
    void send(text, activeThread)
  }

  function onOpenArtifact(a: Artifact) {
    setOpenArtifact(a.key)
  }

  return (
    <div className="flex h-full w-full overflow-hidden bg-surf-chat">
      {sidebarOpen && <Sidebar />}

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-9 shrink-0 items-center gap-3 border-b border-b-subtle px-3">
          <button
            type="button"
            onClick={toggleSidebar}
            aria-label={sidebarOpen ? 'Ocultar sidebar' : 'Mostrar sidebar'}
            className="text-t-tertiary hover:text-t-primary"
          >
            <Icon path={ICONS.chevronRight} size={13} />
          </button>

          <ConnectionBadge />

          {activeThread && (
            <span className="truncate font-mono text-[10px] text-t-tertiary" title={activeThread}>
              {activeThread}
            </span>
          )}

          <div className="ml-auto flex items-center gap-1">
            {PANELS.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => setPanel(p.id)}
                aria-pressed={panel === p.id}
                title={p.label}
                className={cx(
                  'flex items-center gap-1.5 rounded-sm px-2 py-1 text-[11px]',
                  panel === p.id
                    ? 'bg-accent-dim text-accent'
                    : 'text-t-tertiary hover:bg-surf-hover hover:text-t-secondary',
                )}
              >
                <Icon path={p.icon} size={12} />
                {p.label}
              </button>
            ))}
          </div>
        </header>

        {error && (
          <div className="flex items-center gap-2 border-b border-b-subtle bg-surf-panel px-3 py-1.5">
            <Icon path={ICONS.alert} size={12} />
            <span className="min-w-0 flex-1 truncate text-[11px] text-danger">{error}</span>
            <Button size="sm" onClick={dismissError}>
              cerrar
            </Button>
          </div>
        )}

        <RunList
          runs={runs}
          onOpenArtifact={onOpenArtifact}
          emptyLabel={
            activeThread
              ? 'Conversación vacía. Escribí un mensaje para empezar.'
              : 'Creá una conversación para empezar.'
          }
        />

        <Composer
          onSend={onSend}
          onCancel={cancel}
          streaming={streaming}
          modelLabel={lead?.model?.split('/').pop()}
        />
      </main>

      {panel !== 'chat' && (
        <aside className="flex h-full w-[380px] shrink-0 flex-col border-l border-b-subtle bg-surf-panel">
          <div className="flex h-9 shrink-0 items-center gap-2 border-b border-b-subtle px-3">
            <span className="font-mono text-[10px] tracking-widest text-t-tertiary uppercase">
              {PANELS.find((p) => p.id === panel)?.label}
            </span>
            {panel === 'system' && lead?.circuit_breaker === 'OPEN' && (
              <Chip tone="danger">circuito abierto</Chip>
            )}
            <button
              type="button"
              onClick={() => setPanel('chat')}
              aria-label="Cerrar panel"
              className="ml-auto text-t-tertiary hover:text-t-primary"
            >
              <Icon path={ICONS.x} size={12} />
            </button>
          </div>

          <div className="min-h-0 flex-1">
            {panel === 'artifacts' && <ArtifactViewer />}
            {panel === 'system' && <SystemPanel />}
            {panel === 'workspace' && <WorkspacePanel />}
          </div>
        </aside>
      )}
    </div>
  )
}
