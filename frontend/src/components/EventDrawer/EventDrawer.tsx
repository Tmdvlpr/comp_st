import { useState, useEffect, useRef, useMemo, useCallback, memo, type MouseEvent } from 'react'
import { animate, stagger } from 'animejs'
import { prefersReducedMotion } from '../../lib/motion'
import type { EventItem, AnomalyKind, Severity } from '../../types'
import { ruSensor } from '../../lib/sensorLabels'
import { KIND_LABEL, SEV_COLOR, SEV_LABEL } from '../../types'
import { useModal } from '../../lib/useModal'
import { fmtStation, stationYMD } from '../../lib/time'

interface EventDrawerProps {
  open: boolean
  events: EventItem[]
  onClose: () => void
  onAck: (id: string) => void
  onAckAll: () => void
  onSelect: (ev: EventItem) => void
}

const ALL_KINDS: AnomalyKind[] = ['ml', 'frozen', 'neg', 'roc', 'seasonal', 'regime', 'cross']

const KIND_DOT: Record<string, string> = {
  ml:       '#CC3333',  //   0° красный        — стат. выброс
  roc:      '#CCA820',  //  45° золото         — скачок ΔV
  seasonal: '#88B020',  //  85° оливковый      — сезонная
  regime:   '#20A040',  // 135° зелёный        — смена режима
  cross:    '#18A8A0',  // 178° бирюзовый      — кросс-ГПА
  frozen:   '#2878CC',  // 213° синий          — датчик завис
  neg:      '#8830C0',  // 278° фиолетовый     — сбой физичности
}
const SEV_ORDER: Severity[] = ['crit', 'warn', 'info', 'ok']

const GROUP_GAP_MS = 2 * 60 * 60 * 1000 // 2 hours

interface EventGroup {
  key: string
  events: EventItem[]
}

function buildGroups(sorted: EventItem[]): EventGroup[] {
  const groups: EventGroup[] = []
  for (const ev of sorted) {
    const last = groups[groups.length - 1]
    const sameKey = last && last.events[0].sensor_id === ev.sensor_id && last.events[0].kind === ev.kind
    const within = last && Math.abs(
      new Date(last.events[last.events.length - 1].timestamp).getTime() - new Date(ev.timestamp).getTime()
    ) <= GROUP_GAP_MS
    if (sameKey && within) {
      last.events.push(ev)
    } else {
      groups.push({ key: `${ev.sensor_id}|${ev.kind}|${ev.id}`, events: [ev] })
    }
  }
  return groups
}

/**
 * Подпись дневного разделителя (визуальная надстройка над списком групп).
 * Сравнивает дату события (в зоне станции) с текущей: «Сегодня» / «Вчера» / «День · dd.mm».
 * ymd — 'YYYY-MM-DD' в зоне станции (stationYMD), ts — исходная метка для форматирования дня/месяца.
 */
function dayLabel(ymd: string, ts: string): string {
  const todayYMD = stationYMD(Date.now())
  const yestYMD = stationYMD(Date.now() - 24 * 60 * 60 * 1000)
  const dm = fmtStation(ts, { day: '2-digit', month: '2-digit' })
  if (ymd === todayYMD) return `Сегодня · ${dm}`
  if (ymd === yestYMD) return `Вчера · ${dm}`
  return `День · ${dm}`
}

