import { useEffect, useRef, useMemo, useState, memo } from 'react'
import type { CSSProperties } from 'react'
import type { SensorChartResponse, SensorMeta, TimeSeriesPoint } from '../../types'
import { KIND_LABEL } from '../../types'
import { ChartSkeleton } from './ChartSkeleton'
import { revealChart } from '../../lib/chartMotion'
import type { RevealHandle } from '../../lib/chartMotion'

export interface GpaOverlay {
  gpa: string
  points: TimeSeriesPoint[]
  /** Явная подпись ряда (для датчиков, перетащенных на график); иначе «ГПА-n» */
  label?: string
  /** true → ряд другого масштаба/единиц (перетащенный датчик) → своя правая ось Y;
   *  false/undefined → тот же датчик с других ГПА (общий масштаб) → основная ось. */
  secondary?: boolean
}

interface SensorChartProps {
  sensor: SensorMeta | null
  chartData: SensorChartResponse | null
  loading: boolean
  /** Запрос графика упал (сеть/БД) — отличаем «ошибка» от «нет данных». */
  error?: boolean
  theme: 'dark' | 'light'
  kindFilter: string | null
  /** Зум/пан пользователя: окно (t0,t1) или null при autorange (double-click) */
  onRangeChange?: (win: { t0: string; t1: string } | null) => void
  viewDays?: number
  focusTimestamp?: string | null
  overlaySeries?: GpaOverlay[]
  /** Выделение участка ПРАВОЙ кнопкой (2D-бокс) → top-5 SHAP по региону.
   *  v0/v1 — диапазон значений (вертикальное выделение); undefined = по времени. */
  onRegionSelect?: (t0: string, t1: string, v0?: number, v1?: number) => void
}

const KIND_COLOR: Record<string, string> = {
  ml:       '#FF4560',
  frozen:   '#FACC15',
  neg:      '#F97316',
  roc:      '#F59E0B',
  seasonal: '#B48DFF',
  regime:   '#84CC16',
  cross:    '#3FB8AF',
}
const KIND_SYMBOL: Record<string, string> = {
  ml:       'diamond',
  frozen:   'circle',
  neg:      'star-diamond',
  roc:      'triangle-up',
  seasonal: 'triangle-up',
  regime:   'star',
  cross:    'triangle-down',
}
// Minimal types to avoid namespace issues with dynamic Plotly import
type PlotlyData = Record<string, unknown>
type PlotlyLayout = Record<string, unknown>
type PlotlyConfig = Record<string, unknown>

// plotly.js-basic-dist-min ships no TypeScript declarations; `any` is
// unavoidable here — the @ts-expect-error on the dynamic import line below
// already documents why the module itself is untyped.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type PlotlyModule = any

// Module-level config: no dynamic values, so we build it once instead of
// recreating an identical object on every useEffect run.
const PLOTLY_CONFIG: PlotlyConfig = {
  displayModeBar: false,
  responsive: true,
  scrollZoom: true,
  // Нативный autorange по двойному клику ОТКЛЮЧЁН: relayout({autorange:true}) на
  // scattergl-трейсах падает в Plotly (`Cannot read properties of undefined (reading
  // '_extremes')`) при гонке с react-перерисовкой. Сброс делаем сами через app-флоу.
  doubleClick: false,
}

let plotlyPromise: Promise<PlotlyModule> | null = null
function getPlotly(): Promise<PlotlyModule> {
  if (!plotlyPromise) {
    // @ts-expect-error plotly.js-basic-dist-min has no bundled TS declarations
    plotlyPromise = import('plotly.js-basic-dist-min').then(m => m.default ?? m)
  }
  return plotlyPromise!
}

function hexToRgba(hex: string, a: number) {
  const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16)
  return `rgba(${r},${g},${b},${a})`
}

// Разноцветные пунктиры для наложенных датчиков (drag-drop / сравн. ГПА),
// чтобы различать их между собой. Избегаем белого (Значение) и синего (Модель).
const OVERLAY_COLORS = ['#46d39a', '#ffb454', '#b48dff', '#e879c9', '#3fb8af', '#facc15', '#ff7e9a', '#7bd88f']

