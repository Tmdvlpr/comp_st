import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useQuery, useQueries, keepPreviousData, useQueryClient } from '@tanstack/react-query'
import { animate } from 'animejs'
import { prefersReducedMotion } from './lib/motion'
import { Ticker } from './components/Ticker/Ticker'
import { Sidebar } from './components/Sidebar/Sidebar'
import { StatsGrid } from './components/Stats/StatsGrid'
import { HeatMap } from './components/HeatMap/HeatMap'
import { SensorChart } from './components/Chart/SensorChart'
import type { GpaOverlay } from './components/Chart/SensorChart'
import { ComparePanel } from './components/Chart/ComparePanel'
import { EventDrawer } from './components/EventDrawer/EventDrawer'
import { KioskMode } from './components/Kiosk/KioskMode'
import { StationSwitcher } from './components/StationSwitcher/StationSwitcher'
import { SchemaPanel } from './components/Schema/SchemaPanel'
import { EngineView } from './components/Engine/EngineView'
import { DetailPanel } from './components/Detail/DetailPanel'
import { ContributingFeatures } from './components/Chart/ContributingFeatures'
import { caseKey } from './lib/caseBase'
import { ruSensor } from './lib/sensorLabels'
import { useTheme, toggleTheme } from './lib/themeStore'
import { fmtStation } from './lib/time'
import { DatePicker } from './components/DatePicker/DatePicker'
import { Freshness } from './components/Freshness/Freshness'
import { PriorityBanner } from './components/PriorityBanner/PriorityBanner'
import { ShiftReport } from './components/Report/ShiftReport'
import { ApiErrorBanner } from './components/ApiErrorBanner/ApiErrorBanner'
import { api } from './api/client'
import type { EventItem } from './types'
import { KIND_LABEL, SEV_COLOR, SEV_LABEL } from './types'
import './styles/globals.css'

const REFETCH_MS = 30_000
const SCHEMA_NOTICE_TTL_MS = 4_200
const STATIONS_REFETCH_MS = 60_000
const ZOOM_SNAP_GRID_MS = 300_000
const EVENT_SELECT_DAYS = 3

// Module-level stable style constants (no new object on each render)
const styleRootMonitor = {
  display: 'grid',
  gridTemplateRows: '36px 1fr',
  height: '100vh',
  background: 'transparent',
  color: 'var(--text)',
} as const

const styleSidebarWrapper = {
  gridColumn: 1,
  gridRow: 2,
  display: 'grid',
  minHeight: 0,
  overflow: 'hidden',
} as const

const styleTickerWrapper = {
  gridColumn: '1 / -1',
  gridRow: 1,
  minWidth: 0,
  overflow: 'hidden',
} as const

const styleTickerLeft = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 14,
  fontFamily: 'Inter, monospace',
  fontSize: 'var(--fs-xs)',
  color: 'var(--text-3)',
  paddingLeft: 'var(--space-3)',
} as const

const styleTickerRightWrap = {
  display: 'flex',
  alignItems: 'center',
  gap: 4,
  paddingRight: 14,
} as const

const styleSensorCountBadge = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 5,
} as const

const styleHeatmapSection = {
  background: 'var(--surface)',
  padding: '12px 14px',
  display: 'flex',
  flexDirection: 'column' as const,
  minHeight: 0,
  overflow: 'hidden',
}

const styleHeatmapHeader = {
  display: 'flex',
  alignItems: 'center',
  marginBottom: 8,
  flexShrink: 0,
} as const

const styleHeatmapTitle = {
  fontFamily: 'Inter, sans-serif',
  fontSize: 'var(--fs-xs)',
  fontWeight: 600,
  letterSpacing: '0.10em',
  textTransform: 'uppercase' as const,
  color: 'var(--text-2)',
}

const styleHeatmapCount = {
  fontFamily: 'Inter, monospace',
  fontSize: 'var(--fs-xs)',
  color: 'var(--text-3)',
  background: 'var(--surface-2)',
  padding: '2px 6px',
  borderRadius: 0,
  marginLeft: 8,
} as const

const styleMainPanel = {
  overflow: 'hidden',
  minHeight: 0,
  display: 'flex',
  flexDirection: 'column' as const,
  padding: '12px 20px',
  gap: 12,
}

const styleDroppedChipsRow = {
  display: 'flex',
  flexWrap: 'wrap' as const,
  gap: 6,
  marginBottom: 6,
  flexShrink: 0,
}

const styleDivider = {
  width: 1,
  height: 12,
  background: 'var(--line-2)',
  margin: '0 4px',
  flexShrink: 0,
} as const

const styleSkipLink = {
  position: 'absolute' as const, top: -40, left: 0, zIndex: 9999,
  padding: '8px 16px', background: 'var(--accent-strong)', color: 'var(--on-accent)',
  fontFamily: 'var(--font-display)', fontSize: 'var(--fs-sm)', fontWeight: 600,
  textDecoration: 'none', borderRadius: 'var(--r-sm)',
  transition: 'top 0.1s',
} as const

const styleVisuallyHidden = {
  position: 'absolute' as const, width: 1, height: 1, padding: 0,
  margin: -1, overflow: 'hidden' as const, clip: 'rect(0,0,0,0)',
  whiteSpace: 'nowrap' as const, borderWidth: 0,
} as const

const styleKindFilterBanner = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
  padding: '6px 12px', background: 'var(--accent-glow)',
  border: '1px solid var(--accent)', borderRadius: 'var(--r-md)',
  color: 'var(--accent)', fontSize: 'var(--fs-xs)', fontFamily: 'Inter, monospace', fontWeight: 600,
} as const

const styleKindFilterClearBtn = {
  background: 'none', border: '1px solid var(--accent)', color: 'var(--accent)',
  padding: '2px 8px', borderRadius: 'var(--r-sm)', fontSize: 'var(--fs-xs)', cursor: 'pointer', fontFamily: 'inherit',
} as const

const styleBottomGrid = {
  flex: 1, display: 'grid',
  gridTemplateColumns: 'clamp(280px, 28vw, 352px) 1fr',
  gap: 12, minHeight: 0,
} as const

const styleRightPanel = {
  gridColumn: 3,
  gridRow: 2,
  minHeight: 0,
  overflow: 'hidden',
} as const

const styleBadgeValue = {
  color: 'var(--text-2)',
} as const

const styleSep = {
  width: 1, height: 16, background: 'var(--line-2)', flexShrink: 0, margin: '0 3px',
} as const

const styleDropOverlay = {
  position: 'absolute' as const, inset: 0, zIndex: 5,
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  background: 'var(--accent-glow)', pointerEvents: 'none' as const,
  fontFamily: 'Inter, monospace', fontSize: 'var(--fs-sm)',
  color: 'var(--accent)', fontWeight: 700, letterSpacing: '0.04em',
} as const

const styleCompareFloatBtn = {
  position: 'absolute' as const, top: 12, right: 14, zIndex: 4,
  padding: '4px 12px', fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)',
  border: '1px solid var(--line)', background: 'var(--surface-2)', color: 'var(--text-2)',
  cursor: 'pointer', borderRadius: 'var(--r-sm)',
} as const

const styleSensorHeaderRow = {
  display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between',
  gap: 'var(--space-4)', marginBottom: 'var(--space-2)', flexShrink: 0,
} as const

const styleSensorToolbar = {
  display: 'flex', alignItems: 'center', flexWrap: 'wrap' as const,
  justifyContent: 'flex-end', columnGap: 'var(--space-1)', rowGap: 4,
  fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)', color: 'var(--text-3)',
} as const