const EventRow = memo(function EventRow({ ev, onSelect, onAck, nested }: { ev: EventItem; onSelect: (ev: EventItem) => void; onAck: (id: string) => void; nested?: boolean }) {
  const [acking, setAcking] = useState(false)
  const handleAck = (e: MouseEvent) => {
    e.stopPropagation()
    if (acking) return
    setAcking(true)
    onAck(ev.id)
    // Сбрасываем флаг через 3с (страховка если родитель не обновит ev.acked вовремя)
    setTimeout(() => setAcking(false), 3000)
  }
  return (
    <div
      // a11y: кликабельная строка ведёт себя как кнопка (Enter/Space, фокус, метка)
      role="button"
      tabIndex={0}
      aria-label={`Событие: ${ruSensor(ev.sensor_name)}, ${SEV_LABEL[ev.severity]}`}
      className="js-evrow relative cursor-pointer transition-colors"
      style={{
        // дизайн-референс .row: цветная полоса слева, колонка времени с border-right,
        // тело, колонка действий с border-left. Внутренние отступы задаём в ячейках,
        // чтобы границы-разделители шли на всю высоту строки.
        borderBottom: nested ? '1px solid var(--line)' : '1px solid var(--line)',
        display: 'grid',
        gridTemplateColumns: 'auto 1fr auto',
        alignItems: 'stretch',
        opacity: ev.acked ? 0.45 : 1,
      }}
      onClick={() => onSelect(ev)}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(ev) }
      }}
      onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = 'var(--surface-2)' }}
      onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = 'transparent' }}
    >
      {/* Left severity bar */}
      <div
        className="absolute left-0 top-0 bottom-0"
        style={{
          width: 3,
          background: ev.acked ? 'var(--ok)' : SEV_COLOR[ev.severity],
          animation: !ev.acked && ev.severity === 'crit' ? 'crit-bar-pulse 1.4s ease-in-out infinite' : 'none',
        }}
      />

      {/* Time (.row-time: моно, по центру, правая граница-разделитель) */}
      <div
        className="font-mono flex items-center justify-center"
        style={{
          fontSize: 'var(--fs-sm)',
          color: 'var(--text-2)',
          fontWeight: 500,
          width: 68,
          flexShrink: 0,
          borderRight: '1px solid var(--line)',
          padding: nested ? 'var(--space-3) var(--space-2) var(--space-3) var(--space-4)' : 'var(--space-4) var(--space-3) var(--space-4) var(--space-5)',
        }}
      >
        {fmtStation(ev.timestamp, { hour: '2-digit', minute: '2-digit' })}
      </div>

      {/* Body (.row-main) */}
      <div className="min-w-0" style={{ padding: nested ? 'var(--space-3) var(--space-4)' : 'var(--space-4) var(--space-4)' }}>
        <div className="flex items-center gap-3 flex-wrap mb-1">
          <span
            className={`badge-sev ${ev.severity}`}
            style={{ animation: ev.severity === 'crit' && !ev.acked ? 'crit-pulse 1.8s ease-in-out infinite' : 'none' }}
          >
            {SEV_LABEL[ev.severity]}
          </span>
          <span className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-2)', letterSpacing: '0.04em' }}>
            {KIND_LABEL[ev.kind]}
          </span>
          <span className="font-mono font-semibold" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-1)' }}>
            {ev.gpa}
          </span>
        </div>
        <div className="font-mono font-semibold mb-[5px]" style={{ fontSize: 'var(--fs-md)', color: 'var(--text-1)' }}>
          {ruSensor(ev.sensor_name)}
        </div>
        <div style={{ fontSize: 'var(--fs-md)', color: 'var(--text-2)', lineHeight: 1.5 }}>
          {ev.description}
        </div>
        {ev.value != null && (
          <div className="font-mono mt-1" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)' }}>
            Значение: <span style={{ color: 'var(--text-1)', fontWeight: 600 }}>{ev.value.toFixed(4)}</span>
            {ev.deviation != null && (
              <> · Отклонение: <span style={{ color: 'var(--text-1)', fontWeight: 600 }}>{ev.deviation.toFixed(4)}</span></>
            )}
          </div>
        )}
      </div>

      {/* Actions (.row-actions: левая граница-разделитель, min-width) */}
      <div
        className="flex flex-col gap-1 items-end justify-center"
        style={{
          minWidth: 132,
          flexShrink: 0,
          borderLeft: '1px solid var(--line)',
          padding: nested ? 'var(--space-3) var(--space-4)' : 'var(--space-4) var(--space-5) var(--space-4) var(--space-4)',
        }}
      >
        {ev.acked ? (
          <span
            className="font-mono font-semibold"
            style={{
              fontSize: 'var(--fs-xs)',
              color: 'var(--ok)',
              padding: 'var(--space-2) var(--space-3)',
              background: 'color-mix(in srgb, var(--ok) 12%, transparent)',
              border: '1px solid color-mix(in srgb, var(--ok) 35%, transparent)',
              borderRadius: 'var(--r-sm)',
              letterSpacing: '0.04em',
            }}
          >
            ✓ Принято
          </span>
        ) : (
          <button
            onClick={handleAck}
            disabled={acking}
            aria-label={`Принять событие: ${ruSensor(ev.sensor_name)}`}
            aria-busy={acking}
            className="font-bold transition-all"
            style={{
              background: acking ? 'var(--surface-2)' : 'var(--accent)',
              color: acking ? 'var(--text-3)' : '#fff',
              border: acking ? '1px solid var(--line)' : 'none',
              padding: 'var(--space-2) var(--space-3)',
              borderRadius: 'var(--r-sm)',
              fontSize: 'var(--fs-xs)',
              cursor: acking ? 'wait' : 'pointer',
              letterSpacing: '0.04em',
              display: 'flex',
              alignItems: 'center',
              gap: 'var(--space-1)',
              opacity: acking ? 0.7 : 1,
            }}
          >
            {acking ? '…' : '✓ Принять'}
          </button>
        )}
        <span className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', letterSpacing: '0.04em' }}>
          {fmtStation(ev.timestamp, { day: '2-digit', month: '2-digit', year: 'numeric' })}
        </span>
      </div>
    </div>
  )
})

