import { useState, useMemo, useRef, useEffect, useCallback, memo } from 'react'
import { animate, stagger } from 'animejs'
import { prefersReducedMotion } from '../../lib/motion'
import type { SensorMeta, Severity } from '../../types'
import { SEV_LABEL } from '../../types'
import { ruSensor } from '../../lib/sensorLabels'

interface SidebarProps {
  sensors: SensorMeta[]
  selectedId: string | null
  onSelect: (id: string) => void
  collapsed: boolean
  onToggleCollapse: () => void
  filteredSensorIds: Set<string> | null
  lastUpdated: string
}

// Монохром: точки одного цвета, severity передаётся прозрачностью (не цветом).
const SEV_DOT: Record<Severity, string> = {
  crit: 'var(--text-1)',
  warn: 'var(--text-1)',
  info: 'var(--text-1)',
  ok:   'var(--text-1)',
}
const SEV_OP: Record<Severity, number> = { crit: 1, warn: 0.6, info: 0.38, ok: 0.22 }
// Второй канал severity (для дальтонизма/слабовидения) помимо прозрачности: РАЗМЕР точки
// + кольцо у crit. Цвет остаётся монохромным по дизайн-правилу (цвет резервируем под heatmap).
const SEV_SIZE: Record<Severity, number> = { crit: 8, warn: 6, info: 5, ok: 4 }

const ROW_H_SENSOR = 28 // sensor button height

// --- Flattened row types for virtualisation ---
type RowGpa     = { kind: 'gpa';    gpa: string;   gpaSev: Severity; gpaSensors: SensorMeta[]; open: boolean }
type RowSub     = { kind: 'sub';    gpa: string;   sub: string;      subKey: string; subSev: Severity; subs: SensorMeta[]; open: boolean }
type RowSensor  = { kind: 'sensor'; sensor: SensorMeta; gpa: string; sub: string }
type Row = RowGpa | RowSub | RowSensor

function buildRows(
  byGpa: Record<string, Record<string, SensorMeta[]>>,
  expandedGpas: Record<string, boolean>,
  expandedSubs: Record<string, boolean>,
): Row[] {
  const rows: Row[] = []
  for (const gpa of Object.keys(byGpa).sort()) {
    const subsystems = byGpa[gpa]
    const gpaSensors = Object.values(subsystems).flat()
    const gpaSev: Severity = gpaSensors.some(s => s.severity === 'crit') ? 'crit'
      : gpaSensors.some(s => s.severity === 'warn') ? 'warn'
      : gpaSensors.some(s => s.severity === 'info') ? 'info'
      : 'ok'
    const gpaOpen = expandedGpas[gpa] ?? true
    rows.push({ kind: 'gpa', gpa, gpaSev, gpaSensors, open: gpaOpen })
    if (gpaOpen) {
      for (const [sub, subs] of Object.entries(subsystems)) {
        const subKey = `${gpa}-${sub}`
        const subSev: Severity = subs.some(s => s.severity === 'crit') ? 'crit'
          : subs.some(s => s.severity === 'warn') ? 'warn'
          : subs.some(s => s.severity === 'info') ? 'info' : 'ok'
        const subOpen = expandedSubs[subKey] ?? true
        rows.push({ kind: 'sub', gpa, sub, subKey, subSev, subs, open: subOpen })
        if (subOpen) {
          for (const sensor of subs) {
            rows.push({ kind: 'sensor', sensor, gpa, sub })
          }
        }
      }
    }
  }
  return rows
}

