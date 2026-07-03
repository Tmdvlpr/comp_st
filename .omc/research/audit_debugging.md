

I now have all the data I need. Let me compile the comprehensive audit report.

## Аудит отладочной инфраструктуры (Debuggability / Observability)

**Проект:** КС-4 — система промышленного мониторинга компрессорной станции
**Файлов проверено:** 14 (backend: main.py, live_predict.py, station_config.py, data_loader.py, train_and_save_models.py, ensure_indexes.py; frontend: main.tsx, App.tsx, client.ts, Freshness.tsx, SchemaPanel.tsx, vite.config.ts; frontend/public: schema.html)
**Общее количество проблем:** 22

---

### Сводка по severity

- **CRITICAL:** 4 (необходимо исправить немедленно)
- **HIGH:** 6 (требуется исправление в ближайшем релизе)
- **MEDIUM:** 7 (желательно исправить)
- **LOW:** 5 (улучшения при наличии ресурсов)

---

### CRITICAL

**[CRIT-1] live_predict.py: полное отсутствие logging -- весь вывод через print() с эмодзи**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\live_predict.py` (весь файл)
Проблема: В live_predict.py **35+ вызовов print()** с эмодзи-префиксами и **0 вызовов logger**. При запуске как фоновый процесс (без терминала) весь stdout теряется. Нет logging.basicConfig, нет FileHandler, нет RotatingFileHandler **нигде в проекте**. Когда live_predict умер на 3 недели -- не было ни одного лог-файла для диагностики.
Аналогично: `train_and_save_models.py` (20+ print), `sync_raw_data.py` (10+ print).
`main.py` и `data_loader.py` используют `logger = logging.getLogger(__name__)`, но **без logging.basicConfig или handler-а** -- при запуске через `uvicorn` логи попадают только в stderr uvicorn, при запуске без uvicorn -- логи не пишутся никуда.
Рекомендация: Создать единый модуль `logging_config.py` с `logging.basicConfig`, `RotatingFileHandler` (в `logs/`), `TimedRotatingFileHandler` (ротация по дням), уровень INFO. Заменить все `print(f"emoji ...")` на `logger.info/warning/error(...)`. Добавить `logging_config.setup()` в точку входа `live_predict.py`, `train_and_save_models.py` и uvicorn startup.

**[CRIT-2] live_predict.py: run_continuous() не ловит исключения -- один необработанный exception убивает процесс навсегда**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\live_predict.py:2619-2639`
Проблема: Основной цикл `while True` вызывает `run_once()` без try/except. Любое необработанное исключение (MemoryError, KeyError в изменённых данных, сетевой таймаут) убивает процесс. Рестарта нет -- нет supervisord, NSSM, Task Scheduler, systemd-unit, ни одного файла `.bat` или `.service` для автозапуска. Процесс молча умирает, state-файл протухает, **никто не замечает 3 недели**.
```python
while True:
    time.sleep(REFRESH_INTERVAL)
    updated_df = run_once(existing_df=current_raw_df)  # <-- без try/except
```
Рекомендация: 1) Обернуть `run_once()` в try/except с `logger.exception()` и backoff-повторами. 2) Создать NSSM-сервис или Task Scheduler задачу с автоперезапуском при падении. 3) Записывать PID-файл при старте для обнаружения зомби/дублей.

**[CRIT-3] Нет supervision / process management -- процессы не перезапускаются при падении**
Файл: Проект в целом (отсутствуют файлы)
Проблема: Не найдено ни одного файла конфигурации supervisor (NSSM, systemd, Windows Task Scheduler XML, .bat-скрипты запуска). И FastAPI backend, и live_predict запускаются вручную. При падении -- никакого автоматического рестарта. Это подтверждается реальным инцидентом (live_predict умер на 3 недели).
Рекомендация: Создать NSSM-сервис для live_predict и uvicorn на Windows. Определить `restart=always`. Добавить скрипт `install_services.bat` в репозиторий.

**[CRIT-4] /api/health не проверяет свежесть данных -- не может обнаружить мёртвый live_predict**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\main.py:557-567`
Проблема: Эндпоинт `/api/health` возвращает `last_updated` как информацию, но **не сравнивает его с текущим временем и всегда возвращает `status: "ok"`**. Внешний мониторинг (Zabbix, Prometheus, простой curl-чекер) получит `200 OK` даже когда state-файл протух на недели. Health check бесполезен для обнаружения мёртвого live_predict.
```python
def health():
    # ...
    result: dict = {"status": "ok", ...}  # <-- всегда "ok"
    # нет проверки: if age > THRESHOLD: status = "degraded"
