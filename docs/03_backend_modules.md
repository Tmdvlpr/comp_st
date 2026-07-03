# 03. Модули backend

> ⚠️ **Актуальный список модулей — в [../ARCHITECTURE.md](../ARCHITECTURE.md) §2 (2026-07-01).** Удалены/слиты: `train_and_save_models.py`, `train_v2.py`, `regime.py`, `calibrator.py`, `domain_features.py`, `data_quality.py` → единый `train.py`; `build_dashboard`/`anomaly_live.html` удалён. Добавлены `run.py`, `run_system.py`.

Все модули — в `backend/`. Импортируют друг друга напрямую (без пакета). Конфигурация — через `StationConfig`.

## station_config.py
Загрузка конфигурации станций из `config/stations/<id>.yaml` с интерполяцией `${ENV:default}`; пул соединений PostgreSQL.

- `@dataclass StationConfig` — `station_id, display_name, db, data, units, models_dir, state_file`; свойства `models_path`, `state_path`.
- `load_station_config(station_id) -> StationConfig` — кэш + валидация `station_id` (regex, защита от path traversal).
- `list_stations() -> list[str]` — станции из `config/stations/*.yaml` (без `_*`).
- `get_db_connection(cfg)` — контекст-менеджер, выдаёт/возвращает соединение из `ThreadedConnectionPool` (2–10, keepalive, `connect_timeout=10`); rollback при исключении.

## data_loader.py
Класс `PostgresDataLoader(cfg)` — доступ к данным и запись аномалий.

- `fetch_training_data(cutoff_date=None)` — вся история (или до даты) в wide-формате.
- `fetch_live_data(hours=2)` — последние N часов (wide).
- `_to_wide(df)` — long→wide: округление времени к 5 мин, dedup `(datetime, point)`, pivot, `ffill(limit=2)`.
- `build_tag_mapping() -> {point: feature}` — из `DISTINCT point`; `_normalize_tag` (`GPA-n.GPA-n.X`/`GT-n.GT-n.X` → `x__GPAn`).
- `ensure_anomalies_table()` — DDL `anomalies` (idempotent).
- `save_anomalies(records) -> int` — батч-INSERT с `ON CONFLICT DO NOTHING` (дедуп); `kind→code` через `KIND_TO_CODE`.

> Планом добавляются: `update_health(rows)` (запись кодов в `raw_data.health`) и `save_notifications(records)` (журнал) — фазы 2/3.

## train_and_save_models.py
Обучение моделей (офлайн). CLI: `--station`, `--cutoff-date`, `--dry-run`, `--list-stations`.

- `_make_features(df, target, predictors)` — предикторы + `lag1/2/3` + `roll6 mean/std` (30 мин), dropna.
- `train_single_sensor(...)` — обучение одной модели: time-split (последние 20% — валидация), early stopping, затем финальный `fit` на 100% с `best_iter`; считает `r2` (in-sample), `residual_std_val`, `residual_mean_val` (= валидационная MAE), `sensor_range`; сохраняет `*.cbm`, возвращает метаданные.
- `train_all(args)` — маппинг тегов, загрузка, фильтр простоев ГПА (по rpm + прогрев), параллельное обучение (joblib) по ГПА, запись `metadata.json`.
- Гиперпараметры: `iterations=800, depth=6, lr=0.05, loss=RMSEWithUncertainty, subsample=0.8, l2_leaf_reg=20`. `ANOMALY_N_SIGMA=5.0`, `MIN_BUFFER_PCT=0.15`, `VAR_SMOOTHING=24`, `VAL_FRACTION=0.2`.

> Аудит метрик (R² in-sample, мёртвый `mae`) и план перехода на MAE — см. [06](06_anomaly_detection.md) и план, фаза 5.

## live_predict.py
**Онлайн-движок детекции.** CLI: `--mode once|live`, `--station`.

