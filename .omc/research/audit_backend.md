

I now have a thorough understanding of the entire codebase. Let me compile the comprehensive audit report.

## Полный аудит backend-кода: Мониторинг КС (cs_4)

**Файлы проверены:** 8 (main.py, live_predict.py, station_config.py, data_loader.py, anomaly_types.py, ohangaron.yaml, .env, sync_raw_data.py, fix_anomalies_table.py, fetch_deals.py, train_and_save_models.py)

---

### Сводка по severity

| Severity | Кол-во |
|----------|--------|
| CRITICAL | 3 |
| HIGH | 8 |
| MEDIUM | 11 |
| LOW | 6 |
| **Итого** | **28** |

---

### CRITICAL (нужно исправить немедленно)

**[C1] SQL-инъекция через f-string интерполяцию идентификаторов БД**
Файлы: `main.py:261-271`, `main.py:371-375`, `main.py:435-441`, `main.py:497-508`, `main.py:597-602`, `live_predict.py:119-133`, `live_predict.py:153-158`, `data_loader.py:151-163`, `data_loader.py:180-187`

Имена schema, table, column подставляются через f-string напрямую в SQL. Хотя значения приходят из YAML-конфига (а не от пользователя HTTP-запроса), это опасно по нескольким причинам: (1) если YAML-конфиг будет скомпрометирован или ошибочно отредактирован -- прямой путь к SQL injection; (2) это анти-паттерн, который легко копировать в новый код, где источник может быть менее доверенным; (3) спецсимволы в именах схем/таблиц (дефис, пробел) вызовут синтаксическую ошибку SQL или непредсказуемое поведение.

Примеры:
```python
# main.py:261
sql = f"""
    WITH ordered AS (
        SELECT {dt_col}, {pt_col}, {val_col},
            LAG({val_col}) OVER (PARTITION BY {pt_col} ORDER BY {dt_col}) AS prev_val
        FROM {schema}.{table}
        ...
```
```python
# live_predict.py:119-124
query = f"""
    SELECT {dt_col}, {pt_col}, {val_col}
    FROM {schema}.{table}
    WHERE {dt_col} > %s
    ORDER BY {dt_col};
"""
```

**Рекомендация:** Использовать `psycopg2.sql.Identifier` для всех идентификаторов БД. В `data_loader.py` это уже частично сделано (методы `fetch_training_data`, `fetch_live_data`, `build_tag_mapping` правильно используют `pgsql.Identifier`), но `ensure_anomalies_table` и `save_anomalies` -- нет. В `main.py` и `live_predict.py` нигде не используется. Нужно унифицировать подход из data_loader по всему коду.

---

**[C2] Пароль БД в открытом виде в .env, закоммиченном в проект**
Файл: `.env:5`

```
CS_DB_PASSWORD=id6838
```

Файл `.env` лежит в рабочей директории и содержит пароль к production БД PostgreSQL на `10.1.30.164`. Если этот файл попадет в git (а `.gitignore` не обнаружен в проекте -- проект не является git-репозиторием) -- это прямая утечка credential.

**Рекомендация:** (1) Добавить `.env` в `.gitignore`. (2) Создать `.env.example` с пустыми значениями. (3) Рассмотреть использование более сильного пароля. (4) В идеале -- vault или environment-level secrets management.

---

**[C3] CORS: `allow_origins=["null"]` открывает доступ для любого `file://` контекста**
Файл: `main.py:31-36`

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],                    # file:// → Origin: null
    allow_origin_regex=r"http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Origin `"null"` -- это строковый литерал, который отправляют браузеры для `file://`, `data:`, и `sandboxed` iframe. Разрешение этого origin означает, что любой HTML-файл, открытый с локальной файловой системы на любой машине в сети, имеет полный доступ к API. В сочетании с тем, что API управляет мониторингом промышленной КС -- это серьезная поверхность атаки.