```
Рекомендация: Сравнивать `last_updated` с `time.time()` и возвращать `status: "degraded"` если данные старше 15 минут, `status: "down"` если старше 1 часа. Добавить HTTP 503 для degraded/down -- чтобы простой HTTP-чекер мог поймать проблему.

---

### HIGH

**[HIGH-1] live_predict.py: 4 блока `except Exception: pass` глотают ошибки без лога**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\live_predict.py:2478, 2501, 2556, 2568`
Проблема: В функции `_build_state()` четыре блока `except Exception: pass` -- ошибки формирования серий, аномалий и событий молча игнорируются. Если формат данных изменится или появится NaN/None в неожиданном месте -- данные молча пропадут без следа. Блок на строке 2556 особенно опасен -- он скрывает ошибки эпизодизации аномалий, из-за чего аномалии могут не попасть в state_events.
Рекомендация: Заменить `pass` на `logger.debug("Skipped point: %s", e, exc_info=True)` минимум. Для строки 2556 -- `logger.warning` т.к. потеря аномалий влияет на оператора.

**[HIGH-2] main.py: list_station_infos() -- `except Exception: pass` скрывает ошибки конфигурации станций**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\main.py:249-250`
Проблема: Если конфигурация станции повреждена или БД для state-файла недоступна, станция молча исчезает из списка. Оператор видит пустой список станций и не понимает почему.
Рекомендация: Добавить `logger.exception("Failed to load station %s", sid)` в except блок.

**[HIGH-3] live_predict.py: нет защиты от двойного запуска -- два процесса пишут в один state-файл**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\live_predict.py:2610-2615`
Проблема: Нет PID-файла, нет file-lock, нет проверки порта. Если запущены два экземпляра (от разных интерпретаторов Python, как в описанном инциденте) -- оба пишут `os.replace(tmp, out)` в один state-файл, гонки данных, непредсказуемое поведение. Операция `os.replace` атомарна, но результат -- случайный выбор "победителя".
Рекомендация: Использовать файловую блокировку (например `msvcrt.locking` на Windows или `portalocker`) на PID-файл при старте. Если lock не получен -- выдать ошибку и не стартовать.

**[HIGH-4] live_predict.py: нет graceful shutdown -- нет обработки сигналов**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\live_predict.py` (отсутствует)
Проблема: Нет `signal.signal(SIGTERM, ...)`, `atexit`, или KeyboardInterrupt handler. При kill -9 или остановке сервиса -- state-файл может остаться в состоянии `.tmp` (если process убит между `open(tmp)` и `os.replace`), что приведёт к потере данных для dashboard.
Рекомендация: Добавить `signal.signal(signal.SIGTERM, shutdown_handler)` и `atexit.register(cleanup)`. В shutdown_handler установить флаг для выхода из `while True`, дождаться завершения текущего цикла.

**[HIGH-5] Frontend: полное отсутствие React ErrorBoundary**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\src\main.tsx`, `App.tsx`
Проблема: Ни в main.tsx, ни в App.tsx нет ErrorBoundary. Любое необработанное исключение в React-компоненте (например NaN в данных графика, undefined свойство сенсора) вызовет белый экран смерти. Для 24/7 системы мониторинга это означает, что оператор потеряет всю видимость при JS-ошибке.
Рекомендация: Добавить `<ErrorBoundary>` вокруг `<App />` в main.tsx с fallback UI, показывающим "Ошибка отображения, данные обновляются, перезагрузите страницу".

**[HIGH-6] main.py: _get_live_state() и _get_metadata() молча возвращают пустые/кэшированные данные при ошибках**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\main.py:79-80, 98-100`
Проблема: Обе функции при ошибке чтения JSON (битый файл, partial write) молча возвращают кэшированные данные или `{}` без логирования. Оператор видит устаревшие данные, не подозревая о проблеме.
Рекомендация: Добавить `logger.warning("Failed to read state file: %s", e)` в except блоки.

---

### MEDIUM

**[MED-1] Отсутствие timestamp в print-выводе live_predict**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\live_predict.py` (все print-вызовы)
Проблема: Из 35+ print-вызовов только один содержит время (`Обновление #{iteration} ({pd.Timestamp.now()...})`). Остальные -- без timestamp. Даже если stdout перенаправлен в файл, невозможно определить КОГДА произошло событие.
Рекомендация: При переходе на logging -- формат `"%(asctime)s %(levelname)s %(name)s %(message)s"` решит это автоматически.

