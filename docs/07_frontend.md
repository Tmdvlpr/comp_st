# 07. Фронтенд

SPA-дашборд оператора. React 19 + Vite 8 + TypeScript + Plotly + TanStack Query + Tailwind. Каталог `frontend/`.

## 7.1 Запуск и сборка
- `npm run dev` — Vite dev-сервер.
- `npm run build` — `tsc -b && vite build`.
- `npm run preview` / `npm run lint`.
- Базовый URL API: `import.meta.env.VITE_API_URL ?? ''` (пусто → тот же origin / прокси Vite).

## 7.2 Загрузка данных
`src/api/client.ts` — тонкая обёртка над `fetch` (`api.stations/sensors/sensor/sensorChart/stats/events/heatmap`). Все запросы — через TanStack Query с `refetchInterval`:
- `stations` — 60 с; `sensors/stats/events/heatmap/chart` — 30 с (`REFETCH_MS`).
- График: `placeholderData: keepPreviousData` (плавная смена), исторические окна кешируются (`staleTime: Infinity`).

Поллинг 30 с — основной механизм «реального времени» на фронте (бэкенд обновляет state каждые 5 мин).

## 7.3 Слои графика (App.tsx)
- **Обзор** — пресет `chartDays` (1д…30д), живые данные.
- **Кастомный период** — `DatePicker` (`t0`/`t1`), серверный запрос диапазона; история (конец > 1 ч назад) кешируется навсегда.
- **Зум** — окно Plotly (`onRangeChange` → `zoomWindow`), сервер отдаёт мелкий бакет для выбранного интервала.

## 7.4 Состояние UI
`useState` + `localStorage`: тема (`cs-theme`), сворачивание сайдбара (`cs-sidebar`), квитированные события (`cs-acked`), активная станция (`cs-station`). Представления (`activeView`): `monitor | schema | engine`. Есть флаг `gpaOverlay` (наложение того же датчика по разным ГПА — текущая, ограниченная версия оверлея).

## 7.5 Компоненты (`src/components/`)

| Компонент | Назначение |
|-----------|-----------|
| `Sidebar` | Список датчиков, выбор, фильтры, бейджи качества модели |
| `Chart/SensorChart` | График Plotly: факт + модель + коридор + маркеры аномалий; зум/пан, разделитель окна; проп `overlaySeries` (оверлей по ГПА) |
| `HeatMap` | Тепловая карта датчик×ГПА по severity |
| `Stats/StatsGrid` | KPI-плашки (счётчики по типам/severity) |
| `EventDrawer` | Журнал событий (выезжающая панель), квитирование |
| `Ticker` | Бегущая строка критичных событий |
| `PriorityBanner` | Баннер приоритетной аномалии |
| `StationSwitcher` | Переключение станций |
| `SchemaPanel` | Мнемосхема (использует `/pvsnapshot`) |
| `EnginePanel` | Панель «движка»/состояния ML |
| `DatePicker` | Выбор кастомного периода |
| `Freshness` | Индикатор свежести данных (возраст state) |
| `KioskMode` | Полноэкранный режим для дежурного монитора |
| `Report/ShiftReport` | Сменный отчёт |
| `ApiErrorBanner` + `api/errorStore` | Глобальные ошибки API |
| `ErrorBoundary` | Перехват ошибок рендера |
| `Landing` | Входная страница |

## 7.6 Типы (`src/types/index.ts`)
Зеркалят контракт API: `SensorMeta`, `EventItem`, `StatsResponse`, `TimeSeriesPoint{t,v,p,lo,hi}`, `AnomalyPoint`, `SensorChartResponse`, `HeatmapCell`, `StationInfo`. Справочники: `KIND_LABEL`, `KIND_SEVERITY`, `SEV_COLOR`.
`SensorChartResponse` несёт `r2`/`mae` (mae сейчас 0 — см. [06](06_anomaly_detection.md)).

## 7.7 Производительность графиков (текущее состояние)
- Plotly грузится лениво (`plotly.js-basic-dist-min`), обновления через `Plotly.react`, `purge` только при смене датчика.
- Зум — `debounce` 300 мс; `uirevision` сохраняет зум при обновлении данных.
- Даунсемплинг — на сервере (≤1500 точек).

## 7.8 Планируемые доработки (план)
- **Фаза 8:** не рисовать прогноз на обучающем периоде (`connectgaps:false`, разделитель `train_ts`).
- **Фаза 9:** мульти-сенсорный график — произвольные датчики с разных ГПА на одном канвасе (новый эндпоинт `/chart/multi`, нормализация/мультиось, пикер датчиков).
- **Фаза 10:** ускорение (батч-запрос для мульти, кеш TanStack Query по ключу `(sensors,days,t0,t1)`, опц. `scattergl`).
- **Фаза 11:** UI журнала уведомлений и здоровья датчика.
