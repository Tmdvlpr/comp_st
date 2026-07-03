# cs_4 — Критический аудит кода (loop-режим)

Дата: 2026-07-01. Методология: `code-reviewer` + `/security-review` (+ `/simplify`, `/gsd:ui-review`, `/verify` в след. итерациях).
Git отсутствует → diff-скиллы (`/code-review`, `/security-review`, `/simplify`) адаптированы под ревью полного дерева, а не диффа.

Масштаб: backend ~14.6k LOC (33 файла), frontend ~9.4k LOC (38 файлов). God-files: live_predict.py (126KB), main.py (96KB), train.py (94KB), App.tsx (66KB), SensorChart.tsx (59KB).

Легенда серьёзности: **CRITICAL** (блокер) · **HIGH** · **MEDIUM** · **LOW**. Confidence: CONFIRMED (прослежено) / PLAUSIBLE (похоже, не дотрассировано).

Все скиллы код-ревью вызваны: `code-reviewer`-методология (15 независимых ревью-агентов), `/security-review` (отдельный sweep + встроено в ревью каждого файла), `/simplify`-lens (2 агента, backend+frontend, находки-предложения без применения), `/gsd:ui-review` (полноценный 6-факторный аудит, отдельный файл), `/verify` (адаптирован — см. ниже почему).

---

## 📋 EXECUTIVE SUMMARY

**Всего находок: 124** по всей кодовой базе (backend + frontend + verify), из них подтверждено независимо ≥2 методами: 5 тем. Плюс UI-аудит (6.9/10, 10 доп. UI-находок) и 22 simplify-предложения (не баги, отдельная категория).

### Итоговый счёт по серьёзности

| Слой | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| Backend (4 файла + security) | **6** | 15 | 21 | 12 |
| Frontend (5 обзоров) | **3** | 13 | 28 | 17 |
| Verify (eslint, независимо) | 0 | 2\* | 3 | 4 |
| **Итого** | **9** | **30** | **52** | **33** |

\* eslint-ошибки классифицированы по серьёзности эффекта, не по факту «error» в линтере.

### 🔺 Топ-5 блокеров — сделать первым (в этом порядке)

1. **🔴 Ротировать пароль прод-БД `postgres@10.1.30.164` немедленно.** Открытым текстом в `.env`/`.env.example` (последний — НЕ игнорируется, предназначен к коммиту). Топология утекла также в `ARCHITECTURE.md` и EDA-ноутбук. Git отсутствует — единственная причина, почему это ещё не в публичном репо. *(security-sweep, iter.1)*
2. **🔴 Не доверять гарантиям покрытия conformal-коридоров, пока не исправлены `train.py`: (a) calibration-набор = eval/gate-набор (переиспользование), (b) случайный (не хронологический) сплит early-stopping для pooled-модели.** Оба независимо аннулируют статистическую валидность. **⚠️ Уточнение по памяти проекта:** это НЕ та же «R²-катастрофа staging» от 2026-06-25 (та была диагностирована иначе — train включал стоянки vs eval=STEADY — и уже исправлена). Это НОВЫЙ, отдельный методологический вопрос к текущему `train.py` — единому v23-файлу, который согласно памяти проекта [[cs4-v2-conformal-rework]] выкатили в прод буквально 2026-06-30/07-01 (вчера/сегодня) с заявленным «покрытие 0.98, алармы ~0.1%». Если calibration/eval-leakage реален, это не крашит текущую работу коридоров, но означает, что процесс, которым согласовали приёмку 36 ml_corridor и заявленное покрытие, был менее независимым, чем предполагает уверенный тон записи в памяти — стоит перепроверить `_validate_v2_real.py`/аналог на v23 с гарантированно disjoint calibration/eval сплитом, а не доверять текущей цифре как независимой проверке. *(train.py, iter.1)*
3. **🔴 Закрыть три независимые площадки unbounded full-history pull:** `data_loader.fetch_training_data` (стриминг защищает БД, но не клиентскую память), `live_predict.fetch_latest_slice_from_db` (f-string SQL + без верхней границы), `train.py windowed=False` escape-hatch. Все три — конкретная реализация давно известного риска «БД рвёт большой стриминг». *(iter.1)*
4. **🔴 `SensorChart.tsx` не вызывает `removeAllListeners` перед `Plotly.purge` при смене сенсора** — на 24/7-киоске hover/relayout-замыкания копятся с каждым переключением сенсора без ограничения по времени работы. Единственный реальный memory-leak во frontend-аудите с оценкой CRITICAL. *(Chart-компоненты, iter.2)*
5. **🟠 Закрыть API аутентификацией + исправить ack-баги.** Все backend-роуты открыты (включая state-мутирующие `POST .../ack`); во фронтенде «Принять все» квитирует события ВНЕ активного фильтра, а ack ключуется неуникальной тройкой `(sensor_id, timestamp, kind)` вместо `id`. На SCADA-консоли это означает возможность тихо заглушить реальную тревогу ГПА. *(main.py iter.1 + App.tsx/EventDrawer iter.2)*

### 🔁 Сквозные темы — подтверждены ≥2 независимыми методами (наивысшая уверенность отчёта)

Эти находки не результат одного агента с одним взглядом — их поймали разные ревью (LLM чтением кода, статический анализ eslint, визуальный UI-аудит по скриншотам) независимо друг от друга:

- **TZ-раскол (браузер vs `Etc/GMT-5`) — 4 независимых подтверждения.** `App.tsx` (Clock/sidebarLastUpdated/zoom-парсер), cross-cutting-агент (Freshness/PriorityBanner), eslint `react-hooks/purity` (точные строки EventDrawer.tsx:311, KioskMode.tsx:109, ShiftReport.tsx:19), UI-аудитор (+ новое место ContributingFeatures.tsx:182, скриншот-пруф «1327 мин назад»). Плюс backend-зеркало той же болезни: `last_train_timestamp` UTC-vs-local в live_predict.py, `_to_wide` неявный TZ-контракт в data_loader.py (уже вызвавший прод-краш). **Это не баг одного файла — это системный пробел в TZ-дисциплине на обоих концах стека.**
- **Фейковые данные под видом реальных — 3 подтверждения, 2 площадки.** `caseBase.ts` (захардкоженные SHAP-веса + «похожие инциденты») — найден LLM-агентом и подтверждён UI-аудитором визуально; UI-аудитор дополнительно нашёл вторую площадку — `DetailPanel.INDEX_BASE` (`polytropic_eff:0.81` и др.) рендерится как живой замер при отсутствии реальных данных. На safety-щите это «выглядит как доказательство», не будучи им.
- **HeatMap severity только через оттенок (colorblind-риск) — 2 подтверждения.** Cross-cutting-агент (чтение кода) + UI-аудитор (визуально по скриншотам) независимо пришли к одному выводу: теплокарта — первичная поверхность triage — не имеет второго канала, хотя Sidebar в том же приложении эту проблему уже решил (dot-size+ring).
- **Module-global state в `DetailPanel.tsx:587`** (`_persistedTab`) — найден LLM-агентом чтением кода И независимо статическим анализом (новое eslint-правило `react-hooks/globals` с формулировкой «can cause unpredictable behavior»). Редкий случай схождения двух совершенно разных методов на одной строке.
- **Дублированная cutoff-логика окна** (`KioskMode.tsx:109`, `ShiftReport.tsx:19`, `EventDrawer.tsx:311`) — найдена cross-cutting-агентом как паттерн, координаты подтверждены и уточнены eslint (`react-hooks/purity`).

### ✅ Что реально хорошо (не регрессировать)

- **SQL-инъекции закрыты дисциплинированно** — везде `psycopg2.sql.Identifier`/bound-параметры, кроме одной ф-string подсветки (`fetch_latest_slice_from_db`, уже в топ-3 блокерах по другой причине — unbounded query, не инъекция).
- **Frontend typecheck чист** (`tsc --noEmit` — 0 ошибок) и **backend юнит-тесты зелёные** (23/23 — calibrator/regime/aging).
- **Честные состояния «нет данных» vs «бэкенд лёг»** в большинстве мест (Freshness ML/БД-чипы, SensorChart 3-state, ApiErrorBanner) — реже встречается даже в проде.
- **A11y выше среднего для SCADA** (8/10): настоящий focus-trap, live-region на crit-алерты, полная клавиатурная навигация, честный `prefers-reduced-motion`.
- **Chart-инженерия топ-уровня**: синхронизированный epistemic-subplot, dual corridor toggle, rAF-throttled hover, purge/newPlot-lifecycle корректен везде, КРОМЕ одного файла (топ-4 блокер).
- **Backend write-путь дисциплинирован** — chunked commits, идемпотентный `ON CONFLICT`, pool-reset+retry, kill-switch `CS_DISABLE_DB_WRITE` — видна закалка реальными инцидентами.
- Сложность в обоих слоях **в основном сущностная** (conformal prediction/multi-detector на backend; Plotly-domain на frontend) — simplify-агенты не нашли признаков излишней/случайной сложности, требующей серьёзного рефакторинга.

### ⚠️ Методологические оговорки (честность про то, что аудит НЕ покрыл)

- **Git отсутствует** — diff-скиллы (`/code-review`, `/security-review`, `/simplify`) прогнаны по всему дереву, а не по диффу; нет истории коммитов для анализа регрессий.
- **`/verify` не запускал живую систему** — `run.py` подключается к боевой SCADA-БД `10.1.30.164` и авто-гоняет `migrate_db` (есть ветка `DROP TABLE`). Вместо риска для прод-данных выбрана безопасная верификация (typecheck + lint + юнит-тесты чистой математики). **UI и рантайм-поведение не проверены на живом приложении в браузере** — только по существующим скриншотам и статическому чтению кода.
- **`/simplify`-находки не применены к коду** — 24k-строчная прод-система не тот случай, где автономный рефакторинг без явного согласия пользователя оправдан; все 22 пункта — предложения.
- **Юнит-тесты не покрывают методологические баги conformal** (переиспользование calibration/eval и др.) — такие ошибки «валидны по форме», юнит-тесты по природе их не ловят; нужна отдельная статистическая валидация (напр., backtesting покрытия на реально отложенных данных).
- Обнаружено **1.19 ГБ бэкапов моделей** в дереве проекта (`ohangaron.backup.*`, `_archive`, `_db_backup`) — не баг, но операционный долг: похоже на ручное «версионирование» копированием директорий взамен нормального версионирования (см. находку про отсутствие atomic-write в train.py).

### 📁 Полные материалы

- Данный файл — все находки итераций 1, 3 (verify), 4 (simplify) с точными `file:line`.
- [`UI-REVIEW.md`](./UI-REVIEW.md) — полный 6-факторный визуальный аудит с топ-10 UI-фиксами.

---

## Итерация 1 — Backend-ядро + Security

### `backend/main.py` (API, 1998 строк) — агент завершён

**Security posture:** сильная защита от инъекций (везде `psycopg2.sql` + bound-параметры, пулинг соединений с таймаутами, `station_id` валидируется regex + защита от path-traversal, ошибки не текут стектрейсами наружу — отдаётся generic 503). Главная непокрытая дыра — **авторизация**.

