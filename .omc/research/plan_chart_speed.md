У меня достаточно контекста. Ниже полный план.

---

# План ускорения графиков (FastAPI + PostgreSQL raw_data + React/Plotly)

Цель: любой зум/кастомный период отображается < 400 мс perceived (Doherty), ответ chart-endpoint ≤ ~100 КБ и ≤ 1500 точек, без полного рефетча при зуме.

Корневые причины задержек (в порядке вклада):
1. Seq scan по raw_data (миллионы строк) на каждый chart-запрос — нет индекса (point, datetime DESC).
2. Передача и рендер ~8500 точек × (v,p,lo,hi) ≈ 670 КБ на каждый запрос, каждые 30 с.
3. Кастомный период и зум не сужают запрос: клиент качает полные 30д и фильтрует/удваивает days.

---

## А. Индексы (эффект максимальный, трудозатраты минимальные) — ПРИОРИТЕТ 1

**Файлы:** `c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\ensure_indexes.py` (готов, идемпотентен), гейт `CS_ENSURE_INDEXES` в `backend\main.py`.

**Суть:** скрипт уже создаёт ровно нужные индексы:
- `idx_raw_data_point_dt ON raw_data (point, datetime DESC)` — закрывает и chart (`WHERE point=… AND datetime>=…`), и pvsnapshot (DISTINCT ON).
- `idx_anomalies_sensor_ts (sensor_id, event_ts DESC)` и `idx_anomalies_ts (event_ts DESC)` — закрывают SQL по аномалиям в chart-endpoint и журнал событий.

**Действие (ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ ПОЛЬЗОВАТЕЛЯ — DDL на проде):**
- Вариант 1 (предпочтительный, ручной): `python backend\ensure_indexes.py ohangaron` в окно низкой нагрузки.
- Вариант 2: выставить `CS_ENSURE_INDEXES=1` для разового запуска при старте.
- Важно: `CREATE INDEX IF NOT EXISTS` без `CONCURRENTLY` берёт SHARE-lock на таблицу — на таблице с активной записью (5-мин инжест) построение на миллионах строк заблокирует INSERT на время билда (десятки секунд — минуты). Рекомендация в плане: предложить пользователю либо согласиться на короткое окно, либо однократно вручную выполнить `CREATE INDEX CONCURRENTLY idx_raw_data_point_dt ON <schema>.<table> (point, datetime DESC);` (нельзя в транзакции; ensure_indexes коммитит после каждого стейтмента, так что можно расширить скрипт, но проще one-off psql).

**Ожидаемый эффект:** seq scan (секунды) → index scan ~8.5К строк одного тега (десятки мс). Это 80% выигрыша всего плана.

**Риски:** lock при билде (см. выше); рост размера БД (~один btree на point+dt); незначительное замедление вставки.

**Проверка:**
- `EXPLAIN (ANALYZE, BUFFERS) SELECT datetime, value FROM <schema>.<table> WHERE point='<tag>' AND datetime >= NOW()-make_interval(days=>30) ORDER BY datetime LIMIT 50000;` — до: Seq Scan; после: Index Scan using idx_raw_data_point_dt.
- `curl -s -o NUL -w "%{time_total}\n" "http://localhost:8000/api/stations/ohangaron/sensors/<id>/chart?days=30"` до/после.

---

## Б. Серверный диапазон t0/t1 + SQL-даунсемплинг — ПРИОРИТЕТ 2

**Файлы/функции:** `backend\main.py` — `station_sensor_chart` (строка 439), `_fetch_raw_db_series` (384); `frontend\src\api\client.ts` — `sensorChart`.

**1) Параметры endpoint:**
```python
def station_sensor_chart(station_id, sensor_id,
    days: int = Query(0, ge=0),
    t0: Optional[str] = Query(None),   # ISO 8601
    t1: Optional[str] = Query(None)):
```
Обратная совместимость: если t0/t1 не заданы — как сейчас `effective_days = days or 30`, t0 = NOW()-days, t1 = NOW() (вычислить на стороне БД или передать `make_interval` как сейчас). Если задан только t0 — t1 = NOW(). Валидация: t0 < t1, парсинг через `datetime.fromisoformat`, 422 при мусоре. Учесть таймзону: фронт сейчас получает время в Etc/GMT-5 naive — t0/t1 от клиента трактовать в той же зоне и конвертировать в UTC перед сравнением с datetime-колонкой (проверить тип колонки timestamptz/timestamp при реализации).

