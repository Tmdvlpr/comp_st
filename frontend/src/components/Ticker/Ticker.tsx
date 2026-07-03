import { useEffect, useRef } from 'react'
import { animate } from 'animejs'
import { prefersReducedMotion } from '../../lib/motion'

interface TickerProps {
  time: React.ReactNode
  onOpenDrawer: () => void
  unackedCount: number
  left?: React.ReactNode
  right?: React.ReactNode
}

// Верхняя панель: часы + статистика (left) + кнопка «Журнал» + свежесть данных (right).
// Бегущая лента событий убрана по требованию — события смотрят в «Журнале».
export function Ticker({ time, onOpenDrawer, unackedCount, left, right }: TickerProps) {
  // Pop-анимация бейджа при росте счётчика непринятых (bklit-badge-pop pattern)
  const badgeRef = useRef<HTMLSpanElement>(null)
  const prevCountRef = useRef(unackedCount)
  useEffect(() => {
    const prev = prevCountRef.current
    prevCountRef.current = unackedCount
    const el = badgeRef.current
    if (!el || unackedCount <= prev || prefersReducedMotion()) return
    animate(el, {
      scale: [0.6, 1.2, 1],
      duration: 320,
      ease: 'outCubic',
    })
  }, [unackedCount])

  return (
    <div
      className="relative flex items-center overflow-hidden font-mono"
      style={{ height: 36, background: 'var(--bg)', fontSize: 'var(--fs-xs)' }}
    >
      {/* Label: индикатор + часы */}
      <div
        className="flex items-center gap-2 h-full flex-shrink-0 z-10 font-bold uppercase"
        style={{
          paddingLeft: 'var(--space-5)',
          paddingRight: 'var(--space-4)',
          color: 'var(--text-3)',
          letterSpacing: '0.08em',
        }}
      >
        <span
          className="w-[5px] h-[5px] rounded-full animate-pulse-dot"
          style={{ background: 'var(--ok)', flexShrink: 0 }}
        />
        {time}
      </div>

      {left && <div className="flex items-center flex-shrink-0 h-full">{left}</div>}

      {/* Распорка вместо бегущей ленты — прижимает «Журнал»/свежесть вправо */}
      <div className="flex-1" />

      {/* Journal CTA */}
      <button
        onClick={onOpenDrawer}
        className="flex items-center gap-1 h-full px-4 flex-shrink-0 font-bold uppercase cursor-pointer transition-all duration-150"
        style={{ background: 'var(--bg)', color: 'var(--text-2)', fontSize: 'var(--fs-xs)', letterSpacing: '0.10em', border: 'none' }}
        onMouseEnter={e => { (e.currentTarget as HTMLElement).style.color = 'var(--accent)' }}
        onMouseLeave={e => { (e.currentTarget as HTMLElement).style.color = 'var(--text-2)' }}
      >
        {unackedCount > 0 && (
          <span ref={badgeRef} className="px-2 py-[1px] rounded-full font-bold text-white" style={{ background: 'var(--crit)', fontSize: 'var(--fs-xs)', display: 'inline-block' }}>
            {unackedCount}
          </span>
        )}
        <span>Журнал (J)</span>
        <span>↗</span>
      </button>

      {right && <div className="flex items-center flex-shrink-0 h-full">{right}</div>}
    </div>
  )
}
