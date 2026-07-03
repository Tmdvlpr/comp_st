import { useState, useMemo, useEffect, useRef } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { animate } from 'animejs'
import { prefersReducedMotion } from '../../lib/motion'
import type { SensorMeta, EventItem, IndexInfo, Severity } from '../../types'
import { KIND_LABEL, SEV_COLOR, SEV_LABEL } from '../../types'
import { ruSensor } from '../../lib/sensorLabels'
import { fmtStation, stationYMD } from '../../lib/time'

// ── props ──────────────────────────────────────────────────────────────────
interface DetailPanelProps {
  sensor: SensorMeta | null
  events?: EventItem[]
  onSelectEvent?: (ev: EventItem) => void
  /** Навести график на день (YYYY-MM-DD) — клик по дню без события в ленте severity */
  onFocusDate?: (ymd: string) => void
}

type DetailTab = 'health' | 'quality' | 'drift' | 'calib' | 'idx'

const TABS: [DetailTab, string][] = [
  ['health', 'Здоровье'],
  ['quality', 'Качество модели'],
  ['drift', 'Дрейф'],
  ['calib', 'Калибровка'],
  ['idx', 'Доменные индексы'],
]

// ── доменные индексы (база; 1:1 с макетом IDX_BASE) ──────────────────────────
// Ровно 10 доменных индексов здоровья, которые бэкенд отдаёт по каждому ГПА
// (metadata['health_index'] → sensor.domain). Прежде показывалось 6, причём
// rpm_tvd_red не входит в health_index и реальных данных по нему не приходит
// (всегда рисовался дефолт) — убран; вместо него — 5 реальных недостающих.
const INDEX_ORDER: string[] = [
  'polytropic_eff',
  'polytropic_head',
  'dT_disch',
  'dT_cooler',
  'avo_approach',
  'shaft_resid_tnd',
  'shaft_resid_st',
  'shaft_ratio',
  'specific_fuel',
  'dT_bearings',
]