**2) Адаптивный bucket:**
```
range_s = (t1 - t0).total_seconds()
bucket_s = max(300, math.ceil(range_s / 1500 / 300) * 300)   # кратно 5 мин
```
(кратность 300с важна, чтобы ключи бакетов при bucket=300 совпадали с 5-мин сеткой state).

**3) SQL-агрегация вместо pandas-ресемпла** (новая ветка в `_fetch_raw_db_series` или новая функция `_fetch_raw_db_series_bucketed`):
```sql
SELECT to_timestamp(floor(extract(epoch FROM datetime) / %(bucket)s) * %(bucket)s) AS ts,
       avg(value) AS v
FROM schema.table
WHERE point = %(tag)s AND datetime >= %(t0)s AND datetime < %(t1)s
GROUP BY 1 ORDER BY 1
```
(`date_bin('%s seconds', datetime, '2000-01-03')` — если PG ≥ 14; `to_timestamp(floor(...))` универсален). Index Scan по (point, datetime) + GroupAggregate — данные ужимаются в БД, по сети уходит ≤1500 строк. pandas-этап round/dedup при bucket-агрегации не нужен; оставить только tz-конверсию в Etc/GMT-5 и форматирование. LIMIT 50000 как страховка можно оставить.

**4) Мерж p/lo/hi из state при bucket > 300с:** state-серия 5-минутная по ключу минуты — при крупном бакете ключи не совпадут. Ресемплить state-серию тем же bucket в памяти (она уже в процессе, ~8.5К точек — дёшево):
```python
# группировка по floor(epoch(t)/bucket): p=avg, lo=min, hi=max
```
min(lo)/max(hi) сохраняет коридор консервативно (не сужает), avg(p) — гладкая модель. При bucket == 300 — текущая логика `t[:16]` без изменений.

**5) Аномалии:** существующий SQL по диапазону уже подходит — подставлять t0/t1; маркер аномалии биндить к ближайшему бакету (`floor(epoch/bucket)` тот же ключ), а value аномалии брать из строки anomalies (он там есть), чтобы маркер не «висел» мимо усреднённой линии.

**6) client.ts:**
```ts
sensorChart: (id, opts: { days?: number; t0?: string; t1?: string }, stationId)
```

**Риски:** avg сглаживает пики — на больших масштабах выбросы визуально гаснут (аномалии остаются видимы маркерами из таблицы anomalies, это компенсирует); смещение сетки бакетов против 5-мин меток; tz-ошибки (главный источник багов — сверить с текущей конверсией `tz_convert("Etc/GMT-5")`).
Опционально для сохранения пиков: добавить `min(value), max(value)` в SELECT и рисовать тонкую полосу min-max (можно отложить).

**Проверка:** `curl "…/chart?t0=2026-05-13T00:00:00&t1=2026-06-12T00:00:00" | jq '.series | length'` → ≤1500; размер ответа `-w "%{size_download}"` → ~100-130 КБ vs 670 КБ; EXPLAIN ANALYZE bucket-запроса.

---

## В. HTTP / react-query кеширование — ПРИОРИТЕТ 3

**Файлы:** `backend\main.py` (chart endpoint — добавить Response и заголовок, по образцу существующих max-age=25), `frontend\src\App.tsx:101-107`.

**Backend:** диапазон «исторический», если `t1 < NOW() - 1h` (данные иммутабельны, хвост ~10 мин не задет с запасом):
- исторический: `Cache-Control: public, max-age=3600, immutable` (плюс можно `stale-while-revalidate=86400`);
- живой (t1 отсутствует или близок к NOW): `Cache-Control: public, max-age=25` как у остальных.
Условие считать на сервере по тем же распарсенным t0/t1.

