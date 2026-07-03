import { useEffect, useRef, memo } from 'react'
import { animate, stagger, utils } from 'animejs'
import { prefersReducedMotion } from '../../lib/motion'
import { CHART_ENTER_EASE } from '../../lib/chartMotion'
import type { StatsResponse } from '../../types'

/* Число с animejs-пересчётом: при изменении значения цифра «добегает»
   от старого значения к новому и карточка слегка подпрыгивает */
function AnimatedValue({ value }: { value: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const prev = useRef(value)

  useEffect(() => {
    const el = ref.current
    if (!el || prev.current === value) return
    const from = prev.current
    prev.current = value
    if (prefersReducedMotion()) return
    const obj = { v: from }
    const a = animate(obj, {
      v: value, duration: 650, ease: 'outCubic',
      modifier: utils.round(0),
      onUpdate: () => { el.textContent = String(obj.v) },
    })
    const pop = animate(el, { scale: [1.12, 1], duration: 350, ease: 'outCubic' })
    return () => { a.pause(); pop.pause() }
  }, [value])

  return (
    <div
      ref={ref}
      className="font-bold leading-none"
      style={{
        fontFamily: 'var(--font-display)',
        // Von Restorff: ненулевая карточка «кричит» размером числа (2xl),
        // нулевая остаётся тихой (xl). Цвет числа НЕ трогаем — моно.
        fontSize: value > 0 ? 'var(--fs-2xl)' : 'var(--fs-xl)',
        letterSpacing: '-0.03em',
        // монохром: число одного цвета (яркое при >0, приглушённое при 0)
        color: value > 0 ? 'var(--text-1)' : 'var(--text-3)',
        fontVariantNumeric: 'tabular-nums',
        transformOrigin: 'left center',
        transition: 'font-size .16s',
      }}
    >
      {value}
    </div>
  )
}

interface StatCardProps {
  label: string
  value: number
  color: string
  active: boolean
  onClick: () => void
}

function StatCard({ label, value, color, active, onClick }: StatCardProps) {
  // Von Restorff: ненулевая карточка — «тревога» (alert), нулевая — тихая.
  // crit-порог (>=10) поднимает её до критической: единственное место, где
  // разрешён цвет (var(--crit)) — на левом борде, подсветке фона и CRIT-бейдже.
  const alert = value > 0
  const crit = value >= 10
  // нейтральная тревога для warn — жёлтый токен; цветная (красная) — для crit
  const warn = alert && !crit
  const sevColor = crit ? 'var(--crit)' : warn ? 'var(--warn)' : 'var(--line-2)'
  // фоновая подсветка тревоги (приоритет active над severity)
  const bgRest = active
    ? 'var(--accent-glow)'
    : crit
      ? 'color-mix(in srgb, var(--crit) 8%, var(--surface))'
      : warn
        ? 'color-mix(in srgb, var(--warn) 6%, var(--surface))'
        : 'var(--surface)'
  // тень: активная > крит > warn с кольцом, тихая — базовая
  const shadowRest = active
    ? '0 0 0 1px var(--accent), var(--shadow-lg)'
    : crit
      ? '0 0 0 1px color-mix(in srgb, var(--crit) 45%, transparent), var(--shadow-md)'
      : warn
        ? '0 0 0 1px color-mix(in srgb, var(--warn) 35%, transparent), var(--shadow-md)'
        : alert
          ? 'var(--shadow-lg)'
          : 'var(--shadow-md)'
  return (
    <button
      onClick={onClick}
      className="card relative flex flex-col min-w-0 overflow-hidden text-left cursor-pointer"
      style={{
        padding: 'var(--space-3) var(--space-4)',
        background: bgRest,
        borderColor: active ? 'var(--accent)' : 'var(--line)',
        // левый бордер по severity: толще и заметнее у тревоги, цветной у crit
        borderLeft: `${alert ? 3 : 1}px solid ${active ? 'var(--accent)' : alert ? sevColor : 'var(--line)'}`,
        boxShadow: shadowRest,
        transition: 'transform .16s, box-shadow .16s, background .16s, border-color .16s',
        transform: 'translateY(0)',
      }}
      onMouseEnter={e => {
        const el = e.currentTarget as HTMLElement
        el.style.transform = 'translateY(-2px)'
        if (!active) el.style.background = 'var(--surface-2)'
      }}
      onMouseLeave={e => {
        const el = e.currentTarget as HTMLElement
        el.style.transform = 'translateY(0)'
        if (!active) el.style.background = bgRest
      }}
    >
      <div
        className="font-bold uppercase flex items-center gap-1.5"
        style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', letterSpacing: '0.10em', marginBottom: 'var(--space-2)' }}
      >
        <span className="w-[6px] h-[6px] rounded-full" style={{ background: color, boxShadow: alert ? `0 0 6px ${color}` : 'none' }} />
        <span className="min-w-0 truncate">{label}</span>
        {/* КРИТ-бейдж — единственное цветное пятно по правилу пользователя */}
        {crit && (
          <span
            className="badge-sev crit ml-auto"
            style={{ fontSize: 'var(--fs-xs)', padding: '0 var(--space-1)', borderRadius: 'var(--r-sm)' }}
          >
            КРИТ
          </span>
        )}
      </div>
      <AnimatedValue value={value} />
    </button>
  )
}

interface StatsGridProps {
  stats: StatsResponse
  activeFilter: string | null
  onFilter: (kind: string | null) => void
}

export const StatsGrid = memo(function StatsGrid({ stats, activeFilter, onFilter }: StatsGridProps) {
  // Однократный каскадный вход карточек (staggered reveal в духе bklit) при первом
  // монтировании. Обновления значений анимирует AnimatedValue отдельно (без конфликта).
  const rootRef = useRef<HTMLDivElement>(null)
  const enteredRef = useRef(false)
  useEffect(() => {
    const root = rootRef.current
    if (!root || enteredRef.current || prefersReducedMotion()) return
    enteredRef.current = true
    const els = root.querySelectorAll('button')
    if (!els.length) return
    const a = animate(els, {
      opacity: [0, 1], translateY: [10, 0],
      duration: 520, ease: CHART_ENTER_EASE, delay: stagger(55),
    })
    return () => { a.pause() }
  }, [])

  // Монохром: точка/полоска карточек — единый нейтральный цвет (severity по числу/яркости)
  const ACCENT = 'var(--text-2)'
  const cards = [
    { key: 'ml',       label: 'Стат. выброс',     value: stats.ml_count,       color: ACCENT },
    { key: 'neg',      label: 'Физичность',        value: stats.neg_count,      color: ACCENT },
    { key: 'frozen',   label: 'Датчик завис',      value: stats.frozen_count,   color: ACCENT },
    { key: 'roc',      label: 'Скачок ΔV',         value: stats.roc_count,      color: ACCENT },
    { key: 'seasonal', label: 'Сезонная',          value: stats.seasonal_count, color: ACCENT },
    { key: 'regime',   label: 'Смена режима',      value: stats.regime_count,   color: ACCENT },
    { key: 'cross',    label: 'Кросс-ГПА',         value: stats.cross_count,    color: ACCENT },
  ]

  return (
    <div ref={rootRef} className="grid gap-2 flex-shrink-0" style={{ gridTemplateColumns: 'repeat(7, 1fr)' }}>
      {cards.map(c => (
        <StatCard
          key={c.key}
          label={c.label}
          value={c.value}
          color={c.color}
          active={activeFilter === c.key}
          onClick={() => onFilter(activeFilter === c.key ? null : c.key)}
        />
      ))}
    </div>
  )
})
