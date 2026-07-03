

У меня теперь достаточно данных для формирования полного отчёта. Подведу итоги всех находок.

Ключевые факты:
1. **Секция 39**: ищет `rect[x="1170"][y="308/528/748"]` в `wrap` — эти rect находятся в шаблоне `sh-s1`, а data-key="gpa100/200/300" доступен как якорь. Координатная привязка хрупкая.
2. **Глобальное загрязнение**: ~418 var/let/const/function объявлений, множество addEventListener без removeEventListener, setInterval без clearInterval при unmount, глобальные переменные (PVDATA, GPA_SEL, UX, cur, wrap, etc.)
3. **Monkey-patching**: showSheet переопределяется 5 раз (_origShow, _origShow2, _origShow3, _origShow5, _origShow6), injectSensors — 2 раза (_inject6, _injSens39), select — 2 раза (_sel4, _sel6), saveState — 1 раз (_saveState2), updatePVBtn — 1 раз (_updPV6). Цепочка из 5+ звеньев.
4. **statesText/GPA_SEL**: statesText() использует GPA_SEL глобально. На листе s1 видны все 3 ГПА, но бейджи показывают данные только для GPA_SEL (одного агрегата).
5. **Файлы отличаются**: 864477 vs 863650 (разница 827 байт, 15 строк — секция 39 добавлена только в schema.html).
6. **Анимация потока**: buildFlow() анимирует ВСЕ элементы с классом `.pv`/`.flowmove` — без проверки привязки к конкретному ГПА, остановленные агрегаты показывают поток.

## Аудит интерактивной схемы КС «Ахангаран» — Структурированный отчёт

---

### Сводка

**Файлы проверены:** 3 (frontend/public/schema.html, КС_Ахангаран_схема_1_интерактив.html, frontend/src/components/Schema/SchemaPanel.tsx)

**Всего проблем:** 12

| Severity | Кол-во |
|----------|--------|
| CRITICAL | 2 |
| HIGH | 4 |
| MEDIUM | 4 |
| LOW | 2 |

---

### CRITICAL

**[CRIT-1] Утечка таймера setInterval при размонтировании React-компонента — memory leak и double-init**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html:2074`
`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\src\components\Schema\SchemaPanel.tsx:38-43`

Скрипт schema.html на строке 2074 запускает `pvAutoTimer=setInterval(fetchPVFromAPI, 300_000)`. SchemaPanel.tsx инжектит скрипт через `document.createElement('script')` (строки 38-43), но при unmount (строки 62-67) очищает только стили -- таймер `setInterval` НЕ уничтожается. При повторном монтировании компонента (переключение вкладок React) скрипт выполняется заново: создаётся второй setInterval, третий и т.д. Каждый тик запускает `fetchPVFromAPI()` + `refreshPV()` + `injectSensors()` с полной цепочкой monkey-patched функций.

Последствия:
- N таймеров после N переключений вкладок, каждый делает HTTP-запрос каждые 5 минут
- Глобальные переменные (PVDATA, GPA_SEL, wrap, cur) перезаписываются при re-init, но старые замыкания ссылаются на устаревшие объекты
- Рост DOM-узлов (бейджи, flowspark элементы), утечка памяти
- Двойные event listener (hashchange, keydown, resize, pointer*) — дублированные обработчики

**Рекомендация:** В SchemaPanel.tsx при unmount вызывать cleanup-функцию, экспортируемую скриптом. Минимальный вариант — добавить в schema.html глобальную `window.__schemaCleanup`, которая делает `clearInterval(pvAutoTimer)` и снимает все addEventListener. В useEffect return вызывать `window.__schemaCleanup?.()`. Альтернативно — загружать schema.html в `<iframe>`, что изолирует scope и автоматически уничтожает всё при удалении iframe из DOM.

---

**[CRIT-2] Глобальное загрязнение namespace — конфликты при инжекте в React SPA**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html` (весь скрипт)
`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\src\components\Schema\SchemaPanel.tsx:35,38-43`

Скрипт schema.html объявляет в глобальном scope: `PVDATA`, `GPA_SEL`, `UX`, `cur`, `wrap`, `SHEETS`, `DATA`, `SENS`, `LAYER_STATE`, `showSheet`, `select`, `injectSensors`, `refreshPV`, `updatePVBtn`, `saveState`, `applyFlow`, `buildFlow`, `fit`, `pvTag`, `statesText`, `statesTextA`, `alarmList`, `cmpModal`, `alarmModal`, `goSensor`, `flash`, `applyHash`, `pvAutoTimer` и ещё ~380 идентификаторов. SchemaPanel.tsx инжектит скрипт через `replaceChild(script)` — всё попадает в `window`.