**[MED-2] Freshness-индикатор покрывает только данные live_predict, не покрывает доступность БД**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\src\components\Freshness\Freshness.tsx:1-43`
Проблема: Freshness показывает `last_updated` из state-файла live_predict. Если БД недоступна (как описано в контексте -- "периодически лежит"), но state-файл свежий -- оператор не видит проблему. Нет индикатора доступности БД, нет индикатора состояния API.
Рекомендация: Добавить в `/api/health` проверку доступности БД (простой `SELECT 1`). Отображать отдельный индикатор "БД" во Freshness. При ошибках fetch в `api/client.ts` -- показывать красный индикатор "API недоступен".

**[MED-3] api/client.ts: ошибки API не показываются оператору**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\src\api\client.ts:16-17`
Проблема: `get<T>()` бросает `throw new Error(...)` при ошибке, но React Query с `retry: 1` молча ретраит и затем просто не обновляет данные. Нет toast/banner/индикатора для оператора, что API вернул ошибку. Оператор видит устаревшие данные без понимания причины.
Рекомендация: Добавить `onError` callback в React Query defaultOptions или глобальный QueryCache error handler, показывающий banner "Ошибка обновления данных".

**[MED-4] schema.html: секция 37 "Самопроверка" и PV API ошибки видны только в DevTools console**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html:2374-2383, 2007`
Проблема: `console.warn('САМОПРОВЕРКА: найдены проблемы', errs)` и `console.warn('PV API недоступен:', e.message)` -- оператор на мониторе у ЦПУ не откроет DevTools. Проблемы с конфигурацией схемы и недоступность PV API невидимы.
Рекомендация: Показать визуальный banner/toast в интерфейсе схемы при ошибках самопроверки. При недоступности PV API -- показать жёлтый banner "Данные SCADA временно недоступны".

**[MED-5] Sourcemaps отключены в production build**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\vite.config.ts:6-18`
Проблема: Нет `build.sourcemap: true` в vite.config.ts. Vite по умолчанию не генерирует sourcemaps в production. При JS-ошибке в production -- стектрейс будет указывать на минифицированный код, диагностика практически невозможна.
Рекомендация: Добавить `build: { sourcemap: 'hidden' }` -- sourcemaps будут генерироваться, но не ссылаться из bundle (безопасно для production).

**[MED-6] Нет структурированного логирования -- невозможно грепать по sensor_id/station**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\live_predict.py`, `main.py`
Проблема: Даже те print-вызовы, что есть, содержат свободный текст с эмодзи. Невозможно сделать `grep "sensor_id=bearing_temp__GPA2"` по логам. Нет контекста station_id в сообщениях (кроме стартового).
Рекомендация: При переходе на logging использовать structured extra: `logger.info("Prediction complete", extra={"station": station_id, "sensor": sensor_name, "rows": len(df)})`. Рассмотреть `python-json-logger` для JSON-формата.

**[MED-7] main.py: SQL injection в station_anomalies_db через f-string**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\main.py:534-535`
Проблема: `where = f"AND sensor_id = %(sid)s"` -- сам по себе безопасен (параметризован), но `f"FROM {schema}.anomalies"` использует f-string для schema. Schema приходит из config-файла (не от пользователя напрямую), но принцип "defence in depth" нарушен. Аналогичные f-string SQL в `_fetch_raw_db_series` (строка 469) и `station_pvsnapshot` (строки 631-636).
Рекомендация: Использовать `pgsql.Identifier(schema)` вместо f-string для имён таблиц/схем, как это уже сделано в `data_loader.py` и `_count_regime_transitions`.

---

### LOW

**[LOW-1] train_and_save_models.py: print() вместо logger**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\train_and_save_models.py` (20+ print-вызовов)
Проблема: Скрипт обучения -- offline-задача, менее критично, но при запуске по cron логи также теряются.
Рекомендация: Перевести на logging вместе с live_predict.

**[LOW-2] ensure_indexes.py: смешанный стиль (print + logger)**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\ensure_indexes.py:57,64`
Проблема: Использует и `logger.exception()` (строка 57), и `print()` (строка 64).
Рекомендация: Заменить print на logger.info.

