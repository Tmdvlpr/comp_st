import { useEffect, useRef, useMemo, useState } from 'react'
// rangeslider управляется из ComparePanel (showRangeslider), по умолчанию выкл
import type { MultiSeriesItem } from '../../types'
import { ruSensor } from '../../lib/sensorLabels'
import { ChartSkeleton } from './ChartSkeleton'
import { revealChart } from '../../lib/chartMotion'
import type { RevealHandle } from '../../lib/chartMotion'
import { useBklitHover } from './useBklitHover'
import { ChartLegend } from './ChartLegend'
import type { LegendItem } from './ChartLegend'

interface MultiSensorChartProps {
  data: MultiSeriesItem[]
  loading: boolean
  theme: 'dark' | 'light'
  /** true → нормировка [0,1] (форма сопоставима при разных единицах);
   *  false → реальные единицы (мультиось для 2–3 серий) */
  normalized: boolean
  /** Полоса навигации (rangeslider) под графиком. Выкл по умолчанию: вторая
   *  мини-копия всех рядов удваивает стоимость перерисовки. */
  showRangeslider?: boolean
}

type PlotlyData = Record<string, unknown>
type PlotlyLayout = Record<string, unknown>
type PlotlyConfig = Record<string, unknown>
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type PlotlyModule = any

let plotlyPromise: Promise<PlotlyModule> | null = null
function getPlotly(): Promise<PlotlyModule> {
  if (!plotlyPromise) {
    // @ts-expect-error plotly.js-basic-dist-min has no bundled TS declarations
    plotlyPromise = import('plotly.js-basic-dist-min').then(m => m.default ?? m)
  }
  return plotlyPromise!
}

// Устойчивая палитра по индексу серии.
// Первые 3 разнесены по тону И яркости (синий → красный → зелёный),
// чтобы не сливались между собой и не путались с соседними рядами.
const PALETTE = ['#58A6FF', '#FF4560', '#3FB950', '#FACC15', '#B48DFF', '#F97316', '#22D3EE', '#EC4899']
const AXIS_SIDES = ['left', 'right', 'right'] as const

