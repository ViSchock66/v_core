/**
 * Protocolo de eventos de V-CORE — lado cliente.
 *
 * Espejo de `api/events.py`. Si el backend cambia el catálogo de tipos, esto
 * tiene que cambiar con él: por eso los tipos están como unión discriminada y
 * no como `string`, así TypeScript obliga a manejar cada caso al reducir.
 *
 * Regla de oro del diseño: **la UI no inventa estado**. Todo lo que se ve sale
 * de reducir este log. Es lo que hace que el replay de una conversación
 * reproduzca exactamente lo que pasó, sin código duplicado entre "vivo" y
 * "historial".
 */

export const PROTOCOL_VERSION = 1;

export type EventType =
  | 'run.start'
  | 'run.end'
  | 'run.error'
  | 'text.delta'
  | 'reasoning.delta'
  | 'tool.start'
  | 'tool.end'
  | 'tool.denied'
  | 'approval.requested'
  | 'approval.resolved'
  | 'artifact.created'
  | 'graph.snapshot'
  | 'circuit.state'
  | 'usage.update';

/** Envelope canónico, tal como lo persiste el backend. */
export interface AgentEvent<T extends EventType = EventType> {
  v: number;
  seq: number;
  id: string;
  ts: number;
  run_id: string;
  thread_id: string;
  type: T;
  data: EventData<T>;
}

/** Payload por tipo. `unknown` en los campos libres fuerza validación al usarlos. */
export interface EventPayloads {
  'run.start': { run_id?: string; thread_id?: string; model?: string; message_preview?: string };
  'run.end': { reason?: 'completed' | 'error' | 'cancelled' };
  'run.error': { reason?: string; detail?: string };
  'text.delta': { content?: string };
  'reasoning.delta': { content?: string };
  'tool.start': { tool?: string; args?: Record<string, unknown> };
  'tool.end': {
    tool?: string;
    args?: Record<string, unknown>;
    result?: string | null;
    duration_ms?: number;
    error?: string | null;
  };
  'tool.denied': { tool?: string; rule_id?: string; risk?: string; reason?: string };
  'approval.requested': {
    request_id?: string;
    tool?: string;
    agent_id?: string;
    rule_id?: string;
    risk?: string;
    reason?: string;
    matched?: string[];
    params_preview?: Record<string, string>;
  };
  'approval.resolved': { request_id?: string; tool?: string; approved?: boolean };
  'artifact.created': {
    kind?: string;
    title?: string;
    path?: string;
    content?: string;
    explanation?: string;
    risk?: string;
    action?: string;
    stats?: Record<string, unknown>;
  };
  'graph.snapshot': { graph_id?: string; nodes?: Array<Record<string, unknown>> };
  'circuit.state': { provider?: string; model?: string; state?: string };
  'usage.update': { tokens_in?: number; tokens_out?: number; model?: string };
}

export type EventData<T extends EventType> = T extends keyof EventPayloads
  ? EventPayloads[T]
  : Record<string, unknown>;

/** Evento del wire legacy (SSE plano) que el backend sigue emitiendo. */
export interface LegacyEvent {
  type: string;
  seq?: number;
  id?: string;
  run_id?: string;
  thread_id?: string;
  [k: string]: unknown;
}

/** Respuesta de `GET /threads/{id}/events`. */
export interface EventsPage {
  thread_id: string;
  after: number;
  count: number;
  next_after: number;
  events: AgentEvent[];
}

/** Resumen de `GET /threads/{id}/summary`. */
export interface ThreadSummary {
  thread_id: string;
  events: number;
  runs: number;
  types: Record<string, number>;
  first_ts: number | null;
  last_ts: number | null;
}
