
# Промпт: Рефакторинг CS Monitor AI — обучение из БД + мульти-станционная архитектура + запись аномалий в БД

## Контекст проекта

Ты работаешь над `cs_4` — production-сервисом мониторинга аномалий газоперекачивающих агрегатов (ГПА).

**Текущий стек:**
- Backend: Python (FastAPI), CatBoost, psycopg2, pandas
- Frontend: React + TypeScript + Vite + Plotly
- Деплой: Docker Compose (3 сервиса: `predictor`, `backend`, `frontend`)
- БД источника: PostgreSQL `ohangaron.raw_data` (host: 10.1.30.164)
- Схема данных: таблица `raw_data(datetime, point, value)` — временные ряды датчиков в long-формате

**Что сейчас работает:**
- `train_and_save_models.py` — обучает CatBoost-модели, читая из `raw_data_2.csv` (Excel-выгрузка). Использует `ETL CS.csv` как маппинг TAG → feature_name
- `live_predict.py` — читает модели, забирает данные из PostgreSQL, пишет `live_state.json` каждые 5 минут. Детектирует 7 типов аномалий: ml, frozen, neg, roc, seasonal, regime, cross
- `main.py` — FastAPI сервер, читает `live_state.json` и отдаёт через REST API

**Проблема:** `train_and_save_models.py` читает данные из CSV-файла (устаревший подход), а не из PostgreSQL напрямую.

---

## Задача 1: Переписать обучение — читать данные из PostgreSQL

### Требования:
1. Убрать зависимость от `raw_data_2.csv` и `ETL CS.csv` в `train_and_save_models.py`
2. Загружать данные напрямую из PostgreSQL с фильтром по дате: **данные строго до `2026-05-01 00:00:00`**
3. Маппинг TAG → feature_name строить из самих данных БД (теги уже содержат имена признаков в поле `point`) или вынести маппинг в конфиг-файл `config/stations/ohangaron.yaml` (см. Задачу 3)
4. Добавить параметр запуска CLI: `--cutoff-date` (по умолчанию `2026-05-01`)
5. Сохранить всю логику feature engineering: lag1/2/3, rolling mean/std, ранняя остановка по val_fraction=0.2
6. В `metadata.json` дополнительно записывать:
   - `train_cutoff_date` — дата среза
   - `station_id` — идентификатор станции (например, `ohangaron`)
   - `data_source` — `postgresql` вместо `csv`
   - `db_host`, `db_name`, `db_schema`, `db_table` — откуда брались данные

### Пример вызова:
```bash
python train_and_save_models.py --station ohangaron --cutoff-date 2026-05-01
```

### Схема загрузки данных из БД:
```python
query = """
    SELECT datetime, point, value
    FROM {schema}.{table}
    WHERE datetime < %s
    ORDER BY datetime
"""
# cutoff_date = '2026-05-01'
```

---

## Задача 2: Сохранить обратную совместимость live_predict.py

`live_predict.py` уже читает из PostgreSQL — хорошо. Нужно:
1. Убрать все оставшиеся ссылки на `raw_data_2.csv` и `ETL CS.csv` (если есть)
2. Читать `station_id` и параметры подключения из `metadata.json` (там теперь будет `db_*` информация)
3. Убедиться что `live_state.json` содержит поле `station_id`
4. Добавить параметр `--station` для CLI: `python live_predict.py --station ohangaron --mode live`

---

## Задача 3: Мульти-станционная архитектура (заложить фундамент)

Это ключевая архитектурная задача. В будущем появятся другие компрессорные станции с другими БД. Нужно спроектировать и реализовать фундамент сейчас, не реализуя сами дополнительные станции.

### Концепция:

```
config/
  stations/
    ohangaron.yaml      ← конфиг КС Охангарон (уже есть)
    fergana.yaml        ← будущая КС Фергана (пример для документации)
  global.yaml           ← глобальные настройки
```

