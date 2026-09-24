import { useEffect, useRef, useState } from 'react'
import type { Artifact, Run } from '@/lib/reduce'
import { ApprovalCard } from '@/components/ApprovalCard'
import { ToolCallCard } from '@/components/ToolCallCard'
import { ArtifactCard, AssistantText, ReasoningBlock } from '@/components/RunView'
import { Chip, cx } from '@/components/ui'

function timeOf(ts?: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/**
 * Un turno completo: lo que pidió el usuario y todo lo que el agente hizo.
 *
 * El orden de los bloques es el orden en que ocurrieron (razonamiento → tools →
 * aprobaciones → artefactos → texto), y por eso el mensaje se lee como una
 * traza de lo que pasó y no como una caja negra.
 */
function RunItem({ run, onOpenArtifact }: { run: Run; onOpenArtifact: (a: Artifact) => void }) {
  const streaming = run.status === 'running'

  return (
    <div className="mb-6 flex flex-col gap-2">
      {run.prompt && (
        <div className="flex justify-end">
          <div
            className="max-w-[80%] rounded-lg border px-3 py-2 text-[13px] whitespace-pre-wrap"
            style={{
              background: 'var(--msg-user-bg)',
              borderColor: 'var(--msg-user-border)',
            }}
          >
            {run.prompt}
          </div>
        </div>
      )}

      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2 text-[10px] text-t-tertiary">
          <span className="font-mono">{timeOf(run.startedAt)}</span>
          {run.model && (
            <span className="font-mono" title={run.model}>
              {run.model.split('/').pop()}
            </span>
          )}
          <Chip
            tone={
              run.status === 'done'
                ? 'success'
                : run.status === 'error'
                  ? 'danger'
                  : run.status === 'cancelled'
                    ? 'warning'
                    : 'accent'
            }
          >
            {run.status === 'running' ? 'en curso' : run.status}
          </Chip>
        </div>

        <ReasoningBlock text={run.reasoning} streaming={streaming} />

        {run.tools.map((t) => (
          <ToolCallCard key={t.key} call={t} />
        ))}

        {run.approvals.map((a) => (
          <ApprovalCard key={a.requestId} approval={a} />
        ))}

        {run.artifacts.map((a) => (
          <ArtifactCard key={a.key} artifact={a} onOpen={onOpenArtifact} />
        ))}

        {run.error && (
          <div className="rounded-md border border-b-default p-2.5 text-[12px] text-danger">
            {run.error}
          </div>
        )}

        <AssistantText text={run.text} streaming={streaming} />

        {streaming && !run.text && !run.reasoning && run.tools.length === 0 && (
          <div className="flex items-center gap-2 text-[11px] text-t-tertiary">
            <span className="vc-pulse h-1.5 w-1.5 rounded-full bg-accent" />
            pensando…
          </div>
        )}
      </div>
    </div>
  )
}

export function RunList({
  runs,
  onOpenArtifact,
  emptyLabel,
}: {
  runs: Run[]
  onOpenArtifact: (a: Artifact) => void
  emptyLabel?: string
}) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const [pinned, setPinned] = useState(true)

  // Auto-scroll solo cuando el usuario ya está abajo. Si scrolleó hacia arriba
  // para leer algo, la vista no se le mueve bajo los pies — que es el bug más
  // molesto de un chat con streaming.
  useEffect(() => {
    if (pinned) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [runs, pinned])

  function onScroll() {
    const el = scrollerRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    setPinned(atBottom)
  }

  return (
    <div
      ref={scrollerRef}
      onScroll={onScroll}
      className="relative flex-1 overflow-y-auto px-4 py-4"
      role="log"
      aria-live="polite"
    >
      <div className="mx-auto max-w-3xl">
        {runs.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center py-20 text-center">
            <div className="mb-2 font-mono text-lg tracking-[0.3em] text-t-tertiary">V—CORE</div>
            <p className="text-[13px] text-t-secondary">
              {emptyLabel ?? 'Escribe un mensaje para empezar'}
            </p>
          </div>
        )}

        {runs.map((r) => (
          <RunItem key={r.runId} run={r} onOpenArtifact={onOpenArtifact} />
        ))}
        <div ref={bottomRef} />
      </div>

      {!pinned && (
        <button
          type="button"
          onClick={() => setPinned(true)}
          className={cx(
            'sticky bottom-2 mx-auto block rounded-full border border-b-default',
            'bg-surf-panel px-3 py-1 text-[11px] text-t-secondary shadow-md hover:text-t-primary',
          )}
        >
          ir al final
        </button>
      )}
    </div>
  )
}
