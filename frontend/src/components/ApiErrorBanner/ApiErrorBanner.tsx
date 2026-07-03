import { useState, useEffect } from 'react'
import { useApiErrorAt } from '../../api/errorStore'

// Баннер показывается, если последняя ошибка API была менее 60с назад.
const WINDOW_MS = 60_000

export function ApiErrorBanner() {
  const lastErrorAt = useApiErrorAt()
  const [, setTick] = useState(0)

  // Раз в 5с пересчитываем видимость, чтобы баннер сам исчез через 60с.
  useEffect(() => {
    if (!lastErrorAt) return
    const t = setInterval(() => setTick(v => v + 1), 5_000)
    return () => clearInterval(t)
  }, [lastErrorAt])

  const visible = lastErrorAt > 0 && Date.now() - lastErrorAt < WINDOW_MS
  if (!visible) return null

  return (
    <div
      role="alert"
      className="anim-slide-down"
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        zIndex: 9000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 'var(--space-2)',
        padding: 'var(--space-2) var(--space-4)',
        background: 'color-mix(in srgb, var(--crit) 22%, var(--bg))',
        borderBottom: '1px solid var(--crit)',
        color: 'var(--text-1)',
        fontFamily: 'var(--font-display)',
        fontSize: 'var(--fs-xs)',
        fontWeight: 600,
        letterSpacing: '0.04em',
        textAlign: 'center',
      }}
    >
      <span style={{ color: 'var(--crit)' }}>⚠</span>
      <span>Ошибка обновления данных — показаны последние полученные значения</span>
    </div>
  )
}