### Структура `config/stations/ohangaron.yaml`:
```yaml
station_id: ohangaron
display_name: "КС Охангарон"
enabled: true

database:
  host: "${CS_DB_HOST}"          # env var
  port: "${CS_DB_PORT:5432}"
  name: "${CS_DB_NAME}"
  user: "${CS_DB_USER}"
  password: "${CS_DB_PASSWORD}"
  schema: ohangaron
  table: raw_data

units:                            # компрессорные агрегаты
  - id: GPA1
    display_name: "ГПА-1"
    tag_prefix: "GPA-1.GPA-1"
  - id: GPA2
    display_name: "ГПА-2"
    tag_prefix: "GPA-2.GPA-2"
  - id: GPA3
    display_name: "ГПА-3"
    tag_prefix: "GPA-3.GPA-3"

tag_mapping_strategy: auto        # auto | manual | etl_csv
# при auto — маппинг строится автоматически из тегов в БД
# при manual — используется блок tag_overrides ниже
tag_overrides: {}                 # опциональные ручные переопределения

models_dir: "models/ohangaron"    # папка для моделей этой станции
state_file: "state/ohangaron_live_state.json"

training:
  refresh_interval_sec: 300
  val_fraction: 0.2
  anomaly_n_sigma: 5.0
  min_buffer_pct: 0.15
  var_smoothing: 24
```

### Рефакторинг кода под мульти-станции:

**`backend/station_config.py`** — новый модуль:
```python
# Загружает и валидирует конфиги станций
# Подставляет env vars в значения вида ${VAR_NAME}
# Возвращает StationConfig dataclass

def load_station_config(station_id: str) -> StationConfig
def list_available_stations() -> list[str]
def get_db_connection(station_cfg: StationConfig) -> psycopg2.connection
```

**`backend/data_loader.py`** — новый модуль:
```python
# Загрузка данных из PostgreSQL (единственный поддерживаемый источник)
# Все станции используют PostgreSQL, но с разными хостами/схемами/таблицами

class PostgresDataLoader:
    def __init__(self, station_cfg: StationConfig): ...
    def fetch_training_data(self, cutoff_date: str) -> pd.DataFrame: ...
    def fetch_live_slice(self, since_ts: str) -> pd.DataFrame: ...
    def get_connection(self) -> psycopg2.connection: ...
```

> **Важно:** источник данных всегда PostgreSQL. Абстрактный базовый класс не нужен — не усложняем архитектуру тем, что не потребуется.

### Обновлённая структура папок:
```
backend/
  config/
    stations/
      ohangaron.yaml
      _template.yaml         ← шаблон для новых станций
    global.yaml
  models/
    ohangaron/               ← модели для КС Охангарон
      metadata.json
      *.cbm
  state/
    ohangaron_live_state.json
  station_config.py          ← NEW
  data_loader.py             ← NEW
  train_and_save_models.py   ← рефакторинг
  live_predict.py            ← рефакторинг
  main.py                    ← обновление под multi-station
```

---

## Задача 3б: Классификация аномалий (числовые коды)

Ввести единую числовую классификацию типов аномалий. Используется везде: в БД, в API, в UI.

| Код | Тип        | Описание                                        | Severity |
|-----|------------|-------------------------------------------------|----------|
| 1   | `ml`       | Статистический выброс (CatBoost residual > N·σ) | crit     |
| 2   | `neg`      | Сбой физичности (отрицательное значение)        | crit     |
| 3   | `frozen`   | Датчик завис (≥5 одинаковых значений подряд)    | warn     |
| 4   | `roc`      | Скачок скорости изменения (Rate of Change)      | warn     |
| 5   | `seasonal` | Сезонная аномалия (отклонение от часового профиля) | info  |
| 6   | `regime`   | Смена режима работы агрегата                    | info     |
| 7   | `cross`    | Кросс-ГПА отклонение (один ГПА выбивается из группы) | info |

Хранить маппинг в `backend/anomaly_types.py`:

```python
# anomaly_types.py

from dataclasses import dataclass

@dataclass(frozen=True)
class AnomalyTypeInfo:
    code: int
    key: str          # строковый идентификатор (для обратной совместимости API)
    name_ru: str      # человекочитаемое название
    severity: str     # crit | warn | info

ANOMALY_TYPES: dict[str, AnomalyTypeInfo] = {
    "ml":       AnomalyTypeInfo(1, "ml",       "Статистический выброс",   "crit"),
    "neg":      AnomalyTypeInfo(2, "neg",       "Сбой физичности",         "crit"),
    "frozen":   AnomalyTypeInfo(3, "frozen",    "Датчик завис",            "warn"),
    "roc":      AnomalyTypeInfo(4, "roc",       "Скачок ΔV",               "warn"),
    "seasonal": AnomalyTypeInfo(5, "seasonal",  "Сезонная аномалия",       "info"),
    "regime":   AnomalyTypeInfo(6, "regime",    "Смена режима",            "info"),
    "cross":    AnomalyTypeInfo(7, "cross",     "Кросс-ГПА отклонение",    "info"),
}

CODE_TO_KEY: dict[int, str] = {v.code: k for k, v in ANOMALY_TYPES.items()}
```

