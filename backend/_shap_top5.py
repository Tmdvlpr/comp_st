# -*- coding: utf-8 -*-
"""Топ-5 параметров по вкладу SHAP для КАЖДОЙ модели (read-only).

Для каждой модели в metadata считаем mean|SHAP| по выборке healthy-steady точек её ГПА и берём
5 фич с наибольшим вкладом. Pooled-модели (общие, обучены на z-нормализованных данных) → SHAP
считаем на НОРМАЛИЗОВАННЫХ фичах (ранжирование фич инвариантно к монотонной z-нормировке);
один расчёт на общий файл, переиспользуется для 3 ГПА. Вывод: JSON + читаемая таблица.

Запуск:
    python _shap_top5.py --models-dir models/ohangaron_v23_pooled --cache-wide models/ohangaron_v23_staging/_wide_cache.pkl
"""
import os
os.environ.setdefault("CS_DISABLE_DB_WRITE", "1")
import sys
import io
import json
import argparse

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

import numpy as np
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import train as TR
from station_config import load_station_config
from data_loader import PostgresDataLoader
import weather as W


def _shap_top5(model, X, feats, pooled, norm):
    """Top-5 (feat, mean|shap|) для модели на выборке X (DataFrame, сырые фичи)."""
    from catboost import Pool
    Xm = X[feats].copy()
    if pooled and norm:                      # pooled: нормализуем как при обучении общей модели
        fmu = pd.Series(norm["feat_mu"]); fsd = pd.Series(norm["feat_sd"])
        Xm = (Xm - fmu) / fsd
    try:
        sv = np.asarray(model.get_feature_importance(Pool(Xm), type="ShapValues"), float)
        # RMSEWithUncertainty: возможна форма (n, 2, F+1) → берём выход mean (срез 0)
        if sv.ndim == 3:
            sv = sv[:, 0, :]
        contrib = np.mean(np.abs(sv[:, :-1]), axis=0)    # последний столбец — bias
    except Exception:
        # fallback: штатная важность CatBoost (PredictionValuesChange)
        contrib = np.asarray(model.get_feature_importance(), float)
    order = np.argsort(contrib)[::-1][:5]
    tot = float(contrib.sum()) or 1.0
    return [(feats[i], round(float(contrib[i]), 4), round(100 * float(contrib[i]) / tot, 1))
            for i in order if contrib[i] > 0]


def main():
    import joblib
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="ohangaron")
    ap.add_argument("--models-dir", default="models/ohangaron_v23_pooled")
    ap.add_argument("--cache-wide", default=None)
    ap.add_argument("--sample", type=int, default=1500)
    ap.add_argument("--out", default="shap_top5_per_model.json")
    args = ap.parse_args()

    cfg = load_station_config(args.station)
    meth = cfg.methodology or {}
    TR.configure_gas(meth.get("gas"))
    limits = {k: tuple(v) for k, v in (meth.get("limits") or {}).items()}
    with open(os.path.join(args.models_dir, "metadata.json"), encoding="utf-8") as f:
        meta = json.load(f)
    cutoff = pd.Timestamp(meta.get("last_train_timestamp") or meth.get("train_cutoff"))
    if getattr(cutoff, "tzinfo", None) is not None:
        cutoff = cutoff.tz_convert("Etc/GMT-5").tz_localize(None)

    loader = PostgresDataLoader(cfg)
    if args.cache_wide and os.path.exists(args.cache_wide):
        print(f"  wide из кэша: {args.cache_wide}", flush=True)
        raw = pd.read_pickle(args.cache_wide)
    else:
        tag_to_name = loader.build_tag_mapping()
        raw = TR._load_wide_windowed(loader)
        raw = raw.rename(columns=tag_to_name).sort_index()
        if getattr(raw.index, "tz", None) is not None:
            raw.index = raw.index.tz_convert("Etc/GMT-5").tz_localize(None)
        if args.cache_wide:
            raw.to_pickle(args.cache_wide)
    try:
        amb = W.get_ambient_series(cfg, raw.index)
    except Exception:
        amb = None

    # per-GPA dfu + выборка healthy ≤cutoff
    dfu = {}
    for g in ("1", "2", "3"):
        suf = f"__GPA{g}"
        cols = [c for c in raw.columns if c.endswith(suf)]
        if len(cols) < 3:
            continue
        d = raw[cols].copy(); d.columns = [c[:-len(suf)] for c in cols]; d = d.ffill(limit=2)
        if amb is not None:
            d["ambient_temp"] = amb.reindex(d.index).values
        d = TR.physically_clean(d, limits)
        run = TR.steady_running_mask(d)
        d = TR.add_domain_features_gpa(d, running_mask=run, train_cutoff=cutoff)
        h = TR.verified_healthy_mask(d, limits, cfg=TR.RegimeConfig()).reindex(d.index).fillna(False)
        dfu[g] = d.loc[h & (d.index <= cutoff)]

    out = {}
    file_cache = {}      # SHAP на общий pooled-файл считаем один раз
    for key, info in meta["models"].items():
        g = info.get("gpa_id"); name = info["name"]
        feats = [f for f in (info.get("feat_cols") or []) if g in dfu and f in dfu[g].columns]
        if g not in dfu or name not in dfu[g].columns or len(feats) < 2:
            out[key] = {"top5": [], "note": "нет данных/фич"}
            continue
        samp = dfu[g].dropna(subset=feats)
        if len(samp) > args.sample:
            samp = samp.sample(args.sample, random_state=42)
        if len(samp) < 50:
            out[key] = {"top5": [], "note": f"мало точек ({len(samp)})"}
            continue
        mp = os.path.join(args.models_dir, info["model_file"])
        if not os.path.exists(mp):
            out[key] = {"top5": [], "note": "нет файла"}
            continue
        # ключ кэша для pooled: файл+gpa (нормализация разная по ГПА, но ранжирование ~одно;
        # считаем на фактической выборке этого ГПА — корректно)
        if mp not in file_cache:
            file_cache[mp] = joblib.load(mp)["model"]
        model = file_cache[mp]
        top5 = _shap_top5(model, samp, feats, bool(info.get("pooled")), info.get("norm"))
        out[key] = {"pooled": bool(info.get("pooled")), "detector_mode": info.get("detector_mode"),
                    "top5": [{"feature": f, "mean_abs_shap": v, "pct": p} for f, v, p in top5]}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # читаемая таблица
    print("=" * 92)
    print(f"ТОП-5 SHAP-ПАРАМЕТРОВ ПО КАЖДОЙ МОДЕЛИ ({args.models_dir})  → {args.out}")
    print("=" * 92)
    for key in sorted(out):
        v = out[key]
        t = v.get("top5") or []
        if not t:
            print(f"{key:42s} — {v.get('note','—')}")
            continue
        s = ", ".join(f"{x['feature']}({x['pct']}%)" for x in t)
        print(f"{key:42s} [{'pool' if v.get('pooled') else 'unit'}] {s}")
    print("=" * 92)


if __name__ == "__main__":
    main()