export function EventDrawer({ open, events, onClose, onAck, onAckAll, onSelect }: EventDrawerProps) {
  const [filterSev, setFilterSev] = useState<Severity | 'all'>('all')
  const [filterKind, setFilterKind] = useState<AnomalyKind | 'all'>('all')
  const [filterDays, setFilterDays] = useState<number | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  // Двухшаговое подтверждение «Принять все»: первый клик показывает Подтвердить/Отмена,
  // второй — выполняет действие. Нет window.confirm, работает со скринридерами.
  const [confirmAckAll, setConfirmAckAll] = useState(false)
  // Оптимистичное состояние: набор ID событий, которые локально считаются принятыми
  // до ответа сервера. Сбрасываются при изменении prop events (сервер ответил).
  const [optimisticAcked, setOptimisticAcked] = useState<Set<string>>(new Set())
  // Состояние загрузки для кнопок «Принять все» групп: ключ → true пока идёт запрос
  const [groupAcking, setGroupAcking] = useState<Record<string, boolean>>({})
  // Выпадающий список тип-фильтра (по паттерну DatePicker: click-outside + Esc-закрытие).
  // Меняет ТОЛЬКО отображение фильтра — сама логика filterKind не тронута.
  const [kindOpen, setKindOpen] = useState(false)
  const kindWrapRef = useRef<HTMLDivElement>(null)
  const kindBtnRef = useRef<HTMLButtonElement>(null)

  // Обёртки с оптимистичным обновлением
  const handleAck = useCallback((id: string) => {
    setOptimisticAcked(prev => { const n = new Set(prev); n.add(id); return n })
    onAck(id)
  }, [onAck])

  const handleAckAll = useCallback(() => {
    setOptimisticAcked(new Set(events.map(e => e.id)))
    setConfirmAckAll(false)
    onAckAll()
  }, [onAckAll, events])

  // При обновлении props events (сервер подтвердил) сбрасываем оптимистичный оверлей
  useEffect(() => {
    setOptimisticAcked(new Set())
  }, [events])

  const closeRef = useRef<HTMLButtonElement>(null)
  // фокус-трап + закрытие по Esc + возврат фокуса на триггер (WCAG 2.1.2 / 2.4.3)
  const dialogRef = useModal<HTMLDivElement>(open, onClose)

  const toggleExpand = (key: string) =>
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key); else next.add(key)
      return next
    })

  // Тип-дропдаун: закрытие по клику вне панели и по Escape (паттерн DatePicker).
  // Esc внутри дропдауна закрывает только его (stopPropagation) и возвращает фокус на кнопку.
  useEffect(() => {
    if (!kindOpen) return
    const onDown = (e: globalThis.MouseEvent) => {
      if (kindWrapRef.current && !kindWrapRef.current.contains(e.target as Node)) setKindOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); setKindOpen(false); kindBtnRef.current?.focus() }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey, true)
    }
  }, [kindOpen])

  // animejs: каскад первых строк журнала при открытии
  const bodyRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const body = bodyRef.current
    if (!open || !body || prefersReducedMotion()) return
    const rows = [...body.querySelectorAll('.js-evrow')].slice(0, 18)
    if (!rows.length) return
    // только transform: у строк есть собственная inline-opacity (acked 0.45)
    const a = animate(rows, {
      translateY: [14, 0], duration: 300, ease: 'outCubic', delay: stagger(20),
    })
    return () => { a.pause() }
  }, [open])

  // Оптимистичный оверлей: синхронно помечаем принятые события ещё до ответа сервера
  const eventsView = useMemo(
    () => optimisticAcked.size === 0
      ? events
      : events.map(e => optimisticAcked.has(e.id) ? { ...e, acked: true } : e),
    [events, optimisticAcked],
  )

  // Мемоизированная фильтрация и группировка — пересчитываются только при изменении
  // массива событий (с оптимистичным оверлеем) или активных фильтров.
  const { filtered, groups, unacked } = useMemo(() => {
    const dayCutoff = filterDays !== null
      ? new Date(Date.now() - filterDays * 24 * 60 * 60 * 1000).toISOString()
      : null
    const filtered = eventsView.filter(ev => {
      if (filterSev !== 'all' && ev.severity !== filterSev) return false
      if (filterKind !== 'all' && ev.kind !== filterKind) return false
      if (dayCutoff && ev.timestamp < dayCutoff) return false
      return true
    })
    const groups = buildGroups(filtered)
    const unacked = eventsView.filter(e => !e.acked).length
    return { filtered, groups, unacked }
  }, [eventsView, filterSev, filterKind, filterDays])

  const countBySev = useCallback((sev: Severity) => eventsView.filter(e => e.severity === sev).length, [eventsView])

  // Стабильный обработчик групповой квитации: использует оптимистичный handleAck.
  // groupKey используется для отслеживания состояния загрузки кнопки группы.
  const handleGroupAck = useCallback((groupKey: string, groupEvents: EventItem[]) => {
    setGroupAcking(prev => ({ ...prev, [groupKey]: true }))
    groupEvents.forEach(g => { if (!g.acked) handleAck(g.id) })
    // Сбрасываем флаг через 3с — страховка если props не обновятся вовремя
    setTimeout(() => setGroupAcking(prev => { const n = { ...prev }; delete n[groupKey]; return n }), 3000)
  }, [handleAck])

  return (
    <div
      // .scrim: полупрозрачный фон, диалог выезжает снизу и прижат к нижнему краю
      className="fixed inset-0 flex flex-col justify-end items-center z-[91]"
      style={{
        background: 'rgba(6,9,20,.55)',
        opacity: open ? 1 : 0,
        /* visibility:hidden убирает закрытый диалог из tab-order и дерева доступности
           (раньше при !open фокусируемые дети оставались достижимы по Tab) */
        visibility: open ? 'visible' : 'hidden',
        pointerEvents: open ? 'all' : 'none',
        transition: open
          ? 'opacity var(--dur-moderate) var(--ease-decelerate)'
          : 'opacity var(--dur-normal) var(--ease-accelerate)',
      }}
      // клик по подложке (вне листа) закрывает журнал
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label="Журнал событий"
      // .drawer: bottom-sheet — выезд снизу, скруглённый верх, тень над листом
      className="flex flex-col"
      style={{
        width: '100%',
        height: '92vh',
        maxHeight: 920,
        minHeight: 0,
        background: 'var(--bg)',
        borderTop: '1px solid var(--line-2)',
        borderRadius: '18px 18px 0 0',
        boxShadow: '0 -20px 60px rgba(0,0,0,.5)',
        overflow: 'hidden',
        /* Вход: выезд снизу с пружиной (spring). Выход: быстрый accelerate вниз. */
        transform: open ? 'translateY(0)' : 'translateY(100%)',
        transition: open
          ? 'transform var(--dur-moderate) var(--ease-spring)'
          : 'transform var(--dur-normal) var(--ease-accelerate)',
      }}
    >
      {/* Header */}
      <div
        className="flex items-center justify-between gap-4 flex-shrink-0 border-b border-[var(--line)]"
        style={{ padding: 'var(--space-5) var(--space-6) var(--space-3)', background: 'var(--surface)' }}
      >
        <div className="flex flex-col gap-[2px]">
          <h2 style={{ fontFamily: 'var(--font-display)', fontSize: 'var(--fs-lg)', fontWeight: 700, letterSpacing: '0.04em' }}>
            Журнал событий
          </h2>
          <p className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)' }}>
            {unacked} непринятых · {events.length} всего
          </p>
        </div>
        <button
          ref={closeRef}
          onClick={onClose}
          aria-label="Закрыть журнал (Esc)"
          className="flex items-center justify-center transition-all"
          style={{
            width: 'var(--space-6)', height: 'var(--space-6)',
            background: 'var(--surface-2)',
            border: '1px solid var(--line)',
            borderRadius: 'var(--r-sm)',
            color: 'var(--text-2)',
            fontSize: 'var(--fs-md)',
            cursor: 'pointer',
          }}
          onMouseEnter={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--crit)'; (e.currentTarget as HTMLElement).style.color = 'var(--crit)' }}
          onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--line)'; (e.currentTarget as HTMLElement).style.color = 'var(--text-2)' }}
          title="Закрыть (Esc)"
        >
          ✕
        </button>
      </div>

      {/* Filters */}
      <div
        className="flex items-center gap-2 flex-shrink-0 border-b border-[var(--line)] flex-wrap"
        style={{ padding: 'var(--space-2) var(--space-6)', background: 'var(--surface)' }}
      >
        {/* Period filter */}
        <div className="flex items-center gap-1 mr-2">
          {([null, 1, 3, 7, 30] as const).map(d => {
            const active = filterDays === d
            return (
              <button
                key={d ?? 'all'}
                onClick={() => setFilterDays(d)}
                aria-pressed={active}
                aria-label={d === null ? 'Период: все' : `Период: ${d} дней`}
                className="font-mono transition-all"
                style={{
                  padding: 'var(--space-1) var(--space-3)',
                  borderRadius: 'var(--r-sm)',
                  fontSize: 'var(--fs-xs)',
                  cursor: 'pointer',
                  border: `1px solid ${active ? 'var(--accent)' : 'var(--line)'}`,
                  background: active ? 'var(--accent)' : 'var(--surface-2)',
                  color: active ? '#fff' : 'var(--text-2)',
                  fontWeight: active ? 700 : 400,
                }}
              >
                {d === null ? 'Все' : `${d}д`}
              </button>
            )
          })}
        </div>
        <div style={{ width: 1, height: 20, background: 'var(--line)', flexShrink: 0 }} />

        {/* Severity tabs */}
        {(['all', ...SEV_ORDER] as const).map(sev => {
          const count = sev === 'all' ? events.length : countBySev(sev)
          const active = filterSev === sev
          return (
            <button
              key={sev}
              onClick={() => setFilterSev(sev)}
              aria-pressed={active}
              aria-label={`Фильтр важности: ${sev === 'all' ? 'все' : SEV_LABEL[sev]}`}
              className="inline-flex items-center gap-[5px] font-mono transition-all"
              style={{
                padding: 'var(--space-1) var(--space-3)',
                borderRadius: 'var(--r-sm)',
                fontSize: 'var(--fs-xs)',
                letterSpacing: '0.04em',
                cursor: 'pointer',
                border: `1px solid ${active ? 'var(--accent)' : 'var(--line)'}`,
                background: active ? 'var(--accent)' : 'var(--surface-2)',
                color: active ? '#fff' : 'var(--text-2)',
                fontWeight: active ? 700 : 400,
              }}
            >
              {sev === 'all' ? 'Все' : SEV_LABEL[sev]}
              <span style={{ background: 'color-mix(in srgb, var(--text-1) 14%, transparent)', padding: '0 5px', borderRadius: 2, fontSize: 'var(--fs-xs)' }}>
                {count}
              </span>
            </button>
          )
        })}

        {/* Kind filter — свёрнут в выпадающий список (кнопка + поповер) */}
        <div className="ml-4" style={{ position: 'relative' }} ref={kindWrapRef}>
          {(() => {
            const curDot = filterKind !== 'all' ? KIND_DOT[filterKind] : null
            const curLabel = filterKind === 'all' ? 'Все типы' : KIND_LABEL[filterKind]
            return (
              <button
                ref={kindBtnRef}
                onClick={() => setKindOpen(v => !v)}
                aria-haspopup="dialog"
                aria-expanded={kindOpen}
                aria-label={`Фильтр типа: ${filterKind === 'all' ? 'все типы' : KIND_LABEL[filterKind]}. Открыть список`}
                className="font-mono transition-all inline-flex items-center gap-[6px]"
                style={{
                  padding: 'var(--space-1) var(--space-3)',
                  borderRadius: 'var(--r-sm)',
                  fontSize: 'var(--fs-xs)',
                  cursor: 'pointer',
                  border: `1px solid ${filterKind !== 'all' && curDot ? curDot : 'var(--line)'}`,
                  background: 'var(--surface-2)',
                  color: filterKind !== 'all' && curDot ? curDot : 'var(--text-2)',
                }}
              >
                {curDot && (
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: curDot, flexShrink: 0 }} />
                )}
                {curLabel}
                <span style={{ fontSize: 10, opacity: 0.8, transition: 'transform var(--dur-fast) var(--ease-standard)', transform: kindOpen ? 'rotate(180deg)' : 'rotate(0deg)' }}>▾</span>
              </button>
            )
          })()}

          {kindOpen && (
            <div
              role="dialog"
              aria-label="Фильтр по типу события"
              className="anim-slide-down flex flex-col gap-1"
              style={{
                position: 'absolute',
                top: 'calc(100% + 4px)',
                left: 0,
                zIndex: 'var(--z-dropdown)',
                minWidth: 200,
                background: 'var(--surface-2)',
                border: '1px solid var(--line)',
                borderRadius: 'var(--r-sm)',
                boxShadow: 'var(--shadow-md)',
                padding: 'var(--space-2)',
              }}
            >
              {(['all', ...ALL_KINDS] as const).map(kind => {
                const active = filterKind === kind
                const dot = kind !== 'all' ? KIND_DOT[kind] : null
                return (
                  <button
                    key={kind}
                    onClick={() => { setFilterKind(kind); setKindOpen(false); kindBtnRef.current?.focus() }}
                    aria-pressed={active}
                    aria-label={`Фильтр типа: ${kind === 'all' ? 'все типы' : KIND_LABEL[kind]}`}
                    className="font-mono transition-all inline-flex items-center gap-[6px]"
                    style={{
                      padding: 'var(--space-1) var(--space-2)',
                      borderRadius: 'var(--r-sm)',
                      fontSize: 'var(--fs-xs)',
                      cursor: 'pointer',
                      textAlign: 'left',
                      width: '100%',
                      border: `1px solid ${active && dot ? dot : active ? 'var(--accent)' : 'transparent'}`,
                      background: active && dot ? `${dot}18` : active ? 'var(--accent-glow)' : 'transparent',
                      color: active && dot ? dot : active ? 'var(--accent)' : 'var(--text-3)',
                    }}
                    onMouseEnter={e => { if (!active) (e.currentTarget as HTMLElement).style.background = 'var(--surface-3)' }}
                    onMouseLeave={e => { if (!active) (e.currentTarget as HTMLElement).style.background = 'transparent' }}
                  >
                    {dot && (
                      <span style={{ width: 6, height: 6, borderRadius: '50%', background: dot, flexShrink: 0, opacity: active ? 1 : 0.6 }} />
                    )}
                    {kind === 'all' ? 'Все типы' : KIND_LABEL[kind]}
                  </button>
                )
              })}
            </div>
          )}
        </div>

        {/* Actions */}
        <div className="ml-auto flex items-center gap-2">
          {confirmAckAll ? (
            <>
              <span className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-2)' }}>Принять все?</span>
              <button
                onClick={handleAckAll}
                aria-label="Подтвердить принятие всех событий"
                className="font-mono transition-all"
                style={{ padding: 'var(--space-1) var(--space-3)', borderRadius: 'var(--r-sm)', fontSize: 'var(--fs-xs)', cursor: 'pointer', background: 'var(--accent)', border: '1px solid var(--accent)', color: '#fff', fontWeight: 700 }}
              >
                Подтвердить
              </button>
              <button
                onClick={() => setConfirmAckAll(false)}
                aria-label="Отмена принятия всех событий"
                className="font-mono transition-all"
                style={{ padding: 'var(--space-1) var(--space-3)', borderRadius: 'var(--r-sm)', fontSize: 'var(--fs-xs)', cursor: 'pointer', background: 'var(--surface-2)', border: '1px solid var(--line)', color: 'var(--text-2)' }}
              >
                Отмена
              </button>
            </>
          ) : (
            <button
              onClick={() => setConfirmAckAll(true)}
              aria-label="Принять все события"
              className="font-mono transition-all"
              style={{ padding: 'var(--space-1) var(--space-3)', borderRadius: 'var(--r-sm)', fontSize: 'var(--fs-xs)', cursor: 'pointer', background: 'var(--surface-2)', border: '1px solid var(--line)', color: 'var(--text-2)' }}
              onMouseEnter={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--accent)'; (e.currentTarget as HTMLElement).style.color = 'var(--accent)' }}
              onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--line)'; (e.currentTarget as HTMLElement).style.color = 'var(--text-2)' }}
            >
              ✓ Принять все
            </button>
          )}
        </div>
      </div>

      {/* Body */}
      <div
        ref={bodyRef}
        className="flex-1 overflow-auto min-h-0"
        style={{ scrollbarWidth: 'thin' }}
      >
        {eventsView.length === 0 ? (
          <div
            className="font-mono text-center"
            style={{ padding: '60px var(--space-6)', color: 'var(--text-3)', fontSize: 'var(--fs-sm)', gridColumn: '1 / -1' }}
          >
            Событий нет
          </div>
        ) : filtered.length === 0 ? (
          <div
            className="font-mono text-center"
            style={{ padding: '60px var(--space-6)', color: 'var(--text-3)', fontSize: 'var(--fs-sm)', gridColumn: '1 / -1' }}
          >
            Нет событий для выбранных фильтров
          </div>
        ) : null}
        {groups.map((group, i) => {
          // Дневные разделители: чисто визуальная надстройка над уже отфильтрованным
          // и сгруппированным списком groups. Не меняет buildGroups. Ключ дня — по
          // первому событию группы в зоне станции (stationYMD). Разделитель ставим,
          // когда день текущей группы отличается от дня предыдущей группы (по индексу —
          // без мутации внешних переменных внутри рендера).
          const first = group.events[0]
          const ymd = stationYMD(first.timestamp)
          const prevYmd = i > 0 ? stationYMD(groups[i - 1].events[0].timestamp) : null
          const sep = ymd !== prevYmd
            ? (
              <div
                className="font-mono flex items-center"
                style={{
                  gap: 'var(--space-3)',
                  padding: 'var(--space-4) var(--space-5) var(--space-2)',
                  fontSize: 'var(--fs-xs)',
                  letterSpacing: '0.06em',
                  textTransform: 'uppercase',
                  color: 'var(--text-3)',
                }}
              >
                <span style={{ flexShrink: 0 }}>{dayLabel(ymd, first.timestamp)}</span>
                <span style={{ flex: 1, height: 1, background: 'var(--line)' }} />
              </div>
            )
            : null
          return (
            <div key={`sec-${group.key}`}>
              {sep}
              {renderGroup(group)}
            </div>
          )
        })}
      </div>

      {/* Footer */}
      <div
        className="flex justify-between items-center border-t border-[var(--line)] flex-shrink-0 font-mono"
        style={{ padding: 'var(--space-3) var(--space-6)', fontSize: 'var(--fs-xs)', color: 'var(--text-3)', background: 'var(--surface)' }}
      >
        <span>{filtered.length} из {events.length} событий</span>
      </div>
    </div>
    </div>
  )

  // Рендер одной группы (одиночное событие или сворачиваемый аккордеон).
  // Вынесено в замыкание, чтобы обёртка дневных разделителей осталась простой.
  function renderGroup(group: EventGroup) {
          if (group.events.length === 1) {
            return <EventRow key={group.events[0].id} ev={group.events[0]} onSelect={onSelect} onAck={handleAck} />
          }
          // Multi-event group (collapsible)
          const head = group.events[0]
          const allAcked = group.events.every(e => e.acked)
          const times = group.events.map(e => new Date(e.timestamp).getTime())
          const from = new Date(Math.min(...times))
          const to = new Date(Math.max(...times))
          const fmt = (d: Date) => fmtStation(d, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
          const isOpen = expanded.has(group.key)
          return (
            <div key={group.key} style={{ borderBottom: '1px solid var(--line)' }}>
              <div
                // a11y: заголовок группы — кнопка-аккордеон (Enter/Space, фокус, состояние)
                role="button"
                tabIndex={0}
                aria-expanded={isOpen}
                aria-label={`Группа событий: ${ruSensor(head.sensor_name)}, ${group.events.length} шт. ${isOpen ? 'Свернуть' : 'Развернуть'}`}
                className="js-evrow relative cursor-pointer transition-colors"
                style={{
                  padding: 'var(--space-4) var(--space-5) var(--space-4) var(--space-5)',
                  display: 'grid',
                  gridTemplateColumns: 'auto 1fr auto',
                  gap: 'var(--space-4)',
                  alignItems: 'flex-start',
                  opacity: allAcked ? 0.45 : 1,
                }}
                onClick={() => toggleExpand(group.key)}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleExpand(group.key) }
                }}
                onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = 'var(--surface-2)' }}
                onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = 'transparent' }}
              >
                <div
                  className="absolute left-0 top-0 bottom-0"
                  style={{
                    width: 3,
                    background: allAcked ? 'var(--ok)' : SEV_COLOR[head.severity],
                    animation: !allAcked && head.severity === 'crit' ? 'crit-bar-pulse 1.4s ease-in-out infinite' : 'none',
                  }}
                />
                <div className="font-mono pt-[2px] flex items-center gap-1" style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-2)', minWidth: 56, fontWeight: 500 }}>
                  <span style={{ display: 'inline-block', transition: 'transform .15s', transform: isOpen ? 'rotate(0)' : 'rotate(-90deg)' }}>▾</span>
                </div>
                <div className="min-w-0">
                  <div className="flex items-center gap-3 flex-wrap mb-1">
                    <span className={`badge-sev ${head.severity}`} style={{ animation: head.severity === 'crit' && !allAcked ? 'crit-pulse 1.8s ease-in-out infinite' : 'none' }}>
                      {SEV_LABEL[head.severity]}
                    </span>
                    <span className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-2)', letterSpacing: '0.04em' }}>
                      {KIND_LABEL[head.kind]}
                    </span>
                    <span className="font-mono font-semibold" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-1)' }}>
                      {head.gpa}
                    </span>
                    <span className="font-mono font-bold" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-1)', background: 'var(--surface-3)', padding: '1px var(--space-2)', borderRadius: 'var(--r-sm)', letterSpacing: '0.04em' }}>
                      ×{group.events.length}
                    </span>
                  </div>
                  <div className="font-mono font-semibold mb-[5px]" style={{ fontSize: 'var(--fs-md)', color: 'var(--text-1)' }}>
                    {ruSensor(head.sensor_name)}
                  </div>
                  <div className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)' }}>
                    {fmt(from)} — {fmt(to)}
                  </div>
                </div>
                <div className="flex flex-col gap-1 items-end" style={{ minWidth: 132 }}>
                  {allAcked ? (
                    <span className="font-mono font-semibold" style={{ fontSize: 'var(--fs-xs)', color: 'var(--ok)', padding: 'var(--space-2) var(--space-3)', background: 'color-mix(in srgb, var(--ok) 12%, transparent)', border: '1px solid color-mix(in srgb, var(--ok) 35%, transparent)', borderRadius: 'var(--r-sm)', letterSpacing: '0.04em' }}>
                      ✓ Принято
                    </span>
                  ) : (() => {
                    const isGroupAcking = !!groupAcking[group.key]
                    return (
                      <button
                        // клик по кнопке не должен сворачивать/разворачивать группу
                        onClick={e => { e.stopPropagation(); if (isGroupAcking) return; handleGroupAck(group.key, group.events) }}
                        disabled={isGroupAcking}
                        aria-label={`Принять все события группы: ${ruSensor(head.sensor_name)}`}
                        aria-busy={isGroupAcking}
                        className="font-bold transition-all"
                        style={{
                          background: isGroupAcking ? 'var(--surface-2)' : 'var(--accent)',
                          color: isGroupAcking ? 'var(--text-3)' : '#fff',
                          border: isGroupAcking ? '1px solid var(--line)' : 'none',
                          padding: 'var(--space-2) var(--space-3)',
                          borderRadius: 'var(--r-sm)',
                          fontSize: 'var(--fs-xs)',
                          cursor: isGroupAcking ? 'wait' : 'pointer',
                          letterSpacing: '0.04em',
                          display: 'flex',
                          alignItems: 'center',
                          gap: 'var(--space-1)',
                          opacity: isGroupAcking ? 0.7 : 1,
                        }}
                      >
                        {isGroupAcking ? '…' : '✓ Принять все'}
                      </button>
                    )
                  })()}
                </div>
              </div>
              {isOpen && (
                <div style={{ background: 'var(--surface)', paddingLeft: 'var(--space-4)' }}>
                  {group.events.map(ev => (
                    <EventRow key={ev.id} ev={ev} onSelect={onSelect} onAck={handleAck} nested />
                  ))}
                </div>
              )}
            </div>
          )
  }
}
