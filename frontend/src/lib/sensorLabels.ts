// Авто-сгенерировано из «ETL CS.csv» (колонки Фича→Описание) + домен-фичи.
// Русские названия датчиков для отображения. Ключ — англ. фича (без суффикса __GPAn).
export const SENSOR_RU: Record<string, { ru: string; unit?: string }> = {
  ambient_temp: { ru: 'Наружная температура', unit: '°C' },
  anti_surge_valve_pos: { ru: 'Положение антипомпажного клапана' },
  avo_approach: { ru: 'Недоохлаждение АВО', unit: '°C' },
  compressor_rotor_axial_shift_t1: { ru: 'Осевой сдвиг ротора нагнетателя Т1' },
  compressor_rotor_axial_shift_t2: { ru: 'Осевой сдвиг ротора нагнетателя Т2' },
  dT_bearings: { ru: 'Перепад темп. подшипников ΔT', unit: '°C' },
  dT_cooler: { ru: 'Охлаждение в АВО ΔT', unit: '°C' },
  dT_disch: { ru: 'Нагрев газа при сжатии ΔT', unit: '°C' },
  fuel_gas_flow_rate_sec: { ru: 'Расход топливного газа на ГПА в сек.' },
  fuel_gas_pressure_in_gtd: { ru: 'Давление топливного газа на входе в газотурбинный двигатель' },
  gas_leak_bfs: { ru: 'Загазованность в блоке очистки газа' },
  gas_leak_bftg: { ru: 'Загазованность над блоке фильтрации топливного газа' },
  gas_leak_over_bpg: { ru: 'Загазованность над блоком подготовки газа' },
  gas_leak_over_compressor_1: { ru: 'Загазованность над компрессором, точка 1' },
  gas_leak_over_compressor_2: { ru: 'Загазованность над компрессором, точка 2' },
  gas_leak_over_enclosure: { ru: 'Загазованность над кожухом компрессора' },
  gas_leak_over_sk12: { ru: 'Загазованность над стопорным клапаном 12' },
  gas_leak_over_uptg: { ru: 'Загазованность над установкой подготовки топливного газа' },
  gas_pressure_in_gpa: { ru: 'Давление газа перед ГПА' },
  gas_pressure_out_avo: { ru: 'Давление газа на выходе Аппарата воздушного охлаждения' },
  gas_pressure_out_gpa: { ru: 'Давление газа после ГПА' },
  gas_temp_in_gpa: { ru: 'Температура газа перед ГПА' },
  gas_temp_out_avo: { ru: 'Температура газа на выходе Аппарата воздушного охлаждения' },
  gas_temp_out_gpa: { ru: 'Температура газа на выходе с ГПА' },
  is_anti_surge_to_main: { ru: 'Антипомпаж в магистраль' },
  is_anti_surge_to_ring: { ru: 'Антипомпаж на КОЛЬЦО' },
  is_emergency_stop_no_venting: { ru: 'Аварийный Останов БЕЗ стравливания' },
  is_emergency_stop_with_venting: { ru: 'Аварийный Останов со стравливанием' },
  is_gtd_status_running: { ru: 'Газотурбинный двигатель в РАБОТЕ' },
  is_gtd_status_stopped: { ru: 'Газотурбинный двигатель  ОСТАНОВЛЕН' },
  is_mode_mainline: { ru: 'Работа на МАГИСТРАЛЬ' },
  is_mode_ring: { ru: 'Работа на КОЛЬЦО' },
  is_normal_stop_no_venting: { ru: 'Нормальный Останов БЕЗ стравливания газа' },
  is_normal_stop_with_venting: { ru: 'Нормальный Останов со стравливанием газа' },
  oil_temp_in_pod: { ru: 'Температура масла на входе в передней опоры двигателя' },
  oil_temp_out_st: { ru: 'Температура масла на выходе силовой турбины' },
  oil_temp_out_zod: { ru: 'Температура масла на выходе задней опоры двигателя' },
  polytropic_eff: { ru: 'Политропный КПД' },
  polytropic_head: { ru: 'Политропный напор', unit: 'кДж/кг' },
  pressure_ratio: { ru: 'Степень сжатия' },
  rpm_st: { ru: 'Обороты силовой турбины в мин.' },
  rpm_st_red: { ru: 'Приведённые обороты СТ', unit: 'об/мин' },
  rpm_tnd: { ru: 'Обороты турбины низкого давления в мин.' },
  rpm_tnd_red: { ru: 'Приведённые обороты ТНД', unit: 'об/мин' },
  rpm_tvd: { ru: 'Обороты турбины высокого давления в мин.' },
  rpm_tvd_red: { ru: 'Приведённые обороты ТВД', unit: 'об/мин' },
  shaft_ratio: { ru: 'Отношение оборотов валов' },
  shaft_resid_st: { ru: 'Рассогласование валов СТ' },
  shaft_resid_tnd: { ru: 'Рассогласование валов ТНД' },
  specific_fuel: { ru: 'Удельный расход топлива' },
  temp_active_pads_front_bearing: { ru: 'Температура активных колодок переднего опорного подшипника' },
  temp_front_bearing_pads: { ru: 'Температура колодок переднего опорного подшипника' },
  temp_passive_pads_rear_bearing: { ru: 'Температура пассивных колодок заднего опорного подшипника' },
  temp_rear_bearing_pads: { ru: 'Температура колодок заднего опорного подшипника' },
  vibro_disp_rotor_rear_horiz: { ru: 'Виброперемещение ротора задней опоры  нагнетателя (горизонтальная составляющая)' },
  vibro_disp_rotor_rear_vert: { ru: 'Виброперемещение ротора задней опоры нагнетателя (вертикальная составляющая)' },
  vibro_front_support: { ru: 'Вибрация передней опоры' },
  vibro_rear_support: { ru: 'Вибрация задней опоры' },
  vibro_rotor_front_horiz: { ru: 'Вибрация ротора передней опоры нагнетателя (горизонтальная составляющая)' },
  vibro_rotor_front_vert: { ru: 'Вибрация ротора передней опоры нагнетателя (вертикальная составляющая)' },
  vibro_st_support: { ru: 'Вибрация опоры силовой турбины' },
}

/** Русское имя датчика по id/фиче (снимает суффикс __GPAn). Фолбэк — исходное имя. */
export function ruSensor(nameOrId: string): string {
  const base = nameOrId.split("__")[0]
  return SENSOR_RU[base]?.ru ?? base
}

/** Единица измерения по id/фиче (или пусто). */
export function ruUnit(nameOrId: string): string {
  return SENSOR_RU[nameOrId.split("__")[0]]?.unit ?? ""
}