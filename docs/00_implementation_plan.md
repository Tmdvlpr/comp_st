# Единый план реализации: КС — аномалии, здоровье датчиков, графики, единый запуск

**Проект:** CS Monitor AI (`C:\Users\Timur\Desktop\UTG\КС\cs_4`)
**Дата:** 2026-06-15
**Что делаем:** дополняем уже работающую систему выявления аномалий КС:
1. запись кодов аномалий в `raw_data.health` (+ миграция, + бэкафилл всей истории);
2. отдельная таблица `ohangaron."journal notifications"` для уведомлений;
3. обучение моделей один раз на данных до сегодня (без переобучения), **метрика MAE вместо R²**;
4. графики: **несколько датчиков с разных ГПА на одном канвасе**, **ускорение отрисовки**, **без линии прогноза на обучающем (историческом) периоде**;
5. запуск всей системы **одним файлом**;
6. детекция аномалий работает **онлайн на текущих данных** (не только на истории).

> Это **промпт-план**: каждая фаза самодостаточна (цель, файлы, шаги, критерии приёмки, готовый промпт для агента). Изменения в коде сейчас **не вносятся** — только план. Это единый файл; справочная документация — в соседних файлах папки `docs/` (ссылки ниже).

---

## Документация модуля (ссылки)

Этот план опирается на справочную документацию в той же папке `docs/`:

| Документ | О чём |
|----------|-------|
| [README.md](README.md) | Обзор модуля, индекс документации, глоссарий |
| [01_architecture.md](01_architecture.md) | Архитектура, компоненты, потоки данных, топология |
| [02_architecture_audit.md](02_architecture_audit.md) | Полный аудит архитектуры (находки A1–A14, рекомендации) |
| [03_backend_modules.md](03_backend_modules.md) | Справочник модулей backend |
| [04_api_reference.md](04_api_reference.md) | Справочник REST API (эндпоинты, схемы) |
| [05_database.md](05_database.md) | Схема БД (`raw_data`, `anomalies`, `health`, журнал) |
| [06_anomaly_detection.md](06_anomaly_detection.md) | Обучение, 7 детекторов, метрики, онлайн-цикл |
| [09_training_methodology.md](09_training_methodology.md) | Подробный отчёт по методике обучения моделей |
| [07_frontend.md](07_frontend.md) | Архитектура фронтенда, графики |
| [08_operations.md](08_operations.md) | Эксплуатация, запуск, логи, переобучение, диагностика |

---

## 0. Золотые правила (для всех фаз)

1. **Не ломать прод.** Все DDL идемпотентны (`IF NOT EXISTS`); никаких `DROP` рабочих таблиц.
2. **Не переобучать в рантайме.** Обучение однократное (фаза 5); далее модели только загружаются.
3. **Многостанционность.** Никаких хардкодов `ohangaron`/`raw_data` — всё через `StationConfig` (`schema`, `data.table`, …).
4. **Часовые пояса.** `raw_data.datetime` читается как UTC → переводится в `Etc/GMT-5` (naive) внутри пайплайна. Любая запись назад в БД по ключу `(point, datetime)` ведётся по **исходному UTC-времени строки**.
5. **Точечные правки.** Не рефакторить чужой код без необходимости.

---

## 1. Текущая система (результат анализа)

> Полный разбор — в документации: [архитектура](01_architecture.md), [модули backend](03_backend_modules.md), [API](04_api_reference.md), [БД](05_database.md).

### 1.1 Архитектура

| Слой | Технологии | Где |
|------|-----------|-----|
| API | Python 3.11, FastAPI, uvicorn | `backend/main.py` |
| ML-движок | CatBoost (`RMSEWithUncertainty`), pandas, numpy | `backend/live_predict.py` (~2700 строк) |
| Обучение | CatBoost + joblib | `backend/train_and_save_models.py` |
| Доступ к данным | psycopg2 + пул | `backend/data_loader.py`, `backend/station_config.py` |
| Frontend | React 19 + Vite + TS + Plotly + TanStack Query + Tailwind | `frontend/` |
| БД | PostgreSQL, схема `ohangaron` | — |
| Модели | `*.cbm` + `metadata.json` | `backend/models/ohangaron/` |
| Запуск | два `.bat` + venv | `scripts/` |

### 1.2 Модули backend (коротко)

- `station_config.py` — YAML-конфиг станции, пул соединений `get_db_connection(cfg)`.
- `data_loader.py` — `PostgresDataLoader`: чтение `raw_data`→wide, `build_tag_mapping()`, `ensure_anomalies_table()`, `save_anomalies()` (дедуп `ON CONFLICT`).
- `train_and_save_models.py` — обучение на 100% данных, флаг `--cutoff-date`, сохранение `*.cbm`+`metadata.json`; фильтрует простои ГПА.
- `live_predict.py` — ядро: грузит модели, тянет данные после `last_train_timestamp`, считает **7 детекторов**, пишет аномалии в `anomalies`, пишет `state/…_live_state.json`, генерит HTML-дашборд; режимы `--mode once|live`.
- `main.py` — FastAPI: sensors/stats/events/**chart**/heatmap/anomalies/health.
- `anomaly_types.py` — словарь кодов (см. 1.4). `ensure_indexes.py` — индексы (образец DDL). `logging_config.py` — `setup`/`single_instance`/`install_signal_handlers`. `tests/test_smoke.py` — pytest.

