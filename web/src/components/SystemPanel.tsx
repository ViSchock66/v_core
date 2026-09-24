import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useChat } from '@/state/chat'
import { Chip, Icon, ICONS } from '@/components/ui'

function Section({ title, children, hint }: { title: string; children: React.ReactNode; hint?: string }) {
  return (
    <section className="border-b border-b-subtle px-3 py-3">
      <div className="mb-2 flex items-baseline gap-2">
        <h2 className="font-mono text-[10px] tracking-widest text-t-tertiary uppercase">{title}</h2>
        {hint && <span className="text-[9px] text-t-tertiary">{hint}</span>}
      </div>
      {children}
    </section>
  )
}

function Row({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="flex items-baseline gap-2 py-0.5 text-[11px]">
      <span className="w-28 shrink-0 truncate text-t-tertiary">{label}</span>
      <span className={`min-w-0 flex-1 truncate font-mono ${tone ?? 'text-t-secondary'}`}>
        {value}
      </span>
    </div>
  )
}

/**
 * Panel de sistema.
 *
 * Es el material que el frontend anterior nunca mostró: resiliencia real
 * (circuit breakers por provider), gasto por modelo, las decisiones del motor
 * de política con su regla, y el estado de la capa de embeddings. Sin esto, la
 * ingeniería existe pero es invisible.
 */
