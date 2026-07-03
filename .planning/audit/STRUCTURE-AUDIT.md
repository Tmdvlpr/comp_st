# cs_4 — Структурный аудит: дублирование, мёртвый код, «не подключено»

Дата: 2026-07-01. Отдельный разрез от [`FINDINGS.md`](./FINDINGS.md) (тот — про баги/безопасность/UI).
Здесь — только структурная гигиена: что продублировано, что не используется, что не подключено к рабочей системе.

Методология: каждая находка «мёртвый код»/«не подключено» обязана быть обоснована реальным grep по всему дереву
(а не «я не видел ссылки в тех файлах, что читал») — иначе это false positive.

---

## Frontend — мёртвый код / неиспользуемые экспорты (второй эшелон файлов) — агент завершён

**Метод подтверждён:** каждая находка — grep по всему `frontend/src`, не «не видел в тех файлах, что читал».

### Мёртвые экспорты (ноль мест использования)
- `lib/gpa.ts:7` — `RPM_RUNNING_THRESHOLD` — используется только внутри `isRunning` того же файла, наружу не импортируется.
- `lib/sensorLabels.ts:74` — `ruUnit` — сосед `ruSensor` импортируется в 9+ файлах, а `ruUnit` — нигде.
- `lib/motion.ts:6` — `useReducedMotion` (хук) — все потребители используют функцию-соседа `prefersReducedMotion` вместо хука.
- `lib/time.ts:7` — `STATION_TZ` — используется только внутри самого файла (`fmtStation`/`stationYMD`), потребители зовут только эти функции.
- `lib/chartMotion.ts:18` — `CHART_ENTER_MS` — сосед `CHART_ENTER_EASE` импортируется в StatsGrid.tsx, а этот — нет.

### Осиротевший файл целиком
- **`components/Chart/ChartBrush.tsx`** — нигде не импортируется. Доказательство прямо в коде: комментарий в `SensorChart.tsx:857` буквально говорит «Отдельный ChartBrush и EpistemicStrip удалены» (заменены встроенным Plotly-subplot) — файл забыли удалить после рефакторинга. **Безопасно удалить.**

### Возможно неиспользуемые зависимости
- `plotly.js` (bare, не `-basic-dist-min`) и devDependency `@types/plotly.js` — реально используется везде только динамический импорт `plotly.js-basic-dist-min`; типы Plotly объявлены локально как `Record<string,unknown>` (сами не читают `@types/plotly.js`). Confidence: PLAUSIBLE (lockfile-уровень транзитивных зависимостей не проверялся).

### Дублирование в этом эшелоне — НЕ НАЙДЕНО
`lib/motion.ts` (`prefersReducedMotion`), `lib/time.ts` (`fmtStation`/`stationYMD`), `lib/sensorLabels.ts` (`ruSensor`) — каждый единственная реализация, консистентно реиспользуемая 9-11 файлами. **Позитивная находка:** в этом эшелоне нет конкурирующих реализаций одной идеи.

## Cross-stack — устаревшие ссылки в докax / disconnected config — агент завершён

### 🔴 Докстрейл: `train_and_save_models.py` (удалён 2026-06-30) — 7 мест, часть БЕЗ предупреждения

Файл заменён единым `backend/train.py`. Часть докoв имеют баннер-ссылку на ARCHITECTURE.md наверху (частичная защита), но **следующие файлы — БЕЗ баннера вообще**, включая copy-paste команды на несуществующий файл:

- **`docs/00_implementation_plan.md:56,67,229,243,365,373`** — повторно называет `backend/train_and_save_models.py` как файл для правки/запуска в implementation-плане. **Нет баннера.** Читатель, следующий плану сегодня, целится в удалённый файл.
- **`docs/06_anomaly_detection.md:7`** (`## 6.2 Обучение (train_and_save_models.py...)`) и **`docs/09_training_methodology.md:1-3`** (`Источник — backend/train_and_save_models.py`) — **ноль баннеров** (подтверждено grep) — самые тихо-устаревшие докcы в наборе, описывают до-миграционный пайплайн как текущий.
- **`scripts/README.md:56`** — рабочая инструкция `cd backend && ..\venv\Scripts\python.exe train_and_save_models.py --station ohangaron` под «Переобучение моделей» — copy-paste команда на несуществующий файл. Текущая точка входа — `train.py`.
- `TASK_PROMPT.md` (весь документ, напр. строки 16,20,40,165,542-548,576,589) — исходный до-миграционный тех.бриф, без баннера вообще. Большинство пунктов реализовано (multi-station YAML, anomaly_types.py, StationSwitcher — все существуют), но конкретное имя скрипта и CSV-based путь данных (`raw_data_2.csv`/`ETL CS.csv`) — уже нет (только `sync_raw_data.py` трогает `raw_data_2.csv`, но как DB→CSV tail-sync утилиту, не train-инпут).
- `DESIGN.md:48,57,67,137`, `docs/03_backend_modules.md:27-33,80`, `docs/01_architecture.md:16,28,45` — имеют баннер наверху (частичная защита), но конкретные секции/диаграммы/CLI-флаги ниже баннера всё равно называют удалённый файл без пометки.

