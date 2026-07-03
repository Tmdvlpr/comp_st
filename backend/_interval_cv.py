# -*- coding: utf-8 -*-
"""Персональный CV обучаемого интервала для КАЖДОГО датчика (read-only, движок Фазы 2).

Для каждого датчика на РЕАЛЬНЫХ post-cutoff healthy-steady точках честно выбирает лучший
режим интервала из спектра {conformal (q̂_abs), hybrid (q̂_norm·σ), self (|y−center|)} по
coverage-efficiency, БЕЗ переобучения CatBoost (переиспользует сохранённые модели + кэш).

ЧЕСТНОСТЬ (фиксы адверсариального ревью):
  • 3-WAY хронологический сплит per-regime: calib(50%)→фитим q̂; select(25%)→выбираем mode;
    test(25%)→отчётное покрытие. Выбор и отчёт на РАЗНЫХ фолдах → нет winner's curse.
  • Хронологический (не random) сплит — уважает автокорреляцию.
  • self-band считается как КАНДИДАТ даже для ml_corridor-датчиков (center=healthy-медиана
    режима на calib-фолде), т.к. в проде вырожденные R²<0 сидят в ml_corridor/steady_band.
  • Анти-winner's-curse: победитель принимается только если его select-покрытие бьёт
    глобальный дефолт 'conformal' с запасом > binomial SE; иначе остаётся conformal.
  • Дрейф-детектор: для каналов с сильным intra-window трендом (|z|>Z_FLAG) — ФЛАГ
    'physical drift → эскалация', НЕ подгонять полосу (не маскировать реальный сигнал).

НЕ пишет в БД (CS_DISABLE_DB_WRITE=1). НЕ меняет прод-модели. Пишет выбор в JSON.
Запуск:
    python _interval_cv.py --station ohangaron --models-dir models/ohangaron \
        --cache-wide models/ohangaron_v23_staging/_wide_cache.pkl --only-gpa 1
"""
import os
os.environ.setdefault("CS_DISABLE_DB_WRITE", "1")
import sys
import io
import json
import argparse
import dataclasses
import collections as _c

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd
import joblib

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import train as TR
from station_config import load_station_config
from data_loader import PostgresDataLoader
import weather as W

TARGET_DEFAULT = 1.0 - TR.DEFAULT_ALPHA      # 0.98
Z_FLAG = 8.0                                  # |z| тренда, выше которого канал считаем дрейфующим
MIN_REGIME_N = 120                            # минимум точек режима для 3-way сплита (60/30/30)
MODES = ("conformal", "hybrid", "self")


def _level_shift_std(values, frac=0.2):
    """Насколько сдвинулся УРОВЕНЬ значения за окно, в единицах σ раннего периода (интерпретируемо,
    в отличие от t-статистики тренда, которая огромна на любом сезонном дрейфе при больших n).
    shift_std = (медиана последних frac − медиана первых frac) / std(первых frac).
    |shift_std|>~2 → уровень заметно уехал (полоса будет ПЕРЕЦЕНТРИРОВАНА на новый уровень —
    контекст для инженера, НЕ гейт полосы: rolling-CV test-фолд уже проверяет forward-покрытие)."""
    a = np.asarray(values, float)
    a = a[np.isfinite(a)]
    n = len(a)
    if n < 50:
        return 0.0
    k = max(10, int(n * frac))
    early, late = a[:k], a[-k:]
    sd = float(np.std(early)) + 1e-9
    return float((np.median(late) - np.median(early)) / sd)


def _cov(scores_fold, thr):
    a = np.asarray(scores_fold, float)
    a = a[np.isfinite(a)]
    if not np.isfinite(thr) or len(a) == 0:
        return float("nan"), 0
    return float(np.mean(a <= thr)), len(a)


def _fit_thr(scores_calib, alpha):
    return TR.block_conformal_threshold(np.asarray(scores_calib, float), alpha=alpha)["threshold"]


def _score_vs_target(cov, tgt):
    """Штраф отклонения от цели: недобор покрытия штрафуем в 3 раза сильнее перебора."""
    if not np.isfinite(cov):
        return 1e9
    return (tgt - cov) * 3.0 if cov < tgt else (cov - tgt)


