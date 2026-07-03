# UI-REVIEW — CS Monitor AI (frontend, host-launch v23)

**Audited:** 2026-07-01
**Scope:** `cs_4/frontend/src` (React 19 + Vite + Plotly + Tailwind), judged against `DESIGN.md` §8 and `ARCHITECTURE.md` §6.
**Method:** Full code read of shell + all named components + tokens; visual cross-check against 8 rendered screenshots (`audit-01-landing`, `unified-monitor/-detail/-features`, `detailpanel-fixed`, `epistemic-strip-verify`, `verify-subplot`, `verify2`).
**Posture:** Critical / picky — this is a safety-adjacent 24/7 control-room wall display.

---

## Overall score: **6.9 / 10**

This is a genuinely strong, mature dashboard with a real design system, honest empty/error/loading states in most places, a colorblind-safe *chart* encoding, thorough keyboard/ARIA work, and correct `prefers-reduced-motion` handling. It is held back from an 8+ by **three data-integrity/trust defects that are unacceptable on a safety board** (fabricated SHAP/"similar incidents"/domain-index values rendered as if real; a two-timezone clock split; a leaked English tooltip), plus a colorblind gap on the HeatMap (the single most-scanned element) and a visual hierarchy that ranks anomaly cards by *count* rather than *severity*.

| # | Pillar | Score |
|---|--------|-------|
| 1 | Visual hierarchy & layout | 7 / 10 |
| 2 | Color & contrast | 7 / 10 |
| 3 | Typography | 8 / 10 |
| 4 | Consistency & design-system adherence | 6 / 10 |
| 5 | Feedback & states | 6 / 10 |
| 6 | Accessibility & interaction | 8 / 10 |

---

## Pillar 1 — Visual hierarchy & layout — **7 / 10**

### What's good
- **Wall-display-appropriate grid.** `App.tsx` uses an explicit 3-column CSS grid (`rootGridStyle`, App.tsx:804) with a 36 px ticker row and `100vh` lock; sidebar collapses 272→48 px. Density is right for an operator screen and nothing scrolls the shell.
- **Stale banner dominates correctly.** "ДАННЫЕ УСТАРЕЛИ" renders red and bold in the top bar (visible in every monitor screenshot); the top-of-screen `ApiErrorBanner` (ApiErrorBanner.tsx:21) is `position:fixed; z-index:9000` full-width red — the highest-priority failure state genuinely wins the top of the visual field.
- **PriorityBanner** surfaces the single worst unacked event above the fold (PriorityBanner.tsx:12-24, crit-outranks-warn then newest), and the focused-event panel uses Von-Restorff elevation (`--surface-3` + colored 3px left border, App.tsx:1087-1097).
- **Chart is the star.** In `epistemic-strip-verify.png` / `verify2.png` the main plot correctly dominates the center column with a synchronized epistemic sub-plot, range slider, corridor band and a "Начало мониторинга" divider — dense but legible.

### Problems (evidence → fix)
1. **Severity is out-ranked by magnitude in the StatsGrid.** `AnimatedValue` bumps font-size to `--fs-2xl` (56 px) whenever `value>0` regardless of severity (StatsGrid.tsx:37), and `crit` styling only triggers at `value>=10` (StatsGrid.tsx:64). In `unified-monitor.png` the amber **"СКАЧОК ΔV 208"** is the largest, loudest card while the actually-critical **"СТАТ. ВЫБРОС 10"** is visually equal-or-smaller. On an alarm board the eye must go to *severity*, not to the biggest counter (info/warn spam). **Fix:** rank/size cards by max severity first (crit > warn > info), then by count; cap the "count-driven" size jump so a 208-count `info` never outweighs a `crit`.
2. **Enormous empty chart void on first paint.** With no sensor selected (`unified-monitor.png`) the entire center-right is blank apart from "Нажмите на датчик…"; even after selecting, `unified-detail.png` shows a large "Загрузка данных…" emptiness. For a default wall view this wastes the most valuable real estate. **Fix:** auto-select the highest-severity sensor (or show a plant-health summary / mini heat-strip) when `selectedId` is null so the center is never dead.
3. **`--fs-xl` (30 px) → `--fs-2xl` (56 px) is a big jump with no step between** (globals.css:60-61); the kiosk RPM uses a *hardcoded* 72 px (KioskMode.tsx:271) outside the scale, so the largest display numerals aren't on one ramp. Minor, but it means the type scale doesn't actually govern the biggest elements.

