import { memo } from 'react'

interface ChartSkeletonProps {
  /** Подпись под областью графика. По умолчанию «Загрузка данных…». */
  label?: string
  /** Фиксированная высота (px) — для модалок/панелей. Иначе тянется по родителю (flex:1). */
  height?: number
}

// Форма «призрачного» графика (viewBox 0 0 100 40, растягивается по контейнеру).
// Плавная кривая (кубические безье) вместо ломаной — красивее и «дороже» на вид.
const LINE_PATH = 'M0,29 C9,29 13,21 22,21 C31,21 35,27 44,25 C53,23 57,15 65,16 C73,17 79,11 87,11 C93,11 97,7 100,6'
// Та же кривая, замкнутая до низа — площадь под линией (area).
const AREA_PATH = `${LINE_PATH} L100,40 L0,40 Z`
// Маска по площади под кривой: белая фигура = видимая зона блика (alpha-маска).
const AREA_MASK = `url("data:image/svg+xml,${encodeURIComponent(
  `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 40' preserveAspectRatio='none'><path d='${AREA_PATH}' fill='#fff'/></svg>`,
)}")`

const maskStyle = {
  WebkitMaskImage: AREA_MASK,
  maskImage: AREA_MASK,
  WebkitMaskSize: '100% 100%',
  maskSize: '100% 100%',
  WebkitMaskRepeat: 'no-repeat',
  maskRepeat: 'no-repeat',
} as const

/**
 * Скелетон-заглушка области графика на время загрузки в стиле bklit-ui
 * `AreaChartLoading loadingStyle="sweep"`, но нативно под наш стек:
 * статичная заливка площади под «призрачной» кривой + мягкий ДИАГОНАЛЬНЫЙ блик,
 * который проходит ТОЛЬКО по этой площади (mask по кривой), а не по всему
 * прямоугольнику. Сетка-гридлайны — фоном, контур кривой — поверх.
 *
 * prefers-reduced-motion: блик (.chart-sweep) гасится в globals.css
 * (animation:none) → остаётся статичная форма area+линия, не пустота.
 */
export const ChartSkeleton = memo(function ChartSkeleton({ label, height }: ChartSkeletonProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      className="font-mono anim-fade-up"
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 'var(--space-2)',
        width: '100%',
        minHeight: 0,
        height: height ?? undefined,
        flex: height ? 'none' : 1,
      }}
    >
      <div
        style={{
          position: 'relative',
          flex: 1,
          minHeight: 0,
          overflow: 'hidden',
          borderRadius: 'var(--r-sm)',
          border: '1px solid var(--line)',
          background: 'var(--surface-2)',
        }}
      >
        {/* призрачная сетка (горизонтальные линии, как gridlines графика) */}
        <div
          aria-hidden="true"
          style={{
            position: 'absolute',
            inset: 0,
            backgroundImage:
              'repeating-linear-gradient(to top, transparent 0, transparent calc(25% - 1px), var(--line) calc(25% - 1px), var(--line) 25%)',
            opacity: 0.5,
          }}
        />
        {/* статичная заливка площади под кривой (area) — видна всегда, в т.ч. при reduced-motion */}
        <svg
          aria-hidden="true"
          viewBox="0 0 100 40"
          preserveAspectRatio="none"
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}
        >
          <defs>
            <linearGradient id="skeleton-area-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--line-2)" stopOpacity={0.35} />
              <stop offset="100%" stopColor="var(--line-2)" stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <path d={AREA_PATH} fill="url(#skeleton-area-fill)" />
        </svg>
        {/* sweep-блик: диагональная полоса, замаскированная по площади под кривой */}
        <div className="chart-sweep" aria-hidden="true" style={{ position: 'absolute', inset: 0, ...maskStyle }} />
        {/* контур призрачной линии графика — поверх */}
        <svg
          aria-hidden="true"
          viewBox="0 0 100 40"
          preserveAspectRatio="none"
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}
        >
          <path
            d={LINE_PATH}
            fill="none"
            stroke="var(--line-2)"
            strokeWidth={0.8}
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
            opacity={0.7}
          />
        </svg>
      </div>
      <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', textAlign: 'center', flexShrink: 0 }}>
        {label ?? 'Загрузка данных…'}
      </span>
    </div>
  )
})
