import { useEffect } from 'react'
import { create } from 'zustand'

export type Theme = 'dark' | 'light'
export type Accent = 'cyan' | 'emerald' | 'amber' | 'rose' | 'violet' | 'slate'

export const ACCENTS: Accent[] = ['cyan', 'emerald', 'amber', 'rose', 'violet', 'slate']

const LS_THEME = 'vc.theme'
const LS_ACCENT = 'vc.accent'

interface ThemeStore {
  theme: Theme
  accent: Accent
  setTheme: (t: Theme) => void
  setAccent: (a: Accent) => void
  toggleTheme: () => void
}

function read<T extends string>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key)
    return (v as T) || fallback
  } catch {
    return fallback
  }
}

export const useTheme = create<ThemeStore>((set, get) => ({
  // La preferencia guardada manda; si no hay, se respeta el tema del sistema.
  // El frontend anterior forzaba 'dark' por defecto e ignoraba al usuario.
  theme: read<Theme>(
    LS_THEME,
    typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: light)').matches
      ? 'light'
      : 'dark',
  ),
  accent: read<Accent>(LS_ACCENT, 'cyan'),

  setTheme: (theme) => {
    try {
      localStorage.setItem(LS_THEME, theme)
    } catch {
      /* almacenamiento bloqueado: el tema igual se aplica en esta sesión */
    }
    set({ theme })
  },

  setAccent: (accent) => {
    try {
      localStorage.setItem(LS_ACCENT, accent)
    } catch {
      /* idem */
    }
    set({ accent })
  },

  toggleTheme: () => get().setTheme(get().theme === 'dark' ? 'light' : 'dark'),
}))

/** Aplica tema y acento al `<html>`, que es donde viven los tokens CSS. */
export function useApplyTheme(): void {
  const theme = useTheme((s) => s.theme)
  const accent = useTheme((s) => s.accent)

  useEffect(() => {
    const root = document.documentElement
    root.setAttribute('data-theme', theme)
    root.setAttribute('data-accent', accent)
    // `color-scheme` hace que los controles nativos (scrollbars, inputs) sigan
    // el tema. Sin esto, en light los selects y scrollbars quedan oscuros.
    root.style.colorScheme = theme
  }, [theme, accent])
}
