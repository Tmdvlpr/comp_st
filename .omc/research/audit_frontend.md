

I now have a comprehensive view of the entire frontend codebase. Let me compile the full audit report.

---

## Полный аудит frontend-кода КС-МОН

**Файлов проверено:** 17 (все .tsx/.ts/.css/.json в src/ + конфигурация)
**Всего находок:** 27

---

### По серьёзности

- **CRITICAL:** 2
- **HIGH:** 6
- **MEDIUM:** 12
- **LOW:** 7

---

### CRITICAL

**[CRIT-1] SchemaPanel: инъекция и исполнение произвольных скриптов из внешнего HTML (XSS)**
Файл: `src/components/Schema/SchemaPanel.tsx:35-43`

Компонент загружает `/schema.html`, вставляет его содержимое через `container.innerHTML = doc.body.innerHTML`, затем пересоздаёт все `<script>` элементы, которые исполняются в контексте основного приложения. Это классический DOM-based XSS. Если `/schema.html` будет подменён (MITM, компрометация сервера, ошибка деплоя) -- атакующий получает полный контроль над приложением: доступ к localStorage (acked-события, настройки), куки, DOM основного React-дерева.

Дополнительные риски сознательного подхода (без iframe):
- **Глобальные переменные:** скрипты schema.html пишут в `window.*` и могут перезаписать React, Plotly или пользовательские объекты.
- **Утечки таймеров:** если schema.html запускает `setInterval`/`setTimeout`, они продолжают работать после переключения на вкладку "Мониторинг" -- стили убираются (строка 58), но таймеры не очищаются. При повторном переключении назад скрипты не пересоздаются (guard `loaded` на строке 11), но старые интервалы всё ещё тикают, вызывая утечки памяти.
- **Конфликт стилей:** хотя стили добавляются/убираются из `document.head` при переключении, между моментом добавления и рендером возможно мерцание. Глобальные CSS-селекторы schema.html (body, div, span) будут перезаписывать стили основного приложения.
- **Повторная инициализация:** guard `if (!active || loaded)` предотвращает повторную загрузку, но не перезапуск скриптов -- если скрипты в schema.html привязываются к DOM-элементам, после toggle display:none/block привязки могут быть потеряны.

**Рекомендация:** Если iframe недопустим -- как минимум: (1) добавить Content-Security-Policy header для `/schema.html`; (2) при переключении на "Мониторинг" вызывать `clearInterval`/`clearTimeout` для всех таймеров, зарегистрированных скриптами schema.html (через monkey-patch `window.setInterval` до инъекции скриптов, собирая ID в массив для последующей очистки); (3) использовать Shadow DOM вместо обычного div для изоляции стилей; (4) добавить CSP nonce для скриптов.

---

**[CRIT-2] SchemaPanel: нет очистки таймеров schema.html при unmount**
Файл: `src/components/Schema/SchemaPanel.tsx:62-67`

Cleanup-эффект на строке 63 удаляет только стили (`stylesRef.current.forEach(s => s.parentNode?.removeChild(s))`), но не останавливает скрипты/таймеры, запущенные инжектированным HTML. Если schema.html содержит `setInterval` для обновления данных в реальном времени (что вероятно для интерактивной схемы КС), эти таймеры продолжат работать после удаления компонента, вызывая:
- Утечки памяти (ссылки на удалённые DOM-узлы)
- Ошибки в консоли (обращение к несуществующим элементам)
- Рост потребления CPU со временем

**Рекомендация:** Перед инъекцией скриптов перехватить `window.setInterval` и `window.setTimeout`, собрать все ID. В cleanup-эффекте вызвать `clearInterval`/`clearTimeout` для каждого. Пример:
```typescript
const intervalIds: number[] = []
const origSetInterval = window.setInterval
window.setInterval = ((...args: Parameters<typeof setInterval>) => {
  const id = origSetInterval(...args)
  intervalIds.push(id as unknown as number)
  return id
}) as typeof setInterval
// ... инъекция скриптов ...
// В cleanup:
return () => { intervalIds.forEach(clearInterval); window.setInterval = origSetInterval }
```

---

### HIGH

**[HIGH-1] Полный бандл Plotly.js (~3.5 МБ gzip ~1 МБ) загружается целиком**
Файл: `src/components/Chart/SensorChart.tsx:54-55`, `package.json:14`

Импортируется `plotly.js/dist/plotly` -- это полный бандл со всеми модулями (geo, 3d, mapbox, sankey и т.д.), тогда как используется только `scatter`. При этом `react-plotly.js` в зависимостях вообще не используется -- мёртвая зависимость.

