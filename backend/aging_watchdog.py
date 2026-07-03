"""
Watchdog устаревания моделей (аудит #9 + §10 ТЗ): детектит, когда модель пора ПЕРЕОБУЧИТЬ.

Два сигнала на свежих healthy steady-точках:
  1. epistemic-OOD: доля точек, где knowledge-uncertainty (virtual ensembles) >> эталона
     калибровки → агрегат работает в НЕЗНАКОМОМ режиме (ровно случай GPA-1: обучен на
     весенних ambient 10-16°, вернулся в летние 26.5° → деревья не экстраполируют → модель
     неприменима). Высокая доля → retrain на свежих данных.
  2. деградация R²: свежий R² на healthy много хуже обучающего r2_eval → связь распалась.
  3. КОНДИЦИОННЫЙ сдвиг (conditional_shift): отклик P(y|режим) уехал при входах В ДИАПАЗОНЕ —
     маргинальные сигналы (1) и feature_range_ood ЕГО НЕ ЛОВЯТ (смотрят распределение входов).
     Доказано ГПА-1 (feat-range-OOD 0.02, а отклик уехал на 9/10 датчиков). Меряется ВРЕМЕННО
     (half-split смещения / тренд), т.к. агрегат маскирует дрейф (ГПА-1: агрегат +1σ «ok»,
     half-split +1.23σ→+4.57σ).

Не переобучает сам — возвращает вердикт {needs_retrain, reasons, ...} для оркестратора
(retrain только на verified-healthy периодах; ср. train-once + re-calibrate). Это и есть
мостик к авто-исцелению GPA-1: как накопит summer-steady, watchdog снимет флаг после retrain.
"""
from __future__ import annotations

import logging
import numpy as np

logger = logging.getLogger(__name__)


def _mean(mdl, X):
    p = np.asarray(mdl.predict(X), float)
    return p[:, 0] if p.ndim == 2 else p


def epistemic_ood_fraction(mdl, X, epistemic_ref: dict, k: float = 1.5) -> float:
    """Доля точек со knowledge-uncertainty выше k·p95-эталона (новизна режима).
    Требует CatBoost с RMSEWithUncertainty+posterior_sampling. Иначе → nan."""
    p95 = (epistemic_ref or {}).get("know_p95")
    if not p95 or not hasattr(mdl, "virtual_ensembles_predict"):
        return float("nan")
    try:
        ve = np.asarray(mdl.virtual_ensembles_predict(
            X, prediction_type="TotalUncertainty", virtual_ensembles_count=10), float)
        know = ve[:, 1]
        return float(np.mean(know > float(p95) * k))
    except Exception as e:
        logger.debug("epistemic_ood: %s", e)
        return float("nan")


def _r2(y, p):
    y, p = np.asarray(y, float), np.asarray(p, float)
    m = np.isfinite(y) & np.isfinite(p)
    y, p = y[m], p[m]
    if len(y) < 5:
        return float("nan")
    ss = float(np.sum((y - np.mean(y)) ** 2))
    return float(1 - np.sum((y - p) ** 2) / ss) if ss > 1e-12 else float("nan")


def feature_range_ood_fraction(X_recent, feat_ranges: dict, margin: float = 0.10) -> float:
    """Доля свежих точек, где ХОТЯ БЫ одна фича вне обучающего диапазона [q01,q99] (±margin).
    НАДЁЖНЫЙ OOD-сигнал для tree-моделей (в отличие от epistemic): прямо ловит экстраполяцию
    (GPA-1: летний ambient 26.5° вне обучающего ≤22°). X_recent — DataFrame с именами фич."""
    if not feat_ranges or not hasattr(X_recent, "columns"):
        return float("nan")
    n = len(X_recent)
    if n == 0:
        return float("nan")
    out_any = np.zeros(n, dtype=bool)
    used = 0
    for f, (lo, hi) in feat_ranges.items():
        if f not in X_recent.columns or lo is None or hi is None:
            continue
        span = (hi - lo) or 1.0
        v = X_recent[f].values
        out_any |= np.asarray((v < lo - margin * span) | (v > hi + margin * span)) & np.isfinite(v)
        used += 1
    return float(np.mean(out_any)) if used else float("nan")


def _trend_z(values) -> float:
    """z-наклон ряда по времени (значимость тренда). Кондиционная ось обязана быть ВРЕМЕННО́Й:
    агрегатное смещение маскирует дрейф внутри окна (ГПА-1: агрегат +1σ ok, +1.23→+4.57 по половинам)."""
    v = np.asarray(values, float); v = v[np.isfinite(v)]; n = len(v)
    if n < 20:
        return 0.0
    t = np.arange(n, dtype=float)
    sl, ic = np.polyfit(t, v, 1)
    res = v - (sl * t + ic)
    se = np.sqrt(np.sum(res ** 2) / max(n - 2, 1)) / (np.sqrt(np.sum((t - t.mean()) ** 2)) + 1e-12)
    return float(sl / (se + 1e-12))


