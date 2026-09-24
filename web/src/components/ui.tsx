/**
 * Primitivas de UI.
 *
 * Sin librería de componentes a propósito: son pocas y así no se agrega una
 * dependencia que haya que actualizar. Lo que sí se respeta es la
 * accesibilidad — roles, `aria-*` y foco visible — porque es lo que separa una
 * UI de demo de una UI usable.
 */

import type { ButtonHTMLAttributes, ReactNode } from 'react'

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'ghost' | 'solid' | 'danger' | 'accent'
  size?: 'sm' | 'md'
}

export function Button({ variant = 'ghost', size = 'md', className, ...rest }: ButtonProps) {
  const base =
    'inline-flex items-center gap-1.5 rounded-md border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40'
  const sizes = size === 'sm' ? 'px-2 py-1 text-[11px]' : 'px-3 py-1.5 text-xs'
  const variants: Record<string, string> = {
    ghost: 'border-b-default bg-transparent text-t-secondary hover:bg-surf-hover hover:text-t-primary',
    solid: 'border-b-strong bg-surf-card text-t-primary hover:bg-surf-active',
    accent: 'border-transparent bg-accent text-accent-fg hover:opacity-90',
    danger: 'border-b-default bg-transparent text-danger hover:bg-surf-hover',
  }
  return <button className={cx(base, sizes, variants[variant], className)} {...rest} />
}

export function Chip({
  children,
  tone = 'neutral',
  title,
}: {
  children: ReactNode
  tone?: 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'purple'
  title?: string
}) {
  const tones: Record<string, string> = {
    neutral: 'border-b-default text-t-tertiary',
    accent: 'border-accent-solid text-accent',
    success: 'border-transparent text-success',
    warning: 'border-transparent text-warning',
    danger: 'border-transparent text-danger',
    purple: 'border-transparent text-purple',
  }
  return (
    <span
      title={title}
      className={cx(
        'inline-flex items-center rounded-sm border px-1.5 py-0.5 font-mono text-[10px] tracking-wide uppercase',
        tones[tone],
      )}
    >
      {children}
    </span>
  )
}

/** Colores por nivel de riesgo. Es la escala que usa la política de permisos. */
export function riskTone(risk?: string): 'neutral' | 'success' | 'warning' | 'danger' | 'accent' {
  switch ((risk ?? '').toLowerCase()) {
    case 'low':
      return 'success'
    case 'medium':
      return 'warning'
    case 'high':
    case 'critical':
      return 'danger'
    default:
      return 'neutral'
  }
}

export function Icon({ path, size = 14 }: { path: string; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={path} />
    </svg>
  )
}

export const ICONS = {
  chat: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  folder: 'M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z',
  code: 'M16 18l6-6-6-6M8 6l-6 6 6 6',
  activity: 'M22 12h-4l-3 9L9 3l-3 9H2',
  send: 'M12 19V5M5 12l7-7 7 7',
  stop: 'M6 6h12v12H6z',
  plus: 'M12 5v14M5 12h14',
  x: 'M18 6L6 18M6 6l12 12',
  check: 'M20 6L9 17l-5-5',
  chevronDown: 'M6 9l6 6 6-6',
  chevronRight: 'M9 18l6-6-6-6',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  sun: 'M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4',
  moon: 'M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z',
  brain: 'M9 3a3 3 0 0 0-3 3v1a3 3 0 0 0 0 6v1a3 3 0 0 0 3 3h1V3zM15 3a3 3 0 0 1 3 3v1a3 3 0 0 1 0 6v1a3 3 0 0 1-3 3h-1V3z',
  clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2',
  copy: 'M20 9h-9a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-9a2 2 0 0 0-2-2zM5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1',
  file: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6',
  alert: 'M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01',
  terminal: 'M4 17l6-6-6-6M12 19h8',
} as const
