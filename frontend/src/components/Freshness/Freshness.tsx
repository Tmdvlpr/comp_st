import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'

interface FreshnessProps {
  lastUpdated?: string | null
}

const STALE_MIN = 30
const WARN_MIN = 10

const BASE = import.meta.env.VITE_API_URL ?? ''

interface HealthResponse {
  status?: 'ok' | 'degraded' | 'down'
  db?: 'ok' | 'error'
  state_age_seconds?: number
}

// /api/health может вернуть 503 (degraded/down) с JSON-телом — читаем тело при любом статусе.
async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch(BASE + '/api/health')
  try {
    return (await res.json()) as HealthResponse
  } catch {
    // Нет валидного JSON (например, network/proxy ошибка) — считаем недоступным.
    return { status: 'down' }
  }
}

export function Freshness({ lastUpdated }: FreshnessProps) {
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const t = setInterval(() => setTick(v => v + 1), 30_000)
    return () => clearInterval(t)
  }, [])

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 60_000,
    retry: 1,
  })

  // tick keeps the elapsed calculation fresh every 30s
  void tick

  const dbState = health?.db
  const status = health?.status
  const isDown = status === 'down'

  // Если нет ни данных свежести, ни health — нечего показывать.
  if (!lastUpdated && !health) return null

  // Компактно: иконка-часы (цвет = свежесть) + время; чипы ●ML / ●БД. Подробности —
  // в тултипах, критичные состояния (устарели / ML стоит / БД нет) подсвечены красным.
  const dot = (color: string, pulse = false) => (
    <span className={pulse ? 'animate-crit-dot' : undefined} style={{ width: 6, height: 6, borderRadius: '50%', background: color, flexShrink: 0 }} />
  )

  let timeNode: React.ReactNode = null
  if (lastUpdated) {
    const updated = new Date(lastUpdated)
    if (!isNaN(updated.getTime())) {
      const ageMin = Math.max(0, Math.floor((Date.now() - updated.getTime()) / 60_000))
      const hhmm = updated.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
      const stale = ageMin >= STALE_MIN
      const clockColor = stale ? 'var(--crit)' : ageMin >= WARN_MIN ? 'var(--warn)' : 'var(--ok)'
      const ageText = ageMin < 1 ? 'только что' : `${ageMin} мин назад`
      timeNode = (
        <span
          className="inline-flex items-center gap-[5px]"
          title={`Данные обновлены: ${updated.toLocaleString('ru-RU')} · ${ageText}${stale ? ' · УСТАРЕЛИ' : ''}`}
          style={{ flexShrink: 0 }}
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" style={{ color: clockColor, flexShrink: 0 }}>
            <circle cx="6" cy="6" r="4.6" stroke="currentColor" strokeWidth="1.2" />
            <path d="M6 3.4V6l1.8 1.1" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span style={{ color: stale ? 'var(--crit)' : 'var(--text-2)', fontWeight: stale ? 700 : 400 }}>{hhmm}</span>
          {stale && <span style={{ color: 'var(--crit)', fontWeight: 700 }}>!</span>}
        </span>
      )
    }
  }

  return (
    <span
      className="inline-flex items-center gap-[10px] font-mono"
      style={{ fontSize: 'var(--fs-xs)', letterSpacing: '0.04em', color: 'var(--text-3)', flexShrink: 0 }}
    >
      {timeNode}

      {status && (
        <span
          className="inline-flex items-center gap-[4px]"
          title={isDown ? 'ML-движок остановлен' : 'ML-движок работает'}
          style={{ color: isDown ? 'var(--crit)' : 'var(--text-3)', fontWeight: isDown ? 700 : 400, flexShrink: 0 }}
        >
          {dot(isDown ? 'var(--crit)' : 'var(--ok)', isDown)}ML
        </span>
      )}

      {dbState && (
        <span
          className="inline-flex items-center gap-[4px]"
          title={dbState === 'ok' ? 'База данных доступна' : 'База данных недоступна'}
          style={{ color: dbState === 'error' ? 'var(--crit)' : 'var(--text-3)', fontWeight: dbState === 'error' ? 700 : 400, flexShrink: 0 }}
        >
          {dot(dbState === 'ok' ? 'var(--ok)' : 'var(--crit)', dbState === 'error')}БД
        </span>
      )}
    </span>
  )
}
