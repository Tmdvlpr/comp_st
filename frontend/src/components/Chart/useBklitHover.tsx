import { useRef } from 'react'

/**
 * Переиспользуемый bklit-style ховер для Plotly-графиков: точки-маркеры на каждой
 * линии + плавающий тултип у курсора (строки «точка · имя · значение») + пилюля со
 * временем на оси X. Сам крест-курсор (вертикаль) рисует компонент через shapes —
 * хук отвечает за DOM-оверлеи.
 *
 * Использование:
 *   const hover = useBklitHover(theme)
 *   <div ref={hover.wrapRef} style={{position:'relative',...}}>
 *     <div ref={plotDiv} .../>
 *     {hover.overlays}
 *   </div>
 *   // в plotly_hover:  hover.onHover(el, ev)
 *   // в plotly_unhover: hover.hide()
 */
interface HoverRow { name: string; val: number; color: string; py: number }

export function useBklitHover(theme: 'dark' | 'light', opts?: { onActive?: (name: string | null) => void }) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const dotsRef = useRef<HTMLDivElement>(null)
  const pillRef = useRef<HTMLDivElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)
  const dotPoolRef = useRef<HTMLDivElement[]>([])
  const rafRef = useRef(0)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const pendingRef = useRef<any>(null)
  const lastKeyRef = useRef('')
  const lastActiveRef = useRef<string | null>(null)

  const styleRef = useRef({ bg: '', border: '', fg: '' })
  styleRef.current = theme === 'dark'
    ? { bg: 'rgba(13,20,38,0.94)', border: 'rgba(88,166,255,0.45)', fg: '#CDD9E5' }
    : { bg: 'rgba(255,255,255,0.97)', border: '#C2CCD6', fg: '#1B2530' }

  const apply = () => {
    rafRef.current = 0
    const h = pendingRef.current
    const wrap = wrapRef.current
    if (!h || !wrap) return
    const W = wrap.clientWidth, H = wrap.clientHeight
    const st = styleRef.current
    const pad = (n: number) => String(n).padStart(2, '0')
    const d = new Date(h.ms)
    const okd = Number.isFinite(d.getTime())
    const fnum = (v: number) => (Number.isFinite(v) ? v.toPrecision(4) : '—')

    // точки-маркеры (пул переиспользуемых узлов, плавный glide вдоль линии)
    const layer = dotsRef.current
    if (layer) {
      const pool = dotPoolRef.current
      h.rows.forEach((r: HoverRow, i: number) => {
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

    // плавающий тултип
    const tip = tipRef.current
    if (tip && h.rows.length) {
      tip.innerHTML = h.rows.map((r: HoverRow) =>
        `<div style="display:flex;align-items:center;gap:6px;line-height:1.55;white-space:nowrap">`
        + `<span style="width:8px;height:8px;border-radius:50%;background:${r.color};flex-shrink:0"></span>`
        + `<span style="color:var(--text-2);max-width:260px;overflow:hidden;text-overflow:ellipsis">${r.name}</span>`
        + `<span style="margin-left:14px;font-weight:700;color:${st.fg}">${fnum(r.val)}</span></div>`,
      ).join('')
      tip.style.background = st.bg
      tip.style.borderColor = st.border
      const tipW = tip.offsetWidth || 160, tipH = tip.offsetHeight || 56
      const topPy = Math.min(...h.rows.map((r: HoverRow) => r.py))
      let left = h.xpx + 16
      if (left + tipW > W - 4) left = h.xpx - tipW - 16
      if (left < 4) left = 4
      let top = topPy - tipH - 12
      if (top < 4) top = Math.min(topPy + 16, H - tipH - 4)
      tip.style.left = left + 'px'
      tip.style.top = top + 'px'
      tip.style.opacity = '1'
    }

    // пилюля со временем (HH:MM 24ч) на оси X
    const pill = pillRef.current
    if (pill) {
      pill.textContent = okd ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : ''
      pill.style.left = h.xpx + 'px'
      pill.style.top = (h.plotBottom + 5) + 'px'
      pill.style.opacity = '1'
    }

    // активная серия (ближайшая к курсору по Y) → подсветка записи в легенде
    if (opts?.onActive) {
      let name: string | null = null
      if (h.cursorY != null && h.rows.length) {
        const cy = h.cursorY - wrap.getBoundingClientRect().top
        let best = Infinity
        for (const r of h.rows as HoverRow[]) {
          const dd = Math.abs(r.py - cy)
          if (dd < best) { best = dd; name = r.name }
        }
      }
      if (name !== lastActiveRef.current) { lastActiveRef.current = name; opts.onActive(name) }
    }
  }

  const hide = () => {
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
    pendingRef.current = null
    lastKeyRef.current = ''
    if (tipRef.current) tipRef.current.style.opacity = '0'
    if (pillRef.current) pillRef.current.style.opacity = '0'
    for (const dot of dotPoolRef.current) dot.style.opacity = '0'
    if (opts?.onActive && lastActiveRef.current !== null) { lastActiveRef.current = null; opts.onActive(null) }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const onHover = (el: any, ev: any) => {
    const pts = ev?.points
    const x = pts?.[0]?.x
    if (x == null) return
    const key = String(x)
    if (key === lastKeyRef.current) return
    lastKeyRef.current = key
    const fl = el?._fullLayout
    const xa = fl?.xaxis
    if (!xa?.range) return
    const ms = typeof x === 'number' ? x : new Date(String(x).replace(' ', 'T')).getTime()
    const ax0 = new Date(xa.range[0]).getTime(), ax1 = new Date(xa.range[1]).getTime()
    const xpx = xa._offset + ((ms - ax0) / ((ax1 - ax0) || 1)) * xa._length
    const plotBottom = (fl?.yaxis?._offset ?? 0) + (fl?.yaxis?._length ?? 0)
    const rows: HoverRow[] = []
    for (const p of pts) {
      if (typeof p?.y !== 'number' || !Number.isFinite(p.y)) continue
      const ya = p?.yaxis ?? fl?.yaxis              // мультиось: точка на своей оси
      if (!ya?.range) continue
      const yv0 = Number(ya.range[0]), yv1 = Number(ya.range[1])
      const py = ya._offset + (1 - (p.y - yv0) / ((yv1 - yv0) || 1)) * ya._length
      // показываем РЕАЛЬНОЕ значение из customdata (если есть, напр. при нормировке), иначе y
      const val = (typeof p?.customdata === 'number' && Number.isFinite(p.customdata)) ? p.customdata : p.y
      const color = p?.data?.line?.color ?? '#CDD9E5'
      rows.push({ name: p?.data?.name ?? '', val, color, py })
    }
    if (!rows.length) return
    pendingRef.current = { xpx, ms, rows, plotBottom, cursorY: (ev?.event?.clientY ?? null) }
    if (!rafRef.current) rafRef.current = requestAnimationFrame(apply)
  }

  const overlays = (
    <>
      {/* Точки-маркеры на линиях (узлы создаются динамически) */}
      <div ref={dotsRef} aria-hidden="true" style={{ position: 'absolute', inset: 0, zIndex: 5, pointerEvents: 'none' }} />
      {/* Пилюля со временем на оси X */}
      <div
        ref={pillRef}
        aria-hidden="true"
        className="font-mono"
        style={{
          position: 'absolute', zIndex: 6, opacity: 0, pointerEvents: 'none',
          transform: 'translateX(-50%)', transition: 'left .09s linear, opacity .15s ease',
          padding: '2px 9px', borderRadius: 999, fontSize: 11, whiteSpace: 'nowrap',
          background: 'var(--text-1)', color: 'var(--bg)', fontWeight: 700, boxShadow: 'var(--shadow-md)',
        }}
      />
      {/* Плавающий тултип у курсора */}
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
    </>
  )

  return { wrapRef, overlays, onHover, hide }
}