### 1.3 Схема БД (`ohangaron`)

```
raw_data ( datetime, point, value, health )      -- health СУЩЕСТВУЕТ, кодом НЕ заполняется
anomalies ( id, sensor_id, event_ts TIMESTAMPTZ, anomaly_type SMALLINT,
            severity, value, deviation, created_at, UNIQUE(sensor_id,event_ts,anomaly_type) )
```

`point` — SCADA-тег (`GPA-1.GPA-1.PD.PV`); `sensor_id` — feature-имя (`gas_pressure_out_gpa__GPA1`). Связь — в `metadata.json` (`tag_to_name`/`name_to_tag`). Подробно: [05_database.md](05_database.md).

### 1.4 Словарь аномалий (`anomaly_types.py`) — оставляем как есть

| Код | kind | Описание | Severity |
|----|------|----------|----------|
| 1 | `ml` | ML/стат. отклонение от модели | crit |
| 2 | `neg` | Отрицательное/нефизичное значение | crit |
| 3 | `frozen` | Залипание (≥12 равных) | warn |
| 4 | `roc` | Скачок скорости | warn |
| 5 | `seasonal` | Отклонение от почасового профиля | info |
| 6 | `regime` | Смена режима | info |
| 7 | `cross` | Кросс-ГПА отклонение | info |

> Соглашение для `health`: **0 = OK** (оценено, аномалий нет), **NULL** = не оценивалось. Как работают детекторы — [06_anomaly_detection.md](06_anomaly_detection.md).

### 1.5 Графики (frontend)

- Эндпоинт `GET /api/stations/{id}/sensors/{sensor_id}/chart?days|t0|t1` → `SensorChartResponse{ series:[{t,v,p,lo,hi}], anomalies, r2, mae, … }`. Даунсемплинг в БД (адаптивный бакет, цель ≤1500 точек). `p/lo/hi` берутся из `live_state` и **присылаются на всю историю, включая обучающий период**.
- `frontend/src/components/Chart/SensorChart.tsx` — Plotly (ленивый импорт `plotly.js-basic-dist-min`), `Plotly.react` на обновления, purge только при смене датчика, debounce зума 300мс. Один датчик на канвас + уже есть проп `overlaySeries` (оверлей **того же** датчика по разным ГПА). Линия модели (`p`) рисуется **везде**, включая историю.
- `frontend/src/api/client.ts` — `api.sensorChart(...)`. `frontend/src/types/index.ts` — типы. Подробно: [07_frontend.md](07_frontend.md).

### 1.6 Запуск (сейчас)

`scripts/start_backend.bat` (uvicorn :8000) + `scripts/start_live_predict.bat` (ML-цикл 5 мин). Обучение — вручную. Подробно: [08_operations.md](08_operations.md).

---

## 2. Требования и gap-анализ

| # | Требование | Статус | Фаза(ы) |
|---|------------|--------|---------|
| 1 | Аномалии: статистика + ML | ✅ есть | — |
| 2 | Обучить до сегодня; файлы моделей + метаданные на сервере; не переобучать | 🟡 почти | 5 |
| 3 | Все аномалии каждого датчика в таблицу (здоровье) | ✅ есть (`anomalies`) | 2,3 |
| 4 | Словарь аномалий (1,2,…) | ✅ есть | 1.4 |
| 5 | Писать аномалии в `raw_data.health`; миграция; дополнить столбец | ❌ нет | 1,2,4 |
| 6 | Таблица `ohangaron."journal notifications"` | ❌ нет | 1,3,11 |
| 7 | Запуск одним файлом, всё интегрировано | ❌ нет | 12 |
| 8 | **MAE вместо R²** (+ мёртвая метрика `mae`=0) | 🔴 баг | 5 (+6,7) |
| 9 | **Разные датчики/ГПА на одном канвасе** | 🟡 частично (только тот же датчик) | 9 |
| 10 | **Ускорить отрисовку графиков** | 🟡 можно лучше | 8,10 |
| 11 | **Не показывать прогноз на исторических данных** | ❌ рисуется везде | 8 |
| 12 | **Детекция онлайн на текущих данных** | ✅ есть (5-мин цикл) | 12 (запуск) |

---

## 3. Принятые решения