### 🟠 `docs/05_database.md:24-40` документирует СТАРУЮ схему `anomalies`, не текущую `anomalies_t`
Живая таблица — `anomalies_t` (с `shap_top`, `station_id`, `kind`, 4 индекса, `data_loader.py:289-398`); обе `ensure_anomalies_table()`/`ensure_anomalies_t()` существуют в коде, но докcы и `DESIGN.md §4` документируют только старую. Без баннера.

### 🟡 Конфликт портов — заслуживает отдельной проверки
`CS_API_PORT` в `main.py:1995-1996` дефолтится на **8010**, тогда как ВЕЗДЕ ЕЩЁ (docker-compose, run.py, scripts, ARCHITECTURE.md) используется **:8000**. Не подтверждено как баг (просто env-var дефолт), но настолько специфичное число расходится с остальной системой, что похоже на забытый leftover — стоит проверить отдельно, не тянется ли где-то мёртвый путь через 8010.

### `UZEX_API_AUTH` — определена, никогда не читается
`.env`/`.env.example:13` (комментарий «Uzex.uz integration API (fetch_deals.py)») — `fetch_deals.py` не существует НИГДЕ в кодовой базе. **Спланированная, но никогда не построенная интеграция.**

### docker-compose.yml описывает АРХИТЕКТУРУ, которой больше нет
3-сервисный compose (`predictor`/`backend`/`frontend`, порт 80) — это до-миграционная two-process топология. `ARCHITECTURE.md:1-26` говорит систему запускает единый `python run.py` (host-супервизор → `run_system.py` в одном процессе + Vite `:5173`) — **ни один сервис в compose не запускает `run.py`/`run_system.py`**. compose не обновлён в шаг с архитектурной ревизией от 07-01.

### Прочитано-но-не-определено env vars (в основном безопасные дефолты, не баги)
`CS_DISABLE_DB_WRITE` (ставится самими утилитами как safety-guard — намеренно), `CS_CORRIDOR_MODE`, `CS_WRITE_PREDICTIONS`, `CS_PREDICTIONS_RETENTION_DAYS`, `CS_ENSURE_INDEXES` (задокументирован в docs/08 как env var, но отсутствует в .env файлах), `CS_DASHBOARD_SOURCE`, `CS_API_HOST`/`CS_API_PORT` (см. конфликт портов выше), `VITE_API_URL`.

### Сироты / артефакты — классифицированы, БОЛЬШИНСТВО намеренные (не находки)
- `start_live_monitor.bat` — сирота относительно `scripts/README.md` (не упомянут), почти дублирует `start_live_predict.bat`, но не мёртвый код — вероятно ad-hoc копия под конкретный инцидент.
- `EDA/*.ipynb`, `KS_schema_count.txt` — standalone research-артефакты, ноль ссылок из backend/frontend — **намеренно вне живой системы**, не проблема.
- Архивные бэкапы (`_legacy_backend_backup_*.tar.gz`, `_db_backup_*`, `models/*backup*`) — задокументированы в ARCHITECTURE.md §7/чекпоинте как rollback-артефакты — намеренные.
- **Мета-находка:** ARCHITECTURE.md §8 САМ уже помечает `_wf_*.py`/`fix_anomalies_table.py` как «кандидаты на аудит актуальности» — проект уже знает про эти файлы, вопрос ещё не закрыт (пересекается с находками backend orphaned-scripts sweep ниже).

## Backend — сироты / не подключено — агент завершён

**Метод подтверждён:** прослежена реальная цепочка входа `run.py → run_system.py → {main.py через uvicorn, live_predict.py, train.py через subprocess}`, каждый из ~30 файлов сверен по ней + grep по всему дереву.

### 🎯 ГЛАВНАЯ НАХОДКА — `aging_watchdog.py`: построен, протестирован, НЕ ПОДКЛЮЧЁН
**Confirmed:** grep `aging_watchdog|check_retrain_needed|epistemic_ood_fraction|conditional_shift` по `train.py`, `live_predict.py`, `run_system.py`, `main.py` — **ноль обращений**. Единственная ссылка во всём дереве — собственный тест-файл `tests/test_aging_watchdog.py`.

