# CS Monitor AI — документация модуля

Система выявления аномалий компрессорной станции (КС) на статистике и ML. Мониторинг датчиков ГПА, прогноз ожидаемых значений, выявление отклонений в реальном времени, журнал событий, веб-дашборд.

**Версия документации:** 2026-06-15 · **Станция по умолчанию:** `ohangaron` (Охангаронская КС, ГПА-1/2/3)

> ⚠️ **Актуальная архитектура и запуск (host-launch, v23 conformal+epistemic, 2026-07-01) — в [../ARCHITECTURE.md](../ARCHITECTURE.md).**
> Ниже — исходные доки модуля; часть деталей заменена: единый запуск **`python run.py`** (FastAPI :8000 + Vite :5173),
> обучение — единый **`train.py`** (не `train_and_save_models`), статический `build_dashboard`/`anomaly_live.html` **удалён**
> (UI — фронт Vite/React), метод — normalized-conformal + эпистемика + pooled (не CQR).

---

## Состав документации

| Файл | О чём |
|------|-------|
| [00_implementation_plan.md](00_implementation_plan.md) | **Единый план реализации** (всё, что обсуждали: health, журнал, MAE, графики, единый запуск, онлайн-детекция) |
| [01_architecture.md](01_architecture.md) | Архитектура, компоненты, потоки данных, топология запуска |
| [02_architecture_audit.md](02_architecture_audit.md) | **Полный аудит архитектуры**: находки, серьёзность, рекомендации |
| [03_backend_modules.md](03_backend_modules.md) | Справочник модулей backend (назначение, функции, зависимости) |
| [04_api_reference.md](04_api_reference.md) | **Справочник REST API**: эндпоинты, параметры, схемы ответов |
| [05_database.md](05_database.md) | Схема БД (`raw_data`, `anomalies`, планируемые `health`/журнал) |
| [06_anomaly_detection.md](06_anomaly_detection.md) | Обучение моделей и 7 детекторов аномалий, метрики, онлайн-цикл |
| [09_training_methodology.md](09_training_methodology.md) | **Подробный отчёт по методике обучения моделей** |
| [07_frontend.md](07_frontend.md) | Архитектура фронтенда (React/Vite), компоненты, загрузка данных |
| [08_operations.md](08_operations.md) | Эксплуатация: запуск, переменные, логи, переобучение, диагностика |

> Единый план доработок (миграции `health`, журнал уведомлений, MAE вместо R², мульти-графики, единый запуск, онлайн-детекция) — [00_implementation_plan.md](00_implementation_plan.md).

---

## Система за минуту

- **Запуск (host):** `python run.py --station ohangaron` — супервизор поднимает бэк+ML (`run_system.py`: FastAPI :8000 + `live_predict` цикл) и фронт Vite (:5173). Открывать `http://localhost:5173`.
- **Источник данных:** PostgreSQL, схема `ohangaron`, таблица `raw_data(datetime, point, value, health)` — телеметрия SCADA в long-формате (один тег = одна строка на момент времени).
- **Обучение (офлайн, единый файл):** `train.py` — CatBoost RMSEWithUncertainty + virtual ensembles → σ; normalized-conformal коридор (2 режима) + эпистемика; pooled-по-типу. → `models/<station>/*.joblib` + `metadata.json`.
- **Детекция (онлайн, непрерывно):** `live_predict.run_continuous` (в `run_system`) каждые 5 минут прогоняет детекторы, пишет `predictions`/`anomalies_t`(SHAP)/`domain`/`health` в БД + `live_state.json`; на старте догоняет пропуск (`catch_up_missing`).
- **API:** `main.py` (FastAPI, API-only) отдаёт датчики, статистику, события, графики (вкл. эпистемику `series[].e`), тепловую карту, здоровье — из БД + JSON-состояния.
- **UI:** React/Vite (`frontend/`) опрашивает API; статический HTML-дашборд удалён.

```
SCADA → PostgreSQL.raw_data
                 │            python run.py (супервизор)
     ┌───────────┼────────────────────┴───────────────────┐
     ▼                                                       ▼
train.py (офлайн)                    run_system.py: FastAPI :8000 + live_predict (5 мин)
  → models/*.joblib + metadata.json    → predictions/anomalies_t/domain/health (БД) + live_state.json
                                              │
                                     main.py API  ◄──/api proxy──  frontend Vite :5173 (React)
```

---

## Глоссарий

- **ГПА** — газоперекачивающий агрегат (GPA1/2/3).
- **point / тег** — идентификатор сигнала SCADA, напр. `GPA-1.GPA-1.PD.PV`.
- **sensor_id / feature** — нормализованное имя датчика, напр. `gas_pressure_out_gpa__GPA1`.
- **kind / код аномалии** — тип аномалии (ml/neg/frozen/roc/seasonal/regime/cross → 1..7), см. [06](06_anomaly_detection.md).
- **state-файл** — `state/<station>_live_state.json`, снимок результатов последнего цикла детекции для API.
- **last_train_timestamp** — граница «обучение / мониторинг»: аномалии ищутся только после неё (онлайн).