---

## Pillar 2 — Color & contrast — **7 / 10**

### What's good
- **Documented WCAG remediation.** The tokens carry explicit contrast notes: `--text-3:#9BA5BC` "≈4.8:1 on --bg" (globals.css:20), the `*-ink` badge colors are tuned for ≥4.5:1 on tinted fills (globals.css:32-38), and `SEV_TEXT_ON_SOLID` (types/index.ts:247-252) uses dark text on amber/green pills with a comment proving `#1a1f3a` gives ≥7:1 — exactly right, and visible working in `detailpanel-fixed.png` (КРИТ pill = white-on-red, В РАБОТЕ = dark-on-green).
- **Chart severity is dual-encoded (colorblind-safe).** `KIND_SYMBOL` gives each anomaly kind a distinct *shape* — ml=diamond, regime=star, roc/seasonal=triangle-up, cross=triangle-down, neg=star-diamond (SensorChart.tsx:46-54) — on top of color. Confirmed in `verify2.png`: red diamonds + green stars are distinguishable without hue. This is best-practice for an alarm chart.
- **Sidebar severity has a real second channel.** Contrary to the standing "opacity-only" note, `Sidebar` encodes severity by **dot size** (`SEV_SIZE` 8/6/5/4, Sidebar.tsx:28) *and* a **ring on crit** (`boxShadow: 0 0 0 3px --text-1`, Sidebar.tsx:282) in addition to opacity — genuinely accessible.
- **Dark/light parity.** Every severity/surface token has a `.light` counterpart (globals.css:97-130); `SEV_COLOR`/`SEV_LABEL` route through CSS vars (types/index.ts:233-238), so theme switching is correct. HeatMap moved off hardcoded rgba to `color-mix` on vars (HeatMap.tsx:16-21) precisely so it re-themes.

### Problems (evidence → fix)
1. **HeatMap encodes severity by hue only — the most-scanned element fails colorblind users.** `SEV_CLASS` (HeatMap.tsx:16-21) differentiates cells purely by *color* (crit=red-ish, warn=amber, ok=green, info=grey) at similar low fill %; there is **no shape, glyph, count, or pattern** as a second channel. The Sidebar got size+ring but the HeatMap didn't — an inconsistency the standing review flagged and I confirm in `unified-monitor.png`/`verify2.png` (a red-green deuteranope cannot tell crit `gt01.ctrl.in` cells from ok cells). This is the single biggest accessibility risk because the matrix is the primary triage surface. **Fix:** add a per-cell glyph or the `anomaly_count` number, and/or a fill *pattern* (e.g. diagonal hatch for crit like the chart corridor), so severity is legible without hue.
2. **Three different, hardcoded color palettes for the same 7 anomaly kinds.** `SensorChart.KIND_COLOR` (`ml:#FF4560…`, SensorChart.tsx:37-45), `EventDrawer.KIND_DOT` (`ml:#CC3333…`, EventDrawer.tsx:21-29), and `ContributingFeatures.FEAT_COLORS` (SensorChart uses `#FF4560` for ml while the drawer uses `#CC3333`). The same "Стат. выброс" is a different red on the chart, in the journal filter, and in the case card. None are tokens, so none re-theme. **Fix:** define one `KIND_COLOR` token set in `globals.css` (with `.light` variants) and import everywhere.
3. **`EventDrawer.KIND_DOT` hexes don't re-theme** and were chosen for an even hue-wheel (comment "0°/45°/85°…"), but on the *light* theme these mid-saturation dots on light surfaces will drop below 3:1. **Fix:** route through tokens as above.