Это НЕ scratch-скрипт (нет `__main__`/CLI/«Запуск:» — не предназначен для ручного вызова) — это полноценный, покрытый тестами (38/38 pytest) библиотечный модуль, который сам о себе говорит в докстринге: «детектит, когда модель пора ПЕРЕОБУЧИТЬ» (epistemic-OOD, R²-деградация, conditional-shift), но явно «не переобучает сам — возвращает вердикт... **для оркестратора**». **Такого оркестратора не существует ни в одном рабочем пути.** Построенная, протестированная фича повисла в воздухе — либо забыли подключить, либо задизайнили и не довели, либо descoped без удаления. Стоит явно решить: подключить к train.py/run_system.py как периодическую проверку, или удалить как незавершённую работу.

### (A) Активно подключены — все ключевые модули
`run_system.py` (запускается `run.py`) → `main.py` (uvicorn), `live_predict.py`, `train.py` (subprocess) → `migrate_db.py` → `ensure_indexes.py`; `station_config.py`/`data_loader.py`/`anomaly_types.py`/`logging_config.py`/`detection_methods.py`/`weather.py` — все реально импортируются из рабочих модулей.

### (B) Легитимные ручные инструменты — НЕ мёртвый код (все с docstring/CLI/runbook-упоминанием)
`backfill_health.py`, `_validate_corridors.py`, `_shap_top5.py`, `_cutoff_sweep.py` (шеллит `_validate_corridors.py`), `synthetic_harness.py`, `report_v2.py`, `sync_raw_data.py`, `fix_anomalies_table.py`, `_wf_backup_anomalies_t.py`, `_wf_validate_staging.py`, `_wf_verify_api.py` — **каждый** имеет `argparse`/докстринг «Запуск: python X.py» или явное упоминание как шаг runbook в `_CHECKPOINT_2026-06-30.md`. Это ровно те `_wf_*.py`, которые ARCHITECTURE.md §8 сам называет «кандидатами на аудит актуальности» — теперь этот аудит подтверждает: они легитимны как ручные инструменты для конкретного (уже прошедшего) деплой-события, не мусор.

### (C) Классических сирот (scratch-код без всякого назначения) — НЕ НАЙДЕНО
Позитивная находка: в backend нет забытых debug-скриптов без всякой цели.

### Транзитивно мёртвых цепочек — НЕ НАЙДЕНО
Единственный кандидат (`_cutoff_sweep.py`→`_validate_corridors.py`) — оба конца легитимные ручные инструменты.

### Побочное подтверждение
Устаревшие упоминания `train_v2.py`/`regime.py`/`calibrator.py`/`domain_features.py`/`data_quality.py`/`fetch_deals.py` встречаются только в `.omc/research/`-заметках и чекпоинте — сами файлы подтверждённо удалены (glob: 0 совпадений), это архивные заметки о прошлом, не текущие сироты.

## Backend — дублирование во «втором эшелоне» файлов — агент завершён

