# -*- coding: utf-8 -*-
"""Read-only валидация ОБОИХ коридоров для выбора corridor_mode в проде.

Честный held-out split на РЕАЛЬНЫХ данных: post-cutoff healthy-steady точки делятся 60/40
ПО ВРЕМЕНИ внутри каждого ok-режима; q̂ пересчитывается на calib(60%), покрытие меряется на
test(40%) для ОБОИХ коридоров:
  • conformal — |факт−предикт| ≤ q̂_abs           (плоский порог)
  • hybrid    — |факт−предикт| ≤ q̂_norm·σ         (нормализованный, σ из виртуального ансамбля)
Цель покрытия ≈ 1−α (по умолчанию 98%). Кто ближе к цели и с меньшим FP — тот и дефолт.

НЕ пишет в БД (CS_DISABLE_DB_WRITE=1). Запуск:
    python _validate_corridors.py --station ohangaron --models-dir models/ohangaron_v23_staging
"""
import os
os.environ.setdefault("CS_DISABLE_DB_WRITE", "1")     # страховка: только чтение из БД
import sys
import io
import json
import argparse
import dataclasses

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


def main():
    ap = argparse.ArgumentParser(description="Held-out валидация коридоров conformal vs hybrid")
    ap.add_argument("--station", default="ohangaron")
    ap.add_argument("--models-dir", default=None, help="папка моделей (default: прод cfg.models_path)")
    ap.add_argument("--alpha", type=float, default=TR.DEFAULT_ALPHA)
    ap.add_argument("--from-date", default=None, help="нижняя граница загрузки (default: min в БД)")
    ap.add_argument("--cache-wide", default=None, help="pickle-кэш wide (читать если есть, иначе создать)")
    ap.add_argument("--eval-to", default=None, help="верхняя граница held-out окна ISO (отсечь OOD-хвост)")
    ap.add_argument("--only-gpa", default=None, help="валидировать только этот ГПА")
    args = ap.parse_args()
    eval_to = pd.Timestamp(args.eval_to) if args.eval_to else None
    if eval_to is not None and getattr(eval_to, "tzinfo", None) is not None:
        eval_to = eval_to.tz_convert("Etc/GMT-5").tz_localize(None)

    cfg = load_station_config(args.station)
    meth = cfg.methodology or {}
    TR.configure_gas(meth.get("gas"))
    models_dir = args.models_dir or str(cfg.models_path)
    with open(os.path.join(models_dir, "metadata.json"), encoding="utf-8") as f:
        meta = json.load(f)
    cutoff = pd.Timestamp(meta.get("last_train_timestamp") or meth.get("train_cutoff"))
    if getattr(cutoff, "tzinfo", None) is not None:
        cutoff = cutoff.tz_convert("Etc/GMT-5").tz_localize(None)
    limits = {k: tuple(v) for k, v in (meth.get("limits") or {}).items()}
    _rc_fields = {f.name for f in dataclasses.fields(TR.RegimeConfig)}
    regime_cfg = TR.RegimeConfig(**{k: v for k, v in (meta.get("regime_config") or {}).items()
                                    if k in _rc_fields})

    loader = PostgresDataLoader(cfg)
    if args.cache_wide and os.path.exists(args.cache_wide):
        print(f"  wide из кэша: {args.cache_wide}", flush=True)
        raw = pd.read_pickle(args.cache_wide)
    else:
        tag_to_name = loader.build_tag_mapping()
        print(f"📡 Загрузка истории (оконно) для валидации, cutoff={cutoff.date()}...", flush=True)
        raw = TR._load_wide_windowed(loader, from_date=args.from_date)
        raw = raw.rename(columns=tag_to_name).sort_index()
        if getattr(raw.index, "tz", None) is not None:
            raw.index = raw.index.tz_convert("Etc/GMT-5").tz_localize(None)
        if args.cache_wide:
            raw.to_pickle(args.cache_wide)
            print(f"  wide закэширован: {args.cache_wide} {raw.shape}", flush=True)
    try:
        amb = W.get_ambient_series(cfg, raw.index)
    except Exception:
        amb = None

    import collections as _c
    models = meta["models"]
    agg = {"conformal": [], "hybrid": []}      # списки (n_test, n_covered)
    rows = []                                   # (key, regime, n_test, cov_conf, cov_hybr)
    ml_per_gpa = _c.Counter()                   # ml_corridor моделей на ГПА (из metadata)
    eval_sensors = _c.defaultdict(set)          # датчики, давшие ≥1 held-out оценку
    skips = _c.Counter()                        # причины пропуска

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

        for key, info in models.items():
            if info.get("gpa_id") != gid or info.get("detector_mode") != "ml_corridor":
                continue
            ml_per_gpa[gid] += 1
            tgt = info["name"]
            feats = list(info.get("feat_cols") or [])
            if tgt not in dfu.columns or any(f not in dfu.columns for f in feats) or len(feats) < 2:
                skips[f"ГПА-{gid}: нет фич/таргета в данных"] += 1
                continue
            mf = os.path.join(models_dir, info["model_file"])
            if not os.path.exists(mf):
                skips[f"ГПА-{gid}: нет файла модели"] += 1
                continue
            raw_model = joblib.load(mf)["model"]
            if info.get("pooled") and info.get("norm"):
                n = info["norm"]                      # pooled: оборачиваем в адаптер (де-норм в сырые единицы ГПА)
                mdl = TR._PooledAdapter(raw_model, list(n["feat"]), pd.Series(n["feat_mu"]),
                                        pd.Series(n["feat_sd"]), n["tgt_mu"], n["tgt_sd"])
            else:
                mdl = raw_model
            ntrees = int(getattr(mdl, "tree_count_", 0) or 0)
            art = TR.CalibrationArtifact.from_dict(info.get("calibration") or {})

            cutoff_m = pd.Timestamp(info.get("last_train_ts") or cutoff)
            if getattr(cutoff_m, "tzinfo", None) is not None:
                cutoff_m = cutoff_m.tz_convert("Etc/GMT-5").tz_localize(None)
            post = healthy & (dfu.index > cutoff_m)
            if eval_to is not None:
                post = post & (dfu.index <= eval_to)
            sub = dfu.loc[post].dropna(subset=[tgt] + feats)
            if len(sub) < 100:
                skips[f"ГПА-{gid}: <100 healthy-steady в held-out окне (n={len(sub)})"] += 1
                continue
            X = sub[feats]
            pred = np.asarray(mdl.predict(X), float)
            pred = pred[:, 0] if pred.ndim == 2 else pred
            sigma, _ = TR.ensemble_sigma_uepi(mdl, X, ntrees, int(info.get("ve_count", TR.VE_COUNT)))
            y = sub[tgt].values
            rkv = rk_all.reindex(sub.index).values

            _ev = False
            for rkey in sorted(set(rkv.tolist())):
                rc = art.by_regime.get(rkey)
                if not rc or rc.get("decision") != "ok":
                    continue
                idx = np.where(rkv == rkey)[0]
                if len(idx) < 100:
                    continue
                ncal = int(len(idx) * 0.6)
                cal, tst = idx[:ncal], idx[ncal:]
                if len(tst) < 30:
                    continue
                s_cal = np.maximum(sigma[cal], 1e-12)
                r_cal = np.abs(y[cal] - pred[cal])
                q_abs = TR.block_conformal_threshold(r_cal, alpha=args.alpha)["threshold"]
                q_norm = TR.block_conformal_threshold(r_cal / s_cal, alpha=args.alpha)["threshold"]
                r_t = np.abs(y[tst] - pred[tst])
                s_t = np.maximum(sigma[tst], 1e-12)
                cov_c = float(np.mean(r_t <= q_abs)) if np.isfinite(q_abs) else float("nan")
                cov_h = float(np.mean(r_t <= q_norm * s_t)) if np.isfinite(q_norm) else float("nan")
                if np.isfinite(cov_c):
                    agg["conformal"].append((len(tst), cov_c * len(tst)))
                if np.isfinite(cov_h):
                    agg["hybrid"].append((len(tst), cov_h * len(tst)))
                rows.append((key, rkey, len(tst), round(cov_c, 4), round(cov_h, 4)))
                _ev = True
            if _ev:
                eval_sensors[gid].add(key)
            else:
                skips[f"ГПА-{gid}: post-cutoff режим не совпал с ok-полосой / мало точек"] += 1

    print("=" * 88)
    print(f"ВАЛИДАЦИЯ КОРИДОРОВ (held-out 60/40 на ТОЛЬКО post-cutoff точках, цель {1 - args.alpha:.0%}) "
          f"— {models_dir}")
    print(f"cutoff={cutoff.date()} → покрытие меряется на index>cutoff (train-период только как контекст фич)")
    print("=" * 88)
    print(f"{'датчик · ГПА | бин':<58}{'n_test':>7}{'conform':>9}{'hybrid':>9}")
    # сортировка по ГПА, затем по датчику
    for key, rkey, n, cc, ch in sorted(rows, key=lambda x: (x[0].split('__GPA')[-1], x[0])):
        sensor = key.split('__GPA')[0]
        g = key.split('__GPA')[-1]
        lbl = (f"{sensor}·ГПА{g}|{str(rkey).split('|')[-1]}")[:58]
        print(f"{lbl:<58}{n:>7}{cc:>9.3f}{ch:>9.3f}")
    print("-" * 88)
    # разрез по ГПА
    pg = _c.defaultdict(lambda: {"c": [0, 0.0], "h": [0, 0.0]})
    for key, rkey, n, cc, ch in rows:
        g = key.split('__GPA')[-1]
        pg[g]["c"][0] += n; pg[g]["c"][1] += cc * n
        pg[g]["h"][0] += n; pg[g]["h"][1] += ch * n
    for g in sorted(set(list(pg) + [str(x) for x in ml_per_gpa])):
        c, h = pg[g]["c"], pg[g]["h"]
        cov_c = c[1] / c[0] if c[0] else float("nan")
        cov_h = h[1] / h[0] if h[0] else float("nan")
        print(f"  ГПА-{g}: ml_corridor={ml_per_gpa[g]}, оценено датчиков={len(eval_sensors[g])}  | "
              f"conformal={cov_c:.3f}  hybrid={cov_h:.3f}  (n_test={c[0]})")
    if skips:
        print("  ПРОПУЩЕНЫ датчики (почему GPA выпал из таблицы):")
        for reason, cnt in sorted(skips.items()):
            print(f"    • {reason}: {cnt}")
    print("-" * 88)
    for mode in ("conformal", "hybrid"):
        tot_n = sum(n for n, _ in agg[mode])
        tot_c = sum(c for _, c in agg[mode])
        cov = tot_c / tot_n if tot_n else float("nan")
        print(f"  ИТОГ {mode:9s}: покрытие={cov:.4f}  FP={1 - cov:.4f}  "
              f"(на {tot_n} held-out точках, {len(agg[mode])} режимов)")
    print("=" * 88)
    print("Выбор corridor_mode: ближе к цели по покрытию И меньший FP на healthy.")


if __name__ == "__main__":
    main()
