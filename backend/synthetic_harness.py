"""
Двухосевой синтетический харнесс для v2-методологии коридора.

Зачем: на реальной статичной неделе коридор не оживает (всё уходит в univariate_only).
Харнесс генерит ДИНАМИЧНЫЙ SCADA (нагрузка варьируется → conditioning-response обучаем),
с АВТОКОРРЕЛИРОВАННЫМ (AR1) шумом отклика, стопами/пусками (ротация) и инъекцией дефекта.

Мерит ОБЕ оси (coverage ≠ ценность детектора):
  • ось ВЕРОЯТНОСТИ — эмпирическое покрытие на healthy-тесте (всего + по режимам),
    block vs naive conformal → решает, нужен ли upgrade на EnbPI;
  • ось ВРЕМЕНИ — miss-rate и lead-time на инъекции (ступень/рамп), с persistence и без,
    + кластерность ложняков → решает persistence/severity.
Плюс сценарий РОТАЦИИ: плановый останов → рестарт в сдвинутой рабочей точке → OOD-флаг
должен увести в univariate_only, пока не накопится свежая калибровка.

Здесь же реализована логика v2-ИНФЕРЕНСА (predict_v2) — прообраз правки predict_sensor.
Внешних данных не требует.
"""
from __future__ import annotations

import sys
import io
import numpy as np
import pandas as pd

# Вся методология консолидирована в train.py.
import train as RG       # режим/healthy/бины (label_regime, sub_mode, LoadBinning, regime_key, ...)
import train as CAL      # CalibrationArtifact
import train as TV       # train_sensor, SensorModel, ensemble_sigma_uepi
import detection_methods as DM

FEATS = ["rpm_tvd", "rpm_tnd", "rpm_st", "gas_pressure_in_gpa", "ambient_temp",
         "anti_surge_valve_pos", "fuel_gas_flow_rate_sec", "fuel_gas_pressure_in_gtd",
         "pressure_ratio"]
TARGET = "temp_front_bearing_pads"
LIMITS = {"rpm_tvd": (0.0, 9000.0), "rpm_tnd": (0.0, 8000.0), "rpm_st": (0.0, 7000.0),
          "gas_pressure_in_gpa": (-0.1, 12.0), "gas_pressure_out_gpa": (-0.1, 12.0),
          "fuel_gas_flow_rate_sec": (0.0, 10.0), "temp_front_bearing_pads": (-20.0, 200.0),
          "ambient_temp": (-60.0, 60.0)}