1. **Коды аномалий** — без изменений (1=ML … 7=cross), + `0=OK` для `health`.
2. **`raw_data.health` хранит НЕСКОЛЬКО кодов**: `TEXT`, отсортированный список через запятую (`"1,4"`), `"0"`=норма, `NULL`=не оценено. Детальная история — в `anomalies` (источник правды).
3. **Охват `health`** — вся история + новые данные (бэкафилл, фаза 4 → далее инкремент в `live_predict`).
4. **Единый запуск** — `backend/run_system.py` (миграции → проверка моделей → API + ML вместе, graceful shutdown). `scripts/start_all.bat` — тонкая обёртка.
5. **Метрика** — MAE/нормированная MAE вместо in-sample R² (и в порогах, и в UI). R² оставить как вторичную честную метрику.
6. **Прогноз на графике** — `p/lo/hi` только для периода мониторинга (`t > last_train_timestamp`); на обучающем периоде — только факт. В `SensorChartResponse` добавить `train_ts`.
7. **Мульти-график** — по умолчанию нормализация серий (для сопоставимой формы при разных единицах), с переключателем «реальные единицы / мультиось» для 2–3 датчиков.
8. **Детекция онлайн (обязательно).** Выявление аномалий работает в реальном времени на **текущих** данных, а не только на истории. При запуске система поднимает непрерывный ML-цикл (`live_predict --mode live` → `run_continuous`, интервал 5 мин): каждый цикл берёт свежий срез из `raw_data`, прогоняет детекторы по новым точкам (`is_live`: время > `last_train_timestamp`) и пишет результат в `anomalies` + `raw_data.health` + `"journal notifications"`. Бэкафилл (фаза 4) — отдельный однократный проход по истории; он **не заменяет** онлайн-цикл. Единый запуск (фаза 12) обязан стартовать именно `run_continuous` (не `--once`).

---

## 4. Изменения в БД (модуль `backend/migrate_db.py`)

Идемпотентно, по образцу `ensure_indexes.py`. Запуск оркестратором при старте и вручную (`python migrate_db.py --station ohangaron`). Полная схема — [05_database.md](05_database.md).

```sql
-- health: гарантировать TEXT
ALTER TABLE {schema}.raw_data ADD COLUMN IF NOT EXISTS health TEXT;
-- (если health есть с другим типом — безопасно привести через временный столбец)

-- журнал уведомлений (имя С ПРОБЕЛОМ → всегда sql.Identifier!)
CREATE TABLE IF NOT EXISTS {schema}."journal notifications" (
    id BIGSERIAL PRIMARY KEY,
    station_id TEXT NOT NULL, sensor_id TEXT NOT NULL, point TEXT, gpa TEXT,
    event_ts TIMESTAMPTZ NOT NULL, anomaly_type SMALLINT NOT NULL,
    kind TEXT, severity TEXT, value DOUBLE PRECISION, deviation DOUBLE PRECISION,
    message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT journal_notifications_dedup UNIQUE (sensor_id, event_ts, anomaly_type)
);
CREATE INDEX IF NOT EXISTS idx_journal_notif_ts     ON {schema}."journal notifications" (event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_journal_notif_status ON {schema}."journal notifications" (status, event_ts DESC);
-- индекс raw_data (point, datetime) уже создаётся ensure_indexes.py — нужен для UPDATE health
```

> Имя `"journal notifications"` (с пробелом) хранить в `config/global.yaml` (`journal_table: "journal notifications"`); в коде — только `psycopg2.sql.Identifier`, без конкатенации строк.

---

## 5. Фазы реализации

### Группа A — БД и конвейер данных

#### Фаза 1 — Миграции БД
**Цель:** `migrate_db.py`: `health TEXT`, таблица `"journal notifications"`, индексы.
**Файлы:** ➕`backend/migrate_db.py`; ✏️`backend/config/global.yaml`.
**Шаги:** функция `migrate(station_id)` (схема/таблица из `StationConfig`); проверить тип `health` через `information_schema.columns` и привести к `TEXT` без потери данных; создать журнал и индексы (раздел 4); транзакции, логирование, повторный запуск без эффекта.
**Критерии:** повторный запуск чистый; `\d raw_data` → `health TEXT`; журнал создан; старые данные не тронуты.
**Промпт:**
> Создай идемпотентный `backend/migrate_db.py` по образцу `ensure_indexes.py` (StationConfig, get_db_connection, psycopg2.sql, логирование, CLI `--station` default ohangaron). Он: (1) гарантирует `health TEXT` в `{schema}.raw_data` (добавить/безопасно привести); (2) создаёт `{schema}."journal notifications"` по DDL из раздела 4 через `sql.Identifier("journal notifications")`; (3) создаёт индексы из раздела 4. Имя журнала вынеси в `config/global.yaml` (`journal_table`). Все DDL — `IF NOT EXISTS`, данные не трогать.

#### Фаза 2 — Запись `health` в инкрементальном цикле
**Цель:** на каждом цикле `live_predict` писать коды (или `0`) в `raw_data.health`.
**Файлы:** ✏️`backend/data_loader.py` (`update_health`), ✏️`backend/live_predict.py` (`_write_live_state`).
**Шаги:** `update_health(rows)` — батчевый `UPDATE … SET health WHERE point AND datetime` (`execute_batch`, чанки 2000); в `_write_live_state` собрать по `(sensor_id, время)` множество кодов (маски → `KIND_TO_CODE`), `sensor_id→point` (`name_to_tag`), локальное время → исходный UTC; оценённым здоровым → `"0"`, неоценённым → `NULL`; вызов в `try/except`.
**Критерии:** коды в `health` совпадают с `anomalies` за тот же `(sensor,ts)`; сбой БД не роняет цикл.
**Промпт:**
> Добавь `PostgresDataLoader.update_health(rows)` — батчевый `UPDATE {schema}.raw_data SET health=%(health)s WHERE point=%(point)s AND datetime=%(datetime)s` (`execute_batch`, чанки 2000). В `live_predict._write_live_state` собери по `(sensor_id, время)` коды сработавших масок (через `KIND_TO_CODE`), переведи `sensor_id→point` (`metadata['name_to_tag']`), а локальное naive-время → исходный UTC из `raw_df`; здоровым оценённым ставь `"0"`, неоценённым — `NULL`. Вызови `update_health` в try/except рядом с `save_anomalies`.

