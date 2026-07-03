import { useEffect, useRef, useMemo, useState, memo } from 'react'
import type { CaseInfo, Severity, SensorExplain } from '../../types'
import { SEV_COLOR } from '../../types'
import { ruSensor } from '../../lib/sensorLabels'
import { useModal } from '../../lib/useModal'
import { ChartSkeleton } from './ChartSkeleton'
import { revealChart, drawPath } from '../../lib/chartMotion'
import type { RevealHandle } from '../../lib/chartMotion'
import { useBklitHover } from './useBklitHover'

interface ContributingFeaturesProps {
  open: boolean
  sensorName: string
  caseInfo: CaseInfo | null
  /** Реальная атрибуция аномалии из /explain (SHAP). Если есть — приоритетна над caseInfo.feats. */
  explain?: SensorExplain | null
  /** Время события, по которому открыта атрибуция (для подписи и маркера). */
  eventTs?: string | null
  /** Конец участка (region SHAP): если задан — режим «по участку», иначе «по событию». */
  regionTo?: string | null
  theme?: 'dark' | 'light'
  loading?: boolean
  onClose: () => void
}

// Палитра вкладчиков (циклично) — стабильный цвет по позиции в топе.
const FEAT_COLORS = ['#ff5c6c', '#ffb454', '#5cc8ff', '#9d7bff', '#46d39a', '#e879c9']

// ── Интерактивный наложенный график вкладчиков (зум/ховер/слайдер) ──
type PlotlyData = Record<string, unknown>
type PlotlyLayout = Record<string, unknown>
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type PlotlyModule = any
let _plotlyP: Promise<PlotlyModule> | null = null
function getPlotly(): Promise<PlotlyModule> {
  if (!_plotlyP) {
    // @ts-expect-error plotly.js-basic-dist-min без бандл-типов
    _plotlyP = import('plotly.js-basic-dist-min').then(m => m.default ?? m)
  }
  return _plotlyP!
}