Последствия:
- Любой другой скрипт React-приложения может случайно перезаписать `select`, `fit`, `wrap`, `DATA` и т.п.
- При re-init (CRIT-1) monkey-patching цепочки накапливаются бесконечно (showSheet вызывает _origShow6 -> _origShow5 -> _origShow3 -> _origShow2 -> _origShow -> первоначальный showSheet, а при повторной загрузке поверх них создаётся ещё один слой)
- `document.addEventListener('keydown', ...)` на строках 1650 и 2318 добавляется без возможности снятия (анонимные функции)

**Рекомендация:** Обернуть весь JS в schema.html в IIFE `(function(){ ... })()` и экспортировать только необходимый API через `window.__schema = { cleanup, refreshPV }`. Долгосрочно — перенести на `<iframe src="/schema.html">` с postMessage API для взаимодействия. Это решает и CRIT-1, и CRIT-2 одним махом.

---

### HIGH

**[HIGH-1] Хрупкая привязка секции 39 к SVG-координатам (rect x="1170" y="308/528/748")**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html:2379,2386`

```javascript
const _GPA_BLOCK_Y={1:'308',2:'528',3:'748'};
wrap.querySelectorAll(`rect[x="1170"][y="${_GPA_BLOCK_Y[g]}"]`)
```

Код ищет блоки ГПА по абсолютным SVG-координатам. Анализ показал, что эти rect находятся внутри `<g class="hot" data-key="gpa100/200/300">` в шаблоне `sh-s1`. Любое изменение SVG (перемещение, масштабирование, экспорт из редактора) сломает привязку бесшумно — `querySelectorAll` вернёт пустой NodeList, без ошибок.

Дополнительный риск: в шаблоне `sh-plan` есть `rect x="1170" y="1080"` и `data-key="gpa100/200/300"` — если бы y=308/528/748 там совпали, произошло бы ложное срабатывание на другом листе (сейчас не совпадают, но хрупкость остаётся).

**Рекомендация:** Заменить координатный селектор на семантический:

```javascript
// Вместо: wrap.querySelectorAll(`rect[x="1170"][y="${_GPA_BLOCK_Y[g]}"]`)
// Использовать:
const gpaGroup = wrap.querySelector(`[data-key="gpa${g}00"]`);
if (gpaGroup) {
  const rect = gpaGroup.querySelector('rect'); // первый rect в группе — фон блока
  rect?.setAttribute('fill', fill);
  rect?.setAttribute('stroke', stroke);
}
```

Или добавить `data-gpa="1/2/3"` атрибут на сами rect в SVG.

---

**[HIGH-2] statesText()/statesTextA() на листе s1 показывают данные только для GPA_SEL, хотя видны все 3 ГПА**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html:2247,2253-2258,2264-2265`

Функция `statesText(prefix)` (строка 2247) вызывает `statesTextA(GPA_SEL, ...)` — то есть всегда для текущего выбранного агрегата. Функция `injectSensors` (переопределённая на строке 2248-2268) проходит по `.sens` элементам и для датчиков типа `S` (status) показывает `statesText(...)`, который возвращает статус только GPA_SEL.

На листе `sh-s1` видны все три блока ГПА (gpa100, gpa200, gpa300). Если GPA_SEL=1, бейджи STATES_GTD/STATES_GPA для ГПА-200 и ГПА-300 показывают статус ГПА-100. Пользователь видит "В РАБОТЕ" на всех трёх агрегатах, даже если ГПА-200 остановлен.

Отдельно: `updatePVCards()` (строка 1551-1565) корректно итерирует `for(let a=1;a<=3;a++)` и использует `statesTextA(a, ...)` — то есть карточки (DATA) показывают правильные значения для каждого агрегата. Проблема только в SVG-бейджах.

**Рекомендация:** В `injectSensors` (секция 31, строки 2248-2268) определять номер агрегата из контекста SVG-элемента (например, ближайший родитель `[data-key]` содержит "gpa1/2/3"), а не из GPA_SEL. Для датчиков на общих листах (не привязанных к конкретному ГПА) показывать все три статуса или использовать отдельный бейдж для каждого агрегата.

---