**Рекомендация:** Заменить на кастомный бандл через `plotly.js-basic-dist-min` (~350 КБ gzip) или собрать собственный через `plotly.js/lib/core` + `require('plotly.js/lib/scatter')`. Удалить `react-plotly.js` и `@types/react-plotly.js` из package.json.

---

**[HIGH-2] Дублирование KIND_LABEL в 3 местах с расхождениями значений**
Файлы:
- `src/types/index.ts:84-92` -- каноническое определение, типизировано как `Record<AnomalyKind, string>`, значение `neg` = "Сбой физичности"
- `src/App.tsx:19-22` -- локальная копия, `Record<string, string>`, значение `neg` = "Физичность" (ОТЛИЧАЕТСЯ)
- `src/components/Chart/SensorChart.tsx:33-41` -- ещё одна копия, `Record<string, string>`, значение `neg` = "Сбой физичности"

Расхождение: в App.tsx `neg` = "Физичность", в types и SensorChart -- "Сбой физичности". Пользователь видит разные названия одной аномалии в разных местах интерфейса. Кроме того, в App.tsx отсутствует ключ `seasonal` из полного набора (в StatsGrid.tsx используется "Сезонная", а в types -- "Сезонная аномалия").

**Рекомендация:** Использовать единственный экспорт `KIND_LABEL` из `types/index.ts` во всех компонентах. EventDrawer уже делает это правильно (строка 3). Удалить локальные копии из App.tsx и SensorChart.tsx.

---

**[HIGH-3] Дублирование SEV_COLOR в 4 местах**
Файлы:
- `src/types/index.ts:104-109` -- каноническое определение
- `src/App.tsx:331` -- инлайн внутри render-функции (пересоздаётся при каждом рендере)
- `src/components/EventDrawer/EventDrawer.tsx:68-73` -- внутри компонента (пересоздаётся)
- `src/components/Ticker/Ticker.tsx:14-19` -- модульный уровень
- `src/components/Kiosk/KioskMode.tsx:29-34` -- модульный уровень

**Рекомендация:** Импортировать `SEV_COLOR` из `types/index.ts` повсюду.

---

**[HIGH-4] Жёстко зашитые GPA ID нарушают мультистанционность**
Файлы:
- `src/components/Kiosk/KioskMode.tsx:90` -- `const gpas = ['GPA1', 'GPA2', 'GPA3']`
- `src/components/Sidebar/Sidebar.tsx:24` -- `{ GPA1: true, GPA2: true, GPA3: true }`

Kiosk-режим всегда показывает ровно 3 ГПА с фиксированными именами, игнорируя реальные данные станции. Если станция имеет 2 или 4 ГПА (или другие имена), режим работает некорректно. Sidebar тоже предполагает ровно 3 GPA для начального состояния.

**Рекомендация:** В KioskMode вычислять список ГПА из `sensors` (уже передаётся в props): `const gpas = [...new Set(sensors.map(s => s.gpa))].sort()`. В Sidebar инициализировать `expandedGpas` как `{}` и считать отсутствующий ключ как `true` (что уже делается на строке 205: `expandedGpas[gpa] ?? true`).

---

**[HIGH-5] tsconfig: отсутствует `strict: true`**
Файл: `tsconfig.app.json`

Конфигурация TypeScript не включает `strict: true`. Это означает отключены `strictNullChecks`, `strictFunctionTypes`, `strictBindCallApply`, `noImplicitAny` и другие. Как следствие -- компилятор не ловит null/undefined ошибки, что для промышленного мониторинга критически важно.

**Рекомендация:** Добавить `"strict": true` в `compilerOptions` файла `tsconfig.app.json`. Исправить возникшие ошибки типов.

---

**[HIGH-6] KioskMode: пересчёт тяжёлых данных на каждый рендер без мемоизации**
Файл: `src/components/Kiosk/KioskMode.tsx:91-114`

Вычисление `gpaStats` (фильтрация событий по 3 ГПА, подсчёт severity, поиск rpm-датчиков) выполняется прямо в теле компонента без `useMemo`. При каждом рендере (а Clock обновляется каждую секунду -- строка 39) все эти вычисления повторяются. С 500 событиями и 95 датчиками это заметная нагрузка.

**Рекомендация:** Обернуть в `useMemo(() => ..., [events, sensors])`. Также вынести компонент `Clock` с его 1-секундным интервалом выше и не допускать, чтобы он вызывал ререндер всего KioskMode -- либо `memo(Clock)` уже есть (Clock отдельная функция), но её ререндер не затрагивает родителя. Проверить: если Clock вызывает `setNow` внутри себя, ререндерится только Clock. Это корректно. Однако gpaStats всё равно нужно мемоизировать на случай смены events/sensors.

