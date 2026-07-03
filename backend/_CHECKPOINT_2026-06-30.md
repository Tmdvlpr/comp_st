# ЧЕКПОИНТ 2026-06-30 — v23 normalized-conformal + pooled-по-типу (выкатка не завершена)

## TL;DR
Метод переписан на **v23 normalized-conformal + epistemic**, добавлен **pooled-по-типу** (термо/мех =
кросс-ГПА общая модель на z-норме; вибро = per-unit). Код готов и протестирован (pytest 38/38).
**ПРОД ВСЁ ЕЩЁ CQR v22 — свап НЕ сделан.** Прод-кандидат обучен и валидирован, лежит в
`models/ohangaron_v23_pooled/`. Завтра: выполнить деструктивную выкатку (шаги 3–7 ниже).

## Что СДЕЛАНО (код, всё компилируется, pytest 38/38)
- **`backend/train.py`** — единый файл методологии (инлайн regime/calibrator/domain_features/
  data_quality). v23: CatBoost RMSEWithUncertainty+posterior_sampling; σ=√(u_epi+u_ale) из
  virtual_ensembles; нормализованный block-Mondrian conformal `s=|факт−предикт|/σ`; **dual-коридор**
  (хранятся оба порога: `threshold`=q̂_norm для hybrid, `threshold_abs`=q̂_abs для conformal);
  эпистемика κ=p95(u_epi). **POOLED**: `train_sensor_pooled` + `_PooledAdapter` (де-норм в сырые
  единицы ГПА) + `_finalize_calibration`; маршрутизация `is_pooled_target(name, meth)` по маске
  `config.methodology.pooling`. Опции CLI: `--cutoff-date --from-date --gpa --output-dir
  --per-unit-cutoff --cache-wide`. `train_all` строит общий __POOLED.joblib + per-ГПА записи с norm.
- **`backend/live_predict.py`**:
  - `reprocess_history(from,to)` — оконный пересчёт, пишет ВСЕ derived (через `_write_live_state(historical=True)`:
    health/anomalies_t/журнал/predictions/domain за ПОЛНОЕ окно). **catch_up_missing переведён на неё**
    → самовосстановление: рестарт после простоя досчитает пропуск без дыр.
  - CLI `--reprocess-from <ISO> [--reprocess-to <ISO>]` — разовая полная перезапись.
  - **pooled-инференс**: `mdl` оборачивается в `TR._PooledAdapter` по `info['norm']` (норм входа →
    predict → де-норм). Потоково, проверено фактом (предикты в сырых единицах корректны).
  - **ОБА коридора в выходе**: активный → `lo/hi`+детекция; альтернативный → `series[].lo2/hi2`
    (UI-тумблер). Переключатель `CS_CORRIDOR_MODE` > cfg.methodology.corridor_mode > metadata > 'conformal'.
  - **эпистемика в выходе**: `series[].e` (u_epi) + `state_sensors[].epistemic_thr` (для фиолет-полосы).
  - FIX: `regime_mask` (детектор-2 новизна) теперь реально пишется (был заглушкой False).
  - FIX: `_shap_top_for_event(info=)` — для pooled SHAP считается на НОРМАЛИЗОВАННОМ входе.
  - `load_models_and_metadata` кэширует общий __POOLED.joblib (грузит 1×).
- **`config/stations/ohangaron.yaml`** → `methodology.pooling` (маска): `enabled:true, per_unit:["vibro"], pooled:[]`.
  `corridor_mode` дефолт = **conformal** (по решению юзера; hybrid = «кнопка» через env/cfg).
- **Удалены** (бэкап `cs_4/_legacy_backend_backup_2026-06-30.tar.gz`): train_v2.py,
  train_and_save_models.py, regime.py, calibrator.py, domain_features.py, data_quality.py, CQR/staging
  `_*.py`. `detection_methods.py` остался (EWMA/CUSUM, инференс).
- **Новые скрипты** (read-only, из кэша): `_validate_corridors.py` (held-out покрытие, pooled-aware,
  `--eval-to --only-gpa --cache-wide`), `_unit_segments.py`, `_pooled_experiment{,2,3}.py`, `_shap_top5.py`.

## Артефакты на диске
- **`models/ohangaron_v23_pooled/`** — ★ ПРОД-КАНДИДАТ. 48 моделей, **36 ml_corridor** (24 pooled термо/мех
  + 12 vibro per-unit), 9 общих `__POOLED.joblib` + 21 per-unit, norm-параметры в metadata,
  `corridor_mode=conformal`, cutoff 2026-06-12.