---

## Задача 3в: Запись аномалий в PostgreSQL (`ohangaron.anomalies`)

### DDL таблицы (создать если не существует):
```sql
CREATE TABLE IF NOT EXISTS ohangaron.anomalies (
    id            BIGSERIAL PRIMARY KEY,
    detected_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(), -- время обнаружения предиктором (когда отработал цикл)
    event_date    DATE         NOT NULL,               -- дата аномалии (для удобных запросов по дням)
    event_ts      TIMESTAMPTZ  NOT NULL,               -- точная метка аномалии, округлённая до 5 мин (= слот из raw_data)
    station_id    VARCHAR(64)  NOT NULL,               -- 'ohangaron'
    unit_id       VARCHAR(16)  NOT NULL,               -- 'GPA1' | 'GPA2' | 'GPA3'
    sensor_id     VARCHAR(128) NOT NULL,               -- 'vibro_front_support__GPA1'
    sensor_tag    VARCHAR(256),                        -- SCADA-тег датчика
    anomaly_type  SMALLINT     NOT NULL,               -- числовой код (1-7), см. классификацию выше
    anomaly_key   VARCHAR(16)  NOT NULL,               -- строковый ключ: 'ml', 'frozen', ...
    severity      VARCHAR(8)   NOT NULL,               -- 'crit' | 'warn' | 'info'
    value         DOUBLE PRECISION,                    -- фактическое значение датчика в момент аномалии
    predicted     DOUBLE PRECISION,                    -- предсказанное моделью значение (заполняется для type=1)
    deviation_pct DOUBLE PRECISION,                    -- отклонение факта от прогноза, %
    description   TEXT,                               -- автогенерированное текстовое описание
    acked         BOOLEAN      NOT NULL DEFAULT FALSE, -- подтверждена оператором
    acked_at      TIMESTAMPTZ,                         -- когда подтверждена
    acked_by      VARCHAR(64)                          -- кем подтверждена (логин / 'system')
);

-- event_date заполняется автоматически из event_ts при вставке:
-- INSERT ... event_date = event_ts::date ...

-- Индексы для типичных запросов
CREATE INDEX IF NOT EXISTS idx_anomalies_event_date   ON ohangaron.anomalies (event_date DESC);
CREATE INDEX IF NOT EXISTS idx_anomalies_event_ts     ON ohangaron.anomalies (event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_anomalies_station_unit ON ohangaron.anomalies (station_id, unit_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_sensor_id    ON ohangaron.anomalies (sensor_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_type         ON ohangaron.anomalies (anomaly_type);
CREATE INDEX IF NOT EXISTS idx_anomalies_acked        ON ohangaron.anomalies (acked) WHERE acked = FALSE;
```

> **Про `event_date` vs `event_ts`:**
> - `event_ts` — полная метка (`TIMESTAMPTZ`), содержит и дату и время, округлена до 5-минутного слота (совпадает с дискретностью `raw_data`). Используется как точный ключ для совмещения с временным рядом на графике.
> - `event_date` — отдельный столбец типа `DATE` (только дата, без времени), вычисляется как `event_ts::date`. Нужен для удобных запросов вида «все аномалии за сегодня» без `DATE(event_ts)` в WHERE-условии — это позволяет индексу нормально работать.

### UNIQUE constraint — дедупликация по слоту:

```sql
-- Уникальность: один датчик не может иметь два события одного типа в один 5-минутный слот
ALTER TABLE ohangaron.anomalies
    ADD CONSTRAINT uq_anomaly_slot
    UNIQUE (sensor_id, event_ts, anomaly_type);
-- event_ts уже округлён до 5 мин → это и есть "дата + временной слот + датчик + тип"
```

> **Почему не `UNIQUE(sensor_id, event_date, anomaly_type)`?** Потому что за один день датчик может законно детектировать несколько аномалий одного типа в разные временные слоты (например, вибрация скакнула утром и вечером). Дедуплицируем именно по 5-минутному слоту, а не по дате.