---

## Pillar 3 — Typography — **8 / 10**

### What's good
- **Tabular numerals where it matters.** `fontVariantNumeric: 'tabular-nums'` on the stat values (StatsGrid.tsx:41) and `'tabular-nums lining-nums'` on the kiosk clock (KioskMode.tsx:45) — sensor/anomaly counts don't jitter as they animate/refetch. Correct call for a metrics board.
- **Deliberate two-family system.** `--font-display` (Inter) for UI, `--font-mono` (JetBrains Mono) for sensor tags / timestamps / values (globals.css:93-94). The mono stack is used consistently for SCADA tags like `GPA-1.GPA-1.AX202.PV` (visible in every chart header) — good, tags stay column-aligned.
- **A real, tokenized scale + line-height + measure tokens.** `--fs-xs..2xl`, `--lh-tight/base/relaxed`, and `--measure-sm/md` (globals.css:53-67); long explanatory prose in `ContributingFeatures`/`DetailPanel` is clamped to `var(--measure-md)` (e.g. ContributingFeatures.tsx:411) — readable ~60-80ch lines, which is unusually disciplined.
- **Kiosk legibility at distance.** 72 px RPM, `--fs-2xl` clock, uppercase tracked labels (KioskMode.tsx) read from across a control room.

### Problems (evidence → fix)
1. **Widespread off-scale numeric font-sizes bypass the ramp.** Many inline styles use raw px that aren't tokens: `fontSize: 13/15/16/12.5` throughout `DetailPanel` (e.g. Metric 13/15 at DetailPanel.tsx:214-215, IdxCard `12.5` at :308, `Gauge` 30 at :276), `fontSize: 9/10/11` micro-labels in HeatMap/ContributingFeatures. The scale exists but ~40% of text sizes don't use it, so "one type ramp" is aspirational rather than enforced. **Fix:** map these to `--fs-*` (add an `--fs-2xs:11px` if 11 is truly needed) and lint against raw px font-size.
2. **`fontFamily: 'Inter, monospace'` fallback is wrong** in several inline styles (App.tsx:67, styleTickerLeft; SensorChart layout `family:"'Inter', monospace"` at :356). If Inter fails to load, the browser falls back to a *monospace* face for a proportional UI element — jarring. **Fix:** `'Inter', system-ui, sans-serif` (as `--font-display` already defines) — just reference the token.
3. **The chart legend leaks a metric the design doc calls dead.** Legend text is `Модель (MAE: 2.55)` / `(MAE: 0.0314)` (SensorChart.tsx:194), yet `DESIGN.md` §10 says "MAE — мёртвая метрика … везде `0.0`". Non-zero values now appear, so either it's silently repurposed (residual_mean_val) or genuinely fixed — but the label still advertises a number the canonical doc distrusts, at high prominence. Typographically it also crams a parenthetical metric into a legend swatch. **Fix:** move model quality out of the legend into the Detail→"Качество модели" tab (which already shows nMAE/R² honestly), and reconcile the label with the doc.

---

## Pillar 4 — Consistency & design-system adherence — **6 / 10**

### What's good
- **The token layer is excellent and broadly used** — spacing (`--space-1..6`, multiples of 4), radii, shadows, z-index scale (globals.css:69-72, preventing stacking conflicts), and a full **motion-token system** (`--dur-*`, `--ease-*`, globals.css:73-90) that components reference rather than hardcoding. `.card`/`.chip`/`.badge-sev`/`.sev-dot` component classes (globals.css:176-218) give repeatable primitives.
- **Time formatting is centralized** in `lib/time.ts` with a documented station-TZ convention, and *most* components use it (`EventDrawer`, `DetailPanel`, `KioskMode`, `ShiftReport` all call `fmtStation`).
- **State/URL discipline** — hash-synced sensor/view/range (App.tsx:340-351), `localStorage` for theme/sidebar/acked, cross-tab ack sync via `storage` events (App.tsx:329-336). Coherent.