def _ar1(n, phi, sigma, rng):
    e = rng.normal(0, sigma, n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + e[i]
    return r


def synth_unit(n_days=60, seed=0, phi=0.9, noise=0.6,
               stop_window=None, restart_load_shift=0.0):
    """Динамичный SCADA одного ГПА. conditioning движется латентной нагрузкой (суточный
    цикл + медленный случайный блуждание); отклик = физ.функция(нагрузка, погода, сжатие)
    + AR1-шум. stop_window=(s,e) — плановый останов (rpm→0). restart_load_shift сдвигает
    рабочую точку после рестарта (для теста OOD)."""
    rng = np.random.default_rng(seed)
    n = n_days * 288
    t = pd.date_range("2026-01-01", periods=n, freq="5min")
    hod = t.hour + t.minute / 60.0
    daily = 0.5 + 0.3 * np.sin(2 * np.pi * hod / 24)
    walk = np.cumsum(rng.normal(0, 0.01, n))
    walk = (walk - walk.min()) / (np.ptp(walk) + 1e-9)
    load = np.clip(0.5 * daily.values + 0.5 * walk, 0.05, 1.0)

    after = np.zeros(n, bool)
    if stop_window is not None:
        s, e = pd.Timestamp(stop_window[0]), pd.Timestamp(stop_window[1])
        after = (t >= e)
        load = load + restart_load_shift * after          # сдвиг рабочей точки после рестарта
        load = np.clip(load, 0.05, 1.3)

    p_in = 4.0 + 0.5 * load + rng.normal(0, 0.02, n)
    p_out = p_in * (1.4 + 0.3 * load) + rng.normal(0, 0.02, n)
    ambient = 10 + 10 * np.sin(2 * np.pi * (hod.values - 6) / 24) + rng.normal(0, 0.5, n)
    # широкий разброс нагрузки (CV>0.05) → fit_load_bins даёт 3 бина → проверяем Mondrian
    df = pd.DataFrame({
        "rpm_tvd": 4000 + 3000 * load + rng.normal(0, 20, n),
        "rpm_tnd": 3600 + 2800 * load + rng.normal(0, 20, n),
        "rpm_st": 3200 + 2600 * load + rng.normal(0, 20, n),
        "gas_pressure_in_gpa": p_in, "gas_pressure_out_gpa": p_out,
        "ambient_temp": ambient,
        "anti_surge_valve_pos": np.clip(20 - 10 * load + rng.normal(0, 2, n), 0, 100),
        "fuel_gas_flow_rate_sec": 0.8 + 0.3 * load + rng.normal(0, 0.01, n),
        "fuel_gas_pressure_in_gtd": 3.0 + 0.5 * load + rng.normal(0, 0.02, n),
    }, index=t)
    df["pressure_ratio"] = (df["gas_pressure_out_gpa"] + 0.101325) / (df["gas_pressure_in_gpa"] + 0.101325)
    base = 40 + 25 * load + 0.3 * ambient + 5 * df["pressure_ratio"].values
    df[TARGET] = base + _ar1(n, phi, noise, rng)

    if stop_window is not None:
        s, e = pd.Timestamp(stop_window[0]), pd.Timestamp(stop_window[1])
        mask = (t >= s) & (t < e)
        for c in ("rpm_tvd", "rpm_tnd", "rpm_st"):
            df.loc[mask, c] = 0.0
    return df


def inject_defect(df, target, onset, kind="ramp", magnitude=4.0, ramp_days=1.0):
    """Дефект ПОВЕРХ здорового сигнала (сохраняет автокорреляцию). step — ступень,
    ramp — линейный рост до magnitude за ramp_days. Возвращает (df', onset_ts)."""
    out = df.copy()
    onset = pd.Timestamp(onset)
    idx = out.index
    add = np.zeros(len(idx))
    after = idx >= onset
    if kind == "step":
        add[after] = magnitude
    else:
        days = (idx - onset).total_seconds() / 86400.0
        ramp = np.clip(days / max(ramp_days, 1e-9), 0, 1) * magnitude
        add = np.where(after, ramp, 0.0)
    out[target] = out[target].values + add
    return out, onset


# ── ИНФЕРЕНС (прообраз predict_sensor) ──────────────────────────────────────────
def predict_v2(meta: "TV.SensorModel", df: pd.DataFrame, regime_cfg, ood_factor=1.5,
               corridor_mode="hybrid"):
    """Вход → [mean, sigma, epistemic, lo, hi, regime, mode, ood, anomaly]. Аномалия = факт вне
    коридора, где режим откалиброван (ml_corridor), не маргинальный OOD и steady.
    corridor_mode: 'hybrid' (hw=q̂_norm·σ) | 'conformal' (hw=q̂_abs). Эпистемику НЕ глушим коридор."""
    model = meta._model
    X = df[meta.feat_cols]
    mean = np.asarray(model.predict(X), float)
    mean = mean[:, 0] if mean.ndim == 2 else mean
    sigma, know = TV.ensemble_sigma_uepi(model, X, int(meta.n_trees), TV.VE_COUNT)
    lab = RG.label_regime(df, regime_cfg)
    sm = RG.sub_mode(df, regime_cfg)
    binning = RG.LoadBinning(axis=meta.load_binning.get("axis"),
                             edges=meta.load_binning.get("edges"),
                             cv=meta.load_binning.get("cv", 0.0),
                             n_bins=meta.load_binning.get("n_bins", 1))
    rk = RG.regime_key(lab, sm, RG.load_bin_labels(df, binning))
    art = CAL.CalibrationArtifact.from_dict(meta.calibration)
    ref_p95 = float(meta.epistemic_ref.get("know_p95", np.inf) or np.inf)
    ood = (know > ood_factor * ref_p95) if np.isfinite(ref_p95) else np.zeros(len(df), bool)

    hw = np.full(len(df), np.nan)
    mode = np.array(["univariate_only"] * len(df), dtype=object)
    steady = (lab == RG.STEADY).values
    for i, key in enumerate(rk.values):
        rc = art.by_regime.get(key)
        if rc and rc.get("decision") == "ok" and steady[i]:
            if corridor_mode == "conformal":
                q = rc.get("threshold_abs")
                hw[i] = float(q) if q is not None else np.nan          # плоский: q̂_abs
            else:
                hw[i] = float(rc["threshold"]) * sigma[i]              # гибрид: q̂_norm·σ
            mode[i] = "ml_corridor"
    y = df[meta.target].values
    anomaly = np.isfinite(hw) & (np.abs(y - mean) > hw)
    return pd.DataFrame(dict(mean=mean, sigma=sigma, epistemic=know, lo=mean - hw, hi=mean + hw,
                             regime=rk.values, mode=mode, ood=ood, anomaly=anomaly, know=know),
                        index=df.index)


def _runs(flags):
    runs, c = [], 0
    for v in flags:
        if v:
            c += 1
        elif c:
            runs.append(c); c = 0
    if c:
        runs.append(c)
    return runs


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    cfg = RG.RegimeConfig(n_min_calib=300)
    calib_cfg = dict(alpha=0.02, n_eff_min=49, n_boot=200)

    # ── обучение на здоровом динамичном юните ──
    df = synth_unit(n_days=60, seed=1)
    cutoff = df.index.min() + pd.Timedelta(days=40)
    meta = TV.train_sensor(df, TARGET, FEATS, cutoff, LIMITS, "S", cfg, calib_cfg)
    print("=" * 70)
    print(f"ОБУЧЕНИЕ: detector_mode={meta.detector_mode} n_trees={meta.n_trees} "
          f"R²_eval={meta.r2_eval} (baseline={meta.r2_baseline}) range={meta.sensor_range}")
    print(f"нагрузка: ось={meta.load_binning.get('axis')} CV={meta.load_binning.get('cv'):.3f} "
          f"бинов={meta.load_binning.get('n_bins')} edges={meta.load_binning.get('edges')}")
    for rk, rc in meta.calibration.get("by_regime", {}).items():
        print(f"  [{rk}] thr_block={rc['threshold']:.3f} naive={rc['q_naive']:.3f} "
              f"n={rc['n']} n_eff={rc['n_eff']} L={rc['block_len']} → {rc['decision']}")
    if meta.detector_mode != "ml_corridor":
        print("Коридор не построен — нечего валидировать.", meta.note); return

    # ── ОСЬ ВЕРОЯТНОСТИ: покрытие на healthy-тесте (всего + по режимам), block vs naive ──
    test = df[df.index > cutoff]
    healthy = RG.verified_healthy_mask(df, LIMITS, cfg=cfg).reindex(test.index).fillna(False)
    ht = test[healthy.values]
    print("\n" + "=" * 70 + "\nОСЬ ВЕРОЯТНОСТИ — покрытие на healthy-тесте (цель ~98%)")
    for src in ("conformal", "hybrid"):
        pr = predict_v2(meta, ht, cfg, corridor_mode=src)
        act = pr[pr["mode"] == "ml_corridor"]
        cov = float(1 - act["anomaly"].mean()) if len(act) else float("nan")
        line = f"  {src:9s}: покрытие_всего={cov:.4f} (n={len(act)})  | по режимам:"
        per = []
        for rk, g in act.groupby("regime"):
            per.append(f"{rk.split('|')[-1]}={1 - g['anomaly'].mean():.4f}(n={len(g)})")
        print(line, " ".join(per))

    # ── ОСЬ ВРЕМЕНИ: инъекция (ступень/рамп), miss-rate и lead-time, ±persistence ──
    print("\n" + "=" * 70 + "\nОСЬ ВРЕМЕНИ — инъекция дефекта (детекция и упреждение)")
    onset = cutoff + pd.Timedelta(days=10)
    for kind, mag in (("step", 3.0), ("ramp", 5.0)):
        dfi, on = inject_defect(df, TARGET, onset, kind=kind, magnitude=mag)
        seg = dfi[(dfi.index > cutoff)]
        pr = predict_v2(meta, seg, cfg)
        post = pr.index >= on
        raw = pr["anomaly"].values & post & (pr["mode"] == "ml_corridor").values
        persist = DM.run_length_filter(pr["anomaly"].values & (pr["mode"] == "ml_corridor").values,
                                       min_len=3) & post
        def lead(mask):
            w = np.where(mask)[0]
            return (pr.index[w[0]] - on).total_seconds() / 3600.0 if len(w) else None
        lr, lp = lead(raw), lead(persist)
        print(f"  {kind:4s} (маг={mag}): miss(raw)={'НЕТ' if raw.any() else 'ДА(пропуск!)'} "
              f"lead_raw={lr if lr is None else round(lr,2)}ч | "
              f"miss(persist≥3)={'НЕТ' if persist.any() else 'ДА'} "
              f"lead_persist={lp if lp is None else round(lp,2)}ч")

    # ── кластерность ложняков на healthy (ось времени для FP) ──
    pr_h = predict_v2(meta, ht, cfg)
    fp = pr_h["anomaly"].values & (pr_h["mode"] == "ml_corridor").values
    runs = _runs(fp)
    print(f"\n  ложняки на healthy: {int(fp.sum())} точек, эпизодов={len(runs)}, "
          f"средняя длина серии={np.mean(runs) if runs else 0:.1f} "
          f"→ persistence≥3 оставит {sum(1 for r in runs if r >= 3)} эпизодов")

    # ── СЦЕНАРИЙ РОТАЦИИ: останов → рестарт в сдвинутой точке → OOD ──
    print("\n" + "=" * 70 + "\nСЦЕНАРИЙ РОТАЦИИ — рестарт в сдвинутой рабочей точке → OOD")
    s = df.index.min() + pd.Timedelta(days=45)
    e = s + pd.Timedelta(days=5)
    dfr = synth_unit(n_days=60, seed=1, stop_window=(s, e), restart_load_shift=0.5)
    after = dfr[(dfr.index >= e)]
    prr = predict_v2(meta, after, cfg)
    work = prr[prr["regime"].astype(str).str.startswith("steady")]
    ood_rate = float(work["ood"].mean()) if len(work) else float("nan")
    uni_rate = float((work["mode"] == "univariate_only").mean()) if len(work) else float("nan")
    print(f"  после рестарта (load_shift): рабочих точек={len(work)}, "
          f"OOD-флаг={ood_rate:.1%}, уведены в univariate_only={uni_rate:.1%}  "
          f"(epistemic ref p95={meta.epistemic_ref.get('know_p95')})")
    print("=" * 70)


if __name__ == "__main__":
    main()
