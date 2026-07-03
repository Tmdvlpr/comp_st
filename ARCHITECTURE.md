# CS_4 — Архитектура системы (host-launch, v23 conformal + epistemic)

Ревизия после пересборки под запуск через локалхост (2026-07-01). Статического
HTML-дашборда больше нет — оператор работает через веб-UI (Vite) + FastAPI.

---

## 1. Запуск — единая точка входа `python run.py`

```
python run.py --station ohangaron         # бэк + фронт + ML
python run.py --no-frontend               # только бэк+ML
python run.py --frontend preview          # собранный dist вместо dev-сервера
```

`run.py` — супервизор (root): поднимает двух детей и держит их вместе (падение любого →
гасит остальных; Ctrl+C → CTRL_BREAK → graceful):

1. **`backend/run_system.py`** — миграции БД → проверка моделей → в потоках:
   **uvicorn FastAPI** (`main:app`, `:8000`) + **`live_predict.run_continuous`** (ML-цикл 5 мин).
   На старте ML-ядро само догоняет пропуск (`catch_up_missing` → `reprocess_history`) —
   самовосстановление после простоя, без дыр в derived-таблицах.
2. **frontend (Vite)** — `npm run dev` (`:5173`), проксирует `/api` → `127.0.0.1:8000`.

> Открывать **`http://localhost:5173`** (vite слушает IPv6 `::1`; `127.0.0.1:5173` может не отвечать).

---

## 2. Модули backend

| Файл | Роль | ~строк |
|---|---|---|
| `run.py` | супервизор фронт+бэк+ML | 171 |
| `run_system.py` | оркестратор: миграции + uvicorn + ML-поток | 153 |
| `main.py` | FastAPI **API-only** (без статики); эндпоинты датчиков/чартов/аномалий/explain | 1975 |
| `live_predict.py` | онлайн-детекция, `reprocess_history`, запись derived + `live_state.json` | 2114 |
| `train.py` | **единый файл методологии** обучения (см. §5) | 1734 |
| `data_loader.py` | PostgreSQL I/O (raw/health/anomalies_t/predictions/domain) | 826 |
| `station_config.py` | конфиг станции, пул соединений БД | — |
| `migrate_db.py` / `ensure_indexes.py` | схема/индексы (идемпотентно) | — |
| `detection_methods.py` | EWMA/CUSUM + инференс-хелперы | — |
| `weather.py` | ambient-температура (внешний признак) | — |
| `anomaly_types.py`, `logging_config.py` | коды аномалий; логи + single-instance-lock | — |

**Утилиты (вне горячего пути):** `backfill_health.py` (health-only, покрыт тестом),
`_validate_corridors.py`, `_shap_top5.py`, `synthetic_harness.py`, `report_v2.py`,
`aging_watchdog.py`, `sync_raw_data.py`. Кандидаты на аудит актуальности: `_wf_*.py`,
`fix_anomalies_table.py`.

**Frontend:** React 19 + Vite 8 + Plotly (`plotly.js-basic-dist-min`), react-query (polling API).
Ключевые компоненты графика: `SensorChart`, **`EpistemicStrip`** (новый, §6), `ChartBrush`,
`MultiSensorChart`, `ComparePanel`, `ContributingFeatures`.

---

## 3. Данные — PostgreSQL (`10.1.30.164`, схема `ohangaron`)

| Таблица | Содержимое | Пишет |
|---|---|---|
| `raw_data` | **источник** телеметрии + колонка `health` | вход (health — live) |
| `predictions` | серия модели: `prediction, lo, hi` + **`e`, `e_thr`** (эпистемика) | live/reprocess |
| `anomalies_t` | эпизоды аномалий + SHAP-атрибуция (`shap_top`) | live/reprocess |
| `"journal notifications"` | журнал уведомлений (ack) | live |
| `domain` | доменные индексы (η_p, polytropic_head, …) | live/reprocess |

`live_state.json` — оперативный снимок «сейчас» (фолбэк для API до заполнения БД).
`raw_data` и `set_of_graphs` (пресеты) при перезаписи derived **не трогаются**.

