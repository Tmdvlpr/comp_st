# Stitch Prompt Pack — CS Monitor AI

Пакет промптов для генерации UI всех экранов системы через **Google Stitch MCP**
(`stitch.googleapis.com/mcp`). Собран из канонов проекта:
[DESIGN.md](../../DESIGN.md) (система) + [globals.css](../../frontend/src/styles/globals.css) (визуальные токены).

**Как это используется циклом `/loop`:** одна итерация = один экран. Берётся
`PREAMBLE` (Часть 0) + промпт конкретного экрана (Часть 2), склеивается и подаётся в
Stitch. Результат сохраняется, отмечается в чеклисте (Часть 3), цикл берёт следующий.

> Промпты написаны на английском (Stitch/Gemini лучше генерят по-английски), но **весь UI-текст —
> на русском**: конкретные подписи указаны в каждом промпте. Токены заданы явными hex/px, чтобы
> результат совпадал с существующей дизайн-системой.

---

## Часть 0 — GLOBAL CANON PREAMBLE (вставляется в КАЖДЫЙ промпт)

```
You are designing screens for "CS Monitor AI" — a real-time anomaly-monitoring operator
dashboard for a gas compressor station (Russian: «компрессорная станция», КС).
Default station: Охангаронская КС, with three gas-pumping units labeled ГПА-1, ГПА-2, ГПА-3.
Audience: control-room operators watching live SCADA telemetry. Priorities: information density,
instant severity scanning, zero ambiguity, calm under 24/7 always-on display.

VISUAL LANGUAGE — immersive dark "control room" glassmorphism, industrial-professional, precise.
Not playful, not consumer. Think SCADA / mission-control, but modern and clean.

COLOR TOKENS (dark theme — primary):
- App background: deep #0d1426 with a subtle multi-stop gradient overlay
  (purple radial glow top-left rgba(96,66,168,0.34), blue radial top-right rgba(40,86,168,0.22),
   diagonal dark gradient #1a1533 → #141b30 → #0c1222). Background is fixed, does not scroll.
- Glass surfaces (cards/panels): rgba(26,31,56,0.78) with 12px backdrop blur;
  deeper surface rgba(38,44,74,0.82); highest rgba(52,58,94,0.88).
- Hairline borders: rgba(132,140,200,0.16); emphasized rgba(132,140,200,0.30).
- Text: primary #E6ECF6, pure #FFFFFF for key numbers, secondary #B7C0D6, tertiary #9BA5BC.
- Accent (interactive/brand): #58A6FF, lighter #7CC0FF, strong-for-buttons #2563c4 (white text on it).
- SEVERITY (the most important scan colors):
    crit = #FF5C6C, warn = #F5B14C, info = #8C93B0, ok = #3FB950.
    Severity badges: UPPERCASE monospace, 20%-tinted background of the severity color,
    text in the lighter "ink" variant (crit #FF8893, warn #F7C06B, ok #6EE67D, info #C5CBDF).

SHAPE & SPACING:
- Radii: 8px (small), 12px (medium), 18px (large cards).
- Spacing scale, multiples of 4: 4, 8, 12, 16, 24, 32. Keep a consistent rhythm.
- Shadows: soft large — 0 8px 30px rgba(0,0,0,0.35), bigger 0 18px 54px rgba(0,0,0,0.5).

TYPOGRAPHY:
- UI font: Inter (system-ui fallback). Numeric/technical/tag text: JetBrains Mono (monospace).
- Sizes: 12 (xs, mono labels/tags), 14 (base body), 16 (md), 20 (lg section titles),
  30 (xl page/KPI), 56 (2xl hero numbers, kiosk).
- Line-height: 1.25 tight (headings/numbers), 1.5 base, 1.75 relaxed.
- SCADA tags & sensor ids are ALWAYS monospace (e.g. GPA-1.GPA-1.PD.PV, gas_pressure_out_gpa__GPA1).

COMPONENTS VOCABULARY:
- Card: 18px radius, glass surface, hairline border, soft shadow, backdrop blur.
- Chip: pill (12px radius), monospace 11px, glass surface-2; active chip = solid accent-strong
  #2563c4 with white bold text.
- Severity dot: 6px circle in the severity color (crit dot has a soft glow).
- Badge-sev: tiny uppercase mono badge, tinted bg, wide letter-spacing.

MOTION (subtle, purposeful — this is an always-on screen, avoid noise):
- crit items softly pulse; a top news-ticker scrolls slowly; skeleton shimmer during load.
- Everything must degrade gracefully with prefers-reduced-motion.

ACCESSIBILITY:
- WCAG AA contrast on all text. Visible keyboard focus ring: 2px solid accent, 2px offset.

LANGUAGE: ALL visible UI copy is in RUSSIAN. Use the exact Russian labels given per screen.

ALSO PROVIDE a light-theme variant where relevant (bg #e9ecf6, white glass surfaces,
text #1b2138, accent #2f7ff0, severity crit #d8394a / warn #c2780f / info #6b7494 / ok #1f9d54).
```

