# 04. Справочник REST API

FastAPI-приложение `main.py`. Базовый порт `:8000`. Все ответы — JSON. CORS: только `http://localhost:<port>`.

Канонический префикс — `/api/stations/{station_id}/...`. Есть **compat-алиасы** без `station_id` (делегируют на станцию по умолчанию `ohangaron`).

Время в ответах: серии/события — naive ISO в зоне `Etc/GMT-5` (локальное время станции). `created_at`/`event_ts` в `/anomalies` — как в БД (`TIMESTAMPTZ`, текстом).

---

## Станции

### GET /api/stations
Список станций. → `StationInfo[]`.

```jsonc
[{ "id":"ohangaron","display_name":"Охангаронская КС","enabled":true,
   "units":["GPA1","GPA2","GPA3"],"live_data":true,"last_updated":"2026-06-15T12:30:00" }]
```

---

## Датчики

### GET /api/stations/{station_id}/sensors
Параметры: `gpa` (опц., напр. `GPA1`). → `SensorMeta[]`. `Cache-Control: max-age=25`.

### GET /api/stations/{station_id}/sensors/{sensor_id}
Один датчик. → `SensorMeta`. 404 если не найден.

**SensorMeta:**
```jsonc
{ "id":"gas_pressure_out_gpa__GPA1","name":"gas_pressure_out_gpa","gpa":"GPA1",
  "tag":"GPA-1.GPA-1.PD.PV","r2":0.97,"mae":0.0,"cur":58.2,
  "anomaly_count":3,"anomaly_count_30d":11,"anomaly_types":["ml","roc"],
  "severity":"crit","subsystem":"GAS" }
```
> `mae` сейчас всегда `0.0` (метрика не заполняется обучением) — исправляется планом (фаза 5).

---

## Статистика

### GET /api/stations/{station_id}/stats
→ `StatsResponse`. `Cache-Control: max-age=25`. Считает датчики по severity и события по типам; при отсутствии режимных событий добивает `regime_count` подсчётом переходов `STATES_GTD.5` за 7 дней.

```jsonc
{ "total_sensors":84,"crit_count":2,"warn_count":5,"info_count":7,"ok_count":70,
  "ml_count":3,"frozen_count":1,"neg_count":0,"regime_count":4,"roc_count":6,
  "seasonal_count":2,"cross_count":1,"total_anomalies":17,"last_updated":"2026-06-15T12:30:00" }
```

---

## События (из state.json)

### GET /api/stations/{station_id}/events
Параметры: `severity`, `gpa`, `kind`, `limit`, `days` (все опц.). → `EventItem[]`. `Cache-Control: max-age=25`.

**EventItem:**
```jsonc
{ "id":"gas_pressure_out_gpa__GPA1__ml__2026-06-15T11:05:00","timestamp":"2026-06-15T11:05:00",
  "ts_end":"2026-06-15T11:20:00","points":4,"sensor_id":"...","sensor_name":"gas_pressure_out_gpa",
  "gpa":"GPA1","kind":"ml","severity":"crit","value":61.0,"deviation":5.2,
  "description":"gas pressure out gpa: ml ×4","acked":false }
```

---

## График датчика

### GET /api/stations/{station_id}/sensors/{sensor_id}/chart
Параметры: `days` (≥0, 0 = по умолчанию 30) **или** `t0`/`t1` (ISO; naive трактуется как `Etc/GMT-5`). → `SensorChartResponse`.

- Даунсемплинг в БД: адаптивный бакет, цель ≤1500 точек. `Cache-Control`: исторические окна (конец > 1 ч назад) — `max-age=3600`, иначе `25`.
- `series[].p/lo/hi` — прогноз и коридор из `state.json`. *(Планом, фаза 8: на обучающем периоде `p/lo/hi=null` + поле `train_ts`.)*

**SensorChartResponse:**
```jsonc
{ "sensor_id":"...","tag":"GPA-1.GPA-1.PD.PV","r2":0.97,"mae":0.0,
  "current":58.2,"predicted":57.9,"deviation":0.5,
  "series":[{ "t":"2026-06-15T11:05:00","v":58.2,"p":57.9,"lo":55.1,"hi":60.7 }],
  "anomalies":[{ "t":"2026-06-15T11:05:00","v":61.0,"kind":"ml","severity":"crit" }] }
```

---

## Тепловая карта

### GET /api/stations/{station_id}/heatmap
Параметры: `gpa` (опц.). → `HeatmapCell[]` (`sensor_id, name, gpa, severity, anomaly_count`). `Cache-Control: max-age=25`.

---

## Аномалии (из БД)

### GET /api/stations/{station_id}/anomalies
Параметры: `limit` (≤1000, default 200), `sensor_id` (опц.). → `AnomalyRecord[]`, упорядочены по `event_ts DESC`. 503 при недоступности БД.

**AnomalyRecord:**
```jsonc
{ "id":12345,"sensor_id":"...","event_ts":"2026-06-15 11:05:00+05","anomaly_type":1,
  "severity":"crit","value":61.0,"deviation":5.2,"created_at":"2026-06-15 11:06:01+05" }
```
`anomaly_type` — код из словаря (1=ml…7=cross), см. [06](06_anomaly_detection.md).

---

## Мнемосхема (срез значений)

### GET /api/stations/{station_id}/pvsnapshot
Последний срез `tag→{v, sev}` за 24 ч из `raw_data` (для интерактивной схемы). Синтезирует биты `STATES` из оборотов, если их нет; реплицирует станционные теги (`GC_BFTG`/`GC_FG7001`/`GC_UPTG`) между ГПА. → `{ "tags": {<tag>:{v,sev}}, "ts": "<last_updated>" }`. 503 при ошибке БД.

---

## Здоровье системы

### GET /api/health
Сводный статус. HTTP 200 при `ok`; **503** при `degraded`/`down`.

```jsonc
{ "status":"ok|degraded|down","timestamp":1718450000.0,
  "stations":{ "ohangaron":{ "live_data":true,"last_updated":"...","state_age_seconds":42,
                              "model_drift":{ "count":0,"sensors":[],"retrain_recommended":false } } },
  "db":"ok|error","state_age_seconds":42,"ml_engine":"ok|stale|down" }
```
Пороги: state старше 15 мин → `degraded`/`stale`; старше 60 мин → `down`. БД недоступна → `degraded`.

---

## Compat-алиасы (→ ohangaron)

`GET /api/sensors[?gpa]` · `GET /api/sensors/{sensor_id}` · `GET /api/sensors/{sensor_id}/chart[?days]` · `GET /api/stats` · `GET /api/events[...]` · `GET /api/heatmap[?gpa]` · `GET /api/pvsnapshot`.

> Технодолг (A14): фронт постепенно переводить на канонический префикс.

---

## Планируемые эндпоинты (план доработок)

| Метод | Путь | Назначение | Фаза |
|-------|------|-----------|------|
| GET | `/api/stations/{id}/notifications` | Журнал уведомлений (фильтры `status/severity/sensor_id/limit/days`) | 11 |
| POST | `/api/stations/{id}/notifications/{nid}/ack` | Квитирование уведомления | 11 |
| GET | `/api/stations/{id}/sensors/{sensor_id}/health` | Агрегат здоровья датчика (`health`/`anomalies`) | 11 |
| GET | `/api/stations/{id}/chart/multi?sensors=a,b,c` | Несколько датчиков на один график (только факт, ≤1500 точек/серию) | 9 |

В `SensorChartResponse` добавляется `train_ts` (граница мониторинга) — фаза 8.
