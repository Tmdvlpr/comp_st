# -*- coding: utf-8 -*-
"""СИСТЕМНЫЙ (unit-level) МОНИТОР СТРУКТУРНОГО ЗДОРОВЬЯ — глобальный, многомерный.

Не про один датчик. Оценивает АГРЕГАТ ЦЕЛИКОМ: сместилась ли совместная (многомерная) структура
всех его датчиков относительно замороженной healthy-базы обучения. Одиночный датчик, уехавший на
2σ, — слабое свидетельство; скоординированный сдвиг ДЕСЯТКОВ датчиков — сильное и ЗАЩИТИМОЕ:
«машина в другом рабочем состоянии».

Метод (стандартный многомерный SPC / PCA-monitoring, ISO-совместимый):
  • База = healthy-steady-running точки ≤ cutoff (ЗАМОРОЖЕНА, anomaly-safe). z-норма по-датчику.
  • PCA на базе, k главных компонент (≥95% дисперсии) = «здоровое подпространство» машины.
  • SPE (Q) = ‖остаток вне подпространства‖² → НОВАЯ структура связей (концепт-сдвиг), которой в
    базе не было (именно это на ГПА-1: связи датчиков сменились). T² = махаланобис внутри подпр-ва.
  • Контрольные пределы SPE/T² = 99-й перцентиль базы. Индекс здоровья = доля свежих точек за
    пределом + медианное превышение. Тренд по дням → КОГДА началось. Вклад датчиков → ЧТО сместило.
  • ФЛОТ: ГПА-1 vs ГПА-2/3 — отделяет unit-специфичное от общесезонного.

Read-only, БД не пишет. Периодический прод-джоб уровня агрегата.
Запуск:
    python system_monitor.py --station ohangaron --models-dir models/ohangaron \
        --cache-wide models/ohangaron_v23_staging/_wide_cache.pkl --recent-days 14
"""
import os
os.environ.setdefault("CS_DISABLE_DB_WRITE", "1")
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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import train as TR
from station_config import load_station_config
from data_loader import PostgresDataLoader
import weather as W

VAR_KEEP = 0.95        # доля дисперсии для числа главных компонент
CTRL_PCT = 99.0        # перцентиль базы для контрольного предела SPE/T²
MIN_BASE = 200         # минимум базовых точек
MIN_RECENT = 60        # минимум свежих точек


def _fit_pca(Xz):
    """PCA через SVD на z-нормированной базе. Возврат (компоненты P[p×k], собств.знач λ[k], k)."""
    U, s, Vt = np.linalg.svd(Xz, full_matrices=False)
    n = Xz.shape[0]
    lam = (s ** 2) / max(n - 1, 1)               # дисперсии компонент
    frac = np.cumsum(lam) / np.sum(lam)
    k = int(np.searchsorted(frac, VAR_KEEP) + 1)
    k = max(2, min(k, Xz.shape[1] - 1))
    return Vt[:k].T, lam[:k], k                  # P: p×k


def _spe_t2(Xz, P, lam):
    """SPE (остаток вне подпр-ва) и T² (махаланобис внутри) для строк Xz."""
    scores = Xz @ P                               # n×k
    recon = scores @ P.T                          # n×p
    resid = Xz - recon
    spe = np.sum(resid ** 2, axis=1)              # n
    t2 = np.sum((scores ** 2) / np.maximum(lam, 1e-9), axis=1)
    return spe, t2, resid