---

### MEDIUM

**[MED-1] SensorChart: cleanup Plotly через Promise -- не гарантирован порядок**
Файл: `src/components/Chart/SensorChart.tsx:228-235`

Cleanup-эффект вызывает `getPlotly().then(Plotly => { Plotly.purge(el) })`. Если пользователь быстро переключает датчики, cleanup (purge) выполняется асинхронно, а новый `newPlot` может начаться до завершения purge. Это вызовет гонку: `newPlot` на уже очищенном или ещё не очищенном элементе.

**Рекомендация:** Кешировать Plotly-модуль в ref после первого resolve, чтобы последующие вызовы были синхронными. Или добавить флаг `cancelled` в cleanup.

---

**[MED-2] App.tsx: 9 useEffect и 360 строк в одном компоненте -- нарушение SRP**
Файл: `src/App.tsx` -- весь файл

Компонент App содержит: управление темой, sidebar, queries, clock, keyboard shortcuts, chart state, event focus, kiosk -- всё в одном месте. Это затрудняет тестирование и поддержку.

**Рекомендация:** Извлечь кастомные хуки: `useTheme()`, `useAckedEvents()`, `useKeyboardShortcuts()`, `useChartState()`. Это разделит логику без изменения архитектуры.

---

**[MED-3] EventDrawer: весь список events рендерится без виртуализации**
Файл: `src/components/EventDrawer/EventDrawer.tsx:237-346`

При 500 событиях создаётся 500 DOM-узлов с вложенной разметкой (severity bar, badges, кнопки). Это может вызывать задержку при открытии журнала.

**Рекомендация:** Использовать виртуализацию (`@tanstack/react-virtual` -- уже совместимо с react-query stack) или хотя бы ленивый рендеринг первых N элементов с кнопкой "показать ещё".

---

**[MED-4] HeatMap: O(N*M) поиск find() в каждой ячейке таблицы**
Файл: `src/components/HeatMap/HeatMap.tsx:101`

В строке `const cell = byGpa[gpa]?.find(c => c.name === name)` для каждой комбинации (sensorName, gpa) выполняется линейный поиск. При 95 датчиках и 3 ГПА это 285 вызовов find(), каждый из которых проходит до ~32 элементов. Общая сложность: O(sensorNames * gpas * sensorsPerGpa).

**Рекомендация:** Построить `Map<string, Map<string, HeatmapCell>>` (gpa -> name -> cell) в useMemo, заменив find() на O(1) lookup.

---

**[MED-5] SensorChart: dep-массив основного эффекта не включает все используемые значения**
Файл: `src/components/Chart/SensorChart.tsx:209`

Массив зависимостей: `[sensor?.id, chartData, theme, kindFilter, viewDays]`. Но внутри эффекта используется `onLoadMore` (строка 192), `focusTimestamp` косвенно через отдельный эффект -- ОК, однако `gridColor` и `fontColor` (строки 73-74) зависят от `theme` и пересчитываются при каждом рендере, но не являются deps (они и не нужны, т.к. `theme` уже в deps). Более существенно: `onLoadMore` захватывается в замыкании `plotly_relayout` обработчика (строка 192) и никогда не обновляется, т.к. обработчик привязывается один раз при `newPlot`. Если `onLoadMore` изменится -- обработчик будет вызывать устаревшую версию.

**Рекомендация:** Хранить `onLoadMore` в ref: `const onLoadMoreRef = useRef(onLoadMore); onLoadMoreRef.current = onLoadMore` и использовать `onLoadMoreRef.current()` в обработчике.

---

**[MED-6] App.tsx: EventDrawer всегда рендерится в DOM даже когда закрыт**
Файл: `src/App.tsx:381`

`<EventDrawer open={drawerOpen} .../>` всегда присутствует в дереве. Внутри EventDrawer рендерит все 500 событий, применяя `opacity:0; pointerEvents:none` когда закрыт (строка 86). DOM-узлы всех событий существуют постоянно.

**Рекомендация:** Добавить условие `{drawerOpen && <EventDrawer ... />}` или ленивый mount (рендерить содержимое только когда `open=true`).

---

**[MED-7] Отсутствует `strict: true` в tsconfig -- повторяю как отдельное замечание о типизации**
Файл: `src/components/Chart/SensorChart.tsx:44-49`