function OverlayContribChart({ explain, theme }: { explain: SensorExplain; theme: 'dark' | 'light' }) {
  const ref = useRef<HTMLDivElement>(null)
  const initRef = useRef(false)
  // bklit-style «построение» графика вкладчиков при открытии модалки
  const revealRef = useRef<RevealHandle | undefined>(undefined)
  const hover = useBklitHover(theme)   // bklit-ховер: точки + плавающий тултип + пилюля
  // rangeslider выкл по умолчанию (вторая мини-копия всех рядов = удвоение перерисовки)
  const [showRangeslider, setShowRangeslider] = useState(false)

  const { traces, layout } = useMemo(() => {
    const gridColor = theme === 'dark' ? '#2C2C32' : '#D0D4D4'
    const fontColor = theme === 'dark' ? '#848494' : '#6A7878'
    const normParams = (s: { t: string; v: number }[]) => {
      const ys = s.map(p => p.v).filter(Number.isFinite)
      const lo = ys.length ? Math.min(...ys) : 0
      const hi = ys.length ? Math.max(...ys) : 1
      return { lo, span: (hi - lo) || 1 }
    }
    const traces: PlotlyData[] = []
    // целевой датчик — опорная (контрастная) линия: видно «цель vs драйверы»
    if (explain.target_series && explain.target_series.length) {
      const ts = explain.target_series
      const { lo, span } = normParams(ts)
      traces.push({
        x: ts.map(p => p.t), y: ts.map(p => (p.v - lo) / span),
        name: `${ruSensor(explain.sensor_id)} — цель`, type: 'scatter', mode: 'lines',
        line: { color: theme === 'dark' ? '#E6EDF3' : '#1B2530', width: 2.4 },
        customdata: ts.map(p => p.v),
        hoverinfo: 'none',   // нативный бокс не рисуем — свой bklit-тултип
      })
    }
    explain.contributors.forEach((c, i) => {
      if (!c.series?.length) return
      const { lo, span } = normParams(c.series)
      const color = FEAT_COLORS[i % FEAT_COLORS.length]
      const sign = c.contrib >= 0 ? '+' : '−'
      const label = `${ruSensor(c.name)} · вклад ${sign}${Math.abs(c.contrib).toFixed(3)}`
      traces.push({
        x: c.series.map(p => p.t), y: c.series.map(p => (p.v - lo) / span),
        name: label, type: 'scatter', mode: 'lines',
        line: { color, width: 1.7 },
        customdata: c.series.map(p => p.v),
        hoverinfo: 'none',   // нативный бокс не рисуем — свой bklit-тултип
      })
    })
    // подсветка выбранного участка
    const shapes: PlotlyLayout[] = []
    if (explain.region?.t0 && explain.region?.t1) {
      shapes.push({
        type: 'rect', xref: 'x', yref: 'paper',
        x0: explain.region.t0, x1: explain.region.t1, y0: 0, y1: 1,
        fillcolor: 'rgba(88,166,255,0.10)',
        line: { width: 1, color: 'rgba(88,166,255,0.45)', dash: 'dot' }, layer: 'below',
      })
    }
    const layout: PlotlyLayout = {
      paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
      font: { family: "'Inter', monospace", size: 11, color: fontColor },
      xaxis: {
        gridcolor: gridColor, linecolor: gridColor, tickformat: '%d.%m %H:%M',
        hoverformat: '%d.%m.%Y %H:%M', showgrid: true, zeroline: false,
        rangeslider: { visible: showRangeslider, thickness: 0.1 },
      },
      yaxis: {
        gridcolor: gridColor, linecolor: gridColor, showgrid: true, zeroline: false,
        title: { text: 'нормировано 0–1', font: { size: 10 } }, range: [-0.04, 1.04],
      },
      legend: { orientation: 'h', x: 0, y: 1.06, xanchor: 'left', yanchor: 'bottom', font: { size: 10.5 } },
      margin: { t: 40, r: 10, b: 28, l: 44 },
      hovermode: 'x unified',
      dragmode: 'zoom',
      shapes,
      hoverlabel: { font: { size: 11, family: "'Inter', monospace" }, bgcolor: 'rgba(13,20,38,0.9)', bordercolor: 'rgba(88,166,255,0.4)' },
      uirevision: explain.sensor_id + explain.event_ts,
    }
    return { traces, layout }
  }, [explain, theme, showRangeslider])

  useEffect(() => {
    const el = ref.current
    if (!el || !traces.length) return
    let cancelled = false
    const config = { displayModeBar: 'hover', responsive: true, scrollZoom: true, modeBarButtonsToRemove: ['lasso2d', 'select2d', 'toImage'] }
    getPlotly().then(Plotly => {
      // флаг отмены: импорт Plotly мог зарезолвиться уже после размонтирования
      if (cancelled || !ref.current) return
      if (initRef.current) Plotly.react(el, traces, layout, config)
      else {
        Plotly.newPlot(el, traces, layout, config); initRef.current = true
        revealRef.current?.cancel()
        revealRef.current = revealChart(el)
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        ;(el as any).on('plotly_hover', (e: any) => hover.onHover(el, e))
        ;(el as any).on('plotly_unhover', () => hover.hide())
      }
    })
    return () => { cancelled = true }
  }, [traces, layout])

  useEffect(() => () => {
    const el = ref.current
    revealRef.current?.cancel(); revealRef.current = undefined
    getPlotly().then(Plotly => { if (el) { Plotly.purge(el); initRef.current = false } })
  }, [])

  return (
    <div ref={hover.wrapRef} style={{ position: 'relative', width: '100%', height: 380 }}>
      <div ref={ref} style={{ width: '100%', height: '100%' }} />
      {hover.overlays}
      <button
        onClick={() => setShowRangeslider(v => !v)}
        title="Полоса навигации по всему диапазону (range slider)"
        style={{
          position: 'absolute', bottom: 6, right: 8, zIndex: 3,
          padding: '2px 8px', fontFamily: 'var(--font-mono)', fontSize: 11,
          border: '1px solid', borderColor: showRangeslider ? 'var(--accent)' : 'var(--line)',
          background: showRangeslider ? 'var(--accent-glow)' : 'rgba(13,20,38,0.7)',
          color: showRangeslider ? 'var(--accent)' : 'var(--text-2)',
          cursor: 'pointer', borderRadius: 'var(--r-sm)',
        }}
      >Полоса</button>
    </div>
  )
}