export function MultiSensorChart({ data, loading, theme, normalized, showRangeslider = false }: MultiSensorChartProps) {
  // интерактивная легенда: активная серия — от наведения на запись ИЛИ на линию графика
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const hover = useBklitHover(theme, { onActive: setActiveKey })   // ховер + подсветка легенды
  const divRef = useRef<HTMLDivElement>(null)
  const initializedRef = useRef(false)
  const rafRef = useRef(0)
  // bklit-style «построение» графика + повтор при смене набора серий (revealSignature)
  const revealRef = useRef<RevealHandle | undefined>(undefined)
  const seriesSigRef = useRef('')
  // сигнатура набора: список датчиков + режим нормировки (смена режима = «перестроение»)
  const seriesSig = data.map(d => d.sensor_id).join(',') + '|' + normalized
  // цвет вертикального креста-курсора (рисуем сами на plotly_hover)
  const hoverLineColorRef = useRef('')
  hoverLineColorRef.current = theme === 'dark' ? 'rgba(88,166,255,0.85)' : 'rgba(40,90,160,0.8)'

  const { traces, layout } = useMemo(() => {
    const gridColor = theme === 'dark' ? '#2C2C32' : '#D0D4D4'
    // Цвет осей/легенды — контрастнее прежнего (тусклый серый плохо читался)
    const fontColor = theme === 'dark' ? '#B7C0D6' : '#3C486A'

    // Мультиось только для ≤3 серий в режиме реальных единиц
    const multiAxis = !normalized && data.length >= 2 && data.length <= 3

    const traces: PlotlyData[] = data.map((s, i) => {
      const color = PALETTE[i % PALETTE.length]
      const x = s.series.map(p => p.t)
      const vReal = s.series.map(p => p.v)
      let y: number[] = vReal
      if (normalized) {
        // границы — одним проходом по конечным значениям (без Math.min(...spread):
        // спред больших массивов рискует RangeError и лишними проходами; NaN ломал
        // нормировку целиком — теперь нечисловые значения пропускаются).
        let lo: number, hi: number
        if (s.range_min != null && s.range_max != null) {
          lo = s.range_min; hi = s.range_max
        } else {
          lo = Infinity; hi = -Infinity
          for (const v of vReal) {
            if (typeof v === 'number' && Number.isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v }
          }
          if (s.range_min != null) lo = s.range_min
          if (s.range_max != null) hi = s.range_max
          if (!Number.isFinite(lo)) lo = 0
          if (!Number.isFinite(hi)) hi = 1
        }
        const span = hi > lo ? hi - lo : 1
        y = vReal.map(v => (typeof v === 'number' && Number.isFinite(v) ? (v - lo) / span : v))
      }
      const label = `ГПА-${s.gpa.replace('GPA', '')} · ${ruSensor(s.name)}`
      const trace: PlotlyData = {
        x, y, name: label, type: 'scatter', mode: 'lines',
        line: { color, width: 1.6 },
        customdata: vReal,
        // плавающий бокс Plotly не рисуем — значения показываем в угловом ридауте
        hoverinfo: 'none',
      }
      if (multiAxis && i > 0) trace.yaxis = `y${i + 1}`
      return trace
    })

    const layout: PlotlyLayout = {
      paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
      font: { family: "'Inter', monospace", size: 11, color: fontColor },
      xaxis: {
        gridcolor: gridColor, linecolor: gridColor, tickfont: { size: 11 },
        tickformat: '%d.%m %H:%M', hoverformat: '%d.%m.%Y %H:%M',
        showgrid: true, zeroline: false, showspikes: false,
        rangeslider: { visible: showRangeslider, thickness: 0.08 },
      },
      showlegend: false,   // нативную легенду прячем — рисуем свою интерактивную (ChartLegend)
      margin: { t: 30, r: multiAxis ? 56 : 8, b: 28, l: 48 },
      hovermode: 'x unified',
      hoverdistance: -1,
      dragmode: 'zoom',
      // theme и showRangeslider включены, чтобы Plotly сбрасывал UI при их смене
      uirevision: `${normalized}-${theme}-${showRangeslider}-${data.map(d => d.sensor_id).join(',')}`,
    }

    if (normalized) {
      layout.yaxis = {
        gridcolor: gridColor, linecolor: gridColor, tickfont: { size: 11 },
        showgrid: true, zeroline: false, title: { text: 'нормировано', font: { size: 10 } },
        // без жёсткого range: иначе зум/выделение по вертикали (значению) сбрасывается
        // на каждой перерисовке; autorange даёт ~0–1, а box-zoom по Y теперь работает.
        fixedrange: false, autorange: true, showspikes: false,
      }
    } else if (multiAxis) {
      data.forEach((_s, i) => {
        const key = i === 0 ? 'yaxis' : `yaxis${i + 1}`
        const ax: PlotlyLayout = {
          tickfont: { size: 10, color: PALETTE[i % PALETTE.length] },
          side: AXIS_SIDES[i], showgrid: i === 0, gridcolor: gridColor, zeroline: false, showspikes: false,
        }
        if (i > 0) { ax.overlaying = 'y'; ax.anchor = 'free'; ax.position = i === 1 ? 1 : 0.94 }
        layout[key] = ax
      })
    } else {
      layout.yaxis = {
        gridcolor: gridColor, linecolor: gridColor, tickfont: { size: 11 },
        showgrid: true, zeroline: false, showspikes: false,
      }
    }

    return { traces, layout }
  }, [data, theme, normalized, showRangeslider])

  useEffect(() => {
    const el = divRef.current
    if (!el) return
    const config: PlotlyConfig = {
      displayModeBar: 'hover', responsive: true,
      // зум колёсиком с центром в точке курсора (как на основном графике)
      scrollZoom: true,
      modeBarButtonsToRemove: ['lasso2d', 'select2d', 'toImage'],
    }
    getPlotly().then(Plotly => {
      if (!el) return
      if (!traces.length) {
        // нет данных → чистим холст (иначе под надписью «Выберите датчики» оставался
        // старый график от предыдущего выбора)
        if (initializedRef.current) {
          if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
          try { (el as PlotlyModule).removeAllListeners?.('plotly_hover'); (el as PlotlyModule).removeAllListeners?.('plotly_unhover') } catch { /* noop */ }
          Plotly.purge(el); initializedRef.current = false
        }
        return
      }
      if (initializedRef.current) {
        Plotly.react(el, traces, layout, config)
        // повтор «построения» при смене набора серий / режима нормировки
        if (seriesSigRef.current !== seriesSig) {
          seriesSigRef.current = seriesSig
          revealRef.current?.cancel()
          revealRef.current = revealChart(el)
        }
        return
      }

      Plotly.newPlot(el, traces, layout, config)
      initializedRef.current = true
      // bklit-style «построение» графика при первом рендере набора
      seriesSigRef.current = seriesSig
      revealRef.current?.cancel()
      revealRef.current = revealChart(el)

      // ── Вертикальный крест-курсор + угловой ридаут (без перекрытия графика) ──
      const vLine = (x: unknown): PlotlyLayout => ({
        type: 'line', xref: 'x', yref: 'paper', x0: x, x1: x, y0: 0, y1: 1,
        line: { color: hoverLineColorRef.current, width: 1, dash: 'dot' }, layer: 'above',
      })
      // Троттлинг через rAF: при быстром движении plotly_hover сыплет события — копим
      // целевые shapes и применяем максимум раз в кадр, чтобы крест не «прыгал».
      let pendingShapes: PlotlyLayout[] | null = null
      let lastKey = ''
      const flushHover = () => {
        rafRef.current = 0
        if (pendingShapes && initializedRef.current) {
          try { Plotly.relayout(el, { shapes: pendingShapes }) } catch { /* график мог быть очищен до кадра */ }
        }
      }
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ;(el as any).on('plotly_hover', (ev: any) => {
        const pts = ev?.points
        const x = pts?.[0]?.x
        if (x == null) return
        const key = String(x)
        if (key === lastKey) return
        lastKey = key
        pendingShapes = [vLine(x)]
        hover.onHover(el, ev)   // точки-маркеры + плавающий тултип + пилюля
        if (!rafRef.current) rafRef.current = requestAnimationFrame(flushHover)
      })
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ;(el as any).on('plotly_unhover', () => {
        if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
        pendingShapes = null
        lastKey = ''
        hover.hide()
        // Guard: el may already be purged if this fires after unmount
        if (initializedRef.current) {
          try { Plotly.relayout(el, { shapes: [] }) } catch { /* noop */ }
        }
      })
    })
    // Cleanup: only cancel the pending hover frame. Do NOT remove the plotly_hover/
    // plotly_unhover listeners here — on a [traces,layout] update the effect body takes
    // the Plotly.react() path (no re-attach), so removing them would permanently kill the
    // hover crosshair + readout. Listeners are dropped only on the no-data purge (above)
    // and on unmount (the separate [] effect).
    return () => {
      if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
    }
  }, [traces, layout, seriesSig])

  useEffect(() => {
    return () => {
      const el = divRef.current
      revealRef.current?.cancel(); revealRef.current = undefined
      if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
      getPlotly().then(Plotly => {
        if (el) {
          try { (el as PlotlyModule).removeAllListeners?.('plotly_hover'); (el as PlotlyModule).removeAllListeners?.('plotly_unhover') } catch { /* noop */ }
          Plotly.purge(el); initializedRef.current = false
        }
      })
    }
  }, [])

  // ── Интерактивная легенда (наведение на запись ↔ на линию) ───────────────────
  const legendItems: LegendItem[] = data.map((s, i) => {
    const label = `ГПА-${s.gpa.replace('GPA', '')} · ${ruSensor(s.name)}`
    return { key: label, name: label, color: PALETTE[i % PALETTE.length] }
  })
  // наведение на запись легенды → акцент линии: приглушаем прочие через opacity
  const dimOthers = (activeName: string | null) => {
    const el = divRef.current
    if (!el || !initializedRef.current) return
    getPlotly().then(Plotly => {
      if (divRef.current !== el) return
      const op = ((el as PlotlyModule).data ?? []).map((t: PlotlyData) =>
        activeName == null || t.name === activeName ? 1 : 0.16)
      try { Plotly.restyle(el, { opacity: op }) } catch { /* график мог быть очищен */ }
    })
  }
  const onLegendHover = (key: string) => { setActiveKey(key); dimOthers(key) }
  const onLegendLeave = () => { setActiveKey(null); dimOthers(null) }

  // div графика смонтирован ВСЕГДА (стабильный ref — без багов перемонтирования Plotly);
  // подсказка/загрузка — оверлеем поверх пустого холста.
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, width: '100%' }}>
      {data.length > 0 && (
        <ChartLegend items={legendItems} activeKey={activeKey} onHover={onLegendHover} onLeave={onLegendLeave} />
      )}
      <div ref={hover.wrapRef} style={{ position: 'relative', flex: 1, minHeight: 0, width: '100%', display: 'flex' }}>
        <div ref={divRef} style={{ flex: 1, minHeight: 0, width: '100%' }} />
        {hover.overlays}
        {!data.length && (
          loading ? (
            // Загрузка → скелетон-заглушка формы графика (поверх пустого холста)
            <div style={{ position: 'absolute', inset: 0, padding: 'var(--space-3) var(--space-2)', display: 'flex' }}>
              <ChartSkeleton label="Загрузка…" />
            </div>
          ) : (
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
              <span style={{ fontFamily: 'Inter, monospace', fontSize: 'var(--fs-sm)', color: 'var(--text-3)' }}>
                Выберите датчики для сравнения (можно с разных ГПА)
              </span>
            </div>
          )
        )}
      </div>
    </div>
  )
}
