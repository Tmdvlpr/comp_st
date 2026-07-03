import { useMemo, useRef, useEffect, memo } from 'react'
import { animate, stagger } from 'animejs'
import { prefersReducedMotion } from '../../lib/motion'
import type { HeatmapCell, Severity } from '../../types'
import { SEV_LABEL_LONG } from '../../types'
import { ruSensor } from '../../lib/sensorLabels'

interface HeatMapProps {
  cells: HeatmapCell[]
  selectedSensorId: string | null
  onSelect: (id: string) => void
  filteredSensorIds: Set<string> | null
}

// CSS-переменные вместо хардкод-rgba: правильно переключаются между светлой и тёмной темой
const SEV_CLASS: Record<Severity, { bg: string; border: string }> = {
  crit: { bg: 'color-mix(in srgb, var(--crit) 20%, transparent)',  border: 'color-mix(in srgb, var(--crit) 55%, transparent)' },
  warn: { bg: 'color-mix(in srgb, var(--warn) 18%, transparent)',  border: 'color-mix(in srgb, var(--warn) 50%, transparent)' },
  info: { bg: 'color-mix(in srgb, var(--info) 14%, transparent)',  border: 'color-mix(in srgb, var(--info) 40%, transparent)' },
  ok:   { bg: 'color-mix(in srgb, var(--ok)   14%, transparent)',  border: 'color-mix(in srgb, var(--ok)   35%, transparent)' },
}

const LEGEND = [
  { sev: 'crit' as Severity, label: SEV_LABEL_LONG.crit },
  { sev: 'warn' as Severity, label: SEV_LABEL_LONG.warn },
  { sev: 'ok'   as Severity, label: SEV_LABEL_LONG.ok },
  { sev: 'info' as Severity, label: SEV_LABEL_LONG.info },
]