// Module-level helpers (stable references, no closure needed)
const _fmtLocal = (ms: number) => {
  const d = new Date(ms), p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
const _tsMs = (ts: string) => new Date(ts.replace(' ', 'T')).getTime()

export const SensorChart = memo(function SensorChart({ sensor, chartData, loading, error, theme, kindFilter, onRangeChange, viewDays, focusTimestamp, overlaySeries, onRegionSelect }: SensorChartProps) {
  const divRef = useRef<HTMLDivElement>(null)
  const initializedRef = useRef(false)
  // handle bklit-style «построения» графика (clip-wipe) при первом рендере датчика
  const revealRef = useRef<RevealHandle | undefined>(undefined)
  // нативный dblclick-листенер навешиваем один раз (div переживает смену датчика)
  const dblBoundRef = useRef(false)
  // выделение участка правой кнопкой (region SHAP)
  const onRegionSelectRef = useRef(onRegionSelect)
  onRegionSelectRef.current = onRegionSelect
  const [selRect, setSelRect] = useState<{ x: number; y: number; w: number; h: number } | null>(null)
  // кастомный тулбар: режим взаимодействия (вместо modebar Plotly) + слой рисования
  const [tool, setTool] = useState<'pointer' | 'pan' | 'draw'>('pointer')
  // Два НЕЗАВИСИМЫХ коридора (отдельные кнопки): conformal (строгий) и hybrid (σ-масштабированный,
  // всегда присутствует, шире в неуверенности). Можно включить оба сразу для сравнения.
  const [showConformal, setShowConformal] = useState(true)
  const [showHybrid, setShowHybrid] = useState(false)
  const toolRef = useRef(tool); toolRef.current = tool
  const drawCanvasRef = useRef<HTMLCanvasElement>(null)
  const strokesRef = useRef<{ x: number; y: number }[][]>([])
  const viewDaysRef = useRef<number | undefined>(undefined)
  // refs против stale closure: обработчик relayout вешается один раз при newPlot
  const onRangeChangeRef = useRef(onRangeChange)
  onRangeChangeRef.current = onRangeChange
  const debounceRef = useRef<number | undefined>(undefined)
  const programmaticRef = useRef(false)   // не реагировать на свои же relayout
  // для плавного кроссфейда при смене датчика (прячем резкий скачок шкалы Y)
  const prevSensorRef = useRef<string | undefined>(undefined)
  // таймер/кадр кроссфейда — отменяем при быстрой смене датчика/размонтировании
  // (иначе старый setTimeout перерисует НОВЫЙ датчик старыми данными / залипнет opacity)
  const fadeTimerRef = useRef<number | undefined>(undefined)
  const fadeRafRef = useRef<number | undefined>(undefined)
  // обёртка графика — для нативного capture-перехвата правого клика (до слоя Plotly)
  const wrapRef = useRef<HTMLDivElement>(null)
  // bklit-style ховер: плавающий тултип у курсора + точки-маркеры на линиях + пилюля даты на оси X
  const tipRef = useRef<HTMLDivElement>(null)
  const pillRef = useRef<HTMLDivElement>(null)
  const dotsRef = useRef<HTMLDivElement>(null)
  const dotPoolRef = useRef<HTMLDivElement[]>([])
  // RAF handle для hover-троттлинга — хранится в ref, чтобы отменять при размонтировании
  const hoverRafRef = useRef<number>(0)
  // для собственной вертикальной линии-курсора (рисуем на plotly_hover)
  const shapesRef = useRef<PlotlyLayout[]>([])
  const hoverLineColorRef = useRef('')
  hoverLineColorRef.current = theme === 'dark' ? 'rgba(88,166,255,0.85)' : 'rgba(40,90,160,0.8)'
  // стиль тултипа читается once-registered plotly_hover-обработчиком (без stale-theme замыкания)
  const tipStyleRef = useRef({ bg: '', border: '', fg: '' })
  tipStyleRef.current = theme === 'dark'
    ? { bg: 'rgba(13,20,38,0.94)', border: 'rgba(88,166,255,0.45)', fg: '#CDD9E5' }
    : { bg: 'rgba(255,255,255,0.97)', border: '#C2CCD6', fg: '#1B2530' }

  // ── Traces + shapes: не зависят от темы ────────────────────────────────────
  // Пересчёт только при изменении данных/фильтра — смена темы не пересоздаёт массивы
  const { traces, shapes, times, annotations, hasSecondary, hasEpi } = useMemo(() => {
    // chartData.sensor_id !== sensor.id → данные от ПРЕДЫДУЩЕГО датчика (keepPreviousData
    // при переключении). Не строим чужое: пустые traces → эффект не перерисовывает старое.
    if (!sensor || !chartData?.series.length || chartData.sensor_id !== sensor.id) {
      return { traces: [] as PlotlyData[], shapes: [] as PlotlyLayout[], times: [] as string[], annotations: [] as PlotlyLayout[], hasSecondary: false, hasEpi: false }
    }

    const times   = chartData.series.map(p => p.t)
    const reality = chartData.series.map(p => p.v)
    const pred    = chartData.series.map(p => p.p)
    // ДВА независимых коридора (кнопки-тумблеры conf/hyb, можно показать оба сразу для сравнения).
    // Активный слот бэка (lo/hi) = chartData.corridor_mode; альтернативный = lo2/hi2. Раскладываем
    // на conformal/hybrid по corridor_mode, каждый рисуем своим цветом. Границы обнуляем там, где
    // нет любой из них ИЛИ нет прогноза (иначе заливка «перепрыгивает» разрыв ломаной диагональю).
    const activeMode = (chartData.corridor_mode ?? 'conformal')
    // 'self' = univariate_band: единственный коридор = нормальный диапазон по режиму (в lo/hi),
    // без conformal/hybrid и без тумблера. Иначе — два независимых коридора conf/hyb.
    const isSelf = activeMode === 'self'
    const confLo: (number | null)[] = [], confHi: (number | null)[] = []
    const hybLo:  (number | null)[] = [], hybHi:  (number | null)[] = []
    for (const p of chartData.series) {
      const okP = Number.isFinite(p.p as number)
      const aLo = p.lo, aHi = p.hi, xLo = (p.lo2 ?? null), xHi = (p.hi2 ?? null)
      if (isSelf) {
        const ok = okP && Number.isFinite(aLo as number) && Number.isFinite(aHi as number)
        confLo.push(ok ? (aLo as number) : null); confHi.push(ok ? (aHi as number) : null)
        hybLo.push(null); hybHi.push(null)
        continue
      }
      const cLo = activeMode === 'conformal' ? aLo : xLo, cHi = activeMode === 'conformal' ? aHi : xHi
      const hLo = activeMode === 'hybrid'    ? aLo : xLo, hHi = activeMode === 'hybrid'    ? aHi : xHi
      const cok = okP && Number.isFinite(cLo as number) && Number.isFinite(cHi as number)
      const hok = okP && Number.isFinite(hLo as number) && Number.isFinite(hHi as number)
      confLo.push(cok ? (cLo as number) : null); confHi.push(cok ? (cHi as number) : null)
      hybLo.push(hok ? (hLo as number) : null);  hybHi.push(hok ? (hHi as number) : null)
    }
    // ── ЭПИСТЕМИКА (детектор новизны) — нижний subplot на оси y3 (общий X → синхронный зум/hover) ──
    const epiVals = chartData.series.map(p => (p.e != null && Number.isFinite(p.e) ? (p.e as number) : null))
    const epiThr  = chartData.epistemic_thr ?? null
    const hasEpi  = epiVals.some(v => v != null)

    const traces: PlotlyData[] = [
      // hoverinfo:'none' — плавающий бокс Plotly не рисуем; значения в bklit-тултипе
      // (plotly_hover при 'none' продолжает приходить, в отличие от 'skip').
      // type:'scatter' (SVG), НЕ 'scattergl': при смене числа трейсов коридора (зум↔полный
      // диапазон) react+autorange на scattergl падал с `_extremes`. Точек ~1.5k — SVG тянет.
      { x: times, y: reality, name: 'Значение',  line: { color: '#CDD9E5', width: 2 }, hoverinfo: 'none', type: 'scatter', mode: 'lines' },
      { x: times, y: pred,    name: `Модель (MAE: ${chartData.mae != null ? chartData.mae.toPrecision(3) : '—'})`, line: { color: '#58A6FF', width: 1.5, dash: 'dash' }, hoverinfo: 'none', connectgaps: false, type: 'scatter', mode: 'lines' },
    ]
    // Коридор ОТДЕЛЬНЫМИ непрерывными сегментами: каждый участок = пара (нижняя граница + заливка
    // tonexty). Так заливка не «перепрыгивает» разрыв диагональю между несмежными кусками.
    const pushBand = (loA: (number | null)[], hiA: (number | null)[], rgb: string, name: string) => {
      const npts = chartData.series.length
      let k = 0, firstBand = true
      while (k < npts) {
        if (loA[k] == null || hiA[k] == null) { k++; continue }
        let j = k
        while (j < npts && loA[j] != null && hiA[j] != null) j++
        // изолированный 1-точечный сегмент рисуется как вертикальный «пик» — пропускаем
        // (артефакт скачка предикта; настоящий коридор всегда ≥2 смежных точек)
        if (j - k < 2) { k = j; continue }
        const xs = times.slice(k, j)
        traces.push(
          { x: xs, y: loA.slice(k, j), line: { color: `rgba(${rgb},0.45)`, width: 1, dash: 'dash' }, hoverinfo: 'skip', showlegend: false, type: 'scatter', mode: 'lines' },
          { x: xs, y: hiA.slice(k, j), name, fill: 'tonexty',
            fillcolor: `rgba(${rgb},0.05)`,
            fillpattern: { shape: '/', size: 6, solidity: 0.28, fgcolor: `rgba(${rgb},0.42)`, bgcolor: `rgba(${rgb},0.05)` },
            line: { color: `rgba(${rgb},0.55)`, width: 1, dash: 'dash' }, hoverinfo: 'skip', showlegend: firstBand, type: 'scatter', mode: 'lines' },
        )
        firstBand = false
        k = j
      }
    }
    if (isSelf) {
      pushBand(confLo, confHi, '46,211,154', 'Норма-диапазон (self)')       // зелёный: self-conformal
    } else {
      if (showConformal) pushBand(confLo, confHi, '88,166,255', 'Conformal')   // синий
      if (showHybrid)    pushBand(hybLo,  hybHi,  '224,162,58', 'Hybrid')      // янтарь
    }

    // Наложенные ряды (пунктир, приглушённые). Перетащенные датчики (secondary)
    // имеют другой масштаб/единицы → отправляем их на правую ось Y2 (иначе на общей
    // оси они «прижимаются» к нулю и теряют смысл). Кросс-ГПА (тот же датчик) —
    // на основной оси, чтобы абсолютные значения были сопоставимы.
    let hasSecondary = false
    if (overlaySeries) {
      overlaySeries.forEach((ov, i) => {
        if (!ov.points.length) return
        if (ov.secondary) hasSecondary = true
        traces.push({
          x: ov.points.map(p => p.t), y: ov.points.map(p => p.v),
          name: ov.label ?? `ГПА-${ov.gpa.replace('GPA', '')}`, type: 'scatter', mode: 'lines',
          line: { color: OVERLAY_COLORS[i % OVERLAY_COLORS.length], width: 1.2, dash: 'dot' },
          opacity: 0.7, hoverinfo: 'skip',
          ...(ov.secondary ? { yaxis: 'y2' } : {}),
        })
      })
    }

    const byKind: Record<string, { t: string[]; v: number[] }> = {}
    for (const a of chartData.anomalies) {
      if (!byKind[a.kind]) byKind[a.kind] = { t: [], v: [] }
      byKind[a.kind].t.push(a.t)
      byKind[a.kind].v.push(a.v)
    }
    for (const [kind, pts] of Object.entries(byKind)) {
      const isHighlighted = !kindFilter || kindFilter === kind
      traces.push({
        x: pts.t, y: pts.v, mode: 'markers', type: 'scatter',
        name: KIND_LABEL[kind as keyof typeof KIND_LABEL] ?? kind,
        opacity: isHighlighted ? 1 : 0.15,
        // ховер показывает только Значение и Модель — маркеры аномалий из тултипа исключены
        hoverinfo: 'skip',
        marker: {
          color: KIND_COLOR[kind] ?? '#888',
          size: kindFilter === kind ? 13 : 9,
          symbol: KIND_SYMBOL[kind] ?? 'circle',
          line: kindFilter === kind ? { color: '#fff', width: 1.5 } : undefined,
        },
      })
    }

    // Soft background fills for highlighted anomaly kind (clustered)
    const shapes: PlotlyLayout[] = []
    const annotations: PlotlyLayout[] = []

    // Вертикальный разделитель «Начало мониторинга» на границе обучения:
    // слева — только факт, справа — факт + прогноз/коридор.
    if (chartData.train_ts && times.length > 1) {
      const tt = chartData.train_ts.replace('T', ' ')
      const ttMs = _tsMs(tt)
      if (ttMs > _tsMs(times[0]) && ttMs < _tsMs(times[times.length - 1])) {
        shapes.push({
          type: 'line', x0: tt, x1: tt, y0: 0, y1: 1, yref: 'paper',
          line: { color: 'rgba(132,204,22,0.55)', width: 1, dash: 'dot' },
        })
        annotations.push({
          x: tt, y: 1, yref: 'paper', yanchor: 'bottom', xanchor: 'left',
          text: 'Начало мониторинга', showarrow: false,
          font: { size: 10, color: 'rgba(132,204,22,0.9)' },
        })
      }
    }

    if (kindFilter && byKind[kindFilter] && times.length > 1) {
      const sorted = [...byKind[kindFilter].t].sort()
      const totalMs = _tsMs(times[times.length - 1]) - _tsMs(times[0])
      const gapMs   = Math.max(totalMs * 0.02, 30 * 60_000)
      const color   = KIND_COLOR[kindFilter] ?? '#888'
      let clStart = sorted[0], clEnd = sorted[0]
      const flush = () => {
        const buf = gapMs * 0.5
        shapes.push({
          type: 'rect', y0: 0, y1: 1, yref: 'paper',
          x0: _fmtLocal(_tsMs(clStart) - buf),
          x1: _fmtLocal(_tsMs(clEnd)   + buf),
          fillcolor: hexToRgba(color, 0.09),
          line: { width: 0.5, color: hexToRgba(color, 0.22) },
        })
      }
      for (let i = 1; i < sorted.length; i++) {
        if (_tsMs(sorted[i]) - _tsMs(sorted[i-1]) <= gapMs) {
          clEnd = sorted[i]
        } else { flush(); clStart = sorted[i]; clEnd = sorted[i] }
      }
      flush()
    }

    // ── Эпистемика на нижней оси y3 (заливка u_epi + маркеры-новизны выше порога) ──
    if (hasEpi) {
      traces.push({
        x: times, y: epiVals, yaxis: 'y3', type: 'scatter', mode: 'lines',
        name: 'Эпистемика', line: { color: '#C77DFF', width: 1.4 },
        fill: 'tozeroy', fillcolor: 'rgba(199,125,255,0.16)', connectgaps: false,
        hoverinfo: 'none', showlegend: false,   // 'none' (не 'skip') → точка попадает в plotly_hover.points → значение в тултипе
      })
      if (epiThr != null && times.length) {
        // порог новизны κ·1.5 — пунктирная линия на всю ширину
        traces.push({
          x: [times[0], times[times.length - 1]], y: [epiThr, epiThr], yaxis: 'y3',
          type: 'scatter', mode: 'lines', name: 'порог', hoverinfo: 'skip', showlegend: false,
          line: { color: '#C77DFF', width: 1, dash: 'dash' }, opacity: 0.55,
        })
        const ax: string[] = [], ay: number[] = []
        for (let i = 0; i < epiVals.length; i++) {
          const v = epiVals[i]
          if (v != null && v > epiThr) { ax.push(times[i]); ay.push(v) }
        }
        if (ax.length) traces.push({
          x: ax, y: ay, yaxis: 'y3', type: 'scatter', mode: 'markers', name: 'новизна',
          marker: { color: '#C77DFF', size: 4 }, hoverinfo: 'skip', showlegend: false,
        })
      }
    }

    return { traces, shapes, times, annotations, hasSecondary, hasEpi, epiThr }
  }, [chartData, kindFilter, overlaySeries, sensor?.id, showConformal, showHybrid])

  // статические shapes (граница мониторинга, кластеры) — для дорисовки линии-курсора
  shapesRef.current = shapes


  // ── Layout: пересчёт только при смене темы или shapes ──────────────────────
  const layout = useMemo(() => {
    const gridColor    = theme === 'dark' ? '#2C2C32' : '#D0D4D4'
    // Читаемость осей/тиков с дистанции оператора: контрастнее тусклого серого.
    // Тёмная — ближе к var(--text-2) (#B7C0D6); светлая — насыщенный тёмный (#3c486a).
    const fontColor    = theme === 'dark' ? '#B7C0D6' : '#3c486a'
    // Вертикальную линию-курсор рисуем сами (shape на plotly_hover) — встроенный
    // spike в режиме 'x unified' игнорирует цвет и выходит грубо-белым. Поэтому
    // showspikes:false, а красивую тонкую акцентную линию ставим обработчиком ниже.
    const hoverBg      = theme === 'dark' ? 'rgba(13,20,38,0.94)' : 'rgba(255,255,255,0.97)'
    const hoverBorder  = theme === 'dark' ? 'rgba(88,166,255,0.45)' : '#C2CCD6'
    const hoverFont    = theme === 'dark' ? '#CDD9E5' : '#1B2530'
    return {
      paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
      font: { family: "'Inter', monospace", size: 11, color: fontColor },
      xaxis: {
        gridcolor: gridColor, linecolor: gridColor, tickfont: { size: 11 },
        tickformat: '%d.%m %H:%M', hoverformat: '%d.%m.%Y %H:%M', showgrid: true, zeroline: false,
        showspikes: false,
        // При эпистемике ось X якорится к нижнему subplot (y3) → тики внизу, оба графика
        // делят один X (нативно синхронный зум/пан/hover). Rangeslider выкл (свой зум).
        anchor: hasEpi ? 'y3' : 'y',
        rangeslider: { visible: false },
      },
      // Основной график: при эпистемике занимает верх (domain 0.26–1), нижние 0–0.15 — y3.
      yaxis: { domain: hasEpi ? [0.26, 1] : [0, 1], gridcolor: gridColor, linecolor: gridColor, tickfont: { size: 11 }, showgrid: true, zeroline: false, fixedrange: false, showspikes: false },
      // Правая ось для наложенных датчиков иного масштаба (перетащенных): свой
      // autorange → форма ряда читаема, а не прижата к нулю общей оси.
      ...(hasSecondary ? {
        yaxis2: {
          overlaying: 'y', side: 'right', showgrid: false, zeroline: false,
          autorange: true, fixedrange: false,
          tickfont: { size: 10, color: OVERLAY_COLORS[0] },
          linecolor: gridColor,
        },
      } : {}),
      // Нижний subplot ЭПИСТЕМИКИ (детектор новизны): общий X с основным графиком.
      ...(hasEpi ? {
        yaxis3: {
          domain: [0, 0.15], anchor: 'x', gridcolor: gridColor, linecolor: gridColor,
          tickfont: { size: 9 }, showgrid: false, zeroline: false, fixedrange: true,
          rangemode: 'tozero', nticks: 3,
        },
      } : {}),
      dragmode: 'zoom',
      legend: {
        orientation: 'h', x: 0, y: 1.04,
        xanchor: 'left', yanchor: 'bottom',
        font: { size: 11 }, bgcolor: 'rgba(0,0,0,0)', bordercolor: 'rgba(0,0,0,0)',
      },
      margin: { t: 36, r: hasSecondary ? 52 : 8, b: 30, l: 48 },
      hovermode: 'x unified',
      hoverdistance: -1,   // ховер ловит курсор на любом расстоянии по Y
      hoverlabel: {
        font: { size: 11.5, family: "'Inter', monospace", color: hoverFont },
        bgcolor: hoverBg,
        bordercolor: hoverBorder,
        align: 'left',
        namelength: -1,
      },
      shapes,
      // подпись нижнего subplot эпистемики (над осью y3)
      annotations: hasEpi ? [...annotations, {
        xref: 'paper', x: 0, xanchor: 'left', yref: 'paper', y: 0.17, yanchor: 'bottom',
        text: 'ЭПИСТЕМИЧЕСКАЯ НЕОПР. · детектор новизны', showarrow: false,
        font: { size: 9, color: '#C77DFF' },
      }] : annotations,
      // зум/пан пользователя переживает Plotly.react при обновлении данных;
      // при смене темы Plotly перерисовывает UI с нуля (браш на uirevision не влияет).
      uirevision: `${sensor?.id}-${theme}`,
    }
  }, [theme, shapes, annotations, sensor?.id, hasSecondary, hasEpi])

  // ── Plotly render: вызывается только когда реально меняются данные или тема ─
  useEffect(() => {
    if (!divRef.current || !sensor || !traces.length) return

    const el = divRef.current
    const isFirst = !initializedRef.current
    const shouldZoom = viewDays !== undefined && (isFirst || viewDays !== viewDaysRef.current)
    // отменяем незавершённый кроссфейд предыдущей смены (этот эффект перезапускается
    // при смене датчика → стайл-таймер старого датчика не должен перерисовать новый)
    if (fadeTimerRef.current) { window.clearTimeout(fadeTimerRef.current); fadeTimerRef.current = undefined }
    if (fadeRafRef.current) { cancelAnimationFrame(fadeRafRef.current); fadeRafRef.current = undefined }
    getPlotly().then(Plotly => {
      if (!el) return
      if (initializedRef.current) {
        // Та же кривая (смена темы/фильтра/зума/догон данных) → обновляем НА МЕСТЕ.
        // Смена датчика идёт через purge+newPlot+reveal (ветка else): флаг сброшен
        // синхронно в cleanup, поэтому старый график НЕ «моргает» перед новым.
        el.style.opacity = '1'
        Plotly.react(el, traces, layout, PLOTLY_CONFIG)
      } else {
        Plotly.newPlot(el, traces, layout, PLOTLY_CONFIG)
        initializedRef.current = true
        // bklit-style «построение»: горизонтальная шторка слева-направо — при первом
        // показе И при каждой смене датчика (фикс «старый→новый»: чистое построение).
        revealRef.current?.cancel()
        revealRef.current = revealChart(el)
        if (!dblBoundRef.current) {
          dblBoundRef.current = true
          // двойной клик → сброс зума через app-флоу (пресетный диапазон), БЕЗ Plotly
          // autorange (он падает на scattergl, см. doubleClick:false в PLOTLY_CONFIG).
          el.addEventListener('dblclick', () => { onRangeChangeRef.current?.(null) })
        }
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        ;(el as any).on('plotly_relayout', (ev: Record<string, unknown>) => {
          if (programmaticRef.current) { programmaticRef.current = false; return }
          if (ev['xaxis.autorange']) { onRangeChangeRef.current?.(null); return }
          const r0 = ev['xaxis.range[0]'] as string | undefined
          const r1 = ev['xaxis.range[1]'] as string | undefined
          if (!r0 || !r1) return
          const cb = onRangeChangeRef.current
          if (!cb) return
          // debounce: drag-зум шлёт серию событий
          if (debounceRef.current) window.clearTimeout(debounceRef.current)
          debounceRef.current = window.setTimeout(() => cb({ t0: r0, t1: r1 }), 300)
        })

        // ── Собственный крест-курсор: тонкие пунктирные акцентные линии ──
        // Вертикаль — на момент времени (x), горизонталь — на уровне значения датчика.
        // shapes-relayout не содержит xaxis.range → обработчик relayout его
        // безопасно игнорирует (r0/r1 = undefined), programmaticRef трогать не нужно.
        const vLine = (x: unknown): PlotlyLayout => ({
          type: 'line', xref: 'x', yref: 'paper', x0: x, x1: x, y0: 0, y1: 1,
          line: { color: hoverLineColorRef.current, width: 1, dash: 'dot' }, layer: 'above',
        })
        // Троттлинг через rAF: plotly_hover сыплет сотни событий/сек — копим целевые
        // shapes + позиции DOM-оверлеев (точки/тултип/пилюля) и применяем раз в кадр.
        let pendingShapes: PlotlyLayout[] | null = null
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        let pendingHover: any = null
        let lastKey = ''
        // ── bklit-style ховер: точки-маркеры на линиях + плавающий тултип + пилюля даты ──
        const applyHoverDom = () => {
          const h = pendingHover
          const wrap = wrapRef.current
          if (!h || !wrap) return
          const W = wrap.clientWidth, H = wrap.clientHeight
          const st = tipStyleRef.current
          const pad = (n: number) => String(n).padStart(2, '0')
          const d = new Date(h.ms)
          const okd = Number.isFinite(d.getTime())
          const fnum = (v: number) => (Number.isFinite(v) ? v.toFixed(4) : '—')
          // точки-маркеры на каждой линии (пул переиспользуемых узлов, плавный glide)
          const layer = dotsRef.current
          if (layer) {
            const pool = dotPoolRef.current
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            h.rows.forEach((r: any, i: number) => {
              let dot = pool[i]
              if (!dot) {
                dot = document.createElement('div')
                dot.style.cssText = 'position:absolute;width:10px;height:10px;border-radius:50%;'
                  + 'transform:translate(-50%,-50%);box-shadow:0 0 0 2px var(--surface);pointer-events:none;'
                  + 'transition:left .09s linear, top .09s linear, opacity .15s ease;'
                layer.appendChild(dot)
                pool[i] = dot
              }
              dot.style.background = r.color
              dot.style.left = h.xpx + 'px'
              dot.style.top = r.py + 'px'
              dot.style.opacity = '1'
            })
            for (let i = h.rows.length; i < pool.length; i++) pool[i].style.opacity = '0'
          }
          // плавающий тултип: заголовок-дата + строки «точка · имя · значение»
          const tip = tipRef.current
          if (tip && h.rows.length) {
            // дату/время перенесли в пилюлю на оси X — в тултипе только значения серий
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            tip.innerHTML = h.rows.map((r: any) =>
                  `<div style="display:flex;align-items:center;gap:6px;line-height:1.55;white-space:nowrap">`
                  + `<span style="width:8px;height:8px;border-radius:50%;background:${r.color};flex-shrink:0"></span>`
                  + `<span style="color:var(--text-2)">${String(r.name).replace(/\s*\(MAE[^)]*\)/, '')}</span>`
                  + `<span style="margin-left:14px;font-weight:700;color:${st.fg}">${fnum(r.val)}</span></div>`
                ).join('')
            tip.style.background = st.bg
            tip.style.borderColor = st.border
            const tipW = tip.offsetWidth || 150, tipH = tip.offsetHeight || 56
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const topPy = Math.min(...h.rows.map((r: any) => r.py))
            let left = h.xpx + 16
            if (left + tipW > W - 4) left = h.xpx - tipW - 16     // флип влево у правого края
            if (left < 4) left = 4
            let top = topPy - tipH - 12
            if (top < 4) top = Math.min(topPy + 16, H - tipH - 4)
            tip.style.left = left + 'px'
            tip.style.top = top + 'px'
            tip.style.opacity = '1'
          }
          // пилюля с датой на оси X (по центру под точкой времени)
          const pill = pillRef.current
          if (pill) {
            pill.textContent = okd ? `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}` : ''
            pill.style.left = h.xpx + 'px'
            pill.style.top = (h.plotBottom + 5) + 'px'
            pill.style.opacity = '1'
          }
        }
        const flushHover = () => {
          hoverRafRef.current = 0
          if (pendingShapes) Plotly.relayout(el, { shapes: pendingShapes })
          applyHoverDom()
        }
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        ;(el as any).on('plotly_hover', (ev: any) => {
          const pts = ev?.points
          const x = pts?.[0]?.x
          if (x == null) return
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const yp = pts.find((p: any) => p?.data?.name === 'Значение') ?? pts[0]
          const y = yp?.y
          const key = `${x}|${y}`
          if (key === lastKey) return        // та же точка — не дёргаем кадр
          lastKey = key
          pendingShapes = [...shapesRef.current, vLine(x)]
          // пиксельный маппинг (как в region-select): data → px по диапазону осей
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const fl = (el as any)._fullLayout
          const xa = fl?.xaxis, ya = fl?.yaxis
          if (xa?.range && ya?.range) {
            const ms = typeof x === 'number' ? x : new Date(String(x).replace(' ', 'T')).getTime()
            const ax0 = new Date(xa.range[0]).getTime(), ax1 = new Date(xa.range[1]).getTime()
            const xpx = xa._offset + ((ms - ax0) / ((ax1 - ax0) || 1)) * xa._length
            const yv0 = Number(ya.range[0]), yv1 = Number(ya.range[1])
            const toPy = (v: number) => ya._offset + (1 - (v - yv0) / ((yv1 - yv0) || 1)) * ya._length
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const ya3 = (fl as any)?.yaxis3
            // эпистемика лежит на y3 (иной масштаб/диапазон) → её точку позиционируем по y3, а не по
            // основной оси. Значение берём ПРЯМО из hover-точки (p.yaxis===y3), без сопоставления ключа.
            const toPy3 = (ya3?.range) ? (v: number) => {
              const e0 = Number(ya3.range[0]), e1 = Number(ya3.range[1])
              return ya3._offset + (1 - (v - e0) / ((e1 - e0) || 1)) * ya3._length
            } : null
            const rows = pts
              // линии основной оси Y + эпистемика (y3); наложения y2 (иной масштаб) исключаем
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              .filter((p: any) => typeof p?.y === 'number' && Number.isFinite(p.y) && p?.yaxis !== fl?.yaxis2)
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              .map((p: any) => {
                const isEpi = !!(ya3 && p?.yaxis === ya3)
                return { name: p?.data?.name ?? '', val: p.y, color: p?.data?.line?.color ?? '#CDD9E5',
                         py: (isEpi && toPy3) ? toPy3(p.y) : toPy(p.y) }
              })
            // пилюля даты крепится к НИЗУ оси X: при эпистемике X якорится к y3 (нижний subplot),
            // поэтому берём низ y3, а не основной оси (иначе пилюля висит на стыке графиков).
            const _pb = (ya3?._offset != null) ? ya3._offset + ya3._length : ya._offset + ya._length
            pendingHover = { xpx, ms, rows, plotBottom: _pb }
          }
          if (!hoverRafRef.current) hoverRafRef.current = requestAnimationFrame(flushHover)
        })
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        ;(el as any).on('plotly_unhover', () => {
          if (hoverRafRef.current) { cancelAnimationFrame(hoverRafRef.current); hoverRafRef.current = 0 }
          pendingShapes = null
          pendingHover = null
          lastKey = ''
          if (tipRef.current) tipRef.current.style.opacity = '0'
          if (pillRef.current) pillRef.current.style.opacity = '0'
          for (const dot of dotPoolRef.current) dot.style.opacity = '0'
          Plotly.relayout(el, { shapes: shapesRef.current })
        })
      }
      if (shouldZoom && viewDays && times.length > 1) {
        const endMs   = _tsMs(times[times.length - 1])
        const startMs = Math.max(endMs - viewDays * 86_400_000, _tsMs(times[0]))
        programmaticRef.current = true
        Plotly.relayout(el, { 'xaxis.range': [_fmtLocal(startMs), times[times.length - 1]] })
      }
      viewDaysRef.current = viewDays
      prevSensorRef.current = sensor?.id
    })
  // No cleanup here — purge only happens when sensor changes (see effect below)
  }, [sensor?.id, traces, layout, viewDays])

  useEffect(() => {
    if (!focusTimestamp || !divRef.current || !initializedRef.current) return
    getPlotly().then(Plotly => {
      const center = new Date(focusTimestamp.replace(' ', 'T')).getTime()
      const window2d = 2 * 24 * 3600 * 1000
      programmaticRef.current = true
      Plotly.relayout(divRef.current, {
        'xaxis.range': [_fmtLocal(center - window2d), _fmtLocal(center + window2d)],
      })
    })
  }, [focusTimestamp])

  // Separate cleanup: purge only when sensor changes or component unmounts
  // This prevents expensive newPlot on every theme/filter/zoom change
  useEffect(() => {
    return () => {
      revealRef.current?.cancel(); revealRef.current = undefined
      if (fadeTimerRef.current) { window.clearTimeout(fadeTimerRef.current); fadeTimerRef.current = undefined }
      if (fadeRafRef.current) { cancelAnimationFrame(fadeRafRef.current); fadeRafRef.current = undefined }
      if (hoverRafRef.current) { cancelAnimationFrame(hoverRafRef.current); hoverRafRef.current = 0 }
      if (debounceRef.current) { window.clearTimeout(debounceRef.current); debounceRef.current = undefined }
      const el = divRef.current
      // СИНХРОННО сбрасываем флаг: следующий рендер (новый датчик) пойдёт по ветке
      // newPlot+reveal (а не react) — иначе старый график мелькает перед новым.
      initializedRef.current = false
      viewDaysRef.current = undefined
      getPlotly().then(Plotly => { if (el) Plotly.purge(el) })
    }
  }, [sensor?.id])

  // ── Выделение участка ПРАВОЙ кнопкой (2D-бокс) → onRegionSelect(t0,t1,v0,v1) ──
  // НАТИВНЫЙ capture-слушатель на обёртке: срабатывает ДО слоя перетаскивания Plotly,
  // поэтому Plotly не воспринимает правый клик как зум (иначе оставлял чёрную рамку и
  // зумил в пустоту). Drag ведём на уровне document — устойчиво при выходе курсора.
  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap) return
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const axOf = (k: string) => (divRef.current as any)?._fullLayout?.[k]
    const pxTs = (relX: number): string | null => {
      const ax = axOf('xaxis')
      if (!ax?.range) return null
      const frac = Math.max(0, Math.min(1, (relX - (ax._offset ?? 0)) / (ax._length || 1)))
      const a = new Date(ax.range[0]).getTime(), b = new Date(ax.range[1]).getTime()
      if (!Number.isFinite(a) || !Number.isFinite(b)) return null
      return _fmtLocal(a + frac * (b - a))
    }
    const pyVal = (relY: number): number | null => {
      const ax = axOf('yaxis')
      if (!ax?.range) return null
      const off = ax._offset ?? 0, len = ax._length || 1
      const frac = Math.max(0, Math.min(1, (relY - off) / len))
      const vmin = Number(ax.range[0]), vmax = Number(ax.range[1])
      if (!Number.isFinite(vmin) || !Number.isFinite(vmax)) return null
      return vmax - frac * (vmax - vmin)
    }
    // если drag активен — здесь лежит функция снятия document-слушателей; вызывается
    // в onUp и в cleanup эффекта (страховка от утечки, если mouseup не пришёл —
    // напр. кнопку отпустили вне окна или компонент размонтировался во время выделения)
    let detachDrag: (() => void) | null = null
    const suppressMenu = (ev: Event) => ev.preventDefault()
    const onDown = (e: MouseEvent) => {
      if (e.button !== 2 || !onRegionSelectRef.current) return
      e.stopPropagation(); e.preventDefault()        // Plotly не должен видеть правый клик
      const rect = wrap.getBoundingClientRect()
      const sx = e.clientX - rect.left, sy = e.clientY - rect.top
      setSelRect({ x: sx, y: sy, w: 0, h: 0 })
      const onMove = (ev: MouseEvent) => {
        const cx = ev.clientX - rect.left, cy = ev.clientY - rect.top
        setSelRect({ x: Math.min(sx, cx), y: Math.min(sy, cy), w: Math.abs(cx - sx), h: Math.abs(cy - sy) })
      }
      const onUp = (ev: MouseEvent) => {
        detach()
        setSelRect(null)
        const ex = ev.clientX - rect.left, ey = ev.clientY - rect.top
        if (Math.abs(ex - sx) < 6 && Math.abs(ey - sy) < 6) return   // клик, не выделение
        const t0 = pxTs(Math.min(sx, ex)), t1 = pxTs(Math.max(sx, ex))
        if (!t0 || !t1 || !onRegionSelectRef.current) return
        const yax = axOf('yaxis')
        const fullH = yax?._length || 0
        let v0: number | undefined, v1: number | undefined
        if (fullH && Math.abs(ey - sy) < fullH * 0.85) {
          v0 = pyVal(Math.max(sy, ey)) ?? undefined   // нижний пиксель (большой Y) → меньшее значение
          v1 = pyVal(Math.min(sy, ey)) ?? undefined   // верхний пиксель (малый Y) → большее значение
        }
        onRegionSelectRef.current(t0, t1, v0, v1)
      }
      const detach = () => {
        document.removeEventListener('mousemove', onMove, true)
        document.removeEventListener('mouseup', onUp, true)
        document.removeEventListener('contextmenu', suppressMenu, true)
        detachDrag = null
      }
      detachDrag = detach
      document.addEventListener('mousemove', onMove, true)
      document.addEventListener('mouseup', onUp, true)
      document.addEventListener('contextmenu', suppressMenu, { capture: true, once: true })
    }
    wrap.addEventListener('mousedown', onDown, true)   // capture-фаза
    return () => { wrap.removeEventListener('mousedown', onDown, true); if (detachDrag) detachDrag() }
  }, [sensor?.id, !!chartData?.series?.length])

  // ── Кастомный тулбар: режим взаимодействия → dragmode Plotly ────────────────
  useEffect(() => {
    if (!initializedRef.current) return
    const el = divRef.current
    if (!el) return
    getPlotly().then(Plotly => {
      if (divRef.current !== el) return
      // pan → панорама, draw → drag выключен (рисуем на canvas), иначе box-zoom
      Plotly.relayout(el, { dragmode: tool === 'pan' ? 'pan' : tool === 'draw' ? false : 'zoom' })
    })
  }, [tool])

  // ── Слой рисования (карандаш): свободные штрихи на canvas поверх графика ─────
  // basic-сборка Plotly не содержит инструментов рисования → собственный canvas.
  useEffect(() => {
    const cv = drawCanvasRef.current, wrap = wrapRef.current
    if (!cv || !wrap) return
    const ctx = cv.getContext('2d')
    const redraw = () => {
      if (!ctx) return
      ctx.clearRect(0, 0, cv.width, cv.height)
      ctx.strokeStyle = hoverLineColorRef.current || '#58A6FF'
      ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round'
      for (const st of strokesRef.current) {
        if (!st.length) continue
        ctx.beginPath(); ctx.moveTo(st[0].x, st[0].y)
        for (let i = 1; i < st.length; i++) ctx.lineTo(st[i].x, st[i].y)
        ctx.stroke()
      }
    }
    const resize = () => { cv.width = wrap.clientWidth; cv.height = wrap.clientHeight; redraw() }
    resize()
    const ro = new ResizeObserver(resize); ro.observe(wrap)
    ;(cv as HTMLCanvasElement & { _redraw?: () => void })._redraw = redraw
    let drawing = false
    const pt = (e: MouseEvent) => { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top } }
    const down = (e: MouseEvent) => {
      if (toolRef.current !== 'draw' || e.button !== 0) return
      drawing = true; strokesRef.current.push([pt(e)]); redraw()
    }
    const move = (e: MouseEvent) => {
      if (!drawing) return
      strokesRef.current[strokesRef.current.length - 1].push(pt(e)); redraw()
    }
    const up = () => { drawing = false }
    cv.addEventListener('mousedown', down)
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => {
      ro.disconnect()
      cv.removeEventListener('mousedown', down)
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
    // переустанавливаем при появлении графика (canvas рендерится только при наличии данных)
  }, [sensor?.id, !!chartData?.series?.length])

  // Зум ± с центром в текущем окне просмотра; сброс — к autorange; очистка рисунка.
  const zoomBy = (factor: number) => {
    const el = divRef.current as (HTMLDivElement & { _fullLayout?: Record<string, { range?: unknown[] }> }) | null
    if (!el?._fullLayout) return
    getPlotly().then(Plotly => {
      const upd: Record<string, unknown> = {}
      const xr = el._fullLayout?.xaxis?.range
      if (xr) {
        const a = new Date(xr[0] as string).getTime(), b = new Date(xr[1] as string).getTime()
        if (Number.isFinite(a) && Number.isFinite(b)) {
          const c = (a + b) / 2, half = ((b - a) / 2) * factor
          upd['xaxis.range'] = [_fmtLocal(c - half), _fmtLocal(c + half)]
        }
      }
      const yr = el._fullLayout?.yaxis?.range
      if (yr) {
        const a = Number(yr[0]), b = Number(yr[1])
        if (Number.isFinite(a) && Number.isFinite(b)) {
          const c = (a + b) / 2, half = ((b - a) / 2) * factor
          upd['yaxis.range'] = [c - half, c + half]
        }
      }
      if (Object.keys(upd).length) Plotly.relayout(el, upd)
    })
  }
  const resetZoom = () => {
    // Сброс через app-флоу (пресетный диапазон) вместо Plotly autorange:
    // relayout({autorange:true}) на scattergl падает с `_extremes`.
    onRangeChangeRef.current?.(null)
  }
  const clearDrawing = () => {
    strokesRef.current = []
    ;(drawCanvasRef.current as (HTMLCanvasElement & { _redraw?: () => void }) | null)?._redraw?.()
  }
  const tbtnStyle = (active: boolean): CSSProperties => ({
    width: 24, height: 24, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
    border: '1px solid', borderColor: active ? 'var(--accent)' : 'transparent',
    background: active ? 'var(--accent-glow)' : 'transparent',
    color: active ? 'var(--accent)' : 'var(--text-2)',
    borderRadius: 'var(--r-sm)', cursor: 'pointer', fontSize: 13, lineHeight: 1, padding: 0,
    transition: 'color .12s, border-color .12s, background .12s',
  })
  const tbDivStyle: CSSProperties = { width: 1, height: 16, background: 'var(--line-2)', margin: '0 1px', flexShrink: 0 }

  if (!sensor) {
    return (
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', color: 'var(--text-3)' }}>
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ opacity: 0.45 }}>
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
        </svg>
        <span style={{ fontFamily: 'Inter, monospace', fontSize: 'var(--fs-sm)', opacity: 0.65, marginTop: 8 }}>
          Нажмите на датчик или ячейку тепловой карты
        </span>
      </div>
    )
  }

  // chartData может прийти от ПРЕДЫДУЩЕГО датчика (keepPreviousData при переключении) →
  // matched=false. Чужой график не строим (гейт в traces выше), а показываем скелетон
  // ОВЕРЛЕЕМ; div графика остаётся смонтирован → Plotly.purge отрабатывает без утечки.
  const matched = !!chartData && chartData.sensor_id === sensor.id
  const hasData = matched && !!chartData?.series.length
  const showLoader = loading || (!!chartData && !matched)
  // self-conformal датчик (univariate_band): один коридор «Норма-диапазон», тумблер conf/hyb не нужен
  const corridorIsSelf = chartData?.corridor_mode === 'self'

  return (
    <div
      ref={wrapRef}
      style={{ position: 'relative', flex: 1, minHeight: 0, width: '100%', display: 'flex', flexDirection: 'column', userSelect: 'none' }}
      onContextMenu={e => { if (onRegionSelectRef.current) e.preventDefault() }}
    >
      <div
        ref={divRef}
        role="img"
        aria-label={`График датчика ${sensor.name ?? sensor.id}: значение и прогноз модели${chartData?.series?.length ? `, ${chartData.series.length} точек` : ''}`}
        style={{ flex: 1, minHeight: 0, width: '100%' }}
      />
      {/* Эпистемика теперь — нижний subplot В ТОЙ ЖЕ фигуре Plotly (общий X → синхронный
          зум/пан/hover-крестик). Отдельный ChartBrush и EpistemicStrip удалены. */}
      {/* Слой свободного рисования (карандаш) — поверх графика, активен только в режиме draw */}
      <canvas
        ref={drawCanvasRef}
        style={{
          // canvas — replaced-элемент: inset:0 его НЕ растягивает, нужны явные 100%
          position: 'absolute', top: 0, left: 0, width: '100%', height: '100%',
          zIndex: tool === 'draw' ? 6 : 3,
          pointerEvents: tool === 'draw' ? 'auto' : 'none',
          cursor: tool === 'draw' ? 'crosshair' : 'default',
        }}
      />
      {/* Кастомный тулбар (вместо modebar Plotly) */}
      <div style={{
        position: 'absolute', top: 40, right: 8, zIndex: 7,
        display: 'flex', alignItems: 'center', gap: 3, padding: 3,
        borderRadius: 'var(--r-sm)', background: theme === 'dark' ? 'rgba(13,20,38,0.85)' : 'rgba(255,255,255,0.92)',
        border: '1px solid var(--line)',
      }}>
        {([
          ['pointer', '↖', 'Указатель (выделение / бокс-зум)'],
          ['pan', '✋', 'Захват (панорама)'],
          ['draw', '✎', 'Карандаш (рисование поверх графика)'],
        ] as const).map(([t, ic, title]) => (
          <button key={t} title={title} aria-label={title} aria-pressed={tool === t} onClick={() => setTool(t)} style={tbtnStyle(tool === t)}
            onMouseEnter={e => { if (tool !== t) (e.currentTarget as HTMLElement).style.color = 'var(--accent)' }}
            onMouseLeave={e => { if (tool !== t) (e.currentTarget as HTMLElement).style.color = 'var(--text-2)' }}
          >{ic}</button>
        ))}
        <span style={tbDivStyle} />
        <button title="Увеличить" aria-label="Увеличить" onClick={() => zoomBy(0.7)} style={tbtnStyle(false)}>+</button>
        <button title="Уменьшить" aria-label="Уменьшить" onClick={() => zoomBy(1.4)} style={tbtnStyle(false)}>−</button>
        <button title="Сбросить масштаб" aria-label="Сбросить масштаб" onClick={resetZoom} style={tbtnStyle(false)}>⤢</button>
        <span style={tbDivStyle} />
        {/* Кнопки conf/hyb — только для кросс-сенсорных коридоров; для self (univariate_band)
            коридор один («Норма-диапазон»), тумблер не показываем. */}
        {!corridorIsSelf && (<>
          <button
            title="Conformal — строгий коридор (гаснет на OOD/новизне). Клик — показать/скрыть."
            aria-label="Показать conformal интервал" aria-pressed={showConformal}
            onClick={() => setShowConformal(v => !v)}
            style={{ ...tbtnStyle(showConformal), width: 'auto', padding: '0 8px', fontSize: 10, fontWeight: 600, color: showConformal ? '#58A6FF' : 'var(--text-2)', borderColor: showConformal ? '#58A6FF' : 'transparent' }}
          >conf</button>
          <button
            title="Hybrid — σ-масштабированный коридор (присутствует всегда, честно шире в неуверенности). Клик — показать/скрыть."
            aria-label="Показать hybrid интервал" aria-pressed={showHybrid}
            onClick={() => setShowHybrid(v => !v)}
            style={{ ...tbtnStyle(showHybrid), width: 'auto', padding: '0 8px', fontSize: 10, fontWeight: 600, color: showHybrid ? '#E0A23A' : 'var(--text-2)', borderColor: showHybrid ? '#E0A23A' : 'transparent' }}
          >hyb</button>
          <span style={tbDivStyle} />
        </>)}
        <button title="Очистить рисунок" aria-label="Очистить рисунок" onClick={clearDrawing} style={tbtnStyle(false)}>⌫</button>
      </div>
      {/* ── bklit-style ховер ─────────────────────────────────────────────── */}
      {/* Точки-маркеры на линиях (узлы создаются динамически в plotly_hover) */}
      <div ref={dotsRef} aria-hidden="true" style={{ position: 'absolute', inset: 0, zIndex: 5, pointerEvents: 'none' }} />
      {/* Пилюля с датой на оси X */}
      <div
        ref={pillRef}
        aria-hidden="true"
        className="font-mono"
        style={{
          position: 'absolute', zIndex: 6, opacity: 0, pointerEvents: 'none',
          transform: 'translateX(-50%)', transition: 'left .09s linear, opacity .15s ease',
          padding: '2px 9px', borderRadius: 999, fontSize: 11, whiteSpace: 'nowrap',
          background: 'var(--text-1)', color: 'var(--bg)', fontWeight: 700,
          boxShadow: 'var(--shadow-md)',
        }}
      />
      {/* Плавающий тултип у курсора (заголовок-дата + строки серий), плавно следует */}
      <div
        ref={tipRef}
        aria-hidden="true"
        className="font-mono"
        style={{
          position: 'absolute', zIndex: 6, opacity: 0, pointerEvents: 'none',
          transition: 'left .1s ease, top .1s ease, opacity .15s ease',
          padding: '7px 10px', borderRadius: 'var(--r-sm)', fontSize: 11.5, minWidth: 132,
          border: '1px solid var(--line-2)', boxShadow: 'var(--shadow-md)',
          background: theme === 'dark' ? 'rgba(13,20,38,0.94)' : 'rgba(255,255,255,0.97)',
        }}
      />
      {selRect && (selRect.w > 1 || selRect.h > 1) && (
        <div style={{
          position: 'absolute', left: selRect.x, top: selRect.y, width: selRect.w, height: selRect.h,
          background: 'rgba(88,166,255,0.15)', border: '1px solid var(--accent)', borderRadius: 2,
          pointerEvents: 'none', zIndex: 4,
        }} />
      )}
      {/* Загрузка / переключение датчика / ошибка — ПОВЕРХ (div графика остаётся
          смонтирован, чтобы Plotly.purge работал корректно, без утечки инстанса). */}
      {!hasData && (
        <div
          role="status"
          aria-live="polite"
          style={{
            position: 'absolute', inset: 0, zIndex: 9, display: 'flex',
            alignItems: 'center', justifyContent: 'center',
            padding: 'var(--space-3) var(--space-2)', background: 'var(--bg)',
          }}
        >
          {showLoader ? (
            <ChartSkeleton />
          ) : error ? (
            <span style={{ fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)', color: 'var(--crit)' }}>⚠ Не удалось загрузить график (сеть/БД). Повтор выполняется автоматически.</span>
          ) : (
            <span style={{ fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)', color: 'var(--text-3)' }}>Нет данных для отображения</span>
          )}
        </div>
      )}
    </div>
  )
})
