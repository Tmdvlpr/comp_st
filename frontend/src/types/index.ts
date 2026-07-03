export type Severity = 'crit' | 'warn' | 'info' | 'ok'
// + drift (8, медленный дрейф остатка) и index (9, отклонение доменного индекса)
export type AnomalyKind = 'ml' | 'frozen' | 'neg' | 'roc' | 'seasonal' | 'regime' | 'cross' | 'drift' | 'index'

export interface SensorMeta {
  id: string
  name: string
  gpa: string
  tag: string
  r2: number
  mae: number
  cur?: number | null
  anomaly_count: number
  anomaly_count_30d?: number | null
  anomaly_types: AnomalyKind[]
  severity: Severity
  subsystem: string
  // ── аналитика research-методологии (опционально; заполняется API Ф2) ──
  nmae?: number | null
  rmse?: number | null
  r2_val?: number | null
  r2_insample?: number | null
  best_model?: string | null
  drift_score?: number | null
  // реальная пер-сенсорная аналитика (DetailPanel)
  drift?: {
    score?: number | null; trend?: number | null; reversibility?: string | null
    cusum?: boolean | null; ph?: boolean | null; fired?: boolean | null
  } | null
  calibration?: {
    conformal_thr?: number | null; pot_thr?: number | null
    n_sigma_cal?: number | null; n_sigma?: number | null
    coverage?: number | null; alarm_rate?: number | null
  } | null
  domain?: Record<string, number> | null
  // ── v2 (опц.): режим детекции и текущий режим работы ──
  detector_mode?: string | null   // ml_corridor | univariate_only | legacy
  regime?: string | null          // текущий regime_key (steady|mainline|L0 …)
}

// Карточка кейса аномалии (база интерпретаций; в проде — таблица anomaly_cases)
export interface CaseInfo {
  feats: [string, string, string][]   // [имя, вклад, цвет] — параметры-вкладчики (SHAP)
  diag: string                        // что произошло
  cause: string                       // вероятная причина
  check: string[]                     // что проверить
  similar: string[]                   // похожие исторические кейсы
  sev: Severity
  sevl: string                        // подпись «CRIT · ML»
}

// Вариации доменного индекса (база; в проде — таблица index_variations)
export interface IndexInfo {
  name: string
  val: string
  unit: string
  status: Severity
  norm: string
  now: string                         // интерпретация текущего состояния
  vars: [string, string][]            // [условие, что значит/действие]
}

// Ответ /sensors/{id}/explain?t= — локальная атрибуция аномалии (SHAP)
export interface SensorExplain {
  sensor_id: string
  event_ts: string
  actual?: number | null
  expected?: number | null
  contributors: { name: string; contrib: number; series: { t: string; v: number }[] }[]
  kind?: AnomalyKind
  severity?: Severity
  reversibility?: string
  // ряд самого датчика на окне (наложение «цель vs драйверы») и выбранный участок
  target_series?: { t: string; v: number }[] | null
  region?: { t0: string; t1: string; v0?: number | null; v1?: number | null } | null
}

export interface EventItem {
  id: string
  timestamp: string
  sensor_id: string
  sensor_name: string
  gpa: string
  kind: AnomalyKind
  severity: Severity
  value?: number
  deviation?: number
  description: string
  acked: boolean
}

export interface StatsResponse {
  total_sensors: number
  crit_count: number
  warn_count: number
  info_count: number
  ok_count: number
  ml_count: number
  frozen_count: number
  neg_count: number
  regime_count: number
  roc_count: number
  seasonal_count: number
  cross_count: number
  drift_count?: number
  total_anomalies: number
  last_updated: string
}

export interface TimeSeriesPoint {
  t: string
  v: number
  p: number | null
  lo: number | null
  hi: number | null
  /** Альтернативный коридор (hybrid) для тумблера conformal↔hybrid */
  lo2?: number | null
  hi2?: number | null
  /** Эпистемическая неопр. u_epi (детектор-2, фиолет-полоса под графиком); null вне окна модели */
  e?: number | null
}

export interface AnomalyPoint {
  t: string
  v: number
  kind: AnomalyKind
  severity: Severity
}

