// Конвенция проекта: БД хранит время в UTC, а оператору показываем фиксированную
// зону станции Etc/GMT-5 (= UTC+5 = Asia/Tashkent). Метки времени от API — ISO/UTC.
// toLocale*-вызовы БЕЗ timeZone форматируют в зоне браузера/RDP-сервера, из-за чего
// на машине в иной TZ время события не совпадало с реальным временем станции.
// Эти помощники жёстко фиксируют зону станции.

export const STATION_TZ = 'Etc/GMT-5'

/** Форматирование метки времени в зоне станции (ru-RU). */
export function fmtStation(ts: string | number | Date, opts: Intl.DateTimeFormatOptions): string {
  const d = ts instanceof Date ? ts : new Date(ts)
  if (!Number.isFinite(d.getTime())) return ''
  return d.toLocaleString('ru-RU', { timeZone: STATION_TZ, ...opts })
}

/** Дата в зоне станции как 'YYYY-MM-DD' (стабильный ключ дня для группировки). */
export function stationYMD(ts: string | number | Date): string {
  const d = ts instanceof Date ? ts : new Date(ts)
  if (!Number.isFinite(d.getTime())) return ''
  return d.toLocaleDateString('en-CA', { timeZone: STATION_TZ }) // en-CA → YYYY-MM-DD
}
