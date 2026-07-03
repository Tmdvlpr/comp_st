import { useEffect, useRef, useState } from 'react'
import { toggleTheme } from '../../lib/themeStore'

interface EngineViewProps {
  open: boolean
  gpa: string
  theme?: 'dark' | 'light'
  onClose: () => void
  onSelectSensor: (name: string) => void
}

// Полноэкранный «Двигатель»: реальная анимация НК-16-18СТ из public/engine.html,
// внедрённая полноценной инъекцией (как SchemaPanel со schema.html): HTML парсится,
// тело вставляется в контейнер, <style> добавляются в <head>, <script> пересоздаются
// и выполняются в контексте приложения (canvas, pan/zoom, параметры тракта).
export function EngineView({ open, theme = 'dark', onClose, onSelectSensor }: EngineViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const stylesRef = useRef<HTMLStyleElement[]>([])

  // engine.html инъектирован в то же окно → шлёт выбор датчика CustomEvent'ом
  // (клик по параметру/детали). Ловим и пробрасываем в App (откроет график).
  useEffect(() => {
    if (!open) return
    const h = (e: Event) => {
      const id = (e as CustomEvent).detail
      if (typeof id === 'string' && id) onSelectSensor(id)
    }
    window.addEventListener('engine-select-sensor', h as EventListener)
    return () => window.removeEventListener('engine-select-sensor', h as EventListener)
  }, [open, onSelectSensor])

  // Грузим engine.html один раз при первом открытии
  useEffect(() => {
    if (!open || loaded || !containerRef.current) return
    const controller = new AbortController()

    fetch('/engine.html', { signal: controller.signal })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text() })
      .then(html => {
        if (controller.signal.aborted) return
        const container = containerRef.current
        if (!container) return
        const doc = new DOMParser().parseFromString(html, 'text/html')

        // Стили engine.html — в <head> (управляем по open ниже). Помечаем data-engine.
        doc.querySelectorAll('style').forEach(style => {
          const s = document.createElement('style')
          s.setAttribute('data-engine', '1')
          s.textContent = style.textContent
          stylesRef.current.push(s)
        })

        // Тело в контейнер (скрипты ещё не активны)
        container.innerHTML = doc.body.innerHTML

        // Пересоздаём и выполняем <script> по порядку (canvas/анимация/обработчики)
        Array.from(container.querySelectorAll('script')).forEach(old => {
          const s = document.createElement('script')
          Array.from(old.attributes).forEach(a => s.setAttribute(a.name, a.value))
          s.textContent = old.textContent
          old.parentNode?.replaceChild(s, old)
        })

        setLoaded(true)
      })
      .catch(e => { if (e.name !== 'AbortError') setError(e.message) })

    return () => controller.abort()
  }, [open, loaded])

  // Стили engine.html добавляем в head только пока открыто (engine.html задаёт
  // body{background/cursor} — глобально нужно лишь на время полноэкранного режима)
  useEffect(() => {
    if (!loaded) return
    if (open) stylesRef.current.forEach(s => document.head.appendChild(s))
    else stylesRef.current.forEach(s => s.parentNode?.removeChild(s))
    // Сообщаем инъектированной сцене о видимости: при закрытии она ставит свой
    // 60fps WebGL-рендер на паузу (иначе крутился бы вечно в фоне после 1-го открытия).
    window.dispatchEvent(new CustomEvent('engine-visibility', { detail: open }))
  }, [open, loaded])

  // Тема движка СЛЕДУЕТ глобальной теме (themeStore): шлём событие в engine.html,
  // оно согласованно применяет CSS (body.is-light) и WebGL-шейдер 3D-модели. Раньше
  // движок имел свою независимую тему → отличался от системы и не синхронизировался.
  useEffect(() => {
    if (!open || !loaded) return
    window.dispatchEvent(new CustomEvent('engine-set-theme', { detail: theme === 'light' }))
    return () => { document.body.classList.remove('is-light') }
  }, [open, loaded, theme])

  // Кнопка темы внутри движка флипает ГЛОБАЛЬНУЮ тему (а не свою) → единый источник
  // правды, тема сохраняется и применяется на всех страницах.
  useEffect(() => {
    if (!open) return
    const h = () => toggleTheme()
    window.addEventListener('engine-toggle-theme', h)
    return () => window.removeEventListener('engine-toggle-theme', h)
  }, [open])

  // ESC — выход
  useEffect(() => {
    if (!open) return
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [open, onClose])

  // Очистка стилей при размонтировании
  useEffect(() => () => { stylesRef.current.forEach(s => s.parentNode?.removeChild(s)) }, [])

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Двигатель · режим просмотра"
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 60,
        background: '#000',
        transform: open ? 'translateY(0)' : 'translateY(100%)',
        transition: 'transform .5s cubic-bezier(.16,1,.3,1)',
        visibility: open ? 'visible' : 'hidden',
        overflow: 'hidden',
      }}
    >
      {/* Контейнер инъекции engine.html (canvas + подписи) */}
      <div ref={containerRef} style={{ position: 'absolute', inset: 0 }} />

      {error && (
        <div style={{
          position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'var(--text-3)', fontFamily: 'var(--font-mono)', fontSize: 14,
        }}>
          Ошибка загрузки анимации двигателя: {error}
        </div>
      )}

      {/* Плавающая кнопка выхода (поверх анимации) */}
      <button
        onClick={onClose}
        title="Выход (Esc)"
        style={{
          position: 'absolute', top: 14, left: 16, zIndex: 2,
          padding: '7px 14px', cursor: 'pointer',
          background: 'rgba(13,20,38,0.72)', backdropFilter: 'blur(6px)',
          border: '1px solid var(--line-2)', borderRadius: 'var(--r-md)',
          color: 'var(--text-1)', fontFamily: 'var(--font-display)', fontSize: 13,
          letterSpacing: '0.06em',
          transition: 'border-color var(--dur-fast) var(--ease-standard)',
        }}
        onMouseEnter={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--accent)' }}
        onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--line-2)' }}
      >
        ↓ Выход
      </button>
    </div>
  )
}