---

## Часть 0.5 — AUDIT FIXES (2026-07-01) — обязательны во ВСЕХ экранах

Из дизайн-аудита ([DESIGN_AUDIT_2026-07-01.md](../ui-reviews/DESIGN_AUDIT_2026-07-01.md), балл 3.75/5).
Каждый генерируемый экран ДОЛЖЕН исправлять эти проблемы, а не воспроизводить их:

```
1. SEVERITY CONTRAST & SEMANTICS: text on any solid/tinted severity fill must meet WCAG AA
   (≥4.5:1) — use the ink text variants on 20% tints, never low-contrast text on a saturated pill.
   Keep ONE consistent color contract for severity AND for anomaly types across every screen
   (chart, journal, SHAP/feature views) — the same anomaly type must look identical everywhere.
2. TYPOGRAPHY SCALE: use ONLY the type scale (12/14/16/20/30/56) — no off-scale ad-hoc sizes.
   Design an explicit oversized wall/kiosk scale for the kiosk screen.
3. CHART CARD DECLUTTER: the chart card header must be calm — group controls into labeled
   buttons or an overflow menu, drop decorative/ambiguous tools; every icon has a text label or tooltip.
4. ALERT-BLINDNESS ANTIDOTE: pulsing/attention motion applies ONLY to the top few NEWEST crit items,
   never to all alerts at once. Everything else is static-but-legible.
5. DISTINGUISH STATES: visually separate «info»-severity from «нет модели / нет данных». Surface
   «данные устарели» / «нет связи с БД» as a top-level, unmissable status (not a quiet footnote).
```

## Stitch runtime (заполнено 2026-07-01)
- projectId: `7758599457006288338`
- designSystem: `assets/1e8122f286b1477387c5b122a5228607` (deviceType `DESKTOP`; для `09-kiosk` тоже DESKTOP/крупный масштаб)
- Дизайн-система УЖЕ несёт все токены — в per-screen промпт токены не дублируем, шлём: назначение + layout + русские подписи + audit-fixes + состояния.

---

## Часть 1 — Инвентарь экранов (порядок исполнения циклом)

| # | ID | Экран | Компоненты-источники |
|---|-----|-------|----------------------|
| 0 | `00-app-shell` | Каркас приложения (тикер + сайдбар + контент + переключатель видов) | Ticker, Sidebar, StationSwitcher |
| 1 | `01-landing` | Экран входа / выбор станции | Landing, StationSwitcher |
| 2 | `02-monitor-overview` | Главный дашборд «Мониторинг» (вид по умолчанию) | Overview, Stats, PriorityBanner, HeatMap, Ticker |
| 3 | `03-sensor-detail` | Детализация датчика: график факт+модель+коридор+аномалии | Chart, Detail, DatePicker |
| 4 | `04-heatmap` | Тепловая карта датчик × ГПА | HeatMap |
| 5 | `05-event-journal` | Журнал событий / выдвижная панель аномалий | EventDrawer |
| 6 | `06-schema-mnemonic` | Мнемосхема (P&ID) с живыми значениями PV | Schema |
| 7 | `07-engine-view` | Панель агрегата ГПА (двигатель) | Engine |
| 8 | `08-shift-report` | Сменный отчёт (печатный) | Report |
| 9 | `09-kiosk` | Киоск-режим (настенный экран) | Kiosk |
| 10 | `10-states` | Состояния: загрузка/пусто/ошибка/скелетоны | ApiErrorBanner, ErrorBoundary, Freshness |

---

## Часть 2 — Промпты по экранам