- `load_models_and_metadata()` — грузит `metadata.json` + все `*.cbm`.
- `fetch_data_from_db / fetch_latest_slice_from_db` — только SELECT; первый запуск ограничен `MAX_HISTORY_DAYS=30`.
- `prepare_wide_data(raw_df, tag_to_name)` — wide + флаги работы ГПА (`__running_GPAn`) из статус-тегов.
- `predict_sensor(model, df_wide, target, info, metadata)` — прогноз + 7 детекторов (ml/neg/frozen/roc/seasonal + позже cross/regime), коридор по неопределённости, подавление на стоянке/прогреве/переходах. Аномалии только при `is_live` (время > `last_train_timestamp`).
- `run_once(existing_df=None)` — один цикл: данные → прогноз → кросс-ГПА → смена режима → дашборд → `_write_live_state`.
- `_write_live_state(results, metadata)` — формирует `state.json` (сенсоры, серии 30 дн, события-эпизоды, model_drift) и вызывает `save_anomalies`. **Сюда планом добавляются `update_health` и `save_notifications`.**
- `run_continuous()` — бесконечный цикл (5 мин) с backoff; `build_dashboard(...)` — HTML-дашборд.
- `REFRESH_INTERVAL=300`, `OUTPUT_HTML=anomaly_live.html`.

## main.py
FastAPI-приложение (`app`). CORS только `localhost:*`. На старте опционально `ensure_indexes` (флаг `CS_ENSURE_INDEXES=1`).

- Pydantic-модели ответов: `StationInfo, SensorMeta, EventItem, StatsResponse, TimeSeriesPoint, SensorChartResponse, HeatmapCell, AnomalyRecord`.
- Кеши: `_state_cache`, `_meta_cache` (по mtime), `_db_check_cache` (30 с).
- Эндпоинты `/api/stations/...` + compat-алиасы — см. [04_api_reference.md](04_api_reference.md).
- `__main__`: `uvicorn.run("main:app", host=0.0.0.0, port=8000, workers=4)`.

## anomaly_types.py
Единый словарь аномалий (источник правды): `ML=1, NEG=2, FROZEN=3, ROC=4, SEASONAL=5, REGIME=6, CROSS=7`; `CODE_TO_KIND`, `KIND_TO_CODE`, `KIND_SEVERITY`.

## ensure_indexes.py
Идемпотентные индексы: `idx_raw_data_point_dt (point, datetime DESC)`, `idx_anomalies_ts (event_ts DESC)`, `idx_anomalies_sensor_ts (sensor_id, event_ts DESC)`. CLI: `python ensure_indexes.py [station]`. **Образец стиля для будущих миграций.**

## fix_anomalies_table.py
Служебный: `DROP` + `CREATE` таблицы `anomalies` с корректной схемой. Использовать осторожно (теряет данные).

## sync_raw_data.py
Дозапись хвоста `raw_data` из БД в CSV (`raw_data_2.csv`) по последней метке. Подтверждает 4 столбца `raw_data`: `datetime, point, value, health`. Env-driven конфиг БД.

## logging_config.py
- `setup(name, tee_stdout=True)` — root-логгер: `RotatingFileHandler logs/<name>.log` (10МБ×5) + консоль + tee stdout/stderr с таймстампами.
- `single_instance(name) -> bool` — эксклюзивный файл-lock `logs/<name>.lock` (msvcrt/fcntl).
- `install_signal_handlers(cb)` — SIGINT/SIGTERM → cb (graceful shutdown).

## tests/test_smoke.py
Smoke: импорты модулей, консистентность `anomaly_types`, валидация `station_id`, lock, `_severity_rank`. Запуск: `pytest tests/`.

## Карта зависимостей

```
station_config ◄── data_loader ◄── train_and_save_models
       ▲                ▲                 live_predict ──► anomaly_types
       │                │                     │
       └──── main ──────┴─────────────────────┘
logging_config ◄── live_predict, main, train_*
ensure_indexes ◄── main (startup, опц.)
```