**Рекомендация:** Если фронтенд подается из HTML-файлов -- поднять его за HTTP-сервером (FastAPI static files или nginx) и указать конкретный origin. Убрать `"null"` из allowed origins. Если нужен dev-доступ -- использовать отдельный профиль для разработки.

---

### HIGH (нужно исправить до продакшена)

**[H1] Синхронные DB-запросы в async-фреймворке FastAPI блокируют event loop**
Файлы: `main.py:273-280`, `main.py:377-381`, `main.py:442-445`, `main.py:513-516`, `main.py:594-603`

Все endpoints в `main.py` являются `def` (не `async def`), что означает, что FastAPI запускает их в thread pool. Однако при `workers=4` (строка 667) и `ThreadedConnectionPool(maxconn=10)` каждый тяжелый DB-запрос (например, `pvsnapshot` с DISTINCT ON по 24 часам данных, или `chart` endpoint без LIMIT) блокирует один из рабочих потоков. При параллельных запросах от нескольких пользователей дашборда сервер легко saturates.

**Рекомендация:** (1) Перейти на `asyncpg` + `async def` endpoints, или (2) как минимум увеличить pool до 20+ и поставить отдельный `workers` для uvicorn, или (3) обернуть тяжелые запросы в `run_in_executor`.

---

**[H2] `_fetch_raw_db_series` без LIMIT может вернуть миллионы строк**
Файл: `main.py:356-396`, вызов на строке 415

```python
raw_pts = _fetch_raw_db_series(cfg_obj, info["tag"], days if days > 0 else None)
```

Когда `days=0` (по умолчанию), передается `None`, и `where_extra` остается пустым. Запрос `SELECT datetime, value FROM ohangaron.raw_data WHERE point = %(tag)s ORDER BY datetime` вернет ВСЕ данные по тегу за всю историю. При 5-минутных интервалах за год это ~105,000 строк на один тег, за несколько лет -- до миллионов. Затем строится полный DataFrame + join со state_map.

**Рекомендация:** Установить дефолтный `days=30` вместо 0 в endpoint, или всегда ограничивать запрос до `MAX_HISTORY_DAYS`. Добавить `LIMIT 50000` как safety net.

---

**[H3] `pvsnapshot` endpoint: DISTINCT ON без LIMIT по 24 часам всей таблицы**
Файл: `main.py:597-602`

```python
cur.execute(f"""
    SELECT DISTINCT ON ({pt_col}) {pt_col}, {val_col}
    FROM {schema}.{table}
    WHERE {dt_col} >= NOW() - INTERVAL '24 hours'
    ORDER BY {pt_col}, {dt_col} DESC
""")
```

`DISTINCT ON` с `ORDER BY` на large-table может быть дорогим без составного индекса `(point, datetime DESC)`. При ~300 тегах и 5-минутных интервалах за 24 часа это ~86,400 строк для сортировки. Без индекса -- sequential scan.

**Рекомендация:** (1) Создать индекс `CREATE INDEX idx_raw_data_point_dt ON ohangaron.raw_data (point, datetime DESC)`. (2) Добавить комментарий в код о зависимости от этого индекса.

---

**[H4] `_write_live_state` пишет ВСЕ аномалии в БД при каждом цикле (дедуп только ON CONFLICT)**
Файл: `live_predict.py:2471-2477`

```python
if state_events and _loader is not None:
    try:
        saved = _loader.save_anomalies(state_events)
```

`state_events` содержит до 500 событий. При каждом 5-минутном цикле все 500 отправляются INSERT-ом с `ON CONFLICT DO NOTHING`. Это значит ~500 INSERT-попыток каждые 5 минут, из которых подавляющее большинство -- дубли. Это создает unnecessary I/O на БД и WAL write amplification.

**Рекомендация:** Отслеживать уже сохраненные event_id в памяти и отправлять только новые. Или фильтровать по `state_events` -- отправлять только события из последнего 5-минутного среза.

---