---

## 4. Поток данных

```
raw_data ──> live_predict (модели + 2 детектора) ──> predictions / anomalies_t /
             domain / raw_data.health / live_state.json
                                   │
                                   ▼
                    main.py FastAPI (:8000)  ──polling──>  frontend Vite (:5173)
```

---

## 5. Методология моделей (`train.py`, инлайн)

- **CatBoost `RMSEWithUncertainty` + `posterior_sampling`(SGLB) + `virtual_ensembles`** →
  на точку `[mean, u_epi (незнание), u_ale (шум данных)]`; **σ = √(u_epi + u_ale)**.
- **Нормализованный block-Mondrian conformal**: нонконформность `s = |факт − предикт| / σ`;
  **два порога** на регим (`q̂_norm` для hybrid, `q̂_abs` для conformal); α=0.02; `n_eff=n/L`
  (block-bootstrap, L из ACF).
- **Детектор-2 (новизна)**: `u_epi > κ` (κ = p95(u_epi_train)·1.5) ∨ marginal-OOD → `regime_mask` (info).
- **Pooled-по-типу**: термо/мех — общая кросс-ГПА модель на z-норме (`_PooledAdapter` де-нормирует
  в сырые единицы ГПА); вибро — per-unit. Маска в `config.methodology.pooling`. Потоково-инференсится.
- **2 коридора в проде** одновременно: `conformal` (дефолт) и `hybrid`; переключение
  `CS_CORRIDOR_MODE` > `cfg.methodology.corridor_mode` > metadata.

---

## 6. Эпистемическая неопределённость в UI (реализовано в этой сессии)

- **Персистенция в БД**: `predictions.e` (u_epi по точкам) + `predictions.e_thr` (порог новизны,
  время-зависим между ретрейнами → по строкам). `save_predictions` пишет 8-кортежи;
  `_fetch_pred_db_series` агрегирует `avg(e)`, `max(e_thr)`.
- **API** `/chart`: `series[].e` + `epistemic_thr` (приоритет БД, фолбэк на `live_state`).
- **Фронт** `EpistemicStrip.tsx` (Plotly): фиолетовая полоса под графиком датчика — заливка `u_epi`,
  пунктирный порог, точки-«новизна» выше порога; ось X зеркалит окно зума основного графика
  (`brushWindow`). Скрывается, если у датчика нет данных `e`.

---

## 7. Убрано как legacy (эта сессия)

- **`build_dashboard()` + `OUTPUT_HTML` + `anomaly_live.html`** — статический HTML-генератор
  (−1837 строк из `live_predict.py`); заменён фронтом Vite/React.
- Убран `build_dashboard` из ML-цикла; ссылки `train_and_save_models.py` → `train.py`.
- Архив: `_pooled_experiment{,2,3}.py`, `_unit_segments.py`, `_windowed_restore.py` → `backend/_archive/`.
- **Резерв для отката**: `backend/_archive/live_predict.pre-dashboard-removal.py`,
  `_legacy_backend_backup_2026-06-30.tar.gz`, `models/ohangaron.backup.v22cqr.2026-06-30/`.

---

## 8. Открытые хвосты / рекомендации

- **Историческая эпистемика в БД**: колонка `e` добавлена, но старые ~256k строк `predictions`
  имеют `e=NULL` (регенерация была до колонки). Добэкфилить одноразово:
  `live_predict.py --station ohangaron --reprocess-from 2026-02-02` (idempotent, ~10–20 мин; при
  стабильной связи с БД). Текущее окно эпистемику уже показывает (свежие точки БД + `live_state`).
- **`main.py /explain`** — при необходимости привести pooled-SHAP к паритету с `_shap_top_for_event`
  (SHAP на нормализованном входе для pooled-моделей).
- **vite/IPv6** — при желании зафиксировать `server.host` в `vite.config.ts`.
- Аудит утилит `_wf_*.py`, `fix_anomalies_table.py`, `report_v2.py`, `aging_watchdog.py` на актуальность.
