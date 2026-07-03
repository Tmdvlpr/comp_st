"""Юнит-тесты regime.py (метка режима, бины, калибр.окно, пер-юнитный cutoff).
Запуск: pytest tests/test_regime.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import train as RG          # методология консолидирована в train.py

LIMITS = {"rpm_tvd": (0.0, 9000.0), "gas_pressure_in_gpa": (-0.1, 12.0),
          "gas_pressure_out_gpa": (-0.1, 12.0)}


def _synth(n=3000, stop_first_half=False, load_varies=False, seed=0):
    idx = pd.date_range("2026-03-01", periods=n, freq="5min")
    rng = np.random.default_rng(seed)
    rpm = np.full(n, 6000.0) + rng.normal(0, 20, n)
    if load_varies:
        rpm = np.linspace(4500, 6800, n) + rng.normal(0, 20, n)   # широкий диапазон нагрузки
    if stop_first_half:
        rpm[: n // 2] = 0.0
    p_in = 1.6 + rng.normal(0, 0.02, n)
    return pd.DataFrame({
        "rpm_tvd": rpm, "rpm_tnd": rpm * 0.8, "rpm_st": rpm * 0.7,
        "gas_pressure_in_gpa": p_in, "gas_pressure_out_gpa": 3.0 + rng.normal(0, 0.03, n),
        "anti_surge_valve_pos": np.full(n, 5.0),
        "fuel_gas_flow_rate_sec": 1.2 + rng.normal(0, 0.02, n),
        "temp_front_bearing_pads": 70 + rng.normal(0, 0.5, n),
    }, index=idx)


def test_label_regime_running_mostly_steady():
    lab = RG.label_regime(_synth())
    assert (lab == RG.STEADY).mean() > 0.9


def test_label_regime_stop_detected():
    lab = RG.label_regime(_synth(stop_first_half=True))
    assert (lab == RG.STOP).sum() > 100
    assert (lab == RG.STEADY).sum() > 100


def test_sub_mode_ring_by_asv():
    df = _synth(); df["anti_surge_valve_pos"] = 80.0
    assert (RG.sub_mode(df) == RG.RING).all()
    df2 = _synth(); df2["anti_surge_valve_pos"] = 3.0
    assert (RG.sub_mode(df2) == RG.MAINLINE).all()


def test_load_bins_low_cv_single():
    df = _synth()                                   # rpm почти константа → 1 бин
    b = RG.fit_load_bins(df, RG.label_regime(df) == RG.STEADY)
    assert b.n_bins == 1


def test_load_bins_varying_splits():
    df = _synth(load_varies=True)                   # нагрузка гуляет → >1 бина
    b = RG.fit_load_bins(df, RG.label_regime(df) == RG.STEADY)
    assert b.n_bins >= 2 and b.edges


def test_regime_key_steady_format():
    df = _synth()
    lab = RG.label_regime(df); sm = RG.sub_mode(df)
    lb = RG.load_bin_labels(df, RG.fit_load_bins(df, lab == RG.STEADY))
    rk = RG.regime_key(lab, sm, lb)
    assert all(k.startswith("steady|") for k in set(rk[lab == RG.STEADY]))


def test_resolve_unit_cutoff_ok_when_post_data():
    df = _synth(n=3000)
    cutoff = df.index[1000]                          # 2000 healthy post-cutoff ≥ n_min_calib
    res = RG.resolve_unit_cutoff(df, cutoff, LIMITS)
    assert res["mode"] == RG.CALIB_OK
    assert pd.Timestamp(res["unit_cutoff"]) == cutoff


def test_resolve_unit_cutoff_per_unit_when_stopped_after():
    # агрегат работает первую половину, стоит после cutoff → вариант B (пер-юнитный cutoff назад)
    df = _synth(n=3000); df.loc[df.index[1500:], "rpm_tvd"] = 0.0
    df.loc[df.index[1500:], ["rpm_tnd", "rpm_st"]] = 0.0
    cutoff = df.index[2000]                          # после cutoff агрегат стоит → нет healthy
    res = RG.resolve_unit_cutoff(df, cutoff, LIMITS)
    assert res["mode"] in (RG.CALIB_PER_UNIT, RG.CALIB_STARVED)
    if res["mode"] == RG.CALIB_PER_UNIT:
        assert pd.Timestamp(res["unit_cutoff"]) < cutoff   # сдвинут назад к рабочему окну


def test_select_calibration_returns_decisions():
    df = _synth(n=3000)
    cutoff = df.index[500]
    sel = RG.select_calibration(df, cutoff, LIMITS)
    assert sel and all("decision" in v and "n" in v for v in sel.values())