### `00-app-shell`
**Цель:** общий каркас, в который вложены все виды.
```
Design the application shell for CS Monitor AI (see canon preamble).
Layout, full-viewport, no page scroll:
- TOP BAR (48–56px tall, glass): left = product mark "CS Monitor AI" + station switcher
  (a compact dropdown showing «Охангаронская КС»); center = a slow horizontal NEWS TICKER
  scrolling the latest anomalies as monospace chips colored by severity
  («КРИТ · ГПА-1 · давление на выходе · 12:35»); right = a "freshness" indicator
  («обновлено 2 мин назад», green dot when fresh, amber when stale >15м, red when down >60м),
  a theme toggle (sun/moon icon-swap), and a kiosk-mode button.
- LEFT SIDEBAR (collapsible, ~280px): view switcher with three primary views —
  «Мониторинг», «Схема», «Двигатель»; below it a scrollable sensor list grouped by ГПА-1/2/3,
  each sensor row = severity dot + monospace sensor name + tiny anomaly count badge; a search field
  («поиск датчика…») and severity filter chips («КРИТ», «ВНИМ», «ИНФО») at the top of the list.
- MAIN CONTENT AREA: the active view fills the remaining space.
Show the shell with «Мониторинг» active. Deliver both collapsed and expanded sidebar states.
```

### `01-landing`
**Цель:** первый экран / выбор станции.
```
Design the landing / entry screen for CS Monitor AI (see canon preamble).
A centered hero over the immersive gradient background: large product title «CS Monitor AI»,
subtitle «Выявление аномалий компрессорной станции в реальном времени».
Below, a grid of STATION CARDS (glass cards, 18px radius). Feature card: «Охангаронская КС»
with a live status line (3 units ГПА-1/2/3, small severity summary: «2 крит · 5 вним»),
last-update timestamp (monospace), and a primary button «Открыть дашборд» (accent-strong).
Other station cards appear as «нет данных» / disabled. Include a subtle animated entrance
(fade-up, staggered). Keep it calm and premium, not marketing-flashy.
```

### `02-monitor-overview`
**Цель:** главный операционный дашборд.
```
Design the main "Мониторинг" dashboard for CS Monitor AI (see canon preamble), inside the app shell.
Top of content: a PRIORITY BANNER that appears only when critical anomalies exist — a full-width
crit-tinted bar with a soft pulse: «⚠ 2 критические аномалии требуют внимания» + a «Показать» button
that opens the event journal.
KPI ROW: 4–5 stat cards (glass), each a big monospace number + label + tiny trend:
  «Активные аномалии» (split by severity with colored dots), «Критические», «Датчиков онлайн»,
  «Дрейф моделей», «Свежесть данных». Critical count uses crit color; the card subtly pulses if >0.
MAIN GRID (2 columns on wide screens):
  - LEFT (wider): a HEATMAP card — matrix of sensors (rows) × units ГПА-1/2/3 (columns),
    cells colored by current worst severity (ok/info/warn/crit), monospace axis labels; hover = tooltip.
  - RIGHT: an EVENT LIST card — latest anomaly episodes, each row: severity badge, monospace sensor id,
    human time («12:35»), type («ML-выброс» / «залипание» / «скачок» …), deviation %, and an
    «квитировать» (acknowledge) action. Acknowledged rows dim.
Below: a full-width SENSOR CHART preview for the currently selected sensor (see 03).
Everything on glass cards with consistent 16–24px gaps. Show a version with zero anomalies (calm,
all «норма», ok-green accents) AND a version with active crit/warn anomalies.
```

### `03-sensor-detail`
**Цель:** ключевой экран анализа одного датчика.
```
Design the SENSOR DETAIL view for CS Monitor AI (see canon preamble).
Header: monospace sensor id (gas_pressure_out_gpa__GPA1) + human name («Давление газа на выходе, ГПА-1»)
+ current value (big mono) + severity badge + a range/time selector (chips: «6ч», «24ч», «7д», «30д»,
plus a custom date-range picker «с … по …»).
CENTER — a large TIME-SERIES CHART (Plotly-style, dark) showing FOUR layers:
  1) actual sensor value (solid accent-blue line),
  2) model-predicted value (dashed secondary line),
  3) the "normal corridor" (translucent band around the prediction),
  4) anomaly markers (crit/warn/info colored dots/segments on the points that breached the corridor).
  A subtle vertical divider marks last_train_timestamp — DO NOT draw the model line before it
  (training period). Include zoom/pan affordances and a legend with toggle chips.
BELOW the chart — a HEALTH TIMELINE strip: a thin horizontal ribbon over the same time axis,
colored per 5-minute tick by severity (ok = muted green #22351f, else severity color) so operators
scan when the sensor was unhealthy.
SIDE PANEL: sensor metadata — model R²/MAE, residual std, sensor range, drift flag, last anomaly,
detector breakdown (counts per type 1..7). All numbers monospace.
Include a chart LOADING state: diagonal sweep shimmer over the plot area.
```

