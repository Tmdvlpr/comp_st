# DESIGN — CS Monitor AI

> ⚠️ **Актуальное состояние (host-launch, v23 conformal+epistemic, 2026-07-01) — в [ARCHITECTURE.md](ARCHITECTURE.md).** Ниже — исходный дизайн; часть решений (CQR, `train_and_save_models`/`train_v2`/`regime.py`/`calibrator.py`, статический `build_dashboard`/`anomaly_live.html`) заменена: единый `train.py`, запуск `python run.py`, UI — Vite/React.

Дизайн-документ системы выявления аномалий компрессорной станции (КС).
Цель документа — собрать в одном месте назначение, ключевые проектные решения,
модель данных, ML-подход, контракт API и фронтенд, инварианты надёжности и
известный технодолг. Подробности — в [docs/](docs/).

**Версия:** 2026-06-23 · **Станция по умолчанию:** `ohangaron` (Охангаронская КС, ГПА-1/2/3)

---

## 1. Задача и подход

CS Monitor AI выявляет аномалии в работе КС по телеметрии SCADA в реальном времени.

Базовая идея — **модель физической согласованности**: для каждого датчика обучается
регрессия «ожидаемого значения» (предсказывает датчик по другим датчикам того же ГПА
и по собственной короткой истории). Расхождение факта с прогнозом плюс набор
статистических правил формируют аномалии. Датчик считается аномальным, когда ведёт
себя не так, как «договорились» остальные сигналы и его недавнее прошлое.

Архитектура **мульти-станционная**: каждая станция описывается YAML-конфигом, код
параметризуется через `StationConfig`.

### Принципы проектирования
- **Онлайн в реальном времени.** Аномалии ищутся на свежих данных каждые 5 минут, а не
  только постфактум по истории.
- **Разделение офлайн-обучения и онлайн-детекции.** Обучение — разовая тяжёлая операция;
  рантайм только применяет готовые модели.
- **Подавление ложных срабатываний — первоклассная задача.** Стоянка ГПА, прогрев,
  переходные режимы обрабатываются явно.
- **Устойчивость к перезапускам.** Журнал аномалий переживает рестарт; оперативный снимок
  пишется атомарно.
- **Артефакты и состояние — на диске, факты — в БД.** Модели и снимок состояния — файлы;
  источник правды по аномалиям — таблица в PostgreSQL.

---

## 2. Архитектура

```
SCADA → PostgreSQL.raw_data
                 │
     ┌───────────┼─────────────────────────┐
     ▼                                       ▼
train_and_save_models.py            live_predict.py (цикл 5 мин, ОНЛАЙН)
  → models/*.cbm + metadata.json      → anomalies (БД) + live_state.json
                                              │
                                          main.py (FastAPI) → frontend (React)
```

| Компонент | Роль | Тип процесса |
|-----------|------|--------------|
| PostgreSQL | Хранилище телеметрии (`raw_data`) и аномалий (`anomalies`) | внешний |
| `train_and_save_models.py` | Обучение моделей (офлайн, по требованию) | разовый запуск |
| `live_predict.py --mode live` | Онлайн-детекция: цикл 5 мин, запись аномалий и состояния | постоянный фоновый |
| `main.py` (FastAPI) | REST API для фронтенда | постоянный (uvicorn, 4 воркера) |
| `frontend/` (React/Vite) | Дашборд оператора | статика / SPA |
| `models/<station>/` | Артефакты: `*.cbm` + `metadata.json` | файлы на диске |
| `state/<station>_live_state.json` | Снимок результатов последнего цикла для API | файл на диске |

### Потоки данных
1. **Ingestion.** Телеметрия попадает в `raw_data` извне (SCADA/АСУ ТП), формат long:
   `(datetime, point, value, health)`. `sync_raw_data.py` синхронизирует хвост в CSV.
2. **Обучение (офлайн).** `train_and_save_models.py` строит wide-таблицу (теги → столбцы),
   отбрасывает простои ГПА, обучает по одной CatBoost-модели на датчик, сохраняет
   `*.cbm` + единый `metadata.json`. В рантайме не вызывается.
3. **Детекция (онлайн).** `live_predict.py` каждые 5 минут берёт свежий срез `raw_data`,
   считает прогноз и 7 детекторов **по новым точкам**, пишет аномалии в БД, снимок в
   `state.json`, HTML-дашборд.
4. **API.** `main.py` читает `state.json` (mtime-кеш), `metadata.json` и БД, отдаёт REST;
   даунсемплинг графиков делает прямо в БД (≤1500 точек).
5. **UI.** React-дашборд опрашивает API каждые 30 с (TanStack Query), рисует графики
   (Plotly), тепловую карту, журнал событий, KPI.

---