**[H5] Рост памяти: `_accumulated_raw_df` растет бесконечно в `run_continuous`**
Файл: `live_predict.py:2175-2176`, `2206`

```python
_accumulated_raw_df = None
...
raw_df = pd.concat([existing_df, new_db_df], ignore_index=True)
```

В `run_continuous` режиме (строка 2488-2509) DataFrame `current_raw_df` растет каждые 5 минут: новые данные конкатенируются, но старые никогда не обрезаются. При ~300 тегах × 12 срезов/час × 24ч = ~86,400 строк/день. За месяц это ~2.5M строк. Первоначальная загрузка ограничена `MAX_HISTORY_DAYS=30`, но инкрементальное накопление не ограничено.

**Рекомендация:** После concat обрезать данные старше `MAX_HISTORY_DAYS`:
```python
cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=MAX_HISTORY_DAYS)
raw_df = raw_df[pd.to_datetime(raw_df['datetime'], utc=True) >= cutoff]
```

---

**[H6] Race condition при чтении/записи state JSON-файла**
Файлы: `live_predict.py:2481-2484` (запись), `main.py:61-64` (чтение)

Запись использует атомарный `os.replace(tmp, out)` -- это хорошо. Однако чтение в `main.py` использует mtime-based cache с не-atomic проверкой:

```python
mtime = state_path.stat().st_mtime  # строка 55
# ... window here where file can be replaced ...
with open(state_path, encoding="utf-8") as f:
    state = json.load(f)  # строка 62 -- может прочитать частично записанный файл?
```

При `workers=4` (uvicorn) несколько процессов могут одновременно читать файл. `os.replace` на Windows НЕ гарантирует атомарность при чтении другим процессом.

**Рекомендация:** (1) Читать из tmp-копии, или (2) добавить retry с json.JSONDecodeError, или (3) перейти на IPC через Redis/shared memory вместо файла.

---

**[H7] `fix_anomalies_table.py` использует `get_db_connection` как обычную функцию, а не context manager**
Файл: `fix_anomalies_table.py:32-42`

```python
conn = get_db_connection(cfg)
try:
    with conn.cursor() as cur:
        cur.execute(DROP_SQL)
```

`get_db_connection` -- это `@contextmanager`, который возвращает generator. Вызов `conn = get_db_connection(cfg)` без `with` не входит в контекст -- `conn` будет объект generator, а не connection. Этот скрипт просто не работает (упадет с `AttributeError: 'generator' object has no attribute 'cursor'`).

**Рекомендация:** Заменить на `with get_db_connection(cfg) as conn:`.

---

**[H8] `fetch_data_from_db` без `since_timestamp` загружает ВСЮ таблицу без LIMIT**
Файл: `live_predict.py:128-134`

```python
else:
    query = f"""
        SELECT {dt_col}, {pt_col}, {val_col}
        FROM {schema}.{table}
        ORDER BY {dt_col};
    """
    print(f"📡 Загрузка ВСЕХ данных...", flush=True)
    df = pd.read_sql_query(query, conn)
```

Хотя на строке 2194-2195 `run_once` вычисляет `since_ts` с учетом `MAX_HISTORY_DAYS`, этот fallback-путь (else branch) остается доступным и загрузит всю таблицу raw_data в память. Именно это вызывало MemoryError с 2.5M строк.

**Рекомендация:** Удалить else-ветку или добавить `WHERE {dt_col} >= NOW() - INTERVAL '{MAX_HISTORY_DAYS} days'` как минимальное ограничение.

---

### MEDIUM (желательно исправить)

**[M1] Молчаливое проглатывание ошибок в 13+ except-блоках**
Файлы: `main.py:65`, `main.py:84`, `main.py:232`, `main.py:456`, `live_predict.py:605,647,658,2396,2419,2449`, `station_config.py:137`

Паттерн:
```python
except Exception:
    return cached[0] if cached else {}
```