export function SystemPanel() {
  const activeThread = useChat((s) => s.activeThread)
  const chat = useChat((s) => s.chat)

  const models = useQuery({ queryKey: ['models'], queryFn: api.models, refetchInterval: 20000 })
  const circuits = useQuery({
    queryKey: ['circuits'],
    queryFn: api.circuitStatus,
    refetchInterval: 15000,
  })
  const usage = useQuery({ queryKey: ['usage'], queryFn: () => api.usage(200), refetchInterval: 20000 })
  const gate = useQuery({ queryKey: ['gate-log'], queryFn: () => api.gateLog(60), refetchInterval: 15000 })
  const embeddings = useQuery({
    queryKey: ['embeddings'],
    queryFn: api.embeddings,
    refetchInterval: 60000,
  })
  const policy = useQuery({ queryKey: ['policy'], queryFn: api.policy, staleTime: 120000 })

  const thread = activeThread ? chat.threads[activeThread] : null
  const runCount = thread?.runs.length ?? 0
  const eventCount = thread?.lastSeq ?? 0

  // Gasto agregado por modelo: lo que el frontend anterior resumía en un
  // contador de "tokens" que en realidad sumaba el histórico completo de la
  // base, no la sesión.
  const perModel = new Map<string, { in: number; out: number; calls: number }>()
  for (const u of usage.data ?? []) {
    const k = u.model ?? 'desconocido'
    const acc = perModel.get(k) ?? { in: 0, out: 0, calls: 0 }
    acc.in += u.tokens_in ?? 0
    acc.out += u.tokens_out ?? 0
    acc.calls += 1
    perModel.set(k, acc)
  }

  return (
    <div className="h-full overflow-y-auto">
      <Section title="Conversación" hint="lo que la UI reconstruye del log">
        <Row label="hilo" value={activeThread ?? '—'} />
        <Row label="runs" value={runCount} />
        <Row label="último seq" value={eventCount} />
        <Row
          label="artefactos"
          value={thread?.runs.reduce((n, r) => n + r.artifacts.length, 0) ?? 0}
        />
        <Row
          label="tools usadas"
          value={thread?.runs.reduce((n, r) => n + r.tools.length, 0) ?? 0}
        />
      </Section>

      <Section title="Modelos por rol" hint="hot-swap en caliente">
        {models.isError && (
          <div className="text-[11px] text-danger">{(models.error as Error).message}</div>
        )}
        {models.data &&
          Object.entries(models.data).map(([role, m]) => (
            <div key={role} className="flex items-center gap-2 py-0.5 text-[11px]">
              <span className="w-28 shrink-0 truncate text-t-tertiary">{role}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-t-secondary" title={m.model}>
                {m.model.split('/').pop()}
              </span>
              <Chip tone={m.circuit_breaker === 'OPEN' ? 'danger' : 'success'}>
                {m.circuit_breaker?.toLowerCase() ?? '—'}
              </Chip>
            </div>
          ))}
      </Section>

      <Section title="Circuit breakers" hint="aislamiento por provider">
        {circuits.data &&
          Object.entries(circuits.data).map(([prov, info]) => (
            <div key={prov} className="flex items-center gap-2 py-0.5 text-[11px]">
              <span className="w-28 shrink-0 truncate text-t-tertiary">{prov}</span>
              <span className="ml-auto">
                <Chip
                  tone={
                    info.state === 'OPEN'
                      ? 'danger'
                      : info.state === 'HALF-OPEN'
                        ? 'warning'
                        : 'success'
                  }
                >
                  {info.state.toLowerCase()}
                </Chip>
              </span>
            </div>
          ))}
      </Section>

      <Section title="Gasto por modelo" hint={`${usage.data?.length ?? 0} llamadas`}>
        {[...perModel.entries()]
          .sort((a, b) => b[1].in + b[1].out - (a[1].in + a[1].out))
          .slice(0, 6)
          .map(([model, v]) => (
            <Row
              key={model}
              label={model.split('/').pop() ?? model}
              value={`${v.calls}× · ${(v.in + v.out).toLocaleString()} tok`}
            />
          ))}
        {perModel.size === 0 && <div className="text-[11px] text-t-tertiary">sin datos aún</div>}
      </Section>

      <Section title="Decisiones de política" hint={`${gate.data?.length ?? 0} últimas`}>
        {(gate.data ?? []).slice(0, 12).map((g, i) => (
          <div key={i} className="flex items-start gap-2 py-0.5 text-[10px]">
            <Chip tone={g.effect === 'deny' ? 'danger' : g.effect === 'ask' ? 'warning' : 'success'}>
              {g.effect ?? (g.auto_approved ? 'permit' : 'ask')}
            </Chip>
            <span className="w-24 shrink-0 truncate font-mono text-t-secondary" title={g.tool}>
              {g.tool}
            </span>
            <span className="min-w-0 flex-1 truncate text-t-tertiary" title={g.reason}>
              {g.rule_id ?? g.reason}
            </span>
          </div>
        ))}
        {gate.data?.length === 0 && <div className="text-[11px] text-t-tertiary">sin decisiones</div>}
      </Section>

      <Section title="Embeddings" hint="capa vectorial">
        {embeddings.data && (
          <>
            <Row
              label="tier activo"
              value={`${embeddings.data.probe.active.tier} · ${embeddings.data.probe.active.dims}d`}
              tone="text-success"
            />
            <Row
              label="modelo"
              value={embeddings.data.probe.active.model.split('/').pop() ?? ''}
            />
            <Row
              label="memoria"
              value={
                embeddings.data.memory
                  ? `${embeddings.data.memory.total_entries ?? '?'} recuerdos · idx ${embeddings.data.memory.chromadb_docs ?? '?'}`
                  : '—'
              }
            />
            <Row
              label="knowledge"
              value={
                embeddings.data.knowledge
                  ? `${embeddings.data.knowledge.docs ?? '?'} docs`
                  : '—'
              }
            />
            {embeddings.data.probe.tiers.map((t) => (
              <div key={t.tier} className="flex items-center gap-2 py-0.5 text-[10px]">
                <span className="w-28 shrink-0 text-t-tertiary">{t.tier}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-t-tertiary">
                  {t.ok ? `${t.dims}d` : 'no disponible'}
                </span>
                <Chip tone={t.ok ? 'success' : 'neutral'}>{t.ok ? 'on' : 'off'}</Chip>
              </div>
            ))}
          </>
        )}
      </Section>

      <Section title="Política" hint="motor determinístico">
        {policy.data && (
          <>
            <Row label="raíz de trabajo" value={policy.data.workspace_dir} />
            <Row label="tools" value={Object.keys(policy.data.tools).length} />
            <Row label="binarios permitidos" value={Object.keys(policy.data.command_allowlist).length} />
            <Row
              label="red"
              value={String((policy.data.perimeter as { network?: string })?.network ?? '—')}
              tone="text-warning"
            />
            <details className="mt-1.5">
              <summary className="cursor-pointer text-[10px] text-t-tertiary select-none">
                ver reglas completas
              </summary>
              <pre className="mt-1 max-h-64 overflow-auto rounded-sm bg-surf-code p-2 font-mono text-[9px] text-t-tertiary">
                {JSON.stringify(
                  { tools: policy.data.tools, allowlist: policy.data.command_allowlist },
                  null,
                  2,
                )}
              </pre>
            </details>
          </>
        )}
      </Section>

      <Section title="Contexto de riesgo" hint="qué nunca se auto-aprueba">
        <div className="space-y-1 text-[10px] text-t-tertiary">
          {['red (egress)', 'lectura de secretos', 'operaciones destructivas', 'push a rama protegida', 'instalación de paquetes'].map(
            (x) => (
              <div key={x} className="flex items-center gap-1.5">
                <Icon path={ICONS.shield} size={10} />
                {x}
              </div>
            ),
          )}
        </div>
      </Section>
    </div>
  )
}