## 3. Ключевой инвариант: граница «обучение / мониторинг»

Аномалии фиксируются **только после** `last_train_timestamp` (флаг `is_live`: время точки
позже границы обучения). На обучающем периоде прогноз in-sample (тривиально точный) и для
алертов не используется. Это делает детекцию по-настоящему онлайн: каждая новая 5-минутка
проверяется на лету. Бэкафилл по истории — отдельная разовая операция, не подменяющая
онлайн-цикл.

---

## 4. Модель данных (PostgreSQL)

Схема станции задаётся в конфиге (`db.schema`); для `ohangaron` — схема `ohangaron`.
Подключение через пул (`station_config.get_db_connection`), параметры из env (`CS_DB_*`).

### `raw_data` — телеметрия (long-формат)
| Столбец | Тип | Описание |
|---------|-----|----------|
| `datetime` | timestamp | Метка времени среза (UTC при чтении → `Etc/GMT-5`) |
| `point` | text | SCADA-тег, напр. `GPA-1.GPA-1.PD.PV` |
| `value` | double | Значение сигнала |
| `health` | text | Существует; кодом аномалий пока не заполняется (план) |

- Одна строка = один тег в один момент. Wide-таблица строится в коде (`_to_wide`),
  округление к 5 мин + `ffill(limit=2)`.
- Объём — миллионы строк, отсюда даунсемплинг графиков в БД и `MAX_HISTORY_DAYS=30`.
- Индекс `idx_raw_data_point_dt (point, datetime DESC)`.

### `anomalies` — журнал детекций (источник правды)
```sql
CREATE TABLE {schema}.anomalies (
    id            BIGSERIAL PRIMARY KEY,
    sensor_id     TEXT NOT NULL,        -- feature-имя (gas_pressure_out_gpa__GPA1)
    event_ts      TIMESTAMPTZ NOT NULL,
    anomaly_type  SMALLINT NOT NULL,    -- код 1..7
    severity      TEXT,                 -- crit/warn/info
    value         DOUBLE PRECISION,
    deviation     DOUBLE PRECISION,     -- отклонение, %
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT anomalies_dedup UNIQUE (sensor_id, event_ts, anomaly_type)
);
```
Дедуп по `(sensor_id, event_ts, anomaly_type)`, запись батчем `ON CONFLICT DO NOTHING`.
Индексы: `idx_anomalies_ts`, `idx_anomalies_sensor_ts`.

### Связь идентификаторов
`raw_data.point` (SCADA-тег) ↔ `sensor_id` (feature) — через `metadata.json`
(`tag_to_name` / `name_to_tag`).

### Часовые пояса
`raw_data.datetime` читается как UTC и переводится в `Etc/GMT-5` (naive). Любая запись
назад (план: `health`) — по исходному UTC, иначе `WHERE datetime=...` не найдёт строк.

---

## 5. ML-дизайн

### Обучение (`train_and_save_models.py`, офлайн)
1. **Маппинг тегов** из БД: `GPA-1.GPA-1.PD.PV → gas_pressure_out_gpa__GPA1`.
2. **Загрузка** истории (или до `--cutoff-date`) в wide; исключаются boolean-датчики (`is_*`).
3. **Фильтр простоев** по оборотам: стоп-строки → NaN, +30 мин прогрева после пуска
   отбрасываются. Так пороги считаются по рабочему режиму.
4. **Признаки** (`_make_features`): другие датчики ГПА + `lag1/2/3` целевого +
   `roll6 mean/std` (30 мин, только прошлое).
5. **Сплит/обучение:** последние 20% — валидация; CatBoost `RMSEWithUncertainty` с early
   stopping; финальный `fit` на 100% данных с `best_iter`.
6. **Метрики:** `r2` (in-sample), `residual_std_val`, `residual_mean_val` (валидационная
   MAE по сути), `sensor_range` (перцентиль 99–1).
7. **Сохранение:** `models/<station>/<sensor>.cbm` + единый `metadata.json`.

**Гиперпараметры:** `iterations=800, depth=6, lr=0.05, RMSEWithUncertainty, subsample=0.8,
l2_leaf_reg=20, seed=42`. Глобальные: `ANOMALY_N_SIGMA=5.0`, `MIN_BUFFER_PCT=0.15`,
`VAR_SMOOTHING=24`, `VAL_FRACTION=0.2`.

### Коридор нормы (ML-детектор)
`y_std` — из неопределённости CatBoost (сглажена `VAR_SMOOTHING`), не ниже
`residual_std_val`. Полуширина:
`hw = max(n_sigma·y_std, sensor_range·min_buffer)·transient_boost`, не ниже `min_abs_error`
и небольшого относительного пола. `transient_boost` расширяет коридор на быстрых переходах.