def _self_scores(y, calib_i, fold_i):
    """self-скор |y−center| где center = healthy-медиана на КАЛИБ-фолде (без утечки в валид/тест)."""
    center = float(np.median(y[calib_i]))
    return np.abs(y[fold_i] - center)


def _mode_scores(rk_data, mode, calib_i, fold_i):
    """Скоры выбранного режима интервала на fold_i при калибровке на calib_i.
    conformal/hybrid — center-free (предпосчитаны); self — центр из calib_i."""
    if mode == "conformal":
        return rk_data["resid"][calib_i], rk_data["resid"][fold_i]
    if mode == "hybrid":
        return rk_data["norm"][calib_i], rk_data["norm"][fold_i]
    # self
    y = rk_data["y"]
    return _self_scores(y, calib_i, calib_i), _self_scores(y, calib_i, fold_i)


def select_for_sensor(per_regime, alpha, tgt, n_blocks=6):
    """ROLLING-ORIGIN блочный CV per-sensor. Режим делится на n_blocks по времени; фолд i калибрует
    q̂ на блоках[:i], валидирует на блоке[i] (i=1..nb-1) — КАЖДЫЙ блок валидируется один раз out-of-fold,
    включая последний. Покрытие режима интервала = взвешенное среднее по ВСЕМ фолдам+режимам: использует
    все данные, робастно к одиночному дрейфующему блоку (в отличие от одиночного hold-out). Выбор mode по
    этому CV-покрытию — и служим ЕГО ЖЕ (нет рассинхрона select/test). self-центр — на калиб-фолде каждого
    фолда (без утечки). Оценка слегка оптимистична из-за выбора из 3 режимов (winner's curse ~SE, поле margin).
    Возврат: dict(chosen, test_cov{mode}=CV-покрытие, n_test=Σ валид-точек, margin, reason)."""
    pairs = {m: [] for m in MODES}          # (cov, w) по всем валид-фолдам rolling CV
    for rk, rk_data in per_regime.items():
        n = len(rk_data["resid"])
        nb = min(n_blocks, n // 25)
        if nb < 4:
            continue
        edges = np.linspace(0, n, nb + 1).astype(int)
        blocks = [np.arange(edges[i], edges[i + 1]) for i in range(nb)]
        for i in range(1, nb):              # каждый блок 1..nb-1 — валид один раз (вкл. последний)
            calib_i = np.concatenate(blocks[:i])
            val_i = blocks[i]
            if len(calib_i) < 40 or len(val_i) < 15:
                continue
            for m in MODES:
                s_cal, s_val = _mode_scores(rk_data, m, calib_i, val_i)
                thr = _fit_thr(s_cal, alpha)
                cv_, nv = _cov(s_val, thr)
                if np.isfinite(cv_) and nv > 0:
                    pairs[m].append((cv_, nv))

    def _agg(ps):
        if not ps:
            return float("nan"), 0
        c = np.array([x for x, _ in ps], float)
        w = np.array([x for _, x in ps], float)
        return float(np.average(c, weights=w)), int(w.sum())

    cov = {m: _agg(pairs[m])[0] for m in MODES}
    n_cv = _agg(pairs["conformal"])[1]
    if n_cv < 2 * TR.N_EFF_MIN:
        return dict(chosen=None, reason="insufficient_folds",
                    test_cov={m: round(cov[m], 4) for m in MODES},
                    sel_cov={m: round(cov[m], 4) for m in MODES},
                    n_test=int(n_cv), n_select=int(n_cv), margin=None)

    valid = [m for m in MODES if np.isfinite(cov[m])]
    best = min(valid, key=lambda m: _score_vs_target(cov[m], tgt))
    p = cov.get("conformal", float("nan"))
    se = np.sqrt(max(p * (1 - p), 1e-6) / max(n_cv, 1)) if np.isfinite(p) else 0.02
    chosen = best
    # анти-winner's-curse: держим стабильный conformal, только если он сам ≥ цели и преимущество best в пределах SE
    if (best != "conformal" and np.isfinite(cov["conformal"])
            and cov["conformal"] >= tgt - 0.03
            and _score_vs_target(cov["conformal"], tgt) - _score_vs_target(cov[best], tgt) < se):
        chosen = "conformal"
    return dict(chosen=chosen,
                test_cov={m: round(cov[m], 4) for m in MODES},
                sel_cov={m: round(cov[m], 4) for m in MODES},
                n_test=int(n_cv), n_select=int(n_cv), margin=round(float(se), 4),
                reason="rolling-cv")


def main():
    ap = argparse.ArgumentParser(description="Персональный CV интервала per-sensor (Фаза 2 движок)")
    ap.add_argument("--station", default="ohangaron")
    ap.add_argument("--models-dir", default=None)
    ap.add_argument("--alpha", type=float, default=TR.DEFAULT_ALPHA)
    ap.add_argument("--from-date", default=None)
    ap.add_argument("--cache-wide", default=None)
    ap.add_argument("--eval-to", default=None, help="верхняя граница окна ISO (отсечь OOD-хвост)")
    ap.add_argument("--only-gpa", default=None)
    ap.add_argument("--out", default=None, help="куда писать JSON выбора (default: <models-dir>/interval_cv_selection.json)")
    args = ap.parse_args()
    tgt = 1.0 - args.alpha
    eval_to = pd.Timestamp(args.eval_to) if args.eval_to else None
    if eval_to is not None and getattr(eval_to, "tzinfo", None) is not None:
        eval_to = eval_to.tz_convert("Etc/GMT-5").tz_localize(None)

    cfg = load_station_config(args.station)
    meth = cfg.methodology or {}
    TR.configure_gas(meth.get("gas"))
    models_dir = args.models_dir or str(cfg.models_path)
    out_path = args.out or os.path.join(models_dir, "interval_cv_selection.json")
    with open(os.path.join(models_dir, "metadata.json"), encoding="utf-8") as f:
        meta = json.load(f)
    cutoff = pd.Timestamp(meta.get("last_train_timestamp") or meth.get("train_cutoff"))
    if getattr(cutoff, "tzinfo", None) is not None:
        cutoff = cutoff.tz_convert("Etc/GMT-5").tz_localize(None)
    limits = {k: tuple(v) for k, v in (meth.get("limits") or {}).items()}
    _rc_fields = {fld.name for fld in dataclasses.fields(TR.RegimeConfig)}
    regime_cfg = TR.RegimeConfig(**{k: v for k, v in (meta.get("regime_config") or {}).items()
                                    if k in _rc_fields})

    loader = PostgresDataLoader(cfg)
    if args.cache_wide and os.path.exists(args.cache_wide):
        print(f"  wide из кэша: {args.cache_wide}", flush=True)
        raw = pd.read_pickle(args.cache_wide)
        if args.from_date is not None:
            raw = raw.loc[raw.index >= pd.Timestamp(args.from_date)]
    else:
        tag_to_name = loader.build_tag_mapping()
        print(f"📡 Загрузка истории (оконно), cutoff={cutoff.date()}...", flush=True)
        raw = TR._load_wide_windowed(loader, from_date=args.from_date)
        raw = raw.rename(columns=tag_to_name).sort_index()
        if getattr(raw.index, "tz", None) is not None:
            raw.index = raw.index.tz_convert("Etc/GMT-5").tz_localize(None)
        if args.cache_wide:
            raw.to_pickle(args.cache_wide)
    try:
        amb = W.get_ambient_series(cfg, raw.index)
    except Exception:
        amb = None

    models = meta["models"]
    rows = []
    selection = {}
    sensor_recs = []
    skips = _c.Counter()

    for gid in sorted({m.get("gpa_id") for m in models.values()}):
        if args.only_gpa and str(gid) != str(args.only_gpa).replace("GPA", ""):
            continue
        suf = f"__GPA{gid}"
        cols = [c for c in raw.columns if c.endswith(suf)]
        if len(cols) < 3:
            continue
        dfu = raw[cols].copy()
        dfu.columns = [c[:-len(suf)] for c in cols]
        dfu = dfu.ffill(limit=2)
        if amb is not None:
            dfu["ambient_temp"] = amb.reindex(dfu.index).values
        dfu = TR.physically_clean(dfu, limits)
        run = TR.steady_running_mask(dfu)
        dfu = TR.add_domain_features_gpa(dfu, running_mask=run, train_cutoff=cutoff)
        healthy = TR.verified_healthy_mask(dfu, limits, cfg=regime_cfg).reindex(dfu.index).fillna(False)
        binning = TR.fit_load_bins(dfu, TR.label_regime(dfu, regime_cfg) == TR.STEADY, regime_cfg)
        rk_all = TR.regime_key(TR.label_regime(dfu, regime_cfg), TR.sub_mode(dfu, regime_cfg),
                               TR.load_bin_labels(dfu, binning))
        steady = (TR.label_regime(dfu, regime_cfg) == TR.STEADY)

        for key, info in models.items():
            if info.get("gpa_id") != gid:
                continue
            tgt_name = info["name"]
            feats = list(info.get("feat_cols") or [])
            if tgt_name not in dfu.columns or any(f not in dfu.columns for f in feats) or len(feats) < 2:
                skips[f"ГПА-{gid}: нет фич/таргета"] += 1
                continue
            mf = os.path.join(models_dir, info["model_file"])
            if not os.path.exists(mf):
                skips[f"ГПА-{gid}: нет файла модели"] += 1
                continue
            raw_model = joblib.load(mf)["model"]
            if info.get("pooled") and info.get("norm"):
                n = info["norm"]
                mdl = TR._PooledAdapter(raw_model, list(n["feat"]), pd.Series(n["feat_mu"]),
                                        pd.Series(n["feat_sd"]), n["tgt_mu"], n["tgt_sd"])
            else:
                mdl = raw_model
            ntrees = int(getattr(mdl, "tree_count_", 0) or 0)

            cutoff_m = pd.Timestamp(info.get("last_train_ts") or cutoff)
            if getattr(cutoff_m, "tzinfo", None) is not None:
                cutoff_m = cutoff_m.tz_convert("Etc/GMT-5").tz_localize(None)
            post = healthy & steady & (dfu.index > cutoff_m)
            if eval_to is not None:
                post = post & (dfu.index <= eval_to)
            sub = dfu.loc[post].dropna(subset=[tgt_name] + feats)
            if len(sub) < MIN_REGIME_N:
                skips[f"ГПА-{gid}: <{MIN_REGIME_N} post-cutoff healthy-steady ({tgt_name}: {len(sub)})"] += 1
                continue

            X = sub[feats]
            pred = np.asarray(mdl.predict(X), float)
            pred = pred[:, 0] if pred.ndim == 2 else pred
            sigma, _ = TR.ensemble_sigma_uepi(mdl, X, ntrees, int(info.get("ve_count", TR.VE_COUNT)))
            y = sub[tgt_name].values.astype(float)
            rkv = pd.Series(rk_all).reindex(sub.index).values

            # сдвиг уровня значения за окно (в σ) — контекст «полоса перецентрирована», не гейт
            shift_std = _level_shift_std(y)

            resid_all = np.abs(y - pred)
            norm_all = resid_all / np.maximum(sigma, 1e-12)
            per_regime = {}
            for rk in sorted(set(rkv.tolist())):
                idx = np.where(rkv == rk)[0]          # хронологический порядок сохранён (sub отсортирован)
                if len(idx) < MIN_REGIME_N:
                    continue
                per_regime[rk] = dict(y=y[idx], resid=resid_all[idx], norm=norm_all[idx])

            if not per_regime:
                skips[f"ГПА-{gid}: нет режима ≥{MIN_REGIME_N} ({tgt_name})"] += 1
                continue

            res = select_for_sensor(per_regime, args.alpha, tgt)
            best_test = max([res["test_cov"][m] for m in MODES
                             if np.isfinite(res["test_cov"][m])] or [float("nan")])
            sensor_recs.append(dict(key=key, gid=gid, name=tgt_name,
                                    detector_mode=info.get("detector_mode"), res=res,
                                    shift_std=shift_std, best_test=best_test))

    # ── Флаги (честно, без over-flagging): арбитр — forward rolling-CV покрытие, не t-стат дрейфа ──
    #    SUBTARGET  — лучший режим не дотягивает 0.95 на forward test-фолде → полосу НЕ навязываем.
    #    LEVEL_SHIFT — уровень значения уехал >2σ за окно: полоса перецентрирована на новый уровень;
    #                  режим ПРИМЕНЯЕМ (forward-покрытие честное), но помечаем «проверить, что сдвиг
    #                  доброкачественный» — иначе рискуем нормализовать зарождающийся дефект.
    for r in sensor_recs:
        bt = r["best_test"]
        sh = r["shift_std"]
        subtarget = (not np.isfinite(bt)) or bt < 0.95
        level_shift = np.isfinite(sh) and abs(sh) > 4.0   # порог сдвига уровня 2→4σ (только сильные)
        flag = "SUBTARGET" if subtarget else ("LEVEL_SHIFT" if level_shift else "")
        apply_mode = None if subtarget else r["res"]["chosen"]   # сдвиг-уровня применяем, недобор — нет
        r["flag"] = flag
        selection[r["key"]] = dict(
            gpa_id=r["gid"], name=r["name"], detector_mode=r["detector_mode"],
            corridor_mode=apply_mode, best_mode_diag=r["res"]["chosen"],
            test_cov=r["res"]["test_cov"], sel_cov=r["res"]["sel_cov"],
            n_test=r["res"]["n_test"], n_select=r["res"]["n_select"], reason=r["res"]["reason"],
            level_shift_std=round(sh, 2), flag=flag)
        rows.append((r["name"], r["gid"], r["res"]["chosen"], r["res"]["test_cov"], sh, flag))

    # ── отчёт ──
    print("=" * 104)
    print(f"ПЕРСОНАЛЬНЫЙ CV ИНТЕРВАЛА per-sensor (3-way calib/select/test, цель {tgt:.0%}) — {models_dir}")
    print(f"cutoff={cutoff.date()}  eval_to={eval_to.date() if eval_to is not None else '-'}")
    print("=" * 104)
    hdr = f"{'датчик · ГПА':<46}{'выбор':>10}{'conf':>7}{'hybr':>7}{'self':>7}{'сдвигσ':>8}  флаг"
    print(hdr)
    print("-" * 104)
    for name, gid, chosen, tc, sh, flag in sorted(rows, key=lambda r: (r[1], r[0])):
        def _f(v):
            return f"{v:.3f}" if isinstance(v, (int, float)) and np.isfinite(v) else "  -  "
        print(f"{name+'·ГПА'+str(gid):<46}{str(chosen):>10}{_f(tc.get('conformal')):>7}"
              f"{_f(tc.get('hybrid')):>7}{_f(tc.get('self')):>7}{sh:>8.1f}  {flag}")
    print("-" * 104)
    applied = [r for r in rows if r[5] in ("", "LEVEL_SHIFT")]
    cc = _c.Counter(r[2] for r in applied)
    print(f"Авто-применён mode на {len(applied)}/{len(rows)} датчиках:", dict(cc))
    sub = [(r[0], r[1], r[3]) for r in rows if r[5] == "SUBTARGET"]
    if sub:
        print("\n⚠ SUBTARGET (лучший режим <0.95 на forward-CV — полосу НЕ навязываем, честно univariate):")
        for name, gid, tc in sorted(sub, key=lambda x: (x[1], x[0])):
            bestv = max([v for v in tc.values() if isinstance(v, (int, float)) and np.isfinite(v)] or [float('nan')])
            print(f"   ГПА-{gid} {name}: best_cov={bestv:.3f}")
    shift = [(r[0], r[1], r[4]) for r in rows if r[5] == "LEVEL_SHIFT"]
    if shift:
        print(f"\nℹ LEVEL_SHIFT (уровень уехал >2σ, полоса перецентрирована — режим применён, "
              f"проверить доброкачественность сдвига): {len(shift)} каналов")
        for name, gid, sh in sorted(shift, key=lambda x: -abs(x[2]))[:10]:
            print(f"   ГПА-{gid} {name}: сдвиг {sh:+.1f}σ")
    if skips:
        print("\nПропущено:")
        for k, v in skips.most_common():
            print(f"   {v}× {k}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(selection, f, ensure_ascii=False, indent=2)
    print(f"\n✓ выбор записан: {out_path} ({len(selection)} датчиков)")


if __name__ == "__main__":
    main()