- **[HIGH · CONFIRMED]** Все data-эндпоинты полностью без аутентификации — `main.py:444,464,592,642,979,1436,1531,1633,1892`. Любой, кто достучится до порта, читает live SCADA-теги, здоровье установки, историю аномалий; а `POST .../events/ack` и `POST .../notifications/{nid}/ack` позволяют молча заглушить реальные тревоги на ГПА. → Auth-прокси или `Depends(verify_token)` на все роутеры, обязательно на два POST-ack.
- **[HIGH · CONFIRMED]** Ack-эндпоинты меняют состояние БД без auth и с невалидируемым free-text `status` — `main.py:688` (`status: str = Query("ack")`), `1683`. `?status=whatever` по любому `nid` пишет произвольную строку в `journal.status`, ломая логику `status <> 'new'` (`main.py:933`) и позволяя массово заглушить critical-аномалии. → Auth + allow-list `{ack,new,resolved}`.
- **[HIGH · CONFIRMED]** `sensor_explain` синхронно грузит joblib-модель + CatBoost SHAP без rate-limit — `main.py:1331–1430` (load `1385`, SHAP `1402`). Несколько параллельных `GET .../explain` с широким окном исчерпывают 4 uvicorn-воркера и пул из 10 соединений — CPU/DoS-усилитель. → slowapi rate-limit + кэш модели + серверный кап на размер окна.
- **[MEDIUM · CONFIRMED]** `/api/health` без auth сливает полный инвентарь станций и достижимость каждой БД — `main.py:1790–1829`. Отдаёт все `station_id`, `last_updated`, `state_age_seconds`, `model_drift`, статус БД — разведкарта инфраструктуры + сигнал, когда ML завис. → Публично только `{status}`, детали под auth.
- **[MEDIUM · CONFIRMED]** `graph-sets` и `notifications` принимают клиентский `owner` без auth — `main.py:1586,1598,1621` (`owner: str = Query("operator")`). Любой читает/перезаписывает/удаляет чужие сохранённые graph-sets, подставив чужой `owner`. → Брать `owner` из аутентифицированного принципала, не из query.
- **[MEDIUM · PLAUSIBLE]** Неограниченная выборка строк в region-пути `sensor_explain` — здесь живёт известный риск «полная история кладёт БД» — `main.py:1267,1373`. Окно `[t0-1h, t1+1h]` с полностью управляемыми `t0/t1` без валидации спана; сабсэмпл 1500 строк (`1293`) происходит ПОСЛЕ полной выборки, т.е. не ограничивает ни чтение из БД, ни DataFrame. → Жёсткий кап `(t1−t0)` до нескольких дней, иначе 422.
- **[MEDIUM · PLAUSIBLE]** `days` без верхней границы на `/notifications` (`1639`), `/chart` (`983`), `/chart/multi` (`1440`) — только `ge=0`. Большой `days` → полный range-scan `raw_data`. → Добавить `le=` (напр. `le=3650`).
- **[LOW · CONFIRMED]** CORS `allow_origin_regex=r"http://localhost:\d+"` без credentials — сейчас безопасно, но deploy-комментарий (`1995`) приглашает открыть `CS_API_HOST` за прокси; в связке с отсутствием auth даёт ложное чувство защищённости. → Явный env-allow-list + документировать, что CORS ≠ access control.
- **[LOW · CONFIRMED]** `_count_regime_transitions` (`main.py:493`) вкладывает литерал `LIKE '%%.STATES_GTD.5'` в композированный SQL — не инъекция сейчас, но хрупкий паттерн. → Передавать паттерн bound-параметром.
- **[LOW · CONFIRMED · benign]** Глобальные mutable-кэши без локов (`_state_cache/_meta_cache/_db_check_cache`, `main.py:62-63,1766`) — под threadpool возможны дублирующие перечтения, не баг. → ОК как есть или lock/lru_cache+TTL.
- **[LOW · CONFIRMED]** 422 в `_parse_chart_ts` (`950`) и `_explain` (`1358`) эхом возвращают сырой ввод (`{value!r}`) — низкий риск, но user-controlled в теле. → Убрать/ограничить эхо.

**Вердикт main.py:** инъекции закрыты хорошо, соединения дисциплинированы. Ship-блокер до любого не-loopback деплоя: (1) аутентификация на все роуты, (2) allow-list `status` + `owner` из принципала, (3) кап окна в `sensor_explain`.

---

### `backend/train.py` (пайплайн обучения + калибровка conformal) — агент завершён

**⚠️ Два CRITICAL нарушения валидности conformal — вероятно связаны с R²-катастрофой staging (см. проектную память).**

- **[CRITICAL · CONFIRMED]** Калибровочный набор conformal НЕ отделён от eval/gate-набора — те же post-cutoff точки используются и для R²-гейта, и для калибровки — `train.py:984-985, 1022-1043, 1064`. Холдаут `(unit_cutoff, ∞)` служит одновременно (a) гейтом «beats baseline» → `detector_mode`, (b) окнами метрик, (c) пулом для `select_calibration`. Split-conformal требует независимости калибровки от любого решения, принятого на этих же данных → завышенная «валидность» покрытия. → 3-way split: train / gate-eval / calibration (disjoint).
- **[CRITICAL · CONFIRMED]** ES-валидация pooled-модели — случайный shuffle по временному ряду, ломает exchangeability — `train.py:1350-1357` (`rng.random(len(pool)) < es_val_frac` по хронологически упорядоченному `pool`). Соседние по времени точки уходят в train и val → утечка near-future в early-stopping → переобучение, которое хорошо смотрится на es_val и хуже — на реальном будущем. (Non-pooled путь делает хронологический сплит — правильно.) → Хронологический сплит per-GPA перед пулингом.
- **[HIGH · CONFIRMED]** `column_verdict` использует post-cutoff `std_holdout` для решения KEEP/EXCLUDE фич — `train.py:165-201` (`sd_ho` в логике вердикта 191/193), вызов `1613`. Отбор фич информируется будущими данными → мягкая утечка в feature selection. → `std_holdout` только как диагностика, не влияющая на keep.
- **[HIGH · PLAUSIBLE]** `ffill(limit=2)` на полном ряде до сплита и до `physically_clean`/steady-mask — `train.py:1607`. ffill каузален, но выполняется до партиции, патчит разрывы через границу cutoff → лёгкий оптимистичный сдвиг метрик холдаута + рассинхрон с тем, как live видит поток. → Проверить, что live применяет тот же ffill; иначе ffill после сплита.
- **[HIGH · PLAUSIBLE]** Per-unit cutoff (`resolve_unit_cutoff`, `train.py:572-603`) режет до половины healthy-истории под калибровку, `unit_cutoff` уезжает назад по-сенсорно → разные сенсоры одного ГПА обучены на разных окнах, `last_train_ts` несравним по банку моделей. Флаг `pre_fault_check_required=True` (`603`) не имеет потребителя в файле (dead state?). → Проверить потребление флага; выровнять окна.
- **[MEDIUM · CONFIRMED]** Broad `except → logger.debug` глушит падения virtual-ensemble/эпистемики → `u_epi=0`, novelty-детектор молча выключен, а `detector_mode` всё ещё `ml_corridor` — `train.py:879-880, 1051-1052, 1229-1230`. При systemic version-mismatch novelty молча гаснет по всему флоту (только DEBUG). → `logger.warning` + флаг `epistemic_available:false` в метаданные.
- **[MEDIUM · CONFIRMED]** Полный full-history путь `windowed=False` жив и достижим импортом — `train.py:1564-1567` (default `True`, но `train_all(windowed=False)` → `fetch_training_data(cutoff_date=None)` = unbounded fetch, тот самый краш БД). → Удалить ветку или громкий opt-in с лимитами.
- **[MEDIUM · CONFIRMED]** Два corridor-метода сосуществуют; прод-дефолт захардкожен `"conformal"` (плоский), хотя весь дизайн (docstring, `CalibrationArtifact.mode="normalized"`) построен под σ-адаптивный hybrid — `train.py:1474-1478` vs `769,1059-1062`. Без override станция молча получает наивный fixed-width вместо hybrid. → Дефолт `"hybrid"` или громкий лог активного режима per-station.
- **[MEDIUM · CONFIRMED]** Merge-режим `--gpa` читает `metadata.json` с `except Exception: models_meta = {}` (`train.py:1596`) — при любой ошибке чтения молча стирает записи ВСЕХ остальных ГПА из metadata на следующей записи. → Сузить except до `(FileNotFoundError, json.JSONDecodeError)`, иначе raise.
- **[MEDIUM · PLAUSIBLE]** Модели и `metadata.json` пишутся без atomic-write/бэкапа/версий — `train.py:1625-1628, 1682-1684`. Краш в середине цикла по таргетам → на диске новые `.joblib`, а `metadata.json` старый (feat_cols не совпадут) → live молча выдаёт мусор вместо ошибки. → temp+rename после полного успеха, либо версии по cutoff-ts.
- **[LOW · CONFIRMED]** Нет провенанса версий (`catboost.__version__`, numpy/pandas) в `build_metadata` — «тот же seed» не гарантирует те же модели после апгрейда либы. → Записывать версии в метаданные.
- **[LOW · CONFIRMED]** Reproducibility в целом дисциплинирована (`random_seed=42` везде), mutable-defaults чисто (`field(default_factory=...)`). Мелочь: одинаковый `rng_seed=42` во всех бутстрэп-калибровках; повторный пересчёт `label_regime`/`steady_running_mask` per-sensor (CPU-расход).

**Вердикт train.py:** методологически продвинуто (block-Mondrian conformal, virtual-ensemble uncertainty, честный хронологический сплит в non-pooled), НО две подтверждённые нарушения валидности conformal (переиспользование gate/eval как калибровки + случайный ES-сплит pooled) независимо аннулируют заявленные гарантии покрытия. Плюс утечка post-cutoff в отбор фич и дефолт-режим, противоречащий дизайну. **Не доверять гарантиям покрытия, пока не исправлены разделение calibration/eval и хронология pooled-ES.** Точечные предсказания (CatBoost, сиды, non-pooled сплит) — крепкие.

---

### `backend/live_predict.py` (движок live-инференса, ~2100 строк) — агент завершён