// Детерминированный PRNG (mulberry32) — стабильный спарклайн по seed (только для mock-фолбэка).
function makeRng(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s |= 0
    s = (s + 0x6d2b79f5) | 0
    let t = Math.imul(s ^ (s >>> 15), 1 | s)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

// Форматирование даты/времени — на уровне модуля, чтобы не создавать функцию при каждом рендере.
function fmtDt(t: string): string {
  return new Date(t).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// Псевдослучайный спарклайн — фолбэк, когда реальных рядов нет (БД офлайн / нет окна).
const MockPlot = memo(function MockPlot({ color, seed }: { color: string; seed: number }) {
  const n = 60
  const rng = makeRng(seed + 1)
  const pts: number[] = []
  let v = 50
  for (let i = 0; i < n; i++) {
    v += (rng() - 0.45) * 4
    if (i > n * 0.7) v += 3
    pts.push(v)
  }
  const mn = Math.min(...pts)
  const mx = Math.max(...pts)
  const span = mx - mn || 1
  const path = pts
    .map((d, i) => `${i ? 'L' : 'M'}${((i / (n - 1)) * 280).toFixed(0)} ${(70 - ((d - mn) / span) * 60).toFixed(0)}`)
    .join(' ')
  const pathRef = useRef<SVGPathElement>(null)
  useEffect(() => {
    if (!pathRef.current) return
    const h = drawPath(pathRef.current)
    return () => h?.cancel()
  }, [path])
  return (
    <svg viewBox="0 0 280 76" style={{ width: '100%' }}>
      <line x1="196" y1="0" x2="196" y2="76" stroke="rgba(255,92,108,0.4)" strokeDasharray="3 2" />
      <path ref={pathRef} d={path} fill="none" stroke={color} strokeWidth={1.6} />
    </svg>
  )
})

// «Разбор режима»: что именно изменилось у драйверов за окно (тренд) и как это
// повлияло на расчётное значение (знак SHAP-вклада). Отвечает на вопрос оператора
// «что в режиме поехало и куда оно толкнуло показатель».
function RegimeSummary({ explain }: { explain: SensorExplain }) {
  // useMemo: SHAP-агрегации пересчитываются только при смене explain, а не при каждом рендере родителя.
  const { rows, top, act, exp, devPct, verdict } = useMemo(() => {
    const rows = explain.contributors.map((c, i) => {
      const ys = (c.series || []).map(p => p.v).filter(Number.isFinite)
      let dPct: number | null = null
      let dir = '→'
      if (ys.length >= 6) {
        const k = Math.max(2, Math.floor(ys.length / 4))
        const first = ys.slice(0, k).reduce((a, b) => a + b, 0) / k
        const last = ys.slice(-k).reduce((a, b) => a + b, 0) / k
        const base = Math.abs(first) > 1e-9 ? Math.abs(first) : (Math.max(...ys) - Math.min(...ys) || 1)
        dPct = ((last - first) / base) * 100
        dir = dPct > 0.5 ? '↑' : dPct < -0.5 ? '↓' : '→'
      }
      return { name: c.name, contrib: c.contrib, dPct, dir, color: FEAT_COLORS[i % FEAT_COLORS.length] }
    })
    const top = rows.slice().sort((a, b) => Math.abs(b.contrib) - Math.abs(a.contrib))[0]
    const act = explain.actual
    const exp = explain.expected
    const devPct = (act != null && exp != null && Math.abs(exp) > 1e-9) ? ((act - exp) / Math.abs(exp)) * 100 : null
    const moved = rows.some(r => r.dPct != null && Math.abs(r.dPct) > 3)
    let verdict = ''
    if (devPct == null) {
      verdict = 'Недостаточно данных для вывода об отклонении.'
    } else if (Math.abs(devPct) < 3) {
      verdict = `Факт близок к прогнозу (${devPct >= 0 ? '+' : ''}${devPct.toFixed(1)}%) — режим объясняет поведение, явной аномалии на участке нет.`
    } else if (devPct < 0) {
      verdict = `Факт ниже прогноза на ${Math.abs(devPct).toFixed(1)}% (недобор). ${moved ? 'Режим на участке менялся — частично объяснимо сдвигом драйверов; ' : 'Режим стабилен — '}устойчивый недобор указывает на снижение эффективности/деградацию.`
    } else {
      verdict = `Факт выше прогноза на ${devPct.toFixed(1)}% (превышение). ${moved ? 'Режим менялся (см. драйверы выше); ' : 'Режим стабилен; '}проверьте, не растёт ли показатель быстрее ожидаемого по режиму.`
    }
    return { rows, top, act, exp, devPct, verdict }
  }, [explain])

  return (
    <div className="anim-fade-up" style={{ background: 'var(--surface-2)', border: '1px solid var(--line)', borderLeft: '3px solid var(--accent)', borderRadius: 'var(--r-sm)', padding: 'var(--space-3)', marginTop: 'var(--space-3)' }}>
      <div style={{ fontWeight: 700, fontSize: 13, color: 'var(--text-1)', marginBottom: 'var(--space-2)' }}>
        🧠 Автоанализ участка
      </div>
      {act != null && exp != null && (
        <div className="font-mono" style={{ fontSize: 12, color: 'var(--text-2)', marginBottom: 'var(--space-2)' }}>
          Среднее по участку: факт <b style={{ color: 'var(--text-1)' }}>{act.toFixed(3)}</b> · прогноз <b style={{ color: 'var(--text-1)' }}>{exp.toFixed(3)}</b>
          {devPct != null && <> · отклонение <b style={{ color: Math.abs(devPct) < 3 ? 'var(--ok)' : devPct < 0 ? 'var(--warn)' : 'var(--crit)' }}>{devPct >= 0 ? '+' : ''}{devPct.toFixed(1)}%</b></>}
        </div>
      )}
      {/* Вердикт и пояснения ограничены по ширине (var(--measure-md)) — длинные строки читаемее */}
      <div style={{ fontSize: 12.5, color: 'var(--text-1)', marginBottom: 'var(--space-2)', lineHeight: 1.45, maxWidth: 'var(--measure-md)' }}>{verdict}</div>
      {top && (
        <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 'var(--space-2)', lineHeight: 1.45, maxWidth: 'var(--measure-md)' }}>
          Главный драйвер — <b style={{ color: 'var(--text-1)' }}>{ruSensor(top.name)}</b>
          {top.dPct != null && <> ({top.dir} {Math.abs(top.dPct).toFixed(1)}% за окно)</>},
          {' '}он {top.contrib >= 0 ? 'повышал' : 'понижал'} ожидаемое значение сильнее прочих.
        </div>
      )}
      {rows.map((r, i) => (
        <div key={r.name + i} style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--space-2)', fontFamily: 'var(--font-mono)', fontSize: 12, margin: 'var(--space-1) 0', lineHeight: 1.4 }}>
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: r.color, flexShrink: 0, alignSelf: 'center' }} />
          <span style={{ flex: 1, color: 'var(--text-2)' }}>{ruSensor(r.name)}</span>
          <span className="font-mono" style={{ color: 'var(--text-1)', minWidth: 66, textAlign: 'right' }}>
            {r.dir} {r.dPct != null ? `${Math.abs(r.dPct).toFixed(1)}%` : '—'}
          </span>
          <span style={{ color: r.contrib >= 0 ? 'var(--warn)' : 'var(--teal)', minWidth: 96, textAlign: 'right' }}>
            {r.contrib >= 0 ? '↑ повышал' : '↓ понижал'}
          </span>
        </div>
      ))}
      <div className="font-mono" style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 'var(--space-2)', lineHeight: 1.4, maxWidth: 'var(--measure-md)' }}>
        Стрелка — тренд драйвера за окно; «повышал/понижал» — направление его вклада в расчётное значение.
        Дрейф = устойчивое отклонение остатка, не объяснимое этими режимными сдвигами.
      </div>
    </div>
  )
}

