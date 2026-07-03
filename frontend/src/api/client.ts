import type {
  SensorMeta, EventItem, StatsResponse,
  SensorChartResponse, HeatmapCell, StationInfo,
  MultiSeriesItem, NotificationItem, SensorExplain, GraphSet,
} from '../types'

const BASE = import.meta.env.VITE_API_URL ?? ''

// Типизированные подклассы ошибок для точечной обработки в UI
export class OfflineError extends Error {
  constructor() { super('нет соединения'); this.name = 'OfflineError' }
}
export class UnauthorizedError extends Error {
  constructor(detail?: string) { super(detail ?? 'нет доступа (401)'); this.name = 'UnauthorizedError' }
}
export class ForbiddenError extends Error {
  constructor(detail?: string) { super(detail ?? 'доступ запрещён (403)'); this.name = 'ForbiddenError' }
}
export class NotFoundError extends Error {
  constructor(detail?: string) { super(detail ?? 'ресурс не найден (404)'); this.name = 'NotFoundError' }
}

// Извлекаем тело ошибки (FastAPI отдаёт {detail: ...}) — иначе оператор и логи видели
// только статус без причины инцидента на КС.
async function errorFrom(res: Response): Promise<Error> {
  let detail = ''
  try {
    const txt = await res.text()
    if (txt) {
      try {
        const j = JSON.parse(txt)
        detail = typeof j?.detail === 'string' ? j.detail : j?.detail != null ? JSON.stringify(j.detail) : txt
      } catch { detail = txt }
    }
  } catch { /* тело недоступно */ }
  const msg = detail ? detail.slice(0, 300) : undefined
  if (res.status === 401) return new UnauthorizedError(msg)
  if (res.status === 403) return new ForbiddenError(msg)
  if (res.status === 404) return new NotFoundError(msg)
  return new Error(`${res.status} ${res.statusText}${msg ? ` — ${msg}` : ''}`)
}

async function get<T>(path: string, params?: Record<string, string | number>): Promise<T> {
  let fullPath = BASE + path
  if (params) {
    const qs = new URLSearchParams(
      Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)]))
    ).toString()
    fullPath += '?' + qs
  }
  let res: Response
  try {
    res = await fetch(fullPath)
  } catch (err) {
    // TypeError = сеть недоступна (офлайн, DNS, CORS preflight fail)
    if (err instanceof TypeError) throw new OfflineError()
    throw err
  }
  if (!res.ok) throw await errorFrom(res)
  return res.json() as Promise<T>
}

const DEFAULT_STATION = 'ohangaron'

