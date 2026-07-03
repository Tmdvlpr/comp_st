import { useEffect, useRef } from 'react'

/**
 * Доступная модалка (WCAG 2.1.2 / 2.4.3): фокус-трап, закрытие по Escape,
 * возврат фокуса на элемент-триггер при закрытии.
 *
 * Использование:
 *   const dialogRef = useModal<HTMLDivElement>(open, onClose)
 *   ...
 *   <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="...">
 *
 * Хук активен только когда open=true. onClose может пересоздаваться на каждом
 * рендере (инлайн-стрелка) — он читается через ref, поэтому эффект НЕ перезапускает
 * фокусировку и не крадёт фокус при ре-рендерах. Возврат фокуса — в cleanup.
 */
const FOCUSABLE =
  'a[href],area[href],input:not([disabled]),select:not([disabled]),' +
  'textarea:not([disabled]),button:not([disabled]),' +
  '[tabindex]:not([tabindex="-1"]),[contenteditable="true"]'

const isVisible = (el: HTMLElement) =>
  !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)

export function useModal<T extends HTMLElement = HTMLDivElement>(
  open: boolean,
  onClose: () => void,
) {
  const ref = useRef<T>(null)
  const restoreRef = useRef<HTMLElement | null>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    if (!open) return
    const node = ref.current
    restoreRef.current = (document.activeElement as HTMLElement) ?? null

    const focusables = () =>
      node ? Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(isVisible) : []

    // начальный фокус внутрь диалога (первый интерактивный элемент или сам контейнер)
    const f0 = focusables()
    if (f0[0]) f0[0].focus()
    else if (node) { node.tabIndex = -1; node.focus() }

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); closeRef.current(); return }
      if (e.key !== 'Tab' || !node) return
      const f = focusables()
      if (f.length === 0) { e.preventDefault(); return }
      const first = f[0]
      const last = f[f.length - 1]
      const active = document.activeElement
      // если фокус ушёл за пределы диалога — вернуть внутрь
      if (!node.contains(active)) { e.preventDefault(); first.focus(); return }
      if (e.shiftKey && active === first) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus() }
    }

    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      const r = restoreRef.current
      if (r && document.body.contains(r) && typeof r.focus === 'function') r.focus()
    }
  }, [open])

  return ref
}
