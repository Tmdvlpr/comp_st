import { useEffect, useRef, useState } from 'react'
import { animate, stagger } from 'animejs'
import { prefersReducedMotion } from '../../lib/motion'

export function SchemaPanel({ active }: { active: boolean }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const stylesRef = useRef<HTMLElement[]>([])
  const innerAnimatedRef = useRef(false)

  // Load schema HTML once on first activation
  useEffect(() => {
    if (!active || loaded || !containerRef.current) return

    const controller = new AbortController()

    fetch('/schema.html', { signal: controller.signal })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.text()
      })
      .then(html => {
        if (controller.signal.aborted) return
        const container = containerRef.current
        if (!container) return
        const doc = new DOMParser().parseFromString(html, 'text/html')

        // Шрифтовые <link> (Google Fonts) — узкий PT Sans Narrow, под который свёрстан
        // текст в SVG-чертеже. Без них при инъекции в дашборд шрифт не грузится → откат
        // на широкий → текст вылезает за края блоков. Инъектируем вместе со стилями.
        doc.querySelectorAll('link[rel="stylesheet"], link[rel="preconnect"]').forEach(link => {
          const l = document.createElement('link')
          Array.from(link.attributes).forEach(a => l.setAttribute(a.name, a.value))
          l.setAttribute('data-schema', '1')
          stylesRef.current.push(l)
        })

        // Collect style tags (don't inject yet — managed by active toggle below)
        doc.querySelectorAll('style').forEach(style => {
          const s = document.createElement('style')
          s.setAttribute('data-schema', '1')
          s.textContent = style.textContent
          stylesRef.current.push(s)
        })

        // Inject body HTML (scripts not yet active)
        container.innerHTML = doc.body.innerHTML

        // API через Vite-прокси /api → :8000 (см. vite.config.ts) — относительный путь
        ;(window as Window & { __SCHEMA_API_BASE?: string }).__SCHEMA_API_BASE = ''

        // Re-create and execute each <script> in order
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
  }, [active, loaded])

  // Add / remove schema styles from <head> when switching views
  useEffect(() => {
    if (!loaded) return
    if (active) {
      stylesRef.current.forEach(s => document.head.appendChild(s))
    } else {
      stylesRef.current.forEach(s => s.parentNode?.removeChild(s))
    }
  }, [active, loaded])

  // animejs: плавное появление контейнера при каждом переключении на «Схема КС»
  useEffect(() => {
    const el = containerRef.current
    if (!active || !loaded || !el || prefersReducedMotion()) return
    const a = animate(el, { opacity: [0, 1], translateY: [10, 0], duration: 300, ease: 'outCubic' })
    return () => { a.pause() }
  }, [active, loaded])

  // animejs: анимации внутри schema.html — только при первой загрузке.
  // #wrap transform трогать нельзя (pan/zoom скриптов); #panel/.hot .hl — тоже.
  // Безопасны: сайдбар .nb/.cat/.grp2, сам SVG-лист (#wrap opacity), #onboard.
  useEffect(() => {
    const el = containerRef.current
    if (!active || !loaded || !el || prefersReducedMotion() || innerAnimatedRef.current) return
    innerAnimatedRef.current = true

    // SVG-схема — fade-in без transform (transform занят pan/zoom)
    const wrap = el.querySelector<HTMLElement>('#wrap')
    if (wrap) {
      wrap.style.opacity = '0'
      animate(wrap, { opacity: [0, 1], duration: 480, ease: 'outCubic', delay: 120 })
    }

    // Сайдбар: каскад пунктов меню слева
    const navItems = el.querySelectorAll<HTMLElement>('.nb, .cat, .grp2')
    if (navItems.length) {
      animate(navItems, {
        opacity: [0, 1], translateX: [-10, 0],
        duration: 260, ease: 'outCubic', delay: stagger(10, { start: 200 }),
      })
    }

    // Onboarding-подсказка — появление снизу
    const onboard = el.querySelector<HTMLElement>('#onboard')
    if (onboard) {
      onboard.style.opacity = '0'
      animate(onboard, { opacity: [0, 1], translateY: [18, 0], duration: 380, ease: 'outCubic', delay: 500 })
    }
  }, [active, loaded])

  // Final cleanup on unmount
  useEffect(() => {
    return () => {
      // снос таймеров и глобальных слушателей schema.html (см. секцию 40)
      ;(window as Window & { __schemaCleanup?: () => void }).__schemaCleanup?.()
      stylesRef.current.forEach(s => s.parentNode?.removeChild(s))
    }
  }, [])

  if (error) {
    return (
      <div style={{
        flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'var(--text-3)', fontSize: 'var(--fs-sm)',
      }}>
        Ошибка загрузки схемы: {error}
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      style={{
        flex: 1,
        minHeight: 0,
        overflow: 'auto',
        width: '100%',
        display: active ? 'block' : 'none',
      }}
    />
  )
}
