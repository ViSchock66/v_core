import { useEffect, useRef, useState } from 'react'
import { Button, Icon, ICONS } from '@/components/ui'

/**
 * Caja de entrada.
 *
 * Detalles que importan: auto-alto hasta un tope, Enter envía y Shift+Enter
 * hace salto de línea, y durante un run el botón se convierte en detener (que
 * aborta el fetch de verdad, no solo cambia el ícono — el frontend anterior
 * llamaba a `endStream()` local y dejaba el request del backend corriendo).
 */
export function Composer({
  onSend,
  onCancel,
  streaming,
  disabled,
  modelLabel,
  reasoning,
}: {
  onSend: (text: string) => void
  onCancel: () => void
  streaming: boolean
  disabled?: boolean
  modelLabel?: string
  reasoning?: boolean
}) {
  const [value, setValue] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [value])

  function submit() {
    const text = value.trim()
    if (!text || streaming || disabled) return
    setValue('')
    onSend(text)
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="shrink-0 border-t border-b-subtle bg-surf-chat px-4 pt-3 pb-4">
      <div className="mx-auto max-w-3xl">
        <div className="flex items-end gap-2 rounded-lg border border-b-default bg-surf-input p-2">
          <textarea
            ref={ref}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            disabled={disabled}
            placeholder={disabled ? 'Seleccioná o creá una conversación…' : 'Escribí un mensaje…'}
            aria-label="Mensaje"
            className="max-h-[200px] flex-1 resize-none bg-transparent px-1 py-1 text-[13px] text-t-primary outline-none placeholder:text-t-tertiary"
          />

          {streaming ? (
            <Button variant="danger" onClick={onCancel} title="Detener el run">
              <Icon path={ICONS.stop} size={13} />
              Detener
            </Button>
          ) : (
            <Button
              variant="accent"
              onClick={submit}
              disabled={!value.trim() || disabled}
              title="Enviar (Enter)"
            >
              <Icon path={ICONS.send} size={13} />
            </Button>
          )}
        </div>

        <div className="mt-1.5 flex items-center gap-3 px-1 text-[10px] text-t-tertiary">
          <span className="font-mono">Enter envía · Shift+Enter salto</span>
          {modelLabel && (
            <span className="ml-auto flex items-center gap-1.5">
              {reasoning && <span title="modelo de razonamiento">◆</span>}
              <span className="font-mono">{modelLabel}</span>
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
