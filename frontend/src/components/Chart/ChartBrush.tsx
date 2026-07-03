import { useMemo, useRef, useState, memo } from 'react'
import type { CSSProperties } from 'react'

/**
 * Нижний браш (окно прокрутки/зума) в стиле bklit utility/brush: спарклайн всего
 * ряда + перетаскиваемое ШТРИХОВАННОЕ окно с ручками. Контролируемый компонент:
 * текущее окно приходит пропом `window`, перетаскивание эмитит `onWindow` (ms),
 * а родитель (SensorChart) задаёт диапазон X основного графика и обновляет `window`.
 *
 * Сам по себе лёгкий (один SVG-путь), поэтому, в отличие от родного rangeslider
 * Plotly (вторая мини-копия ВСЕХ рядов), не удваивает стоимость перерисовки.
 */
interface BrushPoint { t: string; v: number | null }
interface ChartBrushProps {
  points: BrushPoint[]
  theme: 'dark' | 'light'
  /** Текущее окно [t0,t1] в ms; null = весь диапазон. */
  window: { t0: number; t1: number } | null
  /** Перетаскивание окна/ручек → новый диапазон в ms. */
  onWindow: (t0: number, t1: number) => void
  height?: number
}

const _ms = (ts: string) => new Date(ts.replace(' ', 'T')).getTime()

export const ChartBrush = memo(function ChartBrush({ points, theme, window: win, onWindow, height = 50 }: ChartBrushProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const [dragging, setDragging] = useState(false)

  // Полный диапазон + сглаженный спарклайн (даунсэмпл ~240 точек, viewBox 0..100 × 0..100).
  const { minMs, maxMs, path } = useMemo(() => {
    if (points.length < 2) return { minMs: 0, maxMs: 1, path: '' }
    const t0 = _ms(points[0].t), t1 = _ms(points[points.length - 1].t)
    const span = (t1 - t0) || 1
    const step = Math.max(1, Math.floor(points.length / 240))
    const xs: number[] = [], vs: number[] = []
    for (let i = 0; i < points.length; i += step) {
      const v = points[i].v
      if (typeof v === 'number' && Number.isFinite(v)) {
        vs.push(v); xs.push(((_ms(points[i].t) - t0) / span) * 100)
      }
    }
    if (vs.length < 2) return { minMs: t0, maxMs: t1, path: '' }
    const lo = Math.min(...vs), hi = Math.max(...vs), vspan = (hi - lo) || 1
    const path = vs.map((v, i) =>
      `${i ? 'L' : 'M'}${xs[i].toFixed(2)},${(96 - ((v - lo) / vspan) * 88).toFixed(2)}`,
    ).join(' ')
    return { minMs: t0, maxMs: t1, path }
  }, [points])

  const span = (maxMs - minMs) || 1
  const w0 = win ? ((win.t0 - minMs) / span) * 100 : 0
  const w1 = win ? ((win.t1 - minMs) / span) * 100 : 100
  const left = Math.max(0, Math.min(100, Math.min(w0, w1)))
  const width = Math.max(0.6, Math.min(100 - left, Math.abs(w1 - w0)))

  // Перетаскивание: body = панорама окна, 'l'/'r' = изменение границ. Считаем в
  // долях трека, эмитим ms, троттлим через rAF (relayout основного графика недёшев).
  const startDrag = (mode: 'body' | 'l' | 'r') => (e: React.MouseEvent) => {
    e.preventDefault(); e.stopPropagation()
    setDragging(true)
    const track = trackRef.current
    if (!track) return
    const rect = track.getBoundingClientRect()
    const startX = e.clientX
    const startL = left, startR = left + width
    const wdt = startR - startL
    let raf = 0
    let pending: { t0: number; t1: number } | null = null
    const flush = () => { raf = 0; if (pending) onWindow(pending.t0, pending.t1) }
    const move = (ev: MouseEvent) => {
      const dFrac = ((ev.clientX - startX) / (rect.width || 1)) * 100
      let nl = startL, nr = startR
      if (mode === 'body') {
        nl = startL + dFrac; nr = startR + dFrac
        if (nl < 0) { nl = 0; nr = wdt }
        if (nr > 100) { nr = 100; nl = 100 - wdt }
      } else if (mode === 'l') {
        nl = Math.max(0, Math.min(startR - 1, startL + dFrac))
      } else {
        nr = Math.min(100, Math.max(startL + 1, startR + dFrac))
      }
      pending = { t0: minMs + (nl / 100) * span, t1: minMs + (nr / 100) * span }
      if (!raf) raf = requestAnimationFrame(flush)
    }
    const up = () => {
      setDragging(false)
      document.removeEventListener('mousemove', move, true)
      document.removeEventListener('mouseup', up, true)
      if (raf) cancelAnimationFrame(raf)
    }
    document.addEventListener('mousemove', move, true)
    document.addEventListener('mouseup', up, true)
  }

  const sparkColor = theme === 'dark' ? 'rgba(205,217,229,0.65)' : 'rgba(40,72,106,0.7)'
  const veil: CSSProperties = { position: 'absolute', top: 0, bottom: 0, background: 'color-mix(in srgb, var(--bg) 52%, transparent)' }
  const handleHit: CSSProperties = { position: 'absolute', top: 0, bottom: 0, width: 9, cursor: 'ew-resize', display: 'flex', alignItems: 'center', justifyContent: 'center' }
  const handleBar: CSSProperties = { width: 2, height: '46%', background: 'var(--accent)', borderRadius: 2 }

  return (
    <div style={{ position: 'relative', height, flexShrink: 0, marginTop: 6 }}>
      <div
        ref={trackRef}
        style={{
          position: 'absolute', inset: 0, overflow: 'hidden',
          borderRadius: 'var(--r-sm)', border: '1px solid var(--line)', background: 'var(--surface-2)',
        }}
      >
        {/* спарклайн всего ряда */}
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}>
          <path d={path} fill="none" stroke={sparkColor} strokeWidth={0.8} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        </svg>
        {/* затемнение вне выбранного окна */}
        <div aria-hidden="true" style={{ ...veil, left: 0, width: `${left}%` }} />
        <div aria-hidden="true" style={{ ...veil, left: `${left + width}%`, right: 0 }} />
        {/* выбранное окно: ДИАГОНАЛЬНАЯ штриховка + акцентный бордер + ручки */}
        <div
          role="slider"
          aria-label="Окно просмотра графика"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(left + width / 2)}
          tabIndex={0}
          onMouseDown={startDrag('body')}
          style={{
            position: 'absolute', top: 0, bottom: 0, left: `${left}%`, width: `${width}%`,
            cursor: dragging ? 'grabbing' : 'grab', border: '1px solid var(--accent)',
            transition: dragging ? undefined : 'left var(--dur-fast) var(--ease-standard), width var(--dur-fast) var(--ease-standard)',
            backgroundColor: 'color-mix(in srgb, var(--accent) 7%, transparent)',
            backgroundImage:
              'repeating-linear-gradient(45deg, color-mix(in srgb, var(--accent) 24%, transparent) 0, color-mix(in srgb, var(--accent) 24%, transparent) 1px, transparent 1px, transparent 6px)',
          }}
        >
          <div onMouseDown={startDrag('l')} style={{ ...handleHit, left: -4 }}><span style={handleBar} /></div>
          <div onMouseDown={startDrag('r')} style={{ ...handleHit, right: -4 }}><span style={handleBar} /></div>
        </div>
      </div>
    </div>
  )
})
