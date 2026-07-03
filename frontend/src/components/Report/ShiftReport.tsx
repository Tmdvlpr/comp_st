import { useMemo } from 'react'
import type { EventItem, SensorMeta } from '../../types'
import { KIND_LABEL, SEV_LABEL } from '../../types'
import { ruSensor } from '../../lib/sensorLabels'
import { useModal } from '../../lib/useModal'
import { fmtStation } from '../../lib/time'
import { rpmTvdFromSensors, isRunning } from '../../lib/gpa'

interface ShiftReportProps {
  open: boolean
  onClose: () => void
  events: EventItem[]
  sensors: SensorMeta[]
  stationName: string
}

export function ShiftReport({ open, onClose, events, sensors, stationName }: ShiftReportProps) {
  const dialogRef = useModal<HTMLDivElement>(open, onClose)
  const cutoff = useMemo(() => new Date(Date.now() - 12 * 60 * 60 * 1000).toISOString(), [open])

  const recentEvents = useMemo(
    () => events
      .filter(e => e.timestamp >= cutoff)
      .sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1)),
    [events, cutoff]
  )

  const gpaStatuses = useMemo(() => {
    const gpas = [...new Set(sensors.map(s => s.gpa))].sort()
    return gpas.map(gpa => {
      // единое правило с KioskMode: обороты ТВД (rpm_tvd) + общий порог
      const rpmValue = rpmTvdFromSensors(sensors, gpa)
      const running = isRunning(rpmValue)
      const hasCrit = sensors.some(s => s.gpa === gpa && s.severity === 'crit')
      return { gpa, running, rpmValue, hasCrit }
    })
  }, [sensors])

  const kindCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const e of recentEvents) counts[e.kind] = (counts[e.kind] ?? 0) + 1
    return Object.entries(counts).sort((a, b) => b[1] - a[1])
  }, [recentEvents])

  if (!open) return null

  const generated = fmtStation(new Date(), {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  })

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={`Отчёт смены — ${stationName}`}
      className="fixed inset-0 z-[120] overflow-auto"
      style={{ background: 'var(--bg)', color: 'var(--text)' }}
    >
      <style>{`
        @media print {
          .report-toolbar { display: none !important; }
          /* Скрываем боковые панели, навигацию и кнопки вне отчёта */
          body > *:not(#root) { display: none !important; }
          #root > *:not([role="dialog"]) { display: none !important; }
          [role="dialog"][aria-label*="Отчёт"] {
            position: static !important;
            overflow: visible !important;
            background: #fff !important;
            color: #000 !important;
          }
          .shift-report {
            max-width: 100% !important;
            padding: 10mm 12mm !important;
            margin: 0 !important;
          }
          /* Документ-карточка разворачивается в плоский документ без рамки/тени */
          .shift-report .report-doc {
            border: none !important;
            box-shadow: none !important;
            border-radius: 0 !important;
            padding: 0 !important;
            background: #fff !important;
          }
          table { page-break-inside: auto; }
          tr { page-break-inside: avoid; page-break-after: auto; }
          thead { display: table-header-group; }
          tfoot { display: table-footer-group; }
          @page { margin: 15mm 12mm; size: A4 portrait; }
        }
      `}</style>
      {/* Тулбар (скрыт при печати) */}
      <div
        className="report-toolbar sticky top-0 flex items-center justify-end"
        style={{ gap: 'var(--space-2)', padding: 'var(--space-3) var(--space-5)', background: 'var(--surface)', borderBottom: '1px solid var(--line)', zIndex: 1 }}
      >
        <button
          onClick={() => window.print()}
          className="font-mono"
          style={{ padding: 'var(--space-2) var(--space-4)', borderRadius: 'var(--r-sm)', fontSize: 'var(--fs-sm)', cursor: 'pointer', background: 'var(--accent-strong)', border: '1px solid var(--accent-strong)', color: 'var(--on-accent)', fontWeight: 700 }}
        >
          ⎙ Печать
        </button>
        <button
          onClick={onClose}
          className="font-mono"
          style={{ padding: 'var(--space-2) var(--space-4)', borderRadius: 'var(--r-sm)', fontSize: 'var(--fs-sm)', cursor: 'pointer', background: 'var(--surface-2)', border: '1px solid var(--line)', color: 'var(--text-2)' }}
        >
          ✕ Закрыть
        </button>
      </div>

      <div className="shift-report" style={{ maxWidth: 992, margin: '0 auto', padding: '28px var(--space-4) 60px' }}>
        {/* Документ-карточка (на экране — рамка/тень; при печати разворачивается в плоский документ) */}
        <div
          className="report-doc"
          style={{
            maxWidth: 960, margin: '0 auto',
            background: 'var(--surface)', border: '1px solid var(--line)', borderRadius: 'var(--r-lg)',
            padding: '36px 40px', boxShadow: 'var(--shadow-md)',
          }}
        >
        {/* Заголовок */}
        <h1 style={{ fontFamily: 'var(--font-display)', fontSize: 'var(--fs-xl)', fontWeight: 700, color: 'var(--text-1)', letterSpacing: '0.02em', margin: '0 0 6px' }}>
          Отчёт смены — {stationName}
        </h1>
        <p className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-3)', margin: '0 0 28px' }}>
          Сформировано: {generated} · период: последние 12 часов
        </p>

        {/* Состояние агрегатов */}
        <h2 style={{ fontFamily: 'var(--font-display)', fontSize: '13px', fontWeight: 700, color: 'var(--text-2)', letterSpacing: '0.06em', textTransform: 'uppercase', borderBottom: '1px solid var(--line)', paddingBottom: 8, margin: '0 0 14px' }}>
          Состояние агрегатов
        </h2>
        <div className="flex flex-wrap" style={{ gap: 'var(--space-3)', marginBottom: 'var(--space-6)' }}>
          {gpaStatuses.map(g => (
            <div
              key={g.gpa}
              style={{
                flex: '1 1 200px', padding: 14, border: '1px solid var(--line)',
                /* Цвет только на CRIT; в остальных случаях нейтральная линия */
                borderLeft: `3px solid ${g.hasCrit ? 'var(--crit)' : 'var(--line-2)'}`,
                background: 'var(--surface-2)', borderRadius: 'var(--r-md)',
              }}
            >
              <div style={{ fontWeight: 700, fontSize: 'var(--fs-md)', color: 'var(--text-1)', marginBottom: 6 }}>ГПА-{g.gpa.replace('GPA', '')}</div>
              {/* Статус — одноцветный текст (без раскраски) */}
              <div className="font-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-2)' }}>
                {g.running ? `В РАБОТЕ · ${g.rpmValue !== null ? Math.round(g.rpmValue).toLocaleString('ru-RU') : '—'} об/мин` : 'ОСТАНОВЛЕН'}
              </div>
              {g.hasCrit && (
                <div className="font-mono flex items-center" style={{ fontSize: 'var(--fs-xs)', color: 'var(--crit)', fontWeight: 700, marginTop: 8, gap: 5 }}>
                  ⚠ есть критические датчики
                </div>
              )}
            </div>
          ))}
        </div>

        {/* Сводка по типам */}
        <h2 style={{ fontFamily: 'var(--font-display)', fontSize: '13px', fontWeight: 700, color: 'var(--text-2)', letterSpacing: '0.06em', textTransform: 'uppercase', borderBottom: '1px solid var(--line)', paddingBottom: 8, margin: '0 0 14px' }}>
          Сводка по типам ({recentEvents.length} событий)
        </h2>
        <div className="flex flex-wrap" style={{ gap: 'var(--space-2)', marginBottom: 'var(--space-6)' }}>
          {kindCounts.length === 0 ? (
            <span className="font-mono" style={{ color: 'var(--text-3)', fontSize: 'var(--fs-sm)' }}>Нет событий за период</span>
          ) : kindCounts.map(([kind, n]) => (
            <span
              key={kind}
              className="inline-flex items-center"
              style={{ gap: 6, padding: '5px 12px', border: '1px solid var(--line)', borderRadius: 999, fontSize: 'var(--fs-xs)', background: 'var(--surface-2)', color: 'var(--text-2)' }}
            >
              {KIND_LABEL[kind as keyof typeof KIND_LABEL] ?? kind}: <b className="font-mono" style={{ color: 'var(--text-1)', fontWeight: 700 }}>{n}</b>
            </span>
          ))}
        </div>

        {/* Таблица событий */}
        <h2 style={{ fontFamily: 'var(--font-display)', fontSize: '13px', fontWeight: 700, color: 'var(--text-2)', letterSpacing: '0.06em', textTransform: 'uppercase', borderBottom: '1px solid var(--line)', paddingBottom: 8, margin: '0 0 14px' }}>
          События за последние 12 часов
        </h2>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12.5px' }}>
          <caption style={{ display: 'none' }}>Отчёт по смене</caption>
          <thead>
            <tr>
              {['Время', 'ГПА', 'Датчик', 'Тип', 'Уровень', 'Квитировано'].map((h, i) => (
                <th
                  key={h}
                  scope="col"
                  title={i === 5 ? 'Квитировано оператором' : undefined}
                  style={{ textAlign: 'left', fontSize: '11px', letterSpacing: '0.05em', textTransform: 'uppercase', color: 'var(--text-3)', fontWeight: 600, padding: '0 10px 8px', borderBottom: '1px solid var(--line-2)' }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {recentEvents.length === 0 ? (
              <tr><td colSpan={6} style={{ padding: 'var(--space-4) var(--space-2)', color: 'var(--text-3)', textAlign: 'center' }}>Нет событий за период</td></tr>
            ) : recentEvents.map(e => (
              <tr key={e.id}>
                <td className="font-mono" style={{ padding: '8px 10px', borderBottom: '1px solid var(--line)', color: 'var(--text-1)', whiteSpace: 'nowrap', verticalAlign: 'middle' }}>
                  {fmtStation(e.timestamp, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}
                </td>
                <td className="font-mono" style={{ padding: '8px 10px', borderBottom: '1px solid var(--line)', color: 'var(--text-1)', whiteSpace: 'nowrap', verticalAlign: 'middle' }}>ГПА-{e.gpa.replace('GPA', '')}</td>
                <td style={{ padding: '8px 10px', borderBottom: '1px solid var(--line)', color: 'var(--text-1)', verticalAlign: 'middle' }}>{ruSensor(e.sensor_name)}</td>
                <td style={{ padding: '8px 10px', borderBottom: '1px solid var(--line)', color: 'var(--text-2)', verticalAlign: 'middle' }}>{KIND_LABEL[e.kind] ?? e.kind}</td>
                {/* Уровень: цвет только на CRIT, прочие — одноцветный текст */}
                <td style={{ padding: '8px 10px', borderBottom: '1px solid var(--line)', color: e.severity === 'crit' ? 'var(--crit)' : 'var(--text-2)', fontWeight: e.severity === 'crit' ? 700 : 400, verticalAlign: 'middle' }}>{SEV_LABEL[e.severity]}</td>
                <td className="font-mono" style={{ padding: '8px 10px', borderBottom: '1px solid var(--line)', textAlign: 'center', color: e.acked ? 'var(--text-2)' : 'var(--text-3)', verticalAlign: 'middle' }}>{e.acked ? '✓ да' : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  )
}
