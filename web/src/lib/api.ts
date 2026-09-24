/**
 * Cliente HTTP del backend. Tipado contra las respuestas reales verificadas.
 *
 * En desarrollo la base es vacía y Vite proxyea al backend; en producción el
 * bundle lo sirve FastAPI, así que mismo origen. En ningún caso se hardcodea el
 * puerto: el backend usa `VCORE_PORT` y el proxy se configura en vite.config.ts.
 */

import type { AgentEvent, EventsPage, ThreadSummary } from '@/lib/protocol'

const BASE = ''

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = body?.detail ?? detail
    } catch {
      /* respuesta sin JSON: se conserva el status */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

// ── Sistema ────────────────────────────────────────────────────────────────

export interface Health {
  status: string
  version: string
}

export interface RoleModel {
  provider: string
  model: string
  circuit_breaker?: string
  context_window?: number
  temperature?: number
}

export interface AvailableModel {
  id: string
  name: string
  context?: string
  priority?: number
  reasoning?: boolean
}

export const api = {
  health: () => req<Health>('/health'),

  /** Modelos por rol — la fuente de verdad del "modelo activo". */
  models: () => req<Record<string, RoleModel>>('/system/model'),

  /** Catálogo para el selector. */
  availableModels: (provider = 'nvidia') =>
    req<{ provider: string; models: AvailableModel[] }>(
      `/system/model/available?provider=${encodeURIComponent(provider)}`,
    ),

  /** Hot-swap: cambia el modelo de un rol sin reiniciar el backend. */
  swapModel: (role: string, model: string, temperature?: number) =>
    req<{ ok: boolean }>('/system/model', {
      method: 'POST',
      body: JSON.stringify({ role, model, temperature }),
    }),

  policy: () =>
    req<{
      rules_path: string
      workspace_dir: string
      allowed_roots: string[]
      tools: Record<string, string>
      command_params: Record<string, string>
      command_allowlist: Record<string, string[] | string>
      perimeter: Record<string, unknown>
    }>('/policy/rules'),

  embeddings: () =>
    req<{
      probe: {
        active: { tier: string; model: string; dims: number; ok: boolean }
        tiers: Array<{ tier: string; model: string; ok: boolean; dims: number }>
      }
      chroma_dir: string
      memory?: Record<string, unknown>
      knowledge?: Record<string, unknown>
    }>('/embeddings/status'),

  circuitStatus: () =>
    req<Record<string, { state: string; consecutive_divergences?: number }>>(
      '/llm/circuit-status',
    ),

  resetCircuit: () =>
    req<{ reset: boolean }>('/llm/circuit-status/reset', { method: 'POST' }),

  usage: (limit = 50) =>
    req<Array<{ model: string; provider: string; tokens_in: number; tokens_out: number; timestamp: number; agent_name?: string }>>(
      `/llm/usage?limit=${limit}`,
    ),

  gateLog: (limit = 50) =>
    req<Array<{ timestamp: number; agent_id: string; tool: string; reason: string; auto_approved?: number; effect?: string; risk?: string; rule_id?: string }>>(
      `/log?limit=${limit}`,
    ),
}

// ── Sesiones y workspaces ──────────────────────────────────────────────────

export interface Session {
  id: number
  title: string | null
  session_dir: string | null
  project?: string
  status?: string
  timestamp?: string
}

export interface WorkspaceInfo {
  session_id: number
  session_dir: string | null
  exists: boolean
  work_dir?: string
  artifacts_dir?: string
  work_files?: Array<{ name: string; type: string; size: number | null }>
  artifacts?: Array<{ name: string; type: string; size: number | null }>
  meta?: Record<string, unknown>
}

export const sessions = {
  list: () => req<Session[]>('/sessions'),

  create: () =>
    req<{ id: number; session_dir: string; title: string; agent: string }>('/sessions', {
      method: 'POST',
      body: '{}',
    }),

  rename: (key: string, title: string) =>
    req<{ renamed: boolean }>(`/sessions/${encodeURIComponent(key)}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),

  remove: (key: string) =>
    req<{ deleted: boolean }>(`/sessions/${encodeURIComponent(key)}`, { method: 'DELETE' }),

  workspace: (key: string) =>
    req<WorkspaceInfo>(`/sessions/${encodeURIComponent(key)}/workspace`),
}

// ── Eventos: replay y reconexión ───────────────────────────────────────────

export const events = {
  /**
   * Log de eventos de una conversación desde `after` (exclusivo).
   *
   * Es la base del replay: la UI aplica el mismo `reduce` que usa para el vivo.
   * Y la base de la reconexión: `after = último seq visto`.
   */
  thread: (threadId: string, after = 0, limit = 2000) =>
    req<EventsPage>(
      `/threads/${encodeURIComponent(threadId)}/events?after=${after}&limit=${limit}`,
    ),

  summary: (threadId: string) =>
    req<ThreadSummary>(`/threads/${encodeURIComponent(threadId)}/summary`),
}

// ── Aprobaciones (HITL sincrónico) ─────────────────────────────────────────

export const approvals = {
  pending: () =>
    req<{
      pending: Array<Record<string, unknown>>
      stats: Record<string, number>
    }>('/approvals/pending'),

  /**
   * Resuelve una aprobación. El backend despierta el `asyncio.Future` donde el
   * agente está pausado y el stream continúa.
   *
   * Devuelve `resolved: false` si ya estaba resuelta (doble click, reintento):
   * el backend es idempotente a propósito para que una acción no se ejecute dos
   * veces.
   */
  resolve: (requestId: string, approved: boolean) =>
    req<{ request_id: string; resolved: boolean; approved: boolean; detail: string | null }>(
      `/approvals/pending/${encodeURIComponent(requestId)}/resolve`,
      { method: 'POST', body: JSON.stringify({ approved }) },
    ),
}

// ── Archivos ───────────────────────────────────────────────────────────────

export interface FileReadResult {
  path: string
  content: string
  size?: number
  truncated?: boolean
}

export const files = {
  read: (path: string) =>
    req<FileReadResult>(`/files/read?path=${encodeURIComponent(path)}`),

  search: (pattern: string, root = 'vcore') =>
    req<Record<string, unknown>>(
      `/files/search?pattern=${encodeURIComponent(pattern)}&root=${encodeURIComponent(root)}`,
    ),
}

// ── Stream de un run ───────────────────────────────────────────────────────

export interface RunRequest {
  message: string
  history: Array<{ role: string; content: string }>
  task_id?: string
  session_dir?: string
  command?: string
}

/**
 * Abre el stream de un run y entrega los eventos legacy a medida que llegan.
 *
 * El backend emite SSE en el formato plano histórico más `seq`/`id`/`run_id` en
 * algunos eventos. Se usa `fetch` + `ReadableStream` en vez de `EventSource`
 * porque el run es un POST (EventSource solo hace GET) y porque así se puede
 * cancelar con `AbortController`.
 */
export async function streamRun(
  body: RunRequest,
  onEvent: (ev: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(BASE + '/agents/route', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // `history` fijo al final: el backend lo espera siempre, aunque sea vacío.
    body: JSON.stringify({ ...body, history: body.history ?? [] }),
    signal,
  })

  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => `HTTP ${res.status}`)
    throw new Error(detail.slice(0, 300) || `HTTP ${res.status}`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE: los frames se separan por doble salto de línea; `data:` puede
    // repetirse. Se parte por líneas y se conserva el resto incompleto.
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('data:')) continue
      const raw = trimmed.slice(5).trim()
      if (!raw) continue
      try {
        onEvent(JSON.parse(raw) as Record<string, unknown>)
      } catch {
        // Frame no-JSON: se ignora en vez de cortar el stream.
      }
    }
  }
}

export type { AgentEvent, EventsPage, ThreadSummary }
