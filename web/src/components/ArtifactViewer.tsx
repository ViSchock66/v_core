import { useEffect, useMemo, useRef, useState } from 'react'
import { EditorState, Compartment } from '@codemirror/state'
import { EditorView, keymap, lineNumbers, highlightActiveLine } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { yaml } from '@codemirror/lang-yaml'
import { json } from '@codemirror/lang-json'
import { python } from '@codemirror/lang-python'
import { javascript } from '@codemirror/lang-javascript'
import { markdown } from '@codemirror/lang-markdown'
import { useChat } from '@/state/chat'
import { allArtifacts, type Artifact } from '@/lib/reduce'
import { Button, Chip, Icon, ICONS } from '@/components/ui'

/** Detección de lenguaje por extensión. Sin dependencia de CDN ni de Monaco. */
function langFor(path: string | undefined) {
  const ext = (path ?? '').split('.').pop()?.toLowerCase() ?? ''
  switch (ext) {
    case 'py':
      return python()
    case 'js':
    case 'jsx':
      return javascript()
    case 'ts':
    case 'tsx':
      return javascript({ typescript: true, jsx: ext === 'tsx' })
    case 'json':
      return json()
    case 'yaml':
    case 'yml':
      return yaml()
    case 'md':
      return markdown()
    default:
      return []
  }
}

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico', 'bmp'])

function isImage(path: string | undefined): boolean {
  const ext = (path ?? '').split('.').pop()?.toLowerCase() ?? ''
  return IMAGE_EXTS.has(ext)
}

/** Detecta contenido binario por bytes de control en el arranque del texto. */
function looksBinary(content: string): boolean {
  // eslint-disable-next-line no-control-regex
  return /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(content.slice(0, 2000))
}

/**
 * Editor de solo lectura del artefacto.
 *
 * Se usa CodeMirror 6 en vez de Monaco: pesa ~1/5, se instala por npm (sin CDN)
 * y no necesita un loader en runtime. El frontend anterior cargaba Monaco desde
 * jsdelivr, así que sin internet el visor quedaba en blanco — y el panel abría
 * vacío mientras el editor terminaba de llegar, sin decirlo.
 */
function CodeView({ content, path, theme }: { content: string; path?: string; theme: string }) {
  const host = useRef<HTMLDivElement>(null)
  const view = useRef<EditorView | null>(null)
  const themeComp = useRef(new Compartment())

  useEffect(() => {
    if (!host.current) return

    const dark = theme === 'dark'
    const cmTheme = EditorView.theme(
      {
        '&': {
          backgroundColor: 'transparent',
          color: 'var(--text-primary)',
          fontSize: '11px',
          height: '100%',
        },
        '.cm-content': { fontFamily: 'var(--font-mono)', padding: '8px 0' },
        '.cm-gutters': {
          backgroundColor: 'transparent',
          color: 'var(--text-tertiary)',
          border: 'none',
        },
        '.cm-activeLine': { backgroundColor: 'var(--surf-hover)' },
        '.cm-activeLineGutter': { backgroundColor: 'transparent' },
        '&.cm-focused': { outline: 'none' },
      },
      { dark },
    )

    const state = EditorState.create({
      doc: content,
      extensions: [
        lineNumbers(),
        highlightActiveLine(),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        themeComp.current.of(cmTheme),
        EditorView.editable.of(false),
        EditorState.readOnly.of(true),
        EditorView.lineWrapping,
        langFor(path),
      ],
    })

    view.current?.destroy()
    view.current = new EditorView({ state, parent: host.current })

    return () => {
      view.current?.destroy()
      view.current = null
    }
  }, [content, path, theme])

  return <div ref={host} className="h-full overflow-auto" />
}

function EmptyState() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
      <Icon path={ICONS.file} size={20} />
      <p className="text-[12px] text-t-secondary">Sin artefacto abierto</p>
      <p className="max-w-xs text-[10px] text-t-tertiary">
        Los artefactos aparecen cuando el agente escribe o modifica un archivo. Se
        persisten como eventos, así que siguen acá al reabrir la conversación.
      </p>
    </div>
  )
}

export function ArtifactViewer() {
  const activeThread = useChat((s) => s.activeThread)
  const chat = useChat((s) => s.chat)
  const openKey = useChat((s) => s.openArtifactKey)
  const setOpen = useChat((s) => s.openArtifact)
  const theme = useChatTheme()

  const thread = activeThread ? chat.threads[activeThread] : null
  const artifacts = useMemo(() => allArtifacts(thread ?? null), [thread])

  const [selected, setSelected] = useState<Artifact | null>(null)
  useEffect(() => {
    if (openKey) {
      const found = artifacts.find((a) => a.key === openKey)
      if (found) setSelected(found)
    }
  }, [openKey, artifacts])

  // Si no hay selección explícita, se muestra el artefacto más reciente: es lo
  // que el usuario espera al abrir el panel después de que el agente escribió.
  const current = selected ?? artifacts[0] ?? null

  if (!current) return <EmptyState />

  const binary = looksBinary(current.content)
  const image = isImage(current.path)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-b-subtle px-3 py-2">
        <span className="truncate text-xs font-medium text-t-primary">{current.title}</span>
        <Chip>{current.kind}</Chip>
        {current.risk && <Chip tone={current.risk === 'low' ? 'success' : 'warning'}>{current.risk}</Chip>}
        <span className="ml-auto flex shrink-0 gap-1">
          <Button
            size="sm"
            onClick={() => void navigator.clipboard.writeText(current.content)}
            title="Copiar contenido"
          >
            <Icon path={ICONS.copy} size={11} />
          </Button>
        </span>
      </div>

      {artifacts.length > 1 && (
        <div className="flex gap-1 overflow-x-auto border-b border-b-subtle px-2 py-1.5">
          {artifacts.map((a) => (
            <button
              key={a.key}
              type="button"
              onClick={() => {
                setSelected(a)
                setOpen(a.key)
              }}
              className={`shrink-0 rounded-sm px-2 py-0.5 font-mono text-[10px] ${
                a.key === current.key
                  ? 'bg-accent-dim text-accent'
                  : 'text-t-tertiary hover:text-t-secondary'
              }`}
              title={a.path ?? a.title}
            >
              {a.title}
            </button>
          ))}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-hidden">
        {image && current.path ? (
          // eslint-disable-next-line jsx-a11y/img-redundant-alt
          <img
            src={`/files/view?path=${encodeURIComponent(current.path)}`}
            alt={current.title}
            className="max-h-full max-w-full object-contain p-3"
          />
        ) : binary ? (
          <div className="p-4 text-[11px] text-warning">
            El contenido parece binario y no se puede mostrar como texto.
            {current.path && (
              <div className="mt-2 font-mono text-[10px] text-t-tertiary">{current.path}</div>
            )}
          </div>
        ) : (
          <CodeView content={current.content} path={current.path} theme={theme} />
        )}
      </div>

      {current.path && (
        <div className="truncate border-t border-b-subtle px-3 py-1.5 font-mono text-[10px] text-t-tertiary">
          {current.path}
        </div>
      )}
    </div>
  )
}

/** Lee el tema vigente sin acoplar el componente al store de tema. */
function useChatTheme(): string {
  const [theme, setTheme] = useState(
    () => document.documentElement.getAttribute('data-theme') ?? 'dark',
  )
  useEffect(() => {
    const obs = new MutationObserver(() =>
      setTheme(document.documentElement.getAttribute('data-theme') ?? 'dark'),
    )
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])
  return theme
}