### Дрейф моделей
`model_drift`: если медиана |остатков| за 7 рабочих дней > 2× `residual_mean_val` — модель
помечается дрейфующей. **Решение проекта:** переобучение не предусмотрено, сигнал
информационный.

---

## 6. Детекторы аномалий (7 типов)

| Код | kind | Детектор | Severity | Логика (кратко) |
|----|------|----------|----------|-----------------|
| 1 | `ml` | Стат./ML-выброс | crit | `|факт−прогноз| > hw`, при `is_live`; подавлен на стоянке/прогреве |
| 2 | `neg` | Сбой физичности | crit | отрицательное значение у неотрицательных датчиков, 2 точки подряд |
| 3 | `frozen` | Залипание | warn | ≥12 одинаковых значений подряд (60 мин); не на стоянке |
| 4 | `roc` | Скачок скорости | warn | `|Δ| > порог` по типу датчика, с анти-спайком; не на переходах |
| 5 | `seasonal` | Сезонная | info | отклонение от почасового профиля > 3σ_h |
| 6 | `regime` | Смена режима | info | синхронный дрейф группы сигналов ГПА |
| 7 | `cross` | Кросс-ГПА | info | z-score относительно тех же датчиков на других ГПА > 2.5, подтверждение 15 мин |

Источник правды словаря — `anomaly_types.py` (`CODE_TO_KIND` / `KIND_TO_CODE` /
`KIND_SEVERITY`). Соглашение `0=OK` — для `health`.

### Подавление ложных срабатываний
- **Стоянка ГПА** (`__running_GPAn < 0.5`): ml/frozen/roc/seasonal подавлены; загазованность мониторится всегда.
- **Прогрев** (+30 мин после пуска): пропускаются только грубые отклонения.
- **Переходы пуск/останов** (±2 точки): roc/cross подавлены как легальные скачки.
- **Смена режима** (окно ±15 мин): ml/roc/seasonal гасятся (кроме очень крупных остатков).
- **fail-open:** нет статус-тега → считаем «работает» (риск ложных на стоянке, аудит D3).

### Эпизодизация
В `_write_live_state` true-тики с разрывом ≤2 тиков (10 мин) объединяются в один
эпизод-событие (`timestamp..ts_end`, `points`, пиковое значение/отклонение). Эти же
эпизоды → строки `anomalies`.

---

## 7. API (FastAPI, `main.py`)

Порт `:8000`, ответы JSON, CORS только `http://localhost:<port>`. Канонический префикс —
`/api/stations/{station_id}/...`; есть compat-алиасы без `station_id` (→ `ohangaron`).
Время в сериях/событиях — naive ISO в `Etc/GMT-5`.

| Метод | Путь | Назначение |
|-------|------|-----------|
| GET | `/api/stations` | Список станций |
| GET | `/api/stations/{id}/sensors[?gpa]` | Датчики (метаданные, severity, счётчики) |
| GET | `/api/stations/{id}/sensors/{sensor_id}` | Один датчик |
| GET | `/api/stations/{id}/stats` | KPI: счётчики по severity и типам |
| GET | `/api/stations/{id}/events` | События из `state.json` (фильтры `severity/gpa/kind/limit/days`) |
| GET | `/api/stations/{id}/sensors/{sensor_id}/chart` | График: факт + прогноз + коридор + аномалии (`days` или `t0`/`t1`) |
| GET | `/api/stations/{id}/heatmap[?gpa]` | Тепловая карта датчик×ГПА |
| GET | `/api/stations/{id}/anomalies` | Исторические аномалии из БД (`limit≤1000`, `sensor_id`) |
| GET | `/api/stations/{id}/pvsnapshot` | Последний срез `tag→{v,sev}` для мнемосхемы |
| GET | `/api/health` | Сводный статус (200 ok / 503 degraded/down) |

**Кеширование (`Cache-Control`):** живые ответы — `max-age=25`; исторические окна графика
(конец > 1 ч назад) — `max-age=3600`. Пороги health: state > 15 мин → `degraded/stale`,
> 60 мин → `down`; БД недоступна → `degraded`.

---

## 8. Фронтенд (`frontend/`)

SPA-дашборд оператора: **React 19 + Vite 8 + TypeScript + Plotly + TanStack Query +
Tailwind**.

- **Данные:** `src/api/client.ts` — обёртка над `fetch`; все запросы через TanStack Query с
  `refetchInterval` (станции 60 с, остальное 30 с). Поллинг 30 с — основной механизм
  «реального времени» (бэкенд обновляет state каждые 5 мин).
- **Графики:** факт + модель + коридор + маркеры аномалий; зум/пан (Plotly `uirevision`,
  `debounce` 300 мс), ленивый `plotly.js-basic-dist-min`, даунсемплинг на сервере.
