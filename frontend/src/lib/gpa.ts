import type { SensorMeta } from '../types'

// Единый порог «в работе» и единое правило выбора оборотов (ТВД, rpm_tvd) для всех
// экранов. Раньше KioskMode (живой snapshot rpm_tvd) и ShiftReport (макс. по ЛЮБЫМ
// датчикам rpm*) расходились, из-за чего один ГПА мог быть «В РАБОТЕ» в киоске и
// «ОСТАНОВЛЕН» в печатном отчёте. Теперь оба опираются на rpm_tvd + этот порог.
export const RPM_RUNNING_THRESHOLD = 500 // об/мин (рабочие ~4000–7000, останов ~0)

export const isRunning = (rpm: number | null): boolean =>
  rpm !== null && Number.isFinite(rpm) && rpm > RPM_RUNNING_THRESHOLD

/** Обороты ТВД (rpm_tvd) для ГПА из метаданных датчиков (поле cur). */
export function rpmTvdFromSensors(sensors: SensorMeta[], gpa: string): number | null {
  const s = sensors.find(x => x.gpa === gpa && x.name === 'rpm_tvd')
  const v = s?.cur
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