**[HIGH-3] Рассинхронизация двух копий файла схемы**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html` (864477 байт, MD5: 19dc2127...)
`c:\Users\Timur\Desktop\UTG\КС\cs_4\КС_Ахангаран_схема_1_интерактив.html` (863650 байт, MD5: 8d9b3e1e...)

Файлы различаются на 827 байт и 15 строк. Оригинал (КС_Ахангаран_схема_1_интерактив.html) заканчивается секцией 38 (deep-link). Копия (frontend/public/schema.html) содержит секцию 39 (цвет блоков ГПА), которой нет в оригинале. schema.html был обновлён сегодня в 16:30, оригинал — в 10:10.

Последствия:
- Правки SVG в оригинале не попадут в рабочую копию
- Правки JS (секция 39) в frontend/public/schema.html не попадут в оригинал
- Два разработчика могут одновременно править разные файлы

**Рекомендация:** Определить один файл как source of truth. Варианты:
1. Удалить оригинал из корня, работать только с `frontend/public/schema.html`
2. Сделать build-step (копирование) из оригинала в public при сборке
3. Симлинк: `frontend/public/schema.html` -> `../../КС_Ахангаран_схема_1_интерактив.html`

В любом случае прямо сейчас: перенести секцию 39 в оригинал.

---

**[HIGH-4] Анимация потока газа не учитывает состояние ГПА — поток через остановленный агрегат отображается**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html:1930-1951`

Функция `buildFlow()` анимирует ВСЕ элементы `.pv` и `.flowmove` на текущем листе SVG. Нет проверки, принадлежит ли труба остановленному агрегату. Если ГПА-200 остановлен (STATES_GTD.5=0), но его обвязка содержит `.pv`-пути, анимация «бегущий пунктир» всё равно отображается.

Код `buildFlow()`:
```javascript
svg.querySelectorAll('.pv,.flowmove').forEach(p=>{
  // ...клонирует и добавляет анимированные элементы без фильтрации по агрегату
});
```

Нет логики типа «если parent data-key=gpaXXX и ГПА остановлен — пропустить». Секция 39 красит блоки ГПА по STATES_GTD, но `buildFlow()` вызывается из `applyFlow()` (строка 1952-1959) БЕЗ координации с данными PVDATA.

**Рекомендация:** В `buildFlow()` добавить проверку: для каждого `.pv`-пути определить, принадлежит ли он ГПА (через ближайший `<g>` с `data-key` или через отдельный атрибут `data-gpa`), и если соответствующий STATES_GTD.5 === 0, пропускать элемент. Примерная реализация:

```javascript
svg.querySelectorAll('.pv,.flowmove').forEach(p => {
  const gpaGroup = p.closest('[data-key^="gpa"]');
  if (gpaGroup) {
    const num = gpaGroup.dataset.key.match(/gpa(\d)/)?.[1];
    if (num && (PVDATA[`GPA-${num}.GPA-${num}.STATES_GTD.5`] || 0) < 0.5) return;
  }
  // ...existing spark creation code
});
```

---

### MEDIUM

**[MED-1] Monkey-patching цепочка из 5+ звеньев — хрупкая и необратимая**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html` строки: 1646, 1870, 1961, 2179, 2312 (showSheet); 2248, 2389 (injectSensors); 2069, 2314 (select); 1965 (saveState); 2298 (updatePVBtn)

Функция `showSheet` переопределяется 5 раз через паттерн `const _origShowN=showSheet; showSheet=function(k){_origShowN(k); ...}`. При вызове `showSheet('s1')` выполняется цепочка из 6 функций вложенных вызовов. Если любая из промежуточных функций бросает исключение, последующие декораторы не выполнятся (deep-link не обновится, мини-карта не перерисуется).

`injectSensors` переопределяется 2 раза (секция 31 + секция 39). Порядок критичен: секция 39 (_injSens39) вызывает предыдущую версию, которая включает секцию 31, которая включает оригинал.

**Рекомендация:** Заменить monkey-patching на систему событий или хуков:
```javascript
const hooks = { afterShowSheet: [], afterInjectSensors: [] };
function showSheet(k) { /* original logic */; hooks.afterShowSheet.forEach(fn => fn(k)); }
// Вместо переопределения: hooks.afterShowSheet.push(applyFlow);
```
Это устранит проблему вложенности, порядка и исключений.

---

**[MED-2] Отсутствие очистки event listeners при unmount**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html` строки: 1395, 2316, 2318, 2334, 2345, 1281, 1283, 1370-1389

22 вызова `addEventListener` используют анонимные функции, что делает невозможным вызов `removeEventListener`. При повторном монтировании SchemaPanel.tsx (см. CRIT-1) обработчики накапливаются. Особенно опасны: `window.addEventListener('resize', fit)` (строка 1395) и `window.addEventListener('hashchange', applyHash)` (строка 2316) — они слушают глобальные события.

**Рекомендация:** Сохранять ссылки на все listener-функции и экспортировать cleanup-функцию (см. CRIT-1/CRIT-2). При использовании iframe проблема исчезает автоматически.