**Frontend (App.tsx):**
- queryKey: `['chart', station, sensorId, t0, t1, bucket?]` — диапазон обязан быть в ключе (сейчас только chartDays).
- Для канонизации ключей округлять t0/t1 кастомного диапазона до границ дня/часа — иначе кеш не переиспользуется.
- Исторические ключи (t1 в прошлом >1ч): `staleTime: Infinity`, `refetchInterval: false`, `gcTime: 30-60 мин`. Живые: текущие staleTime 10с / refetchInterval 30с.
- `keepPreviousData` оставить — он и даёт «мгновенную» смену масштаба поверх кеша.
- Кастомный диапазон DatePicker: вместо клиентской фильтрации `displayChartData` — отдельный query с t0/t1 (фильтрацию-useMemo оставить как fallback на время миграции, потом убрать).

**Риски:** залипание кеша при бэкфиле исторических данных (если raw_data когда-либо дописывают задним числом — у нас append-only, риск низкий; страховка — max-age=3600, а не сутки); прокси/браузер кеширует по полному URL — следить за стабильной сериализацией query string.

**Проверка:** второй `curl -w "%{time_total}"` того же исторического URL из браузера — from disk cache (DevTools Network); смена 30д→7д→30д в UI — без сетевых запросов (react-query cache).

---

## Г. Зум без полного рефетча (двухслойная стратегия) — ПРИОРИТЕТ 4

**Файлы:** `frontend\src\components\Chart\SensorChart.tsx` (relayout-handler, строки 201-208), `frontend\src\App.tsx` (`handleLoadMore`:218, chart query).

**Суть:**
1. **Обзорный слой**: всегда держим запрос «последние 30д (или выбранный пресет), ≤1500 точек» — он уже в react-query кеше, живой (refetch 30с).
2. **Детальный слой**: на `plotly_relayout` с `xaxis.range[0]/[1]` — debounce 250-300 мс (зум-драг шлёт серию событий) → setState `zoomWindow {t0,t1}` вверх через новый проп `onRangeChange` → отдельный useQuery `['chart', station, id, t0, t1]` (тот же endpoint Б, bucket автоматически мельче для узкого окна) с `enabled: !!zoomWindow`, `placeholderData: keepPreviousData`.
3. Пока детальный слой грузится — Plotly уже показывает зум обзорного слоя (нативный relayout мгновенный), детальные данные подменяют трейсы по приходу (`Plotly.react`). Никакого «белого экрана».
4. **Зум-аут / double-click (autorange)**: `ev['xaxis.autorange']` → сбросить zoomWindow → обзорный слой из кеша, сеть не нужна. Если новое окно целиком внутри уже закешированного при том же или меньшем bucket — react-query вернёт кеш (исторические ключи staleTime Infinity из В).
5. **Убрать текущий onLoadMore (chartDays*=2)**: заменить логику «зум к левому краю <8%» на тот же `onRangeChange` — пользователь утащил окно левее данных → запросить (t0_окна, t1_окна), а не удваивать весь период. `handleLoadMore` в App.tsx:218 удалить/переписать.
6. Канонизация zoomWindow: округлять t0/t1 до bucket-кратных границ (например до 5 мин/часа в зависимости от ширины) — резко повышает cache hit и ограничивает кардинальность ключей.

**Риски:** циклы relayout (программный `Plotly.relayout` в コде viewDays/focusTimestamp тоже триггерит событие — отличать по флагу/сравнению диапазона, иначе бесконечный цикл запрос→react→relayout→запрос); GPA-overlay (`overlayQueries`) — детальный слой для оверлеев либо не делать (оставить обзорный), либо те же ключи; гонка debounce vs быстрые последовательные зумы — react-query сам отменяет/игнорирует устаревшие через ключи.

**Проверка:** DevTools Network — зум внутрь = 1 запрос ~10-50 КБ после паузы 300 мс; зум-аут = 0 запросов; повторный зум в то же окно = 0 запросов.

---

## Д. Клиент-рендер — ПРИОРИТЕТ 5 (после Б почти всё решено)

**Файл:** `frontend\src\components\Chart\SensorChart.tsx`.

