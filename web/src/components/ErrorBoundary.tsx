import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
  info: string
}

/**
 * Barrera de errores de la app.
 *
 * El frontend anterior tenía ~150 `catch {}` vacíos: cualquier fallo se
 * convertía en silencio y la UI quedaba en un estado raro sin decir nada. Esta
 * barrera garantiza que un error de render se vea, con el stack, en vez de
 * dejar la pantalla en blanco.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: '' }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ info: info.componentStack ?? '' })
    // Queda también en consola: la barrera es para el usuario, no para el dev.
    console.error('[V-CORE] Error de render:', error, info)
  }

  render() {
    const { error, info } = this.state
    if (!error) return this.props.children

    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="max-w-2xl rounded-lg border border-b-default bg-surf-panel p-6">
          <div className="mb-2 font-mono text-[11px] tracking-widest text-danger uppercase">
            Error de interfaz
          </div>
          <h1 className="mb-3 text-lg text-t-primary">{error.message}</h1>
          <details className="text-t-secondary">
            <summary className="cursor-pointer text-xs select-none">Ver detalle técnico</summary>
            <pre className="mt-2 max-h-64 overflow-auto rounded-sm bg-surf-code p-3 font-mono text-[11px] whitespace-pre-wrap">
              {error.stack}
              {info}
            </pre>
          </details>
        </div>
      </div>
    )
  }
}