#### Фаза 3 — Журнал уведомлений
**Цель:** на каждый эпизод аномалии — запись в `"journal notifications"`.
**Файлы:** ✏️`backend/data_loader.py` (`save_notifications`), ✏️`backend/live_predict.py`.
**Шаги:** `save_notifications(records)` (INSERT … `ON CONFLICT … DO NOTHING`, имя из `global.yaml` через `sql.Identifier`); переиспользовать готовые `state_events` (sensor_id, point=`name_to_tag`, gpa, event_ts(UTC), anomaly_type=`KIND_TO_CODE[kind]`, kind, severity, value, deviation, `message` — читаемый текст, `status='new'`); вызов в `try/except`.
**Критерии:** новые аномалии в журнале со `status='new'`, без дублей.
**Промпт:**
> Добавь `PostgresDataLoader.save_notifications(records)` (батчевый INSERT в `{schema}."journal notifications"` с `ON CONFLICT ON CONSTRAINT journal_notifications_dedup DO NOTHING`, имя из global.yaml через sql.Identifier). В `_write_live_state` сформируй записи из `state_events` (message — человекочитаемый русский текст, status='new', point=name_to_tag) и вызови в try/except рядом с save_anomalies.

#### Фаза 4 — Бэкафилл `health` по всей истории
**Цель:** однократно прогнать детекторы по всем `raw_data` до сегодня и заполнить `health`.
**Файлы:** ➕`backend/backfill_health.py`.
**Шаги:** переиспользовать `live_predict` (модели, `prepare_wide_data`, `predict_sensor`, кросс/режим), **без HTML и без цикла**; грузить окнами (≈30 дн, перекрытие ≥1 дн для lag/rolling/roc); звать `update_health`; CLI `--station/--from/--to/--window-days/--with-journal`(default off); идемпотентно, лог прогресса, устойчиво к OOM.
**Критерии:** доля `NULL` минимальна (только реально неоценимые); сверка с `live_predict` совпадает; повтор без роста памяти.
**Промпт:**
> Создай `backend/backfill_health.py` на функциях `live_predict` (без HTML/цикла). Грузит `raw_data` окнами по `--window-days` (default 30) с перекрытием ≥1 дн, считает маски и зовёт `update_health` по всей истории до `--to` (default сегодня). CLI: `--station/--from/--to/--window-days/--with-journal`(off). Идемпотентно, лог прогресса, тримминг от OOM. Здоровым — `"0"`, неоценимым — `NULL`.

---

### Группа B — Модели и детекция

> **Аудит обучения/детекции (находки).** Подробно — [06_anomaly_detection.md](06_anomaly_detection.md) и [02_architecture_audit.md](02_architecture_audit.md). Кратко:
>
> | ID | Находка | Серьёзность | Закрывает фаза |
> |----|---------|-------------|----------------|
> | T1 | `mae_train` читается (`main.py:223`, дашборд), но **никогда не пишется** → MAE везде `0.00`. Валидационная MAE уже считается как `residual_mean_val`. | 🔴 | 5 |
> | T2 | R² считается **in-sample** (на 100% данных после финального refit) → завышен, переобучение не видно. | 🔴 | 5/6 |
> | T3 | Оптимистичный R² **управляет порогом** аномалий (`min_abs_pct`, `live_predict:306-311`). | 🟡 | 5 |
> | T4 | Обучение на данных **с реальными аномалиями**, без робастного отсева → учит аномалии как норму. | 🟡 | 6 |
> | T5 | AR-признаки (lag1/2/3 + roll6) **маскируют медленный дрейф**. | 🟡 | 7 |
> | T6 | Контемпоральные предикторы: общий отказ группы датчиков не ловится (дизайн-предел). | 🔵 | документировать |
> | T7 | Бедные метаданные (нет размеров выборок, важности фич, версии). | 🔵 | 6 |
> | T8 | Стейловые `.cbm` для `is_*` датчиков. | 🔵 | 6 |
> | D1 | Пороги детекторов **не откалиброваны** по факт. доле ложных/пропусков. | 🟡 | 7 |
> | D2 | Неопределённость CatBoost (`y_var`) не проверена на калибровку; `n_sigma=5` «на глаз». | 🟡 | 7 |
> | D3 | `fail-open` по статусу (нет статуса → «работает») → ложные на остановленном. | 🔵 | 7 |
> | D4 | Нет сводной метрики качества детекции. | 🔵 | 7 |
>
> **Главное (R²→MAE):** валидационная MAE уже есть (`residual_mean_val`). Поле `mae` уже в контракте API и типах фронта — оно просто пустое. Задача — достать MAE, добавить нормированную MAE, заменить R² на MAE в порогах и UI.