**[LOW-3] Frontend: нет console.log в src/ -- чисто, но нет и error tracking**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\src/` (все файлы)
Проблема: В src/ нет ни одного console.log (хорошо для production), но также нет никакого error tracking (Sentry, LogRocket и т.д.) для отлова JS-ошибок в production.
Рекомендация: Рассмотреть добавление Sentry или аналога для промышленной системы 24/7.

**[LOW-4] Нет тестов в проекте -- ноль**
Файл: Проект в целом (отсутствуют директории `tests/`, `__tests__/`, файлы `*.test.*`, `*.spec.*`)
Проблема: Не найдено ни одного тестового файла (ни pytest, ни vitest/jest). Нет ни unit, ни integration, ни smoke-тестов. Для промышленной системы с ML-пайплайном и множеством edge-cases -- серьёзный пробел.
Рекомендация: Начать с smoke-теста для `/api/health` и unit-теста для `predict_sensor()`. Добавить pytest.ini и tests/.

**[LOW-5] data_loader.py: inserted count не точен при ON CONFLICT DO NOTHING**
Файл: `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\data_loader.py:206`
Проблема: `inserted += len(chunk)` считает все строки в chunk, включая те, что были отклонены `ON CONFLICT DO NOTHING`. Реальное число вставленных строк может быть меньше.
Рекомендация: Использовать `cur.rowcount` после execute_batch для точного подсчёта.

---

### Положительные наблюдения

1. **Атомарная запись state-файла** (`live_predict.py:2612-2615`): запись через `.tmp` + `os.replace()` -- правильный подход, исключает partial write.
2. **MAX_HISTORY_DAYS = 30** (`live_predict.py:45`): ограничение загрузки данных для предотвращения MemoryError -- хорошая реакция на реальный инцидент.
3. **Connection pooling** (`station_config.py:107-128`): ThreadedConnectionPool с connect_timeout=5 -- правильно для API.
4. **Freshness-компонент** (`Freshness.tsx`): существует индикатор свежести данных с 3-уровневой цветовой шкалой (ok/warn/stale) -- хорошая основа.
5. **Path traversal protection** (`station_config.py:69-76`): regex + resolve().is_relative_to() -- defence-in-depth.
6. **Parameterized SQL в data_loader.py** и большинстве main.py -- правильное использование pgsql.Identifier и параметризованных запросов.
7. **SchemaPanel** загрузка через fetch с AbortController -- правильный lifecycle management.

---

### ТОП-5 исправлений по приоритету

| # | Что | Трудозатраты | Влияние |
|---|-----|-------------|---------|
| 1 | **Logging-инфраструктура**: создать `logging_config.py` с RotatingFileHandler, заменить print->logger в live_predict.py и train_and_save_models.py | 4-6 часов | Закрывает CRIT-1, MED-1, MED-6, LOW-1, LOW-2 |
| 2 | **try/except + restart в run_continuous()**: обернуть run_once() в try/except с backoff; создать NSSM-сервис или .bat + Task Scheduler с auto-restart | 2-3 часа | Закрывает CRIT-2, CRIT-3 |
| 3 | **Починить /api/health**: добавить staleness-проверку state-файла + DB ping; возвращать 503 при degraded | 1-2 часа | Закрывает CRIT-4, MED-2 |
| 4 | **PID-файл + file-lock в live_predict**: предотвратить двойной запуск; добавить signal handler для graceful shutdown | 2-3 часа | Закрывает HIGH-3, HIGH-4 |
| 5 | **React ErrorBoundary + API error banner**: обёртка ErrorBoundary в main.tsx + глобальный onError для React Query | 1-2 часа | Закрывает HIGH-5, MED-3 |

**Общие трудозатраты на топ-5:** ~12-16 часов.

---

### Статистика: print vs logger

| Файл | print() вызовов | logger.* вызовов |
|------|----------------|-----------------|
| live_predict.py | 35+ | 0 |
| train_and_save_models.py | 20+ | 0 |
| sync_raw_data.py | 10+ | 0 |
| main.py | 0 | 5 |
| station_config.py | 0 | 1 (определение) |
| data_loader.py | 0 | 2 |
| ensure_indexes.py | 1 | 1 |
| **ИТОГО** | **~67** | **~9** |

И при этом 0 logging handlers сконфигурировано -- даже 9 logger-вызовов пропадают при запуске без uvicorn.

---

### Вердикт

**REQUEST CHANGES** -- 4 критических и 6 высокоприоритетных проблем. Система промышленного мониторинга 24/7 работает фактически вслепую: при падении процесса -- нет логов, нет рестарта, нет алерта. Health endpoint не выполняет свою функцию. Frontend не защищён от JS-крашей. Все описанные инциденты (3-недельный мёртвый live_predict, невидимая MemoryError, зомби-процессы, двойные экземпляры) являются прямым следствием этих пробелов. Топ-5 исправлений закроют ~80% проблем за ~2 рабочих дня.