export function ContributingFeatures({ open, sensorName, caseInfo, explain, eventTs, regionTo, theme = 'dark', loading, onClose }: ContributingFeaturesProps) {
  // фокус-трап + закрытие по Esc + возврат фокуса на триггер при закрытии (WCAG 2.4.3)
  const cardRef = useModal<HTMLDivElement>(open, onClose)

  if (!open) return null
  // Нужен хотя бы один источник: реальный explain или кейс из базы (либо идёт загрузка).
  const hasReal = !!(explain && explain.contributors && explain.contributors.length)
  if (!hasReal && !caseInfo && !loading) return null

  const sev: Severity = caseInfo?.sev ?? explain?.severity ?? 'warn'
  const sevCol = SEV_COLOR[sev]
  const sevLabel = caseInfo?.sevl ?? (explain?.kind ? `${sev.toUpperCase()} · ${explain.kind}` : sev.toUpperCase())

  const baseTs = eventTs || explain?.event_ts
  const evDate = regionTo && baseTs
    ? `участок ${fmtDt(baseTs)} – ${fmtDt(regionTo)}`
    : (baseTs ? fmtDt(baseTs) : null)

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Важные признаки аномалии"
      onClick={onClose}
      className="fixed inset-0 flex items-center justify-center z-[95]"
      style={{ background: 'rgba(8,10,13,0.78)', animation: 'fade-backdrop var(--dur-moderate) var(--ease-decelerate) both' }}
    >
      {/* .ov-card */}
      <div
        ref={cardRef}
        onClick={e => e.stopPropagation()}
        style={{
          background: 'var(--surface)',
          border: '1px solid var(--accent)',
          borderRadius: 'var(--r-md)',
          width: 'min(900px, 92%)',
          maxHeight: '86%',
          overflow: 'auto',
          padding: 'var(--space-4)',
          boxShadow: 'var(--shadow-lg)',
          animation: 'scale-in var(--dur-moderate) var(--ease-spring) both',
        }}
      >
        {/* .ov-h */}
        <div className="flex items-center justify-between" style={{ marginBottom: 'var(--space-2)' }}>
          <h3 style={{ fontFamily: 'var(--font-display)', fontSize: 'var(--fs-lg)', fontWeight: 700, color: 'var(--text-1)' }}>
            Важные признаки аномалии
          </h3>
          {/* Кнопка закрытия — единый канон с EventDrawer: 32×32, surface-2, hover→crit */}
          <button
            onClick={onClose}
            className="flex items-center justify-center rounded-sm transition-all"
            title="Закрыть (Esc)"
            aria-label="Закрыть"
            style={{
              width: 32,
              height: 32,
              cursor: 'pointer',
              color: 'var(--text-2)',
              fontSize: 'var(--fs-md)',
              border: '1px solid var(--line)',
              background: 'var(--surface-2)',
              lineHeight: 1,
            }}
            onMouseEnter={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--crit)'; (e.currentTarget as HTMLElement).style.color = 'var(--crit)' }}
            onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--line)'; (e.currentTarget as HTMLElement).style.color = 'var(--text-2)' }}
          >
            ✕
          </button>
        </div>

        {/* .ov-sub */}
        <div
          className="flex items-center gap-2 flex-wrap"
          style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-xs)', color: 'var(--text-2)', marginBottom: 'var(--space-4)' }}
        >
          <span
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              padding: '1px 7px',
              borderRadius: 3,
              color: '#000',
              // 10px/1px/7px/3px — вне шкалы (микро-бейдж), оставлены намеренно
              fontWeight: 700,
              background: sevCol,
            }}
          >
            {sevLabel}
          </span>
          <span>{ruSensor(sensorName)}</span>
          {evDate && <span style={{ color: 'var(--text-3)' }}>· {evDate}</span>}
          {explain && explain.actual != null && explain.expected != null && (
            <span style={{ color: 'var(--text-3)' }}>
              · факт <b style={{ color: 'var(--text-1)' }}>{explain.actual.toFixed(3)}</b>
              {' '}/ прогноз <b style={{ color: 'var(--text-1)' }}>{explain.expected.toFixed(3)}</b>
            </span>
          )}
        </div>

        {/* .diag — блок интерпретации (база кейсов) */}
        {caseInfo && (
          <div
            style={{
              background: 'var(--surface-2)',
              border: '1px solid var(--line)',
              borderLeft: `3px solid ${sevCol}`,
              borderRadius: 'var(--r-sm)',
              padding: 'var(--space-3) var(--space-4)',
              marginBottom: 'var(--space-4)',
            }}
          >
            <span style={{ float: 'right', fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-3)' }}>
              кейс из базы
            </span>
            <div style={{ fontWeight: 700, fontSize: 'var(--fs-sm)', marginBottom: 'var(--space-2)', color: 'var(--text-1)' }}>🔍 Интерпретация</div>
            {/* max-width по var(--measure-md): ограничивает длину строки для читабельности */}
            <div style={{ fontSize: 13, color: 'var(--text-2)', margin: 'var(--space-1) 0', lineHeight: 1.45, maxWidth: 'var(--measure-md)' }}>{caseInfo.diag}</div>
            <div style={{ fontSize: 13, color: 'var(--text-2)', margin: 'var(--space-1) 0', lineHeight: 1.45, maxWidth: 'var(--measure-md)' }}>
              <b style={{ color: 'var(--text-1)' }}>Вероятная причина:</b> {caseInfo.cause}
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-2)', margin: 'var(--space-1) 0', lineHeight: 1.45, maxWidth: 'var(--measure-md)' }}>
              <b style={{ color: 'var(--text-1)' }}>Что проверить:</b>
              <div className="flex flex-wrap" style={{ gap: 'var(--space-2)', marginTop: 'var(--space-2)' }}>
                {caseInfo.check.map((c, i) => (
                  <span
                    key={i}
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 11,
                      padding: '3px 9px',
                      border: '1px solid var(--line)',
                      borderRadius: 20,
                      color: 'var(--text-2)',
                      background: 'var(--bg)',
                    }}
                  >
                    {c}
                  </span>
                ))}
              </div>
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-2)', margin: 'var(--space-1) 0', lineHeight: 1.45, maxWidth: 'var(--measure-md)' }}>
              <b style={{ color: 'var(--text-1)' }}>Похожие кейсы:</b>{' '}
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-3)' }}>
                {caseInfo.similar.map((x, i) => (
                  <span key={i}>
                    {i > 0 && ' · '}
                    <span style={{ color: 'var(--accent)' }}>{x}</span>
                  </span>
                ))}
              </span>
            </div>
          </div>
        )}

        {/* подзаголовок сетки + индикатор источника (вживую vs база) */}
        <div
          className="flex items-center justify-between"
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 'var(--fs-xs)',
            color: 'var(--text-3)',
            letterSpacing: '0.04em',
            margin: 'var(--space-2) 0',
          }}
        >
          <span>{regionTo ? 'Параметры, внёсшие вклад (средний по участку)' : 'Параметры, внёсшие вклад (на момент аномалии)'}</span>
          <span style={{ fontSize: 10, color: hasReal ? 'var(--ok)' : 'var(--text-3)' }}>
            {hasReal ? '● вживую (модель)' : loading ? '○ загрузка…' : '○ оценка по базе'}
          </span>
        </div>

        {/* .ov-grid */}
        {loading && !hasReal ? (
          <ChartSkeleton label="Вычисляем вклад параметров на момент аномалии…" height={380} />
        ) : hasReal ? (
          <>
            <OverlayContribChart explain={explain!} theme={theme} />
            {/* Поясняющий текст ограничен по ширине (var(--measure-md)) для читабельности */}
            <div className="font-mono" style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 'var(--space-2)', lineHeight: 1.4, maxWidth: 'var(--measure-md)' }}>
              Все драйверы нормированы 0–1 (форма сопоставима при разных единицах). Белая линия — сам датчик (цель).
              Колёсико/протяжка — зум, наведение — значения. Затенён выбранный участок.
            </div>
            <RegimeSummary explain={explain!} />
          </>
        ) : caseInfo ? (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-3)' }}>
            {caseInfo.feats.map(([name, contrib, color], i) => (
              <div
                key={i}
                style={{
                  background: 'var(--surface-2)',
                  border: '1px solid var(--line)',
                  borderRadius: 'var(--r-sm)',
                  padding: 'var(--space-3)',
                }}
              >
                <div
                  className="flex justify-between"
                  style={{ fontFamily: 'var(--font-mono)', fontSize: 12, marginBottom: 'var(--space-1)', color: 'var(--text-2)' }}
                >
                  <span>{name}</span>
                  <span style={{ color, fontWeight: 700 }}>вклад {contrib}</span>
                </div>
                <MockPlot color={color} seed={i} />
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  )
}