### Логика записи в `live_predict.py`:

После каждого цикла предикта (каждые 5 минут) новые аномалии записываются в БД:

```python
def save_anomalies_to_db(station_cfg, new_anomalies: list[dict]):
    """
    Записывает новые аномалии в ohangaron.anomalies.
    Дедупликация: не пишем дубль если (sensor_id, event_ts, anomaly_type) уже есть.
    """
    if not new_anomalies:
        return
    conn = get_db_connection(station_cfg)
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO ohangaron.anomalies
                (event_ts, event_date, station_id, unit_id, sensor_id, sensor_tag,
                 anomaly_type, anomaly_key, severity, value, predicted,
                 deviation_pct, description)
            VALUES
                (%(event_ts)s, %(event_ts)s::date, %(station_id)s, %(unit_id)s,
                 %(sensor_id)s, %(sensor_tag)s,
                 %(anomaly_type)s, %(anomaly_key)s, %(severity)s, %(value)s, %(predicted)s,
                 %(deviation_pct)s, %(description)s)
            ON CONFLICT ON CONSTRAINT uq_anomaly_slot DO NOTHING
        """, new_anomalies)
        # event_date вычисляется прямо в SQL из event_ts — не нужно передавать отдельным полем
    conn.commit()
    conn.close()
```

> **Дедупликация:** добавить UNIQUE constraint `(sensor_id, event_ts, anomaly_type)` чтобы при перезапуске предиктора не плодить дубли.

```sql
ALTER TABLE ohangaron.anomalies
    ADD CONSTRAINT uq_anomaly_event
    UNIQUE (sensor_id, event_ts, anomaly_type);
```

### Новые эндпоинты в FastAPI для работы с БД-аномалиями:

```
GET  /api/stations/{station_id}/anomalies
     ?from=2026-05-01&to=2026-05-15
     &unit=GPA1
     &type=1,2,3          ← коды типов
     &acked=false
     &limit=500

POST /api/stations/{station_id}/anomalies/{anomaly_id}/ack
     → { acked: true, acked_at: "...", acked_by: "operator" }

POST /api/stations/{station_id}/anomalies/ack-bulk
     body: { ids: [1, 2, 3] }

GET  /api/stations/{station_id}/anomalies/stats
     → { by_type: {1: 45, 2: 3, ...}, by_unit: {GPA1: 20, ...}, total: 120 }
```

---

## Задача 4: Обновить FastAPI (`main.py`) под мульти-станционность

### Новые эндпоинты:

```
GET /api/stations                          ← список всех станций
GET /api/stations/{station_id}/sensors     ← датчики станции
GET /api/stations/{station_id}/stats       ← статистика станции
GET /api/stations/{station_id}/events      ← события станции
GET /api/stations/{station_id}/heatmap     ← тепловая карта
GET /api/stations/{station_id}/sensors/{sensor_id}/chart  ← график датчика
```

**Обратная совместимость:** старые эндпоинты `/api/sensors`, `/api/stats` и т.д. должны остаться и работать как алиасы на `ohangaron` (дефолтная станция).

```python
@app.get("/api/sensors")
def list_sensors_compat(gpa: Optional[str] = None):
    return list_sensors("ohangaron", gpa)  # редирект на дефолтную станцию
```

### Новая модель ответа `/api/stations`:
```json
[
  {
    "id": "ohangaron",
    "display_name": "КС Охангарон",
    "enabled": true,
    "units": ["GPA1", "GPA2", "GPA3"],
    "live_data": true,
    "last_updated": "2026-05-15T10:30:00"
  }
]
```

---

## Задача 4б: Переключатель станций в дашборде (Frontend)

### Концепция UI

В шапке дашборда между Ticker и основным контентом появляется переключатель станций. Когда станция одна — компонент скрыт (не занимает место). Когда станций ≥ 2 — показывается горизонтальная панель с кнопками.

### Компонент `StationSwitcher`

Создать `frontend/src/components/StationSwitcher/StationSwitcher.tsx`:

```tsx
interface StationSwitcherProps {
  stations: StationInfo[]          // список из GET /api/stations
  activeId: string                 // текущая выбранная станция
  onSwitch: (id: string) => void
}

// StationInfo (новый тип в types/index.ts):
interface StationInfo {
  id: string
  display_name: string
  enabled: boolean
  units: string[]
  live_data: boolean
  last_updated: string | null
}
```