def conditional_shift(mdl, X_recent, y_recent, bias_sigma_thresh: float = 2.0,
                      trend_z_thresh: float = 3.0) -> dict:
    """КОНДИЦИОННАЯ ось новизны — дополняет МАРГИНАЛЬНУЮ feature_range_ood_fraction.
    Ловит сдвиг P(y|режим) при входах В ДИАПАЗОНЕ (те же обороты/давления/погода → другой
    отклик). feature_range_ood/IsolationForest её СЛЕПЫ (смотрят маргинальное распределение
    входов) — доказано ГПА-1: feat-range-OOD 0.02, а отклик уехал на 9/10 датчиков. ВРЕМЕННА́Я
    (не агрегат): half-split смещения остатка + z-наклон — агрегат маскирует тренд (ГПА-1:
    whole-window bias +1σ «ok», half-split вскрыл +1.23σ→+4.57σ). X_recent — в порядке времени."""
    resid = np.asarray(y_recent, float) - _mean(mdl, X_recent)
    resid = resid[np.isfinite(resid)]; n = len(resid)
    out = {"shift": False, "bias_sigma": None, "trend_z": None, "half_delta_sigma": None}
    if n < 40:
        return out
    rstd = float(np.std(resid)) or 1e-9
    bias_sigma = float(np.mean(resid)) / rstd
    tz = _trend_z(resid)
    sp = int(n * 0.6)
    half_delta = (float(np.mean(resid[sp:])) - float(np.mean(resid[:sp]))) / rstd
    out.update(bias_sigma=round(bias_sigma, 2), trend_z=round(tz, 2),
               half_delta_sigma=round(half_delta, 2))
    out["shift"] = (abs(bias_sigma) > bias_sigma_thresh or abs(tz) > trend_z_thresh
                    or abs(half_delta) > bias_sigma_thresh)
    return out


def check_retrain_needed(mdl, X_recent, y_recent, info: dict,
                         ood_thresh: float = 0.20, r2_drop: float = 0.3) -> dict:
    """Вердикт о ретрейне по свежим healthy steady-точкам. X_recent — DataFrame (имена фич),
    y_recent — значения. info — запись metadata (r2_eval, feat_ranges, epistemic_ref).
    needs_retrain, если: feature-range-OOD > ood_thresh (НАДЁЖНО), ИЛИ epistemic-OOD > ood_thresh
    (вторично), ИЛИ R² просел ≥ r2_drop и стал < 0."""
    out = {"needs_retrain": False, "reasons": [], "n_recent": int(len(y_recent)),
           "range_ood": None, "epistemic_ood": None, "recent_r2": None, "train_r2": info.get("r2_eval")}
    if len(y_recent) < 30:
        out["reasons"].append("мало свежих точек")
        return out
    rng_ood = feature_range_ood_fraction(X_recent, info.get("feat_ranges") or {})
    epi_ood = epistemic_ood_fraction(mdl, X_recent, info.get("epistemic_ref") or {})
    recent_r2 = _r2(y_recent, _mean(mdl, X_recent))
    out["range_ood"] = None if not np.isfinite(rng_ood) else round(rng_ood, 3)
    out["epistemic_ood"] = None if not np.isfinite(epi_ood) else round(epi_ood, 3)
    out["recent_r2"] = None if not np.isfinite(recent_r2) else round(recent_r2, 3)

    if np.isfinite(rng_ood) and rng_ood > ood_thresh:
        out["needs_retrain"] = True
        out["reasons"].append(f"feature-range-OOD {rng_ood:.0%} (режим вне обучающего, экстраполяция)")
    if np.isfinite(epi_ood) and epi_ood > ood_thresh:
        out["needs_retrain"] = True
        out["reasons"].append(f"epistemic-OOD {epi_ood:.0%}")
    tr_r2 = info.get("r2_eval")
    if (isinstance(tr_r2, (int, float)) and np.isfinite(recent_r2)
            and (tr_r2 - recent_r2) >= r2_drop and recent_r2 < 0):
        out["needs_retrain"] = True
        out["reasons"].append(f"R² просел {tr_r2:.2f}→{recent_r2:.2f}")
    cond = conditional_shift(mdl, X_recent, y_recent)
    out["cond_shift"] = cond
    if cond["shift"]:
        out["needs_retrain"] = True
        out["reasons"].append(
            f"кондиционный сдвиг (bias {cond['bias_sigma']}σ, тренд z={cond['trend_z']}, "
            f"half-Δ {cond['half_delta_sigma']}σ) при входах-в-диапазоне")
    return out
