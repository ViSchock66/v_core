/**
 * Store del chat: el log de eventos es la única fuente de estado.
 *
 * Decisión de diseño verificada contra el backend real: **el core no numera
 * cada delta de texto**. Los eventos de política viajan con `seq` (para que el
 * cliente pueda ubicarse) pero los `text.delta` y `reasoning.delta` no, porque
 * un `seq` por token no aporta nada y engorda el wire.
 *
 * Consecuencia práctica: durante el stream hay texto en pantalla cuyo `seq` el
 * cliente no conoce. Así que el estado en vivo se arma localmente y, **al
 * cerrar el run, se reconcilia con el log autoritativo** (`replayThread`). Eso
 * garantiza que lo que quedó en pantalla sea exactamente lo que el backend
 * persistió — y de paso es la prueba de que ambos `reduce` coinciden.
 */

import { create } from 'zustand'
import { events as eventsApi, sessions as sessionsApi, streamRun } from '@/lib/api'
import {
  emptyChatState,
  emptyThread,
  reduceEvent,
  replayThread,
  type ChatState,
} from '@/lib/reduce'
import type { AgentEvent } from '@/lib/protocol'

type PanelId = 'chat' | 'artifacts' | 'system' | 'workspace'

interface ChatStore {
  // ── Log (event sourcing) ──
  chat: ChatState
  activeThread: string | null

  // ── UI ──
  panel: PanelId
  sidebarOpen: boolean
  streaming: boolean
  error: string | null
  /**
   * Clave del artefacto abierto en el visor (`Artifact.key`), o null.
   *
   * Se llama `openArtifactKey` y no `openArtifact` a propósito: la acción que lo
   * cambia ya se llama `openArtifact`, y tener un valor y una función con el
   * mismo nombre hacía que TypeScript no pudiera distinguirlos.
   */
  openArtifactKey: string | null

  // ── Acciones ──
  setPanel: (p: PanelId) => void
  toggleSidebar: () => void
  openArtifact: (key: string | null) => void

  loadThread: (threadId: string) => Promise<void>
  selectThread: (threadId: string | null) => void
  send: (message: string, sessionDir: string | null) => Promise<void>
  cancel: () => void
  dismissError: () => void
}

/** Aborta el run en curso (cancelación real, no solo visual). */
let abort: AbortController | null = null

const LS_ACTIVE = 'vc.activeThread'

/**
 * Persiste el hilo activo.
 *
 * Sin esto, recargar la página perdía la conversación aunque los eventos
 * estuvieran todos en el log: la UI no recordaba cuál estaba mirando. Era el
 * mismo síntoma que tenía el frontend anterior (historial que desaparece) pero
 * por otra causa — allá no se persistían los eventos, acá sí, pero nadie sabía
 * cuál hilo cargar.
 */
function rememberThread(threadId: string | null): void {
  try {
    if (threadId) localStorage.setItem(LS_ACTIVE, threadId)
    else localStorage.removeItem(LS_ACTIVE)
  } catch {
    /* almacenamiento bloqueado: la sesión actual funciona igual */
  }
}

function recalledThread(): string | null {
  try {
    return localStorage.getItem(LS_ACTIVE)
  } catch {
    return null
  }
}