const styleClearRangeBtn = {
  background: 'none', border: 'none', color: 'var(--text-3)',
  cursor: 'pointer', fontFamily: 'inherit', fontSize: 'inherit',
  padding: '0 2px', lineHeight: 1,
} as const

const styleFeatBtn = {
  height: 26, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
  padding: '0 9px', fontFamily: 'inherit', fontSize: 'inherit',
  borderRadius: 'var(--r-sm)', whiteSpace: 'nowrap' as const, flexShrink: 0,
  border: '1px solid var(--accent)', background: 'var(--accent-glow)',
  color: 'var(--accent)', cursor: 'pointer', fontWeight: 700,
} as const

const styleCompareBtn = {
  height: 26, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
  padding: '0 9px', fontFamily: 'inherit', fontSize: 'inherit',
  borderRadius: 'var(--r-sm)', whiteSpace: 'nowrap' as const, flexShrink: 0,
  border: '1px solid var(--line)', background: 'transparent',
  color: 'var(--text-3)', cursor: 'pointer',
} as const

const styleFocusCloseBtn = {
  background: 'none', border: 'none', color: 'var(--text-3)',
  cursor: 'pointer', fontSize: 14, padding: '0 4px', lineHeight: 1,
} as const

const styleDroppedChip = {
  display: 'inline-flex', alignItems: 'center', gap: 6,
  padding: '2px 6px 2px 8px', fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)',
  color: 'var(--text-2)', background: 'var(--surface-2)',
  border: '1px dashed var(--line-2)', borderRadius: 'var(--r-sm)',
} as const

const styleDroppedChipRemoveBtn = {
  background: 'none', border: 'none', color: 'var(--text-3)',
  cursor: 'pointer', fontSize: 13, lineHeight: 1, padding: 0,
  transition: 'color var(--dur-fast) var(--ease-standard)',
} as const

const styleSchemaNotice = {
  position: 'fixed' as const, top: 48, left: '50%', transform: 'translateX(-50%)',
  zIndex: 80, maxWidth: 480, padding: '10px 16px',
  display: 'flex', alignItems: 'center', gap: 10,
  background: 'var(--surface-3)', border: '1px solid var(--warn)',
  borderRadius: 'var(--r-md)', color: 'var(--text-1)', fontSize: 13,
  boxShadow: 'var(--shadow-md)', backdropFilter: 'blur(6px)',
} as const

const styleNoticeCloseBtn = {
  background: 'none', border: 'none', color: 'var(--text-3)',
  cursor: 'pointer', fontSize: 14, padding: '0 2px', lineHeight: 1,
  flexShrink: 0, marginLeft: 'auto',
} as const

// Чтение query-части хэша (#/s/<station>?sensor=...&view=...): состояние датчика/вида
// в URL переживает F5 и шарится ссылкой. Станцию держит Root (main.tsx) в пути хэша.
function readHashParam(key: string): string | null {
  const q = typeof location !== 'undefined' ? location.hash.split('?')[1] : ''
  return q ? new URLSearchParams(q).get(key) : null
}

