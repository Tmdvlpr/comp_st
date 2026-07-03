"""Юнит-тесты aging_watchdog.py (детект необходимости ретрейна: range-OOD / R²-коллапс).
Запуск: pytest tests/test_aging_watchdog.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import warnings; warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
import aging_watchdog as AW


def _fit():
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"a": rng.uniform(0, 10, 2000), "b": rng.uniform(0, 5, 2000)})
    y = X["a"] * 2 + rng.normal(0, 0.3, 2000)
    m = CatBoostRegressor(iterations=120, depth=4, loss_function="RMSEWithUncertainty",
                          posterior_sampling=True, random_seed=1, logging_level="Silent")
    m.fit(X, y)
    return m


INFO = {"r2_eval": 0.9, "feat_ranges": {"a": (0.1, 9.9), "b": (0.1, 4.9)}}


def test_no_retrain_when_in_range():
    m = _fit(); rng = np.random.default_rng(5)
    X = pd.DataFrame({"a": rng.uniform(0, 10, 200), "b": rng.uniform(0, 5, 200)})
    y = X["a"] * 2 + rng.normal(0, 0.3, 200)
    assert not AW.check_retrain_needed(m, X, y, INFO)["needs_retrain"]


def test_retrain_on_feature_range_ood():
    """Ровно случай GPA-1: фича вышла за обучающий диапазон (летний ambient)."""
    m = _fit(); rng = np.random.default_rng(6)
    X = pd.DataFrame({"a": rng.uniform(20, 30, 200), "b": rng.uniform(0, 5, 200)})  # 'a' вне [0,10]
    y = X["a"] * 2 + rng.normal(0, 0.3, 200)
    r = AW.check_retrain_needed(m, X, y, INFO)
    assert r["needs_retrain"]
    assert r["range_ood"] is not None and r["range_ood"] > 0.9


def test_retrain_on_r2_collapse_without_ranges():
    m = _fit(); rng = np.random.default_rng(7)
    info = {"r2_eval": 0.9, "feat_ranges": {}}        # без диапазонов → опора на R²
    X = pd.DataFrame({"a": rng.uniform(20, 30, 200), "b": rng.uniform(0, 5, 200)})
    y = X["a"] * 2 + rng.normal(0, 0.3, 200)          # экстраполяция → R² рушится
    r = AW.check_retrain_needed(m, X, y, info)
    assert r["needs_retrain"] and r["recent_r2"] < 0


def test_conditional_shift_caught_when_marginal_clean():
    """Случай ГПА-1: входы В ДИАПАЗОНЕ (feat-range-OOD низкий), но отклик уехал/растёт во
    времени. Маргинальная ось слепа, КОНДИЦИОННАЯ (conditional_shift) ловит по тренду."""
    m = _fit(); rng = np.random.default_rng(11); n = 400
    X = pd.DataFrame({"a": rng.uniform(0, 10, n), "b": rng.uniform(0, 5, n)})   # В ДИАПАЗОНЕ
    ramp = np.linspace(0, 6, n)                          # растущий сдвиг отклика во времени
    y = X["a"] * 2 + ramp + rng.normal(0, 0.3, n)
    assert AW.feature_range_ood_fraction(X, INFO["feat_ranges"]) < 0.1   # маргинально чисто
    cs = AW.conditional_shift(m, X, y)
    assert cs["shift"] and abs(cs["trend_z"]) > 3.0                       # кондиционно — пойман трендом
    assert AW.check_retrain_needed(m, X, y, INFO)["needs_retrain"]


def test_conditional_shift_quiet_when_stable():
    """Стабильный отклик в диапазоне → кондиционная ось молчит (нет ложного флага)."""
    m = _fit(); rng = np.random.default_rng(12); n = 300
    X = pd.DataFrame({"a": rng.uniform(0, 10, n), "b": rng.uniform(0, 5, n)})
    y = X["a"] * 2 + rng.normal(0, 0.3, n)
    assert not AW.conditional_shift(m, X, y)["shift"]


def test_too_few_points_no_decision():
    m = _fit()
    X = pd.DataFrame({"a": [1.0, 2.0], "b": [1.0, 2.0]}); y = np.array([2.0, 4.0])
    assert not AW.check_retrain_needed(m, X, y, INFO)["needs_retrain"]