export const useChat = create<ChatStore>((set, get) => ({
  chat: emptyChatState,
  activeThread: null,
  panel: 'chat',
  sidebarOpen: true,
  streaming: false,
  error: null,
  openArtifactKey: null,

  setPanel: (panel) => set({ panel }),
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  openArtifact: (key) =>
    set((s) => ({ openArtifactKey: key, panel: key ? 'artifacts' : s.panel })),

  selectThread: (threadId) => {
    rememberThread(threadId)
    set({ activeThread: threadId, error: null })
    if (threadId) void get().loadThread(threadId)
  },

  /** Carga el log de un hilo y lo reduce: eso es el replay. */
  loadThread: async (threadId) => {
    try {
      const page = await eventsApi.thread(threadId)
      set((s) => ({ chat: replayThread(s.chat, threadId, page.events) }))
    } catch (e) {
      set((s) => ({
        error: `No se pudo cargar la conversación: ${(e as Error).message}`,
        // Un hilo sin eventos no es un error: es una conversación nueva. Se
        // marca cargado para que la UI no muestre "cargando" para siempre.
        chat: {
          ...s.chat,
          threads: { ...s.chat.threads, [threadId]: { ...emptyThread(threadId), loaded: true } },
        },
      }))
    }
  },

  send: async (message, sessionDir) => {
    const threadId = sessionDir ?? get().activeThread ?? 'default'
    const runId = `run_${Math.random().toString(36).slice(2, 14)}`
    rememberThread(threadId)
    abort = new AbortController()

    set((s) => ({
      streaming: true,
      error: null,
      activeThread: threadId,
      chat: {
        ...s.chat,
        threads: {
          ...s.chat.threads,
          [threadId]: s.chat.threads[threadId] ?? emptyThread(threadId),
        },
      },
    }))

    // Aplica un evento al estado vivo. El envelope se sintetiza para los frames
    // legacy del wire (que no traen `v`/`td`), de modo que el mismo reducer
    // sirva para lo que llega en vivo y para lo que se lee del log.
    const applyLive = (raw: Record<string, unknown>) => {
      const type = String(raw.type ?? '')
      const seq = typeof raw.seq === 'number' ? raw.seq : null
      if (seq === null) return // sin seq no se puede ordenar: lo cubre el replay final
      const ev: AgentEvent = {
        v: 1,
        seq,
        id: String(raw.id ?? `${runId}:${seq}`),
        ts: Date.now() / 1000,
        run_id: String(raw.run_id ?? runId),
        thread_id: String(raw.thread_id ?? threadId),
        type: type === 'chunk' ? 'text.delta' : (type as AgentEvent['type']),
        data: raw as AgentEvent['data'],
      }
      set((s) => ({ chat: reduceEvent(s.chat, ev) }))
    }

    try {
      await streamRun(
        { message, history: [], session_dir: sessionDir ?? '', task_id: runId },
        applyLive,
        abort.signal,
      )
    } catch (e) {
      const err = e as Error
      if (err.name !== 'AbortError') {
        set({ error: `El run falló: ${err.message}` })
      }
    } finally {
      abort = null
      set({ streaming: false })
      // Reconciliación: el log del backend es la fuente de verdad. Trae los
      // deltas de texto (que en vivo llegaron sin seq) y garantiza que el
      // estado mostrado sea idéntico al persistido.
      try {
        const page = await eventsApi.thread(threadId)
        set((s) => ({ chat: replayThread(s.chat, threadId, page.events) }))
      } catch {
        /* si el replay falla se conserva el estado en vivo, que ya es correcto */
      }
    }
  },

  cancel: () => {
    abort?.abort()
    abort = null
    set({ streaming: false })
  },

  dismissError: () => set({ error: null }),
}))

/** Crea una sesión nueva y la deja activa. */
export async function startNewSession(): Promise<{ id: number; session_dir: string } | null> {
  try {
    const s = await sessionsApi.create()
    useChat.getState().selectThread(s.session_dir)
    return { id: s.id, session_dir: s.session_dir }
  } catch (e) {
    useChat.setState({ error: `No se pudo crear la sesión: ${(e as Error).message}` })
    return null
  }
}

/**
 * Restaura el hilo que el usuario estaba viendo antes de recargar.
 *
 * Se llama una vez al montar la app. Carga el log de eventos y lo reduce: eso
 * es el replay, y es lo que hace que la conversación reaparezca completa (tool
 * cards, razonamiento, artefactos) en vez de quedar vacía.
 */
export async function restoreActiveThread(): Promise<void> {
  const threadId = recalledThread()
  if (!threadId) return
  const store = useChat.getState()
  if (store.activeThread) return
  store.selectThread(threadId)
}