export interface SensorChartResponse {
  sensor_id: string
  tag: string
  r2: number
  mae: number
  current: number | null
  predicted: number | null
  deviation: number | null
  train_ts?: string | null
  series: TimeSeriesPoint[]
  anomalies: AnomalyPoint[]
  /** Порог новизны κ·1.5 (пунктирная линия на полосе эпистемики); null если нет эталона healthy */
  epistemic_thr?: number | null
  /** Режим активного коридора (lo/hi): 'conformal'|'hybrid'; lo2/hi2 = альтернативный */
  corridor_mode?: string | null
}

export interface HeatmapCell {
  sensor_id: string
  name: string
  gpa: string
  severity: Severity
  anomaly_count: number
}

export interface MultiSeriesItem {
  sensor_id: string
  name: string
  tag: string
  gpa: string
  unit?: string | null
  range_min?: number | null
  range_max?: number | null
  series: { t: string; v: number }[]
}

// Сохранённый набор датчиков (подборка для сравнения) — ohangaron.set_of_graphs
export interface GraphSet {
  id: number
  name: string
  sensor_ids: string[]
  updated_at?: string | null
}

export interface NotificationItem {
  id: number
  station_id: string
  sensor_id: string
  point?: string | null
  gpa?: string | null
  event_ts: string
  anomaly_type: number
  kind?: string | null
  severity?: string | null
  value?: number | null
  deviation?: number | null
  message: string
  status: string
  created_at: string
}

export const KIND_LABEL: Record<AnomalyKind, string> = {
  ml:       'Стат. выброс',
  frozen:   'Датчик завис',
  neg:      'Сбой физичности',
  roc:      'Скачок ΔV',
  seasonal: 'Сезонная аномалия',
  regime:   'Смена режима',
  cross:    'Кросс-ГПА отклонение',
  drift:    'Дрейф (деградация)',
  index:    'Индекс здоровья',
}

export const KIND_SEVERITY: Record<AnomalyKind, Severity> = {
  ml:       'crit',
  neg:      'crit',
  frozen:   'warn',
  roc:      'warn',
  seasonal: 'info',
  regime:   'info',
  cross:    'info',
  drift:    'warn',
  index:    'warn',
}

export const SEV_LABEL: Record<Severity, string> = {
  crit: 'Критично',
  warn: 'Предупрежд.',
  info: 'Инфо',
  ok:   'Норма',
}

// Развёрнутые операторские подписи (легенда теплокарты и подсказки ячеек) — единый
// источник, чтобы легенда и aria-label/title ячеек не расходились в названиях.
export const SEV_LABEL_LONG: Record<Severity, string> = {
  crit: 'Суперважно',
  warn: 'Внимание',
  info: 'Наблюдение',
  ok:   'Норма',
}

// SEV_COLOR использует CSS-переменные — единый источник правды в globals.css.
// Для изменения цветов правьте только globals.css (:root / .light).
export const SEV_COLOR: Record<Severity, string> = {
  crit: 'var(--crit)',
  warn: 'var(--warn)',
  info: 'var(--info)',
  ok:   'var(--ok)',
}

// Цвет ТЕКСТА на СПЛОШНОМ фоне severity (пиллы киоска): белый на красном — ок, но на
// жёлтом/зелёном он нечитаем (контраст <3:1) → тёмный текст. Чинит WCAG-контраст пиллов.
// info: белый на --info (#8C93B0 / #6b7494) даёт ~3.1:1 — ниже AA (4.5:1).
//       Заменён на тёмный (#1a1f3a), который даёт ≥7:1 на обоих оттенках info.
// Значения — статичные hex для inline style (не CSS-переменные), т.к. используются
// в runtime без доступа к computed styles. warn/ok имеют контрастные тёмные пары
// без соответствующих токенов в globals.css — добавить токены при рефакторе пиллов.
export const SEV_TEXT_ON_SOLID: Record<Severity, string> = {
  crit: '#ffffff',
  warn: '#241a05',
  info: '#1a1f3a',
  ok:   '#06230f',
}

export interface StationInfo {
  id: string
  display_name: string
  enabled: boolean
  units: string[]
  live_data: boolean
  last_updated: string | null
}