#### Фаза 5 — Обучение до сегодня + MAE вместо R² (основная ML-фаза)
**Цель:** обучить модели на данных до сегодня; честная метрика MAE; убрать зависимость порогов от in-sample R².
**Файлы:** ▶️/✏️`train_and_save_models.py`, ✏️`live_predict.py` (пороги+дашборд), ✏️`main.py` (SensorMeta.mae), ✏️`frontend/src/types/index.ts`, ✏️`frontend/src/components/Chart/SensorChart.tsx`.
**Шаги:**
1. Запустить обучение до сегодня → `models/ohangaron/*.cbm` + `metadata.json` (last_train_timestamp ≈ сегодня).
2. В `train_single_sensor` писать `mae_val` (=`residual_mean_val`), `rmse_val`, `nmae_val=mae_val/sensor_range`; для совместимости `mae_train=mae_val`.
3. В `predict_sensor` заменить лестницу `min_abs_pct` на основе `r2_train` на лестницу по `nmae_val` (хуже модель = шире коридор); опц. `min_abs_error=max(min_abs_pct·sr, 3·mae_val)`.
4. UI: показывать MAE (физ. ед.) вместо/вместе с R² (дашборд, `SensorChart.tsx`); заполнить `SensorMeta.mae`.
5. `model_drift` → только информационный лог (`retrain_recommended=false`): переобучение не предусмотрено.
**Критерии:** `metadata.json` свежий, есть `mae_val/rmse_val/nmae_val`; UI показывает ненулевую MAE; порог зависит от nMAE/MAE; рантайм только грузит модели.
**Промпт:**
> (1) Запусти `train_and_save_models.py --station ohangaron` (данные до сегодня). (2) В `train_single_sensor` добавь в metadata `mae_val`(=residual_mean_val), `rmse_val`, `nmae_val=mae_val/sensor_range` и `mae_train=mae_val`. (3) В `live_predict.predict_sensor` замени лестницу `min_abs_pct` с `r2_train` на `nmae_val`; завяжи `min_abs_error` на `mae_val`. (4) Покажи MAE вместо/вместе с R² в дашборде, `main.py` (SensorMeta.mae) и фронте (types + SensorChart). (5) `model_drift` сделай информационным (`retrain_recommended=false`). Контракт `mae` не меняй.

#### Фаза 6 — (опц.) Честная валидация, робастное обучение, метаданные
**Шаги:** R²/MAE/RMSE считать на **отложенной валидации** (in-sample → `r2_insample`); робастный отсев точек `|resid|>k·MAD` (или trim 1%) перед финальным refit (T4); метаданные `n_train/n_val/n_features/feature_importance/диапазон дат/время` (T7); не сохранять `.cbm` для `is_*` (T8).
**Промпт:**
> В `train_and_save_models.py`: метрики на отложенной валидации (in-sample как `r2_insample`); робастный отсев больших остатков перед финальным refit (k·MAD/trim 1%, 1–2 итерации); богатые метаданные (n_train, n_val, n_features, top feature_importance, диапазон дат, время); не сохранять модели для `is_*`. Рантайм не переобучает.

#### Фаза 7 — (опц.) Калибровка детекторов + трендовый дрейф
**Шаги:** измерять покрытие коридора и частоту алармов на датчик (D1,D4); калибровать `n_sigma`/коэффициенты под целевую долю ложных <1–2% и проверить калибровку `y_var` (D2); трендовый (не-AR) детектор медленного дрейфа — наклон остатка за N часов (T5); для критичных датчиков убрать `fail-open`/понижать severity (D3).
**Промпт:**
> Добавь измерение покрытия коридора и частоты срабатываний на датчик (лог/отчёт); откалибруй n_sigma/пороги под долю ложных <1–2% на рабочем режиме; проверь калибровку y_var; добавь трендовый не-AR детектор медленного дрейфа (наклон остатка за N часов); для критичных датчиков убери fail-open по статусу или понижай severity.

---

### Группа C — Графики и фронтенд

> Архитектура фронтенда и текущее состояние графиков — [07_frontend.md](07_frontend.md).

#### Фаза 8 — Не показывать прогноз на исторических (обучающих) данных
**Цель:** линия модели `p` и коридор `lo/hi` — только для периода мониторинга (`t > last_train_timestamp`); на обучающем периоде — только факт `v`. Логично (на train прогноз = in-sample, неинформативен) и легче по объёму.
**Файлы:** ✏️`backend/main.py` (`station_sensor_chart`, `SensorChartResponse`), ✏️`frontend/src/components/Chart/SensorChart.tsx`, ✏️`frontend/src/types/index.ts`.
**Шаги:**
1. В `station_sensor_chart` прочитать `last_train_timestamp` (через `_get_metadata`); для точек `t <= train_ts` ставить `p=lo=hi=None` (факт `v` оставить). Это же уменьшает JSON.
2. Добавить в `SensorChartResponse` поле `train_ts` (ISO).
3. Фронт: на трейсах pred/lo/hi выставить `connectgaps:false`; нарисовать вертикальный разделитель на `train_ts` с подписью «Начало мониторинга» (как в HTML-дашборде).
**Критерии:** до `train_ts` — только факт; линия модели/коридор начинаются с `train_ts`; payload меньше; есть разделитель.
**Примечание:** после фазы 5 `train_ts`≈сегодня → прогноз появляется только для новых данных (это корректно). Факт `v` отображается за всю историю как и раньше.
**Промпт:**
> В `main.py.station_sensor_chart` бери `last_train_timestamp` из metadata и для точек серии с `t <= train_ts` ставь `p=lo=hi=None` (факт `v` не трогай). Добавь поле `train_ts` в `SensorChartResponse`. На фронте в `SensorChart.tsx` поставь `connectgaps:false` на трейсы модели/границ и добавь вертикальный shape-разделитель на `train_ts` с подписью «Начало мониторинга». Тип `SensorChartResponse` дополни `train_ts`.

