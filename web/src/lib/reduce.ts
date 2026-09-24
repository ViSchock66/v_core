/**
 * Reduce del log de eventos a estado de UI.
 *
 * Esta es la única función que convierte eventos en lo que el usuario ve. La
 * usan los DOS caminos:
 *
 *   - **vivo**: los eventos llegan por SSE y se aplican a medida que aparecen
 *   - **replay**: `GET /threads/{id}/events` devuelve el log completo y se
 *     aplica el mismo reduce, desde cero
 *
 * Como es la misma función, el historial no puede verse distinto a como se vio
 * en vivo. Eso es lo que el frontend anterior no lograba: guardaba solo el
 * texto final, así que al recargar desaparecían las tool cards, los artefactos
 * y las aprobaciones — información que existía pero no se persistía.
 *
 * El estado es un mapa `threadId -> ThreadState`. Un hilo es una lista de runs;
 * un run agrupa un mensaje del usuario y todo lo que produjo.
 */

import type { AgentEvent, EventType } from '@/lib/protocol'

export type RunStatus = 'running' | 'done' | 'error' | 'cancelled'
export type ToolStatus = 'running' | 'ok' | 'error' | 'denied'
export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'timeout'

export interface ToolCall {
  /** Clave estable para React: nombre + orden de aparición. */
  key: string
  tool: string
  args: Record<string, unknown>
  result?: string | null
  durationMs?: number
  error?: string | null
  status: ToolStatus
  ruleId?: string
  risk?: string
  reason?: string
  startedAt?: number
  endedAt?: number
}

export interface Approval {
  requestId: string
  tool: string
  agentId?: string
  ruleId?: string
  risk: string
  reason: string
  matched: string[]
  paramsPreview: Record<string, string>
  status: ApprovalStatus
  requestedAt: number
  resolvedAt?: number
}

export interface Artifact {
  key: string
  kind: string
  title: string
  path?: string
  content: string
  explanation?: string
  risk?: string
  stats?: Record<string, unknown>
  createdAt: number
}

export interface Run {
  runId: string
  seqStart: number
  status: RunStatus
  /** Mensaje del usuario que originó el run (viene de run.start). */
  prompt: string
  model?: string
  text: string
  reasoning: string
  tools: ToolCall[]
  approvals: Approval[]
  artifacts: Artifact[]
  startedAt: number
  endedAt?: number
  error?: string
}

export interface ThreadState {
  threadId: string
  runs: Run[]
  /** Último `seq` aplicado: es el cursor para reconectar. */
  lastSeq: number
  loaded: boolean
}

export interface ChatState {
  threads: Record<string, ThreadState>
}

export const emptyChatState: ChatState = { threads: {} }

export function emptyThread(threadId: string): ThreadState {
  return { threadId, runs: [], lastSeq: 0, loaded: false }
}

/** Marca un hilo como cargado sin eventos, para no recrear el objeto en cada render. */
export function withLoaded(state: ChatState, threadId: string): ChatState {
  const t = state.threads[threadId] ?? emptyThread(threadId)
  if (t.loaded) return state
  return { ...state, threads: { ...state.threads, [threadId]: { ...t, loaded: true } } }
}

function cloneThread(t: ThreadState, patch: Partial<ThreadState>): ThreadState {
  return { ...t, ...patch }
}

function cloneRun(r: Run, patch: Partial<Run>): Run {
  return { ...r, ...patch }
}

function newRun(runId: string, seq: number, prompt: string, model?: string, ts?: number): Run {
  return {
    runId,
    seqStart: seq,
    status: 'running',
    prompt,
    model,
    text: '',
    reasoning: '',
    tools: [],
    approvals: [],
    artifacts: [],
    startedAt: ts ?? Date.now() / 1000,
  }
}

/**
 * Aplica UN evento al estado. Función pura: no muta `state` ni el evento.
 *
 * Devuelve el mismo objeto si el evento es irrelevante (por ejemplo uno que ya
 * se había aplicado, detectado por `seq`), lo que evita renders inútiles. Ese
 * chequeo de `seq` es lo que hace segura la reconexión: si el cliente pide
 * `after=<último visto>` y el servidor repitiera algo, no se duplica.
 */