Ошибки (включая permission denied, disk full, corrupt JSON, network errors) молча проглатываются. В production-системе мониторинга КС -- это особенно опасно, так как оператор не увидит, что данные стухли.

**Рекомендация:** Как минимум добавить `logger.exception(...)` в каждый except-блок. Для критичных путей (чтение state, загрузка метаданных) -- пробрасывать ошибку или устанавливать health-флаг.

---

**[M2] `_state_cache` и `_meta_cache` не thread-safe**
Файл: `main.py:39-40`

```python
_state_cache: dict[str, tuple[dict, float]] = {}
_meta_cache:  dict[str, tuple[dict, float]] = {}
```

При `workers=4` uvicorn (строка 667) каждый worker -- отдельный процесс, так что кеши не шарятся (это ок). Но если запускать с `reload=True` или gunicorn threads -- возможна гонка при одновременной записи в dict из разных потоков. Dict operations в CPython thread-safe благодаря GIL, но порядок check-then-act (строки 56-57) не атомарный.

**Рекомендация:** Добавить `threading.Lock` по аналогии с `_pools_lock` в `station_config.py`, или использовать `functools.lru_cache`.

---

**[M3] Таймзоны: `live_predict.py` использует `pd.Timestamp.now()` (naive), а `main.py` -- `datetime.now()`**
Файлы: `live_predict.py:2464`, `main.py:343`

```python
# live_predict.py:2464
'last_updated': pd.Timestamp.now().isoformat(),

# main.py:343
cutoff = (datetime.now() - timedelta(days=days)).isoformat()
```

`Timestamp.now()` и `datetime.now()` без timezone возвращают local time. Данные из БД конвертируются в `Etc/GMT-5` (UTC+5). Сравнение naive-timestamp из `datetime.now()` (может быть UTC+5, UTC, или любой другой TZ в зависимости от настроек ОС) с ISO-строками из state events -- ненадежно.

**Рекомендация:** Везде использовать timezone-aware timestamps: `pd.Timestamp.now(tz='Etc/GMT-5')` и `datetime.now(timezone(timedelta(hours=5)))`.

---

**[M4] `station_config.py`: кеш конфигурации никогда не инвалидируется**
Файл: `station_config.py:65,71-72`

```python
_config_cache: dict[str, StationConfig] = {}
...
if station_id in _config_cache:
    return _config_cache[station_id]
```

Если YAML-конфиг изменился на диске (например, добавили новый GPA), изменения не подхватятся до перезапуска процесса.

**Рекомендация:** Добавить mtime-based инвалидацию (как для state-файла в main.py) или TTL.

---

**[M5] `data_loader.py`: `ensure_anomalies_table` и `save_anomalies` используют f-string для schema**
Файл: `data_loader.py:151-163`, `180-187`

```python
ddl = f"""
CREATE TABLE IF NOT EXISTS {schema}.anomalies (
```

Несоответствие: `fetch_training_data` и `fetch_live_data` в том же файле корректно используют `pgsql.Identifier`, а DDL и INSERT -- нет.

**Рекомендация:** Использовать `pgsql.Identifier` для всех SQL в этом файле.

---

**[M6] `live_predict.py` -- God Object: 2526 строк, смешивает ML, визуализацию, I/O, состояние**
Файл: `live_predict.py:1-2526`

Один файл содержит: загрузку моделей, SQL-запросы к БД, предикт для каждого датчика, детекцию 7 типов аномалий, кросс-ГПА анализ, детекцию смен режима, генерацию HTML-дашборда (~1600 строк встроенного HTML/CSS/JS), запись state JSON, и main loop. Цикломатическая сложность `build_dashboard` и `run_once` выходит далеко за 10.

**Рекомендация:** Разбить на модули: `anomaly_detector.py` (predict_sensor, кросс-ГПА, режимы), `dashboard_builder.py` (HTML), `state_writer.py` (JSON state), и оставить в `live_predict.py` только оркестрацию.

---

