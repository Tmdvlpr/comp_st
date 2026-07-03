import { animate, cubicBezier } from 'animejs'
import { prefersReducedMotion } from './motion'

/**
 * Анимации «построения» графиков в духе bklit-ui, но реализованные нативно под
 * наш стек (Plotly + SVG-спарклайны), т.к. компоненты bklit (@visx + motion)
 * несовместимы с Plotly. Воспроизводим принципы скилла:
 *  - длительность входа декартовых графиков ≈ 1100 мс;
 *  - easing cubic-bezier(0.85, 0, 0.15, 1) (тот же, что у staggered-bar reveal bklit);
 *  - повтор анимации при смене данных (аналог revealSignature);
 *  - уважение prefers-reduced-motion (CSS-guard глобален, но animejs идёт через rAF,
 *    поэтому каждую анимацию гасим явной проверкой).
 *
 * Важно: clip-path/opacity/strokeDashoffset — compositing/paint-свойства, layout
 * не триггерят; анимация одноразовая (~1.1 c), на стоимость перерисовки Plotly не влияет.
 */

export const CHART_ENTER_MS = 1100
/** Тот же ease, что bklit рекомендует для staggered reveal: cubic-bezier(0.85,0,0.15,1).
 *  В animejs v4 это функция easing (строковый парсер JS-анимаций cubicBezier не знает —
 *  строка молча упала бы в дефолт), поэтому собираем через cubicBezier(). */
export const CHART_ENTER_EASE = cubicBezier(0.85, 0, 0.15, 1)
/** Тип easing для опций (EasingFunction из animejs, без отдельного импорта типа). */
type Ease = typeof CHART_ENTER_EASE

export interface RevealHandle {
  /** Остановить анимацию и привести элемент в финальное (видимое) состояние. */
  cancel: () => void
}

/**
 * «Построение» графика: горизонтальная шторка слева-направо (clip-path) + лёгкое
 * проявление. Работает поверх любого рендера Plotly (SVG и WebGL/scattergl), т.к.
 * анимируем контейнер, а не внутренние пути.
 *
 * Вызывать ПОСЛЕ первого Plotly.newPlot (или при смене сигнатуры данных — повтор).
 * Предыдущий незавершённый reveal на том же узле нужно отменить (cancel) до запуска
 * нового — иначе два animate наперегонки пишут clip-path.
 */
export function revealChart(
  el: HTMLElement,
  opts?: { duration?: number; ease?: Ease },
): RevealHandle | undefined {
  if (prefersReducedMotion()) {
    el.style.opacity = '1'
    el.style.clipPath = ''
    return
  }
  const duration = opts?.duration ?? CHART_ENTER_MS
  const ease = opts?.ease ?? CHART_ENTER_EASE

  el.style.willChange = 'clip-path, opacity'
  el.style.opacity = '0'
  el.style.clipPath = 'inset(0 100% 0 0)' // полностью скрыт справа

  const clear = () => {
    el.style.clipPath = ''
    el.style.opacity = '1'
    el.style.willChange = ''
  }

  // Числовой прокси → clip-path (animejs не интерполирует clip-path-строки напрямую).
  const proxy = { p: 0 }
  const wipe = animate(proxy, {
    p: 100,
    duration,
    ease,
    onUpdate: () => {
      el.style.clipPath = `inset(0 ${(100 - proxy.p).toFixed(2)}% 0 0)`
    },
    onComplete: clear,
  })
  // Короткое проявление, чтобы левый край не «выскакивал» жёстко на t=0.
  const fade = animate(el, {
    opacity: [0, 1],
    duration: Math.min(duration, 320),
    ease: 'outQuad',
  })

  return {
    cancel: () => {
      wipe.pause()
      fade.pause()
      clear()
    },
  }
}

/**
 * «Прорисовка» линии SVG слева-направо через stroke-dashoffset. Для спарклайнов
 * обзора, где мы владеем <path> (в отличие от внутренних путей Plotly).
 */
export function drawPath(
  path: SVGPathElement,
  opts?: { duration?: number; ease?: Ease },
): RevealHandle | undefined {
  if (prefersReducedMotion()) {
    path.style.strokeDasharray = ''
    path.style.strokeDashoffset = ''
    return
  }
  let len = 0
  try {
    len = path.getTotalLength()
  } catch {
    return // путь ещё не в DOM / нерисуемый
  }
  if (!len || !Number.isFinite(len)) return

  const duration = opts?.duration ?? 900
  const ease = opts?.ease ?? CHART_ENTER_EASE

  const clear = () => {
    path.style.strokeDasharray = ''
    path.style.strokeDashoffset = ''
  }

  path.style.strokeDasharray = String(len)
  path.style.strokeDashoffset = String(len)
  const a = animate(path, {
    strokeDashoffset: [len, 0],
    duration,
    ease,
    onComplete: clear,
  })

  return {
    cancel: () => {
      a.pause()
      clear()
    },
  }
}