- Основное решает Б: 1500 точек вместо 8500 — Plotly.react на scatter (SVG) с заливкой коридора при 1500 точках укладывается в десятки мс.
- `line: { simplify: true }` — у Plotly включён по умолчанию для scatter; явно не отключать (проверить, что нигде не стоит `simplify: false`).
- Мемоизация трейсов: сейчас effect пересобирает traces на каждый рендер с зависимостями `[sensor?.id, chartData, theme, kindFilter, viewDays, overlaySeries]` — вынести построение массивов x/y и маркеров аномалий в `useMemo(..., [chartData, kindFilter])`, чтобы смена theme/viewDays не пересоздавала массивы данных (Plotly.react быстрее при референсно-стабильных массивах).
- Не трогать `plotly.js-basic-dist-min`/scattergl — при ≤1500 точках не нужно; переход на полный бандл (+1 МБ) не оправдан.
- Hover: при необходимости `hovermode: 'x'`/`hoverinfo` упрощение — микрооптимизация, делать только если профайлер покажет.

**Риски:** минимальные. **Проверка:** Performance-профиль в DevTools: время `Plotly.react` до/после; отсутствие лагов при drag-зуме.

---

## Е. Materialized view / downsample-таблица — НЕ ДЕЛАТЬ СЕЙЧАС

Оценка: **не нужна на текущих объёмах.** После А+Б запрос 90д = index scan ~26К строк одного тега + GROUP BY → единицы-десятки мс. MV `raw_data_1h (point, hour, avg, min, max)` + REFRESH по cron оправдан только если: (а) горизонты вырастут до года+, (б) EXPLAIN ANALYZE после А+Б покажет >200-300 мс на агрегации, (в) появятся мульти-теговые сводные графики. Зафиксировать как отложенный пункт с критерием активации; стоимость (cron, инвалидация, второй источник правды, расхождение хвоста) сейчас выше пользы.

---

## Порядок внедрения и приоритизация (эффект/трудозатраты)

| Шаг | Эффект | Трудозатраты | Заметка |
|---|---|---|---|
| А. Индексы | очень высокий | ~0 (скрипт готов) | требует подтверждения пользователя, окно для DDL |
| Б. t0/t1 + bucket-агрегация | высокий | средние (backend + client.ts) | основа для В и Г |
| В. Cache-Control + react-query ключи | средний | низкие | зависит от Б (ключи по t0/t1) |
| Г. Зум-окно вместо days*2 | высокий UX | средние (frontend) | зависит от Б |
| Д. Рендер-мемоизация | низкий | низкие | полировка |
| Е. MV | — | — | отложено, критерии выше |

Внедрять последовательно А → Б → В → Г → Д; после каждого шага — замер.

## Сквозная проверка (до/после каждого шага)

1. **БД:** `EXPLAIN (ANALYZE, BUFFERS)` обоих SQL (raw-выборка и bucket-агрегация) для 1/30/90д.
2. **API latency:** `curl -s -o NUL -w "code=%{http_code} t=%{time_total}s size=%{size_download}\n" "http://localhost:8000/api/stations/ohangaron/sensors/<id>/chart?days=30"` — повторить 5 раз (тёплый кеш ОС), то же для `?t0=&t1=` и узкого окна. Целевые: 30д < 150 мс, размер < 150 КБ.
3. **Frontend:** DevTools Network при сценарии «открыть датчик → зум ×3 → зум-аут → DatePicker неделя месяц назад → вернуться»: число запросов, размеры, время; Performance-профиль рендера Plotly.
4. **Регрессия:** старый клиент с `?days=N` продолжает работать (совместимость Б); значения линии при bucket=5мин побитово совпадают с текущими; аномалии-маркеры на местах; коридор lo/hi не сужается при крупном bucket.

### Critical Files for Implementation
- c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\main.py (station_sensor_chart:439, _fetch_raw_db_series:384)
- c:\Users\Timur\Desktop\UTG\КС\cs_4\backend\ensure_indexes.py
- c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\src\App.tsx (chart query:101, displayChartData:109, handleLoadMore:218)
- c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\src\components\Chart\SensorChart.tsx (plotly_relayout:201)
- c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\src\api\client.ts (sensorChart:33)