export const Sidebar = memo(function Sidebar({ sensors, selectedId, onSelect, collapsed, onToggleCollapse, filteredSensorIds }: SidebarProps) {
  const [rawSearch, setRawSearch] = useState('')
  const [search, setSearch] = useState('')
  const [expandedGpas, setExpandedGpas] = useState<Record<string, boolean>>({})
  const [expandedSubs, setExpandedSubs] = useState<Record<string, boolean>>({})
  const collapsedRef = useRef<HTMLDivElement>(null)
  const expandedRef = useRef<HTMLDivElement>(null)
  const navRef = useRef<HTMLDivElement>(null)

  // Плавающая кнопка сворачивания следует за курсором БЕЗ React-state: раньше
  // onMouseMove звал setState на каждый пиксель → ре-рендер всего дерева датчиков
  // (сотни кнопок) десятки раз/сек. Теперь двигаем DOM напрямую, коалесим в кадр.
  const collapsedBtnRef = useRef<HTMLDivElement>(null)
  const expandedBtnRef = useRef<HTMLDivElement>(null)
  const moveRafRef = useRef<number | undefined>(undefined)
  const followBtn = (container: HTMLDivElement | null, btn: HTMLDivElement | null, clientY: number) => {
    if (!container || !btn) return
    if (moveRafRef.current) cancelAnimationFrame(moveRafRef.current)
    moveRafRef.current = requestAnimationFrame(() => {
      moveRafRef.current = undefined
      const top = Math.max(16, Math.min(clientY - container.getBoundingClientRect().top - 14, container.clientHeight - 44))
      btn.style.top = `${top}px`
      btn.style.visibility = 'visible'
    })
  }
  const hideBtn = (btn: HTMLDivElement | null) => {
    if (moveRafRef.current) { cancelAnimationFrame(moveRafRef.current); moveRafRef.current = undefined }
    if (btn) btn.style.visibility = 'hidden'
  }
  useEffect(() => () => { if (moveRafRef.current) cancelAnimationFrame(moveRafRef.current) }, [])

  // Debounce search by 150ms to avoid synchronous re-filter on every keystroke
  useEffect(() => {
    const timer = setTimeout(() => setSearch(rawSearch), 150)
    return () => clearTimeout(timer)
  }, [rawSearch])

const filtered = useMemo(() => {
    if (!search) return sensors
    const q = search.toLowerCase()
    return sensors.filter(s =>
      s.name.toLowerCase().includes(q) ||
      ruSensor(s.name).toLowerCase().includes(q) ||
      s.tag.toLowerCase().includes(q))
  }, [sensors, search])

  const byGpa = useMemo(() => {
    const m: Record<string, Record<string, SensorMeta[]>> = {}
    for (const s of filtered) {
      if (!m[s.gpa]) m[s.gpa] = {}
      if (!m[s.gpa][s.subsystem]) m[s.gpa][s.subsystem] = []
      m[s.gpa][s.subsystem].push(s)
    }
    return m
  }, [filtered])

  // Stable toggle callbacks — prevent every SensorButton from re-rendering on each state update
  const toggleGpa = useCallback((gpa: string) =>
    setExpandedGpas(p => ({ ...p, [gpa]: !p[gpa] })), [])

  const toggleSub = useCallback((key: string) =>
    setExpandedSubs(p => ({ ...p, [key]: !p[key] })), [])

  // Stable select handler
  const handleSelect = useCallback((id: string) => onSelect(id), [onSelect])

  const { critCount, warnCount } = useMemo(() => ({
    critCount: sensors.filter(s => s.severity === 'crit').length,
    warnCount: sensors.filter(s => s.severity === 'warn').length,
  }), [sensors])

  // Build the flat row list for the virtualised list
  const rows = useMemo(
    () => buildRows(byGpa, expandedGpas, expandedSubs),
    [byGpa, expandedGpas, expandedSubs],
  )

  useEffect(() => {
    if (!selectedId || !navRef.current) return
    const el = navRef.current.querySelector(`[data-sensor-id="${CSS.escape(selectedId)}"]`)
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [selectedId, rows])

  // animejs: каскад групп при смене станции (сигнатура по первому датчику,
  // чтобы 30-секундный refetch с теми же id не перезапускал анимацию)
  const stationSig = sensors[0]?.id ?? ''
  useEffect(() => {
    const nav = expandedRef.current
    if (!nav || !stationSig || prefersReducedMotion()) return
    const groups = nav.querySelectorAll('.js-sub-group')
    if (!groups.length) return
    const a = animate(groups, {
      translateX: [-10, 0], opacity: [0, 1],
      duration: 320, ease: 'outCubic', delay: stagger(22),
    })
    return () => { a.pause() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stationSig])

  // Renderer for each virtualised row
  const renderRow = useCallback((index: number) => {
    const row = rows[index]
    if (!row) return null

    if (row.kind === 'gpa') {
      const { gpa, gpaSev, gpaSensors, open } = row
      return (
        <div key={gpa}>
          <button
            className="flex items-center justify-between rounded-sm select-none transition-colors"
            style={{ padding: 'var(--space-2) var(--space-3)', height: '100%', width: '100%', background: 'transparent', border: 'none', cursor: 'pointer', textAlign: 'left' }}
            onClick={() => toggleGpa(gpa)}
            aria-expanded={open}
            aria-label={`ГПА-${gpa.replace('GPA', '')}, ${gpaSensors.filter(s => s.severity !== 'ok').length} активных аномалий, ${open ? 'свернуть' : 'развернуть'}`}
            onMouseEnter={e => (e.currentTarget.style.background = 'var(--surface-2)')}
            onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
          >
            <div className="flex items-center gap-2" style={{ fontSize: 'var(--fs-xs)', fontWeight: 700, letterSpacing: '0.10em', textTransform: 'uppercase', color: 'var(--text-2)' }}>
              <span className="sev-dot" aria-hidden="true" style={{ background: SEV_DOT[gpaSev], opacity: SEV_OP[gpaSev] }} />
              ГПА-{gpa.replace('GPA', '')}
            </div>
            <div className="flex items-center gap-2">
              <span className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', fontWeight: 600 }}>
                {gpaSensors.filter(s => s.severity !== 'ok').length > 0
                  ? gpaSensors.filter(s => s.severity !== 'ok').length
                  : gpaSensors.length}
              </span>
              <span aria-hidden="true" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', transition: 'transform .2s', display: 'inline-block', transform: open ? 'rotate(0)' : 'rotate(-90deg)' }}>▾</span>
            </div>
          </button>
        </div>
      )
    }

    if (row.kind === 'sub') {
      const { subKey, sub, subSev, subs, open } = row
      return (
        <div key={subKey} className="js-sub-group" style={{ borderRadius: 'var(--r-sm)' }}>
          <button
            className="flex items-center gap-1 rounded-sm select-none transition-colors"
            style={{ padding: 'var(--space-1) var(--space-2)', fontSize: 'var(--fs-xs)', letterSpacing: '0.04em', textTransform: 'uppercase', color: 'var(--text-2)', fontWeight: 600, height: '100%', width: '100%', background: 'transparent', border: 'none', cursor: 'pointer', textAlign: 'left' }}
            onClick={() => toggleSub(subKey)}
            aria-expanded={open}
            aria-label={`${sub}, ${subs.length} датчиков, ${open ? 'свернуть' : 'развернуть'}`}
            onMouseEnter={e => { e.currentTarget.style.background = 'var(--surface-2)'; e.currentTarget.style.color = 'var(--text)' }}
            onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-2)' }}
          >
            <span aria-hidden="true" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', width: 12, display: 'inline-block', transition: 'transform .15s', transform: open ? 'rotate(0)' : 'rotate(-90deg)' }}>▾</span>
            <span className="flex-1">{sub}</span>
            <span className="inline-flex items-center gap-1 font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)' }}>
              <span aria-hidden="true" className="sev-dot" style={{ width: 6, height: 6, background: SEV_DOT[subSev], opacity: SEV_OP[subSev] }} />
              {subs.length}
            </span>
          </button>
        </div>
      )
    }

    // row.kind === 'sensor'
    const { sensor } = row
    const dimmed = filteredSensorIds !== null && !filteredSensorIds.has(sensor.id)
    const highlighted = filteredSensorIds !== null && filteredSensorIds.has(sensor.id)
    const baseOpacity = dimmed ? 0.15 : sensor.severity === 'ok' ? 0.55 : 1
    return (
      <div key={sensor.id}>
        <button
          data-sensor-id={sensor.id}
          onClick={() => handleSelect(sensor.id)}
          aria-label={`${ruSensor(sensor.name)}, ${SEV_LABEL[sensor.severity]}`}
          draggable
          onDragStart={e => {
            e.dataTransfer.setData('application/x-sensor-id', sensor.id)
            e.dataTransfer.effectAllowed = 'copy'
          }}
          title={`${ruSensor(sensor.name)} — перетащите на график, чтобы наложить`}
          className="w-full text-left rounded-sm flex items-center gap-1 font-mono"
          style={{
            padding: 'var(--space-1) var(--space-2)',
            fontSize: 'var(--fs-xs)',
            height: ROW_H_SENSOR,
            border: highlighted ? '1px solid var(--accent)' : '1px solid transparent',
            cursor: 'grab',
            background: selectedId === sensor.id ? 'var(--accent-glow)' : highlighted ? 'var(--accent-glow)' : 'transparent',
            color: selectedId === sensor.id ? 'var(--accent)' : 'var(--text-2)',
            opacity: baseOpacity,
            boxShadow: 'none',
            transition: 'background-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard), opacity var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard)',
          }}
          onMouseEnter={e => {
            if (selectedId !== sensor.id) {
              ;(e.currentTarget as HTMLElement).style.background = 'var(--surface-2)'
              ;(e.currentTarget as HTMLElement).style.color = 'var(--text)'
              ;(e.currentTarget as HTMLElement).style.opacity = '1'
            }
          }}
          onMouseLeave={e => {
            if (selectedId !== sensor.id) {
              ;(e.currentTarget as HTMLElement).style.background = highlighted ? 'var(--accent-glow)' : 'transparent'
              ;(e.currentTarget as HTMLElement).style.color = 'var(--text-2)'
              ;(e.currentTarget as HTMLElement).style.opacity = String(baseOpacity)
            }
          }}
        >
          <span
            className="flex-shrink-0 rounded-full"
            aria-hidden="true"
            style={{
              width: SEV_SIZE[sensor.severity], height: SEV_SIZE[sensor.severity],
              background: SEV_DOT[sensor.severity], opacity: SEV_OP[sensor.severity],
              // кольцо у crit — отличимо по форме, а не только по прозрачности
              boxShadow: sensor.severity === 'crit' ? '0 0 0 1.5px var(--surface), 0 0 0 3px var(--text-1)' : 'none',
            }}
          />
          <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap" title={ruSensor(sensor.name)}>
            {ruSensor(sensor.name)}
          </span>
        </button>
      </div>
    )
  }, [rows, selectedId, filteredSensorIds, handleSelect, toggleGpa, toggleSub])

  if (collapsed) {
    return (
      <div
        ref={collapsedRef}
        role="button"
        tabIndex={0}
        aria-label="Развернуть панель датчиков"
        aria-expanded={false}
        className="relative flex flex-col items-center"
        style={{ width: 48, background: 'var(--surface)', cursor: 'pointer', paddingBottom: 'var(--space-3)', gap: 0 }}
        onClick={onToggleCollapse}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggleCollapse() } }}
        onMouseMove={e => followBtn(collapsedRef.current, collapsedBtnRef.current, e.clientY)}
        onMouseLeave={() => hideBtn(collapsedBtnRef.current)}
      >
        {/* Vertical label */}
        <div
          className="font-mono font-bold flex-shrink-0"
          style={{
            writingMode: 'vertical-rl',
            textOrientation: 'mixed',
            transform: 'rotate(180deg)',
            fontSize: 13,
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
            color: 'var(--text-3)',
            flex: 1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          Датчики
        </div>

        {/* Severity indicators */}
        <div className="flex flex-col items-center gap-[6px] flex-shrink-0" style={{ marginTop: 10 }}>
          {critCount > 0 && (
            <div className="flex flex-col items-center gap-[2px]">
              <div style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--text-1)' }} />
              <span className="font-mono font-bold" style={{ fontSize: 9, color: 'var(--text-1)', lineHeight: 1 }}>{critCount}</span>
            </div>
          )}
          {warnCount > 0 && (
            <div className="flex flex-col items-center gap-[2px]">
              <div style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--text-1)', opacity: 0.6 }} />
              <span className="font-mono" style={{ fontSize: 9, color: 'var(--text-2)', lineHeight: 1 }}>{warnCount}</span>
            </div>
          )}
          {critCount === 0 && warnCount === 0 && sensors.length > 0 && (
            <div style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--text-1)', opacity: 0.22 }} />
          )}
        </div>

        <div
          ref={collapsedBtnRef}
          aria-hidden="true"
          style={{
            position: 'absolute',
            top: 16,
            left: '50%',
            transform: 'translateX(-50%)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 28,
            height: 28,
            background: 'var(--surface-3)',
            border: '1px solid var(--line-2)',
            borderRadius: 'var(--r-sm)',
            color: 'var(--text-2)',
            fontSize: 13,
            fontWeight: 700,
            pointerEvents: 'none',
            zIndex: 10,
            visibility: 'hidden',
          }}
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M3 2.5L6.5 6 3 9.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            <path d="M6.5 2.5L10 6 6.5 9.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </div>
      </div>
    )
  }

  return (
    <div
      ref={expandedRef}
      className="flex flex-col min-h-0 relative"
      style={{ width: 272, background: 'var(--surface)' }}
      onMouseMove={e => followBtn(expandedRef.current, expandedBtnRef.current, e.clientY)}
      onMouseLeave={() => hideBtn(expandedBtnRef.current)}
    >
      {/* Floating collapse button — всегда в DOM, видимость/позиция через ref (без re-render) */}
      <div
        ref={expandedBtnRef}
        role="button"
        tabIndex={0}
        aria-label="Свернуть панель датчиков"
        aria-expanded={true}
        onClick={onToggleCollapse}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggleCollapse() } }}
        onFocus={e => { e.currentTarget.style.visibility = 'visible'; e.currentTarget.style.top = '16px' }}
        onBlur={e => { e.currentTarget.style.visibility = 'hidden' }}
        style={{
          position: 'absolute',
          top: 16,
          right: -14,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          width: 28, height: 28,
          background: 'var(--surface-3)',
          border: '1px solid var(--line-2)',
          borderRadius: 'var(--r-sm)',
          color: 'var(--text-2)',
          cursor: 'pointer',
          zIndex: 20,
          visibility: 'hidden',
        }}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path d="M9 2.5L5.5 6 9 9.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          <path d="M5.5 2.5L2 6l3.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </div>

      {/* Search */}
      <div style={{ padding: 'var(--space-2) var(--space-3)' }}>
        <input
          id="sensor-search"
          aria-label="Поиск датчика"
          value={rawSearch}
          onChange={e => setRawSearch(e.target.value)}
          placeholder="Поиск датчика..."
          style={{
            width: '100%',
            background: 'var(--surface-2)',
            border: '1px solid transparent',
            borderRadius: 'var(--r-md)',
            padding: 'var(--space-2) var(--space-3)',
            color: 'var(--text)',
            fontSize: 'var(--fs-xs)',
            fontFamily: 'inherit',
            outline: 'none',
          }}
          onFocus={e => (e.target.style.borderColor = 'var(--accent)')}
          onBlur={e => (e.target.style.borderColor = 'transparent')}
        />
      </div>

      {/* Nav */}
      <div ref={navRef} className="flex-1 min-h-0" style={{ padding: 'var(--space-1) var(--space-2)', overflowY: 'auto', overflowX: 'hidden' }}>
        {rows.map((_, i) => renderRow(i))}
      </div>

      {/* Footer */}
    </div>
  )
})