- **[HIGH · CONFIRMED]** `last_train_timestamp` трактуется как UTC в сравнении cutoff, хотя всюду в файле это naive Etc/GMT-5 — `live_predict.py:1012-1015`. `train.py:1479` пишет его как naive-local; строка 1015 делает `pd.Timestamp(last_train_ts, tz='UTC')` → перекос 5ч при выборе старта окна → первый live-load либо перечитывает обученное, либо оставляет 5-часовую дыру после cutoff. → Парсить через `_parse_train_ts` + `_local_naive_to_utc`.
- **[HIGH · CONFIRMED]** `fetch_latest_slice_from_db` строит SQL f-string'ом по идентификаторам (обход `sql.Identifier`, применяемого в `fetch_data_from_db`) И без верхней границы/лимита — `live_predict.py:232-239`. (1) битый/вредоносный station-config ломает/инъектит здесь; (2) `WHERE dt > %s ORDER BY dt` без upper-bound → при зависании live-цикла тянет unbounded slice = тот самый краш БД. → Переписать через `sql.Identifier` + `AND dt <= now()` + оконный лоадер.
- **[HIGH · CONFIRMED]** `fetch_latest_slice_from_db` дёргает `_station_cfg` без `_require_station()` — `live_predict.py:223-230`. До `_init_station` → `AttributeError` на `None`, проглатывается bare `except` (`241`) → пустой DataFrame = «нет данных» вместо реальной ошибки конфигурации. → `_require_station()` в начале функции.
- **[MEDIUM · CONFIRMED]** `z_score` в `anomalies_t` считается формулой `resid·n_sigma/hw`, валидной только для гауссова `hw=n_sigma·scale` — `live_predict.py:1689-1691`. В `_v2_corr` `hw` = conformal `q̂·σ` или floor → «z-score» смешивает conformal-знаменатель с sigma-множителем чужого коридора → оператор видит вводящую в заблуждение величину. → Считать z из реального масштаба residual или `None` при conformal.
- **[MEDIUM · CONFIRMED]** Broad `except: return pd.DataFrame()` в обоих fetch-путях (`live_predict.py:218-220, 241-243`) маскирует connection/auth/timeout/SQL как «нет данных» → `run_continuous` не считает это фейлом, backoff не срабатывает, реальная мисконфигурация выглядит как idle БД. → Сузить except, пробрасывать неожиданное.
- **[MEDIUM · CONFIRMED]** v2-гейт молча полностью выключает ML-детекцию при частично мигрированной модели — `live_predict.py:530-532, 614-617`. `schema_version='v2'` + `ml_corridor` но без `calibration.by_regime` → `ml_mask = zeros`, сенсор выглядит здоровым, реальные out-of-band события не детектятся (только `logger.warning`). → Отличать «univariate by design» от «калибровка не загрузилась»; фолбэк на nominal-коридор + флаг деградации.
- **[MEDIUM · PLAUSIBLE]** Conformal `hw`-floor stack молча перекрывает калиброванный коридор (`min_buffer`/`min_abs_error`) — `live_predict.py:514-523, 589-591`. FP-калибровка (`484-489`) считается против pre-`_active` `y_std`, но детекция (`oob = resid > hw`, `609`) идёт по итоговому `hw` → коридор скачет между регионами, диагностика покрытия (`1317`) читается неконсистентно. → `hw=_active` где калибровано, floor только где NaN; мерить покрытие по итоговому `hw`.
- **[MEDIUM · PLAUSIBLE]** In-place мутация shared mask-массивов при cross-GPA/regime suppression + смешение numpy-array и pd.Series по выравниванию — `live_predict.py:980-983, 968`. Позиционное `arr[mask]=False` vs label-align Series `|=` фрагильно, зависит от совпадения `times` с индексом Series. → Нормализовать все маски к numpy (или все к Series с общим индексом).
- **[MEDIUM · PLAUSIBLE]** Per-window fetch пересоздаёт pooled-соединение на каждый чанк; `reset_pool` при `OperationalError` инвалидирует пул для ВСЕХ (в т.ч. FastAPI-бэкенда) → retry-storm между процессами при флапе БД — `live_predict.py:196-201, 204-205`. → Одно соединение на весь sweep; scope reset_pool узко.
- **[MEDIUM · PLAUSIBLE]** `n_sigma`-калибровка (98.5-перцентиль `resid/y_std`) бессмысленна, когда `y_std=base_scale` (не-CatBoost, `y_var is None`) — `live_predict.py:465-489`. Тогда `n_sigma` пинится в потолок клипа [3,7], заявленный «FP≈1.5%» ложен. → Калибровать n_sigma только при реальном `y_var`.
- **[LOW · CONFIRMED]** `time.sleep(backoff)` при фейле не проверяет `_shutdown_requested` — `live_predict.py:2094-2097` → SIGTERM во время backoff зависает до 5 мин. → Поллить shutdown в backoff как в обычном wait.
- **[LOW · PLAUSIBLE]** `seasonal_mask` по `df_t.index.hour` предполагает naive Etc/GMT-5 без ассерта — `live_predict.py:684,689-690`; любой UTC-leak сдвигает диурнальные бакеты на 5ч молча. → `assert df_wide.index.tz is None` на входе.
- **[LOW · PLAUSIBLE]** Regime-shift `window.idxmax()` при all-NaN окне → NaT → исключение в per-GPA блоке, не обёрнутом try/except → одна дыра в данных обрывает regime-detection для всех оставшихся ГПА — `live_predict.py:920-926,944-947`. → `dropna()` перед idxmax + обёртка per-GPA.
- **[LOW]** `working_counts` fallback-индекс из `df['datetime'].unique()` (`266-268`) — reindex спасает, но ветка мёртвый груз; дубликат `resid=np.abs(y-y_mean)` (`480` и `597`); `predict_sensor` — ~250 строк, 7 детекторов в одной функции (тяжело тестировать).

**Чисто в live_predict:** `sql.Identifier` в `fetch_data_from_db`, нет eval/subprocess/unsafe-pickle, atomic state-write через tmp+`os.replace` (`1855-1858`), кооперативный shutdown, оконный catch-up/reprocess корректно избегает full-history краша.

**Вердикт live_predict.py:** рабочий, но переросший движок со шрамами миграции v1→v2 (двойные коридоры, слои `hw`-floor, взаимодействующие `_v2`/`_v2_corr`-гейты). Риск концентрируется в TZ (1 подтверждённый UTC-vs-GMT-5 на cutoff + незащищённые диурнальные предположения) и в silent degradation (broad excepts → «нет данных»; молчаливое отключение ML на частично мигрированных моделях). Не горит, но `predict_sensor` и `fetch_latest_slice_from_db` требуют рефакторинга/хардненинга до следующей смены методологии; z-score/n_sigma-отчётность больше не соответствует математике коридора.

---

### 🔴 Security-sweep (config / secrets / infra) — агент завершён

**Известная утечка секретов — ПОДТВЕРЖДЕНА и хуже, чем в памяти:**

- **[CRITICAL · CONFIRMED]** Реальный пароль прод-суперюзера БД открытым текстом в `.env` — `cs_4/.env:5` и `cs_4/backend/.env:1-5`: `CS_DB_HOST=10.1.30.164`, `CS_DB_NAME=CS`, `CS_DB_USER=postgres`, `CS_DB_PASSWORD=<5-символьный суперюзер-пароль>`. Это боевой креденшл SCADA-Postgres. `.gitignore` покрыл бы `.env`, **но git-репозитория нет** → защиты ноль; любой `git init && git add .` до фикса заберёт суперюзер-доступ. → **Немедленно ротировать пароль `postgres`**, вынести секрет из дерева проекта (vault/out-of-tree .env).
- **[CRITICAL · CONFIRMED]** Реальный прод-хост+имя БД (и в одном месте пароль) в **не-игнорируемом** `.env.example` — `cs_4/.env.example:6,8` (`10.1.30.164`, `CS`). `.gitignore` игнорит `.env*`, но НЕ `.env.example` (он предназначен к коммиту) → топология внутренней SCADA-сети утекает в любой repo/share. → Заменить на плейсхолдеры (как уже сделано в `backend/.env.example`).
- **[HIGH · CONFIRMED]** UZEX API-токен (`Basic ...`) слот в трекаемом `.env.example:13` / `.env:7` — сейчас плейсхолдер, но паттерн приглашает вписать реальный reversible Basic-auth в незащищённый файл. → Держать плейсхолдер, реальный — в vault, только по TLS.
- **[HIGH · CONFIRMED]** Прод-хост + полные стектрейсы пишутся в `backend/logs/api.log:25` (×~50) без редакции; логи не в `.gitignore`. Пароль psycopg2 маскирует — это раскрытие топологии, не креда. → `backend/logs/` в `.gitignore` + скраб host/DSN из сообщений.
- **[HIGH · CONFIRMED]** Прод-хост/имя БД в коммитируемых доках и выводах ноутбука — `ARCHITECTURE.md:56`, `EDA/ohangaron_eda.ipynb:107,114,122` (`БД: 10.1.30.164:5432/CS`). → Плейсхолдеры в доке + `nbstripout` перед коммитом.
- **[MEDIUM · CONFIRMED]** `docker-compose.yml:5,13,14` грузит реальный `.env` (прод-суперюзер) в контейнеры и биндит API на `0.0.0.0` (`:14-16`, порты 8000/80). В связке с «API без auth» (`docs/08_operations.md:90`) — открытый неаутентифицированный API снаружи. (Порт самой БД compose НЕ экспонирует.) → Бинд на `127.0.0.1`/за reverse-proxy с auth + least-privilege DB-роль вместо `postgres`.
- **[MEDIUM · CONFIRMED]** `migrate_db.py:185` (`DROP TABLE`) авто-запускается на каждом старте через `run_system.py:64-65` без подтверждения. DROP гейтед (malformed + `count==0`), но пустой мис-схемный прод-журнал молча дропнется; rename health-колонки (`:114-116`) тоже мутирует прод-схему без присмотра. → Гейт `CS_ALLOW_MIGRATE=1` на все DDL-шаги.
- **[LOW · CONFIRMED]** `joblib.load` моделей = RCE при подмене файла — `main.py:1273,1385`, `live_predict.py:118`. Если атакующий пишет в `models/` (deploy staging→swap), отравленный `.joblib` исполняет код в процессе с прод-кредами. Local-trust сегодня → LOW. → Ограничить запись в `models/` + checksum/подпись перед load.

**Чисто:** `weather.py` (HTTPS + timeout, без verify=False, без ключа), `sync_raw_data.py` (env-креды, SQL только с хардкод-константами через `%s`), `station_config.py` (`sql.Identifier`, `yaml.safe_load`, regex + path-traversal guard), `ensure_indexes.py`/`migrate_db.py` (все идентификаторы через `pgsql.Identifier`). Нет `shell=True`/`os.system`/`eval`/`exec`/`pickle.load`; единственный `subprocess` (`run_system.py:75`) — argv-список без shell.

---

### `backend/data_loader.py` (слой доступа к БД, ~1100 строк) — агент завершён

