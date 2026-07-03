import { useMemo, useEffect, useRef } from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { animate, stagger } from 'animejs'
import { api } from '../../api/client'
import type { StationInfo, SensorMeta } from '../../types'
import { drawPath } from '../../lib/chartMotion'
import { prefersReducedMotion } from '../../lib/motion'

interface StationOverviewProps {
  lang: 'UZ' | 'RU'
  onSelect: (stationId: string) => void
  onLangChange: (l: 'UZ' | 'RU') => void
  onThemeToggle: () => void
  theme: 'dark' | 'light'
}

// Локализация экрана обзора (переключатель RU/UZ раньше не работал — все строки были
// захардкожены по-русски). Узбекский — латиница, официальный для региона КС.
type Lang = 'UZ' | 'RU'
const T: Record<Lang, Record<string, string>> = {
  RU: {
    stations: 'Компрессорные станции', loading: 'Загрузка станций…',
    notFound: 'Станции не найдены', error: 'Не удалось загрузить станции', retry: 'Повторить',
    running: 'В работе', reserve: 'В резерве', units: 'Агрегаты',
    pumping: 'ПРОКАЧКА', transit: 'ТРАНЗИТ',
    notifications: 'Уведомления', profile: 'Профиль', theme: 'Тема',
  },
  UZ: {
    stations: 'Kompressor stansiyalari', loading: 'Stansiyalar yuklanmoqda…',
    notFound: 'Stansiyalar topilmadi', error: 'Stansiyalarni yuklab boʻlmadi', retry: 'Qayta urinish',
    running: 'Ishlamoqda', reserve: 'Zaxirada', units: 'Agregatlar',
    pumping: 'HAYDASH', transit: 'TRANZIT',
    notifications: 'Bildirishnomalar', profile: 'Profil', theme: 'Mavzu',
  },
}

