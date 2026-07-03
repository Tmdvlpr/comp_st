import { memo } from 'react'

/**
 * Интерактивная легенда в стиле bklit (utility/legend): наведение на запись →
 * подсветка её и (через onHover) акцент линии на графике с приглушением прочих;
 * наведение на линию графика подсвечивает соответствующую запись (через activeKey,
 * который выставляет ховер-хук по ближайшей к курсору серии). Нативная легенда
 * Plotly так не умеет — поэтому она отключена (showlegend:false), а рисуем свою.
 */
export interface LegendItem {
  key: string
  name: string
  color: string
  /** пунктирная линия (напр. «Модель») → штриховой свотч */
  dash?: boolean
}

interface ChartLegendProps {
  items: LegendItem[]
  activeKey?: string | null
  onHover?: (key: string) => void
  onLeave?: () => void
}

export const ChartLegend = memo(function ChartLegend({ items, activeKey, onHover, onLeave }: ChartLegendProps) {
  if (!items.length) return null
  const dimmed = activeKey != null
  return (
    <div
      className="font-mono"
      style={{
        display: 'flex', flexWrap: 'wrap', gap: '6px 16px', justifyContent: 'center',
        alignItems: 'center', padding: '2px 8px 6px', fontSize: 11, flexShrink: 0,
      }}
    >
      {items.map(it => {
        const active = activeKey === it.key
        return (
          <button
            key={it.key}
            type="button"
            onMouseEnter={() => onHover?.(it.key)}
            onMouseLeave={() => onLeave?.()}
            aria-pressed={active}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6, padding: '1px 2px',
              background: 'none', border: 'none', cursor: 'pointer', lineHeight: 1.2,
              color: active ? 'var(--text-1)' : 'var(--text-2)',
              fontWeight: active ? 700 : 400,
              // приглушаем неактивные, когда что-то выбрано (наведением на линию или запись)
              opacity: dimmed && !active ? 0.4 : 1,
              transition: 'opacity .15s ease, color .15s ease',
            }}
          >
            <span
              aria-hidden="true"
              style={{
                width: 16, flexShrink: 0,
                ...(it.dash
                  ? { height: 0, borderTop: `2px dashed ${it.color}` }
                  : { height: 3, background: it.color, borderRadius: 2 }),
              }}
            />
            <span
              style={{
                whiteSpace: 'nowrap', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis',
                borderBottom: active ? `2px solid ${it.color}` : '2px solid transparent',
                paddingBottom: 1,
              }}
            >
              {it.name}
            </span>
          </button>
        )
      })}
    </div>
  )
})
