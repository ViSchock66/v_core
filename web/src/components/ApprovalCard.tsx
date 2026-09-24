import { useState } from 'react'
import { approvals as approvalsApi } from '@/lib/api'
import type { Approval } from '@/lib/reduce'
import { Button, Chip, Icon, ICONS, riskTone } from '@/components/ui'

/**
 * Tarjeta de aprobación humana (HITL).
 *
 * Es el punto donde el usuario decide si una acción se ejecuta. Muestra lo que
 * el motor de política evaluó —regla, riesgo y motivo— y los argumentos
 * redactados (el backend omite valores sensibles antes de enviarlos).
 *
 * Al resolver, el backend despierta el `asyncio.Future` donde el agente quedó
 * pausado y el stream continúa solo. El cliente NO hace polling: la resolución
 * es un POST y el mismo stream sigue abierto.
 */
export function ApprovalCard({ approval }: { approval: Approval }) {
  const [busy, setBusy] = useState(false)
  const [local, setLocal] = useState<string | null>(null)

  const settled = approval.status !== 'pending' || local !== null

  async function decide(approved: boolean) {
    setBusy(true)
    try {
      const r = await approvalsApi.resolve(approval.requestId, approved)
      // `resolved: false` significa que ya estaba resuelta (doble click o
      // reintento). No es un error: la idempotencia del backend evitó ejecutar
      // la acción dos veces.
      setLocal(r.resolved ? (approved ? 'approved' : 'rejected') : 'ya resuelta')
    } catch (e) {
      setLocal(`error: ${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  const finalTone =
    local === 'approved' || approval.status === 'approved'
      ? 'success'
      : local === 'rejected' || approval.status === 'rejected'
        ? 'danger'
        : 'neutral'

  return (
    <div className="vc-enter rounded-md border border-b-strong bg-surf-panel p-3">
      <div className="mb-2 flex items-center gap-2">
        <Icon path={ICONS.shield} size={13} />
        <span className="text-xs font-medium text-t-primary">Aprobación requerida</span>
        <span className="ml-auto flex items-center gap-2">
          <Chip tone={riskTone(approval.risk)}>{approval.risk}</Chip>
        </span>
      </div>

      <div className="mb-2 space-y-1">
        <div className="flex gap-2 text-[11px]">
          <span className="w-16 shrink-0 text-t-tertiary">agente</span>
          <span className="font-mono text-t-secondary">{approval.agentId ?? 'Orchestrator'}</span>
        </div>
        <div className="flex gap-2 text-[11px]">
          <span className="w-16 shrink-0 text-t-tertiary">acción</span>
          <span className="font-mono text-t-primary">{approval.tool}</span>
        </div>
        {approval.ruleId && (
          <div className="flex gap-2 text-[11px]">
            <span className="w-16 shrink-0 text-t-tertiary">regla</span>
            <span className="font-mono text-t-secondary">{approval.ruleId}</span>
          </div>
        )}
      </div>

      {approval.reason && (
        <p className="mb-2 rounded-sm bg-surf-code p-2 text-[11px] text-t-secondary">
          {approval.reason}
        </p>
      )}

      {Object.keys(approval.paramsPreview).length > 0 && (
        <details className="mb-2">
          <summary className="cursor-pointer text-[10px] text-t-tertiary select-none">
            ver parámetros
          </summary>
          <pre className="mt-1 max-h-40 overflow-auto rounded-sm bg-surf-code p-2 font-mono text-[10px] text-t-secondary">
            {JSON.stringify(approval.paramsPreview, null, 2)}
          </pre>
        </details>
      )}

      {settled ? (
        <div className="flex items-center gap-2">
          <Chip tone={finalTone}>
            {local === 'ya resuelta'
              ? 'ya resuelta'
              : local === 'approved' || approval.status === 'approved'
                ? 'aprobada'
                : local === 'rejected' || approval.status === 'rejected'
                  ? 'rechazada'
                  : String(local ?? approval.status)}
          </Chip>
          <span className="text-[10px] text-t-tertiary">
            {local?.startsWith('error') ? local : 'el agente ya recibió la decisión'}
          </span>
        </div>
      ) : (
        <div className="flex gap-2">
          <Button variant="accent" size="sm" disabled={busy} onClick={() => void decide(true)}>
            <Icon path={ICONS.check} size={12} />
            Aprobar
          </Button>
          <Button variant="danger" size="sm" disabled={busy} onClick={() => void decide(false)}>
            <Icon path={ICONS.x} size={12} />
            Rechazar
          </Button>
        </div>
      )}
    </div>
  )
}