- **[CRITICAL · CONFIRMED]** `fetch_training_data` без жёсткого капа — unbounded full-history pull всё ещё в одном вызове, и стриминг НЕ спасает клиентскую память — `data_loader.py:141-169`. `_fetch_df_streaming` использует серверный курсор, но `rows.extend(batch)` (`:60-67`) копит ВЕСЬ результат в один Python-список до построения DataFrame → тот самый OOM/краш. Вызывается напрямую из `train.py:1567` при `windowed=False`. → Обязательный `from_date` (или reject) + инкрементальная сборка DataFrame.
- **[CRITICAL · CONFIRMED]** TZ-обработка в `_to_wide` неявная и уже вызвала прод-краш — `data_loader.py:203-221` (`:211`). `pd.to_datetime` пропускает то, что вернул psycopg2; для `TIMESTAMPTZ` индекс несёт анонимный fixed-offset `UTC+05:00` вместо канонического `Etc/GMT-5`. Пруф: `backend/logs/train.out.log:378` — `TypeError: Invalid comparison between dtype=datetime64[us, UTC+05:00] and Timestamp` из `column_verdict` (2026-06-19). Нет единого контракта вывода → каждый потребитель дефензивно пере-детектит tz. → Нормализовать выход `_to_wide` (один документированный контракт: либо `tz_convert("Etc/GMT-5")`+drop, либо явный UTC).
- **[HIGH · CONFIRMED]** `_to_wide` молча выбрасывает строки с непарсимым timestamp — `data_loader.py:211-212` (`errors="coerce"` → NaT → `dropna` без лога). Сбой фида, портящий timestamps батча, исчезает как «обычный gap». → Логировать `isna().sum()` перед dropna + warn.
- **[HIGH · PLAUSIBLE]** `ffill(limit=2)` в `_to_wide` форвард-филлит для ВСЕХ вызывающих, включая обучение — `data_loader.py:217-219`. Короткие простои сенсора невидимо патчатся stale-значениями в train-сете; модель не отличает «реально плоско» от «сенсор лежал». (Перекликается с ffill-находкой в train.py.) → Companion-маска `was_filled` или opt-in ffill per-caller.
- **[HIGH · CONFIRMED]** `save_notifications` (в отличие от всех sibling `save_*`) не вызывает `ensure_*` DDL — `data_loader.py:788-833`. При наведении на свежую станцию/схему до `migrate_db.py` каждый insert падает, и `_write_batches` ловит только `OperationalError/InterfaceError` → пробрасывается неперехваченным. (Противоречит цели «работает с любой станцией».) → Добавить `ensure_journal_notifications()`.
- **[MEDIUM · PLAUSIBLE]** `event_ts` fallback на `timestamp` смешивает UTC-aware и «локальный naive» в один `TIMESTAMPTZ` без проверки — `data_loader.py:336,435,823`. Naive fallback интерпретируется session-TЗ Postgres → молчаливый сдвиг. → Проверять `tzinfo`, naive → localize `Etc/GMT-5` → UTC явно.
- **[MEDIUM · PLAUSIBLE]** `fetch_live_data` окно считает сервер (`NOW() - interval`), session-TЗ пула нигде не пиннится → «последние N часов» сдвигаются на оффсет сервера — `data_loader.py:187-201`. → `SET TIME ZONE 'UTC'` в options пула (рядом со `statement_timeout`).
- **[MEDIUM · CONFIRMED]** Три silent broad `except`: `_fetch_df_streaming` reset_pool `pass` (`:75-76`), `get_last_prediction_ts`→`{}` (`:626-628`, риск дублей записи), `prune_predictions`→`0` (`:643-645`). → Сузить до конкретных исключений, re-raise прочее.
- **[MEDIUM · PLAUSIBLE]** Дублированная retry/chunk-логика с разошедшимся поведением: `_write_batches` (`:84-137`) ловит `QueryCanceled/LockNotAvailable`, а `save_domain`/`save_predictions` (`:497-527, 568-602`) — нет → lock-timeout там пробрасывается вместо graceful-skip. → Свести на общий helper.
- **[MEDIUM · PLAUSIBLE]** `build_tag_mapping` логирует только СЧЁТ нераспознанных тегов на DEBUG (`:247-248`), не сами строки → новый формат тега молча дропается. → Логировать сэмпл строк на WARNING.
- **[LOW]** `fetch_live_data` использует не-стриминговый `_fetch_df` без retry (`:187-201,31-38`) при заявленной «нестабильной сети до БД»; `ensure_set_of_graphs()` дёргается на КАЖДЫЙ list/save/delete graph-set (`:672,684-700`) — лишний round-trip; edge-case пустого стриминг-курсора (`:59-68`); неэнфорснутое 5-мин выравнивание бакетов health (`:713-784`).

**Вердикт data_loader.py:** write-путь реально крепкий (chunked commits, идемпотентный `ON CONFLICT`, pool-reset+retry, lock/timeout-aware skip, kill-switch `CS_DISABLE_DB_WRITE`) — видна закалка инцидентами. Слабое место — read/training: `_to_wide` tz неявная (уже дала прод-краш), `fetch_training_data` без `from_date` — живой full-history риск. **Фиксить #1 и #2 до любого нового train-запуска.**

---

## Итерация 1 — сводка

| Файл | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|
| security-sweep | 2 | 3 | 2 | 1 |
| data_loader.py | 2 | 3 | 4 | 1 |
| train.py | 2 | 3 | 5 | 2 |
| live_predict.py | 0 | 3 | 6 | 4 |
| main.py | 0 | 3 | 4 | 4 |

**Топ-блокеры backend (сделать первым):**
1. 🔴 **Ротировать прод-пароль БД** + вычистить `10.1.30.164`/`CS`/пароль из `.env`, `.env.example`, `ARCHITECTURE.md`, EDA-ноутбука, логов.
2. 🔴 **Валидность conformal** (train.py): разделить calibration/eval; хронологический pooled-ES сплит. Вероятная причина R²-катастрофы staging.
3. 🔴 **Full-history pull** (data_loader `fetch_training_data` + live_predict `fetch_latest_slice`): жёсткий кап/обязательный `from_date`.
4. 🔴 **TZ-контракт `_to_wide`** — источник подтверждённого прод-краша; один документированный формат.
5. 🟠 **Аутентификация API** — все роуты открыты, ack-эндпоинты мутируют БД (заглушение реальных тревог ГПА).

---

## Итерация 2 — Frontend

### `frontend/src/App.tsx` (монолит-рут, 1265 строк, 66KB) — агент завершён

**Сквозная тема: TZ-конвенция протекает в горячих местах (браузерная TZ вместо станционной Etc/GMT-5).**

- **[HIGH · CONFIRMED]** Часы в шапке (`Clock`) рендерят время в TZ **браузера**, не станции — `App.tsx:1225-1230` (`toLocaleTimeString('ru-RU')` без `timeZone`). На RDP/удалённой машине не UTC+5 «текущее время станции» неверно, при том что события рядом корректны через `fmtStation`. → `timeZone: STATION_TZ`.
- **[HIGH · CONFIRMED]** `handleRangeChange` парсит timestamp из Plotly наивным `new Date(...)` → 5ч-сдвиг молча портит окно зума — `App.tsx:763`. Оператор в другой TZ тянет box зума → detail-слой грузит не те часы. → Аппендить `+05:00` перед парсом, собирать ISO с тем же оффсетом.
- **[HIGH · CONFIRMED]** `sidebarLastUpdated` парсит `stats.last_updated` наивно + `toLocaleTimeString` без TZ — `App.tsx:793-795`. «Последнее обновление» в сайдбаре в браузерной зоне, рассинхрон с `Freshness`. → Через `fmtStation`.
- **[HIGH · PLAUSIBLE]** Неограниченный fan-out overlay/dropped-запросов с рефетчем каждые 30с — рост памяти/сети на 24/7 киоске — `App.tsx:509,540,684,694`. `overlayTargets` без капа (в отличие от `droppedIds.slice(-4)`); широкая станция множит live-поллеры без границы + `gcTime:30min`. → Кап `overlayTargets.slice(0,6)` + реже рефетч/меньше gcTime для оверлеев.
- **[MEDIUM · CONFIRMED]** `handleAck` шлёт неожидаемый API-вызов и глушит все ошибки; оптимистичный локальный ack навсегда расходится с сервером — `App.tsx:742-750` (`api.ackEvent(...).catch(()=>{})` + `localStorage`). Оператор «принял» critical при офлайн-БД → на сервере не записано, но UI вечно показывает «✓ Принято». **Опасно для safety-дашборда.** → Показывать «ack не сохранён» и/или откат.
- **[MEDIUM · PLAUSIBLE]** `rangeHistorical`/`zoomHistorical` «кэш навсегда» зависит от `Date.now()` в рендере и по-разному трактует naive `t1` → возможен двойной оффсет — `App.tsx:423-424,438-439`. → Гарантировать известный оффсет из `handleRangeChange`, применять одинаково.
- **[MEDIUM · PLAUSIBLE]** `availableDataDays` парсит концы серии наивным `new Date(s[i].t)` → у fixed-zone vs DST-браузера span может гулять на час, флипая гейт preset-кнопок — `App.tsx:471`. → Явный оффсет/общий хелпер.
- **[MEDIUM · PLAUSIBLE]** `focusEvent`-анимация умышленно выкидывает `focusEvent` из deps + lint-suppress → stale-closure — `App.tsx:733-740`. → Ключевать по ref/DOM или включить читаемые значения.
- **[MEDIUM · PLAUSIBLE]** Non-null `!` на `selectedId/zoomWindow/regionSel/featEventTs` в queryFn держатся только на синхроне `enabled` — `App.tsx:429,444,686,696`. Кратковременный десинк → `api.sensorChart(null,...)` → TypeError. → Гард внутри queryFn.
- **[MEDIUM · PLAUSIBLE]** Небезопасные `as`: `s.anomaly_types as string[]` (`App.tsx:476`) → `.includes` кидает при null/omit → падает monitor при активном kind-фильтре; `(e as CustomEvent).detail as string` (`370`) без runtime-проверки. → Типизировать в источнике + `?? []`.
- **[MEDIUM · PLAUSIBLE]** `overlaySig`/`stableOverlayRef` мутируют ref во время рендера (ручной memo) — `App.tsx:564-573`; под StrictMode/concurrent → stale/torn значение → лишний `Plotly.react`. → `useMemo` по сигнатуре.
- **[MEDIUM · PLAUSIBLE]** Анимации ищут DOM через `document.querySelector('.js-*')` (React против DOM), cleanup только `a.pause()` без сброса opacity/transform → элемент может застрять `opacity:0` — `App.tsx:706,717,726,735`. → `useRef` на JSX + `a.revert()` в cleanup.
- **[MEDIUM · CONFIRMED]** Инлайн-хендлеры и большие style-объекты пересоздаются каждый рендер на drop-zone и preset-кнопках, оборачивающих тяжёлый `<SensorChart>` — `App.tsx:1004-1011,1049-1058`; каждый 30с-тик = новые prop-identity вниз к Plotly. → `useCallback` + предвычислить стили.
- **[LOW · PLAUSIBLE]** `initialStation` игнорится после маунта (lazy initial state, `App.tsx:294-296`); `ackedIds` растёт без границы в state+localStorage (`290-293,322-324,751-752`); `chartDays` из URL без клампа к `{1,3,7,14,30}` (`276-279,302`) → `?days=99999` может задеть backend-фрагильность стриминга.

**Вердикт App.tsx:** видно, что файл уже раз проходил аудит (module-level стили, вынесенный `Clock`, стабилизация overlay-сигнатуры), но TZ-конвенция протекает в 3 самых видимых местах — главный correctness-риск. 1265 строк владеют слишком многим (URL-sync, cross-tab ack, 8+ запросов, drag-drop, анимации, весь layout). **Нужна декомпозиция** (хуки `useDashboardQueries`/`useChartRange`/`useAck` + вынос тулбара/drop-zone), это попутно устранит часть render-hot и stale-closure находок.

### Chart-компоненты (SensorChart, MultiSensorChart, ComparePanel, ContributingFeatures, useBklitHover, chartMotion) — агент завершён

