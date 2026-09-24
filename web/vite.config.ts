import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

// El backend es la fuente de verdad de la API. En desarrollo el dev server de
// Vite proxyea /api y /threads al backend para no pelear con CORS ni duplicar
// rutas; en produccion el bundle se sirve desde el propio FastAPI.
const BACKEND = process.env.VCORE_BACKEND ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: Object.fromEntries(
      [
        '/agents', '/sessions', '/threads', '/runs', '/system', '/llm',
        '/policy', '/embeddings', '/approvals', '/files', '/search', '/shell',
        '/observability', '/graph', '/state', '/git', '/log', '/tasks',
      ].map((p) => [p, { target: BACKEND, changeOrigin: true }]),
    ),
  },
  build: {
    // El bundle va a web_dist/ y el backend lo monta en /. Mantener el build
    // fuera de web/ evita que FastAPI sirva node_modules o los .tsx crudos.
    outDir: '../web_dist',
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        // CodeMirror y React se separan del bundle principal: el editor pesa
        // mas que toda la app y solo hace falta cuando el usuario abre un
        // artefacto. Un chunk unico de ~900 kB obliga a descargarlo siempre.
        manualChunks: {
          react: ['react', 'react-dom'],
          codemirror: [
            'codemirror',
            '@codemirror/state',
            '@codemirror/view',
            '@codemirror/language',
            '@codemirror/commands',
            '@codemirror/lang-yaml',
            '@codemirror/lang-json',
            '@codemirror/lang-python',
            '@codemirror/lang-javascript',
            '@codemirror/lang-markdown',
          ],
          query: ['@tanstack/react-query'],
        },
      },
    },
  },
})