- **Представления (`activeView`):** `monitor | schema | engine`.
- **Состояние UI:** `useState` + `localStorage` (тема `cs-theme`, сайдбар `cs-sidebar`,
  квитированные события `cs-acked`, активная станция `cs-station`).

Ключевые компоненты: `Sidebar`, `SensorChart`, `HeatMap`, `StatsGrid`, `EventDrawer`,
`Ticker`, `PriorityBanner`, `StationSwitcher`, `SchemaPanel`, `EnginePanel`, `KioskMode`,
`ShiftReport`, `ApiErrorBanner`, `ErrorBoundary`, `Landing`. Типы в `src/types/index.ts`
зеркалят контракт API.

---

## 9. Надёжность и эксплуатация

- **Пул соединений** PostgreSQL (`ThreadedConnectionPool`, 2–10) на станцию, TCP keepalive,
  fail-fast по таймауту.
- **single-instance lock** (`logs/<name>.lock`) — второй `live_predict` не стартует.
- **Graceful shutdown** по SIGINT/SIGTERM.
- **Backoff** в цикле: исключение одного цикла не убивает процесс; при `MemoryError` —
  сброс данных и перезагрузка истории.
- **Атомарная запись** `state.json` (`*.tmp` → `os.replace`) каждый цикл.
- **Кеши:** mtime-кеш `state.json`/`metadata.json`; HTTP `Cache-Control`; кеш `SELECT 1`
  для health (30 с).

### Топология запуска (сейчас)
```
[ Windows-сервер + venv ]
   ├─ scripts/start_backend.bat      → uvicorn main:app :8000 (4 воркера), рестарт 10с
   └─ scripts/start_live_predict.bat → python live_predict.py --mode live, рестарт 30с
[ PostgreSQL ]  (CS_DB_HOST:CS_DB_PORT/CS_DB_NAME, схема ohangaron)
[ frontend ]    Vite dev (npm run dev) или сборка (npm run build) за статикой
```
Планируется единый запуск `run_system.py` (миграции → проверка моделей → API + онлайн-цикл).

### Технологии
Python 3.11, FastAPI, uvicorn, pandas, numpy, CatBoost (`RMSEWithUncertainty`),
scikit-learn, psycopg2, PyYAML, python-dotenv, joblib, plotly. Frontend: React 19, Vite 8,
TypeScript, Plotly, TanStack Query, Tailwind. Деплой: Dockerfile (только API) + `.bat`-скрипты.

---

## 10. Известный технодолг и план

- **MAE — мёртвая метрика.** `mae` читается API/UI, но обучением не пишется → везде `0.0`.
  Валидационная MAE по сути есть (`residual_mean_val`). План: считать `mae_val/rmse_val/nmae_val`,
  перевести пороги и UI с R² на нормированную MAE (фаза 5).
- **R² in-sample.** `r2_train` считается на 100% данных после refit (оптимистичен) и при этом
  управляет шириной коридора аномалий.
- **`raw_data.health` не заполняется.** План — конвенция кодов (`"0"`=норма, `"2,4,5"`=типы,
  `NULL`=не оценено), запись по исходному UTC.
- **Журнал уведомлений.** Планируемая таблица `"journal notifications"` (операторские
  уведомления с `message` и статусом квитирования) + эндпоинты `/notifications`, `/ack`.
- **Управление схемой ad-hoc.** Сейчас `ensure_anomalies_table`/`fix_anomalies_table.py`/
  `ensure_indexes.py`; план — единый идемпотентный `migrate_db.py`, рекомендация —
  версионируемые миграции.
- **Compat-алиасы API** (A14) — фронт постепенно переводить на канонический префикс.
- **Графики (фазы 8–11):** не рисовать прогноз на обучающем периоде (`train_ts`,
  `connectgaps:false`); мульти-сенсорный график (`/chart/multi`); ускорение
  (батч-запрос, кеш, `scattergl`); UI журнала и здоровья датчика.

Детальный план — [docs/00_implementation_plan.md](docs/00_implementation_plan.md);
полный аудит — [docs/02_architecture_audit.md](docs/02_architecture_audit.md).

---

## 11. Глоссарий
- **ГПА** — газоперекачивающий агрегат (GPA1/2/3).
- **point / тег** — идентификатор сигнала SCADA, напр. `GPA-1.GPA-1.PD.PV`.
- **sensor_id / feature** — нормализованное имя датчика, напр. `gas_pressure_out_gpa__GPA1`.
- **kind / код** — тип аномалии (ml/neg/frozen/roc/seasonal/regime/cross → 1..7).
- **state-файл** — `state/<station>_live_state.json`, снимок последнего цикла детекции.
- **last_train_timestamp** — граница «обучение / мониторинг»; аномалии ищутся только после неё.