### `04-heatmap`
**Цель:** обзор состояния всех датчиков разом.
```
Design a full-screen HEATMAP view for CS Monitor AI (see canon preamble).
A dense matrix: rows = sensors (monospace names, grouped/collapsible by measurement family),
columns = units ГПА-1, ГПА-2, ГПА-3 (optionally a time dimension). Each cell colored by current
severity (ok/info/warn/crit) with the small severity-tint palette; empty/no-data cells are neutral
hatched. Sticky row+column headers. Hover tooltip shows sensor, unit, current value, deviation, and
last anomaly time. A severity legend and a unit/severity filter row (chips) on top. Clicking a cell
opens the sensor detail (03). Keep it readable at a glance from across a control room — strong
color contrast, generous cell size option (a «крупные ячейки» toggle for kiosk).
```

### `05-event-journal`
**Цель:** полный журнал аномалий + фильтры.
```
Design the EVENT JOURNAL for CS Monitor AI (see canon preamble), shown both as a right-side
SLIDE-OVER DRAWER and as a full page.
A filter bar: severity chips («КРИТ/ВНИМ/ИНФО»), unit chips («ГПА-1/2/3»), type chips
(«ML», «физичность», «залипание», «скачок», «сезонная», «режим», «кросс-ГПА»), a time-range picker,
and a search. The list = grouped by day, each event row: severity badge, monospace sensor id,
start time..end time, episode duration, type label (Russian), peak value + deviation %, and actions
(«квитировать», «открыть график»). Acknowledged events dim and move to a collapsed «Квитированные»
section. Show an empty state («Событий не найдено») and a busy state with many crit events.
Include a small summary header: counts per severity for the current filter.
```

### `06-schema-mnemonic`
**Цель:** мнемосхема КС (P&ID) с живыми значениями.
```
Design the SCHEMA (mnemonic / P&ID) view for CS Monitor AI (see canon preamble).
An engineering schematic of the compressor station: three gas-pumping units ГПА-1, ГПА-2, ГПА-3
connected by gas piping (inlet → compressor → outlet), with valves, coolers, and key measurement
points. Each live measurement point is a small glass tag showing sensor value (monospace) + unit,
its border/fill colored by current severity (ok/info/warn/crit); crit points pulse softly.
Pipes can tint by flow status. A side legend maps colors to severity and lists active alarms on the
schema. Clicking a point opens sensor detail (03). Provide clean vector-style piping, precise
alignment, industrial look. Include a zoom/pan control and a «только аномалии» toggle that dims
normal points. Show both a normal state and a state with a crit point on ГПА-1.
```

### `07-engine-view`
**Цель:** детальный вид одного агрегата ГПА.
```
Design the ENGINE ("Двигатель") view for CS Monitor AI (see canon preamble) — a deep view of ONE
gas-pumping unit (ГПА-1). A unit selector (ГПА-1/2/3) at top. Center: a schematic/illustrated
representation of the turbine+compressor unit with its key sensors annotated inline (RPM, давление,
температура, вибрация, обороты) as monospace value tags colored by severity. Around it, cards:
running status («в работе» / «стоянка» / «прогрев» with clear state color), a small multi-sensor
mini-chart, current corridor breaches, and suppression status (why alarms may be muted:
«стоянка — ml/frozen подавлены»). Convey the physical-consistency idea: sensors that disagree with
the model are highlighted. Show a «в работе» state and a «стоянка» (idle, alarms suppressed) state.
```

