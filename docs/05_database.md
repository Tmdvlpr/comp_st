# 05. База данных

PostgreSQL. Схема станции задаётся в конфиге (`db.schema`), для `ohangaron` — схема `ohangaron`. Подключение — через пул (`station_config.get_db_connection`), параметры из env (`CS_DB_*`).

## 5.1 Таблица `raw_data` (телеметрия, long-формат)

| Столбец | Тип | Описание |
|---------|-----|----------|
| `datetime` | timestamp | Метка времени среза (UTC при чтении переводится в `Etc/GMT-5`) |
| `point` | text | SCADA-тег, напр. `GPA-1.GPA-1.PD.PV` |
| `value` | double | Значение сигнала |
| `health` | text | **Существует, кодом аномалий пока не заполняется.** Планируется: список кодов через запятую (`"1,4"`), `"0"`=норма, `NULL`=не оценено |

- Одна строка = один тег в один момент. Wide-таблица (теги→столбцы) строится в коде (`_to_wide`), с округлением к 5 мин и `ffill(limit=2)`.
- Объём большой (миллионы строк) — отсюда даунсемплинг графиков в БД и `MAX_HISTORY_DAYS` в предикторе.
- Индекс: `idx_raw_data_point_dt (point, datetime DESC)` — для `pvsnapshot`, графиков и (планируемого) `UPDATE health`.

### Конвенция `health` (план)
- `NULL` — срез не оценивался детектором (нет модели / простой / прогрев / подавление).
- `"0"` — оценено, аномалий нет.
- `"<коды>"` — отсортированный список кодов сработавших детекторов через запятую (несколько типов в одном срезе → `"2,4,5"`).
Детальная история по типам — в `anomalies` (источник правды).

## 5.2 Таблица `anomalies` (журнал детекций)

```sql
CREATE TABLE {schema}.anomalies (
    id            BIGSERIAL PRIMARY KEY,
    sensor_id     TEXT NOT NULL,            -- feature-имя (gas_pressure_out_gpa__GPA1)
    event_ts      TIMESTAMPTZ NOT NULL,     -- время аномалии
    anomaly_type  SMALLINT NOT NULL,        -- код 1..7 (см. словарь)
    severity      TEXT,                     -- crit/warn/info
    value         DOUBLE PRECISION,
    deviation     DOUBLE PRECISION,         -- отклонение, %
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT anomalies_dedup UNIQUE (sensor_id, event_ts, anomaly_type)
);
```
- Заполняется `live_predict._write_live_state` → `save_anomalies` (батч, `ON CONFLICT DO NOTHING`).
- Индексы: `idx_anomalies_ts (event_ts DESC)`, `idx_anomalies_sensor_ts (sensor_id, event_ts DESC)`.

## 5.3 Планируемая таблица `"journal notifications"` (уведомления)

> Имя содержит пробел — **всегда** через `psycopg2.sql.Identifier`. Хранить в `config/global.yaml` (`journal_table`).

```sql
CREATE TABLE {schema}."journal notifications" (
    id BIGSERIAL PRIMARY KEY,
    station_id TEXT NOT NULL, sensor_id TEXT NOT NULL, point TEXT, gpa TEXT,
    event_ts TIMESTAMPTZ NOT NULL, anomaly_type SMALLINT NOT NULL,
    kind TEXT, severity TEXT, value DOUBLE PRECISION, deviation DOUBLE PRECISION,
    message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',  -- new/acked/resolved
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT journal_notifications_dedup UNIQUE (sensor_id, event_ts, anomaly_type)
);
CREATE INDEX idx_journal_notif_ts     ON {schema}."journal notifications" (event_ts DESC);
CREATE INDEX idx_journal_notif_status ON {schema}."journal notifications" (status, event_ts DESC);
```
Разделение ролей: `anomalies` — точечный/эпизодный лог детекций; `"journal notifications"` — операторские уведомления (читаемый `message`, статус квитирования).

## 5.4 Три приёмника аномалий (план)

| Приёмник | Гранулярность | Назначение |
|----------|---------------|-----------|
| `anomalies` | эпизод/точка по типу | нормализованный лог (есть) |
| `raw_data.health` | срез `(point, datetime)` | здоровье датчика «прямо в данных» (план) |
| `"journal notifications"` | эпизод | операторские уведомления (план) |

## 5.5 Связь идентификаторов
`raw_data.point` (SCADA-тег) ↔ `sensor_id` (feature) — через `metadata.json` (`tag_to_name` / `name_to_tag`). Для записи в `health`/журнал по `point` нужно переводить `sensor_id → point` через `name_to_tag`.

## 5.6 Часовые пояса
`raw_data.datetime` читается как UTC и переводится в `Etc/GMT-5` (naive) в пайплайне. Запись назад (`health`) — **по исходному UTC** строки, иначе `WHERE datetime=...` не найдёт записей. См. «подводные камни» в плане и [02_architecture_audit.md](02_architecture_audit.md) (A6).

## 5.7 Управление схемой
Сейчас ad-hoc: `ensure_anomalies_table()`, `fix_anomalies_table.py`, `ensure_indexes.py`. Планируется единый идемпотентный `migrate_db.py` (фаза 1). Рекомендация аудита (A8): версионируемые миграции.