const INDEX_BASE: Record<string, IndexInfo> = {
  polytropic_eff: {
    name: 'Политропный КПД η_p',
    val: '0.81',
    unit: '',
    status: 'ok',
    norm: '0.78–0.85',
    now: 'В норме — нагнетатель преобразует энергию эффективно.',
    vars: [
      ['< 0.75', 'загрязнение/эрозия проточной части, износ уплотнений → промывка, осмотр'],
      ['резкое падение', 'скол лопатки / посторонний предмет / помпаж'],
      ['медленный дрейф ↓', 'постепенная деградация (тренд недели) → планировать ревизию'],
    ],
  },
  polytropic_head: {
    name: 'Политропный напор H_p',
    val: '122',
    unit: 'кДж/кг',
    status: 'ok',
    norm: '100–140 @ режим',
    now: 'Соответствует степени сжатия и оборотам — норма.',
    vars: [
      ['↓ при тех же оборотах', 'падение производительности ступени, рециркуляция'],
      ['↑ резко', 'смена режима / рост противодавления в магистрали'],
      ['нестабилен', 'неустойчивая работа, близость к помпажу'],
    ],
  },
  shaft_resid_tnd: {
    name: 'Shaft mismatch (ТНД=f(ТВД))',
    val: '+38',
    unit: 'об/мин',
    status: 'warn',
    norm: '±20',
    now: 'Повышенный рассинхрон каскадов ВД/НД — отклонение от линии совместной работы.',
    vars: [
      ['|резид| > 20', 'нарушение совместной работы каскадов, дисбаланс нагрузки'],
      ['устойчивый рост', 'деградация турбины НД / проблемы топливоподачи'],
      ['скачок', 'смена режима / переходный процесс (легально)'],
    ],
  },
  specific_fuel: {
    name: 'Удельный расход топлива',
    val: '0.0127',
    unit: '',
    status: 'ok',
    norm: '0.011–0.014',
    now: 'Топливо на единицу полезной работы — в норме.',
    vars: [
      ['↑ при том же H_p', 'снижение КПД ГТД/нагнетателя, рост потерь'],
      ['устойчивый рост', 'деградация ГТД (загрязнение компрессора, износ горячей части)'],
      ['↓ нефизично', 'ошибка расходомера / H_p'],
    ],
  },
  dT_bearings: {
    name: 'ΔT подшипников',
    val: '+2.1',
    unit: '°C',
    status: 'ok',
    norm: '−5…+5',
    now: 'Тепловая симметрия подшипников в норме.',
    vars: [
      ['|ΔT| > 5', 'асимметрия теплоотвода, начальный дисбаланс/расцентровка'],
      ['рост одной опоры', 'деградация смазки/подшипника на этой опоре'],
      ['скачок', 'срыв масляной плёнки → проверить маслосистему'],
    ],
  },
  dT_disch: {
    name: 'Нагрев в нагнетателе ΔT',
    val: '34',
    unit: '°C',
    status: 'ok',
    norm: 'по режиму',
    now: 'Подъём температуры газа от всаса к нагнетанию — соответствует степени сжатия и КПД.',
    vars: [
      ['↑ при той же π', 'падение политропного КПД (нагрев вместо полезной работы) → см. η_p'],
      ['↓ при той же π', 'возможна ошибка датчиков T1/T2 или нефизичный режим'],
      ['нестабилен', 'неустойчивая работа ступени / переходный режим'],
    ],
  },
  dT_cooler: {
    name: 'Перепад на АВО (range)',
    val: '18',
    unit: '°C',
    status: 'ok',
    norm: 'по нагрузке/погоде',
    now: 'Сколько тепла снимает АВО с газа (T на выходе ГПА − T после АВО) — теплосъём в норме.',
    vars: [
      ['↓ устойчиво', 'загрязнение секций АВО / отказ вентиляторов → осмотр, чистка'],
      ['↓ летом', 'частично обратимо — высокая T воздуха снижает теплосъём (см. approach)'],
      ['скачок', 'смена режима обдува / переходный процесс'],
    ],
  },
  avo_approach: {
    name: 'Approach АВО',
    val: '12',
    unit: '°C',
    status: 'ok',
    norm: '8–20 @ погода',
    now: 'Недоохлаждение: насколько газ после АВО теплее наружного воздуха — запас охлаждения есть.',
    vars: [
      ['↑ устойчиво', 'деградация теплообмена АВО (загрязнение, оребрение, вентиляторы)'],
      ['↑ в жару', 'обратимо — мал напор по ΔT с горячим воздухом'],
      ['< 0 нефизично', 'ошибка датчика T воздуха / T газа после АВО'],
    ],
  },
  shaft_resid_st: {
    name: 'Остаток СТ (СТ=f(ТВД,ТНД))',
    val: '+15',
    unit: 'об/мин',
    status: 'ok',
    norm: '±30',
    now: 'Отклонение оборотов силовой турбины от линии совместной работы каскадов — в норме.',
    vars: [
      ['|резид| > 30', 'рассогласование СТ с газогенератором, дисбаланс нагрузки'],
      ['устойчивый рост', 'деградация СТ / проточной части нагнетателя'],
      ['скачок', 'смена режима / переходный процесс (легально)'],
    ],
  },
  shaft_ratio: {
    name: 'Отношение оборотов ТНД/ТВД',
    val: '0.92',
    unit: '',
    status: 'ok',
    norm: 'по режиму',
    now: 'Соотношение оборотов каскадов НД и ВД — соответствует заданному режиму.',
    vars: [
      ['дрейф от режимной линии', 'изменение совместной работы каскадов → см. shaft mismatch'],
      ['устойчивый рост/падение', 'деградация одного из каскадов / топливоподачи'],
      ['скачок', 'смена режима / переходный процесс'],
    ],
  },
}

// ── токены/палитра (из globals.css; --drift отсутствует → --teal) ────────────
const DRIFT_COLOR = 'var(--teal)'

// ── статичный стиль заголовка (нет динамических значений — модульная константа,
//    не пересоздаётся при каждом рендере компонента) ───────────────────────────
const HEADER_STYLE: CSSProperties = {
  padding: '9px 14px',
  fontWeight: 600,
  fontSize: 12,
  letterSpacing: '0.1em',
  textTransform: 'uppercase',
  color: 'var(--text-2)',
  borderBottom: '1px solid var(--line)',
  flexShrink: 0,
  display: 'flex',
  alignItems: 'center',
  gap: 'var(--space-2)',
}