#### Фаза 9 — Мульти-сенсорный график (разные датчики/ГПА на одном канвасе)
**Цель:** выбирать произвольный набор датчиков (в т.ч. разные ГПА и разные типы) и рисовать на одном графике для сравнения трендов/корреляций.
**Файлы:** ✏️`backend/main.py` (➕эндпоинт `/api/stations/{id}/chart/multi`), ✏️`frontend/src/api/client.ts` (➕`multiChart`), ✏️`frontend/src/types/index.ts`, ➕компонент сравнения в `frontend/src/components/Chart/` + пикер датчиков.
**Дизайн:**
- Бэкенд: `chart/multi?sensors=id1,id2,…&days|t0|t1` → на каждый датчик даунсемплированная серия `v` (как `_fetch_raw_db_series`, ≤1500 точек) + мета `{tag,gpa,unit,range_min,range_max}`. Предикт/коридор для оверлеев **не нужны** (только факт) → ответ лёгкий и быстрый.
- Разные единицы/масштабы: по умолчанию **нормализация** (min-max или z-score) — сопоставимая форма; переключатель «реальные единицы / мультиось» (Plotly `yaxis`,`yaxis2`… для 2–3 датчиков).
- Цвета/легенда: устойчивая палитра по датчику; в легенде «имя · ГПА-n». Лимит серий (≈6–8) для читаемости/скорости.
- UI: мультивыбор датчиков (чекбоксы в Sidebar/HeatMap или панель «Сравнение»); единый X (зум/пан синхронны).
**Критерии:** ≥3 датчика с разных ГПА на одном канвасе; переключение единицы/нормировка; ≤1500 точек на серию; зум по X общий.
**Промпт:**
> Бэкенд: добавь `GET /api/stations/{id}/chart/multi?sensors=a,b,c&days|t0|t1`, возвращающий список `{sensor_id, tag, gpa, unit, range_min, range_max, series:[{t,v}]}` — даунсемплинг как в `_fetch_raw_db_series` (≤1500 точек на датчик), без предикта. Фронт: метод `api.multiChart(...)`, новый компонент сравнения на Plotly (N трейсов «факт», общий X), мультивыбор датчиков (в т.ч. с разных ГПА), переключатель «нормировано (по умолчанию) / реальные единицы (мультиось)», устойчивая палитра, лимит ~8 серий. Тип ответа добавь в `types/index.ts`.

#### Фаза 10 — Оптимизация скорости отрисовки графиков
**Цель:** быстрее открывать и обновлять графики.
**Где задержки сейчас:** размер JSON (`p/lo/hi` на всю историю — частично решает фаза 8), слияние state-серии в Python, один датчик = один запрос (для сравнения — N запросов).
**Шаги:**
1. **Меньше данных:** фаза 8 срезает `p/lo/hi` на истории; округление уже есть; не слать `lo/hi`, если близки к `p` в пределах эпсилона.
2. **Батч:** мульти-график одним запросом (фаза 9), не N.
3. **Кеш:** HTTP `max-age` для исторических окон уже есть — усилить; на фронте TanStack Query `staleTime`/`gcTime` для chart-запросов, ключ = (sensor(s), days, t0, t1).
4. **WebGL (опц.):** для серий >~2000 точек — `scattergl`; оценить переход на gl-сборку Plotly при необходимости (сейчас basic-dist-min без gl).
5. **Рендер:** сохранить `Plotly.react` + purge только при смене датчика + debounce зума.
6. **(опц.) Серверное предагрегирование** длинных диапазонов (материализованные 5-мин/1-час бакеты), если БД станет узким местом.
**Критерии:** замер времени до первого рендера и обновления (до/после), заметное снижение на длинных окнах; нет лишних перерисовок.
**Промпт:**
> Ускорь графики: (1) убедись, что фаза 8 убрала `p/lo/hi` на истории, и не присылай `lo/hi`, совпадающие с `p` в пределах эпсилона; (2) для мульти-графика — один батч-запрос; (3) добавь TanStack Query `staleTime`/`gcTime` для chart-запросов с ключом (sensors,days,t0,t1) и усиль HTTP-кеш исторических окон; (4) опционально переключай серии >2000 точек на `scattergl`; (5) сохрани `Plotly.react`/purge-on-sensor-change/debounce. Замерь время рендера до/после.

---

### Группа D — Интеграция и проверка