### Внешний вид переключателя

```
┌─────────────────────────────────────────────────────────────────┐
│  КС Охангарон  ●   │   КС Фергана  ○   │   КС Газалкент  ○    │
│  [активная]        │   [inactive]       │   [inactive]         │
└─────────────────────────────────────────────────────────────────┘
```

- Активная станция: фон `var(--accent-glow)`, бордер `var(--accent)`, текст `var(--accent)`
- Неактивная: фон `var(--surface-2)`, бордер `var(--line)`, текст `var(--text-2)`
- Индикатор живых данных: `●` зелёный (live_data=true) / `○` серый (live_data=false)
- При hover — бордер меняется на `var(--accent)` с transition 150ms
- Шрифт: JetBrains Mono, `var(--fs-xs)`, uppercase, letter-spacing 0.08em

### Интеграция в `App.tsx`

```tsx
// Добавить состояние:
const [activeStation, setActiveStation] = useState<string>('ohangaron')

// Запрос списка станций:
const { data: stations = [] } = useQuery({
  queryKey: ['stations'],
  queryFn: () => api.stations(),
  refetchInterval: 60_000,  // раз в минуту достаточно
})

// Все остальные запросы передают activeStation:
const { data: sensors = [] } = useQuery({
  queryKey: ['sensors', activeStation],
  queryFn: () => api.sensors(activeStation),
  refetchInterval: REFETCH_MS,
})
// аналогично для stats, events, heatmap, chart

// Рендер (между Ticker и Layout):
{stations.length > 1 && (
  <StationSwitcher
    stations={stations}
    activeId={activeStation}
    onSwitch={(id) => {
      setActiveStation(id)
      setSelectedId(null)  // сбросить выбранный датчик при переключении станции
    }}
  />
)}
```

### Обновить `api/client.ts`

Все существующие методы должны принимать `stationId`:

```ts
export const api = {
  stations: () =>
    get<StationInfo[]>('/api/stations'),

  sensors: (stationId: string, gpa?: string) =>
    get<SensorMeta[]>(`/api/stations/${stationId}/sensors`, gpa ? { gpa } : undefined),

  stats: (stationId: string) =>
    get<StatsResponse>(`/api/stations/${stationId}/stats`),

  events: (stationId: string, opts?: {...}) =>
    get<EventItem[]>(`/api/stations/${stationId}/events`, opts),

  heatmap: (stationId: string, gpa?: string) =>
    get<HeatmapCell[]>(`/api/stations/${stationId}/heatmap`, gpa ? { gpa } : undefined),

  sensorChart: (stationId: string, sensorId: string, days = 7) =>
    get<SensorChartResponse>(`/api/stations/${stationId}/sensors/${encodeURIComponent(sensorId)}/chart`, { days }),
}
```

### Добавить в заголовок дашборда название станции

В блоке `Page head` в `App.tsx` под основным заголовком добавить:

```tsx
<p style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 'var(--fs-xs)', color: 'var(--text-3)', marginTop: 2, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
  {stations.find(s => s.id === activeStation)?.display_name ?? activeStation}
</p>
```

### Новый тип в `frontend/src/types/index.ts`

```ts
export interface StationInfo {
  id: string
  display_name: string
  enabled: boolean
  units: string[]
  live_data: boolean
  last_updated: string | null
}
```

---

## Задача 5: Обновить `docker-compose.yml`

Сервис `predictor` должен поддерживать указание станции через env:

```yaml
services:
  predictor-ohangaron:
    build: ./backend
    restart: unless-stopped
    env_file: .env
    command: python live_predict.py --station ohangaron --mode live
    volumes:
      - state_data:/app/state
    environment:
      - STATION_ID=ohangaron

  backend:
    build: ./backend
    restart: unless-stopped
    env_file: .env
    ports:
      - "8000:8000"
    volumes:
      - state_data:/app/state
      - ./backend/config:/app/config:ro
    depends_on:
      - predictor-ohangaron

  frontend:
    build: ./frontend
    restart: unless-stopped
    ports:
      - "80:80"
    depends_on:
      - backend

volumes:
  state_data:
```

Когда появятся новые станции, добавить `predictor-fergana` и т.д.