const fmtNum = (n: number, digits = 3): string =>
  Number.isFinite(n) ? n.toFixed(digits).replace(/\.?0+$/, '') : '—'

// ── мелкие переиспользуемые блоки ────────────────────────────────────────────
function Metric({ k, v, color }: { k: string; v: string; color?: string }) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'baseline',
        padding: 'var(--space-2) 0',
        borderBottom: '1px solid var(--line)',
      }}
    >
      <span style={{ fontSize: 13, color: 'var(--text-2)' }}>{k}</span>
      <span className="font-mono" style={{ fontSize: 15, fontWeight: 600, color: color ?? 'var(--text-1)' }}>
        {v}
      </span>
    </div>
  )
}

function SectionTitle({ children, mt = 12 }: { children: ReactNode; mt?: number }) {
  return (
    <h3
      style={{
        fontSize: 12,
        fontWeight: 600,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        color: 'var(--text-2)',
        marginBottom: 'var(--space-2)',
        marginTop: mt,
      }}
    >
      {children}
    </h3>
  )
}

function Note({ children }: { children: ReactNode }) {
  return (
    <div className="font-mono" style={{ marginTop: 10, fontSize: 12, color: 'var(--text-3)', lineHeight: 1.45 }}>
      {children}
    </div>
  )
}