#### Фаза 11 — API: журнал и здоровье
**Цель:** дать фронту/операторам читать уведомления и здоровье датчиков.
**Файлы:** ✏️`backend/main.py`.
**Шаги:** `GET /api/stations/{id}/notifications` (фильтры status/severity/sensor_id/limit/days; имя таблицы через `sql.Identifier` из конфига); `POST …/notifications/{nid}/ack` (смена status); опц. `GET …/sensors/{sensor_id}/health` (агрегат по `health`/`anomalies`); backward-compat алиасы на default-станцию. *(Эндпоинты графиков — в фазах 8/9. Полный API — [04_api_reference.md](04_api_reference.md).)*
**Критерии:** данные согласованы с таблицами; фильтры/ack работают.
**Промпт:**
> В `main.py` добавь `GET /api/stations/{id}/notifications` (чтение `"journal notifications"` с фильтрами status/severity/sensor_id/limit/days, имя через sql.Identifier из global.yaml) и `POST …/notifications/{nid}/ack`. Опц. `GET …/sensors/{sensor_id}/health` (агрегат). Сохрани стиль существующих эндпоинтов (Pydantic, Cache-Control, обработка ошибок, backward-compat алиас).

#### Фаза 12 — Единый запуск `run_system.py`
**Цель:** один файл поднимает всё: миграции → проверка моделей → API + онлайн-ML, graceful shutdown.
**Файлы:** ➕`backend/run_system.py`, ➕`scripts/start_all.bat`, ✏️`scripts/README.md`.
**Шаги:** `setup()`+`single_instance("run_system")`+`install_signal_handlers`; шаг1 `migrate_db.migrate` + `ensure_indexes`; шаг2 проверить `models/<station>/metadata.json` (нет → внятная ошибка или обучить при `--train-if-missing`); шаг3 параллельно поднять uvicorn (programmatic Server, поток) и `live_predict.run_continuous()` (поток) — **онлайн-детекция**; шаг4 по SIGINT/SIGTERM остановить оба. CLI `--station/--api-port/--no-api/--no-ml/--once/--train-if-missing`. `start_all.bat` — venv + запуск с авторестартом.
**Критерии:** `python run_system.py` поднимает API (`/api/health`→200) и онлайн ML-цикл (через ≤5 мин растут `anomalies`/журнал/`health`/state на текущих данных); Ctrl+C гасит оба; повторный запуск блокируется (single_instance).
**Промпт:**
> Создай `backend/run_system.py` — оркестратор: logging_config (setup/single_instance/install_signal_handlers); (1) `migrate_db.migrate(station)`+`ensure_indexes`; (2) проверка `models/<station>/metadata.json` (или обучение при `--train-if-missing`); (3) параллельно uvicorn (programmatic, поток) и `live_predict.run_continuous()` (поток, ОНЛАЙН-детекция); (4) graceful shutdown по сигналам. CLI `--station/--api-port/--no-api/--no-ml/--once/--train-if-missing`. Добавь `scripts/start_all.bat` (venv+авторестарт) и обнови `scripts/README.md`. Не дублируй live_predict (single_instance уже защищает).

#### Фаза 13 — Тесты и проверка
**Цель:** не сломать систему; подтвердить новое.
**Файлы:** ✏️`backend/tests/test_smoke.py` (+тесты), фронт — ручная проверка/линт.
**Шаги:** импорт-смоук (`migrate_db/backfill_health/run_system`); юниты (сериализация `health`; sensor_id↔point; TZ-конверсия; срез прогноза по `train_ts`); интеграция на тест-БД (миграция → `live_predict --once` → согласованность `anomalies`↔`health`↔журнал); мульти-график возвращает ≤1500 точек/серию; `pytest tests/` + `npm run lint` зелёные.
**Промпт:**
> Дополни `tests/test_smoke.py`: импорт-тесты `migrate_db/backfill_health/run_system`; юниты сериализации `health`, маппинга sensor_id↔point, TZ-конверсии, среза `p/lo/hi` по `train_ts`; интеграционный тест (маркер, нужна БД): миграция → `live_predict` один цикл → сверка `health`↔`anomalies`↔журнал; проверь, что `chart/multi` отдаёт ≤1500 точек на серию. `pytest tests/` и `npm run lint` должны проходить.

---

## 6. Подводные камни

- **Часовые пояса (критично).** Запись `health`/журнала назад в БД — по **исходному UTC** строки `raw_data`, не из локального naive. Вести map `local_5min → original_utc`. (Аудит: A6, [02_architecture_audit.md](02_architecture_audit.md).)
- **Гранулярность.** Пайплайн округляет к 5 мин и дедуплит. Если шаг `raw_data` ≠ 5 мин — один «срез» = несколько строк; решить заранее: точное совпадение `datetime` или диапазон 5-мин бакета.
- **Имя `"journal notifications"`** — всегда `sql.Identifier`; хранить в `global.yaml`.
- **Производительность `UPDATE raw_data`** — индекс `(point, datetime)` (есть), батчи, окна в бэкафилле.
- **`NULL` vs `"0"`** — не путать: `NULL`=не оценивалось, `"0"`=норма.
- **Прогноз ↔ ретрейн.** После фазы 5 `last_train_timestamp`≈сегодня, поэтому при фазе 8 прогноз появится только для новых данных (это корректно, но визуально «модель пустая» до накопления мониторинга).
- **Мульти-график — единицы.** Разные шкалы → по умолчанию нормировать; мультиось только для 2–3 серий.
- **Идемпотентность** — миграции/health/журнал не плодят дубли и не меняют результат при повторе.
- **Совместимость словаря** — единый источник `anomaly_types.py` (`KIND_TO_CODE`/`CODE_TO_KIND`).

