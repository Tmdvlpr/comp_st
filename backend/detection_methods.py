"""
Методы детекции и калибровки порогов (порт research-методологии, Рисёрч РАЗДЕЛ A/9).

- robust_std: σ через MAD (устойчиво к выбросам).
- conformal_threshold / pot_evt_threshold: пороги на свежей норме (split-conformal, POT-EVT).
- ewma / cusum / page_hinkley: детекторы медленного дрейфа.
- run_length_filter: гасит одиночные выбросы (требует длительности).
- reversibility: обратимо (режим/погода) vs необратимая деградация (монотонный тренд остатка).

Калибровка порогов ведётся на СВЕЖЕЙ норме holdout (первые CALIB_DAYS), не на train.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Параметры по умолчанию (Рисёрч стр. 140–143)
CONFORMAL_ALPHA = 0.01    # split-conformal: целевое покрытие ~99%
POT_ALPHA = 1e-3          # POT-EVT: вероятность превышения экстремума
MIN_DURATION = 3          # требуем N подряд точек выше порога
CALIB_DAYS = 7            # первые дни holdout как «свежая норма»


def robust_std(x) -> float:
    """σ_robust = 1.4826·MAD; fallback на std. Всегда > 0 и конечно."""
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    if x.size == 0:
        return 1.0
    s = float(1.4826 * np.median(np.abs(x - np.median(x))))
    if not np.isfinite(s) or s <= 0:
        s = float(np.std(x))
    return s if (np.isfinite(s) and s > 0) else 1.0


def conformal_threshold(resid_calib, alpha: float = CONFORMAL_ALPHA) -> float:
    """Split-conformal порог: (1-alpha)-квантиль |остатка| на калибровочной норме."""
    a = np.sort(np.abs(np.asarray(resid_calib, float)))
    a = a[~np.isnan(a)]
    n = len(a)
    if n < 20:
        return np.nan
    k = int(np.ceil((n + 1) * (1 - alpha))) - 1
    return float(a[min(max(k, 0), n - 1)])


def pot_evt_threshold(resid_calib, alpha: float = POT_ALPHA, u_q: float = 0.95) -> float:
    """POT-EVT порог: хвост |остатка| моделируется обобщённым Парето; уровень с P~alpha."""
    from scipy.stats import genpareto
    a = np.abs(np.asarray(resid_calib, float))
    a = a[~np.isnan(a)]
    n = len(a)
    if n < 100:
        return np.nan
    u = float(np.quantile(a, u_q))
    exc = a[a > u] - u
    Nu = len(exc)
    if Nu < 30:
        return np.nan
    try:
        xi, _, sc = genpareto.fit(exc, floc=0.0)
    except Exception:
        return np.nan
    if abs(xi) > 1e-6:
        return float(u + (sc / xi) * ((alpha * n / Nu) ** (-xi) - 1.0))
    return float(u + sc * np.log(Nu / (alpha * n)))


def ewma(z, alpha: float = 0.05):
    """EWMA сглаживание нормализованного остатка (для мониторинга дрейфа индексов)."""
    return pd.Series(z).ewm(alpha=alpha, adjust=False).mean().values


def cusum(z, k: float = 0.5, h: float = 5.0):
    """CUSUM по нормализованному остатку. Возвращает (sp, sm, alarm_mask)."""
    z = np.nan_to_num(np.asarray(z, float))
    sp = np.zeros(len(z))
    sm = np.zeros(len(z))
    for i in range(1, len(z)):
        sp[i] = max(0.0, sp[i - 1] + z[i] - k)
        sm[i] = max(0.0, sm[i - 1] - z[i] - k)
    return sp, sm, (sp > h) | (sm > h)


def page_hinkley(x, delta: float = 0.005, lam: float | None = None, alpha: float = 0.999):
    """Page-Hinkley: накопление отклонения от бегущего среднего. (PH, alarm_mask)."""
    x = np.nan_to_num(np.asarray(x, float))
    if lam is None:
        lam = 5.0 * (np.std(x) or 1.0)
    mean = 0.0
    mT = 0.0
    PH = np.zeros(len(x))
    mn = 0.0
    al = np.zeros(len(x), bool)
    for i in range(len(x)):
        mean = alpha * mean + (1 - alpha) * x[i] if i else x[i]
        mT += x[i] - mean - delta
        PH[i] = mT
        mn = min(mn, mT)
        al[i] = (mT - mn) > lam
    return PH, al


def run_length_filter(flags, min_len: int = MIN_DURATION):
    """Оставляет только срабатывания длительностью ≥ min_len подряд (гасит одиночные)."""
    f = np.asarray(flags, bool)
    out = np.zeros_like(f)
    i = 0
    while i < len(f):
        if f[i]:
            j = i
            while j < len(f) and f[j]:
                j += 1
            if j - i >= min_len:
                out[i:j] = True
            i = j
        else:
            i += 1
    return out


def reversibility(resid, ewma_alpha: float = 0.05, mono_frac: float = 0.6) -> str:
    """Грубое различение: монотонный (не отыгрывающий) тренд остатка → необратимая деградация."""
    e = ewma(resid, ewma_alpha)
    if len(e) < 10:
        return "?"
    slope = float(np.polyfit(np.arange(len(e)), e, 1)[0])
    frac_mono = float(np.mean(np.sign(np.diff(e)) == np.sign(slope))) if slope != 0 else 0.5
    return "деградация(необратимо)" if (abs(slope) > 0 and frac_mono > mono_frac) else "обратимо(режим/погода)"