export function reduceEvent(state: ChatState, ev: AgentEvent): ChatState {
  const threadId = ev.thread_id
  if (!threadId) return state

  const thread = state.threads[threadId] ?? emptyThread(threadId)
  if (ev.seq <= thread.lastSeq) return state // ya aplicado

  const runs = thread.runs.slice()
  const idx = runs.findIndex((r) => r.runId === ev.run_id)
  const run: Run =
    idx >= 0
      ? runs[idx]
      : newRun(ev.run_id, ev.seq, '', undefined, ev.ts)

  const apply = (next: Run): ChatState => {
    if (idx >= 0) runs[idx] = next
    else runs.push(next)
    return {
      ...state,
      threads: {
        ...state.threads,
        [threadId]: cloneThread(thread, { runs, lastSeq: ev.seq }),
      },
    }
  }

  const d = ev.data as Record<string, unknown>
  const type = ev.type as EventType

  switch (type) {
    case 'run.start': {
      // El prompt del usuario vive en el preview del run.start. No hay un
      // evento de "mensaje del usuario" separado a propósito: el mensaje ES el
      // inicio del run, y tratarlo aparte obligaría a sincronizar dos fuentes.
      const prompt = (d.prompt as string) ?? (d.message_preview as string) ?? ''
      const model = d.model as string | undefined
      return apply(cloneRun(run, { prompt, model, status: 'running' }))
    }

    case 'text.delta':
      return apply(cloneRun(run, { text: run.text + ((d.content as string) ?? '') }))

    case 'reasoning.delta':
      return apply(cloneRun(run, { reasoning: run.reasoning + ((d.content as string) ?? '') }))

    case 'tool.start': {
      const tool = (d.tool as string) ?? 'tool'
      const call: ToolCall = {
        key: `${ev.run_id}:${run.tools.length}:${tool}`,
        tool,
        args: (d.args as Record<string, unknown>) ?? {},
        status: 'running',
        startedAt: ev.ts,
      }
      return apply(cloneRun(run, { tools: [...run.tools, call] }))
    }

    case 'tool.end': {
      const tool = (d.tool as string) ?? 'tool'
      const error = (d.error as string | null) ?? null
      const result = (d.result as string | null) ?? null
      // El backend emite `tool.end` sin un `tool.start` previo (el loop ReAct
      // no anuncia el inicio). Se sintetiza la tarjeta en ese caso; así el
      // reducer funciona con ambas formas y no depende de un evento que puede
      // no llegar nunca.
      const existing = [...run.tools].reverse().find((t) => t.tool === tool && t.status === 'running')
      if (existing) {
        const updated = cloneTool(existing, {
          status: error ? 'error' : 'ok',
          result,
          error,
          durationMs: (d.duration_ms as number) ?? undefined,
          endedAt: ev.ts,
        })
        return apply(cloneRun(run, { tools: run.tools.map((t) => (t === existing ? updated : t)) }))
      }
      const call: ToolCall = {
        key: `${ev.run_id}:${run.tools.length}:${tool}`,
        tool,
        args: (d.args as Record<string, unknown>) ?? {},
        result,
        error,
        durationMs: (d.duration_ms as number) ?? undefined,
        status: error ? 'error' : 'ok',
        startedAt: ev.ts,
        endedAt: ev.ts,
      }
      return apply(cloneRun(run, { tools: [...run.tools, call] }))
    }

    case 'tool.denied': {
      const tool = (d.tool as string) ?? 'tool'
      const call: ToolCall = {
        key: `${ev.run_id}:${run.tools.length}:${tool}`,
        tool,
        args: {},
        status: 'denied',
        ruleId: d.rule_id as string | undefined,
        risk: d.risk as string | undefined,
        reason: d.reason as string | undefined,
        startedAt: ev.ts,
        endedAt: ev.ts,
      }
      return apply(cloneRun(run, { tools: [...run.tools, call] }))
    }

    case 'approval.requested': {
      const ap: Approval = {
        requestId: (d.request_id as string) ?? ev.id,
        tool: (d.tool as string) ?? 'tool',
        agentId: d.agent_id as string | undefined,
        ruleId: d.rule_id as string | undefined,
        risk: (d.risk as string) ?? 'medium',
        reason: (d.reason as string) ?? '',
        matched: (d.matched as string[]) ?? [],
        paramsPreview: (d.params_preview as Record<string, string>) ?? {},
        status: 'pending',
        requestedAt: ev.ts,
      }
      return apply(cloneRun(run, { approvals: [...run.approvals, ap] }))
    }

    case 'approval.resolved': {
      const requestId = d.request_id as string | undefined
      const approved = Boolean(d.approved)
      const approvals = run.approvals.map((a) =>
        a.requestId === requestId
          ? { ...a, status: (approved ? 'approved' : 'rejected') as ApprovalStatus, resolvedAt: ev.ts }
          : a,
      )
      return apply(cloneRun(run, { approvals }))
    }

    case 'artifact.created': {
      const art: Artifact = {
        key: `${ev.run_id}:art:${run.artifacts.length}`,
        kind: (d.kind as string) ?? 'file',
        title: (d.title as string) ?? (d.path as string) ?? 'artefacto',
        path: d.path as string | undefined,
        content: (d.content as string) ?? '',
        explanation: d.explanation as string | undefined,
        risk: d.risk as string | undefined,
        stats: d.stats as Record<string, unknown> | undefined,
        createdAt: ev.ts,
      }
      return apply(cloneRun(run, { artifacts: [...run.artifacts, art] }))
    }

    case 'run.end': {
      const reason = (d.reason as string) ?? 'completed'
      const status: RunStatus =
        reason === 'error' ? 'error' : reason === 'cancelled' ? 'cancelled' : 'done'
      return apply(cloneRun(run, { status, endedAt: ev.ts }))
    }

    case 'run.error': {
      const detail = (d.detail as string) ?? (d.reason as string) ?? 'error desconocido'
      return apply(cloneRun(run, { status: 'error', error: detail, endedAt: ev.ts }))
    }

    default:
      // Tipo desconocido: se avanza el cursor igual, para no reprocesarlo en la
      // próxima reconexión. Un cliente viejo debe poder ignorar tipos nuevos
      // sin romperse ni quedar atascado.
      return {
        ...state,
        threads: {
          ...state.threads,
          [threadId]: cloneThread(thread, { runs: idx >= 0 ? runs : thread.runs, lastSeq: ev.seq }),
        },
      }
  }
}

