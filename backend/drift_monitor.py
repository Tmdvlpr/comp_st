# -*- coding: utf-8 -*-
"""ДЕТЕКТОР СТРУКТУРНОГО ДРЕЙФА (Детектор 3) — ловит то, чего коридор НЕ видит.

Коридор (Детектор 1) ловит выброс ТОЧКИ за полосу. Но медленный структурный сдвиг режима
(уровень уехал + связи датчика с соседями сломались) коридор пропускает: после перецентровки
точки снова «внутри». Именно так на ГПА-1 переднего ротора вибрация структурно изменилась
(уровень упал, корреляции с давлениями/маслом сменили знак) — БЕЗ выхода точек за полосу.

Этот инструмент сравнивает СВЕЖЕЕ окно с ЗАМОРОЖЕННОЙ healthy-базой обучения (frozen reference —
anomaly-safe: база НЕ ползёт за деградацией) по трём осям:
  1. LEVEL_SHIFT   — |медиана_свежая − медиана_база| / σ_база   (уровень уехал);
  2. CORR_BREAK    — среднее |Δкорреляции| датчика с соседями (его feat_cols) база→свежее,
                     + число СМЕН ЗНАКА связи (структура развалилась → концепт-сдвиг);
  3. SPREAD        — σ_свежая / σ_база (разброс изменился).
Плюс ФЛОТ-относительность: агрегат, чей сдвиг отклоняется от парка того же датчика (смена знака /
выброс), помечается UNIT_OUTLIER — именно так ГПА-1 (вибро вниз) отличался от ГПА-2/3 (вверх).

Read-only, БД не пишет. Периодический прод-джоб (напр. раз в сутки на скользящем окне).
Запуск:
    python drift_monitor.py --station ohangaron --models-dir models/ohangaron \
        --cache-wide models/ohangaron_v23_staging/_wide_cache.pkl --recent-days 14
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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import train as TR
from station_config import load_station_config
from data_loader import PostgresDataLoader
import weather as W

# Пороги алертов (консервативные; настраиваются). Персистенция — на уровне периодич. запуска.
TAU_LEVEL = 4.0        # |сдвиг уровня| в σ базы (поднят 2→4: только сильные, защитимые сдвиги)
TAU_UNIT_LEVEL_DIV = 4.0   # расхождение агрегата с парком по уровню (σ) для UNIT_OUTLIER
TAU_CORR = 0.30        # среднее |Δкорр| с соседями
TAU_SIGNFLIP = 2       # число связей, сменивших знак с |Δ|>0.5
TAU_SPREAD_HI = 2.0    # раздувание разброса
TAU_SPREAD_LO = 0.4    # схлопывание разброса
MIN_N = 60             # минимум healthy-steady точек в окне


def _corr_vector(df, target, neighbors):
    """Корреляции target с соседями (пирсон) на общих валидных строках. Возврат dict{neighbor:corr}."""
    out = {}
    y = df[target].astype(float)
    for nb in neighbors:
        if nb not in df.columns or nb == target:
            continue
        x = df[nb].astype(float)
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < 30:
            continue
        xv, yv = x[m].values, y[m].values
        if np.std(xv) < 1e-9 or np.std(yv) < 1e-9:
            continue
        out[nb] = float(np.corrcoef(xv, yv)[0, 1])
    return out


def _corr_break(base_c, rec_c):
    """Сравнение корреляционных векторов: среднее |Δ|, макс |Δ|, число смен знака (|Δ|>0.5)."""
    common = set(base_c) & set(rec_c)
    if not common:
        return dict(mean_abs=0.0, max_abs=0.0, n_signflip=0, n=0, worst=None)
    deltas = {k: rec_c[k] - base_c[k] for k in common}
    signflip = [k for k in common if np.sign(base_c[k]) != np.sign(rec_c[k]) and abs(deltas[k]) > 0.5]
    worst = max(common, key=lambda k: abs(deltas[k]))
    return dict(mean_abs=float(np.mean([abs(d) for d in deltas.values()])),
                max_abs=float(max(abs(d) for d in deltas.values())),
                n_signflip=len(signflip), n=len(common),
                worst=f"{worst}: {base_c[worst]:+.2f}->{rec_c[worst]:+.2f}")


def _severity(level_sig, cb, spread):
    """Классификация + числовой score (для ранжирования)."""
    kinds = []
    if abs(level_sig) > TAU_LEVEL:
        kinds.append("LEVEL_SHIFT")
    if cb["mean_abs"] > TAU_CORR or cb["n_signflip"] >= TAU_SIGNFLIP:
        kinds.append("CORR_BREAK")
    if np.isfinite(spread) and (spread > TAU_SPREAD_HI or spread < TAU_SPREAD_LO):
        kinds.append("SPREAD")
    score = abs(level_sig) / TAU_LEVEL + cb["mean_abs"] / TAU_CORR + 0.5 * cb["n_signflip"]
    return kinds, float(score)


def main():
    ap = argparse.ArgumentParser(description="Детектор структурного дрейфа (Детектор 3)")
    ap.add_argument("--station", default="ohangaron")
    ap.add_argument("--models-dir", default=None)
    ap.add_argument("--cache-wide", default=None)
    ap.add_argument("--from-date", default=None)
    ap.add_argument("--recent-days", type=float, default=14.0, help="ширина свежего окна (дни от конца данных)")
    ap.add_argument("--only-gpa", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_station_config(args.station)
    meth = cfg.methodology or {}
    TR.configure_gas(meth.get("gas"))
    models_dir = args.models_dir or str(cfg.models_path)
    out_path = args.out or os.path.join(models_dir, "drift_alerts.json")
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
        print("📡 Загрузка истории (оконно)...", flush=True)
        raw = TR._load_wide_windowed(loader, from_date=args.from_date)
        raw = raw.rename(columns=tag_to_name).sort_index()
        if getattr(raw.index, "tz", None) is not None:
            raw.index = raw.index.tz_convert("Etc/GMT-5").tz_localize(None)
    try:
        amb = W.get_ambient_series(cfg, raw.index)
    except Exception:
        amb = None

    recent_lo = raw.index.max() - pd.Timedelta(days=args.recent_days)
    models = meta["models"]
    recs = []

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
        steady = (TR.label_regime(dfu, regime_cfg) == TR.STEADY)
        hs = healthy & steady

        base_mask = hs & (dfu.index <= cutoff)                 # ЗАМОРОЖЕННАЯ база (обучающее healthy)
        rec_mask = hs & (dfu.index >= recent_lo)               # свежее окно
        base_df = dfu.loc[base_mask]
        rec_df = dfu.loc[rec_mask]

        for key, info in models.items():
            if info.get("gpa_id") != gid:
                continue
            name = info["name"]
            feats = list(info.get("feat_cols") or [])
            if name not in dfu.columns:
                continue
            yb = base_df[name].astype(float).dropna()
            yr = rec_df[name].astype(float).dropna()
            if len(yb) < MIN_N or len(yr) < MIN_N:
                continue
            mb, sb = float(np.median(yb)), float(np.std(yb))
            mr, sr = float(np.median(yr)), float(np.std(yr))
            level_sig = (mr - mb) / (sb + 1e-9)
            spread = sr / (sb + 1e-9)
            base_c = _corr_vector(base_df, name, feats)
            rec_c = _corr_vector(rec_df, name, feats)
            cb = _corr_break(base_c, rec_c)
            kinds, score = _severity(level_sig, cb, spread)
            recs.append(dict(gpa_id=gid, name=name, kinds=kinds, score=round(score, 2),
                             level_shift_sigma=round(level_sig, 2), spread_ratio=round(spread, 2),
                             corr_mean_delta=round(cb["mean_abs"], 2), corr_signflips=cb["n_signflip"],
                             corr_worst=cb["worst"], n_base=len(yb), n_recent=len(yr),
                             base_median=round(mb, 3), recent_median=round(mr, 3)))

    # ── ФЛОТ-относительность: отделяем UNIT-аномалию (агрегат расходится с парком — как ГПА-1) от
    #    FLEET_COMMON (весь парк дрейфует одинаково = сезон/режим, НЕ поломка). Это ключевой
    #    дискриминатор: сезонный сдвиг корреляций бьёт по всем ГПА, unit-поломка — по одному. ──
    by_name = _c.defaultdict(list)
    for r in recs:
        by_name[r["name"]].append(r)
    for r in recs:
        peers = [p for p in by_name[r["name"]] if p is not r]
        r["unit_outlier"] = False
        r["fleet_common"] = False
        if len(peers) >= 1:
            ls_med = float(np.median([p["level_shift_sigma"] for p in peers]))
            cb_med = float(np.median([p["corr_mean_delta"] for p in peers]))
            level_div = abs(r["level_shift_sigma"] - ls_med)
            corr_div = r["corr_mean_delta"] - cb_med
            if level_div > TAU_UNIT_LEVEL_DIV or corr_div > 0.25:
                r["unit_outlier"] = True                        # расходится с парком → приоритетная аномалия
            elif r["kinds"] and level_div < 1.0 and abs(corr_div) < 0.15:
                r["fleet_common"] = True                        # парк ведёт себя так же → сезон, не поломка

    alerts = [r for r in recs if r["kinds"]]
    unit_alerts = [r for r in alerts if r["unit_outlier"]]
    unit_alerts.sort(key=lambda r: -r["score"])
    other = [r for r in alerts if not r["unit_outlier"] and not r["fleet_common"]]
    other.sort(key=lambda r: -r["score"])
    fleet_common = [r for r in alerts if r["fleet_common"]]

    print("=" * 100)
    print(f"ДЕТЕКТОР СТРУКТУРНОГО ДРЕЙФА — {models_dir}")
    print(f"база(заморож.)=healthy≤{cutoff.date()}  свежее={recent_lo.date()}..{raw.index.max().date()} "
          f"({args.recent_days:g}д)")
    print(f"UNIT-аномалий: {len(unit_alerts)}  |  прочих структурных: {len(other)}  |  "
          f"fleet-common(сезон): {len(fleet_common)}  из {len(recs)}")
    print("=" * 100)

    def _line(r):
        return (f"{r['name']+'·ГПА'+str(r['gpa_id']):<44}{r['score']:>6.1f}{r['level_shift_sigma']:>8.1f}"
                f"{r['corr_mean_delta']:>7.2f}{r['corr_signflips']:>5}  {'+'.join(r['kinds'])}")

    print(f"\n⚑ UNIT-АНОМАЛИИ (агрегат расходится с парком — ПРИОРИТЕТ инженеру):")
    print(f"{'датчик · ГПА':<44}{'score':>6}{'сдвигσ':>8}{'Δкорр':>7}{'знак':>5}  типы")
    print("-" * 100)
    for r in unit_alerts:
        print(_line(r))
    for r in unit_alerts[:8]:
        print(f"     └ уровень {r['base_median']}→{r['recent_median']} ({r['level_shift_sigma']:+.1f}σ); "
              f"худшая связь {r['corr_worst']}")

    if other:
        print(f"\n· Прочие структурные изменения (не расходятся с парком явно):")
        for r in other[:12]:
            print(_line(r))
    if fleet_common:
        print(f"\nℹ Fleet-common (сезон/режим, весь парк — НЕ поломка): "
              f"{', '.join(sorted(set(r['name'] for r in fleet_common)))[:200]}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(dict(cutoff=str(cutoff.date()), recent_days=args.recent_days,
                       generated_rows=len(recs), alerts=alerts), f, ensure_ascii=False, indent=2)
    print(f"\n✓ алерты записаны: {out_path} ({len(alerts)} шт.)")


if __name__ == "__main__":
    main()