---

## 7. Чек-лист приёмки

- [ ] Ф1: `migrate_db.py` идемпотентен; `raw_data.health TEXT`; `"journal notifications"` + индексы созданы.
- [ ] Ф2: `live_predict` пишет коды в `health` (без дублей, в try/except).
- [ ] Ф3: уведомления пишутся в журнал (`status='new'`, читаемый `message`).
- [ ] Ф4: бэкафилл заполнил `health` по всей истории; `NULL` только у неоценимых.
- [ ] Ф5: модели обучены до сегодня; есть `mae_val/rmse_val/nmae_val`; UI показывает MAE; порог завязан на nMAE/MAE; переобучение не рекомендуется.
- [ ] Ф6/Ф7 (опц.): валидационные метрики; робастность; калибровка детекторов; трендовый дрейф.
- [ ] Ф8: на обучающем периоде прогноза нет, есть разделитель `train_ts`; payload меньше.
- [ ] Ф9: ≥3 датчика с разных ГПА на одном канвасе; нормировка/мультиось; ≤1500 точек/серию.
- [ ] Ф10: замерено ускорение рендера; нет лишних перерисовок.
- [ ] Ф11: API журнала/здоровья работает (фильтры, ack).
- [ ] Ф12: `run_system.py` поднимает API + онлайн-ML одним файлом; graceful shutdown; single-instance.
- [ ] Ф13: `pytest tests/` и `npm run lint` зелёные; согласованность `anomalies`↔`health`↔журнал.

---

## 8. Карта файлов

| Действие | Файл | Фаза |
|----------|------|------|
| ➕ создать | `backend/migrate_db.py` | 1 |
| ➕ создать | `backend/backfill_health.py` | 4 |
| ➕ создать | `backend/run_system.py` | 12 |
| ➕ создать | `scripts/start_all.bat` | 12 |
| ➕ создать | компонент сравнения в `frontend/src/components/Chart/` | 9 |
| ✏️ править | `backend/data_loader.py` (`update_health`, `save_notifications`) | 2,3 |
| ✏️ править | `backend/live_predict.py` (health/журнал; model_drift→инфо; пороги на nMAE/MAE) | 2,3,5,7 |
| ✏️ править | `backend/train_and_save_models.py` (mae_val/rmse_val/nmae_val; честная валидация; робастность; метаданные) | 5,6 |
| ✏️ править | `backend/main.py` (chart: срез прогноза + `train_ts`; `chart/multi`; notifications/health; SensorMeta.mae) | 5,8,9,11 |
| ✏️ править | `frontend/src/components/Chart/SensorChart.tsx` (connectgaps, разделитель, MAE) | 5,8 |
| ✏️ править | `frontend/src/api/client.ts` (`multiChart`) | 9 |
| ✏️ править | `frontend/src/types/index.ts` (`train_ts`, тип мульти-графика, MAE) | 5,8,9 |
| ✏️ править | `backend/config/global.yaml` (`journal_table`) | 1 |
| ✏️ править | `backend/tests/test_smoke.py` | 13 |
| ✏️ править | `scripts/README.md` | 12 |
| ▶️ запустить | `train_and_save_models.py` (обучение до сегодня) | 5 |
| ✅ без изменений | `station_config.py`, `anomaly_types.py`, `ensure_indexes.py`, `logging_config.py`, `*.cbm` | — |

---

## 9. Порядок выполнения

1. **Ф1** (миграции) — первой.
2. Далее параллельно: **Ф2+Ф3** (запись health/журнал), **Ф5** (обучение+MAE).
3. **Ф4** (бэкафилл) — после Ф1, Ф2, Ф5 (нужны свежие модели).
4. **Ф8 → Ф9 → Ф10** (графики) — Ф8 первой (срез прогноза упрощает остальное).
5. **Ф11** (API журнала) — после Ф1, Ф3.
6. **Ф12** (единый запуск) — после готовности Ф1/Ф2/Ф3/Ф5.
7. **Ф13** (тесты) — по мере готовности фаз, финально — весь прогон.
8. **Ф6/Ф7** (опц. улучшения ML) — в любой момент после Ф5.

Критический путь: **Ф1 → Ф5 → Ф4 → Ф12**. Фронтовые Ф8–Ф10 независимы от БД-группы (кроме Ф8, которой нужен `train_ts` из metadata — есть после Ф5).

---

*Справочная документация модуля: [README](README.md) · [архитектура](01_architecture.md) · [аудит](02_architecture_audit.md) · [модули](03_backend_modules.md) · [API](04_api_reference.md) · [БД](05_database.md) · [детекция](06_anomaly_detection.md) · [фронтенд](07_frontend.md) · [эксплуатация](08_operations.md)*