function cloneTool(t: ToolCall, patch: Partial<ToolCall>): ToolCall {
  return { ...t, ...patch }
}

/** Aplica un lote de eventos en orden. Es lo que usa el replay. */
export function reduceAll(state: ChatState, events: AgentEvent[]): ChatState {
  let acc = state
  for (const ev of events) acc = reduceEvent(acc, ev)
  return acc
}

/** Reemplaza el log de un hilo con el resultado del replay (idempotente). */
export function replayThread(state: ChatState, threadId: string, events: AgentEvent[]): ChatState {
  const base: ChatState = {
    ...state,
    threads: { ...state.threads, [threadId]: emptyThread(threadId) },
  }
  const next = reduceAll(base, events)
  const t = next.threads[threadId]
  return {
    ...next,
    threads: { ...next.threads, [threadId]: { ...t, loaded: true } },
  }
}

// ── Selectores ──────────────────────────────────────────────────────────────

export function threadOf(state: ChatState, threadId: string | null): ThreadState | null {
  if (!threadId) return null
  return state.threads[threadId] ?? null
}

export function lastRun(thread: ThreadState | null): Run | null {
  if (!thread || thread.runs.length === 0) return null
  return thread.runs[thread.runs.length - 1]
}

export function isStreaming(thread: ThreadState | null): boolean {
  const r = lastRun(thread)
  return !!r && r.status === 'running'
}

export function pendingApprovals(thread: ThreadState | null): Approval[] {
  if (!thread) return []
  return thread.runs.flatMap((r) => r.approvals.filter((a) => a.status === 'pending'))
}

export function allArtifacts(thread: ThreadState | null): Artifact[] {
  if (!thread) return []
  return thread.runs.flatMap((r) => r.artifacts).sort((a, b) => b.createdAt - a.createdAt)
}