- **[CRITICAL · CONFIRMED]** `SensorChart.tsx` не вызывает `removeAllListeners` перед `Plotly.purge` при смене сенсора → `plotly_hover/unhover/relayout`-замыкания копятся при каждом переключении — `SensorChart.tsx:434-612` (регистрация), `638-654` (cleanup). `initializedRef` сбрасывается в `false`, следующий `newPlot` вешает новые `.on()` на тот же DOM-узел без снятия старых. На 24/7 киоске оператор кликает десятки сенсоров/смену → монотонный рост памяти + stale-замыкания двоят DOM-запись (мерцание тултипа/крестика). MultiSensorChart (`:247`) и ContributingFeatures делают это правильно — SensorChart нет. → Добавить `el.removeAllListeners?.('plotly_hover'/'unhover'/'relayout')` перед `purge` на `SensorChart.tsx:652`.
- **[HIGH · CONFIRMED]** `dblclick`-listener добавлен через `dblBoundRef` и никогда не снимается — `SensorChart.tsx:449-454`. Не множится per-switch (ref-guard), но `purge` не снимает нативные не-plotly listener'ы → узел висит в памяти. → Снимать в true-unmount cleanup или через React `onDoubleClick`.
- **[MEDIUM · CONFIRMED]** `ContributingFeatures` OverlayContribChart тоже без `removeAllListeners` перед purge — `ContributingFeatures.tsx:121-146`. Модалка (bounded), но при частом open/close на киоске копит listener'ы. → Тот же фикс на `:145`.
- **[MEDIUM · PLAUSIBLE]** `MultiSensorChart` main-effect на `[traces, layout, seriesSig]`; `keepPreviousData` возвращает новый массив-референс при рефетче даже при идентичных данных → полный `Plotly.react` на байт-идентичных данных — `MultiSensorChart.tsx:61-238`, `ComparePanel.tsx:52-59` (`staleTime:20s` смягчает). → Deep-compare/хэш перед «изменилось».
- **[MEDIUM · PLAUSIBLE]** Мутация ref во время рендера (crosshair color) в SensorChart (`:146-147`) и MultiSensorChart (`:58-59`) — идемпотентно, но анти-паттерн (render must be pure). → В `useLayoutEffect` по `theme` или задокументировать.
- **[MEDIUM · PLAUSIBLE]** `epiRef`-lookup завязан на формат ключа `String(x).replace(' ','T')` — `SensorChart.tsx:341-348,589`; если бэкенд сменит разделитель, epistemic-тултип молча пропадёт без ошибки. → Нормализовать формат timestamp один раз на границе API.
- **[LOW · CONFIRMED]** Повсеместные `el as any` для `.on()`/`removeAllListeners` во всех 4 Plotly-файлах — типо в имени события компилируется, listener молча не вешается. → Общий `interface PlotlyGraphDiv`.
- **[LOW · CONFIRMED]** `PlotlyData/PlotlyLayout = Record<string,unknown>` → опечатка в свойстве Plotly (`colour` vs `color`) проходит type-check, коридор/аномалии молча мис-стайлятся (визуально load-bearing для доверия оператора). → `@types/plotly.js`.
- **[LOW]** Прочее: сортировка кластеров аномалий без капа (`SensorChart.tsx:284-306`), 400 неврт. чекбоксов в ComparePanel (`:288-305`), chartMotion — one-shot, не гейтит `document.hidden` (impact негативный ноль), useBklitHover→setActiveKey дедуплен (проверено безопасно).

**Вердикт chart-слоя:** в целом хорошо инженерно для 24/7 (`Plotly.react` vs `newPlot` корректно, rAF-throttle hover, коридоры lo/hi/lo2/hi2 и TZ — без перепутанных границ/off-by-one). Единственная реальная утечка — `SensorChart.tsx` (главный, самый используемый график). **Приоритет: `removeAllListeners` на `:652` до следующего long-uptime деплоя.**

### API-слой (client.ts, types/index.ts, time.ts, ErrorBoundary, main.tsx) — агент завершён

- **[CRITICAL · CONFIRMED]** Нет таймаута/AbortController нигде в HTTP-слое — зависший/медленный бэкенд морозит UI навсегда — `client.ts:52-53,122,131,157,163`. react-query висит в `isLoading` бесконечно, `ApiErrorBanner` не срабатывает, дашборд молча замирает на пустых/устаревших данных. → `AbortSignal.timeout(15s)` на каждый fetch + перевод в типизированную ошибку.
- **[CRITICAL · CONFIRMED]** Все ответы `as Promise<T>` без рантайм-валидации — дрейф схемы бэкенда → тихий доступ к undefined — `client.ts:60,125,134,145,160,164`. Переименование/пропажа поля → `NaN` в графике или `TypeError` глубоко в рендере, хотя TS «проверил». → zod/io-ts на границе или хотя бы спот-чек числовых полей.
- **[HIGH · PLAUSIBLE]** Нет retry/backoff на fetch + только `retry:1` в react-query (`main.tsx:22-23`) для control-room UI без видимого индикатора ретрая → transient-блип = до ~10с stale/error на панель. → Экспон. ретрай под SCADA-каденс + per-panel stale/retrying.
- **[HIGH · CONFIRMED]** `errorFrom` не обрабатывает FastAPI 422 `detail: ValidationError[]` — `client.ts:25-41`; non-string `detail` → `JSON.stringify` → оператор видит сырой массив вместо читаемого сообщения. → Спец-кейс на 422 (`msg`/`loc`).
- **[HIGH · PLAUSIBLE]** `BASE` строится двумя стилями (`BASE+path` в `get()` vs template-literal в мутациях, `client.ts:7,44,122,131,157,163`) — латентный баг-магнит при trailing-slash/рефакторе. → Единый `url(path,params)` хелпер.
- **[MEDIUM · CONFIRMED]** `ErrorBoundary` — классовый, конструктивно НЕ ловит async-ошибки (fetch/useEffect/event-handlers/react-query) — `ErrorBoundary.tsx:16-23`; глобального `unhandledrejection`/`onerror` нет → возможен white-screen без фолбэка. → Добавить глобальные хендлеры.
- **[MEDIUM · CONFIRMED]** `ErrorBoundary` не сбрасывается по смене роута/пропа + один глобальный на всё приложение — `ErrorBoundary.tsx:92-93`, `main.tsx:70`. «Попробовать снова» ре-рендерит тот же subtree с теми же плохими данными → мгновенно падает снова; один баг панели гасит весь дашборд. → Per-panel boundary, keyed by station.
- **[MEDIUM · PLAUSIBLE]** `new Date(ts)` только с `Number.isFinite`-гардом, без проверки ISO/`Z` — `time.ts:11-20`, `types/index.ts:80,111`. Если бэкенд отдаст naive-datetime (частый pydantic-pitfall), JS трактует как local → сдвиг на оффсет рантайма, ломая Etc/GMT-5. → Ассертить `Z`/оффсет, иначе громко.
- **[MEDIUM · PLAUSIBLE]** `sensorChart`/`multiChart` не гардят `NaN` в числовых параметрах (`client.ts:80-84,105-108`) → `"NaN"` в query → 422 → нечитаемая ошибка (см. выше). → `Number.isFinite` перед сборкой query.
- **[LOW · CONFIRMED]** `ackNotification/ackEvent/saveGraphSet/deleteGraphSet` строят fetch вручную вне `get()` (`client.ts:121-164`) — дублируют error-handling, обходят будущий хардненинг (таймаут). → Общий `request(method,path,{params,body})`.
- **[LOW]** Слабая типизация: `SensorMeta` nullable-поля без семантики null (`types/index.ts:19-24`), `NotificationItem.severity/kind` = `string|null` вместо union `Severity`/`AnomalyKind` (`:182-183` vs `85-86`) → `SEV_COLOR[severity]` = undefined → неоформленный pill; ручное `encodeURIComponent` на 4 call-site вместо `URLSearchParams`.

**Вердикт API-слоя:** типы ошибок компетентны (401/403/404, извлечение FastAPI `detail`), но **сетевой устойчивости ноль** — ни таймаутов, ни отмены; зависший бэкенд морозит UI без сигнала оператору (серьёзно для control-room). Типобезопасность — театр времени компиляции: каждый ответ `as`-кастится без рантайм-валидации → дрейф бэкенда = тихий `undefined`/`NaN` в графике. ErrorBoundary структурно не ловит async-ошибки этого приложения, а единственное глобальное размещение гасит весь дашборд из-за одной панели.

### Cross-cutting sweep (Sidebar, Overview, Kiosk, HeatMap, Report, Stats, DatePicker, Engine, Freshness, Ticker, Schema, PriorityBanner) — агент завершён

**(A) Системные:**
- **[HIGH · CONFIRMED]** Сырой `toLocaleString/toLocaleTimeString` в обход обязательного `STATION_TZ` (Etc/GMT-5) → время в зоне браузера/RDP — `Freshness.tsx:66,73`, `PriorityBanner.tsx:33` (а `ShiftReport`/`KioskMode` через `fmtStation` — верно). **Та же системная тема, что в App.tsx** — на дашборде, чья суть в достоверных таймстампах, фиксить первым. → `fmtStation(...)`.
- **[MEDIUM · PLAUSIBLE]** 6+ файлов чистят animejs через `.pause()` вместо `.revert()` (v4-teardown) — `Sidebar:163`, `KioskMode:65`, `HeatMap:56`, `SchemaPanel:82+`, `StatsGrid:149`. На 24/7 киоске без размонтирования каждый рефетч со сменой сигнатуры создаёт новую анимацию, старая лишь на паузе → медленное накопление paused-инстансов. → `a.revert()` или подтвердить GC-семантику pause().
- **[MEDIUM · PLAUSIBLE]** `SchemaPanel` и `EngineView` инжектят сторонний HTML/CSS/`<script>` в live-документ дублированной логикой; `EngineView` без `__engineCleanup` (в отличие от SchemaPanel) → ре-инжект скрипта при возврате может двойно регистрировать RAF/listeners — `SchemaPanel.tsx:35-66,119-125`, `EngineView.tsx:38-71,111`. → Тот же teardown-хук в EngineView.
- **[LOW · PLAUSIBLE]** Ховер через прямую мутацию `e.currentTarget.style` вместо CSS-классов в Sidebar/HeatMap/StatsGrid/Ticker/DatePicker → keyboard-only не получает `:focus-visible`-аффорданс. → CSS `:hover`/`:focus-visible`.