export const HeatMap = memo(function HeatMap({ cells, selectedSensorId, onSelect, filteredSensorIds }: HeatMapProps) {
  const byGpaName = useMemo(() => {
    const m = new Map<string, Map<string, HeatmapCell>>()
    for (const c of cells) {
      let g = m.get(c.gpa)
      if (!g) { g = new Map(); m.set(c.gpa, g) }
      g.set(c.name, c)
    }
    return m
  }, [cells])

  const gpas = useMemo(() => [...byGpaName.keys()].sort(), [byGpaName])
  const sensorNames = useMemo(
    () => Array.from(new Set(cells.map(c => c.name))).sort(),
    [cells]
  )

  // animejs: каскад строк при смене станции/первой загрузке
  // (сигнатура по первому датчику — refetch с теми же id не перезапускает)
  const rootRef = useRef<HTMLDivElement>(null)
  const dataSig = cells[0]?.sensor_id ?? ''
  useEffect(() => {
    const root = rootRef.current
    if (!root || !dataSig || prefersReducedMotion()) return
    const rows = root.querySelectorAll('.js-heat-row')
    if (!rows.length) return
    const a = animate(rows, {
      opacity: [0, 1], duration: 280, ease: 'outCubic', delay: stagger(9),
    })
    return () => { a.pause() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataSig])

  return (
    <div ref={rootRef} className="flex flex-col min-h-0">
      {/* Legend */}
      <div className="flex flex-wrap gap-x-3 gap-y-1 flex-shrink-0 mb-2">
        {LEGEND.map(({ sev, label }) => (
          <div key={sev} className="flex items-center gap-[5px] font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)' }}>
            <span
              aria-hidden="true"
              style={{
                width: 8, height: 8, borderRadius: 0, display: 'inline-block',
                background: SEV_CLASS[sev].bg,
                border: `1px solid ${SEV_CLASS[sev].border}`,
              }}
            />
            {label}
          </div>
        ))}
      </div>

      {/* Table */}
      <div className="overflow-y-auto flex-1 min-h-0" style={{ scrollbarWidth: 'thin', overflowX: 'hidden' }}>
        <table style={{ borderCollapse: 'separate', borderSpacing: 2, width: '100%' }}>
          <caption style={{ position: 'absolute', width: 1, height: 1, padding: 0, margin: -1, overflow: 'hidden', clip: 'rect(0,0,0,0)', whiteSpace: 'nowrap', borderWidth: 0 }}>
            Тепловая карта критичности датчиков
          </caption>
          <thead>
            <tr>
              <th scope="col" style={{ width: 160, position: 'sticky', top: 0, background: 'var(--surface)', zIndex: 1 }} />
              {gpas.map(gpa => (
                <th
                  key={gpa}
                  scope="col"
                  className="font-mono font-semibold text-center"
                  style={{
                    fontSize: 'var(--fs-xs)',
                    color: 'var(--text-3)',
                    padding: '3px 0',
                    letterSpacing: '0.04em',
                    position: 'sticky', top: 0,
                    background: 'var(--surface)',
                    zIndex: 1,
                  }}
                >
                  ГПА-{gpa.replace('GPA', '')}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sensorNames.map(name => (
              <tr key={name} className="js-heat-row">
                <th
                  scope="row"
                  className="font-mono whitespace-nowrap overflow-hidden text-ellipsis"
                  style={{
                    fontSize: 'var(--fs-xs)',
                    color: 'var(--text-3)',
                    padding: '2px 6px 2px 0',
                    maxWidth: 160,
                    width: 160,
                    fontWeight: 'normal',
                    textAlign: 'left',
                  }}
                  title={ruSensor(name)}
                >
                  {ruSensor(name)}
                </th>
                {gpas.map(gpa => {
                  const cell = byGpaName.get(gpa)?.get(name)
                  if (!cell) {
                    return (
                      <td
                        key={gpa}
                        title={`${name} · ГПА-${gpa.replace('GPA', '')} · датчик отсутствует на этом агрегате`}
                        style={{
                          width: 28, height: 24,
                          borderRadius: 0,
                          background: 'transparent',
                          border: '1px dashed var(--line)',
                          opacity: 0.35,
                        }}
                      />
                    )
                  }
                  const s = SEV_CLASS[cell.severity]
                  const isSelected = selectedSensorId === cell.sensor_id
                  const dimmed = filteredSensorIds !== null && !filteredSensorIds.has(cell.sensor_id)
                  const highlighted = filteredSensorIds !== null && filteredSensorIds.has(cell.sensor_id)
                  // подпись для title и aria-label (дублируется для скринридеров)
                  const cellLabel = `${ruSensor(cell.name)} · ГПА-${gpa.replace('GPA', '')} · ${SEV_LABEL_LONG[cell.severity]} · ${cell.anomaly_count} аномалий`
                  return (
                    <td
                      key={gpa}
                      role="button"
                      tabIndex={0}
                      aria-label={cellLabel}
                      onClick={() => onSelect(cell.sensor_id)}
                      onKeyDown={e => {
                        // Enter/Space — выбор ячейки с клавиатуры
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onSelect(cell.sensor_id)
                        }
                      }}
                      draggable
                      onDragStart={e => {
                        e.dataTransfer.setData('application/x-sensor-id', cell.sensor_id)
                        e.dataTransfer.effectAllowed = 'copy'
                      }}
                      style={{
                        width: 28, height: 24,
                        borderRadius: 0,
                        background: s.bg,
                        border: isSelected ? '2px solid var(--accent)' : highlighted ? `2px solid ${s.border}` : `1px solid ${s.border}`,
                        cursor: 'grab',
                        // явный список вместо 'all': не отслеживаем layout-свойства,
                        // сохраняем плавные fade прозрачности/бордера/подсветки
                        transition: 'transform .15s ease, filter .15s ease, opacity .15s ease, border-color .15s ease',
                        transform: isSelected ? 'scale(1.3)' : undefined,
                        zIndex: isSelected ? 3 : undefined,
                        position: isSelected ? 'relative' : undefined,
                        opacity: dimmed ? 0.15 : 1,
                        filter: highlighted ? 'brightness(1.6)' : undefined,
                      }}
                      title={cellLabel}
                      onMouseEnter={e => {
                        const el = e.currentTarget as HTMLElement
                        el.style.transform = 'scale(1.3)'
                        el.style.zIndex = '2'
                        el.style.position = 'relative'
                        el.style.filter = 'brightness(1.5)'
                      }}
                      onMouseLeave={e => {
                        const el = e.currentTarget as HTMLElement
                        if (!isSelected) {
                          el.style.transform = ''
                          el.style.zIndex = ''
                          el.style.position = ''
                          el.style.filter = ''
                        }
                      }}
                    />
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
})
