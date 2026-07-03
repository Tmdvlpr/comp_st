import { StrictMode, useState, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider, QueryCache } from '@tanstack/react-query'
import App from './App.tsx'
import { StationOverview } from './components/Overview/StationOverview.tsx'
import { ErrorBoundary } from './components/ErrorBoundary/ErrorBoundary.tsx'
import { reportApiError, clearApiError } from './api/errorStore'
import { useTheme, toggleTheme } from './lib/themeStore'

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: () => reportApiError(),
    onSuccess: () => {
      // Очищаем ошибку только когда не осталось других запросов в состоянии error
      if (queryClient.getQueryCache().findAll({ predicate: q => q.state.status === 'error' }).length === 0) {
        clearApiError()
      }
    },
  }),
  defaultOptions: {
    queries: {
      retry: 1,
      retryDelay: (attemptIndex: number) => Math.min(1000 * 2 ** attemptIndex, 30000),
      staleTime: 10_000,
    },
  },
})

// Станция в URL-хэше (#/s/<id>): выбор переживает перезагрузку (F5) и шарится ссылкой;
// пустой хэш = экран-обзор станций.
function stationFromHash(): string | null {
  const m = location.hash.match(/^#\/s\/([^/?#]+)/)
  return m ? decodeURIComponent(m[1]) : null
}

// main.tsx — точка входа (createRoot ниже), не модуль компонентов: правило
// react-refresh здесь неприменимо.
// eslint-disable-next-line react-refresh/only-export-components
function Root() {
  // null = экран-обзор станций (главный); иначе — детальный вид выбранной станции
  const [station, setStation] = useState<string | null>(() => stationFromHash())
  const [lang, setLang] = useState<'UZ' | 'RU'>(() => (localStorage.getItem('cs-lang') as 'UZ' | 'RU') ?? 'RU')
  const theme = useTheme()   // единый стор темы (общий с дашбордом и движком)

  useEffect(() => { localStorage.setItem('cs-lang', lang) }, [lang])

  // Кнопки назад/вперёд браузера → синхронизируем выбранную станцию с хэшем.
  useEffect(() => {
    const onHash = () => setStation(stationFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = (id: string | null) => {
    const target = id ? `#/s/${encodeURIComponent(id)}` : '#/'
    if (location.hash !== target) location.hash = target
    setStation(id)
  }

  const content = station == null
    ? <StationOverview
        lang={lang} theme={theme}
        onSelect={id => { go(id) }}
        onLangChange={setLang}
        onThemeToggle={toggleTheme}
      />
    : <App initialStation={station} onBackToOverview={() => go(null)} />

  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        {content}
      </QueryClientProvider>
    </ErrorBoundary>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)