---

## Задача 6: Скрипт обучения — финальный интерфейс

```bash
# Обучить модели для КС Охангарон, данные до 2026-05-01
python train_and_save_models.py --station ohangaron --cutoff-date 2026-05-01

# Посмотреть доступные станции
python train_and_save_models.py --list-stations

# В будущем, для другой станции:
python train_and_save_models.py --station fergana --cutoff-date 2026-05-01
```

Скрипт должен сохранить модели в `models/{station_id}/` и записать `models/{station_id}/metadata.json`.

---

## Требования к качеству кода

1. **Нет хардкода**: все параметры через конфиг или CLI-аргументы
2. **Env vars**: секреты (пароли, хосты) только через `.env` / переменные окружения, никогда в конфигах напрямую
3. **Логирование**: использовать Python `logging` вместо `print()` где возможно, или хотя бы сохранить текущий стиль с flush=True
4. **Обратная совместимость**: существующие `.env` и `docker-compose.yml` продолжают работать без изменений (ohangaron как дефолтная станция)
5. **Graceful degradation**: если конфиг/модели для станции не найдены — понятное сообщение об ошибке, не падение всего сервиса
6. **Комментарии на русском** — в стиле уже существующего кода
7. **PostgreSQL везде**: никаких других источников данных, никаких абстрактных базовых классов для загрузчика — только `PostgresDataLoader`
8. **Дедупликация аномалий**: UNIQUE constraint в БД + `ON CONFLICT DO NOTHING` в INSERT, чтобы перезапуск предиктора не плодил дубли
9. **Атомарность записи аномалий**: запись в `ohangaron.anomalies` в одной транзакции за цикл, не по одной строке

---

## Порядок выполнения

1. Создать `backend/anomaly_types.py` — числовая классификация
2. Создать `backend/config/stations/ohangaron.yaml` и `backend/config/stations/_template.yaml`
3. Написать `backend/station_config.py`
4. Написать `backend/data_loader.py` (только PostgresDataLoader)
5. Выполнить DDL — создать таблицу `ohangaron.anomalies` с индексами и UNIQUE constraint
6. Рефакторинг `train_and_save_models.py` — убрать CSV, добавить загрузку из PostgreSQL с cutoff
7. Рефакторинг `live_predict.py` — убрать CSV, читать конфиг из station_config, писать аномалии в БД
8. Обновить `main.py` — мульти-станционные эндпоинты, эндпоинты для аномалий из БД, данные графика из двух таблиц
9. Создать компонент `StationSwitcher` в frontend
10. Обновить `App.tsx` и `api/client.ts` под мульти-станционность
11. Обновить `docker-compose.yml`
12. Обновить `requirements.txt` (добавить `PyYAML>=6.0`)
13. Проверить: `--dry-run` режим для train скрипта

---

## Критерий готовности

- [ ] `python train_and_save_models.py --station ohangaron --cutoff-date 2026-05-01` работает без CSV-файлов
- [ ] Модели сохраняются в `models/ohangaron/*.cbm` и `models/ohangaron/metadata.json`
- [ ] `python live_predict.py --station ohangaron --mode live` работает, пишет `state/ohangaron_live_state.json` и вставляет строки в `ohangaron.anomalies`
- [ ] При перезапуске предиктора дубли в `ohangaron.anomalies` не появляются (UNIQUE + ON CONFLICT)
- [ ] `GET /api/stations` возвращает список `[{"id": "ohangaron", ...}]`
- [ ] `GET /api/stations/ohangaron/sensors/vibro_front_support__GPA1/chart` возвращает `series` из `raw_data` и `anomalies` из `ohangaron.anomalies` совмещённые
- [ ] `GET /api/stations/ohangaron/anomalies?acked=false` возвращает данные из БД, не из live_state.json
- [ ] `POST /api/stations/ohangaron/anomalies/{id}/ack` обновляет запись в БД
- [ ] Старые эндпоинты `GET /api/sensors`, `GET /api/stats` продолжают работать (алиасы на ohangaron)
- [ ] В дашборде виден `StationSwitcher` (если станций ≥ 2) или скрыт (если одна)
- [ ] При переключении станции все данные (датчики, события, тепловая карта) обновляются
- [ ] Добавление новой станции требует только: создать `config/stations/{new_station}.yaml` + добавить predictor в docker-compose
- [ ] Нет ни одного `raw_data_2.csv` или `ETL CS.csv` в production code paths