export default function App({ initialStation, onBackToOverview }: { initialStation?: string; onBackToOverview?: () => void } = {}) {
  const theme = useTheme()   // единый стор темы (общий с лендингом и движком)
  const qc = useQueryClient()
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem('cs-sidebar') === '1'
  )
  const [selectedId, setSelectedId] = useState<string | null>(() => readHashParam('sensor'))
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [kioskActive, setKioskActive] = useState(false)
  const [ackedIds, setAckedIds] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(localStorage.getItem('cs-acked') ?? '[]')) }
    catch { return new Set() }
  })
  const [activeStation, setActiveStation] = useState(() =>
    initialStation ?? localStorage.getItem('cs-station') ?? 'ohangaron'
  )
  const [kindFilter, setKindFilter] = useState<string | null>(null)
  const [activeView, setActiveView] = useState<'monitor' | 'schema'>(() => (readHashParam('view') === 'schema' ? 'schema' : 'monitor'))
  // chartFrom/chartTo объявлены здесь (до useEffect, который их читает в dep-массиве)
  const [chartFrom, setChartFrom] = useState(() => readHashParam('from') ?? '')
  const [chartTo, setChartTo] = useState(() => readHashParam('to') ?? '')
  const [chartDays, setChartDays] = useState(() => Number(readHashParam('days')) || 30)
  const [engineOpen, setEngineOpen] = useState(false)   // полноэкранный режим «Двигатель»
  const [featOpen, setFeatOpen] = useState(false)        // модалка «Важные признаки»
  const [reportOpen, setReportOpen] = useState(false)
  const [gpaOverlay, setGpaOverlay] = useState(false)
  const [compareOpen, setCompareOpen] = useState(false)
  // датчики, перетащенные (drag-drop) на текущий график как доп. ряды
  const [droppedIds, setDroppedIds] = useState<string[]>([])
  const [dragOver, setDragOver] = useState(false)
  // выделенный правой кнопкой участок графика → region SHAP (топ-5 вкладчиков).
  // v0/v1 — опц. диапазон значений (вертикальное выделение 2D-боксом).
  const [regionSel, setRegionSel] = useState<{ t0: string; t1: string; v0?: number; v1?: number } | null>(null)
  // Тост при клике «Открыть график» по точке без обучаемой модели (САУиР и т.п.):
  // тег не совпадает ни с одним датчиком дашборда → раньше клик молчал.
  const [schemaNotice, setSchemaNotice] = useState<string | null>(null)
  // Часы вынесены в изолированный leaf-компонент <Clock/> (см. низ файла): тик раз в
  // 10с больше НЕ ре-рендерит всё дерево App (графики/тепловую карту/тулбары).

  // Persist (тема — в themeStore: он сам пишет cs-theme и применяет .light к <html>)
  useEffect(() => { localStorage.setItem('cs-sidebar', sidebarCollapsed ? '1' : '0') }, [sidebarCollapsed])
  useEffect(() => {
    try { localStorage.setItem('cs-acked', JSON.stringify([...ackedIds])) } catch { /* localStorage может быть недоступен */ }
  }, [ackedIds])
  // Кросс-вкладочная синхронизация квитирований: storage-событие приходит из ДРУГИХ
  // вкладок/окон оператора → подхватываем их ack, не давая расходиться состоянию.
  // (Серверная синхронизация между РАЗНЫМИ машинами требует маппинга events→notification-id
  //  на бэке — отдельная задача; здесь закрываем рассинхрон в пределах браузера.)
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== 'cs-acked' || e.newValue == null) return
      try { setAckedIds(new Set(JSON.parse(e.newValue))) } catch { /* битый JSON */ }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])
  useEffect(() => { localStorage.setItem('cs-station', activeStation) }, [activeStation])
  // Зеркалим выбранный датчик/вид/диапазон в query хэша (replaceState — без спама
  // историей и без лишних hashchange). Путь со станцией формирует Root; здесь только query.
  useEffect(() => {
    const base = location.hash.split('?')[0] || `#/s/${activeStation}`
    const params = new URLSearchParams()
    if (selectedId) params.set('sensor', selectedId)
    if (activeView !== 'monitor') params.set('view', activeView)
    if (chartFrom) params.set('from', chartFrom)
    if (chartTo) params.set('to', chartTo)
    if (chartDays !== 30) params.set('days', String(chartDays))   // persist non-default
    const qs = params.toString()
    const target = qs ? `${base}?${qs}` : base
    if (location.hash !== target) history.replaceState(null, '', target)
  }, [selectedId, activeView, activeStation, chartFrom, chartTo, chartDays])

  // Queries
  const { data: stations = [] } = useQuery({
    queryKey: ['stations'],
    queryFn: () => api.stations(),
    refetchInterval: STATIONS_REFETCH_MS,
    staleTime: 30_000,
  })
  const { data: sensors = [] } = useQuery({
    queryKey: ['sensors', activeStation],
    queryFn: () => api.sensors(activeStation),
    refetchInterval: REFETCH_MS,
    staleTime: 20_000,
  })
  // Мост «Схема КС → Мониторинг»: клик по датчику/красному бейджу на схеме шлёт его tag →
  // находим датчик дашборда по tag и открываем его график на вкладке «Мониторинг».
  useEffect(() => {
    const onSchemaOpen = (e: Event) => {
      const tag = (e as CustomEvent).detail as string
      if (!tag) return
      const hit = sensors.find(s => s.tag === tag)
      if (hit) { setSelectedId(hit.id); setActiveView('monitor'); setSchemaNotice(null) }
      else { setSchemaNotice('Для этой точки нет обучаемой модели — графика в «Мониторинге» нет (точка САУиР/контроль, не отклик).') }
    }
    window.addEventListener('schema-open-monitor', onSchemaOpen as EventListener)
    return () => window.removeEventListener('schema-open-monitor', onSchemaOpen as EventListener)
  }, [sensors])
  // Автоскрытие тоста схемы
  useEffect(() => {
    if (!schemaNotice) return
    const id = window.setTimeout(() => setSchemaNotice(null), SCHEMA_NOTICE_TTL_MS)
    return () => window.clearTimeout(id)
  }, [schemaNotice])
  const { data: stats } = useQuery({
    queryKey: ['stats', activeStation],
    queryFn: () => api.stats(activeStation),
    refetchInterval: REFETCH_MS,
    staleTime: 20_000,
  })
  const { data: rawEvents = [] } = useQuery({
    queryKey: ['events', activeStation],
    queryFn: () => api.events({}, activeStation),
    refetchInterval: REFETCH_MS,
    staleTime: 20_000,
  })
  const { data: heatmapCells = [] } = useQuery({
    queryKey: ['heatmap', activeStation],
    queryFn: () => api.heatmap(activeStation),
    refetchInterval: REFETCH_MS,
    staleTime: 20_000,
  })
  const hasRange = chartFrom && chartTo
  // Зум-окно Plotly (детальный слой): сервер вернёт его с мелким бакетом
  const [zoomWindow, setZoomWindow] = useState<{ t0: string; t1: string } | null>(null)

  // Обзорный слой — выбранный пресет (1д..30д), живые данные
  const { data: chartData, isFetching: chartLoading, isError: chartIsError } = useQuery({
    queryKey: ['chart', activeStation, selectedId, chartDays],
    queryFn: () => selectedId ? api.sensorChart(selectedId, chartDays, activeStation) : null,
    enabled: !!selectedId,
    refetchInterval: REFETCH_MS,
    staleTime: 20_000,        // не перезапрашивать тот же пресет в окне свежести
    gcTime: 30 * 60_000,
    placeholderData: keepPreviousData,
  })

  // Кастомный период из DatePicker — серверный запрос диапазона (не клиентский фильтр).
  // История иммутабельна: конец диапазона старше часа => кешируем навсегда.
  // Наивные строки даты — это локальное время станции (Etc/GMT-5 = +05:00). Явно
  // указываем смещение, иначе new Date() трактует их как TZ браузера/RDP, и решение
  // «диапазон в прошлом → кешировать навсегда» сдвигается на разницу зон.
  const rangeHistorical = hasRange
    ? new Date(chartTo + 'T23:59:59+05:00').getTime() < Date.now() - 3_600_000
    : false
  const { data: rangeData, isFetching: rangeLoading, isError: rangeIsError } = useQuery({
    queryKey: ['chart', activeStation, selectedId, 'range', chartFrom, chartTo],
    queryFn: () => api.sensorChart(
      selectedId!, { t0: `${chartFrom}T00:00:00`, t1: `${chartTo}T23:59:59` }, activeStation),
    enabled: !!selectedId && !!hasRange,
    staleTime: rangeHistorical ? Infinity : 10_000,
    refetchInterval: rangeHistorical ? false : REFETCH_MS,
    gcTime: 30 * 60_000,
    placeholderData: keepPreviousData,
  })

  // Детальный слой зума
  const zoomHistorical = zoomWindow
    ? new Date(zoomWindow.t1 + '+05:00').getTime() < Date.now() - 3_600_000
    : false
  const { data: zoomData, isError: zoomIsError } = useQuery({
    queryKey: ['chart', activeStation, selectedId, 'win', zoomWindow?.t0, zoomWindow?.t1],
    queryFn: () => api.sensorChart(
      selectedId!, { t0: zoomWindow!.t0, t1: zoomWindow!.t1 }, activeStation),
    enabled: !!selectedId && !!zoomWindow,
    staleTime: zoomHistorical ? Infinity : 10_000,
    refetchInterval: zoomHistorical ? false : REFETCH_MS,
    gcTime: 30 * 60_000,
    placeholderData: keepPreviousData,
  })

  // Сброс зума при смене датчика/пресета/диапазона
  useEffect(() => { setZoomWindow(null) }, [selectedId, chartDays, chartFrom, chartTo])

  const displayChartData = useMemo(() => {
    if (zoomWindow && zoomData) return zoomData
    if (hasRange) return rangeData ?? null
    return chartData ?? null
  }, [zoomWindow, zoomData, hasRange, rangeData, chartData])

  // Ошибка активного слоя графика (зум/диапазон/пресет) — чтобы отличать «ошибка» от «нет данных».
  const displayError = useMemo(
    () => (zoomWindow ? zoomIsError : hasRange ? rangeIsError : chartIsError),
    [zoomWindow, zoomIsError, hasRange, rangeIsError, chartIsError]
  )

  const availableDataDays = useMemo(() => {
    // по активному слою (пресет/диапазон/зум), иначе индикатор глубины врал в range/zoom
    const s = displayChartData?.series
    if (!s || s.length < 2) return 0
    return Math.round((new Date(s[s.length - 1].t).getTime() - new Date(s[0].t).getTime()) / 86_400_000)
  }, [displayChartData])

  const filteredSensorIds = useMemo<Set<string> | null>(() => {
    if (!kindFilter) return null
    return new Set(sensors.filter(s => (s.anomaly_types as string[]).includes(kindFilter)).map(s => s.id))
  }, [kindFilter, sensors])

  const events = useMemo<EventItem[]>(
    () => rawEvents.map(ev => ({ ...ev, acked: ev.acked || ackedIds.has(ev.id) })),
    [rawEvents, ackedIds]
  )
  const unackedCount = useMemo(() => events.filter(e => !e.acked).length, [events])
  const selectedSensor = useMemo(() => sensors.find(s => s.id === selectedId) ?? null, [sensors, selectedId])

  // Cross-GPA overlay: sibling sensors with same base name on other GPAs
  const overlayTargets = useMemo(() => {
    if (!gpaOverlay || !selectedSensor) return []
    const sep = selectedSensor.id.lastIndexOf('__')
    if (sep < 0) return []
    const base = selectedSensor.id.slice(0, sep)
    const ids = new Set(sensors.map(s => s.id))
    return sensors
      .filter(s => s.id !== selectedSensor.id && s.id.slice(0, s.id.lastIndexOf('__')) === base && ids.has(s.id))
      .map(s => ({ id: s.id, gpa: s.gpa }))
  }, [gpaOverlay, selectedSensor, sensors])

  // Окно для оверлеев = тот же диапазон/пресет, что и основной график. Иначе
  // оверлеи (по 30д) растягивали бы X-ось, игнорируя выбранный датапикером период.
  const overlayArg = useMemo(
    () => hasRange ? { t0: `${chartFrom}T00:00:00`, t1: `${chartTo}T23:59:59` } : chartDays,
    [hasRange, chartFrom, chartTo, chartDays]
  )
  const overlayKey = useMemo(
    () => hasRange ? `range:${chartFrom}:${chartTo}` : `d${chartDays}`,
    [hasRange, chartFrom, chartTo, chartDays]
  )

  const overlayQueries = useQueries({
    queries: overlayTargets.map(t => ({
      queryKey: ['chart', activeStation, t.id, overlayKey] as const,
      queryFn: () => api.sensorChart(t.id, overlayArg, activeStation),
      enabled: gpaOverlay,
      refetchInterval: REFETCH_MS,
      staleTime: 20_000,
      gcTime: 30 * 60_000,
      placeholderData: keepPreviousData,
    })),
  })

  const gpaOverlaySeries = useMemo<GpaOverlay[]>(() => {
    if (!gpaOverlay) return []
    return overlayTargets.map((t, i) => ({
      gpa: t.gpa,
      points: overlayQueries[i]?.data?.series ?? [],
    })).filter(o => o.points.length > 0)
  }, [gpaOverlay, overlayTargets, overlayQueries])

  // ── Перетащенные датчики (drag-drop) → доп. ряды на графике ──
  // Исключаем сам выбранный датчик и дубли; грузим их ряды тем же способом.
  const droppedTargets = useMemo(() => {
    const seen = new Set<string>()
    return droppedIds
      .filter(id => id !== selectedId && !seen.has(id) && (seen.add(id), true))
      .map(id => sensors.find(s => s.id === id))
      .filter((s): s is NonNullable<typeof s> => !!s)
      .map(s => ({ id: s.id, gpa: s.gpa, label: ruSensor(s.name) + ` · ГПА-${s.gpa.replace('GPA', '')}` }))
  }, [droppedIds, selectedId, sensors])

  const droppedQueries = useQueries({
    queries: droppedTargets.map(t => ({
      queryKey: ['chart', activeStation, t.id, overlayKey] as const,
      queryFn: () => api.sensorChart(t.id, overlayArg, activeStation),
      refetchInterval: REFETCH_MS,
      staleTime: 20_000,
      gcTime: 30 * 60_000,
      placeholderData: keepPreviousData,
    })),
  })

  const overlaySeries = useMemo<GpaOverlay[]>(() => {
    const dropped = droppedTargets.map((t, i) => ({
      gpa: t.gpa, label: t.label, secondary: true,
      points: droppedQueries[i]?.data?.series ?? [],
    })).filter(o => o.points.length > 0)
    return [...gpaOverlaySeries, ...dropped]
  }, [gpaOverlaySeries, droppedTargets, droppedQueries])

  // Стабилизация идентичности overlaySeries. useQueries возвращает новый массив на
  // КАЖДЫЙ рендер App, поэтому overlaySeries (даже пустой) менял ссылку на каждом
  // ре-рендере → useMemo traces в SensorChart пересчитывался → effect видел новый
  // traces → лишний Plotly.react (полная перерисовка SVG + rangeslider). Считаем
  // сигнатуру по содержимому и переиспользуем прошлый массив, пока данные те же.
  const overlaySig = overlaySeries
    .map(o => `${o.gpa}|${o.label ?? ''}|${o.secondary ? 1 : 0}|${o.points.length}|${o.points[o.points.length - 1]?.t ?? ''}`)
    .join(';')
  const overlaySigRef = useRef('')
  const stableOverlayRef = useRef<GpaOverlay[]>([])
  if (overlaySig !== overlaySigRef.current) {
    overlaySigRef.current = overlaySig
    stableOverlayRef.current = overlaySeries
  }
  const stableOverlaySeries = stableOverlayRef.current

  // При смене основного датчика: убираем его самого из наложений и сбрасываем участок.
  // НЕ обнуляем весь список — наложения (сравнение) переживают смену основного, а
  // повышенный из «осиротевших» (см. ниже) корректно остаётся, прочие — как наложения.
  useEffect(() => { setDroppedIds(prev => prev.filter(id => id !== selectedId)); setRegionSel(null) }, [selectedId])

  // Наложения привязаны к датчикам текущей станции → при смене станции сбрасываем,
  // иначе «осиротевшие» чужие id могут быть ошибочно повышены в основной (404-запросы).
  useEffect(() => { setDroppedIds([]) }, [activeStation])

  // При смене станции инвалидируем зависящие от станции запросы, чтобы
  // не было вспышки устаревших данных предыдущей станции перед загрузкой новых.
  useEffect(() => {
    qc.invalidateQueries({ queryKey: ['sensors', activeStation] })
    qc.invalidateQueries({ queryKey: ['stats', activeStation] })
    qc.invalidateQueries({ queryKey: ['events', activeStation] })
    qc.invalidateQueries({ queryKey: ['heatmap', activeStation] })
  }, [activeStation, qc])

  // Если основной датчик не выбран, но есть перетащенные — повышаем первый ВАЛИДНЫЙ
  // (существующий в списке датчиков активной станции) до основного. Иначе чипы висят
  // без графика («датчики есть, графиков нет»): SensorChart требует основной sensor.
  // Фильтр по sensors защищает от чужих id (смена станции, устаревшее состояние).
  useEffect(() => {
    if (selectedId) return
    const firstValid = droppedIds.find(id => sensors.some(s => s.id === id))
    if (firstValid) setSelectedId(firstValid)
  }, [selectedId, droppedIds, sensors])

  const handleToggleSidebar = useCallback(() => setSidebarCollapsed(v => !v), [])
  const handleOpenDrawer = useCallback(() => setDrawerOpen(true), [])
  const handleCloseDrawer = useCallback(() => setDrawerOpen(false), [])
  const handleKioskExit = useCallback(() => setKioskActive(false), [])
  const handleOpenKiosk = useCallback(() => setKioskActive(true), [])
  const handleOpenReport = useCallback(() => setReportOpen(true), [])
  const handleCloseReport = useCallback(() => setReportOpen(false), [])
  const handleOpenEngine = useCallback(() => setEngineOpen(true), [])
  const handleCloseEngine = useCallback(() => setEngineOpen(false), [])
  const handleOpenCompare = useCallback(() => setCompareOpen(true), [])
  const handleCloseCompare = useCallback(() => setCompareOpen(false), [])
  const handleCloseFeat = useCallback(() => { setFeatOpen(false); setRegionSel(null) }, [])
  const handleOpenFeat = useCallback(() => { setRegionSel(null); setFeatOpen(true) }, [])
  const handleToggleGpaOverlay = useCallback(() => setGpaOverlay(v => !v), [])
  const handleViewMonitor = useCallback(() => setActiveView('monitor'), [])
  const handleViewSchema = useCallback(() => setActiveView('schema'), [])
  const handleCloseFocusPanel = useCallback(() => setFocusEventId(null), [])
  const handleClearRange = useCallback(() => { setChartFrom(''); setChartTo('') }, [])
  const handleClearKindFilter = useCallback(() => setKindFilter(null), [])
  // Фабрика для кнопок пресета дней: создаёт стабильный колбэк для каждого значения d
  const handleSetDays = useCallback((d: number) => {
    setChartDays(d); setChartFrom(''); setChartTo('')
  }, [])
  const handleFocusDate = useCallback((ymd: string) => {
    setChartFrom(ymd); setChartTo(ymd)
  }, [])
  const handleCloseSchemaNotice = useCallback(() => setSchemaNotice(null), [])
  const handleStationChange = useCallback((id: string) => {
    setActiveStation(id)
    setSelectedId(null)
  }, [])

  const handleEngineSelectSensor = useCallback((name: string) => {
    if (name.startsWith('__gpa__')) return
    const hit = sensors.find(s => s.id === name || s.name === name)
    if (hit) { setSelectedId(hit.id); setActiveView('monitor') }
    setEngineOpen(false)
  }, [sensors])

  const handleDropSensor = useCallback((id: string) => {
    // нет основного датчика → перетащенный становится основным графиком (с моделью/
    // коридором/аномалиями); при наличии основного — добавляем как наложение.
    if (!selectedId) { setSelectedId(id); return }
    setDroppedIds(prev => (prev.includes(id) || id === selectedId ? prev : [...prev, id].slice(-4)))
  }, [selectedId])

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tgt = e.target as HTMLElement | null
      // не перехватывать буквенные шорткаты при наборе в любых полях ввода
      if (tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA' || tgt.tagName === 'SELECT' || tgt.isContentEditable)) return
      if (e.code === 'KeyJ') setDrawerOpen(v => !v)
      if (e.code === 'KeyK' && !e.ctrlKey) setKioskActive(v => !v)
      if (e.code === 'KeyT') toggleTheme()
      if (e.key === 'Escape') { setDrawerOpen(false); setKioskActive(false) }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const [focusEventId, setFocusEventId] = useState<string | null>(null)
  // Живой объект события из актуального списка (не устаревший снимок): отражает
  // обновления при refetch и сам исчезает, если событие пропало или его датчик
  // не равен выбранному.
  const focusEvent = useMemo(
    () => { const e = events.find(x => x.id === focusEventId); return e && e.sensor_id === selectedId ? e : null },
    [events, focusEventId, selectedId]
  )

  // Время аномалии для «Важных признаков»: сфокусированное событие этого датчика,
  // иначе — последняя аномалия на текущем графике. null → модалка покажет фолбэк по базе.
  const featEventTs = useMemo<string | null>(() => {
    if (focusEvent && focusEvent.sensor_id === selectedId) return focusEvent.timestamp
    const an = displayChartData?.anomalies
    if (an && an.length) return an[an.length - 1].t
    return null
  }, [focusEvent, selectedId, displayChartData])

  // SHAP-атрибуция конкретной аномалии (on-demand): тянем только когда модалка открыта.
  // БД офлайн / нет события → запрос не идёт или падает в 503 → фолбэк на базу кейсов.
  const { data: explainData, isFetching: explainLoading } = useQuery({
    queryKey: ['explain', activeStation, selectedId, featEventTs],
    queryFn: () => api.explain(selectedId!, featEventTs!, 6, activeStation),
    enabled: featOpen && !!selectedId && !!featEventTs && !regionSel,
    staleTime: 5 * 60_000,
    gcTime: 10 * 60_000,
    retry: false,
  })

  // Region SHAP: топ-5 вкладчиков в расчёт на выделенном участке [t0,t1]
  const { data: regionExplain, isFetching: regionLoading } = useQuery({
    queryKey: ['explainRegion', activeStation, selectedId, regionSel?.t0, regionSel?.t1, regionSel?.v0, regionSel?.v1],
    queryFn: () => api.explainRegion(selectedId!, regionSel!.t0, regionSel!.t1, regionSel!.v0, regionSel!.v1, 6, activeStation),
    enabled: featOpen && !!selectedId && !!regionSel,
    staleTime: 5 * 60_000,
    gcTime: 10 * 60_000,
    retry: false,
  })

  // animejs: плавный вход main при смене вида/станции
  useEffect(() => {
    if (prefersReducedMotion() || activeView !== 'monitor') return
    const el = document.querySelector('.js-view-monitor')
    if (!el) return
    // Используем motion-токены: стандартный вход — dur-moderate + ease-decelerate
    const a = animate(el, { opacity: [0, 1], translateY: [10, 0], duration: 300, ease: 'outCubic' })
    return () => { a.pause() }
  }, [activeView, activeStation])


  // animejs: появление баннера фильтра
  useEffect(() => {
    if (prefersReducedMotion() || !kindFilter) return
    const el = document.querySelector('.js-filter-banner')
    if (!el) return
    const a = animate(el, { opacity: [0, 1], translateY: [-8, 0], duration: 240, ease: 'outCubic' })
    return () => { a.pause() }
  }, [kindFilter])

  // animejs: вход правой аналитической панели при смене выбранного датчика
  useEffect(() => {
    if (prefersReducedMotion() || !selectedId || activeView !== 'monitor') return
    const el = document.querySelector('.js-detail-panel')
    if (!el) return
    const a = animate(el, { opacity: [0, 1], translateX: [16, 0], duration: 300, ease: 'outCubic' })
    return () => { a.pause() }
  }, [selectedId, activeView])

  // animejs: появление панели события
  useEffect(() => {
    if (prefersReducedMotion() || !focusEvent) return
    const el = document.querySelector('.js-focus-panel')
    if (!el) return
    const a = animate(el, { opacity: [0, 1], translateY: [-8, 0], duration: 260, ease: 'outCubic' })
    return () => { a.pause() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusEvent?.id])

  const handleAck = useCallback((id: string) => {
    setAckedIds(p => new Set([...p, id]))           // оптимистично + локально (offline-tolerant)
    const ev = events.find(e => e.id === id)
    if (ev) {
      api.ackEvent(ev.sensor_id, ev.timestamp, ev.kind, activeStation)
        .then(() => qc.invalidateQueries({ queryKey: ['events', activeStation] }))
        .catch(() => { /* best-effort: локальный ack уже применён */ })
    }
  }, [events, activeStation, qc])
  const handleAckAll = useCallback(() => {
    setAckedIds(new Set(events.map(e => e.id)))
    const toAck = events.filter(e => !e.acked)
    if (toAck.length) {
      Promise.allSettled(toAck.map(e => api.ackEvent(e.sensor_id, e.timestamp, e.kind, activeStation)))
        .then(() => qc.invalidateQueries({ queryKey: ['events', activeStation] }))
    }
  }, [events, activeStation, qc])
  // Зум Plotly -> канонизированное окно (кратно 5 мин — стабильные ключи кеша)
  const handleRangeChange = useCallback((win: { t0: string; t1: string } | null) => {
    if (!win) { setZoomWindow(null); return }
    const GRID = ZOOM_SNAP_GRID_MS
    const t0ms = Math.floor(new Date(win.t0.replace(' ', 'T')).getTime() / GRID) * GRID
    const t1ms = Math.ceil(new Date(win.t1.replace(' ', 'T')).getTime() / GRID) * GRID
    if (!Number.isFinite(t0ms) || !Number.isFinite(t1ms) || t1ms <= t0ms) return
    const iso = (ms: number) => {
      const d = new Date(ms), p = (n: number) => String(n).padStart(2, '0')
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
    }
    setZoomWindow({ t0: iso(t0ms), t1: iso(t1ms) })
  }, [])
  const handleEventSelect = useCallback((ev: EventItem) => {
    setSelectedId(ev.sensor_id)
    setFocusEventId(ev.id)
    setDrawerOpen(false)
    // Любой клик по событию (журнал/баннер/киоск/схема) ведёт на график в «Мониторинге».
    // Без этого, находясь в «Схеме КС» или дежурном режиме, выбор датчика происходил,
    // но вид не переключался → пользователь оставался не на графике.
    setActiveView('monitor')
    setChartDays(EVENT_SELECT_DAYS)
    setChartFrom('')
    setChartTo('')
  }, [])
  // Стабильный колбэк выделения участка (region SHAP). Инлайн-стрелка давала новую
  // ссылку на каждом рендере и сводила на нет memo() у SensorChart.
  const handleRegionSelect = useCallback((t0: string, t1: string, v0?: number, v1?: number) => {
    setRegionSel({ t0, t1, v0, v1 })
    setFeatOpen(true)
  }, [])

  // Derived display strings — memoised to avoid recomputing in JSX on every render
  const sidebarLastUpdated = useMemo(
    () => stats?.last_updated
      ? new Date(stats.last_updated).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
      : '—',
    [stats?.last_updated]
  )
  const stationName = useMemo(
    () => stations.find(s => s.id === activeStation)?.display_name ?? 'КС Ахангаран',
    [stations, activeStation]
  )

  // Grid columns depend on activeView and sidebarCollapsed — memoised to avoid new string each render
  const rootGridStyle = useMemo(() => ({
    ...styleRootMonitor,
    gridTemplateColumns: activeView === 'schema'
      ? '1fr'
      : `${sidebarCollapsed ? '48px' : '272px'} 1fr 300px`,
  }), [activeView, sidebarCollapsed])

  const centerColStyle = useMemo(() => ({
    gridColumn: activeView === 'schema' ? ('1 / -1' as const) : 2,
    gridRow: 2,
    display: 'grid',
    gridTemplateRows: 'minmax(0, 1fr)',
    minHeight: 0,
    minWidth: activeView === 'schema' ? 0 : 480,
    overflow: 'hidden',
  }), [activeView])

  // ARIA live region: отслеживаем новые критические непринятые алерты
  const prevCritCountRef = useRef(0)
  const [liveMsg, setLiveMsg] = useState('')
  const critEventCount = useMemo(() => events.filter(e => !e.acked && e.severity === 'crit').length, [events])
  useEffect(() => {
    if (critEventCount > prevCritCountRef.current) {
      setLiveMsg(`Новая критическая аномалия. Всего непринятых критических: ${critEventCount}`)
      const t = window.setTimeout(() => setLiveMsg(''), 5000)
      prevCritCountRef.current = critEventCount
      return () => window.clearTimeout(t)
    }
    prevCritCountRef.current = critEventCount
  }, [critEventCount])

  return (
    <div style={rootGridStyle}>

      {/* Ссылка-пропуск для клавиатурных пользователей — первый фокусируемый элемент */}
      <a
        href="#main-content"
        style={styleSkipLink}
        onFocus={e => { (e.currentTarget as HTMLAnchorElement).style.top = '8px' }}
        onBlur={e => { (e.currentTarget as HTMLAnchorElement).style.top = '-40px' }}
      >
        Перейти к основному содержимому
      </a>

      {/* ARIA live region: объявляет новые критические алерты скринридерам */}
      <div
        role="status"
        aria-live="polite"
        aria-atomic="true"
        id="live-announcer"
        style={styleVisuallyHidden}
      >
        {liveMsg}
      </div>

      <ApiErrorBanner />

      {/* Сайдбар датчиков — только в режиме «Мониторинг». У «Схемы КС» своя
          навигация (список схем + поиск), поэтому сайдбар и правую панель прячем. */}
      {activeView === 'monitor' && (
      <div style={styleSidebarWrapper}>
      <Sidebar
        sensors={sensors}
        selectedId={selectedId}
        onSelect={setSelectedId}
        collapsed={sidebarCollapsed}
        onToggleCollapse={handleToggleSidebar}
        filteredSensorIds={filteredSensorIds}
        lastUpdated={sidebarLastUpdated}
      />
      </div>
      )}

      {/* ── Тикер: верхняя панель во всю ширину (строка 1, все колонки) — больше не делит ширину с правой панелью, кнопки не срезаются ── */}
      <div style={styleTickerWrapper}>

        {/* ── Ticker ── */}
        <Ticker
          time={<Clock />}
          onOpenDrawer={handleOpenDrawer}
          unackedCount={unackedCount}
          left={
            <span style={styleTickerLeft}>
              <span title="Датчиков на станции" style={styleSensorCountBadge}>
                <svg width="13" height="13" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="1.4" fill="currentColor"/><path d="M4.2 4.2a4 4 0 0 0 0 5.6M9.8 4.2a4 4 0 0 1 0 5.6M2.4 2.4a6.5 6.5 0 0 0 0 9.2M11.6 2.4a6.5 6.5 0 0 1 0 9.2" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/></svg>
                <span style={styleBadgeValue}>{stats?.total_sensors ?? sensors.length}</span>
              </span>
              <span title="Аномалий за период" style={styleSensorCountBadge}>
                <svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M7 1.7l5.5 9.6H1.5z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round"/><path d="M7 5.6v2.4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/><circle cx="7" cy="9.6" r="0.7" fill="currentColor"/></svg>
                <span style={styleBadgeValue}>{stats?.total_anomalies ?? 0}</span>
              </span>
            </span>
          }
          right={
            <div style={styleTickerRightWrap}>
              <Freshness lastUpdated={stats?.last_updated} />
              <Sep />
              {/* Вид: Мониторинг / Схема (сегментный переключатель) */}
              <IconBtn title="Мониторинг" active={activeView === 'monitor'} onClick={handleViewMonitor}>
                <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M2 2v11h11" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" opacity="0.45"/><path d="M2.5 10L5.5 6.5L8 8.5L12.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </IconBtn>
              <IconBtn title="Схема КС" active={activeView === 'schema'} onClick={handleViewSchema}>
                <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><rect x="1.5" y="6" width="3.5" height="3" rx="0.5" stroke="currentColor" strokeWidth="1.3"/><rect x="10" y="2" width="3.5" height="3" rx="0.5" stroke="currentColor" strokeWidth="1.3"/><rect x="10" y="10" width="3.5" height="3" rx="0.5" stroke="currentColor" strokeWidth="1.3"/><path d="M5 7.5h2.5V3.5H10M7.5 7.5V11.5H10" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </IconBtn>
              {/* Двигатель → полноэкранный 3D-режим */}
              <IconBtn title="Двигатель · 3D-режим" onClick={handleOpenEngine}>
                <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><circle cx="7.5" cy="7.5" r="2" stroke="currentColor" strokeWidth="1.3"/><path d="M7.5 1.6v2.1M7.5 11.3v2.1M1.6 7.5h2.1M11.3 7.5h2.1M3.3 3.3l1.5 1.5M10.2 10.2l1.5 1.5M3.3 11.7l1.5-1.5M10.2 4.8l1.5-1.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/></svg>
              </IconBtn>
              <Sep />
              {/* Действия */}
              {activeView === 'monitor' && (
                <IconBtn title="Отчёт смены" onClick={handleOpenReport}>
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3 1.5h4.3L11 5.2V12.5H3z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/><path d="M7.3 1.5V5.2H11" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/><path d="M5 7.5h4M5 9.5h4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/></svg>
                </IconBtn>
              )}
              <IconBtn title="Дежурный режим (K)" onClick={handleOpenKiosk}>
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <rect x="1" y="2" width="12" height="8" rx="1" stroke="currentColor" strokeWidth="1.3"/>
                  <path d="M5 12h4M7 10v2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
                </svg>
              </IconBtn>
              <IconBtn title={`Тема: ${theme} (T)`} onClick={toggleTheme}>
                {/* t-icon-swap: sun ↔ moon crossfade (blur+scale) — transitions-dev */}
                <span className="t-icon-swap" data-state={theme === 'dark' ? 'a' : 'b'}>
                  <span className="t-icon" data-icon="a">
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                      <circle cx="7" cy="7" r="2.5" stroke="currentColor" strokeWidth="1.3"/>
                      <path d="M7 1v1.5M7 11.5V13M1 7h1.5M11.5 7H13M3.05 3.05l1.06 1.06M9.89 9.89l1.06 1.06M3.05 10.95l1.06-1.06M9.89 4.11l1.06-1.06" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
                    </svg>
                  </span>
                  <span className="t-icon" data-icon="b">
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                      <path d="M11.5 8.5A5 5 0 0 1 5.5 2.5a5 5 0 1 0 6 6z" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                  </span>
                </span>
              </IconBtn>
              <Sep />
              {/* Контекст: станция + возврат к обзору */}
              <StationSwitcher
                stations={stations}
                active={activeStation}
                onChange={handleStationChange}
              />
              {onBackToOverview && (
                <IconBtn title="К обзору станций" onClick={onBackToOverview}>
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M8.5 2.5L4 7l4.5 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
                </IconBtn>
              )}
            </div>
          }
        />
      </div>

      {/* ── Центр: Схема / Мониторинг (колонка 2, строка 2). Одна строка 1fr; в потоке
            всегда только активный вид (SchemaPanel при неактиве — display:none) ── */}
      <div style={centerColStyle}>

        {/* ── Schema view ── */}
        <SchemaPanel active={activeView === 'schema'} />

        {/* ── Main ── */}
        {activeView === 'monitor' && <main id="main-content" className="js-view-monitor" style={styleMainPanel}>

          {/* Stats */}
          {stats && (
            <StatsGrid stats={stats} activeFilter={kindFilter} onFilter={setKindFilter} />
          )}

          {/* Priority summary */}
          <PriorityBanner events={events} onSelect={handleEventSelect} />

          {/* Kind filter banner */}
          {kindFilter && (
            <div className="js-filter-banner" style={styleKindFilterBanner}>
              <span>Фильтр: {KIND_LABEL[kindFilter as keyof typeof KIND_LABEL] ?? kindFilter}</span>
              <button onClick={handleClearKindFilter} style={styleKindFilterClearBtn}>
                ✕ Сбросить
              </button>
            </div>
          )}

          {/* Bottom: heatmap + chart */}
          <div style={styleBottomGrid}>

            {/* Heatmap */}
            <div style={styleHeatmapSection}>
              <div style={styleHeatmapHeader}>
                <span style={styleHeatmapTitle}>
                  Тепловая карта
                </span>
                <span style={styleHeatmapCount}>
                  {heatmapCells.length}
                </span>
              </div>
              <HeatMap cells={heatmapCells} selectedSensorId={selectedId} onSelect={setSelectedId} filteredSensorIds={filteredSensorIds} />
            </div>

            {/* Chart — зона приёма drag-drop датчиков (наложение доп. рядов) */}
            <div
              onDragOver={e => { if (e.dataTransfer.types.includes('application/x-sensor-id')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; if (!dragOver) setDragOver(true) } }}
              onDragLeave={e => { if (e.currentTarget === e.target) setDragOver(false) }}
              onDrop={e => {
                const id = e.dataTransfer.getData('application/x-sensor-id')
                setDragOver(false)
                if (id) { e.preventDefault(); handleDropSensor(id) }
              }}
              style={{ position: 'relative', background: 'var(--surface)', padding: '12px 14px', display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden', outline: dragOver ? '2px dashed var(--accent)' : 'none', outlineOffset: -4 }}
            >
              {dragOver && (
                <div style={styleDropOverlay}>
                  Отпустите — наложить датчик на график
                </div>
              )}
              {/* Сравнение — операция уровня станции (любые датчики), доступна и без
                  выбранного датчика. При выбранном — кнопка уже есть в тулбаре ниже. */}
              {!selectedSensor && (
                <button onClick={handleOpenCompare} title="Сравнить датчики (разные ГПА/типы) на одном канвасе" style={styleCompareFloatBtn}
                  onMouseEnter={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--accent)'; (e.currentTarget as HTMLElement).style.color = 'var(--accent)' }}
                  onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--line)'; (e.currentTarget as HTMLElement).style.color = 'var(--text-2)' }}
                >⇄ Сравнить датчики</button>
              )}
              {selectedSensor && (
                <div style={styleSensorHeaderRow}>
                  <div>
                    <div style={{ fontSize: 'var(--fs-md)', fontWeight: 600, color: 'var(--text-1)', letterSpacing: '0.04em', marginBottom: 2 }}>
                      {ruSensor(selectedSensor.name)}
                    </div>
                    <div style={{ fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)', color: 'var(--text-2)', lineHeight: 1.2 }}>
                      {selectedSensor.tag}
                    </div>
                    <div style={{ fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)', color: 'var(--text-3)', lineHeight: 1.5 }}>
                      ГПА-{selectedSensor.gpa.replace('GPA', '')} · {selectedSensor.subsystem}
                    </div>
                  </div>
                  <div style={styleSensorToolbar}>
                    <span>от</span>
                    <DatePicker value={chartFrom} onChange={setChartFrom} highlighted={!!hasRange} />
                    <span>до</span>
                    <DatePicker value={chartTo} onChange={setChartTo} highlighted={!!hasRange} />
                    {hasRange && (
                      <button onClick={handleClearRange} style={styleClearRangeBtn}>✕</button>
                    )}
                    <div style={styleDivider} />
                    {[1, 3, 7, 14, 30].map(d => (
                      <button key={d} onClick={() => handleSetDays(d)} style={{
                        height: 26, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                        padding: '0 9px', fontFamily: 'inherit', fontSize: 'inherit', border: '1px solid',
                        borderRadius: 'var(--r-sm)', whiteSpace: 'nowrap', flexShrink: 0,
                        borderColor: !hasRange && chartDays === d ? 'var(--accent)' : 'var(--line)',
                        background: !hasRange && chartDays === d ? 'var(--accent-glow)' : 'transparent',
                        color: !hasRange && chartDays === d ? 'var(--accent)' : 'var(--text-3)',
                        cursor: 'pointer',
                        opacity: availableDataDays > 0 && d > availableDataDays ? 0.45 : 1,
                      }}>
                        {d}д
                      </button>
                    ))}
                    <div style={styleDivider} />
                    <button onClick={handleOpenFeat} title="Важные признаки аномалии: интерпретация (база кейсов) + графики параметров-вкладчиков. Совет: правой кнопкой выделите участок графика — откроются топ-5 вкладчиков по нему" style={styleFeatBtn}>
                      ★ Важные признаки
                    </button>
                    <button onClick={handleToggleGpaOverlay} title="Наложить тот же датчик с других ГПА (сравнение, кросс-ГПА)" style={{
                      height: 26, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                      padding: '0 9px', fontFamily: 'inherit', fontSize: 'inherit', border: '1px solid',
                      borderRadius: 'var(--r-sm)', whiteSpace: 'nowrap', flexShrink: 0,
                      borderColor: gpaOverlay ? 'var(--accent)' : 'var(--line)',
                      background: gpaOverlay ? 'var(--accent-glow)' : 'transparent',
                      color: gpaOverlay ? 'var(--accent)' : 'var(--text-3)', cursor: 'pointer',
                    }}>
                      ⊕ Сравн. ГПА
                    </button>
                    <button onClick={handleOpenCompare} title="Сравнить с другими датчиками (разные ГПА/типы) на одном канвасе" style={styleCompareBtn}>
                      ⇄ Сравнить
                    </button>
                  </div>
                </div>
              )}
              {/* Event focus panel */}
              {focusEvent && (() => {
                const ev = focusEvent
                const isAcked = ackedIds.has(ev.id)
                return (
                  <div className="js-focus-panel" style={{
                    // Von Restorff: панель сфокусированного события заметно выделена —
                    // более светлая поверхность (--surface-3), усиленный бордер по severity,
                    // акцентный левый бордер 3px и мягкая тень, чтобы не сливалась с графиком.
                    display: 'flex', alignItems: 'center', gap: 'var(--space-3)', flexShrink: 0, marginBottom: 'var(--space-2)',
                    padding: 'var(--space-2) var(--space-3)', borderRadius: 'var(--r-md)',
                    background: 'var(--surface-3)', border: `1px solid color-mix(in srgb, ${SEV_COLOR[ev.severity]} 40%, transparent)`,
                    borderLeft: `3px solid ${SEV_COLOR[ev.severity]}`,
                    boxShadow: 'var(--shadow-md)',
                    fontFamily: 'Inter, monospace', fontSize: 'var(--fs-xs)',
                  }}>
                    <span className={`badge-sev ${ev.severity}`} style={{ flexShrink: 0 }}>{SEV_LABEL[ev.severity]}</span>
                    <span style={{ color: 'var(--text-2)', flexShrink: 0 }}>{KIND_LABEL[ev.kind] ?? ev.kind}</span>
                    <span style={{ color: 'var(--text-3)', flexShrink: 0 }}>
                      {fmtStation(ev.timestamp, { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' })}
                    </span>
                    {ev.value != null && (
                      <span style={{ color: 'var(--text-2)' }}>
                        Значение: <span style={{ color: 'var(--text-1)', fontWeight: 600 }}>{ev.value.toFixed(4)}</span>
                        {ev.deviation != null && <> · Отклонение: <span style={{ color: 'var(--text-1)', fontWeight: 600 }}>{ev.deviation.toFixed(4)}</span></>}
                      </span>
                    )}
                    <div style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }}>
                      {isAcked ? (
                        <span style={{ color: 'var(--ok)', fontWeight: 600 }}>✓ Принято</span>
                      ) : (
                        <button
                          onClick={() => handleAck(ev.id)}
                          style={{
                            background: 'var(--accent-strong)', color: 'var(--on-accent)', border: 'none',
                            padding: '4px 12px', borderRadius: 'var(--r-sm)',
                            fontSize: 'var(--fs-xs)', fontFamily: 'inherit', cursor: 'pointer', fontWeight: 700,
                          }}
                        >
                          ✓ Принять
                        </button>
                      )}
                      <button
                        onClick={handleCloseFocusPanel}
                        aria-label="Закрыть панель события"
                        style={styleFocusCloseBtn}
                      >✕</button>
                    </div>
                  </div>
                )
              })()}
              {droppedTargets.length > 0 && (
                <div style={styleDroppedChipsRow}>
                  {droppedTargets.map(t => (
                    <span key={t.id} style={styleDroppedChip} className="anim-scale-in">
                      {t.label}
                      <button
                        onClick={() => setDroppedIds(prev => prev.filter(id => id !== t.id))}
                        title="Убрать с графика"
                        style={styleDroppedChipRemoveBtn}
                        onMouseEnter={e => { (e.currentTarget as HTMLElement).style.color = 'var(--crit)' }}
                        onMouseLeave={e => { (e.currentTarget as HTMLElement).style.color = 'var(--text-3)' }}
                      >✕</button>
                    </span>
                  ))}
                </div>
              )}
              <SensorChart sensor={selectedSensor} chartData={displayChartData ?? null} loading={chartLoading || (!!hasRange && rangeLoading)} error={displayError} theme={theme} kindFilter={kindFilter} onRangeChange={handleRangeChange} viewDays={hasRange || zoomWindow ? undefined : chartDays} focusTimestamp={hasRange ? null : (focusEvent?.timestamp ?? null)} overlaySeries={stableOverlaySeries} onRegionSelect={handleRegionSelect} />
            </div>
          </div>
        </main>}
      </div>

      {/* ── Правая панель аналитики (колонка 3) — только в «Мониторинге» (в «Схеме» скрыта) ── */}
      {activeView === 'monitor' && (
      <div className="js-detail-panel" style={styleRightPanel}>
        <DetailPanel sensor={selectedSensor} events={events} onSelectEvent={handleEventSelect} onFocusDate={handleFocusDate} />
      </div>
      )}

      {/* ── Двигатель: полноэкранный режим просмотра (выезжает снизу) ── */}
      <EngineView
        open={engineOpen}
        gpa={selectedSensor?.gpa ?? 'GPA1'}
        theme={theme}
        onClose={handleCloseEngine}
        onSelectSensor={handleEngineSelectSensor}
      />

      {/* ── «Важные признаки»: интерпретация (база кейсов) + графики вкладчиков ── */}
      <ContributingFeatures
        open={featOpen}
        sensorName={selectedSensor?.name ?? ''}
        caseInfo={regionSel ? null : (selectedSensor ? caseKey(selectedSensor.id, selectedSensor.anomaly_types) : null)}
        explain={regionSel ? (regionExplain ?? null) : (featEventTs ? (explainData ?? null) : null)}
        eventTs={regionSel ? regionSel.t0 : featEventTs}
        regionTo={regionSel?.t1 ?? null}
        theme={theme}
        loading={regionSel ? regionLoading : explainLoading}
        onClose={handleCloseFeat}
      />

      {/* Тост: клик по точке схемы без модели. role=alert — для предупреждения;
          закрытие кнопкой (клавиатура) и кликом по тосту (мышь). */}
      {schemaNotice && (
        <div
          role="alert"
          style={styleSchemaNotice}
        >
          <span
            role="button"
            tabIndex={0}
            style={{ cursor: 'pointer' }}
            onClick={handleCloseSchemaNotice}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleCloseSchemaNotice() } }}
          >⚠ {schemaNotice}</span>
          <button
            type="button"
            onClick={handleCloseSchemaNotice}
            aria-label="Закрыть уведомление"
            style={styleNoticeCloseBtn}
          >✕</button>
        </div>
      )}

      <ComparePanel open={compareOpen} onClose={handleCloseCompare} sensors={sensors} stationId={activeStation} theme={theme} />

      <EventDrawer open={drawerOpen} events={events} onClose={handleCloseDrawer} onAck={handleAck} onAckAll={handleAckAll} onSelect={handleEventSelect} />
      <KioskMode active={kioskActive} onExit={handleKioskExit} onSelect={handleEventSelect} events={events} sensorCount={sensors.length} sensors={sensors} stationId={activeStation} />
      <ShiftReport
        open={reportOpen}
        onClose={handleCloseReport}
        events={events}
        sensors={sensors}
        stationName={stationName}
      />
    </div>
  )
}

// Изолированные часы: собственный state + setInterval здесь, поэтому тик раз в 10с
// ре-рендерит ТОЛЬКО этот листовой компонент, а не всё дерево App с графиками.
function Clock() {
  const [now, setNow] = useState(() =>
    new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
  )
  useEffect(() => {
    const t = setInterval(() =>
      setNow(new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }))
    , 10_000)
    return () => clearInterval(t)
  }, [])
  return <>{now}</>
}

function IconBtn({ children, title, onClick, active }: { children: React.ReactNode; title: string; onClick: () => void; active?: boolean }) {
  return (
    <button
      onClick={onClick}
      title={title}
      aria-label={title}
      aria-pressed={active}
      style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        width: 28, height: 28, flexShrink: 0,
        background: active ? 'var(--accent-glow)' : 'transparent',
        border: '1px solid', borderColor: active ? 'var(--accent)' : 'transparent',
        borderRadius: 'var(--r-md)',
        color: active ? 'var(--accent)' : 'var(--text-2)', cursor: 'pointer', fontSize: 14,
        transition: 'background-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard)',
      }}
      onMouseEnter={e => { if (!active) (e.currentTarget as HTMLElement).style.color = 'var(--accent)' }}
      onMouseLeave={e => { if (!active) (e.currentTarget as HTMLElement).style.color = 'var(--text-2)' }}
    >
      {children}
    </button>
  )
}

// Вертикальный разделитель групп в верхней панели
function Sep() {
  return <span style={styleSep} />
}
