import { useState, useMemo, useRef, useEffect } from 'react'
import { useQuery, keepPreviousData, useQueryClient } from '@tanstack/react-query'
import type { SensorMeta } from '../../types'
import { api } from '../../api/client'
import { MultiSensorChart } from './MultiSensorChart'
import { ruSensor } from '../../lib/sensorLabels'
import { DatePicker } from '../DatePicker/DatePicker'
import { useModal } from '../../lib/useModal'

// Оператор (владелец наборов). Без полноценного логина — имя из localStorage,
// по умолчанию 'operator'. При появлении авторизации заменить на залогиненного.
const operatorName = () => (typeof localStorage !== 'undefined' && localStorage.getItem('cs_operator')) || 'operator'

interface ComparePanelProps {
  open: boolean
  onClose: () => void
  sensors: SensorMeta[]
  stationId: string
  theme: 'dark' | 'light'
}

export function ComparePanel({ open, onClose, sensors, stationId, theme }: ComparePanelProps) {
  const [selected, setSelected] = useState<string[]>([])
  const [normalized, setNormalized] = useState(true)
  const [showRangeslider, setShowRangeslider] = useState(false)
  const [days, setDays] = useState(7)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [panelW, setPanelW] = useState(360)   // ширина панели названий (раздвигается)
  // Дебаунс поискового запроса: фильтр запускается через 180 мс после остановки ввода.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 180)
    return () => clearTimeout(t)
  }, [search])

  const dialogRef = useModal<HTMLDivElement>(open, onClose)
  // Валидация диапазона: обе даты заданы И «от» не позже «до». Иначе показываем
  // подсказку и не строим инвертированный запрос (откатываемся на пресет дней).
  const rangeInvalid = !!(from && to && from > to)
  const hasRange = !!(from && to) && !rangeInvalid

  // Без лимита на число датчиков — выбираем сколько нужно (нормировка делает их сравнимыми).
  const toggle = (id: string) =>
    setSelected(prev => (prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]))

  // Окно: датапикер (диапазон) приоритетнее пресета дней.
  const chartArg = hasRange ? { t0: `${from}T00:00:00`, t1: `${to}T23:59:59` } : days
  const argKey = hasRange ? `r:${from}:${to}` : `d${days}`

  const { data: multi = [], isFetching } = useQuery({
    queryKey: ['multiChart', stationId, [...selected].sort().join(','), argKey],
    queryFn: () => api.multiChart(selected, chartArg, stationId),
    enabled: open && selected.length > 0,
    staleTime: 20_000,
    gcTime: 30 * 60_000,
    placeholderData: keepPreviousData,
  })

  // ── Сохранённые наборы датчиков (на пользователя, из БД) ──
  const qc = useQueryClient()
  const owner = operatorName()
  const [setName, setSetName] = useState('')
  const [setsBusy, setSetsBusy] = useState(false)
  // Видимый фидбэк по операциям с наборами (вместо молчаливого catch). Автоскрытие ниже.
  const [setsMsg, setSetsMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null)
  useEffect(() => {
    if (!setsMsg) return
    const t = setTimeout(() => setSetsMsg(null), 3000)
    return () => clearTimeout(t)
  }, [setsMsg])
  const { data: savedSets = [] } = useQuery({
    queryKey: ['graphSets', stationId, owner],
    queryFn: () => api.graphSets(owner, stationId),
    enabled: open,
    staleTime: 30_000,
  })
  const refreshSets = () => qc.invalidateQueries({ queryKey: ['graphSets', stationId, owner] })
  const saveSet = async () => {
    const nm = setName.trim()
    if (!nm || selected.length === 0 || setsBusy) return
    setSetsBusy(true)
    try {
      await api.saveGraphSet(owner, nm, selected, stationId); setSetName(''); refreshSets()
      setSetsMsg({ type: 'ok', text: '✓ Набор сохранён' })
    }
    catch { setSetsMsg({ type: 'err', text: 'Не удалось сохранить (БД недоступна)' }) }
    finally { setSetsBusy(false) }
  }
  const loadSet = (ids: string[]) => {
    // оставляем только реально существующие сейчас датчики
    const valid = new Set(sensors.map(s => s.id))
    setSelected(ids.filter(id => valid.has(id)))
  }
  const deleteSet = async (id: number) => {
    if (setsBusy) return
    setSetsBusy(true)
    try {
      await api.deleteGraphSet(owner, id, stationId); refreshSets()
      setSetsMsg({ type: 'ok', text: '✓ Набор удалён' })
    }
    catch { setSetsMsg({ type: 'err', text: 'Не удалось удалить (БД недоступна)' }) }
    finally { setSetsBusy(false) }
  }

  const filtered = useMemo(() => {
    const q = debouncedSearch.trim().toLowerCase()
    const list = q
      ? sensors.filter(s => s.name.toLowerCase().includes(q) || ruSensor(s.name).toLowerCase().includes(q) || s.gpa.toLowerCase().includes(q) || s.tag.toLowerCase().includes(q))
      : sensors
    return list.slice(0, 400)
  }, [sensors, debouncedSearch])

  // Перетаскивание границы панели названий (раздвигание). Панель прижата к левому
  // краю экрана (inset:0), поэтому ширина = clientX курсора.
  const draggingRef = useRef(false)
  useEffect(() => {
    const move = (e: MouseEvent) => {
      if (!draggingRef.current) return
      setPanelW(Math.max(240, Math.min(760, e.clientX)))
    }
    const up = () => { if (draggingRef.current) { draggingRef.current = false; document.body.style.cursor = '' } }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
  }, [])

  if (!open) return null

  const presetBtn = (d: number) => (
    <button key={d} onClick={() => { setDays(d); setFrom(''); setTo('') }}
      aria-pressed={!hasRange && days === d}
      aria-label={`Период ${d} дней`}
      style={{
        padding: '2px 7px', border: '1px solid', cursor: 'pointer',
        borderColor: !hasRange && days === d ? 'var(--accent)' : 'var(--line)',
        background: !hasRange && days === d ? 'var(--accent-glow)' : 'transparent',
        color: !hasRange && days === d ? 'var(--accent)' : 'var(--text-3)',
      }}
      onFocus={e => { e.currentTarget.style.outline = '2px solid var(--accent)'; e.currentTarget.style.outlineOffset = '2px' }}
      onBlur={e => { e.currentTarget.style.outline = '' }}
    >{d}д</button>
  )

  return (
    <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Сравнение датчиков" style={{
      position: 'fixed', inset: 0, zIndex: 60, background: 'var(--bg)',
      display: 'grid', gridTemplateColumns: `${panelW}px 6px 1fr`, gridTemplateRows: '44px 1fr',
      animation: 'fade-up var(--dur-moderate) var(--ease-decelerate) both',
      ['--enter-y' as string]: '8px',
    }}>
      {/* Header */}
      <div style={{
        gridColumn: '1 / 4', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0 16px', borderBottom: '1px solid var(--line)', background: 'var(--surface)', gap: 12,
      }}>
        <span style={{ fontFamily: 'Inter, sans-serif', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--text-1)', flexShrink: 0 }}>
          Сравнение датчиков
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          {/* Датапикер: произвольный период */}
          <span style={{ color: 'var(--text-3)' }}>от</span>
          <DatePicker value={from} onChange={setFrom} highlighted={hasRange} />
          <span style={{ color: 'var(--text-3)' }}>до</span>
          <DatePicker value={to} onChange={setTo} highlighted={hasRange} />
          {(hasRange || rangeInvalid) && (
            <button onClick={() => { setFrom(''); setTo('') }} title="Сбросить период" aria-label="Сбросить период"
              style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontFamily: 'inherit', padding: '0 2px' }}>✕</button>
          )}
          {rangeInvalid && (
            <span role="alert" style={{ color: 'var(--crit)', whiteSpace: 'nowrap' }}>дата «от» позже «до»</span>
          )}
          <div style={{ width: 1, height: 14, background: 'var(--line-2)' }} />
          {[1, 3, 7, 14, 30].map(presetBtn)}
          <div style={{ width: 1, height: 14, background: 'var(--line-2)' }} />
          <button onClick={() => setNormalized(v => !v)} title="Нормировка / реальные единицы" style={{
            padding: '2px 9px', border: '1px solid var(--accent)', cursor: 'pointer',
            background: normalized ? 'var(--accent-glow)' : 'transparent', color: 'var(--accent)',
          }}>
            {normalized ? 'Нормировано' : 'Реальные единицы'}
          </button>
          <button onClick={() => setShowRangeslider(v => !v)} title="Полоса навигации по всему диапазону (range slider)" style={{
            padding: '2px 9px', border: '1px solid', cursor: 'pointer',
            background: showRangeslider ? 'var(--accent-glow)' : 'transparent',
            color: showRangeslider ? 'var(--accent)' : 'var(--text-2)',
            borderColor: showRangeslider ? 'var(--accent)' : 'var(--line)',
          }}>
            Полоса
          </button>
          <button onClick={onClose} style={{
            padding: '2px 10px', border: '1px solid var(--line)', background: 'transparent',
            color: 'var(--text-2)', cursor: 'pointer',
          }}>✕ Закрыть</button>
        </div>
      </div>

      {/* Sensor picker */}
      <div style={{ borderRight: '1px solid var(--line)', display: 'flex', flexDirection: 'column', minHeight: 0, background: 'var(--surface)' }}>
        <div style={{ padding: 8, borderBottom: '1px solid var(--line)' }}>
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Поиск датчика…"
            aria-label="Поиск датчика для сравнения"
            style={{
              width: '100%', padding: '6px 8px', background: 'var(--bg)', border: '1px solid var(--line)',
              color: 'var(--text-1)', fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)',
            }} />
          <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 'var(--fs-xs)', color: 'var(--text-3)', fontFamily: 'Inter, monospace' }}>
            <span>Выбрано {selected.length}</span>
            <button onClick={() => setSelected([])} disabled={selected.length === 0}
              title="Снять выделение со всех датчиков"
              style={{
                padding: '3px 9px', fontFamily: 'inherit', fontSize: 'var(--fs-xs)', borderRadius: 'var(--r-sm)',
                border: `1px solid ${selected.length ? 'var(--line-2)' : 'var(--line)'}`,
                background: 'transparent', color: selected.length ? 'var(--text-2)' : 'var(--text-3)',
                cursor: selected.length ? 'pointer' : 'default', opacity: selected.length ? 1 : 0.5,
              }}
              onMouseEnter={e => { if (selected.length) { (e.currentTarget as HTMLElement).style.borderColor = 'var(--accent)'; (e.currentTarget as HTMLElement).style.color = 'var(--accent)' } }}
              onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = selected.length ? 'var(--line-2)' : 'var(--line)'; (e.currentTarget as HTMLElement).style.color = selected.length ? 'var(--text-2)' : 'var(--text-3)' }}
            >✕ Сбросить выбор</button>
          </div>
        </div>

        {/* ── Мои наборы: готовые подборки датчиков (клик загружает весь список) ── */}
        <div style={{ padding: 'var(--space-2) var(--space-3)', borderBottom: '1px solid var(--line)' }}>
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', fontFamily: 'Inter, monospace', letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: 'var(--space-2)' }}>
            Мои наборы
          </div>
          {savedSets.length === 0 ? (
            <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', fontFamily: 'Inter, monospace', opacity: 0.7, marginBottom: 'var(--space-2)' }}>
              Пока нет. Выберите датчики и сохраните набор ниже.
            </div>
          ) : (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-1)', marginBottom: 'var(--space-2)' }}>
              {savedSets.map(s => (
                <span key={s.id}
                  style={{
                  display: 'inline-flex', alignItems: 'center', gap: 5, padding: '3px 6px 3px 9px',
                  fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)', color: 'var(--text-1)',
                  background: 'var(--surface-2)', border: '1px solid var(--line-2)', borderRadius: 'var(--r-sm)',
                }}
                  onMouseEnter={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--accent)' }}
                  onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--line-2)' }}
                >
                  <button type="button"
                    aria-label={`Загрузить набор ${s.name} (${s.sensor_ids.length} датчиков)`}
                    title={`Загрузить набор (${s.sensor_ids.length} датч.)`}
                    onClick={() => loadSet(s.sensor_ids)}
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 5, background: 'none', border: 'none', color: 'inherit', font: 'inherit', cursor: 'pointer', padding: 0 }}
                  >
                    {s.name} <span style={{ color: 'var(--text-3)' }}>· {s.sensor_ids.length}</span>
                  </button>
                  <button type="button" onClick={() => deleteSet(s.id)} title="Удалить набор" aria-label={`Удалить набор ${s.name}`}
                    style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 13, lineHeight: 1, padding: 0 }}>✕</button>
                </span>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
            <input
              value={setName} onChange={e => setSetName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') saveSet() }}
              aria-label="Название набора"
              placeholder={selected.length ? `Имя набора (${selected.length} датч.)` : 'Сначала выберите датчики'}
              style={{ flex: 1, minWidth: 0, padding: 'var(--space-1) var(--space-2)', background: 'var(--bg)', border: '1px solid var(--line)', color: 'var(--text-1)', fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)' }}
            />
            <button onClick={saveSet} disabled={!setName.trim() || selected.length === 0 || setsBusy}
              title="Сохранить текущий выбор как набор"
              style={{
                padding: 'var(--space-1) var(--space-2)', border: '1px solid var(--accent)', cursor: (!setName.trim() || !selected.length || setsBusy) ? 'default' : 'pointer',
                background: (!setName.trim() || !selected.length) ? 'transparent' : 'var(--accent-glow)',
                color: (!setName.trim() || !selected.length) ? 'var(--text-3)' : 'var(--accent)',
                fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)', whiteSpace: 'nowrap', borderRadius: 'var(--r-sm)',
              }}>
              {setsBusy ? 'Сохранение…' : '💾 Сохранить'}
            </button>
          </div>
          {/* Видимый фидбэк операций с наборами; цвет — только для ошибки (severity), успех моно */}
          {setsMsg && (
            <div role="status" className="anim-fade-up" style={{
              marginTop: 'var(--space-1)', fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)',
              color: setsMsg.type === 'err' ? 'var(--crit)' : 'var(--text-2)',
            }}>
              {setsMsg.text}
            </div>
          )}
        </div>

        <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
          {filtered.map(s => {
            const on = selected.includes(s.id)
            const label = `ГПА-${s.gpa.replace('GPA', '')} · ${ruSensor(s.name)}`
            return (
              <label key={s.id} title={label} style={{
                display: 'flex', alignItems: 'flex-start', gap: 8, padding: '5px 10px', cursor: 'pointer',
                fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)', lineHeight: 1.35,
                background: on ? 'var(--accent-glow)' : 'transparent', color: on ? 'var(--accent)' : 'var(--text-2)',
                transition: 'background-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard)',
              }}>
                <input type="checkbox" checked={on} onChange={() => toggle(s.id)} style={{ marginTop: 2, flexShrink: 0 }} />
                {/* полные названия: перенос строк, без обрезки */}
                <span style={{ whiteSpace: 'normal', wordBreak: 'break-word' }}>{label}</span>
              </label>
            )
          })}
        </div>
      </div>

      {/* Resize handle — раздвигает панель названий */}
      <div
        onMouseDown={() => { draggingRef.current = true; document.body.style.cursor = 'col-resize' }}
        title="Потяните, чтобы изменить ширину панели"
        style={{ cursor: 'col-resize', background: 'var(--line)', borderLeft: '1px solid var(--line-2)', transition: 'background-color var(--dur-fast) var(--ease-standard)' }}
        onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = 'var(--accent)' }}
        onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = 'var(--line)' }}
      />

      {/* Chart */}
      <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, padding: '10px 14px' }}>
        <MultiSensorChart data={multi} loading={isFetching} theme={theme} normalized={normalized} showRangeslider={showRangeslider} />
      </div>
    </div>
  )
}