Типы Plotly определены как `Record<string, unknown>` и `any`. Есть 2 `eslint-disable` и 1 `@ts-expect-error`. При `strict: true` компилятор заставит типизировать взаимодействие с Plotly точнее.

**Рекомендация:** Установить `@types/plotly.js` (уже в devDependencies) и использовать типы `Plotly.Data`, `Plotly.Layout`, `Plotly.Config` вместо `Record<string, unknown>`.

---

**[MED-8] Мёртвые CSS: index.css и App.css -- остатки шаблона Vite**
Файлы: `src/index.css`, `src/App.css`

Оба файла содержат стили Vite-шаблона (`.counter`, `.hero`, `#center`, `#next-steps`, `#docs`), которые нигде не используются в проекте. `index.css` переопределяет CSS-переменные (`:root { --text: #6b6375 }`) которые конфликтуют с `globals.css`. Файл `App.css` не импортируется нигде.

**Рекомендация:** Удалить оба файла. Если `index.css` импортируется через Vite html -- удалить импорт.

---

**[MED-9] Landing: direct DOM manipulation вместо React refs**
Файл: `src/Landing.tsx:35-46`

Компонент напрямую модифицирует `document.body.style.overflow`, `document.documentElement.style.overflow`, и ищет элемент по ID `document.getElementById('root')`. Это работает, но хрупко и нарушает React-парадигму.

**Рекомендация:** Использовать CSS-класс на `<html>` (как сделано для темы в App.tsx:63) и управлять overflow через этот класс.

---

**[MED-10] Множественные keydown-обработчики конфликтуют**
Файлы:
- `src/App.tsx:136-145` -- 'K' toggle kiosk, 'Escape' закрывает всё
- `src/components/KioskMode.tsx:82-88` -- 'K' exit kiosk, 'Escape' exit
- `src/components/EventDrawer/EventDrawer.tsx:47-52` -- 'Escape' close drawer

Когда KioskMode активен и пользователь нажимает Escape -- срабатывают 3 обработчика: App (setDrawerOpen(false), setKioskActive(false)), KioskMode (onExit), EventDrawer (onClose). Нет `e.stopPropagation()` или координации. Это работает сейчас по совпадению, но добавление новых шорткатов легко сломает поведение.

**Рекомендация:** Централизовать обработку клавиш в одном месте (хук `useKeyboardShortcuts`) с приоритетами: modal > drawer > global.

---

**[MED-11] Ticker: дублирование массива events для бесконечной прокрутки**
Файл: `src/components/Ticker/Ticker.tsx:24`

`const items = events.length > 0 ? [...events, ...events] : []` -- массив дублируется при каждом рендере. При 30 событиях (visibleTickerEvents) это 60 элементов, некритично, но при изменении лимита может расти. Также нет `useMemo`.

**Рекомендация:** Обернуть в `useMemo(() => ..., [events])`.

---

**[MED-12] SensorChart: предзагрузка Plotly на уровне модуля**
Файл: `src/components/Chart/SensorChart.tsx:60`

`getPlotly()` вызывается на строке 60 при импорте модуля -- это начинает загрузку ~3.5 МБ JS сразу при входе в приложение, даже если пользователь ещё на Landing-странице или не выбрал датчик.

**Рекомендация:** Вызывать предзагрузку только при первом активировании вкладки "Мониторинг" или при первом выборе датчика.

---

### LOW

**[LOW-1] App.tsx:59 -- пустой catch при записи в localStorage**
Файл: `src/App.tsx:59`

`try { localStorage.setItem(...) } catch {}` -- ошибка записи проглатывается молча. Если localStorage заполнен -- пользователь не узнает.

**Рекомендация:** Добавить хотя бы `console.warn` в catch.

---

**[LOW-2] App.tsx: non-null assertion `kindFilter!` избыточен**
Файл: `src/App.tsx:262`

`KIND_LABEL[kindFilter!]` -- восклицательный знак не нужен, т.к. строка находится внутри `{kindFilter && ...}` guard.

**Рекомендация:** Убрать `!`.

---

**[LOW-3] StatsGrid: prop `active` не используется в StatCard**
Файл: `src/components/Stats/StatsGrid.tsx:8, 11`

Интерфейс `StatCardProps` имеет поле `active: boolean`, оно передаётся, но нигде не используется в JSX компонента для визуального отличия. Нет подсветки активного фильтра на карточке.

**Рекомендация:** Добавить визуальное выделение активной карточки (border/outline) или удалить prop.

---

