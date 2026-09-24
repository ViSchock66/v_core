import { useState } from 'react'
import type { ToolCall } from '@/lib/reduce'
import { Chip, Icon, ICONS, cx, riskTone } from '@/components/ui'

/**
 * Tarjeta de una tool call.
 *
 * Muestra lo que el usuario necesita para confiar o desconfiar de la acción:
 * qué tool, con qué argumentos, cuánto tardó, si falló, y —cuando la política
 * la denegó— la regla exacta y el motivo. La regla es lo que hace la decisión
 * explicable en vez de opaca.
 */
export function ToolCallCard({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(false)

  const tone =
    call.status === 'ok'
      ? 'success'
      : call.status === 'error'
        ? 'danger'
        : call.status === 'denied'
          ? 'warning'
          : 'accent'

  const label =
    call.status === 'running'
      ? 'ejecutando'
      : call.status === 'ok'
        ? 'ok'
        : call.status === 'denied'
          ? 'denegada'
          : 'error'

  // El argumento que más importa según la tool, para el resumen de una línea.
  const summary = (() => {
    const a = call.args
    const first =
      (a.directory as string) ??
      (a.path as string) ??
      (a.query as string) ??
      (a.command as string) ??
      (a.pattern as string) ??
      ''
    return String(first).slice(0, 90)
  })()

  const hasDetail = Boolean(call.result || call.error || call.reason || call.reason)

  return (
    <div className="vc-enter rounded-md border border-b-subtle bg-surf-card/40">
      <button
        type="button"
        onClick={() => hasDetail && setOpen((v) => !v)}
        className={cx(
          'flex w-full items-center gap-2 px-2.5 py-1.5 text-left',
          hasDetail && 'cursor-pointer hover:bg-surf-hover',
        )}
        aria-expanded={open}
      >
        <Icon path={ICONS.terminal} size={12} />
        <span className="font-mono text-[11px] text-t-primary">{call.tool}</span>
        {summary && (
          <span className="truncate font-mono text-[11px] text-t-tertiary">{summary}</span>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {typeof call.durationMs === 'number' && call.durationMs > 0 && (
            <span className="font-mono text-[10px] text-t-tertiary">{call.durationMs}ms</span>
          )}
          <Chip tone={tone}>{label}</Chip>
        </span>
      </button>

      {call.status === 'denied' && (
        <div className="border-t border-b-subtle px-2.5 py-2">
          <div className="flex items-center gap-2">
            <Chip tone={riskTone(call.risk)}>{call.risk ?? 'riesgo?'}</Chip>
            {call.ruleId && (
              <span className="font-mono text-[10px] text-t-tertiary">regla: {call.ruleId}</span>
            )}
          </div>
          {call.reason && <p className="mt-1.5 text-[11px] text-t-secondary">{call.reason}</p>}
          <p className="mt-1 text-[10px] text-t-tertiary">
            La política no autorizó esta acción. No se ejecutó.
          </p>
        </div>
      )}

      {open && call.status !== 'denied' && (
        <div className="border-t border-b-subtle px-2.5 py-2">
          {Object.keys(call.args).length > 0 && (
            <pre className="mb-2 overflow-x-auto rounded-sm bg-surf-code p-2 font-mono text-[10px] text-t-secondary">
              {JSON.stringify(call.args, null, 2)}
            </pre>
          )}
          {(call.result || call.error) && (
            <pre className="max-h-72 overflow-auto rounded-sm bg-surf-code p-2 font-mono text-[10px] whitespace-pre-wrap text-t-secondary">
              {call.error ?? call.result}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}