### Problems (evidence → fix)
1. **The timezone convention is only half-applied — a real split-brain clock (see Pillar 5 #1).** `lib/time.ts` exists precisely to force station TZ, yet the top-bar `Clock` (App.tsx:1226-1230), sidebar "обновлено" (App.tsx:794), `PriorityBanner` (PriorityBanner.tsx:33), `Freshness` (Freshness.tsx:66,73) and `ContributingFeatures.fmtDt` (ContributingFeatures.tsx:182) all use bare `toLocale*` in *browser/RDP* TZ. So the header clock and an event's timestamp on the very same screen can differ by the RDP offset. This is a **design-system violation with safety consequences**. **Fix:** replace all five call sites with `fmtStation` / a `fmtStationTime` helper; add a lint rule banning bare `toLocaleTimeString`/`toLocaleString` outside `lib/time.ts`.
2. **52 hardcoded hex literals across 6 component files** (grep: SensorChart 25, EventDrawer 7, ContributingFeatures 4, useBklitHover 3, MultiSensorChart 3, ErrorBoundary 10). Chart grid/font colors are re-derived from `theme==='dark'` inline (SensorChart.tsx:344-353, ContributingFeatures.tsx:53) instead of reading tokens; anomaly palettes are hardcoded (Pillar 2 #2); `caseBase.ts` embeds `#ff4d5e/#f5a623/#22b8cf/#b48dff` in *data* (caseBase.ts:9-13…). This is the biggest adherence gap: the theme system can't reach any of it. **Fix:** expose chart colors as CSS vars read via `getComputedStyle` (once, memoized) or a shared `chartTheme(theme)` module; move data-colors out of `caseBase`.
3. **Duplicated ad-hoc button styling.** The day-preset buttons are styled inline in `App.tsx` (1049-1061) with the same shape re-implemented in `EventDrawer` filters (EventDrawer.tsx:428-441) and the Detail tabs — a `.btn-toggle` component class would remove ~5 copies. Close buttons (32×32, hover→crit) are re-declared in EventDrawer, ContributingFeatures with identical values.
4. **Two independent `Clock` components** (App.tsx:1224 and KioskMode.tsx:37) with different formats and different TZ correctness. Consolidate.

---

## Pillar 5 — Feedback & states — **6 / 10**

### What's good
- **"No events" is clearly distinguished from "backend down".** This is the headline win: `Freshness` (Freshness.tsx) shows an independent clock color (ok/warn/crit by data age, STALE_MIN=30), plus separate `●ML` and `●БД` chips driven by `/api/health` that turn red + pulse when the engine/DB is down (Freshness.tsx:94-112). `ApiErrorBanner` fires on any API error for 60 s (ApiErrorBanner.tsx). And `SensorChart` explicitly separates the three states — `error` → "⚠ Не удалось загрузить график (сеть/БД)" vs `hasData=false` → "Нет данных" vs `loading` → skeleton (SensorChart.tsx:944-961). An operator can tell "quiet plant" from "we're blind."
- **Optimistic, offline-tolerant ack with confirmation.** Ack is applied locally first and best-effort synced (App.tsx:742-750); "Принять все" is a two-step in-UI confirm (no `window.confirm`, screen-reader friendly) (EventDrawer.tsx:564-595); per-row/per-group `acking` busy state with `aria-busy` (EventDrawer.tsx:188-210).
- **Loading skeletons everywhere** — `ChartSkeleton` with a sweep shimmer, `StationOverview` shape-matched skeleton cards (StationOverview.tsx:307-342), staggered reveals. `keepPreviousData` avoids flashes on refetch.
- **Reduced-motion is thorough** — globals.css:386-408 kills classed *and* inline animations and zeroes the motion tokens; every `animate()` call guards on `prefersReducedMotion()`. Correct for a screen that's on 24/7.

### Problems (evidence → fix)
1. **Two-timezone clock (also a consistency bug).** The top-bar clock renders in RDP/browser TZ while events render in station TZ; the freshness readout in the screenshots ("данные: 17:21 · **1327 мин назад**", ~22 h) is a browser-TZ misread of a UTC/station timestamp. On a control-room display an operator will read the wrong wall-clock time and mis-judge data age. Highest-severity finding in this pillar. **Fix:** as Pillar 4 #1.
2. **Literal "NaN" can render in the journal/focus panel.** `EventRow` guards value with `ev.value != null` then calls `ev.value.toFixed(4)` (EventDrawer.tsx:152-154; identical in the focus panel App.tsx:1103-1106). If the API delivers the JS number `NaN` (which is `!= null`), the UI prints "Значение: **NaN**" — this is the standing finding and it's real. `DetailPanel` already does it right with `Number.isFinite` guards (`fmtNum`, DetailPanel.tsx:199-200). **Fix:** replace the `!= null` guards with `Number.isFinite(ev.value)` and reuse `fmtNum`.
3. **Fabricated data presented as real (trust failure).** See Pillar-wide detail below and Top-10 #1-2. In feedback terms: the "Важные признаки" modal *defaults* to fabricated SHAP + mock sparklines with a fake anomaly marker (`unified-features.png`), and the "Доменные индексы" tab renders hardcoded `val:'0.81'` / warn-colored `+38` (DetailPanel.tsx:47-178) identically to real values. The only differentiator is a tiny 10 px "кейс из базы" / "○ оценка по базе" label (ContributingFeatures.tsx:462-464) — far too subtle for a safety context. **Fix:** when there's no real `explain`/`domain` data, do **not** render numeric values/sparklines that look measured; show a clearly-styled "типовой пример (не измерено)" placeholder, greyed and diagonally-watermarked.
4. **Infinite pulse animations run forever on the always-on screen.** `crit-bar-pulse`/`crit-dot-pulse`/`animate-pulse-dot`/`kiosk-bar-pulse` loop indefinitely (globals.css:346-351). Reduced-motion disables them, but for the *default* 24/7 board, perpetual pulsing on every crit row is fatiguing and can cause burn-in on some panels. **Fix:** cap crit pulse to N cycles then hold, or pulse only the single top-priority crit, not every crit row/dot simultaneously.

---

## Pillar 6 — Accessibility & interaction — **8 / 10**

### What's good
- **Focus management is genuinely done.** A shared `useModal` provides focus-trap + Esc-close + focus-return-to-trigger (used by EventDrawer, ContributingFeatures, KioskMode, ShiftReport); closed drawers set `visibility:hidden` to drop children from tab order (EventDrawer.tsx:342-344). A visible skip-link is the first focusable element (App.tsx:839-846).
- **Global focus-visible ring** at 2px + 2px offset with an explicit rule forbidding `outline:none` without replacement (globals.css:155-163) — WCAG 2.4.7/2.4.11 addressed intentionally.
- **Live region for new crit alerts** — `role=status aria-live=polite` announces "Новая критическая аномалия…" (App.tsx:848-857, 825-833) — screen-reader parity for the most important event.
- **Keyboard everywhere.** HeatMap cells are `role=button tabIndex=0` with Enter/Space and full `aria-label` (name·GPA·severity·count, HeatMap.tsx:152-166); DetailPanel is a proper ARIA `tablist`/`tab`/`tabpanel` with arrow-key navigation and roving tabindex (DetailPanel.tsx:636-690); event rows and kiosk rows are keyboard-operable buttons. Global shortcuts (J/K/T/Esc) with input-field guarding (App.tsx:650-662).
- **Icon-only buttons carry labels.** `IconBtn` sets `title` + `aria-label` + `aria-pressed` (App.tsx:1237-1258); the HeatMap caption and table headers are screen-reader captioned (HeatMap.tsx:85-87).

### Problems (evidence → fix)
1. **The English Plotly tooltip leaks onto a Russian control-room UI.** `verify2.png` shows Plotly's native "**Double-click to zoom back out**" bubble floating over the Analytics panel. Even though `displayModeBar:false` and `doubleClick:false` (SensorChart.tsx:68-76), Plotly still emits its built-in hint. It's untranslated, overlaps content, and looks unfinished. **Fix:** suppress via `config.locale`/CSS on `.plotly-notifier`, or set the plot's `hovermode`/notifier off; verify no other native English strings survive.
2. **Draggable sensors have no keyboard equivalent.** Sidebar rows and HeatMap cells are `draggable` to overlay onto the chart (Sidebar.tsx:241-245, HeatMap.tsx:167-171), but there is no keyboard path to "add as overlay" — keyboard users can select a sensor but can't compose the multi-series comparison. **Fix:** add an "overlay"/"compare" action on Enter+modifier or an explicit "+" button per row.
3. **Custom cursor-follow / drawing / region-select use raw mouse only.** The floating collapse button follows `clientY` (Sidebar.tsx:87-95), pencil drawing and right-drag region-SHAP are pointer-only (SensorChart.tsx:679-717) — acceptable for power features, but the *collapse* control's hit target is a 28 px button that only appears on hover (invisible until `onFocus`, Sidebar.tsx:397) which is fragile. **Fix:** make the collapse toggle a permanently-visible, ≥32 px control.
4. **Tap/hit targets below Fitts comfort for a possibly-touch wall panel.** HeatMap cells are 28×24 px (HeatMap.tsx:173) and several toolbar buttons are 24-26 px (SensorChart tbtn 24 px :812, day-presets 26 px). On a touch kiosk these are under the 44 px guideline. **Fix:** bump interactive minimums to ≥32 px (ideally 44 for kiosk), or enlarge on coarse-pointer media.

---

## What's genuinely good (don't regress these)

- Chart is a serious piece of engineering: synchronized epistemic sub-plot, dual conformal/hybrid corridors as independent toggles, gap-aware band fills, custom bklit hover with rAF throttling, region-SHAP by right-drag, and a purge/newPlot lifecycle that avoids leaks. Visually top-tier (`epistemic-strip-verify.png`).
- Honest ML-quality surfacing in `DetailPanel` (QualityTab/DriftTab/CalibTab) — shows "—" for missing metrics instead of fake numbers, and even explains R²-in-sample-vs-holdout to the operator (DetailPanel.tsx:486-492).
- Real bilingual (RU/UZ) overview with proper `aria-label`s and shape-matched skeletons.
- Print stylesheet that isolates the shift report and forces dark-on-white (globals.css:322-344, ShiftReport.tsx:60-90).
- The failure-mode taxonomy (stale vs down vs no-data vs error) is more rigorous than most production dashboards.

---

## Top 10 UI fixes (ranked by risk × reach)

1. **Kill all fabricated-as-real data.** `caseBase.ts` `CASES.default` feeds `MockPlot` sparklines with a fake anomaly line and invented "вклад +0.30" (ContributingFeatures.tsx:481-501, rendered in `unified-features.png`), and every `CASES` entry ships fictional `similar` incidents ("ГПА-2 · 14.05 — помпаж, останов", caseBase.ts:18). On a safety board an operator can mistake an invented incident/attribution for a real one. Gate the modal to real `/explain` only; when absent show an unmistakable "нет измеренной атрибуции" placeholder. *(Pillar 5)*
2. **Stop rendering hardcoded domain-index values as live readings.** `DetailPanel.INDEX_BASE` hardcodes `polytropic_eff:0.81 (ok)`, `shaft_resid_tnd:+38 (warn)` etc. (DetailPanel.tsx:47-178); `IdxTab` shows them verbatim, warn-colored, when real `domain` data is missing (DetailPanel.tsx:558-564). Render the *number* only when `dom[k]` exists; otherwise show norm/interpretation text without a fake value/status color. *(Pillar 5)*
3. **Unify the clock on station TZ.** Replace bare `toLocale*` at App.tsx:794/1226/1230, PriorityBanner.tsx:33, Freshness.tsx:66/73, ContributingFeatures.tsx:182 with `fmtStation`; lint-ban bare locale formatting outside `lib/time.ts`. Eliminates the header-vs-event time split and the "1327 мин назад" misread. *(Pillars 4,5)*
4. **Give the HeatMap a non-color severity channel.** Add per-cell glyph or `anomaly_count`, and/or a crit hatch pattern, in `SEV_CLASS` cells (HeatMap.tsx:130-205). The matrix is the primary triage surface and is currently hue-only — the worst colorblind gap. *(Pillar 2)*
5. **Rank/size StatsGrid cards by severity, not count.** Change `AnimatedValue` size logic (StatsGrid.tsx:37) and the `crit=value>=10` rule (StatsGrid.tsx:64) so a 208-count `info` never out-shouts a `crit`. *(Pillar 1)*
6. **Fix latent "NaN" in the journal/focus panel.** Swap `ev.value != null` → `Number.isFinite(ev.value)` and reuse `fmtNum` at EventDrawer.tsx:152-154 and App.tsx:1103-1106. *(Pillar 5)*
7. **Suppress the English Plotly "Double-click to zoom back out" bubble** (SensorChart config, :68-76) and audit for any other untranslated native strings. *(Pillar 6)*
8. **Consolidate anomaly-kind colors into one token set.** Reconcile `SensorChart.KIND_COLOR`, `EventDrawer.KIND_DOT`, `ContributingFeatures.FEAT_COLORS` into `--kind-*` CSS vars with `.light` variants so the same kind is one color that re-themes. *(Pillars 2,4)*
9. **Don't leave the center panel dead.** Auto-select the top-severity sensor (or show a plant-health summary) when `selectedId` is null so the wall view is never mostly-empty (`unified-monitor.png`). *(Pillar 1)*
10. **Reduce perpetual pulsing + enlarge kiosk hit targets.** Cap `crit-*-pulse` cycles / pulse only the top crit (globals.css:346-351) to avoid 24/7 fatigue and burn-in; raise HeatMap cells (28×24) and 24-26 px toolbar buttons toward ≥32-44 px for touch/distance use. *(Pillars 5,6)*

---

## Files audited

`App.tsx`, `styles/globals.css`, `types/index.ts`, `lib/{time,caseBase,motion,chartMotion,themeStore,sensorLabels,gpa,useModal}.ts`, `api/{client,errorStore}.ts`, and components: `Sidebar`, `HeatMap`, `Stats/StatsGrid`, `Chart/{SensorChart,ContributingFeatures}` (+ referenced `MultiSensorChart`, `ComparePanel`, `ChartSkeleton`, `ChartBrush`, `ChartLegend`, `useBklitHover`), `Detail/DetailPanel`, `EventDrawer`, `Kiosk/KioskMode`, `Report/ShiftReport`, `Overview/StationOverview`, `Freshness`, `Ticker`, `PriorityBanner`, `ApiErrorBanner`, `ErrorBoundary`, `DatePicker`, `StationSwitcher`, `Schema/SchemaPanel`, `Engine/EngineView`.
Screenshots inspected: `audit-01-landing.png`, `unified-monitor.png`, `unified-detail.png`, `unified-features.png`, `detailpanel-fixed.png`, `epistemic-strip-verify.png`, `verify-subplot.png`, `verify2.png`.