def main():
    ap = argparse.ArgumentParser(description="Системный многомерный монитор структурного здоровья агрегата")
    ap.add_argument("--station", default="ohangaron")
    ap.add_argument("--models-dir", default=None)
    ap.add_argument("--cache-wide", default=None)
    ap.add_argument("--from-date", default=None)
    ap.add_argument("--recent-days", type=float, default=14.0)
    ap.add_argument("--only-gpa", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--write-db", action="store_true",
                    help="писать unit-level здоровье в БД (system_health_t); прод-режим")
    args = ap.parse_args()
    if args.write_db:
        os.environ["CS_DISABLE_DB_WRITE"] = ""     # разрешить запись (иначе setdefault='1' подавит)

    cfg = load_station_config(args.station)
    meth = cfg.methodology or {}
    TR.configure_gas(meth.get("gas"))
    models_dir = args.models_dir or str(cfg.models_path)
    out_path = args.out or os.path.join(models_dir, "system_health.json")
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
    # панель мониторинга агрегата = его модельные датчики (физические величины)
    panel_by_gpa = {}
    for info in models.values():
        panel_by_gpa.setdefault(info.get("gpa_id"), []).append(info["name"])

    units = []
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

        panel = [s for s in dict.fromkeys(panel_by_gpa.get(gid, [])) if s in dfu.columns]
        if len(panel) < 4:
            continue
        M = dfu[panel].astype(float)
        base_idx = M.index[hs & (M.index <= cutoff)]
        rec_idx = M.index[hs & (M.index >= recent_lo)]
        Xb = M.loc[base_idx].dropna()
        if len(Xb) < MIN_BASE:
            continue
        mu, sd = Xb.mean(), Xb.std().replace(0, 1e-9)
        Xbz = ((Xb - mu) / sd).values
        P, lam, k = _fit_pca(Xbz)
        spe_b, t2_b, _ = _spe_t2(Xbz, P, lam)
        spe_lim = float(np.percentile(spe_b, CTRL_PCT))
        t2_lim = float(np.percentile(t2_b, CTRL_PCT))

        Xr = M.loc[rec_idx].dropna()
        if len(Xr) < MIN_RECENT:
            continue
        Xrz = ((Xr - mu) / sd).values
        spe_r, t2_r, resid_r = _spe_t2(Xrz, P, lam)
        exceed_spe = float(np.mean(spe_r > spe_lim))
        exceed_t2 = float(np.mean(t2_r > t2_lim))
        spe_ratio = float(np.median(spe_r) / (spe_lim + 1e-12))
        # вклад датчиков в SPE на превышающих точках (drill-down: ЧТО сместило структуру)
        over = spe_r > spe_lim
        contrib = (resid_r[over] ** 2).mean(axis=0) if over.any() else (resid_r ** 2).mean(axis=0)
        contrib = contrib / (contrib.sum() + 1e-12)
        top_contrib = sorted(zip(panel, contrib), key=lambda x: -x[1])[:5]
        # дневной тренд SPE-exceedance (когда началось)
        rec_days = pd.Series(spe_r > spe_lim, index=Xr.index).groupby(Xr.index.normalize()).mean()

        units.append(dict(
            gpa_id=gid, n_base=len(Xb), n_recent=len(Xr), p=len(panel), k_pc=k,
            spe_exceed=round(exceed_spe, 3), t2_exceed=round(exceed_t2, 3),
            spe_ratio_med=round(spe_ratio, 2),
            top_contrib=[(s, round(float(w), 3)) for s, w in top_contrib],
            daily=[(str(d.date()), round(float(v), 2)) for d, v in rec_days.items()]))

    # фон парка: медиана exceedance по агрегатам = сезонный уровень; unit сверх него = аномалия
    if units:
        fleet_med_spe = float(np.median([u["spe_exceed"] for u in units]))
    else:
        fleet_med_spe = 0.0
    for u in units:
        u["spe_excess_over_fleet"] = round(u["spe_exceed"] - fleet_med_spe, 3)

    print("=" * 96)
    print(f"СИСТЕМНЫЙ МОНИТОР СТРУКТУРНОГО ЗДОРОВЬЯ (PCA/SPE-T², многомерный, unit-level) — {models_dir}")
    print(f"база(заморож.)=healthy≤{cutoff.date()}  свежее={recent_lo.date()}..{raw.index.max().date()} "
          f"({args.recent_days:g}д)  предел={CTRL_PCT:g}%   фон парка SPE-exceed={fleet_med_spe:.1%}")
    print("=" * 96)
    print(f"{'агрегат':<10}{'PC':>4}{'SPE>лим':>9}{'T²>лим':>8}{'SPE/лим':>9}{'сверх парка':>13}  вердикт")
    print("-" * 96)
    for u in sorted(units, key=lambda x: -x["spe_exceed"]):
        # ожидаемо на здоровом ~1% (100−CTRL). >~10% и заметно сверх парка → структурный сдвиг агрегата
        exc = u["spe_excess_over_fleet"]
        if u["spe_exceed"] > 0.10 and exc > 0.05:
            verdict = "⚑ СТРУКТУРНЫЙ СДВИГ (unit-специфичный)"
        elif u["spe_exceed"] > 0.10:
            verdict = "структурный сдвиг (но и парк смещён — возможно сезон)"
        else:
            verdict = "в пределах здорового"
        u["verdict"] = verdict
        print(f"ГПА-{u['gpa_id']:<7}{u['k_pc']:>4}{u['spe_exceed']:>8.1%}{u['t2_exceed']:>8.1%}"
              f"{u['spe_ratio_med']:>9.2f}{exc:>12.1%}   {verdict}")
    print("-" * 96)
    # детализация: что и когда сместило структуру у самого аномального агрегата
    worst = max(units, key=lambda x: x["spe_excess_over_fleet"]) if units else None
    if worst and worst["spe_excess_over_fleet"] > 0.05:
        print(f"\nГПА-{worst['gpa_id']} — вклад в структурный сдвиг (доля SPE):")
        for s, w in worst["top_contrib"]:
            print(f"   {s}: {w:.1%}")
        dd = [d for d in worst["daily"] if d[1] > 0.10]
        if dd:
            print(f"   начало превышений (дни SPE-exceed>10%): {dd[0][0]} … {dd[-1][0]} "
                  f"(всего {len(dd)} дней)")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(dict(cutoff=str(cutoff.date()), recent_days=args.recent_days,
                       fleet_median_spe_exceed=fleet_med_spe, units=units), f, ensure_ascii=False, indent=2)
    print(f"\n✓ системное здоровье записано (JSON): {out_path}")

    # ── ПРОД: пишем unit-level здоровье в БД (система читает БД → считает → пишет новое в БД) ──
    if args.write_db and units:
        ts_end = raw.index.max()                              # конец свежего окна (Etc/GMT-5-naive)
        ts_utc = ts_end.tz_localize("Etc/GMT-5").tz_convert("UTC")
        rows = [(
            args.station, str(u["gpa_id"]), ts_utc.to_pydatetime(),
            float(u["spe_exceed"]), float(u["t2_exceed"]), float(u["spe_ratio_med"]),
            float(u["spe_excess_over_fleet"]), u.get("verdict", ""),
            json.dumps(u["top_contrib"], ensure_ascii=False), int(u["n_recent"]),
        ) for u in units]
        try:
            n = loader.save_system_health(rows)
            print(f"💾 БД: system_health_t — {n} строк (агрегаты) на {ts_utc.date()}")
        except Exception as e:
            print(f"⚠️ запись system_health_t не удалась: {e}")


if __name__ == "__main__":
    main()