// ── helpers ───────────────────────────────────────────────────────────────────
type TagEntry = { v?: number } | number
function tagsOf(snap: unknown): Record<string, TagEntry> {
  if (snap && typeof snap === 'object') {
    const o = snap as Record<string, unknown>
    if (o.tags && typeof o.tags === 'object') return o.tags as Record<string, TagEntry>
    return o as Record<string, TagEntry>
  }
  return {}
}
function tagVal(tags: Record<string, TagEntry>, g: string, sensor: string): number | null {
  const e = tags[`GPA-${g}.GPA-${g}.${sensor}.PV`] ?? tags[`GPA-${g}.GPA-${g}.${sensor}`]
  if (e == null) return null
  const v = typeof e === 'number' ? e : e.v
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
const fmt = (v: number | null, d = 2) =>
  v == null ? '—' : v.toLocaleString('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d })

// Спарклайн рисуем одноцветной линией (моно-правило): текст/линии тренда — var(--text-2)
function Sparkline({ data, color = 'var(--text-2)' }: { data: number[]; color?: string }) {
  const path = useMemo(() => {
    if (!data || data.length < 2) return ''
    const min = Math.min(...data), max = Math.max(...data)
    const span = max - min || 1
    const W = 100, H = 34
    return data.map((v, i) => {
      const x = (i / (data.length - 1)) * W
      const y = H - ((v - min) / span) * (H - 4) - 2
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`
    }).join(' ')
  }, [data])
  // bklit-style «прорисовка» спарклайна слева-направо при появлении данных
  const pathRef = useRef<SVGPathElement>(null)
  useEffect(() => {
    if (!path || !pathRef.current) return
    const h = drawPath(pathRef.current)
    return () => h?.cancel()
  }, [path])
  if (!path) return <div style={{ height: 34, opacity: 0.3 }} />
  return (
    <svg viewBox="0 0 100 34" preserveAspectRatio="none" style={{ width: '100%', height: 48 }}>
      <path ref={pathRef} d={path} fill="none" stroke={color} strokeWidth={0.9} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" opacity={0.9} />
    </svg>
  )
}

function Tile({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div style={{
      background: 'var(--surface-2)', border: '1px solid var(--line)',
      borderRadius: 'var(--r-md)', padding: 'var(--space-3)', minWidth: 0, flex: 1,
    }}>
      {/* Подпись — приглушённый одноцветный текст */}
      <div style={{ fontSize: 'var(--fs-xs)', letterSpacing: '0.12em', textTransform: 'uppercase', color: 'var(--text-3)', fontFamily: 'var(--font-display)' }}>{label}</div>
      <div style={{ marginTop: 'var(--space-1)', fontSize: 'var(--fs-lg)', fontWeight: 600, color: 'var(--text-1)', fontFamily: 'var(--font-mono)', lineHeight: 1 }}>
        {value}{unit && <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', marginLeft: 'var(--space-1)', fontWeight: 400 }}>{unit}</span>}
      </div>
    </div>
  )
}

// ── station card (fetches its own snapshot + spark) ─────────────────────────────
function StationCard({ station, onSelect, lang }: { station: StationInfo; onSelect: (id: string) => void; lang: Lang }) {
  const t = T[lang]
  const { data: snap } = useQuery({
    queryKey: ['pvsnapshot', station.id],
    queryFn: () => api.pvsnapshot(station.id),
    enabled: station.enabled,
    refetchInterval: 30_000,
    staleTime: 20_000,
    placeholderData: keepPreviousData,
    retry: false,
  })
  const { data: sensors = [] } = useQuery<SensorMeta[]>({
    queryKey: ['sensors', station.id],
    queryFn: () => api.sensors(station.id),
    enabled: station.enabled,
    staleTime: 30_000,
    retry: false,
  })

  // sparkline: тренд давления нагнетания репрезентативного датчика
  const sparkSensor = useMemo(
    () => sensors.find(s => /pressure_out|^pd|gas_pressure/i.test(s.id))?.id
       ?? sensors.find(s => /pressure/i.test(s.id))?.id ?? sensors[0]?.id,
    [sensors])
  const { data: chart } = useQuery({
    queryKey: ['overviewSpark', station.id, sparkSensor],
    queryFn: () => sparkSensor ? api.sensorChart(sparkSensor, 1, station.id) : null,
    enabled: station.enabled && !!sparkSensor,
    staleTime: 60_000,
    retry: false,
  })

  const m = useMemo(() => {
    const gpas = station.units.map(u => u.replace(/\D/g, '')).filter(Boolean)
    const tags = tagsOf(snap)
    const states = gpas.map(g => tagVal(tags, g, 'STATES_GTD'))
    // STATES_GTD.5 хранится как отдельный ключ — пробуем оба варианта
    const running = gpas.filter(g => {
      const e = tags[`GPA-${g}.GPA-${g}.STATES_GTD.5`]
      const v = e == null ? null : (typeof e === 'number' ? e : e.v)
      return typeof v === 'number' && v >= 0.5
    })
    const runSet = running.length ? running : gpas.filter((_, i) => states[i] != null)
    const avg = (sensor: string) => {
      const vals = runSet.map(g => tagVal(tags, g, sensor)).filter((v): v is number => v != null)
      return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null
    }
    const sum = (sensor: string) => {
      const vals = runSet.map(g => tagVal(tags, g, sensor)).filter((v): v is number => v != null)
      return vals.length ? vals.reduce((a, b) => a + b, 0) : null
    }
    return {
      runningCount: running.length,
      total: gpas.length,
      isRunning: running.length > 0,
      pIn: avg('PS'),
      pOut: avg('PD'),
      flow: sum('FLOW'),
    }
  }, [snap, station.units])

  const spark = useMemo(() => {
    const s = chart?.series ?? []
    if (s.length < 2) return [] as number[]
    const step = Math.max(1, Math.floor(s.length / 48))
    return s.filter((_, i) => i % step === 0).map(p => p.v)
  }, [chart])

  const statusRunning = m.isRunning
  const metricLabel = statusRunning ? t.pumping : t.transit
  // Когда расхода нет — показываем давление как прокси, но и ПОДПИСЬ единицы должна
  // быть давлением (раньше «млн м³/сут» жёстко стояло поверх значения в МПа).
  const usingFlow = m.flow != null
  const metricVal = usingFlow ? m.flow : m.pOut
  const metricUnit = usingFlow ? 'млн м³/сут' : 'МПа'

  return (
    <button
      onClick={() => onSelect(station.id)}
      className="ov-card"
      aria-label={`${station.display_name}: ${statusRunning ? t.running : t.reserve}, ${t.units} ${m.runningCount}/${m.total}`}
      style={{
        textAlign: 'left', cursor: 'pointer', width: '100%',
        display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
        gap: 'var(--space-5)', alignItems: 'start',
        padding: 'var(--space-5) var(--space-6)', borderRadius: 'var(--r-lg)',
        background: 'var(--surface)',
        border: '1px solid var(--line)',
        boxShadow: 'var(--shadow-md)', color: 'var(--text)',
        backdropFilter: 'blur(var(--glass-blur))', transition: 'transform var(--dur-normal) var(--ease-standard), border-color var(--dur-normal) var(--ease-standard), box-shadow var(--dur-normal) var(--ease-standard)',
      }}
    >
      {/* LEFT */}
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 'var(--fs-lg)', fontWeight: 600, letterSpacing: '0.01em', color: 'var(--text-1)' }}>{station.display_name}</div>
        <div style={{ marginTop: 'var(--space-2)' }}>
          {/* Статус — одноцветный (моно-правило): цвет резервируем под CRIT/тепловую карту */}
          <span style={{
            display: 'inline-flex', alignItems: 'center', gap: 'var(--space-1)', padding: 'var(--space-1) var(--space-3)', borderRadius: 999,
            fontSize: 'var(--fs-xs)', fontFamily: 'var(--font-mono)', letterSpacing: '0.04em',
            background: 'var(--surface-2)',
            color: 'var(--text-2)',
            border: '1px solid var(--line)',
          }}>
            <span style={{ width: 6, height: 6, borderRadius: 999, background: 'var(--text-3)', opacity: statusRunning ? 1 : 0.5 }} />
            {statusRunning ? t.running : t.reserve}
          </span>
        </div>
        <div style={{ marginTop: 'var(--space-5)' }}>
          <div style={{ fontSize: 'var(--fs-xs)', letterSpacing: '0.14em', textTransform: 'uppercase', color: 'var(--text-3)', fontFamily: 'var(--font-display)' }}>{metricLabel}</div>
          <div style={{ fontSize: 'var(--fs-xl)', fontWeight: 700, lineHeight: 1.05, fontFamily: 'var(--font-mono)', color: 'var(--text-1)' }}>
            {fmt(metricVal, 2)}
            {metricVal != null && (
              <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-3)', marginLeft: 'var(--space-2)', fontWeight: 400 }}>{metricUnit}</span>
            )}
          </div>
        </div>
      </div>
      {/* CENTER spark */}
      <div style={{ minWidth: 0, alignSelf: 'center' }}>
        <Sparkline data={spark} />
      </div>
      {/* RIGHT tiles */}
      <div style={{ display: 'flex', gap: 'var(--space-3)' }}>
        <Tile label={t.units} value={`${m.runningCount}/${m.total}`} />
        <Tile label="Pвх" value={fmt(m.pIn, 2)} unit="МПа" />
        <Tile label="Pвых" value={fmt(m.pOut, 2)} unit="МПа" />
      </div>
    </button>
  )
}

// ── overview screen ─────────────────────────────────────────────────────────────
export function StationOverview({ lang, onSelect, onLangChange, onThemeToggle }: StationOverviewProps) {
  const t = T[lang]
  const { data: stations = [], isLoading, isError, refetch } = useQuery({
    queryKey: ['stations'],
    queryFn: () => api.stations(),
    refetchInterval: 60_000,
  })

  // Обновляем заголовок вкладки при отображении экрана обзора
  useEffect(() => {
    const prev = document.title
    document.title = 'КС — Компрессорные станции'
    return () => { document.title = prev }
  }, [])

  // Staggered вход карточек станций при загрузке (bklit-style reveal).
  // Срабатывает только когда stations появился первый раз (длина > 0).
  const cardsRef = useRef<HTMLDivElement>(null)
  const staggeredRef = useRef(false)
  useEffect(() => {
    if (!cardsRef.current || !stations.length || staggeredRef.current || prefersReducedMotion()) return
    staggeredRef.current = true
    const cards = cardsRef.current.querySelectorAll('.ov-card')
    if (!cards.length) return
    // Ставим начальное состояние синхронно — нет мигания
    for (const el of cards) {
      (el as HTMLElement).style.opacity = '0'
    }
    animate(cards, {
      opacity: [0, 1],
      translateY: [18, 0],
      duration: 440,
      ease: 'outCubic',
      delay: stagger(90),
    })
  }, [stations.length])

  return (
    <div style={{
      minHeight: '100vh', width: '100%', overflowY: 'auto',
      background: 'var(--app-grad)',
      color: 'var(--text)', fontFamily: 'var(--font-display)',
    }}>
      {/* top nav */}
      <header style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: 'var(--space-4) var(--space-6)', position: 'sticky', top: 0, zIndex: 5,
        background: 'var(--surface)', backdropFilter: 'blur(var(--glass-blur))',
        borderBottom: '1px solid var(--line)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontWeight: 700, fontSize: 'var(--fs-md)', letterSpacing: '0.06em', color: 'var(--text-1)' }}>
          <span aria-hidden style={{ color: 'var(--accent)', fontSize: 'var(--fs-lg)' }}>🜂</span> UTG
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
          <div style={{ display: 'flex', border: '1px solid var(--line-2)', borderRadius: 999, overflow: 'hidden', fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-sm)' }}>
            {(['UZ', 'RU'] as const).map(l => (
              <button key={l} onClick={() => onLangChange(l)} aria-pressed={lang === l} aria-label={`Язык: ${l}`} style={{
                padding: 'var(--space-1) var(--space-4)', border: 'none', cursor: 'pointer',
                background: lang === l ? 'var(--text-1)' : 'transparent',
                color: lang === l ? 'var(--bg)' : 'var(--text-2)', fontWeight: lang === l ? 700 : 400,
              }}>{l}</button>
            ))}
          </div>
          <button onClick={onThemeToggle} title={t.theme} aria-label={t.theme} style={navIconStyle}>☀</button>
          <button title={t.notifications} aria-label={t.notifications} style={navIconStyle}>🔔</button>
          <button title={t.profile} aria-label={t.profile} style={navIconStyle}>☰</button>
        </div>
      </header>

      <main style={{ maxWidth: 1280, margin: '0 auto', padding: 'var(--space-5) var(--space-6) var(--space-6)' }}>
        <h1 style={{ fontSize: 'var(--fs-xl)', fontWeight: 600, margin: 'var(--space-2) 0 var(--space-5)', letterSpacing: '0.01em', color: 'var(--text-1)' }}>{t.stations}</h1>
        <div ref={cardsRef} style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-5)' }}>
          {isLoading && stations.length === 0 ? (
            // Skeleton: 3 карточки-заглушки повторяют форму StationCard
            <>
              {[0, 1, 2].map(i => (
                <div
                  key={i}
                  aria-hidden="true"
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'minmax(220px,1.1fr) 1.6fr minmax(280px,1.1fr)',
                    gap: 'var(--space-6)',
                    alignItems: 'center',
                    padding: 'var(--space-5) var(--space-6)',
                    borderRadius: 'var(--r-lg)',
                    background: 'var(--surface)',
                    border: '1px solid var(--line)',
                    boxShadow: 'var(--shadow-md)',
                    animation: 'ov-skeleton-pulse 1.4s ease-in-out infinite',
                    animationDelay: `${i * 120}ms`,
                  }}
                >
                  {/* LEFT skeleton */}
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
                    <div style={{ height: 20, width: '70%', borderRadius: 'var(--r-sm)', background: 'var(--surface-2)' }} />
                    <div style={{ height: 14, width: '45%', borderRadius: 'var(--r-sm)', background: 'var(--surface-2)' }} />
                    <div style={{ marginTop: 'var(--space-3)', height: 16, width: '55%', borderRadius: 'var(--r-sm)', background: 'var(--surface-2)' }} />
                    <div style={{ height: 28, width: '60%', borderRadius: 'var(--r-sm)', background: 'var(--surface-2)' }} />
                  </div>
                  {/* CENTER skeleton sparkline */}
                  <div style={{ height: 48, borderRadius: 'var(--r-sm)', background: 'var(--surface-2)' }} />
                  {/* RIGHT skeleton tiles */}
                  <div style={{ display: 'flex', gap: 'var(--space-3)' }}>
                    {[0, 1, 2].map(j => (
                      <div key={j} style={{ flex: 1, height: 58, borderRadius: 'var(--r-md)', background: 'var(--surface-2)', border: '1px solid var(--line)' }} />
                    ))}
                  </div>
                </div>
              ))}
              <style>{`
                @keyframes ov-skeleton-pulse {
                  0%, 100% { opacity: 1; }
                  50% { opacity: 0.55; }
                }
                @media (prefers-reduced-motion: reduce) {
                  [style*="ov-skeleton-pulse"] { animation: none !important; }
                }
              `}</style>
            </>
          ) : isError && stations.length === 0 ? (
            <div role="alert" style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', color: 'var(--text-2)', fontFamily: 'var(--font-mono)' }}>
              <span style={{ color: 'var(--crit)' }}>⚠ {t.error}</span>
              <button onClick={() => refetch()} style={{ ...navIconStyle, width: 'auto', padding: 'var(--space-1) var(--space-3)' }}>{t.retry}</button>
            </div>
          ) : stations.length === 0 ? (
            // Styled empty state
            <div
              role="status"
              aria-live="polite"
              style={{
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                gap: 'var(--space-3)', padding: 'var(--space-6)',
                border: '1px dashed var(--line)', borderRadius: 'var(--r-lg)',
                background: 'var(--surface)', minHeight: 180,
              }}
            >
              <span aria-hidden="true" style={{ fontSize: 'var(--fs-xl)', opacity: 0.35, color: 'var(--text-3)' }}>⊘</span>
              <div style={{ fontSize: 'var(--fs-md)', fontWeight: 600, color: 'var(--text-2)', fontFamily: 'var(--font-display)' }}>Нет данных</div>
              <div style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-3)', fontFamily: 'var(--font-mono)', textAlign: 'center', maxWidth: 340 }}>{t.notFound}</div>
            </div>
          ) : (
            stations.map(st => <StationCard key={st.id} station={st} onSelect={onSelect} lang={lang} />)
          )}
        </div>
      </main>
    </div>
  )
}

const navIconStyle: React.CSSProperties = {
  width: 34, height: 34, borderRadius: 'var(--r-md)', border: '1px solid var(--line)',
  background: 'var(--surface-2)', color: 'var(--text-2)', cursor: 'pointer', fontSize: 'var(--fs-sm)',
  transition: 'background-color var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard)',
}