**[M7] `sync_raw_data.py` и `fetch_deals.py` не используют пул соединений**
Файлы: `sync_raw_data.py:59-61`, `fetch_deals.py:14`

`sync_raw_data.py` создает прямое `psycopg2.connect()` вместо `get_db_connection(cfg)`. `fetch_deals.py` хардкодит URL и Authorization header.

**Рекомендация:** Перевести `sync_raw_data.py` на `StationConfig` + `get_db_connection`. Для `fetch_deals.py` -- вынести auth token в .env.

---

**[M8] Нет индексов на `anomalies` кроме PK и UNIQUE constraint**
Файл: `data_loader.py:152-163`

```sql
CREATE TABLE IF NOT EXISTS {schema}.anomalies (
    ...
    CONSTRAINT anomalies_dedup
        UNIQUE (sensor_id, event_ts, anomaly_type)
);
```

Запросы в `main.py:501-508` фильтруют по `sensor_id` и сортируют по `event_ts DESC`. UNIQUE constraint на `(sensor_id, event_ts, anomaly_type)` не оптимален для `ORDER BY event_ts DESC LIMIT N`. Нужен отдельный индекс.

**Рекомендация:** Добавить `CREATE INDEX idx_anomalies_ts ON {schema}.anomalies (event_ts DESC)` и `CREATE INDEX idx_anomalies_sensor_ts ON {schema}.anomalies (sensor_id, event_ts DESC)`.

---

**[M9] `save_anomalies` в `data_loader.py:208` возвращает `inserted = len(chunk)`, а не реально вставленные строки**
Файл: `data_loader.py:206-208`

```python
psycopg2.extras.execute_batch(cur, insert_sql, chunk, page_size=500)
inserted += len(chunk)
```

При `ON CONFLICT DO NOTHING` часть строк отклоняется. `execute_batch` не возвращает количество реально вставленных строк. Значение `inserted` завышено.

**Рекомендация:** Использовать `cur.rowcount` после каждого batch, или `RETURNING id` + подсчет.

---

**[M10] HTTPException в `main.py:519` раскрывает внутреннюю ошибку БД клиенту**
Файл: `main.py:518-519`

```python
except Exception as e:
    raise HTTPException(status_code=503, detail=f"DB error: {e}")
```

Стектрейс PostgreSQL может содержать имена таблиц, схем, IP-адрес сервера.

**Рекомендация:** Логировать полную ошибку, но возвращать клиенту generic message: `detail="Database temporarily unavailable"`.

---

**[M11] Повторная загрузка конфига в `station_stats`: `_require_station` + `load_station_config`**
Файл: `main.py:285-288`

```python
def station_stats(station_id: str, response: Response = None):
    _require_station(station_id)  # loads config
    ...
    cfg_obj = load_station_config(station_id)  # loads config again
```

`_require_station` уже загружает и возвращает `StationConfig`, но возвращаемое значение игнорируется.

**Рекомендация:** `cfg_obj = _require_station(station_id)`.

---

### LOW (опционально)

**[L1] `warnings.filterwarnings('ignore')` в `live_predict.py:26` и `train_and_save_models.py:17`**
Подавляет все warnings, включая deprecation и runtime warnings от numpy/pandas.

**Рекомендация:** Подавлять только конкретные: `warnings.filterwarnings('ignore', category=FutureWarning)`.

---

**[L2] `sys.stdout` перезаписывается на Windows для кодировки UTF-8**
Файлы: `live_predict.py:22-24`, `train_and_save_models.py:19-21`, `sync_raw_data.py:9-11`

Дублирование в трех файлах.

**Рекомендация:** Вынести в общий `utils.py` или решить через `PYTHONIOENCODING=utf-8` в environment.

---

**[L3] `fetch_deals.py` хардкодит даты и не обрабатывает pagination errors**
Файл: `fetch_deals.py:17-18`

```python
DATE_FROM = "2025-01-01T00:00:01.815Z"
DATE_TO   = "2026-04-08T11:31:20.815Z"
```

