import { useMemo, useState } from 'react'
import type { Artifact } from '@/lib/reduce'
import { Chip, Icon, ICONS, cx } from '@/components/ui'

/**
 * Renderizador de texto del asistente.
 *
 * Decisión consciente: NO interpreta markdown. El frontend anterior parseaba
 * con `marked` y volcaba el resultado con `innerHTML`, lo que abría la puerta a
 * inyección de HTML desde la respuesta del modelo y, además, duplicaba el texto
 * en pantalla (un bug real y visible en las capturas de la auditoría).
 *
 * Acá el texto se pinta como texto, preservando saltos y espacios. Es correcto
 * siempre y no puede inyectar nada. El enriquecido (código, tablas, links) es
 * una capa aparte que se agrega cuando esté la librería correspondiente, sin
 * tocar esto.
 */
export function AssistantText({ text, streaming }: { text: string; streaming?: boolean }) {
  if (!text) return null
  return (
    <div
      className={cx(
        'text-[13px] leading-relaxed whitespace-pre-wrap text-t-primary',
        streaming && 'vc-caret',
      )}
    >
      {text}
    </div>
  )
}

/**
 * Bloque de razonamiento del modelo.
 *
 * Los modelos del catálogo actual (GLM 5.3, Kimi K3, Nemotron 3) son de
 * razonamiento: emiten la cadena de pensamiento en un canal separado que el
 * backend traduce al evento `reasoning.delta`. Se muestra colapsado porque es
 * contexto, no respuesta — pero se muestra, porque es información real sobre
 * cómo llegó el modelo a lo que dijo.
 */
export function ReasoningBlock({ text, streaming }: { text: string; streaming?: boolean }) {
  const [open, setOpen] = useState(false)
  if (!text) return null

  return (
    <div className="rounded-md border border-b-subtle bg-surf-panel/50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-surf-hover"
        aria-expanded={open}
      >
        <Icon path={open ? ICONS.chevronDown : ICONS.chevronRight} size={12} />
        <Icon path={ICONS.brain} size={12} />
        <span className="text-[11px] text-t-secondary">razonamiento</span>
        <span className="font-mono text-[10px] text-t-tertiary">{text.length} chars</span>
        {streaming && <span className="vc-pulse ml-auto h-1.5 w-1.5 rounded-full bg-accent" />}
      </button>
      {open && (
        <pre className="max-h-72 overflow-auto border-t border-b-subtle px-2.5 py-2 font-mono text-[11px] whitespace-pre-wrap text-t-tertiary">
          {text}
        </pre>
      )}
    </div>
  )
}

/** Tarjeta de artefacto. Abre el visor con el contenido completo. */
export function ArtifactCard({
  artifact,
  onOpen,
}: {
  artifact: Artifact
  onOpen: (a: Artifact) => void
}) {
  const preview = useMemo(() => artifact.content.slice(0, 1200), [artifact.content])
  const lines = artifact.content ? artifact.content.split('\n').length : 0

  return (
    <div className="vc-enter overflow-hidden rounded-md border border-b-default bg-surf-panel">
      <div className="flex items-center gap-2 border-b border-b-subtle px-2.5 py-1.5">
        <Icon path={ICONS.file} size={12} />
        <span className="truncate text-xs font-medium text-t-primary">{artifact.title}</span>
        <Chip>{artifact.kind}</Chip>
        {artifact.risk && <Chip tone={artifact.risk === 'low' ? 'success' : 'warning'}>{artifact.risk}</Chip>}
        <span className="ml-auto shrink-0 font-mono text-[10px] text-t-tertiary">
          {lines} líneas
        </span>
      </div>

      {artifact.explanation && (
        <p className="px-2.5 pt-2 text-[11px] text-t-secondary">{artifact.explanation}</p>
      )}

      {preview && (
        <pre className="m-2 max-h-52 overflow-hidden rounded-sm bg-surf-code p-2 font-mono text-[10px] whitespace-pre-wrap text-t-secondary">
          {preview}
          {artifact.content.length > preview.length ? '\n…' : ''}
        </pre>
      )}

      <div className="flex gap-2 border-t border-b-subtle px-2.5 py-1.5">
        <button
          type="button"
          onClick={() => onOpen(artifact)}
          className="text-[11px] text-accent hover:underline"
        >
          abrir en el visor
        </button>
        <button
          type="button"
          onClick={() => void navigator.clipboard.writeText(artifact.content)}
          className="text-[11px] text-t-tertiary hover:text-t-secondary"
        >
          copiar
        </button>
      </div>
    </div>
  )
}