- **[small]** `_severity_rank` в live_predict.py реимплементирует хардкодом то, что уже есть как данные в `anomaly_types.KIND_SEVERITY`+`max_severity()` — `live_predict.py:1342-1346` vs `anomaly_types.py:25-53`. **main.py уже доказывает, что подстановка работает** — импортирует `max_severity as _max_sev` для той же логики (`main.py:29`). → Заменить тело `_severity_rank` на вызов `max_severity`.
- **[small]** Группировка аномальных эпизодов из boolean-маски (`np.where`+`np.diff>2`+пик-residual) продублирована в `backfill_health.py` тем же блоком, что уже известен в `live_predict.py` — `backfill_health.py:96-104` vs `live_predict.py:1662-1670`. Идентичный gap-threshold и peak-pick логика. → Общий helper в `detection_methods.py`.
- **[trivial]** `robust_std` (σ через 1.4826·MAD, фолбэк `np.std`, floor 1.0) определена БАЙТ-В-БАЙТ идентично в двух файлах, причём `train.py` даже не импортирует модуль, где она уже есть — `detection_methods.py:24-33` vs `train.py:102-111`. → Удалить копию, `import detection_methods as DM`.
- **[trivial]** R²-формула с тем же epsilon-guard (`ss>1e-12`) написана независимо в `aging_watchdog.py` и `train.py` — `aging_watchdog.py:49-56` vs `train.py:916-927`. → `aging_watchdog._r2` зовёт `train._metrics(y,p)["r2"]`.
- **[trivial]** Два скрипта руками собирают `logging.basicConfig` вместо общего `logging_config.setup()` (используемого в run_system/backfill_health/train/live_predict) — `ensure_indexes.py:83`, `migrate_db.py:258`. → Заменить на `_log_setup(...)`.
- **[trivial]** Блок `try: from dotenv import load_dotenv... except ImportError: pass` скопирован в 5 файлах — `backfill_health.py:223-227`, `migrate_db.py:263-267`, `run_system.py:44-48`, `sync_raw_data.py:13-17`, `_validate_corridors.py:31-35`. → Один `load_env()` хелпер.
- **[trivial]** `weather.py` дважды в одном файле повторяет идентичный merge-hourly чейн (`drop_duplicates+sort_values+reset_index`) — `weather.py:78-80,114-116`. Плюс `weather.py._get` — уже **6-я отдельная** реализация retry/backoff в кодовой базе (экспоненциальный, 429-aware, отличается от линейных в data_loader/station_config). → `_merge_hourly()` helper; включить в будущую retry-консолидацию.
- **[trivial]** `--station` CLI-флаг с `default="ohangaron"` переобъявлен почти идентично в 4 скриптах — `backfill_health.py:215`, `run_system.py:35`, `_validate_corridors.py:45`, `migrate_db.py:260`. → `add_station_arg(parser)` helper.
- **[trivial]** Фикс Windows-консоли (`sys.stdout = io.TextIOWrapper(...)`) продублирован verbatim в 2 файлах (+ уже известный train.py) — `sync_raw_data.py:9-11`, `synthetic_harness.py:170`. → `fix_windows_console_encoding()` в logging_config.py.

**Вердикт:** дублирование во втором эшелоне реально, но мельче калибром, чем в ядре (~1-строчные/1-формульные повторы вместо ~100-строчных блоков). Самые содержательные пункты — `_severity_rank` (обходит уже готовую `max_severity()`) и дублированная логика группировки эпизодов (backfill_health vs live_predict) — настоящий дрейф логики между файлами, не просто форматирование.

---

## Итоговая сводка структурного аудита

**Что нашли, отвечая на вопрос «продублировано / лишнее / не подключено»:**

| Категория | Находки |
|---|---|
| 🎯 Построено, но НЕ подключено | `aging_watchdog.py` (весь модуль, 38/38 тестов, ждёт оркестратора) |
| 🗑️ Мёртвый код (frontend) | 1 осиротевший файл (`ChartBrush.tsx`), 5 мёртвых экспортов, возможно 2 неиспользуемые зависимости |
| 🗑️ Мёртвый код (backend) | Классических сирот НЕ найдено — всё либо подключено, либо легитимный ручной инструмент с docstring |
| 📋 Устаревшая документация | 7+ мест ссылаются на `train_and_save_models.py` (удалён 06-30), часть БЕЗ предупреждения; `docs/05_database.md` документирует старую схему `anomalies` вместо `anomalies_t` |
| 🔌 Disconnected config | `UZEX_API_AUTH` определена, никогда не читается (`fetch_deals.py` не существует); `docker-compose.yml` описывает архитектуру, которой больше нет |
| 🟡 Странность, требует проверки | `CS_API_PORT` дефолт 8010 vs везде используемый 8000 |
| 🔁 Дублирование (backend) | ~9 находок во 2-м эшелоне (retry-паттерн #6, R²-формула, severity-ranking, CLI/dotenv/console-encoding boilerplate) — мельче калибром, чем в ядре |
| 🔁 Дублирование (frontend) | НЕ найдено во 2-м эшелоне — reduced-motion/time/labels хелперы консистентно единственные |

**Топ-3 действия, если наводить порядок:**
1. **Решить судьбу `aging_watchdog.py`** — подключить как периодическую проверку в train.py/run_system.py, или явно удалить как незавершённую работу. Сейчас это готовая, протестированная фича, которая просто не используется.
2. **Почистить докстрейл `train_and_save_models.py`** — минимум добавить баннер-предупреждение в `docs/00`, `docs/06`, `docs/09`, `TASK_PROMPT.md`, `scripts/README.md` (у остальных уже есть); в идеале — обновить содержание секций на `train.py`.
3. **Удалить `ChartBrush.tsx`** (доказанно осиротевший, замена уже в коде) и 5 мёртвых экспортов — самая безопасная, самая быстрая чистка из всего списка.

Полные материалы: этот файл + пофайловые секции выше. Ничего не применено к коду — все пункты предложения.