**Рекомендация:** Принимать даты как CLI-аргументы.

---

**[L4] Магические числа разбросаны по `predict_sensor`**
Файл: `live_predict.py:256-268`

```python
if   r2 >= 0.95: min_abs_pct = 0.10
elif r2 >= 0.80: min_abs_pct = 0.20
...
hw = np.maximum(hw, y_abs_mean * 0.003)
```

**Рекомендация:** Вынести пороги в конфиг или dataclass с именованными константами.

---

**[L5] Backward-compat aliases в `main.py:537-570` дублируют сигнатуры**
Семь функций-оберток, которые просто вызывают station-specific endpoint с `DEFAULT_STATION`.

**Рекомендация:** Можно заменить на `app.router.add_api_route` loop или decorator.

---

**[L6] `_sensors_list` строит dict из state values каждый раз при вызове**
Файл: `main.py:184-206`

Вызывается в `station_sensors`, `station_sensor`, `station_stats`, `station_heatmap` -- до 4 раз за один рендер страницы.

**Рекомендация:** Закешировать результат вместе со state mtime.

---

### Положительные наблюдения

1. **Атомарная запись state-файла** (`live_predict.py:2481-2484`): использование `tmp + os.replace` -- правильный паттерн для предотвращения corrupt reads.

2. **Connection pool с double-checked locking** (`station_config.py:111-127`): грамотная реализация пула с thread safety.

3. **Defence-in-depth в `load_station_config`** (`station_config.py:69-76`): regex-валидация station_id + проверка path traversal + `yaml.safe_load`.

4. **Mtime-based кеширование** (`main.py:43-66`): эффективное решение для связки через файл -- не перечитывает JSON при каждом запросе.

5. **`MAX_HISTORY_DAYS` ограничение** (`live_predict.py:45`): правильная реакция на MemoryError -- предотвращает загрузку всей истории.

6. **DB-side `NOW()` для фильтрации** (`main.py:360-361`): правильное решение проблемы clock skew.

7. **Дедупликация аномалий через `UNIQUE CONSTRAINT`** (`data_loader.py:161-162`): надежная защита от дублей на уровне БД.

8. **Параметризованные запросы для user-supplied values** (`main.py:375,444`): `%(tag)s` и `%(sid)s` -- правильное использование параметров.

---

### ТОП-5 самых важных исправлений

| # | Severity | Описание | Файлы | Оценка трудозатрат |
|---|----------|----------|-------|-------------------|
| 1 | **CRITICAL** | SQL-инъекция: заменить все f-string подстановки идентификаторов на `psycopg2.sql.Identifier` | main.py, live_predict.py, data_loader.py | 2-3 часа |
| 2 | **CRITICAL** | Убрать `.env` из проекта, добавить `.gitignore`, усилить пароль | .env | 15 минут |
| 3 | **HIGH** | Добавить LIMIT/MAX_DAYS ко всем DB-запросам (chart, pvsnapshot, fetch_data без since) + создать индексы на `raw_data(point, datetime DESC)` и `anomalies(sensor_id, event_ts DESC)` | main.py, live_predict.py, DDL | 1-2 часа |
| 4 | **HIGH** | Ограничить рост `_accumulated_raw_df` в `run_continuous` -- trim по `MAX_HISTORY_DAYS` после каждого concat | live_predict.py | 30 минут |
| 5 | **HIGH** | Добавить logging во все except-блоки (13 мест) + не раскрывать DB errors клиенту | main.py, live_predict.py | 1 час |

---

### Вердикт

**REQUEST CHANGES** -- обнаружены 3 CRITICAL и 8 HIGH проблем. Система функционирует, но имеет серьезные риски безопасности (SQL injection поверхность, утечка credentials, избыточный CORS) и стабильности (OOM при неограниченных запросах, утечка памяти в continuous mode, молчаливое проглатывание ошибок в production мониторинге КС).