---

**[MED-3] localStorage без namespace — коллизии с другими приложениями**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html` строки: 1964, 1966, 1971, 1998

Используются ключи `ks_ahangaran` и `ks_pv` в localStorage без префикса. При размещении нескольких приложений на одном домене (localhost:3000) возможны коллизии.

**Рекомендация:** Добавить префикс приложения: `cs4_ks_ahangaran`, `cs4_ks_pv`.

---

**[MED-4] fetchPVFromAPI жёстко привязан к localhost:8000**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html:1972`

```javascript
const API_BASE='http://localhost:8000';
```

Хардкод URL. При деплое на сервер или изменении порта бэкенда схема перестанет получать живые данные. Ошибка не критична (catch на строке 2000 обрабатывает), но данные будут устаревшими.

**Рекомендация:** Использовать относительный путь (`const API_BASE=''`) или передавать URL через data-атрибут на контейнере, или через `window.__SCHEMA_CONFIG = { apiBase: '...' }` из React-приложения.

---

### LOW

**[LOW-1] console.warn/console.info в продакшен-коде**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html` строки: 2000, 2371, 2372

`console.warn('PV API недоступен:',e.message)` и вывод самопроверки. Не критично, но засоряет консоль.

**Рекомендация:** Обернуть в `if(DEBUG)` или удалить для продакшена.

---

**[LOW-2] Магические числа в CSS-анимации потока**

`c:\Users\Timur\Desktop\UTG\КС\cs_4\frontend\public\schema.html` строки: 1947-1950

```javascript
mk('spk46','#ff7a1a',w+1.6,0.14);
mk('spk36','#ffa11a',w+0.8,0.30);
```

Числа 1.6, 0.14, 0.30 и т.д. не документированы. Подбирались визуально, но при изменении масштаба SVG могут выглядеть некорректно.

**Рекомендация:** Вынести в именованные константы с комментариями.

---

### Позитивные наблюдения

1. **Самопроверка (секция 37)** — грамотный подход: при загрузке валидируются связи NAV_BY_SHEET, GO, SENS, SHEET_TREE, шаблоны. Это защищает от битых ссылок при добавлении листов.

2. **Сравнение агрегатов (секция 27, cmpModal)** — корректно итерирует все 3 ГПА с `for(let a=1;a<=3;a++)`, не зависит от GPA_SEL. Показывает честную таблицу.

3. **updatePVCards()** (строка 1551) — тоже корректно обходит все 3 агрегата для карточек DATA. Проблема GPA_SEL только в SVG-бейджах.

4. **Секция 39** — правильно читает STATES_GTD.5 для каждого ГПА (цикл `for(let g=1;g<=3;g++)`), не зависит от GPA_SEL. Концептуально верное решение, только привязка к координатам хрупкая.

5. **Graceful degradation** — fetchPVFromAPI обёрнут в try/catch, при недоступности API схема продолжает работать со статическими данными.

---

### Топ-5 исправлений (по приоритету)

| # | Severity | Задача | Оценка трудозатрат |
|---|----------|--------|--------------------|
| 1 | CRIT | Перевести инжект schema.html на `<iframe>` вместо innerHTML+script. Это решает утечки таймеров (CRIT-1), глобальное загрязнение (CRIT-2), накопление event listeners (MED-2) одним изменением. Общение через `postMessage`. | 2-4 часа |
| 2 | HIGH | Заменить координатный селектор в секции 39 на `data-key`-якорь (`[data-key="gpa${g}00"] > rect`) и добавить проверку наличия элементов (silent fail -> console.warn при debug). | 15 минут |
| 3 | HIGH | Исправить statesText() в injectSensors (секция 31): на листах с несколькими ГПА определять номер агрегата из SVG-контекста, а не из глобального GPA_SEL. | 1-2 часа |
| 4 | HIGH | Устранить рассинхронизацию файлов: перенести секцию 39 в оригинал, сделать один source of truth, добавить build-step или symlink. | 30 минут |
| 5 | HIGH | В buildFlow() фильтровать анимацию потока для остановленных ГПА: пропускать `.pv`-пути внутри `[data-key]` групп, чей STATES_GTD.5 < 0.5. | 1 час |

---

### Вердикт

**REQUEST CHANGES** -- обнаружены 2 CRITICAL и 4 HIGH проблемы. Наиболее опасны утечка таймеров при re-mount в React (CRIT-1) и глобальное загрязнение namespace (CRIT-2). Рекомендация #1 (iframe) решает оба CRIT и один MED одним рефакторингом.