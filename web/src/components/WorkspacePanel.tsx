import { useQuery } from '@tanstack/react-query'
import { sessions as sessionsApi } from '@/lib/api'
import { useChat } from '@/state/chat'
import { Chip, Icon, ICONS } from '@/components/ui'

function FileList({
  items,
  empty,
  onOpen,
}: {
  items: Array<{ name: string; type: string; size: number | null }>
  empty: string
  onOpen?: (name: string) => void
}) {
  if (items.length === 0) return <div className="px-3 py-1.5 text-[10px] text-t-tertiary">{empty}</div>
  return (
    <div className="px-1">
      {items.map((f) => (
        <button
          key={f.name}
          type="button"
          onClick={() => onOpen?.(f.name)}
          className="flex w-full items-center gap-2 rounded-sm px-2 py-0.5 text-left hover:bg-surf-hover"
        >
          <Icon path={f.type === 'directory' ? ICONS.folder : ICONS.file} size={11} />
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-t-secondary">
            {f.name}
          </span>
          {f.size !== null && (
            <span className="shrink-0 font-mono text-[9px] text-t-tertiary">
              {f.size < 1000 ? `${f.size} B` : `${(f.size / 1024).toFixed(1)} KB`}
            </span>
          )}
        </button>
      ))}
    </div>
  )
}

/**
 * Workspace de la sesión activa.
 *
 * En el frontend anterior esto no existía: `sessions/<id>/` guardaba solo
 * configuración y el agente escribía en la raíz del repo, porque no tenía un
 * espacio de trabajo propio. Acá se muestra el directorio real (`work/`), lo
 * que produjo (`artifacts/`) y los metadatos de la sesión, incluido el modelo
 * lead con que se creó.
 */
export function WorkspacePanel() {
  const activeThread = useChat((s) => s.activeThread)
  const setOpenArtifact = useChat((s) => s.openArtifact)
  const chat = useChat((s) => s.chat)

  const ws = useQuery({
    queryKey: ['workspace', activeThread],
    queryFn: () => sessionsApi.workspace(activeThread as string),
    enabled: Boolean(activeThread),
  })

  if (!activeThread) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-center text-[11px] text-t-tertiary">
        Seleccioná una conversación para ver su workspace.
      </div>
    )
  }

  const thread = chat.threads[activeThread]
  const artifactsFromEvents = thread?.runs.flatMap((r) => r.artifacts) ?? []

  return (
    <div className="h-full overflow-y-auto">
      <section className="border-b border-b-subtle px-3 py-3">
        <h2 className="mb-2 font-mono text-[10px] tracking-widest text-t-tertiary uppercase">
          Sesión
        </h2>
        <div className="space-y-0.5 text-[11px]">
          <div className="flex gap-2">
            <span className="w-24 shrink-0 text-t-tertiary">id</span>
            <span className="font-mono text-t-secondary">{ws.data?.session_id ?? '—'}</span>
          </div>
          <div className="flex gap-2">
            <span className="w-24 shrink-0 text-t-tertiary">directorio</span>
            <span className="min-w-0 truncate font-mono text-t-secondary" title={activeThread}>
              {activeThread}
            </span>
          </div>
          <div className="flex gap-2">
            <span className="w-24 shrink-0 text-t-tertiary">aislado</span>
            <Chip tone={ws.data?.exists ? 'success' : 'warning'}>
              {ws.data?.exists ? 'sí' : 'no'}
            </Chip>
          </div>
          {typeof ws.data?.meta?.lead_model === 'string' && (
            <div className="flex gap-2">
              <span className="w-24 shrink-0 text-t-tertiary">modelo lead</span>
              <span className="min-w-0 truncate font-mono text-t-secondary">
                {ws.data.meta.lead_model}
              </span>
            </div>
          )}
        </div>
      </section>

      <section className="border-b border-b-subtle px-3 py-3">
        <h2 className="mb-1 flex items-center gap-2 font-mono text-[10px] tracking-widest text-t-tertiary uppercase">
          Directorio de trabajo
          <span className="font-normal normal-case">work/</span>
        </h2>
        <p className="mb-2 font-mono text-[9px] break-all text-t-tertiary">
          {ws.data?.work_dir ?? '—'}
        </p>
        <FileList
          items={ws.data?.work_files ?? []}
          empty="vacío: el agente todavía no escribió nada acá"
        />
      </section>

      <section className="border-b border-b-subtle px-3 py-3">
        <h2 className="mb-1 flex items-center gap-2 font-mono text-[10px] tracking-widest text-t-tertiary uppercase">
          Artefactos
          <span className="font-normal normal-case">artifacts/</span>
        </h2>
        <p className="mb-2 font-mono text-[9px] break-all text-t-tertiary">
          {ws.data?.artifacts_dir ?? '—'}
        </p>
        <FileList
          items={ws.data?.artifacts ?? []}
          empty="sin archivos en disco todavía"
          onOpen={(name) => {
            const found = artifactsFromEvents.find((a) => a.title === name || a.path?.endsWith(name))
            if (found) setOpenArtifact(found.key)
          }}
        />
      </section>

      <section className="px-3 py-3">
        <h2 className="mb-2 font-mono text-[10px] tracking-widest text-t-tertiary uppercase">
          Artefactos de esta conversación
        </h2>
        {artifactsFromEvents.length === 0 ? (
          <div className="text-[10px] text-t-tertiary">
            sin artefactos: el agente no escribió archivos en este hilo
          </div>
        ) : (
          <div className="px-1">
            {artifactsFromEvents.map((a) => (
              <button
                key={a.key}
                type="button"
                onClick={() => setOpenArtifact(a.key)}
                className="flex w-full items-center gap-2 rounded-sm px-2 py-0.5 text-left hover:bg-surf-hover"
              >
                <Icon path={ICONS.file} size={11} />
                <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-t-secondary">
                  {a.title}
                </span>
                <Chip>{a.kind}</Chip>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