- `models/ohangaron_v23_staging/` — ранний per-unit-only v23 + **`_wide_cache.pkl`** (кэш wide, 02-02→30-06;
  переиспользовать `--cache-wide models/ohangaron_v23_staging/_wide_cache.pkl` чтобы не качать БД).
- `models/ohangaron.backup.v22cqr.2026-06-30/` — бэкап прода (CQR v22) для отката (315 файлов).
- `_db_backup_2026-06-30/*.csv` — бэкап derived-таблиц (anomalies, anomalies_t, journal, predictions,
  domain, raw_data_health) для отката.
- `shap_top5_per_model.json` — топ-5 SHAP по каждой из 48 моделей (деливерабл юзеру).

## Итоги валидации (held-out 60/40, цель 98%)
- Коридор: дефолт **conformal** (оба хранятся, переключаемы). hybrid ≈ conformal на in-distr, hybrid
  лучше на GPA-1 OOD (0.936 vs 0.910; родной участок GPA-1 — 0.977).
- Pooled-по-типу: **36 ml_corridor (было 31 per-unit)**; GPA-1 термо вытащен **9/9**; polytropic_head
  nMAE **0.171→0.044**. Итог покрытия pooled: GPA-1 0.90 / GPA-2 0.95 / GPA-3 0.91 (conformal).
- Контекст-фичи (run_gpa1/2/3 + calc_gpa, эксп-3): **ОТКЛОНЕНЫ** — нестабильны, местами хуже.
- GPA-1 июнь = ambient-OOD (SHAP подтвердил: термо-датчики на 30-44% от ambient_temp). Само-исцелится
  летним ретрейном; сейчас ловится OOD-гейтом→univariate.

## PENDING — ВЫКАТКА (завтра), шаги 3–7. Монитор НЕ запущен (lock устаревший, single_instance разрулит)
3. Свап: `cp -r models/ohangaron_v23_pooled/* models/ohangaron/` (НЕ копировать `_wide_cache.pkl`;
   `rm -f models/ohangaron/*__q.joblib`). Прод-бэкап уже есть.
4. `../venv/Scripts/python.exe migrate_db.py --station ohangaron`, затем одной транзакцией:
   `TRUNCATE ohangaron.anomalies_t, ohangaron.anomalies, ohangaron."journal notifications",
   ohangaron.predictions, ohangaron.domain; UPDATE ohangaron.raw_data SET health=NULL;`
5. Регенерация всей истории под pooled-моделью (пишет оба коридора + SHAP + всё derived):
   `../venv/Scripts/python.exe live_predict.py --station ohangaron --reprocess-from 2026-02-02`
   (idempotent, ON CONFLICT; читает БД окнами ~10-20 мин; CS_DISABLE_DB_WRITE НЕ ставить).
6. [ЮЗЕР, его терминал] перезапустить монитор: `scripts/start_live_monitor.bat`.
7. Верификация: count/min/max по derived-таблицам; `_wf_verify_api.py`; тест простоя (стоп→пуск→
   catch_up досчитал пропуск во ВСЕХ таблицах).
- Откат: `cp -r models/ohangaron.backup.v22cqr.2026-06-30/* models/ohangaron/` + восстановить таблицы из
  `_db_backup_2026-06-30/*.csv` (TRUNCATE + \COPY FROM).

## Открытые хвосты (не блокеры)
- **Фронт** (UI «потом»): отрисовать фиолетовую полосу эпистемики снизу графика (`series[].e`+`epistemic_thr`)
  и тумблер коридора conformal↔hybrid (`series[].lo2/hi2`). Данные уже пишутся.
- `main.py` `/explain` — возможно нужен такой же pooled-SHAP-norm фикс, как в `_shap_top_for_event` (паритет).
- БД `10.1.30.164` периодически отваливается (был таймаут) — регенерацию гонять при стабильной связи;
  оконный fetch с ретраями устойчив, при обрыве окна reprocess пропускает и идёт дальше (можно перезапустить).

## Команды быстрого старта (завтра)
```
cd cs_4/backend
# проверить связь с БД:
../venv/Scripts/python.exe -c "from station_config import load_station_config,get_db_connection as g; c=load_station_config('ohangaron');
import time; t=time.time();
exec('with g(c) as cc, cc.cursor() as cur:\n cur.execute(\"SELECT 1\"); print(\"DB OK\", time.time()-t)')"
# тесты:
CS_DISABLE_DB_WRITE=1 ../venv/Scripts/python.exe -m pytest tests/ -q
# далее шаги 3–7 выкатки (см. выше)
```
План выкатки: `~/.claude/plans/lucky-wishing-kettle.md`.
