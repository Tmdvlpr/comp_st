import { useState, useRef, useEffect, useCallback } from 'react'

const RU_MONTHS = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь']
const RU_DAYS   = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']

interface Props {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  highlighted?: boolean
  minDate?: string  // YYYY-MM-DD inclusive lower bound
  maxDate?: string  // YYYY-MM-DD inclusive upper bound
}

function calendarDays(year: number, month: number): (Date | null)[] {
  const first = new Date(year, month, 1)
  const last  = new Date(year, month + 1, 0)
  const startDow = (first.getDay() + 6) % 7
  const days: (Date | null)[] = Array(startDow).fill(null)
  for (let d = 1; d <= last.getDate(); d++) days.push(new Date(year, month, d))
  while (days.length % 7 !== 0) days.push(null)
  return days
}

function toYMD(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`
}

// Фокусный ID счётчик для уникальных id при нескольких DatePicker на странице
let _dpIdCounter = 0

export function DatePicker({ value, onChange, placeholder = 'дд.мм.гггг', highlighted = false, minDate, maxDate }: Props) {
  const [open, setOpen] = useState(false)
  const ref  = useRef<HTMLDivElement>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  // Уникальный id для связи кнопки и диалога (aria-labelledby)
  const idRef = useRef(`dp-${++_dpIdCounter}`)
  const today = new Date()
  const parsed = value ? new Date(value + 'T12:00:00') : null

  const [viewYear,  setViewYear]  = useState(parsed?.getFullYear()  ?? today.getFullYear())
  const [viewMonth, setViewMonth] = useState(parsed?.getMonth()      ?? today.getMonth())
  // Фокусированная дата в сетке календаря (для стрелочной навигации)
  const [focusedDate, setFocusedDate] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); btnRef.current?.focus() }
    }
    document.addEventListener('mousedown', h)
    document.addEventListener('keydown', k, true)
    // фокус на выбранный/сегодняшний/первый день при открытии (клавиатурная доступность)
    const raf = requestAnimationFrame(() => {
      const sel = popRef.current?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')
        ?? popRef.current?.querySelector<HTMLButtonElement>('button[aria-current="date"]')
        ?? popRef.current?.querySelector<HTMLButtonElement>('button:not([disabled])')
      sel?.focus()
    })
    return () => {
      document.removeEventListener('mousedown', h)
      document.removeEventListener('keydown', k, true)
      cancelAnimationFrame(raf)
    }
  }, [open])

  const prevMonth = () => viewMonth === 0 ? (setViewMonth(11), setViewYear(y => y - 1)) : setViewMonth(m => m - 1)
  const nextMonth = () => viewMonth === 11 ? (setViewMonth(0),  setViewYear(y => y + 1)) : setViewMonth(m => m + 1)

  const select = (d: Date) => { onChange(toYMD(d)); setOpen(false) }
  const clear  = () => { onChange(''); setOpen(false) }
  const todayStr = toYMD(today)
  const isFuture = (d: Date) => toYMD(d) > todayStr
  const isOutOfRange = (d: Date) => {
    const s = toYMD(d)
    if (minDate && s < minDate) return true
    if (maxDate && s > maxDate) return true
    return false
  }
  const isDisabled = (d: Date) => isFuture(d) || isOutOfRange(d)

  // Стрелочная навигация по сетке календаря (WCAG 2.1 SC 2.1.1)
  const handleDayKeyDown = useCallback((e: React.KeyboardEvent<HTMLButtonElement>, d: Date) => {
    const deltas: Record<string, number> = {
      ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7,
    }
    const delta = deltas[e.key]
    if (delta !== undefined) {
      e.preventDefault()
      const next = new Date(d)
      next.setDate(next.getDate() + delta)
      // Переключить месяц при выходе за границу
      if (next.getFullYear() !== viewYear || next.getMonth() !== viewMonth) {
        setViewYear(next.getFullYear())
        setViewMonth(next.getMonth())
      }
      setFocusedDate(toYMD(next))
      return
    }
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      if (!isDisabled(d)) select(d)
    }
  }, [viewYear, viewMonth, isDisabled, select]) // eslint-disable-line react-hooks/exhaustive-deps

  // Фокус на нужную кнопку после смены viewMonth при стрелочной навигации
  useEffect(() => {
    if (!focusedDate || !open) return
    const btn = popRef.current?.querySelector<HTMLButtonElement>(`button[data-date="${focusedDate}"]`)
    if (btn && !btn.disabled) {
      btn.focus(); setFocusedDate(null)
    } else if (btn) {
      // целевой день недоступен (будущее/вне диапазона) — не теряем фокус:
      // оставляем фокус на ближайшей доступной кнопке грида
      const fallback = popRef.current?.querySelector<HTMLButtonElement>('button[data-date]:not([disabled])')
      fallback?.focus(); setFocusedDate(null)
    }
  }, [focusedDate, viewMonth, open])

  const display = parsed
    ? `${String(parsed.getDate()).padStart(2,'0')}.${String(parsed.getMonth()+1).padStart(2,'0')}.${parsed.getFullYear()}`
    : ''

  const mono: React.CSSProperties = { fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)' }

  return (
    <div ref={ref} style={{ position: 'relative', display: 'inline-block' }}>
      <button
        ref={btnRef}
        id={idRef.current}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={display ? `Выбор даты, выбрано ${display}` : 'Выбор даты'}
        onClick={() => {
          if (!open && parsed) { setViewYear(parsed.getFullYear()); setViewMonth(parsed.getMonth()) }
          setOpen(v => !v)
        }}
        style={{
          ...mono, display: 'flex', alignItems: 'center', gap: 4,
          background: 'var(--surface-2)', border: '1px solid',
          borderColor: highlighted ? 'var(--accent)' : 'var(--line)',
          color: display ? 'var(--text-2)' : 'var(--text-3)',
          padding: '2px 6px', cursor: 'pointer', minWidth: 88,
        }}
      >
        <CalIcon />
        {display || placeholder}
      </button>

      {open && (
        <div ref={popRef} role="dialog" aria-label="Выбор даты" className="anim-slide-down" style={{
          position: 'absolute', top: 'calc(100% + 2px)', right: 0, zIndex: 999,
          background: 'var(--surface-2)', border: '1px solid var(--line)',
          boxShadow: 'var(--shadow-md)', padding: 10, width: 200,
        }}>
          {/* Month nav */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <NavBtn onClick={prevMonth} aria-label="Предыдущий месяц">‹</NavBtn>
            <span style={{ ...mono, color: 'var(--text)', fontWeight: 600, letterSpacing: '0.04em' }} aria-live="polite" aria-atomic="true">
              {RU_MONTHS[viewMonth]} {viewYear}
            </span>
            <NavBtn onClick={nextMonth} aria-label="Следующий месяц">›</NavBtn>
          </div>

          {/* Day names */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', marginBottom: 2 }}>
            {RU_DAYS.map(d => (
              <div key={d} style={{ textAlign: 'center', color: 'var(--text-3)', fontSize: 10, padding: '1px 0' }}>{d}</div>
            ))}
          </div>

          {/* Days */}
          <div
            role="grid"
            aria-label={`${RU_MONTHS[viewMonth]} ${viewYear}`}
            style={{ display: 'grid', gridTemplateColumns: 'repeat(7,1fr)', gap: 1 }}
          >
            {calendarDays(viewYear, viewMonth).map((d, i) => {
              if (!d) return <div key={i} role="gridcell" aria-hidden="true" />
              const s = toYMD(d)
              const isSel   = s === value
              const isToday = s === todayStr
              const disabled = isDisabled(d)
              return (
                <div key={i} role="gridcell">
                  <button
                    data-date={s}
                    onClick={() => { if (!disabled) select(d) }}
                    onKeyDown={e => handleDayKeyDown(e, d)}
                    disabled={disabled}
                    aria-pressed={isSel}
                    aria-current={isToday ? 'date' : undefined}
                    aria-label={`${d.getDate()} ${RU_MONTHS[d.getMonth()]} ${d.getFullYear()}${isSel ? ', выбрано' : ''}${isToday ? ', сегодня' : ''}`}
                    style={{
                      ...mono, textAlign: 'center', padding: '3px 0', width: '100%',
                      cursor: disabled ? 'not-allowed' : 'pointer', border: '1px solid',
                      borderColor: isToday && !isSel ? 'var(--accent)' : 'transparent',
                      background: isSel ? 'var(--accent-strong)' : 'transparent',
                      color: isSel ? 'var(--on-accent)' : disabled ? 'var(--line-2)' : d.getMonth() === viewMonth ? 'var(--text-2)' : 'var(--text-3)',
                      fontWeight: isToday ? 600 : 400,
                      opacity: disabled ? 0.4 : 1,
                    }}
                    onMouseEnter={e => { if (!isSel && !disabled) (e.currentTarget as HTMLElement).style.background = 'var(--surface-3)' }}
                    onMouseLeave={e => { if (!isSel) (e.currentTarget as HTMLElement).style.background = 'transparent' }}
                  >
                    {d.getDate()}
                  </button>
                </div>
              )
            })}
          </div>

          {/* Footer */}
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8, paddingTop: 6, borderTop: '1px solid var(--line)' }}>
            <button
              onClick={clear}
              aria-label="Удалить выбранную дату"
              style={{ ...mono, background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', padding: '2px 4px', borderRadius: 'var(--r-sm)' }}
            >
              Удалить
            </button>
            <button
              onClick={() => select(today)}
              aria-label="Выбрать сегодняшнюю дату"
              style={{ ...mono, background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: '2px 4px', borderRadius: 'var(--r-sm)' }}
            >
              Сегодня
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function CalIcon() {
  return (
    <svg width="9" height="9" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ flexShrink: 0 }}>
      <rect x="1" y="2" width="10" height="9" />
      <path d="M1 5h10M4 1v2M8 1v2" />
    </svg>
  )
}

function NavBtn({ children, onClick, 'aria-label': ariaLabel }: { children: React.ReactNode; onClick: () => void; 'aria-label'?: string }) {
  return (
    <button
      onClick={onClick}
      aria-label={ariaLabel}
      style={{
        background: 'none', border: 'none', color: 'var(--text-2)', cursor: 'pointer',
        fontFamily: 'Inter, monospace', fontSize: 14, padding: '0 4px', lineHeight: 1,
        borderRadius: 'var(--r-sm)',
        transition: 'color var(--dur-fast) var(--ease-standard)',
      }}
      onMouseEnter={e => { (e.currentTarget as HTMLElement).style.color = 'var(--text)' }}
      onMouseLeave={e => { (e.currentTarget as HTMLElement).style.color = 'var(--text-2)' }}
    >
      {children}
    </button>
  )
}