---

## Задача 4в: График датчика — данные из двух таблиц PostgreSQL

Эндпоинт `GET /api/stations/{station_id}/sensors/{sensor_id}/chart` читает **только из PostgreSQL**. Никакого `live_state.json`, никаких CSV-файлов. Два запроса к БД:

### Запрос 1: временной ряд из `ohangaron.raw_data`

```sql
-- Забираем сырые значения датчика за запрошенный период
SELECT datetime, value
FROM ohangaron.raw_data
WHERE point = %s                              -- SCADA-тег датчика из metadata.json
  AND datetime >= NOW() - INTERVAL '%s days'  -- параметр days из запроса
ORDER BY datetime
```

На основе полученного ряда CatBoost-модель строит предсказание (`p`) и доверительный интервал (`lo`, `hi`) — точно так же, как сейчас делает `live_predict.py`, только не в фоне, а по запросу.

Результат → `series: list[TimeSeriesPoint]`  (`t`, `v`, `p`, `lo`, `hi`).

### Запрос 2: маркеры аномалий из `ohangaron.anomalies`

```sql
-- Забираем все зафиксированные аномалии по этому датчику за тот же период
SELECT event_ts, anomaly_type, anomaly_key, severity,
       value, predicted, deviation_pct, description
FROM ohangaron.anomalies
WHERE sensor_id = %s
  AND event_ts >= NOW() - INTERVAL '%s days'
ORDER BY event_ts
```

Результат → `anomalies: list[AnomalyMarker]` — маркеры на график.

### Структура ответа (обновлённая):

```python
class AnomalyMarker(BaseModel):
    t: str                    # event_ts в ISO формате
    v: Optional[float]        # фактическое значение датчика
    predicted: Optional[float]
    anomaly_type: int         # числовой код 1-7
    kind: str                 # 'ml' | 'frozen' | ...
    severity: str             # 'crit' | 'warn' | 'info'
    description: str

class SensorChartResponse(BaseModel):
    sensor_id: str
    tag: str
    r2: float
    mae: float
    current: Optional[float]     # последнее значение из raw_data
    predicted: Optional[float]   # последнее предсказание модели
    deviation: Optional[float]   # % отклонения
    series: list[TimeSeriesPoint]  # из ohangaron.raw_data + CatBoost-предикт
    anomalies: list[AnomalyMarker] # из ohangaron.anomalies
```

### Реализация эндпоинта:

```python
@app.get("/api/stations/{station_id}/sensors/{sensor_id}/chart")
def get_sensor_chart(station_id: str, sensor_id: str, days: int = Query(7, le=90)):
    cfg    = load_station_config(station_id)
    loader = PostgresDataLoader(cfg)

    # 1. Метаданные датчика (тег, feat_cols, r2, mae) — из models/{station_id}/metadata.json
    meta   = get_sensor_meta(station_id, sensor_id)

    # 2. Сырые данные из ohangaron.raw_data → wide-формат → предикт CatBoost
    raw_df = loader.fetch_sensor_series(tag=meta["tag"], days=days)
    series = run_predict_for_chart(raw_df, sensor_id, meta, station_id)

    # 3. Аномалии из ohangaron.anomalies
    anomalies = loader.fetch_anomalies_for_sensor(sensor_id=sensor_id, days=days)

    return SensorChartResponse(
        sensor_id=sensor_id,
        tag=meta["tag"], r2=meta["r2"], mae=meta["mae"],
        current=series[-1].v if series else None,
        predicted=series[-1].p if series else None,
        deviation=...,
        series=series,
        anomalies=anomalies,
    )
```

> **Важно:** `live_state.json` остаётся только как быстрый кеш для главного дашборда (тепловая карта, статы, боковая панель — они должны грузиться мгновенно). Для графика конкретного датчика — только БД, чтобы отображалась полная история, а не только то что попало в последний цикл предиктора.

> **`fetch_sensor_series`** делает два SQL-запроса: сначала сам тег за N дней из `ohangaron.raw_data`, затем все соседние теги того же ГПА (нужны как предикторы для модели). Разворачивает в wide-формат, строит lag-фичи, прогоняет через CatBoost — всё в памяти, результат возвращает как `list[TimeSeriesPoint]`.
