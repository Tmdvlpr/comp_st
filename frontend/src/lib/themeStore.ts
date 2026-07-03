import { useSyncExternalStore } from 'react'

// Единый глобальный стор темы (один источник правды для лендинга, дашборда и движка).
// Раньше тема дублировалась в main.tsx и App.tsx отдельными useState → смена в одном
// месте не доходила до другого, а движок жил со своей кнопкой. Теперь все читают это
// хранилище: смена применяется к <html> мгновенно, пишется в localStorage и
// рассылается подписчикам. Движок синхронизируется отдельным событием (см. EngineView).

export type Theme = 'dark' | 'light'

const KEY = 'cs-theme'
const listeners = new Set<() => void>()

function read(): Theme {
  try { return (localStorage.getItem(KEY) as Theme) === 'light' ? 'light' : 'dark' }
  catch { return 'dark' }
}

let current: Theme = read()

function apply(t: Theme) {
  // Явное переключение обоих классов: .dark/.light всегда отражают активную тему,
  // каскад не зависит от «умолчания» и не конфликтует со сторонними стилями.
  if (typeof document !== 'undefined') {
    document.documentElement.classList.toggle('dark', t === 'dark')
    document.documentElement.classList.toggle('light', t === 'light')
  }
}
apply(current)   // применяем сразу при загрузке модуля

export function getTheme(): Theme { return current }

export function setTheme(t: Theme) {
  if (t === current) return
  current = t
  try { localStorage.setItem(KEY, t) } catch { /* localStorage может быть недоступен */ }
  apply(t)
  for (const l of listeners) l()
}

export function toggleTheme() {
  setTheme(current === 'dark' ? 'light' : 'dark')
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/** Подписка на тему: ре-рендер компонента при смене темы где угодно. */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, getTheme, getTheme)
}