// Глоссарий «Как читать» — заполняет пустое пространство под метриками простым
// объяснением каждого показателя на языке оператора (что это и куда смотреть).
function HowToRead({ items }: { items: [string, ReactNode][] }) {
  return (
    <div style={{ marginTop: 18, paddingTop: 12, borderTop: '1px dashed var(--line)' }}>
      <div style={{ fontSize: 12, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--text-3)', marginBottom: 8 }}>
        Как читать
      </div>
      {items.map(([term, desc]) => (
        <div key={term} style={{ margin: '7px 0', fontSize: 12, lineHeight: 1.5, color: 'var(--text-3)' }}>
          <span style={{ color: 'var(--text-2)', fontWeight: 600 }}>{term}</span> — {desc}
        </div>
      ))}
    </div>
  )
}

function Bar({ pct, color }: { pct: number; color: string }) {
  return (
    <div style={{ height: 7, background: 'var(--surface-2)', borderRadius: 4, overflow: 'hidden', marginTop: 4 }}>
      <div style={{ display: 'block', height: '100%', width: `${Math.max(0, Math.min(100, pct))}%`, background: color }} />
    </div>
  )
}

function Gauge({ label, color, sub }: { label: string; color: string; sub: string }) {
  return (
    <div style={{ textAlign: 'center', padding: '6px 0' }}>
      <div className="font-mono" style={{ fontSize: 30, fontWeight: 700, color }}>
        {label}
      </div>
      <div className="font-mono" style={{ color: 'var(--text-3)', fontSize: 12, marginTop: 2 }}>
        {sub}
      </div>
    </div>
  )
}

function IdxCard({ info }: { info: IndexInfo }) {
  const col = SEV_COLOR[info.status]
  return (
    <div
      style={{
        background: 'var(--surface-2)',
        border: '1px solid var(--line)',
        borderRadius: 'var(--r-sm)',
        padding: '10px var(--space-3)',
        marginBottom: 9,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span style={{ fontSize: 13, color: 'var(--text-2)' }}>{info.name}</span>
        <span className="font-mono" style={{ fontSize: 16, fontWeight: 700, color: col }}>
          {info.val}
          {info.unit ? ` ${info.unit}` : ''}
        </span>
      </div>
      <div className="font-mono" style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>
        норма: {info.norm}
      </div>
      <div style={{ fontSize: 12.5, color: 'var(--text-1)', margin: '6px 0', lineHeight: 1.42 }}>{info.now}</div>
      <div style={{ marginTop: 6, borderTop: '1px dashed var(--line)', paddingTop: 7 }}>
        <span style={{ fontSize: 12, color: 'var(--text-3)', letterSpacing: '0.08em' }}>
          ВАРИАЦИИ / ЧТО ЗНАЧИТ ОТКЛОНЕНИЕ:
        </span>
        {info.vars.map(([cond, meaning]) => (
          <div
            key={cond}
            style={{ display: 'flex', gap: 9, fontSize: 12, color: 'var(--text-2)', margin: '4px 0', lineHeight: 1.38 }}
          >
            <span className="font-mono" style={{ flexShrink: 0, minWidth: 104, color: col }}>
              {cond}
            </span>
            <span>{meaning}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── вкладка: Здоровье ─────────────────────────────────────────────────────────
function HealthTab({ sensor, events, onSelectEvent, onFocusDate }: { sensor: SensorMeta; events: EventItem[]; onSelectEvent?: (ev: EventItem) => void; onFocusDate?: (ymd: string) => void }) {
  const tlColors = ['var(--tl-ok)', 'var(--warn)', DRIFT_COLOR, 'var(--crit)']
  const sevLevel = (s: Severity) => (s === 'crit' ? 3 : s === 'warn' ? 2 : s === 'info' ? 1 : 0)
  const DAYS = 30

  // Реальная лента severity · 30 дней: для каждого дня — макс. severity событий
  // датчика + топ-событие дня (для клика). Мемоизируем — пересчёт только при смене
  // событий/датчика, а не на каждый ре-рендер (переключение вкладок панели).
  const { days, todayIdx, cnt30 } = useMemo(() => {
    // Группировка по КАЛЕНДАРНЫМ суткам станции (Etc/GMT-5 = UTC+5), а не по TZ браузера:
    // номер суток = floor((utcMs + 5ч) / 24ч) → граница дня = реальная полночь станции
    // независимо от зоны машины оператора.
    const OFFSET = 5 * 3_600_000
    const MS = 86_400_000
    const dayIdx = (utcMs: number) => Math.floor((utcMs + OFFSET) / MS)
    const arr = Array.from({ length: DAYS }, () => ({ sev: 0, top: null as EventItem | null }))
    const today = dayIdx(Date.now())
    let n = 0
    for (const ev of events) {
      const back = today - dayIdx(new Date(ev.timestamp).getTime())
      const idx = DAYS - 1 - back
      if (idx < 0 || idx >= DAYS) continue
      n++
      const lv = sevLevel(ev.severity)
      if (lv >= arr[idx].sev) { arr[idx].sev = lv; arr[idx].top = ev }
    }
    return { days: arr, todayIdx: today, cnt30: n }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events, sensor.id])

  const gaugeColor = SEV_COLOR[sensor.severity]
  const gaugeLabel = SEV_LABEL[sensor.severity]
  const cur = sensor.cur != null ? fmtNum(sensor.cur) : '—'
  const codeCount = (kinds: string[]) => events.filter(e => kinds.includes(e.kind)).length

  const recent = events
    .slice()
    .sort((a, b) => b.timestamp.localeCompare(a.timestamp))
    .slice(0, 5)

  return (
    <>
      <Gauge
        label={gaugeLabel}
        color={gaugeColor}
        sub={`${sensor.anomaly_count} аномалий за 24ч · ${sensor.anomaly_count_30d ?? cnt30} за 30д`}
      />
      <Metric k="Текущее значение" v={cur} />

      <SectionTitle mt={12}>Коды здоровья (по событиям)</SectionTitle>
      <Metric k="ml (выброс)" v={String(codeCount(['ml']))} />
      <Metric k="roc (скачок)" v={String(codeCount(['roc']))} />
      <Metric k="drift (дрейф)" v={String(codeCount(['drift']))} />

      <SectionTitle mt={14}>История severity · 30 дней <span style={{ fontSize: 12, color: 'var(--text-3)', textTransform: 'none', letterSpacing: 0 }}>(клик: событие дня / переход к дню)</span></SectionTitle>
      <div style={{ display: 'flex', gap: 2, margin: '6px 0 2px' }}>
        {days.map((d, i) => {
          // UTC-момент станционной полуночи этого дня: idx*24ч − 5ч
          const dayMs = (todayIdx - (DAYS - 1 - i)) * 86_400_000 - 5 * 3_600_000
          const label = fmtStation(dayMs, { day: '2-digit', month: '2-digit' })
          const ymd = stationYMD(dayMs)
          // Кликабелен ЛЮБОЙ день: есть событие → открыть его; иначе → навести график
          // на этот день (раньше пустые дни не реагировали — «ничего не отображалось»).
          const clickable = !!(d.top ? onSelectEvent : onFocusDate)
          const onClick = clickable
            ? () => (d.top && onSelectEvent ? onSelectEvent(d.top) : onFocusDate?.(ymd))
            : undefined
          // a11y: ячейка-кнопка — статус дня и действие проговариваются скринридером
          const statusText = d.top ? `${SEV_LABEL[d.top.severity]}, ${KIND_LABEL[d.top.kind]}` : 'норма'
          const actionText = d.top ? 'открыть событие' : 'показать день на графике'
          const ariaLabel = `${label}: ${statusText} — ${actionText}`
          return (
            <span
              key={i}
              role="button"
              tabIndex={clickable ? 0 : -1}
              aria-label={ariaLabel}
              title={d.top ? `${label} · ${SEV_LABEL[d.top.severity]} · ${KIND_LABEL[d.top.kind]} → открыть событие` : `${label} · норма → показать день на графике`}
              onClick={onClick}
              // a11y: Enter/Space запускают тот же обработчик, что и клик
              onKeyDown={clickable ? (e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick?.() } }) : undefined}
              style={{ flex: 1, height: 24, borderRadius: 2, opacity: 0.85, background: tlColors[d.sev], cursor: clickable ? 'pointer' : 'default', outline: '1px solid transparent' }}
              onMouseEnter={clickable ? (e => { (e.currentTarget as HTMLElement).style.outline = '1px solid var(--accent)' }) : undefined}
              onMouseLeave={clickable ? (e => { (e.currentTarget as HTMLElement).style.outline = '1px solid transparent' }) : undefined}
            />
          )
        })}
      </div>
      <div
        className="font-mono"
        style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--text-3)', marginBottom: 8 }}
      >
        <span>−30д</span>
        <span>сегодня</span>
      </div>

      <SectionTitle mt={12}>Последние события датчика</SectionTitle>
      {recent.length === 0 ? (
        <Note>Событий нет.</Note>
      ) : (
        recent.map(ev => {
          const ts = fmtStation(ev.timestamp, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
          const dotColor = ev.kind === 'drift' ? DRIFT_COLOR : SEV_COLOR[ev.severity]
          const vv = ev.value != null ? `${fmtNum(ev.value)}${ev.deviation != null ? ` (Δ${fmtNum(ev.deviation)})` : ''}` : '—'
          return (
            <div
              key={ev.id}
              style={{
                display: 'flex',
                gap: 9,
                alignItems: 'baseline',
                padding: '7px 0',
                borderBottom: '1px solid var(--line)',
                fontSize: 12.5,
              }}
            >
              <span className="font-mono" style={{ fontSize: 12, color: 'var(--text-3)', minWidth: 78 }}>
                {ts}
              </span>
              <span
                style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, display: 'inline-block', background: dotColor }}
              />
              <span style={{ flex: 1, color: 'var(--text-2)' }}>{KIND_LABEL[ev.kind]}</span>
              <span className="font-mono" style={{ fontSize: 12, color: 'var(--text-1)' }}>
                {vv}
              </span>
            </div>
          )
        })
      )}
    </>
  )
}

// ── вкладка: Качество модели ─────────────────────────────────────────────────
function QualityTab({ sensor }: { sensor: SensorMeta }) {
  // Реальные метрики приходят из metadata (nmae_val/r2_val/r2_insample/best_model).
  // Если их нет (немоделируемый/входной датчик) — показываем «—», а не фейковые числа.
  const best = sensor.best_model ?? '—'
  const mae = Number.isFinite(sensor.mae) ? sensor.mae : NaN
  const nmae = sensor.nmae ?? NaN
  const r2val = sensor.r2_val ?? sensor.r2 ?? NaN
  const r2in = sensor.r2_insample ?? NaN

  const hasNmae = Number.isFinite(nmae)
  const nmaePct = hasNmae ? Math.min(nmae * 100 * 4, 100) : 0
  const nmaeColor = nmae < 0.05 ? 'var(--ok)' : 'var(--warn)'

  return (
    <>
      <Metric k="Модель (прод)" v={best} />
      <Metric k="MAE (holdout)" v={fmtNum(mae)} />
      <Metric k="nMAE" v={fmtNum(nmae)} />
      {hasNmae && <Bar pct={nmaePct} color={nmaeColor} />}
      <Metric k="R² (честный, holdout)" v={fmtNum(r2val)} />
      <Metric k="R² (in-sample)" v={fmtNum(r2in)} />
      <HowToRead items={[
        ['Модель', <>в проде один <b style={{ color: 'var(--text-2)' }}>CatBoost</b> — единственный, кто нативно даёт доверительный интервал прогноза.</>],
        ['MAE (holdout)', 'средняя ошибка прогноза в единицах датчика на данных, которых модель не видела при обучении. Меньше — точнее.'],
        ['nMAE', 'та же ошибка, но в долях разброса датчика (безразмерная) — можно сравнивать разные датчики. <0.05 — отлично (бар зелёный), выше — жёлтый.'],
        ['R² (честный)', 'доля поведения датчика, которую модель объясняет на новых данных. 1.0 — идеал, 0 — не лучше среднего. Это и есть реальное качество.'],
        ['R² (in-sample)', 'то же, но на обучающих данных — почти всегда близко к 1. Само по себе ни о чём не говорит; большой разрыв с «честным» = переобучение.'],
      ]} />
    </>
  )
}

// ── вкладка: Дрейф (реальные пер-сенсорные данные) ───────────────────────────
function DriftTab({ sensor }: { sensor: SensorMeta }) {
  const d = sensor.drift
  if (!d || (d.score == null && d.trend == null)) {
    return <Note>Нет данных дрейфа для этого датчика (недостаточно точек на рабочем режиме).</Note>
  }
  const fired = !!d.fired
  const label = fired ? 'ДРЕЙФ ↑' : 'СТАБИЛЬНО'
  const color = fired ? DRIFT_COLOR : 'var(--ok)'
  const sub = d.score != null ? `метрика дрейфа ${fmtNum(d.score)}× от нормы остатка` : 'медленный дрейф остатка'
  const yn = (b?: boolean | null) => (b == null ? '—' : b ? 'сработал' : 'нет')
  return (
    <>
      <Gauge label={label} color={color} sub={sub} />
      <Metric k="Тренд остатка (7 дн)" v={d.trend != null ? `${d.trend > 0 ? '+' : ''}${fmtNum(d.trend)}` : '—'} />
      <Metric k="Reversibility" v={d.reversibility ?? '—'} />
      <Metric k="CUSUM" v={yn(d.cusum)} />
      <Metric k="Page-Hinkley" v={yn(d.ph)} />
      <HowToRead items={[
        ['Вердикт', 'СТАБИЛЬНО / ДРЕЙФ ↑ — общая оценка медленного ухода остатка (факт − прогноз).'],
        ['Метрика дрейфа', 'во сколько раз текущий дрейф больше нормального шума остатка. <1× — в пределах нормы, заметно >1× — уход.'],
        ['Тренд остатка (7 дн)', 'наклон остатка за неделю. Около 0 — стабильно; устойчивый минус/плюс — систематический уход в одну сторону.'],
        ['Reversibility', '«обратимо (режим/погода)» — уход объясняется сменой режима или погодой, а не деградацией железа.'],
        ['CUSUM / Page-Hinkley', 'два детектора накопленного смещения. «Сработал» сам по себе — не тревога: смотрите вместе с вердиктом и обратимостью.'],
      ]} />
    </>
  )
}

// ── вкладка: Калибровка (реальные пороги модели) ─────────────────────────────
function CalibTab({ sensor }: { sensor: SensorMeta }) {
  const c = sensor.calibration
  if (!c) return <Note>Нет данных калибровки для этого датчика.</Note>
  const pm  = (x?: number | null) => (x != null ? `±${fmtNum(x)}` : '—')
  const pct = (x?: number | null) => (x != null ? `${(x * 100).toFixed(1)}%` : '—')
  return (
    <>
      <Metric k="Коридор conformal (α=1%)" v={pm(c.conformal_thr)} />
      <Metric k="Порог POT-EVT" v={pm(c.pot_thr)} />
      <Metric k="n_sigma" v={c.n_sigma_cal != null ? `${fmtNum(c.n_sigma_cal)} (калибр.)` : c.n_sigma != null ? `${fmtNum(c.n_sigma)} (по умолч.)` : '—'} />
      <Metric k="Покрытие нормой" v={pct(c.coverage)} />
      <Metric k="Доля алармов" v={pct(c.alarm_rate)} />
      <HowToRead items={[
        ['Коридор conformal (α=1%)', 'ширина доверительного интервала: ждём, что факт выходит за него не чаще 1% времени.'],
        ['Порог POT-EVT', 'отдельный порог по теории экстремумов (хвост распределения остатка) — для редких крупных выбросов.'],
        ['n_sigma', 'во сколько robust-сигм остатка стоит порог тревоги. «(калибр.)» = подобран по данным, а не взят по умолчанию.'],
        ['Покрытие нормой', 'доля точек внутри коридора. При α=1% хотим ≈99%.'],
        ['Доля алармов', 'доля точек за порогом. ~3% — норма; резкий рост — реальное отклонение.'],
      ]} />
      <Note>Пороги — в единицах остатка датчика. Калибровка на свежей норме holdout (robust σ через MAD), не на обучающих данных.</Note>
    </>
  )
}

// ── вкладка: Доменные индексы (реальные значения по ГПА) ──────────────────────
function IdxTab({ sensor }: { sensor: SensorMeta }) {
  const dom = sensor.domain ?? {}
  const hasAny = Object.keys(dom).length > 0
  return (
    <>
      <SectionTitle mt={0}>Наблюдаемые индексы — текущее значение и вариации</SectionTitle>
      {INDEX_ORDER.map(k => {
        const info = INDEX_BASE[k]
        if (!info) return null
        const real = dom[k]
        const merged = real != null ? { ...info, val: fmtNum(real) } : info
        return <IdxCard key={k} info={merged} />
      })}
      <Note>
        {hasAny
          ? 'Значения — последние из расчёта по этому ГПА (η_p/H_p/shaft/удельное топливо/ΔT). Нормы и интерпретация — из методологии/паспорта.'
          : 'Доменные индексы по этому ГПА ещё не рассчитаны (ожидают цикл мониторинга). Показаны типовые нормы.'}
      </Note>
    </>
  )
}

// ── панель ────────────────────────────────────────────────────────────────────
// Активная вкладка хранится в модульном ref-хранилище: не сбрасывается при
// смене датчика (prop change), но и не засоряет URL-хэш, который уже занят
// роутером (#/s/<id>). Модульная переменная живёт на время сессии — стабильна
// при любом количестве ре-рендеров DetailPanel.
let _persistedTab: DetailTab = 'health'

export function DetailPanel({ sensor, events = [], onSelectEvent, onFocusDate }: DetailPanelProps) {
  // Вкладка инициализируется из модульного ref-хранилища, поэтому не сбрасывается
  // при смене датчика — пользователь остаётся на той же вкладке (напр. «Дрейф»).
  const [tab, setTabState] = useState<DetailTab>(() => _persistedTab)

  const setTab = (key: DetailTab) => {
    _persistedTab = key
    setTabState(key)
  }

  // Синхронизируем ref-хранилище при внешнем изменении (на случай будущего рефакторинга)
  useEffect(() => {
    _persistedTab = tab
  }, [tab])

  // Fade-in контента при смене вкладки или выборе нового датчика
  const tabPanelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = tabPanelRef.current
    if (!el || prefersReducedMotion()) return
    const a = animate(el, { opacity: [0, 1], translateY: [6, 0], duration: 180, ease: 'outCubic' })
    return () => { a.pause() }
  }, [tab, sensor?.id])

  // Мемоизация фильтрации событий — ДО любого раннего return (Rules of Hooks: все хуки
  // безусловны и в стабильном порядке). sensor может быть null → null-безопасно (вернёт []).
  // БАГ-ФИКС: раньше этот useMemo стоял ПОСЛЕ `if (!sensor) return` → при выборе датчика
  // (null→объект) добавлялся 3-й хук → «Rendered more hooks than during the previous render».
  const sensorEvents = useMemo(
    () => (sensor ? events.filter(e => e.sensor_id === sensor.id) : []),
    [events, sensor?.id],
  )

  if (!sensor) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
        <div style={HEADER_STYLE}>
          Аналитика · <span style={{ color: 'var(--accent)' }}>—</span>
        </div>
        <div
          className="font-mono"
          style={{ padding: '40px var(--space-4)', textAlign: 'center', color: 'var(--text-3)', fontSize: 13 }}
        >
          Выберите датчик в сайдбаре слева или на тепловой карте
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div style={HEADER_STYLE}>
        Аналитика · <span style={{ color: 'var(--accent)' }}>{ruSensor(sensor.name)}</span>
      </div>

      {/* вкладки (.dtabs) — ARIA tab-паттерн: tablist/tab/tabpanel + навигация стрелками */}
      <div
        role="tablist"
        aria-label="Аналитика датчика"
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 3,
          padding: 'var(--space-2) 10px',
          borderBottom: '1px solid var(--line)',
          flexShrink: 0,
        }}
      >
        {TABS.map(([key, label], i) => {
          const on = key === tab
          return (
            <button
              key={key}
              id={`dtab-${key}`}
              role="tab"
              aria-selected={on}
              aria-controls="dtabpanel"
              tabIndex={on ? 0 : -1}
              onClick={() => setTab(key)}
              onKeyDown={e => {
                if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
                  e.preventDefault()
                  const dir = e.key === 'ArrowRight' ? 1 : -1
                  const [nextKey] = TABS[(i + dir + TABS.length) % TABS.length]
                  setTab(nextKey)
                  document.getElementById(`dtab-${nextKey}`)?.focus()
                }
              }}
              className="font-mono"
              style={{
                padding: '5px 8px',
                fontSize: 12,
                // Von Restorff: активная вкладка выделена линией снизу 2px акцентом,
                // неактивные приглушены (тусклый текст + пониженная непрозрачность)
                border: `1px solid ${on ? 'var(--accent)' : 'var(--line)'}`,
                borderBottom: on ? '2px solid var(--accent)' : '1px solid var(--line)',
                background: on ? 'var(--accent-glow)' : 'var(--surface-2)',
                color: on ? 'var(--accent)' : 'var(--text-3)',
                opacity: on ? 1 : 0.7,
                cursor: 'pointer',
                borderRadius: 'var(--r-sm)',
                textTransform: 'uppercase',
                transition: 'color var(--dur-fast) var(--ease-standard), background var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard), opacity var(--dur-fast) var(--ease-standard)',
              }}
            >
              {label}
            </button>
          )
        })}
      </div>

      {/* тело */}
      <div role="tabpanel" id="dtabpanel" aria-labelledby={`dtab-${tab}`} tabIndex={0} style={{ overflow: 'auto', flex: 1, minHeight: 0, scrollbarWidth: 'thin' }}>
        <div ref={tabPanelRef} style={{ padding: 14 }}>
          {tab === 'health' && <HealthTab sensor={sensor} events={sensorEvents} onSelectEvent={onSelectEvent} onFocusDate={onFocusDate} />}
          {tab === 'quality' && <QualityTab sensor={sensor} />}
          {tab === 'drift' && <DriftTab sensor={sensor} />}
          {tab === 'calib' && <CalibTab sensor={sensor} />}
          {tab === 'idx' && <IdxTab sensor={sensor} />}
        </div>
      </div>
    </div>
  )
}
