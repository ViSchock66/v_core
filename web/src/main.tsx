import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import './styles/index.css'

/**
 * Cliente de React Query.
 *
 * El backend es local: no tiene sentido reintentar agresivamente ni mantener
 * cache vieja. `refetchOnWindowFocus` queda apagado porque el estado del log ya
 * se actualiza por el stream, no por polling.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 5000,
      refetchOnWindowFocus: false,
    },
  },
})

const host = document.getElementById('root')
if (!host) throw new Error('No se encontró #root en el documento')

createRoot(host).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)