**[LOW-4] DatePicker: нет ограничения на выбор будущих дат**
Файл: `src/components/DatePicker/DatePicker.tsx:99-120`

Пользователь может выбрать дату в будущем для фильтра графика, что бессмысленно для исторических данных.

**Рекомендация:** Добавить `disabled` стиль и блокировку для дат после `today`.

---

**[LOW-5] App.tsx:332 -- KIND_LABEL_MAP создаётся на каждый рендер в IIFE**
Файл: `src/App.tsx:332`

`const KIND_LABEL_MAP = KIND_LABEL` -- бессмысленное присваивание локальной переменной внутри inline render-функции.

**Рекомендация:** Использовать `KIND_LABEL` напрямую.

---

**[LOW-6] Sidebar: useRef после условного return нарушает правила хуков**
Файл: `src/components/Sidebar/Sidebar.tsx:141`

`const expandedRef = useRef<HTMLDivElement>(null)` объявляется ПОСЛЕ условного `if (collapsed) { return ... }` на строке 57. Это нарушение Rules of Hooks -- хуки должны вызываться безусловно. React может не отловить это на практике, потому что `memo` оборачивает компонент, но это ожидает runtime-ошибку при обновлении React или в StrictMode.

**Рекомендация:** Переместить все `useRef` и `useState` (строки 141-142) выше условного return на строке 57.

---

**[LOW-7] react-plotly.js -- неиспользуемая зависимость**
Файл: `package.json:17`

`react-plotly.js` и `@types/react-plotly.js` установлены, но нигде не импортируются. Plotly используется напрямую через dynamic import.

**Рекомендация:** Удалить `react-plotly.js` и `@types/react-plotly.js` из зависимостей.

---

### Положительные наблюдения

1. **Грамотное использование react-query:** Все API-запросы через `useQuery` с `refetchInterval`, `keepPreviousData`, `staleTime` -- это правильный подход к real-time данным с плавными обновлениями.

2. **AbortController в SchemaPanel:** fetch с `signal: controller.signal` и cleanup через `controller.abort()` -- корректная отмена запросов (строки 14, 49).

3. **Типизация API-клиента:** `api/client.ts` чисто типизирован через generics `get<T>()`, все endpoint возвращают конкретные типы. Функция `encodeURIComponent` применяется к пользовательским ID.

4. **Мемоизация тяжёлых вычислений в App.tsx:** `displayChartData`, `filteredSensorIds`, `events`, `visibleTickerEvents` -- всё обёрнуто в `useMemo`.

5. **Sidebar обёрнут в `memo`** -- предотвращает лишние ререндеры при изменении состояния App.

6. **Дизайн-система через CSS-переменные:** Последовательное использование `--bg`, `--surface`, `--crit` и т.д. делает тему переключаемой одним CSS-классом.

7. **Доступность EventDrawer:** `role="dialog"`, `aria-modal="true"`, `aria-label`, focus trap -- хорошие практики a11y.

8. **Lazy-загрузка Plotly через dynamic import:** Предотвращает блокировку main bundle.

---

### ТОП-5 важнейших исправлений

| # | Серьёзность | Что | Где | Оценка трудозатрат |
|---|------------|-----|-----|-------------------|
| 1 | CRITICAL | Добавить изоляцию таймеров/глобалов для schema.html (monkey-patch setInterval, Shadow DOM для стилей) | `SchemaPanel.tsx` | 3-4 часа |
| 2 | HIGH | Заменить `plotly.js` на `plotly.js-basic-dist-min`, удалить `react-plotly.js` | `SensorChart.tsx`, `package.json` | 30 мин |
| 3 | HIGH | Устранить дублирование `KIND_LABEL` и `SEV_COLOR` -- единый импорт из `types/index.ts` | `App.tsx`, `SensorChart.tsx`, `Ticker.tsx`, `KioskMode.tsx`, `EventDrawer.tsx` | 20 мин |
| 4 | HIGH | Исправить жёстко зашитые `['GPA1','GPA2','GPA3']` -- вычислять из данных | `KioskMode.tsx:90`, `Sidebar.tsx:24` | 15 мин |
| 5 | HIGH | Добавить `"strict": true` в tsconfig и исправить ошибки типов | `tsconfig.app.json` + весь src/ | 1-2 часа |

---

### Вердикт

**REQUEST CHANGES** -- обнаружены 2 CRITICAL и 6 HIGH проблем. Наиболее срочные: изоляция скриптов schema.html (CRIT-1/CRIT-2) и устранение дублирования констант с расхождением значений (HIGH-2), которое уже приводит к несогласованности UI.