import { useEffect, useState, useMemo, useRef } from 'react'
import { animate, stagger } from 'animejs'
import { prefersReducedMotion } from '../../lib/motion'
import { useQuery } from '@tanstack/react-query'
import type { EventItem, Severity, SensorMeta } from '../../types'
import { SEV_COLOR, SEV_TEXT_ON_SOLID } from '../../types'
import { ruSensor } from '../../lib/sensorLabels'
import { api } from '../../api/client'
import { useModal } from '../../lib/useModal'
import { fmtStation } from '../../lib/time'
import { isRunning } from '../../lib/gpa'

interface GpaStats {
  gpa: string
  score: number
  severity: Severity
  crit: number
  warn: number
  info: number
  ok: number
  total: number
  recentEvents: EventItem[]
  running: boolean
  rpmValue: number | null
}

interface KioskModeProps {
  active: boolean
  onExit: () => void
  onSelect: (ev: EventItem) => void
  events: EventItem[]
  sensorCount: number
  sensors: SensorMeta[]
  stationId?: string
}

function Clock() {
  const [now, setNow] = useState(new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  return (
    <div>
      <div className="font-mono font-bold" style={{ fontSize: 'var(--fs-2xl)', lineHeight: 1, letterSpacing: '-0.02em', color: 'var(--text)', fontVariantNumeric: 'tabular-nums lining-nums' }}>
        {fmtStation(now, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
      </div>
      <div className="font-mono text-right" style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-3)', marginTop: 'var(--space-1)', letterSpacing: '0.05em' }}>
        {fmtStation(now, { weekday: 'long', day: 'numeric', month: 'long' })}
      </div>
    </div>
  )
}

export function KioskMode({ active, onExit, onSelect, events, sensorCount, sensors, stationId = 'ohangaron' }: KioskModeProps) {
  const dialogRef = useModal<HTMLDivElement>(active, onExit)
  const tilesRef = useRef<HTMLDivElement>(null)

  // Каскадное появление плиток ГПА при открытии киоска
  useEffect(() => {
    if (!active || prefersReducedMotion()) return
    const tiles = tilesRef.current?.querySelectorAll<HTMLElement>('.kiosk-tile')
    if (!tiles?.length) return
    tiles.forEach(t => { t.style.opacity = '0' })
    const a = animate(tiles, {
      opacity: [0, 1],
      translateY: [16, 0],
      duration: 360,
      ease: 'outCubic',
      delay: stagger(90, { start: 140 }),
    })
    return () => { a.pause() }
  }, [active])

  // Ref-обёртка onExit: keydown-обработчик читает актуальную ссылку, не захватывая
  // устаревшее замыкание при смене prop-функции в родителе между рендерами.
  const onExitRef = useRef(onExit)
  useEffect(() => { onExitRef.current = onExit }, [onExit])
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.key === 'k' || e.key === 'K') && !e.ctrlKey && !e.metaKey) onExitRef.current()
      if (e.key === 'Escape') onExitRef.current()
    }
    if (active) window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [active])

  // Обороты — из живого SCADA-снимка (rpm_tvd = GT-{n}.GT-{n}.GT01.CTRL.IN[1].VALUE).
  // rpm — conditioning-фичи, их нет в списке моделей, поэтому берём из pvsnapshot.
  const { data: snap } = useQuery({
    queryKey: ['pvsnapshot', stationId],
    queryFn: () => api.pvsnapshot(stationId),
    enabled: active,
    refetchInterval: 30_000,
  })
  const tags = useMemo(() => {
    const o = (snap ?? {}) as Record<string, unknown>
    return (o.tags && typeof o.tags === 'object' ? o.tags : o) as Record<string, unknown>
  }, [snap])
  const rpmOf = (gpa: string): number | null => {
    const n = gpa.replace('GPA', '')
    const e = tags[`GT-${n}.GT-${n}.GT01.CTRL.IN[1].VALUE`]
    const v = typeof e === 'number' ? e : (e as { v?: number } | undefined)?.v
    return typeof v === 'number' && Number.isFinite(v) ? v : null
  }

  const gpaStats: GpaStats[] = useMemo(() => {
    const gpas = [...new Set(sensors.map(s => s.gpa))].sort()
    const tenDaysAgo = new Date(Date.now() - 10 * 24 * 60 * 60 * 1000).toISOString()
    return gpas.map(gpa => {
      const gpaEvents = events.filter(e => e.gpa === gpa && e.timestamp >= tenDaysAgo)
      const crit = gpaEvents.filter(e => e.severity === 'crit').length
      const warn = gpaEvents.filter(e => e.severity === 'warn').length
      const info = gpaEvents.filter(e => e.severity === 'info').length
      const ok   = gpaEvents.filter(e => e.severity === 'ok').length
      const total = gpaEvents.length
      const score = total > 0 ? Math.max(0, Math.round(100 - crit * 10 - warn * 3 - info * 0.5)) : 100
      const severity: Severity = crit > 0 ? 'crit' : warn > 0 ? 'warn' : info > 0 ? 'info' : 'ok'

      // Обороты ТВД из живого SCADA-снимка; running — по общему правилу (как в ShiftReport)
      const rpmValue = rpmOf(gpa)
      const running = isRunning(rpmValue)

      return {
        gpa, score, severity,
        crit, warn, info, ok, total,
        recentEvents: gpaEvents.slice(0, 5),
        running, rpmValue,
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, sensors, tags])

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label="Дежурный режим"
      aria-hidden={!active}
      className="fixed inset-0 z-[100] flex flex-col overflow-hidden transition-[opacity,visibility,transform] duration-200"
      style={{
        background: 'var(--bg)',
        // 40px по горизонтали — не из шкалы, осознанный широкий полевой отступ киоска
        padding: 'var(--space-6) 40px',
        gap: 'var(--space-5)',
        opacity: active ? 1 : 0,
        // visibility скрывает из tab-order при закрытом состоянии
        visibility: active ? 'visible' : 'hidden',
        pointerEvents: active ? 'all' : 'none',
        transform: active ? 'translateY(0)' : 'translateY(6px)',
      }}
    >
      {/* Exit hint */}
      <button
        onClick={onExit}
        className="absolute font-mono font-bold flex items-center gap-1 rounded-full transition-all"
        style={{
          top: 20, left: '50%', transform: 'translateX(-50%)',
          background: 'var(--surface)',
          border: '1px solid var(--line)',
          color: 'var(--text-2)',
          padding: 'var(--space-2) var(--space-4)',
          fontSize: 'var(--fs-xs)',
          letterSpacing: '0.08em',
          cursor: 'pointer',
          zIndex: 10,
          boxShadow: 'var(--shadow-md)',
        }}
        onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = 'var(--accent-strong)'; (e.currentTarget as HTMLElement).style.color = 'var(--on-accent)'; (e.currentTarget as HTMLElement).style.borderColor = 'var(--accent-strong)' }}
        onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = 'var(--surface)'; (e.currentTarget as HTMLElement).style.color = 'var(--text-2)'; (e.currentTarget as HTMLElement).style.borderColor = 'var(--line)' }}
        title="Выйти (K или Esc)"
        aria-label="Выйти из дежурного режима (K или Esc)"
      >
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" style={{ flexShrink: 0 }}>
          <path d="M5 2H2.5A.5.5 0 0 0 2 2.5v8a.5.5 0 0 0 .5.5H5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
          <path d="M8.5 4.5 11 6.5 8.5 8.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
          <path d="M5.5 6.5h5.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
        </svg>
        ВЫЙТИ
      </button>

      {/* Header */}
      <div className="flex items-flex-end justify-between border-b border-[var(--line)] pb-4 flex-shrink-0">
        <p className="font-mono font-bold" style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-3)', letterSpacing: '0.08em' }}>
          <span
            className="inline-block w-2 h-2 rounded-full mr-2 animate-pulse-dot"
            style={{ background: 'var(--crit)', verticalAlign: 'middle' }}
          />
          ДЕЖУРНЫЙ РЕЖИМ · {sensorCount} датчиков
        </p>
        <Clock />
      </div>

      {/* GPA tiles */}
      <div ref={tilesRef} className="flex-1 grid min-h-0" style={{ gridTemplateColumns: 'repeat(3, 1fr)', gap: 'var(--space-5)' }}>
        {gpaStats.map(g => (
          <div
            key={g.gpa}
            className="kiosk-tile flex flex-col relative overflow-hidden"
            style={{
              borderRadius: 'var(--r-lg)',
              border: `1.5px solid ${g.severity !== 'ok' ? SEV_COLOR[g.severity] : g.running ? 'var(--ok)' : 'var(--line)'}`,
              background: g.running
                ? 'color-mix(in srgb, var(--ok) 6%, var(--surface))'
                : 'color-mix(in srgb, var(--text-3) 4%, var(--surface))',
              // padding плитки — крупный карточный, оставлен вне шкалы намеренно
              padding: '20px 22px',
              gap: 'var(--space-4)',
            }}
          >
            {/* Top crit bar */}
            {g.severity === 'crit' && (
              <div
                className="absolute top-0 left-0 right-0 animate-kiosk-bar"
                style={{ height: 2, background: 'var(--crit)' }}
              />
            )}

            {/* Title row */}
            <div className="flex items-center justify-between pb-3 border-b border-[var(--line)]">
              <div>
                <div className="flex items-center gap-2">
                  <span style={{ fontFamily: 'var(--font-display)', fontSize: 'var(--fs-lg)', fontWeight: 700, letterSpacing: '-0.02em' }}>
                    ГПА-{g.gpa.replace('GPA', '')}
                  </span>
                  {g.running && (
                    <span
                      className="font-mono font-bold"
                      style={{
                        fontSize: 'var(--fs-xs)',
                        letterSpacing: '0.08em',
                        textTransform: 'uppercase',
                        padding: '2px var(--space-2)',
                        borderRadius: 2,
                        color: SEV_TEXT_ON_SOLID.ok,
                        background: 'var(--ok)',
                        border: '1px solid var(--ok)',
                      }}
                    >
                      ● В РАБОТЕ
                    </span>
                  )}
                </div>
                <div className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', marginTop: 2, letterSpacing: '0.06em', textTransform: 'uppercase' }}>
                  Газоперекачивающий агрегат
                </div>
              </div>
              <span
                className={`font-mono font-bold rounded-full${g.severity === 'crit' ? ' animate-crit-pulse' : ''}`}
                style={{
                  fontSize: 'var(--fs-xs)',
                  letterSpacing: '0.10em',
                  textTransform: 'uppercase',
                  padding: 'var(--space-1) 10px',
                  color: SEV_TEXT_ON_SOLID[g.severity !== 'ok' ? g.severity : g.running ? 'ok' : 'crit'],
                  background: SEV_COLOR[g.severity !== 'ok' ? g.severity : g.running ? 'ok' : 'crit'],
                }}
              >
                {g.severity === 'crit' ? 'КРИТИЧНО' : g.severity === 'warn' ? 'ВНИМАНИЕ' : g.severity === 'info' ? 'ИНФО' : g.running ? 'В РАБОТЕ' : 'ОСТАНОВЛЕН'}
              </span>
            </div>

            {/* RPM + stats */}
            <div className="grid items-center" style={{ gridTemplateColumns: 'auto 1fr', gap: 'var(--space-5)' }}>
              <div>
                {g.running ? (
                  <>
                    <div
                      className="font-mono font-bold"
                      style={{ fontSize: 72, lineHeight: 0.9, letterSpacing: '-0.04em', color: 'var(--ok)' }}
                    >
                      {g.rpmValue !== null ? Math.round(g.rpmValue).toLocaleString('ru-RU') : '—'}
                    </div>
                    <div className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', marginTop: 6, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                      об/мин
                    </div>
                  </>
                ) : (
                  <>
                    <div
                      className="font-mono font-bold"
                      style={{ fontSize: 72, lineHeight: 0.9, letterSpacing: '-0.04em', color: 'var(--text-3)' }}
                    >
                      —
                    </div>
                    <div className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', marginTop: 6, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                      об/мин
                    </div>
                  </>
                )}
              </div>
              <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: '10px 18px' }}>
                {[
                  { label: 'Критич.', val: g.crit,  color: 'var(--crit)' },
                  { label: 'Предупр.', val: g.warn, color: 'var(--warn)' },
                  { label: 'Инфо',    val: g.info,  color: 'var(--info)' },
                  { label: 'Всего',   val: g.total, color: 'var(--text-2)' },
                ].map(item => (
                  <div key={item.label} className="flex flex-col gap-[2px]">
                    <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 600 }}>
                      {item.label}
                    </div>
                    <div className="font-mono font-bold" style={{ fontSize: 'var(--fs-lg)', color: item.color, lineHeight: 1 }}>
                      {item.val}
                    </div>
                  </div>
                ))}
              </div>
            </div>


            {/* Recent events */}
            <div className="border-t border-[var(--line)] pt-3 flex-1 min-h-0 flex flex-col">
              <div className="flex justify-between mb-2" style={{ fontSize: 'var(--fs-xs)', letterSpacing: '0.10em', textTransform: 'uppercase', color: 'var(--text-3)', fontWeight: 600 }}>
                <span>Последние события</span>
                <span>{g.recentEvents.length}</span>
              </div>
              <div className="overflow-hidden flex-1">
                {g.recentEvents.length === 0 ? (
                  <div className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)' }}>Нет событий</div>
                ) : g.recentEvents.map(ev => (
                  <div
                    key={ev.id}
                    className="flex gap-2 items-flex-start border-t border-dotted border-[var(--line)]"
                    style={{ padding: 'var(--space-1) 0', fontSize: 'var(--fs-xs)', cursor: 'pointer' }}
                    // a11y: кастомная кликабельная строка → роль кнопки + клавиатура (Enter/Space)
                    role="button"
                    tabIndex={0}
                    aria-label={`Открыть событие: ${ruSensor(ev.sensor_name)}`}
                    onClick={() => { onSelect(ev); onExit() }}
                    onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(ev); onExit() } }}
                  >
                    {ev.severity !== 'ok' && (
                      <>
                        <div style={{ width: 3, background: SEV_COLOR[ev.severity], flexShrink: 0, alignSelf: 'stretch' }} aria-hidden="true" />
                        <span
                          className="font-mono font-bold"
                          style={{
                            fontSize: 'var(--fs-xs)',
                            letterSpacing: '0.06em',
                            flexShrink: 0,
                            color: SEV_COLOR[ev.severity],
                          }}
                          aria-label={ev.severity === 'crit' ? 'Критично' : ev.severity === 'warn' ? 'Предупреждение' : 'Инфо'}
                        >
                          {ev.severity === 'crit' ? 'КРИТ' : ev.severity === 'warn' ? 'ПРЕД' : 'ИНФО'}
                        </span>
                      </>
                    )}
                    <span className="font-mono" style={{ color: 'var(--text-3)', width: 44, flexShrink: 0 }}>
                      {fmtStation(ev.timestamp, { hour: '2-digit', minute: '2-digit' })}
                    </span>
                    <span className="font-mono overflow-hidden text-ellipsis whitespace-nowrap flex-1" style={{ color: ev.severity === 'crit' ? 'var(--text)' : 'var(--text-2)' }}>
                      {ruSensor(ev.sensor_name)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Footer */}
      <div
        className="flex justify-between items-center border-t border-[var(--line)] font-mono flex-shrink-0"
        style={{ paddingTop: 'var(--space-3)', fontSize: 'var(--fs-sm)', color: 'var(--text-3)', letterSpacing: '0.05em' }}
      >
        <div className="flex gap-6">
          <span><b>K / Esc</b> — выйти</span>
          <span><b>J</b> — журнал</span>
        </div>
      </div>
    </div>
  )
}