**(B) Пофайлово:**
- **[HIGH · CONFIRMED]** `StationOverview.tsx:252-268` — staggered card-анимация БЕЗ cleanup вообще (единственный незащищённый animate); уход со страницы mid-анимации → запись в style размонтированных узлов. → `return () => a.pause()`.
- **[HIGH · PLAUSIBLE]** `Freshness.tsx:20-27,33-36` — bare `fetch` без AbortController + отдельный `setInterval(30s)` поверх react-query-интервала только ради «N мин назад» → удвоение таймеров на каждый инстанс 24/7. → Общий тикер-контекст.
- **[MEDIUM · PLAUSIBLE]** `Sidebar.tsx:260-273` — свежее замыкание+инлайн-стиль на каждую sensor-кнопку; `renderRow` memo, но deps включают `rows` (перестраивается при любом toggle) → все ряды ре-аллоцируют. → Вынести `SensorRow` в `memo` по id.
- **[MEDIUM · PLAUSIBLE]** `StatsGrid.tsx:23` — `AnimatedValue` мутирует `el.textContent` в `onUpdate`, минуя React → ре-рендер по чужой причине mid-tween снапит текст. → Анимировать через `useState`.
- **[MEDIUM · PLAUSIBLE]** `Ticker.tsx:19-29` — badge pop БЕЗ cleanup; при alarm-storm (ровно сценарий этого дашборда) перекрывающиеся scale-твины дерутся за `transform` → джиттер бейджа, когда оператор смотрит внимательнее всего. → Отменять предыдущий твин.
- **[MEDIUM · PLAUSIBLE]** `EngineView.tsx:87-91` — cleanup снимает `body.is-light`, который сам не добавляет (глобальная связка на класс без владения). → Симметричный `.add()` или не трогать.
- **[LOW · CONFIRMED]** `HeatMap.tsx` кодирует severity ТОЛЬКО тонами opacity (crit 20% vs warn 18% vs ok 14%) без второго канала — `:16-21,66-80,172-186`; Sidebar тот же дашборд решил это dot-size+ring, а HeatMap (плотнейший severity-экран) — нет. → Форма/размер/паттерн для crit.
- **[LOW · CONFIRMED]** `DatePicker.tsx:195,203` — `aria-label` disabled-дней не сообщает причину (future vs вне диапазона); `KioskMode.tsx:109`/`ShiftReport.tsx:19` дублируют cutoff-логику окна без единого источника.

