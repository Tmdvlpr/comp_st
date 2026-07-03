"""Юнит-тесты calibrator.py (block-Mondrian conformal). Запуск: pytest tests/test_calibrator.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import train as CAL          # методология консолидирована в train.py


def _ar1(n, phi, seed=0):
    r = np.zeros(n); e = np.random.default_rng(seed).normal(size=n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + e[i]
    return r


def test_autocorr_block_len_white_vs_ar():
    white = np.random.default_rng(0).normal(size=5000)
    assert CAL.autocorr_block_len(white) <= 3            # ~независимо → L≈1
    assert CAL.autocorr_block_len(_ar1(5000, 0.95)) > 5  # сильная автокорр → L большой


def test_split_conformal_quantile():
    a = np.arange(1, 101, dtype=float)            # |scores| = 1..100
    q = CAL.split_conformal_q(a, alpha=0.05)      # ⌈101·0.95⌉=96-я порядк. → ~96
    assert 93 <= q <= 100


def test_block_conformal_fields_and_floor():
    out = CAL.block_conformal_threshold(np.random.default_rng(1).normal(0, 2, 3000),
                                        alpha=0.05, n_boot=50)
    assert np.isfinite(out["threshold"]) and out["threshold"] > 0
    assert out["n"] == 3000 and out["n_eff"] >= 1 and out["block_len"] >= 1
    # слишком мало точек → nan (не выдаём мусорный порог)
    assert not np.isfinite(CAL.block_conformal_threshold(np.arange(5.0))["threshold"])


def test_mondrian_gating_by_n_eff():
    big = np.random.default_rng(2).normal(0, 1, 4000)   # n_eff большой → ok
    tiny = np.random.default_rng(3).normal(0, 1, 15)    # n<20 → nan → univariate
    art = CAL.mondrian_calibrate({"A": big, "B": tiny}, alpha=0.05, n_eff_min=19, n_boot=40)
    assert art.by_regime["A"]["decision"] == "ok"
    assert art.by_regime["B"]["decision"] == "univariate_only"
    assert art.threshold_for("A") is not None
    assert art.threshold_for("B") is None
    assert art.decision_for("Z-неизвестный") == "univariate_only"


def test_artifact_roundtrip_and_recalibrate():
    art = CAL.mondrian_calibrate({"A": np.random.default_rng(4).normal(size=3000)},
                                 alpha=0.05, n_eff_min=19, n_boot=40)
    art2 = CAL.CalibrationArtifact.from_dict(art.to_dict())
    assert art2.threshold_for("A") == art.threshold_for("A")
    assert art2.alpha == art.alpha
    old = art.threshold_for("A")
    art.recalibrate("A", np.random.default_rng(5).normal(0, 5, 3000), n_boot=40)  # шире остатки
    assert art.threshold_for("A") > old                  # порог расширился


def test_pre_fault_detects_trend():
    flat = np.random.default_rng(6).normal(0, 1, 500)
    assert not CAL.pre_fault_sanity(flat)["suspect"]
    trend = np.abs(np.random.default_rng(7).normal(0, 1, 500)) + np.linspace(0, 10, 500)
    assert CAL.pre_fault_sanity(trend)["suspect"]


def test_enbpi_oob_coverage_holds():
    """EnbPI: OOB-остатки leak-free, порог покрывает ~(1-alpha) на held-out (data-efficient)."""
    from sklearn.linear_model import Ridge
    rng = np.random.default_rng(0); n = 2000
    X = rng.normal(size=(n, 3)); coef = np.array([1.0, 0.5, -0.5])
    y = X @ coef + rng.normal(0, 1, n)
    out = CAL.enbpi_threshold(X, y, lambda Xb, yb: Ridge().fit(Xb, yb), alpha=0.10, B=15)
    assert np.isfinite(out["threshold"]) and out["threshold"] > 0
    assert out["n_oob"] >= n * 0.9                     # почти все точки покрыты OOB
    Xt = rng.normal(size=(6000, 3)); yt = Xt @ coef + rng.normal(0, 1, 6000)
    m = Ridge().fit(X, y)
    cov = float(np.mean(np.abs(yt - m.predict(Xt)) <= out["threshold"]))
    assert 0.86 <= cov <= 0.97                         # ~0.90


def test_coverage_holds_on_iid():
    """На iid-калибровке conformal-порог покрывает ~ (1-alpha) на held-out."""
    cal = np.random.default_rng(8).normal(0, 1, 5000)
    thr = CAL.block_conformal_threshold(cal, alpha=0.05, n_boot=60)["threshold"]
    test = np.random.default_rng(9).normal(0, 1, 20000)
    cov = float(np.mean(np.abs(test) <= thr))
    assert 0.93 <= cov <= 0.985                          # ~0.95 ± допуск