export const api = {
  stations: () =>
    get<StationInfo[]>('/api/stations'),

  sensors: (stationId = DEFAULT_STATION, gpa?: string) =>
    get<SensorMeta[]>(`/api/stations/${stationId}/sensors`, gpa ? { gpa } : undefined),

  sensor: (id: string, stationId = DEFAULT_STATION) =>
    get<SensorMeta>(`/api/stations/${stationId}/sensors/${encodeURIComponent(id)}`),

  sensorChart: (
    id: string,
    opts: number | { days?: number; t0?: string; t1?: string } = 30,
    stationId = DEFAULT_STATION,
  ) => {
    const raw = typeof opts === 'number' ? { days: opts } : opts
    const params = Object.fromEntries(
      Object.entries(raw).filter(([, v]) => v !== undefined)
    ) as Record<string, string | number>
    return get<SensorChartResponse>(`/api/stations/${stationId}/sensors/${encodeURIComponent(id)}/chart`, params)
  },

  stats: (stationId = DEFAULT_STATION) =>
    get<StatsResponse>(`/api/stations/${stationId}/stats`),

  events: (opts?: { severity?: string; gpa?: string; kind?: string; limit?: number; days?: number }, stationId = DEFAULT_STATION) => {
    const params = opts
      ? Object.fromEntries(Object.entries(opts).filter(([, v]) => v !== undefined)) as Record<string, string | number>
      : undefined
    return get<EventItem[]>(`/api/stations/${stationId}/events`, params)
  },

  heatmap: (stationId = DEFAULT_STATION, gpa?: string) =>
    get<HeatmapCell[]>(`/api/stations/${stationId}/heatmap`, gpa ? { gpa } : undefined),

  multiChart: (
    sensors: string[],
    opts: number | { days?: number; t0?: string; t1?: string } = 30,
    stationId = DEFAULT_STATION,
  ) => {
    const raw = typeof opts === 'number' ? { days: opts } : opts
    const params: Record<string, string | number> = { sensors: sensors.join(',') }
    for (const [k, v] of Object.entries(raw)) if (v !== undefined) params[k] = v as string | number
    return get<MultiSeriesItem[]>(`/api/stations/${stationId}/chart/multi`, params)
  },

  notifications: (
    opts?: { status?: string; severity?: string; sensor_id?: string; days?: number; limit?: number },
    stationId = DEFAULT_STATION,
  ) => {
    const params = opts
      ? Object.fromEntries(Object.entries(opts).filter(([, v]) => v !== undefined)) as Record<string, string | number>
      : undefined
    return get<NotificationItem[]>(`/api/stations/${stationId}/notifications`, params)
  },

  ackNotification: (nid: number, status = 'ack', stationId = DEFAULT_STATION) =>
    fetch(`${BASE}/api/stations/${stationId}/notifications/${nid}/ack?status=${encodeURIComponent(status)}`,
      { method: 'POST' }).then(async r => {
        if (!r.ok) throw await errorFrom(r)
        return r.json() as Promise<{ id: number; status: string }>
      }),

  // Серверное квитирование показываемого события (из live_state) → статус строки журнала
  // по ключу (sensor_id, момент). Делает ack видимым всем операторам/машинам.
  ackEvent: (sensorId: string, timestamp: string, kind: string | undefined, stationId = DEFAULT_STATION) =>
    fetch(`${BASE}/api/stations/${stationId}/events/ack`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sensor_id: sensorId, timestamp, kind }),
    }).then(async r => { if (!r.ok) throw await errorFrom(r); return r.json() as Promise<{ acked: number; status: string }> }),

  // «Важные признаки»: SHAP-вкладчики конкретной аномалии + их ряды
  explain: (id: string, t: string, hours = 6, stationId = DEFAULT_STATION) =>
    get<SensorExplain>(`/api/stations/${stationId}/sensors/${encodeURIComponent(id)}/explain`, { t, hours }),

  // Region SHAP: топ-5 вкладчиков на участке [t0,t1] (+ опц. диапазон значений [v0,v1])
  explainRegion: (id: string, t0: string, t1: string, v0?: number, v1?: number, hours = 6, stationId = DEFAULT_STATION) => {
    const params: Record<string, string | number> = { t0, t1, hours }
    if (v0 != null) params.v0 = v0
    if (v1 != null) params.v1 = v1
    return get<SensorExplain>(`/api/stations/${stationId}/sensors/${encodeURIComponent(id)}/explain`, params)
  },

  // Последний срез значений тегов (SCADA) — для метрик обзора станций
  pvsnapshot: (stationId = DEFAULT_STATION) =>
    get<Record<string, unknown>>(`/api/stations/${stationId}/pvsnapshot`),

  // ── Сохранённые наборы датчиков (подборки для сравнения, на пользователя) ──
  graphSets: (owner: string, stationId = DEFAULT_STATION) =>
    get<GraphSet[]>(`/api/stations/${stationId}/graph-sets`, { owner }),

  saveGraphSet: (owner: string, name: string, sensorIds: string[], stationId = DEFAULT_STATION) =>
    fetch(`${BASE}/api/stations/${stationId}/graph-sets?owner=${encodeURIComponent(owner)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, sensor_ids: sensorIds }),
    }).then(async r => { if (!r.ok) throw await errorFrom(r); return r.json() as Promise<GraphSet> }),

  deleteGraphSet: (owner: string, setId: number, stationId = DEFAULT_STATION) =>
    fetch(`${BASE}/api/stations/${stationId}/graph-sets/${setId}?owner=${encodeURIComponent(owner)}`, { method: 'DELETE' })
      .then(async r => { if (!r.ok) throw await errorFrom(r); return r.json() as Promise<{ deleted: boolean }> }),
}