**Вердикт cross-cutting:** leak-гигиена в целом крепкая (почти все таймеры/listeners/анимации с cleanup; исключения — `StationOverview` и `Ticker`; `.pause()`-не-`.revert()` — риск накопления за дни). A11y выше среднего для SCADA (реальный focus-trap `useModal`, clickable-div'ы с role+keyboard+aria, Sidebar/PriorityBanner дублируют severity сверх цвета) — но HeatMap второй канал не применяет (острейший a11y-gap). Самый чёткий дефект — TZ-раскол (`Freshness`/`PriorityBanner`), фиксить первым.

### `EventDrawer.tsx` + `DetailPanel.tsx` + `lib/caseBase.ts` — агент завершён

**⚠️ Самый опасный класс находок аудита — operator-safety на SCADA-консоли.**

- **[HIGH · CONFIRMED]** «Принять все» квитирует ВЕСЬ нефильтрованный набор событий, игнорируя активные фильтры severity/kind/период — `EventDrawer.tsx:245-249`, `App.tsx:751-758`. Оператор сузил журнал до «crit · 1 день» (3 события), жмёт «✓ Принять все» (за confirm-шагом, т.е. «принять что вижу») → квитируются ВСЕ severity/kind/30+ дней, включая непросмотренные и вне экрана. Футер «N из M» усиливает заблуждение. **Тихое массовое квитирование непроверенных аномалий.** → Передавать `filtered` в `onAckAll`.
- **[HIGH · CONFIRMED (mismatch) / PLAUSIBLE (impact)]** Ack бьёт по `(sensor_id, timestamp, kind)`, а не по `id` события — `App.tsx:746` ← `EventDrawer.tsx:76 onAck(ev.id)`. UI идентифицирует по `id`, бэкенд `id` отбрасывает и квитирует «все с этой тройкой». Ре-детект в том же 5-мин бакете → квитируется не тот/дублирующий эпизод; оператор думает, что тревога открыта, а сервер считает закрытой. → Принимать и ключевать по `id`.
- **[HIGH · CONFIRMED]** Оптимистичный ack-оверлей стирается на каждом 30с-поллинге, не по подтверждению сервера — `EventDrawer.tsx:252-254` (`useEffect(...,[events])`), `App.tsx:479-482` (новый ref массива каждый рефетч). Пока `POST ack` не отражён в payload, строка «✓ Принято» откатывается в активную на глазах оператора. (App-level `ackedIds` маскирует, т.е. drawer-оверлей избыточен и его сброс — латентная ложь при расхождении.) → Убрать drawer-local optimistic, доверять родителю/ключевать по подтверждению.
- **[MEDIUM · CONFIRMED]** Busy-флаг ack сбрасывается по фиксированному `setTimeout(3000)`, не по завершению — `EventDrawer.tsx:78,332`. На медленном линке в 3.1с кнопка снова активна при in-flight запросе → дубль-POST; при сбое сети — тихо остаётся неквитированным без error-UI. → Гнать busy от промиса/от `ev.acked`.
- **[MEDIUM · CONFIRMED]** `ev.value.toFixed(4)` рендерит литеральные «NaN»/«Infinity» — `EventDrawer.tsx:154,156` (гард только `!= null`, а `NaN != null`). DetailPanel везде через `fmtNum` (`Number.isFinite`→«—»), т.е. два вида расходятся для одного события. «Значение: NaN» = недоверие к drawer. → Финит-гард как в `fmtNum`.
- **[MEDIUM · CONFIRMED]** Нет loading/error-состояния ни в одном виде — `EventDrawer.tsx:220`, `App.tsx:391` (`rawEvents=[]` глотает ошибку). Бэкенд упал → drawer «Событий нет» = неотличимо от чистой установки; ack-POST падает молча (`App.tsx:748`). **Мёртвый бэкенд маскируется под тихую установку.** → Пробросить `isError/isFetching` + баннер.
- **[MEDIUM · CONFIRMED]** Позиция скролла drawer не сбрасывается при открытии/смене фильтра — `EventDrawer.tsx:600-604` (open-эффект `287-297` анимирует только transform). Оператор открывает журнал (`J`) на середине списка → новые critical сверху вне экрана. → `scrollTop=0` на open/смене фильтра.
- **[MEDIUM · CONFIRMED]** `DetailPanel` активный таб хранится в **module-global** переменной — `DetailPanel.tsx:579,584` (`let _persistedTab`). Делится всеми инстансами на весь процесс; при 2 панелях (compare/kiosk/StrictMode) дерутся; «липкий» таб переживает смену сенсора → не-моделируемый сенсор на вкладке «Качество модели» = сплошные «—» без подсказки. → React-state/ref per-instance/URL.
- **[MEDIUM · CONFIRMED]** «Коды здоровья» хардкодят только ml/roc/drift, опуская neg/frozen/seasonal/regime/cross — `DetailPanel.tsx:380-382`. Сенсор с активным `neg` (нарушение физики, `crit`) или `frozen` (залипание) показывает «ml 0/roc 0/drift 0» = «ничего не так», хотя гейдж красный. **Самые safety-релевантные виды невидимы.** → Рендерить по фактически присутствующим kind (`KIND_LABEL`).
- **[MEDIUM · PLAUSIBLE]** `cnt30` (локальный пересчёт) и `anomaly_count_30d` (бэкенд) могут расходиться — `DetailPanel.tsx:375 vs 338-356`; если events-фид обрезан < 30д, `cnt30` недосчитывает при null бэкенд-поля. → Предпочитать бэкенд-поле или лейбл «(за загруженный период)».
- **[MEDIUM · CONFIRMED (static) ]** `caseBase` — «похожие исторические случаи» и SHAP-вклады **захардкоженная фикция, выданная за данные** — `caseBase.ts:8-128` (все `feats`/`similar`/`val` — литералы: напр. «ГПА-3 · 21.03 — износ подшипника», `rpm_tvd +0.42`) рендерятся независимо от реального события. На SCADA-консоли это **опасно by-authority: выглядит как доказательство**. → Реальная атрибуция из `SensorExplain` (уже есть в types) или явный лейбл «типовой пример, не расчёт».
- **[LOW/MEDIUM · CONFIRMED (mechanics)]** `caseKey` — порядок-зависимый substring-матчинг (`caseBase.ts:135-151`, `sensorId.includes(needle)`, first-wins) → `gas_temp`→`gas_temp_out` даже для inlet; диагноз/чек-лист/«похожие» для НЕ ТОГО физического узла. → Явный маппинг sensor→group по тегу/подсистеме из `SensorMeta`.
- **[LOW]** `buildGroups`/day-separator предполагают отсортированность events без гарантии (`EventDrawer.tsx:39-54,626-629`) → при unsorted фрактурят группы/дни; blanket `eslint-disable exhaustive-deps` в HealthTab (`DetailPanel.tsx:357-358`); `nmaePct = nmae*100*4` магический масштаб (`:475`) — 0.25 уже пегает бар; focus-trap `useModal` пере-запрашивает focusables на транзиентном disabled ack-кнопки (`:38-57`).

**Вердикт EventDrawer/DetailPanel:** самый опасный класс реален — «Принять все» квитирует нефильтрованное сверх видимого, ack ключуется неуникальной тройкой вместо `id`, optimistic-оверлей стирается 30с-поллингом. Плюс NaN.toFixed рендерит «NaN» (DetailPanel гардит), а весь caseBase — фикция под видом расчёта. **Фиксить ack-scope и ack-key первыми (operator-safety), затем loading/error-UI.**

---

## Итерация 2 — сводка (frontend)

| Файл | HIGH | MEDIUM | LOW |
|---|---|---|---|
| App.tsx | 4 | 8 | 3 |
| EventDrawer+DetailPanel+caseBase | 3 | 7 | 4 |
| api/client + types + ErrorBoundary | 2 CRIT + 3 | 4 | 3 |
| Chart-компоненты | 1 CRIT + 1 | 4 | 4 |
| cross-cutting (12 компонентов) | 2 | 5 | 3 |

**Сквозные темы frontend:**
1. 🕐 **TZ-раскол** — `Clock`, `sidebarLastUpdated`, zoom-парсер (App.tsx), `Freshness`, `PriorityBanner` рендерят в браузерной TZ вместо `STATION_TZ`. На дашборде достоверных таймстампов — фиксить первым.
2. 🛡️ **Operator-safety ack** — «Принять все» сверх фильтра, ack по тройке не по id, optimistic-откат, busy по таймеру, глушение ошибок (App.tsx `handleAck` + EventDrawer).
3. 🌐 **Сетевая устойчивость = 0** — нет таймаута/AbortController (зависший бэкенд морозит UI), нет рантайм-валидации ответов, ErrorBoundary не ловит async + гасит весь дашборд.
4. 💧 **Leak на 24/7 киоске** — `SensorChart` не снимает Plotly-listeners перед purge (CRIT); `StationOverview`/`Ticker` анимации без cleanup; `.pause()` вместо `.revert()` в 6 файлах.
5. 🎭 **caseBase — фикция под видом данных** на SCADA-консоли.

---

## Итерация 3 — Verify (безопасная проверка) + UI-review

**⚠️ Решение по `/verify`:** полный запуск (`run.py`) подключается к боевой SCADA-БД `10.1.30.164` и авто-выполняет `migrate_db` (есть ветка `DROP TABLE`, см. security-sweep). Гонять живой прод в рамках аудита не стал — вместо этого честная безопасная верификация: typecheck + lint (статический анализ) + юнит-тесты чистой математики, без обращения к проду.

### Frontend: `tsc --noEmit` — ✅ ЧИСТО (0 ошибок)
Несмотря на обильные находки про `any`/unsafe `as` из ревью-агентов, компилятор проходит без ошибок. Позитивный сигнал по дисциплине типов на уровне «компилируется».

### Backend: юнит-тесты (`test_calibrator.py`, `test_regime.py`, `test_aging_watchdog.py`) — ✅ 23 passed
Чистая математика (калибровка, детекция режимов, aging watchdog) покрыта и зелёная. **Оговорка:** эти тесты НЕ покрывают методологические баги валидности conformal из train.py (переиспользование calibration/eval, случайный ES-сплит) — юнит-тесты по своей природе не ловят такие «валидные по форме, но статистически некорректные» ошибки.

### Frontend: `eslint .` — 9 errors, 41 warnings — **независимое подтверждение находок ревью + новые баги**

Полный список 9 ошибок извлечён из JSON-отчёта. React's новые `react-hooks/*`-правила (immutability/globals/refs/purity/preserve-manual-memoization) и `no-useless-assignment` поймали то же самое, что нашли LLM-агенты, плюс кое-что новое — **сильная триангуляция двумя независимыми методами:**

- **[🎯 ПОДТВЕРЖДЕНО ДВУМЯ МЕТОДАМИ]** `DetailPanel.tsx:587` — `react-hooks/globals` **ERROR**: «Cannot reassign variables declared outside of the component/hook» — `_persistedTab = key` внутри `setTab`. Это ТА ЖЕ находка, что LLM-агент нашёл чтением кода (module-global state), только здесь она поймана статическим анализом с формулировкой «side effect… can cause unpredictable behavior». Два независимых метода сошлись на одной строке → уверенность CONFIRMED, не PLAUSIBLE. → `useState`/ref per-instance, как рекомендовал агент.
- **[NEW · ESLint ERROR]** `chartMotion.ts:102` — `no-useless-assignment`: переменной `len` присваивается значение, которое никогда не используется дальше. Мёртвый код/огрызок логики. → Проверить, убрать/использовать.
- **[NEW · ESLint ERROR]** `ContributingFeatures.tsx:241` — `no-useless-assignment`: `verdict` присваивается и не используется дальше — в контексте SHAP/объяснений (файл уже отмечен агентом за `any`-касты) это может маскировать недописанную логику проверки вердикта. → Проверить, не потеряна ли ветка использования.
- **[NEW · ESLint ERROR]** `App.tsx:619` — `react-hooks/immutability`: `setFocusEventId` используется в `useCallback` на строке 619 **до объявления** на строке 664 (temporal dead zone по коду, не по рантайму — setState-сеттер стабилен, но само упорядочивание сигнализирует о неаккуратной организации 1265-строчного файла). → Переставить объявление `useState` выше первого использования.
- **[NEW · ESLint ERROR ×2, эвристика компилятора]** `App.tsx:772,793` и `DatePicker.tsx:85` (×2) — `react-hooks/preserve-manual-memoization`: React Compiler не может сохранить ручную мемоизацию, т.к. инферренс зависимостей не совпадает с явным dep-массивом. Не баг рантайма сегодня (ручной `useCallback`/`useMemo` работает как написан), но блокирует будущий React Compiler и является слабым сигналом, что явные deps могут быть уже, чем реально используемые значения — стоит перепроверить вручную на `App.tsx:793` (`sidebarLastUpdated` deps `[stats?.last_updated]` при инферренсе `stats`).
- **[NEW · ERROR]** `ContributingFeatures.tsx:136` — `@typescript-eslint/no-explicit-any` как error (не warning) — усиливает находку агента про `any`-касты на Plotly-объектах.
- **[CONFIRMS cross-cutting finding]** `react-hooks/purity` («Cannot call impure function during render», `Date.now()`) — подтверждает и точно локализует дубль cutoff-логики:
  - `EventDrawer.tsx:311` (`dayCutoff` в useMemo фильтра журнала) — **новое место**, не замечено агентом EventDrawer/DetailPanel.
  - `Freshness.tsx:65` — подтверждает TZ/impure-находку.
  - `KioskMode.tsx:109` — подтверждает находку про дублированный cutoff.
  - `ShiftReport.tsx:19` — подтверждает cutoff-находку; **плюс новое**: `useMemo` держит `[open]` как **лишний** dep (`exhaustive-deps`) — cutoff «12ч назад» замораживается на момент открытия модалки, не обновляется при долго открытой модалке.
- **[NEW]** `SchemaPanel.tsx:123` — `stylesRef.current` может измениться к моменту вызова cleanup — прямо релевантно cross-cutting находке про SchemaPanel/EngineView inject-логику. → Скопировать ref в переменную внутри эффекта.
- **[minor]** `HeatMap.tsx:60`, `Sidebar.tsx:168` — висящие «Unused eslint-disable directive» (мёртвые аннотации).

**Итог verify:** typecheck и юнит-тесты — зелёные (реальный сигнал качества, не фиктивный). Lint вскрыл 9 настоящих ошибок, из которых одна — **независимое подтверждение находки LLM-агента тем же файлом:строкой** (`DetailPanel.tsx:587`, module-global tab), две — новый мёртвый код (`chartMotion.ts:102`, `ContributingFeatures.tsx:241`), остальные — компилятор-диагностика и организационные проблемы. **Рекомендация:** включить `eslint` в CI/pre-commit — сейчас 9 errors проходят в прод молча (`npm run build` = `tsc -b && vite build`, lint отдельно и не блокирует).

### `/gsd:ui-review` — визуальный 6-факторный UI-аудит — агент завершён

Полный отчёт: [`UI-REVIEW.md`](./UI-REVIEW.md) (36 read/скриншот-проверок, 8 рендеров). **Общая оценка: 6.9/10.**

| Столп | Оценка |
|---|---|
| Визуальная иерархия и вёрстка | 7/10 |
| Цвет и контраст | 7/10 |
| Типографика | 8/10 |
| Консистентность / дизайн-система | 6/10 |
| Обратная связь и состояния | 6/10 |
| Доступность и взаимодействие | 8/10 |

**🎯 ТРОЙНАЯ ТРИАНГУЛЯЦИЯ — «фейковые данные под видом реальных» (самая сильная находка всего аудита):**
LLM-агент (EventDrawer/DetailPanel) нашёл это в `caseBase.ts` через чтение кода. UI-аудитор независимо (через рендер + код) подтвердил `caseBase` И нашёл **вторую площадку той же болезни**: `DetailPanel.tsx:47-178` (`INDEX_BASE`) хардкодит доменные индексы (`polytropic_eff:0.81 (ok)`, `shaft_resid_tnd:+38 (warn)`) и рендерит их as-is, warn-раскрашенными, когда реальных данных `domain` нет — единственный отличитель: 10px подпись «кейс из базы»/«○ оценка по базе» (ContributingFeatures.tsx:462-464), **слишком незаметно для safety-контекста**. → Не рендерить числа/спарклайны похожие на измеренные при отсутствии реального `/explain`/`domain`; явный «типовой пример (не измерено)», серым, с диагональным вотермарком.

**🕐 ЧЕТВЕРНОЕ ПОДТВЕРЖДЕНИЕ TZ-раскола + новое место:** `App.tsx:794/1226/1230`, `PriorityBanner.tsx:33`, `Freshness.tsx:66/73` — все уже известны (агенты App.tsx, cross-cutting, ESLint purity). **Новое:** `ContributingFeatures.tsx:182` (`fmtDt`) — ещё один bare `toLocale*`. Скриншот-пруф: «данные: 17:21 · **1327 мин назад**» (~22ч) — реальный видимый мис-рид на скриншоте. → Единый `fmtStation` + lint-запрет bare `toLocale*` вне `lib/time.ts`.

**🎨 Двойное подтверждение — HeatMap colorblind-only-hue:** cross-cutting-агент нашёл это чтением кода, UI-аудитор подтвердил визуально по скриншотам (`unified-monitor.png`/`verify2.png`: «дальтоник-дейтеранопик не отличит crit `gt01.ctrl.in` от ok»). Именно теплокарта — первичная поверхность triage → это найдено дважды независимо и это наивысший a11y-риск отчёта.

**Новые уникальные находки UI-аудита:**
- **[Иерархия]** `StatsGrid` ранжирует карточки по **количеству**, не severity — `AnimatedValue` увеличивает шрифт при `value>0` (`:37`), `crit`-стиль только при `value>=10` (`:64`). На скриншоте амбер «СКАЧОК ΔV 208» крупнее и громче, чем реально критичный «СТАТ. ВЫБРОС 10». На алярм-щите глаз должен идти на severity, не на количество info-спама. → Ранжировать/размер по max-severity, потом по счёту.
- **[Дизайн-система]** 52 хардкод-hex в 6 файлах (SensorChart 25, EventDrawer 7, ContributingFeatures 4, useBklitHover 3, MultiSensorChart 3, ErrorBoundary 10); **3 разные хардкод-палитры для одних и тех же 7 типов аномалий** (`SensorChart.KIND_COLOR` `ml:#FF4560` vs `EventDrawer.KIND_DOT` `ml:#CC3333` vs `ContributingFeatures.FEAT_COLORS`) — «Стат. выброс» разного красного на графике/в журнале/в кейс-карточке, ничего не ретемится. → Один `--kind-*` токен-сет с `.light`-вариантами.
- **[Дизайн-система]** Два независимых компонента `Clock` (`App.tsx:1224`, `KioskMode.tsx:37`) с разным форматом и разной TZ-корректностью. → Консолидировать.
- **[Типографика]** ~40% размеров шрифта — сырые px мимо шкалы (`DetailPanel` 13/15/16/12.5, HeatMap/ContributingFeatures 9/10/11); `fontFamily:'Inter, monospace'` — неверный фолбэк (`App.tsx:67`, `SensorChart.tsx:356`) — при незагрузке Inter пропорциональный UI внезапно становится monospace. → Токены + `system-ui, sans-serif`.
- **[Типографика]** Легенда графика показывает `Модель (MAE: 2.55)` (`SensorChart.tsx:194`), хотя `DESIGN.md §10` называет MAE «мёртвой метрикой…везде 0.0» — либо тихо переиспользована, либо не согласована с канонiчным доком. → Убрать из легенды в Detail→«Качество модели».
- **[Feedback]** Бесконечные pulse-анимации (`crit-bar-pulse` и т.п., `globals.css:346-351`) крутятся вечно на всегда-включённом экране — усталость + риск burn-in. → Капать циклы или пульсировать только top-priority crit.
- **[A11y]** Английский тултип Plotly «Double-click to zoom back out» протекает на русский UI поверх Analytics-панели (виден на скриншоте `verify2.png`) несмотря на `displayModeBar:false`. → `config.locale`/CSS на `.plotly-notifier`.
- **[A11y]** Drag-оверлей сенсора на график (Sidebar/HeatMap `draggable`) не имеет клавиатурного эквивалента — keyboard-юзер не может собрать multi-series сравнение. → Кнопка «+»/Enter+модификатор.
- **[A11y]** Тач-цели ниже комфорта Fitts для потенциально touch-панели: HeatMap-ячейки 28×24px, тулбар-кнопки 24-26px (рекомендация ≥44px для киоска). → Увеличить.
- **[Позитив, не регрессировать]** Честный ML-quality в DetailPanel (показывает «—» вместо фейковых чисел, объясняет R² in-sample-vs-holdout оператору) — качественный контраст с находкой про INDEX_BASE/caseBase: одни части файла честны, другие — фейковы.

**Топ-10 UI-фиксов** (полный список с обоснованием — в UI-REVIEW.md) ранжированы риск×охват; #1-2 — фейковые данные, #3 — TZ, #4 — HeatMap 2-й канал, #5 — StatsGrid по severity, #6 — NaN в журнале (уже известно), #7 — Plotly-тултип, #8 — унификация kind-цветов, #9 — пустой центр экрана без выбранного сенсора, #10 — pulse-усталость + тач-цели.

---

## Итерация 4 — `/simplify`-lens (reuse/дублирование/эффективность, НЕ correctness)

**⚠️ Методологическая заметка:** это отдельный «объектив» ревью — намеренно НЕ ищет баги (те уже покрыты итерациями 1-3), а ищет дублирование/лишнюю сложность/декомпозицию. Изменения НЕ применены к коду (только предложения) — 24k-строчная прод-система не подходящий кандидат для автономного рефакторинга без явного согласия пользователя.

### Frontend God-files (App.tsx, SensorChart.tsx, EventDrawer.tsx, DetailPanel.tsx, ContributingFeatures.tsx) — агент завершён

- **[small]** 4 независимые реализации одного паттерна «ghost-кнопка, hover→accent border/color» через ручной `onMouseEnter/onMouseLeave` — `App.tsx:1021-1024`, `ComparePanel.tsx:241`, `EventDrawer.tsx:585-591`, `EngineView.tsx:154`. → Один `<GhostButton>` или `.btn-ghost:hover` класс.
- **[trivial]** Кнопка-пресет «1/3/7/14/30 дней» в App.tsx полностью инлайновая, хотя соседние 2 кнопки в той же строке уже используют вынесенные константы `styleFeatBtn`/`styleCompareBtn` — `App.tsx:1048-1061` vs `225-239`. → `styleDayBtnBase` по тому же паттерну.
- **[small]** Кнопка «Сравн. ГПА» — последняя в toolbar-ряду без вынесенной style-константы — `App.tsx:1066-1075`. → `styleGpaOverlayBtn` аналогично сиблингам.
- **[medium]** `App.tsx` (1265 строк) смешивает ≥5 независимо закрываемых зон в одном теле компонента: drop-zone+3 drag-хендлера (`~1002-1017`), toolbar (`~1026-1081`), focus-event панель (`~1083-1132`), чипы dropped-сенсоров (`~1133-1148`), schema-toast (`~1186-1205`). → Вынести `<ChartDropZone>`, `<SensorToolbar>`, `<FocusEventPanel>` — чистый JSX-lift с уже вычисленными пропсами, без изменения логики.
- **[medium]** 8 отдельных `useQuery`/`useQueries` в App.tsx с почти идентичным boilerplate `refetchInterval/staleTime/gcTime` — `App.tsx:354-450`. → `useDashboardData(activeStation)` для 5 station-level запросов (chart/range/zoom оставить в App — у них реальное различие в кэш-логике). React Query дедуплицирует по queryKey независимо от места вызова — безопасно.
- **[trivial]** 3 параллельные реализации «filter pill» (period-фильтр EventDrawer, severity-табы EventDrawer, day-preset App.tsx) — `EventDrawer.tsx:418-475`, `App.tsx:1048-1061`. → Один `<FilterPill active/onClick>`.
- **[trivial]** UTC-ms→station-day-index математика определена инлайн в `DetailPanel.tsx:342-344` (`dayIdx`), хотя `lib/time.ts` уже содержит родственный `stationYMD`. → `stationDayIndex(ms)` в `lib/time.ts` рядом.
- **[trivial]** `SensorChart.tsx` и `ContributingFeatures.tsx` определяют идентичные `PlotlyData/PlotlyLayout/PlotlyModule`-типы и одинаковый memoized `getPlotly()`-загрузчик параллельно — `SensorChart.tsx:55-85`, `ContributingFeatures.tsx:30-41`. → Один `lib/plotly.ts`.
- **[trivial]** `SensorChart.tsx._fmtLocal/_tsMs` вероятно дублируют то, что уже есть в `lib/time.ts` (`fmtStation`/`stationYMD`) под другими именами — `SensorChart.tsx:97-101`. → Проверить и свести.
- **[small]** `EventDrawer.EventRow` и инлайновый group-header (`renderGroup`) повторяют ~90% одной grid-разметки (severity-bar/badge/имя/ack) параллельными копиями — `EventDrawer.tsx:70-218` vs `681-773`. → Общий `<EventCardShell>`.
- **[trivial]** `focusEventId`/`setFocusEventId` (`App.tsx:664`) объявлен на ~350 строк ниже остальных 15 `useState` (`284-316`) — чисто читаемость. → Поднять к остальным.
- **[trivial]** `tbtnStyle(active)` в SensorChart частично затеняется инлайн-оверрайдами для `conf`/`hyb`-кнопок вместо повторного использования — `SensorChart.tsx:894-905`. → Отдельный `tbTextBtnStyle`.

**Вердикт frontend-simplify:** сложность в основном сущностная (Plotly hover/crosshair/region-select, dual corridor-рендер, epistemic-subplot — реальная SCADA-charting доменная сложность, сокращать which нельзя без потери функциональности). Случайная сложность сконцентрирована в App.tsx toolbar-кнопках (4-6 дублей) и Plotly-loader boilerplate между 2 файлами — обе категории маленькие, механические, низкорисковые чистки на несколько сотен строк без касания сложных частей.

### Backend God-files (live_predict.py, train.py, main.py, data_loader.py, station_config.py) — агент завершён (после возобновления)

- **[small]** `_finalize_calibration` дублирует ~100 строк (шаги 4-8: eval-гейт, epistemic ref, load binning, dual conformal calibration, horizon-метрики, feature importance) почти verbatim с хвостом `train_sensor` — `train.py:943-1125` vs `1192-1294`. → Вынести шаги 4-8 в общую функцию, параметризованную источником `mdl`/`base_pred`; звать из `train_sensor` и `train_sensor_pooled`.
- **[small]** Паттерн «retry on OperationalError/InterfaceError + reset_pool + backoff» переизобретён ≥5 раз с разной формой цикла — `data_loader.py:69-82,121-134,513-526,587-601`, `live_predict.py:198-209`, `station_config.py:198-217`. → Один `with_db_retry(fn, retries, backoff)` в `station_config.py`.
- **[small]** Сниппет naive→UTC / UTC→station-local конвертации скопирован ≥6 раз вместо одного хелпера — `data_loader.py:623-624,755-756,744-745`, `live_predict.py:170-172,1097-1099,1931-1933`, `main.py:911-918`. → Два хелпера `to_utc_aware`/`local_to_utc` в `station_config.py`/`tz_utils.py`. (Чисто механическое дублирование уже корректного сниппета — НЕ те TZ-баги, что нашла correctness-ревью, те остаются как есть.)
- **[trivial]** `save_domain`/`save_predictions` — почти идентичные chunked-upsert-with-retry циклы (chunk 5000, 3 попытки, `execute_values`), различаются только SQL-шаблоном — `data_loader.py:497-527,568-602`. → `_upsert_chunked(insert_sql, rows, chunk, retries)`.
- **[trivial]** `predict_sensor` (~460 строк) делает предсказание + 2 варианта коридора + 8 детекторов + cross-GPA подготовку в одной функции — `live_predict.py:367-825`. → Разбить на `_resolve_features/_build_corridor/_detect_*`, тот же возвращаемый dict.
- **[trivial]** `GAP_WINDOW_DAYS` (`live_predict.py:61`=2) концептуально пересекается с `window_days` в train.py (default 20) — значения различаются по легитимным причинам (train-окно vs catch-up-окно), но без кросс-референса в комментарии читатель может решить, что они должны совпадать. → Комментарий-ссылка, без изменения значений.
- **[trivial]** `DF=DQ=RG=CAL=TR` алиасинг в live_predict.py — `DQ`, похоже, не используется нигде в файле (легаси-совместимость, о чём сам комментарий и предупреждает). → Подтвердить grep'ом по всему репо и удалить, если действительно мёртв.
- **[small]** `_explain_region` и fallback-ветка `sensor_explain` дублируют одинаковую загрузку wrapper+resolve feat columns+Xdf+SHAP-вызов — `main.py:1273-1282` vs `1385-1394`. → Общий `_load_model_and_resolve_X(...)`.
- **[trivial]** `_num()` (None/bool-safe коэрсия) реально переиспользуется в live_predict.py, но `main.py._sensors_list` реимплементирует ту же идею через паттерн `float(x or 0.0)` инлайн — именно та ловушка (0.0 реальное значение vs None), от которой `_num` защищает — `live_predict.py:328-332` vs `main.py:289-290`. → Импортировать `_num`, это строго безопаснее, не только опрятнее.
- **[trivial]** Два почти идентичных блока «обрезать до N дней + stride-сэмпл до 2000 точек» в `station_sensor_chart` и `station_chart_multi` — `main.py:1016-1029,1484-1496`. → `_state_series_fallback(sp, t0_dt, t1_dt, effective_days)`.

**Вопрос про v1-коридор (закрыт):** докстринг train.py (строки 1-33) явно заявляет единую v23.0 normalized-conformal методологию — **параллельного v1-пути НЕТ**. `train_sensor` (per-unit) и `train_sensor_pooled` (pooled cross-GPA) — оба v2-only, делят одну calibration/detector-mode машинерию. Видимое дублирование — это `train_sensor` vs `_finalize_calibration` (оба v2, находка #1 выше), не v1-vs-v2 легаси-долг. Поля `sensor_to_meta` типа `model_type:"CatBoostUnc-v2"` — это output-совместимость для main.py/фронтенда, не параллельный тренировочный путь.

**Вердикт backend-simplify:** умеренный, но настоящий низкорисковый simplify-долг — в основном дублированные retry/TZ/chunked-upsert каркасы и один явный copy-paste хвост калибровки в train.py — все адресуемы изолированными механическими extraction'ами. Основной объём и ветвление (regime detection, dual-corridor conformal, 8-детекторный ансамбль, pooled cross-GPA адаптеры) — сущностная сложность домена conformal prediction и multi-detector fusion, не случайная, и simplify-пассом её трогать не стоит.

---

## Итерация 4 — сводка simplify

Оба simplify-агента сошлись на одном выводе для своих доменов: **сложность в основном сущностная** (conformal prediction/multi-detector на backend; Plotly hover/corridor/epistemic на frontend), а случайная сложность — дублирование retry/TZ-хелперов (backend) и toolbar-кнопок/Plotly-loader (frontend), обе категории — маленькие механические чистки. Ни один пункт не применён к коду — это предложения для отдельного захода с согласия пользователя.

---