### `08-shift-report`
**Цель:** печатный сменный отчёт.
```
Design a printable SHIFT REPORT ("Сменный отчёт") for CS Monitor AI (see canon preamble).
This screen has TWO renderings:
1) On-screen (dark glass) with a toolbar: date/shift picker, «Печать», «Экспорт PDF».
2) PRINT layout: white background, black text, no shadows, no glass — a clean document.
Content: report header (station «Охангаронская КС», shift date/time, operator), an executive summary
(counts per severity, most-affected units/sensors), a table of all anomaly episodes for the shift
(time, sensor, unit, type, severity, peak deviation, acknowledged y/n), a section per unit
ГПА-1/2/3 with mini status, and a notes/handover area. Tables use monospace for numeric columns.
Make the print version genuinely paper-ready (A4, legible, no color-on-color).
```

### `09-kiosk`
**Цель:** настенный экран для операторской.
```
Design the KIOSK / wall-display mode for CS Monitor AI (see canon preamble). Full-screen, meant to
be read from across a room — oversized typography (hero numbers up to 56px), high contrast, minimal
chrome, no small controls. Show: a giant station status headline («ОХАНГАРОНСКАЯ КС — 2 КРИТ»),
big severity count tiles (crit/warn/info/ok) with pulsing crit, a large simplified heatmap or unit
status row (ГПА-1/2/3 each as a big colored tile with worst severity), a slow full-width ticker of
active critical anomalies, and a prominent clock + freshness indicator. When all-normal: a calm
full-green «ВСЕ СИСТЕМЫ В НОРМЕ» state. Auto-cycling between overview and heatmap is fine. No
interaction assumed. Maximum legibility.
```

### `10-states`
**Цель:** единый набор системных состояний (используется всеми экранами).
```
Design the shared SYSTEM STATES for CS Monitor AI (see canon preamble), as a small gallery:
- API ERROR BANNER: top-of-screen red-tinted glass bar «Нет связи с сервером — данные могут
  устаревать» with a «Повторить» button; a milder amber «degraded/устаревшие данные» variant.
- ERROR BOUNDARY (whole-view crash): a centered glass card «Что-то пошло не так» + «Перезагрузить».
- LOADING SKELETONS: card skeletons and a chart skeleton with the diagonal sweep shimmer
  (transparent → soft accent highlight → transparent, moving diagonally).
- EMPTY STATES: «Нет данных за выбранный период», «Аномалий не обнаружено» (calm ok-green),
  «Датчик не найден».
- FRESHNESS states: fresh (green dot «обновлено только что»), stale (amber «данные устарели, 22 мин»),
  down (red «нет данных >60 мин»).
Keep all consistent with the glass + severity palette.
```

### Component addendum (мелкие, если нужны отдельно)
```
Also produce isolated component sheets consistent with the canon:
- Ticker (scrolling severity chips), StationSwitcher dropdown, DatePicker / range picker,
  PriorityBanner (crit pulse), Freshness indicator, Sidebar sensor row, severity Badge + Chip +
  sev-dot set, and the theme toggle icon-swap (sun/moon). Show each in its states
  (default / hover / active / disabled) on the dark theme, plus a light-theme strip.
```

---

## Часть 3 — Протокол исполнения циклом `/loop`

**Каждая итерация:**
1. Выбрать первый невыполненный экран из чеклиста ниже (сверху вниз).
2. Склеить `PREAMBLE` (Часть 0) + промпт экрана (Часть 2).
3. Вызвать инструмент Stitch (после перезапуска Claude Code — уточнить точную сигнатуру
   `mcp__stitch__*` через ToolSearch, промпты писались tool-agnostic).
4. Сохранить артефакт (ссылку/файл дизайна) и отметить экран `[x]` в чеклисте.
5. Если Stitch поддерживает уточнения — прогнать 1 ревизию по канону (severity-цвета, mono для чисел,
   контраст AA), затем следующий экран.
6. Остановиться, когда все `[x]` или пользователь прервал.

**Чеклист прогресса:**
- [ ] `00-app-shell`
- [ ] `01-landing`
- [ ] `02-monitor-overview`
- [ ] `03-sensor-detail`
- [ ] `04-heatmap`
- [ ] `05-event-journal`
- [ ] `06-schema-mnemonic`
- [ ] `07-engine-view`
- [ ] `08-shift-report`
- [ ] `09-kiosk`
- [ ] `10-states`
- [ ] component-addendum

**Прерывания:** Stitch недоступен (нет `mcp__stitch__*`) → остановиться и попросить перезапуск.
Rate-limit/ошибка Stitch → бэкофф, повтор того же экрана на следующей итерации.
