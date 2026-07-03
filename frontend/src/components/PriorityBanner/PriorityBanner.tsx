import { useMemo } from 'react'
import type { EventItem } from '../../types'
import { KIND_LABEL, SEV_COLOR, SEV_LABEL } from '../../types'
import { ruSensor } from '../../lib/sensorLabels'

interface PriorityBannerProps {
  events: EventItem[]
  onSelect: (ev: EventItem) => void
}

export function PriorityBanner({ events, onSelect }: PriorityBannerProps) {
  const top = useMemo<EventItem | null>(() => {
    let best: EventItem | null = null
    for (const ev of events) {
      if (ev.acked) continue
      if (ev.severity !== 'crit' && ev.severity !== 'warn') continue
      if (!best) { best = ev; continue }
      // crit outranks warn; within same severity, newer wins
      const bestRank = best.severity === 'crit' ? 2 : 1
      const evRank = ev.severity === 'crit' ? 2 : 1
      if (evRank > bestRank || (evRank === bestRank && ev.timestamp > best.timestamp)) best = ev
    }
    return best
  }, [events])

  if (!top) return null

  // Цвет точки-индикатора отражает severity (тепловой индикатор — цвет допустим)
  const dotColor = SEV_COLOR[top.severity]
  // По правилу канона рамку красим только для CRIT; для warn — нейтральная линия
  const isCrit = top.severity === 'crit'
  const edgeColor = isCrit ? 'var(--crit)' : 'var(--line)'
  const time = new Date(top.timestamp).toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  })

  return (
    <button
      onClick={() => onSelect(top)}
      className="flex items-center w-full text-left cursor-pointer font-mono anim-fade-up"
      style={{
        flexShrink: 0,
        gap: 'var(--space-3)',
        padding: 'var(--space-2) var(--space-4)',
        borderRadius: 'var(--r-md)',
        background: 'var(--surface-2)',
        border: `1px solid ${edgeColor}`,
        borderLeft: `3px solid ${edgeColor}`,
        color: 'var(--text-2)',
        fontSize: 'var(--fs-sm)',
        letterSpacing: '0.02em',
      }}
      title="Перейти к датчику"
    >
      <span
        className={top.severity === 'crit' ? 'animate-pulse-dot' : ''}
        style={{
          width: 8, height: 8, borderRadius: '50%', background: dotColor, flexShrink: 0,
        }}
      />
      <span className={`badge-sev ${top.severity}`} style={{ flexShrink: 0 }}>{SEV_LABEL[top.severity]}</span>
      <span style={{ color: 'var(--text-1)', fontWeight: 600, flexShrink: 0 }}>{top.gpa}</span>
      <span style={{ color: 'var(--text-1)', fontWeight: 600, flexShrink: 0 }}>{ruSensor(top.sensor_name)}</span>
      <span style={{ color: 'var(--text-3)' }}>—</span>
      <span style={{ flexShrink: 0 }}>{KIND_LABEL[top.kind] ?? top.kind}</span>
      <span className="overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: 'var(--text-3)', flex: 1, minWidth: 0 }}>
        {top.description}
      </span>
      <span style={{ color: 'var(--text-3)', flexShrink: 0 }}>{time}</span>
    </button>
  )
}
