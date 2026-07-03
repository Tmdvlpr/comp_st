# -*- coding: utf-8 -*-
"""
ЕДИНЫЙ 5-КЛАССОВЫЙ ДЕТЕКТОР (Тир-3, замена пирамиды порогов live_predict).

Вместо россыпи независимых флагов (seasonal/trend/roc/frozen/neg/ml/epi), каждый из
которых ставит свой маркер и вместе они дают спам, — ОДНА упорядоченная лестница решений,
возвращающая для каждой точки ровно ОДИН класс:

    НЕ_ФИЗ    — значение вне физических границ / sentinel     (ошибка датчика)
    ЗАЛИПАНИЕ — константа при работающем агрегате              (ошибка датчика)
    ВЫБРОС    — одиночный скачок, не персистентный, вернулся   (ошибка датчика)
    РЕЖИМ     — связь датчиков держится / сдвиг согласован /    (норм. работа, не дефект)
                fleet-common — рабочая точка изменилась
    ДЕФЕКТ    — связь сломалась, персистентно, unit-specific    (реальная поломка)
    НОРМА     — ничего из вышеперечисленного

Дискриминатор РЕЖИМ vs ДЕФЕКТ (доказан на данных, см. AUDIT_2026-07-03.md):
  остаток связи r = |факт − предикт_по_соседям| / range
    • r мал  → связь держится → согласованный сдвиг = РЕЖИМ (даже если уровень уехал)
    • r велик + персистентно → кандидат в ДЕФЕКТ, НО:
        - согласованный сдвиг ВНЕ обучающей зоны тоже даёт большой r (экстраполяция, С4),
          поэтому большой r ГЕЙТИТСЯ флот-контрастом и сменой рабочей точки:
            fleet_common ∨ regime_key сменился → РЕЖИМ
            unit_specific ∨ SPE высок          → ДЕФЕКТ
            флот недоступен (неполный парк)    → ДЕФЕКТ, но помечен fleet_unconfirmed

Порядок лестницы (приоритет сверху вниз) НЕ меняется: сначала отсекаем артефакты датчика,
потом объясняем режимом, и только необъяснённое устойчивое уникальное = дефект.

Чистые функции над numpy/pandas. Никакой зависимости от БД/цикла — тестируемо офлайн.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

# Классы (строковые метки — идут во фронт/БД как status)
NORM      = "норма"
NONPHYS   = "не_физ"
STUCK     = "залипание"
SPIKE     = "выброс"
REGIME    = "режим"
DEFECT    = "дефект"

SENSOR_FAULTS = (NONPHYS, STUCK, SPIKE)   # три класса «ошибка датчика»

# ── Пороги (все настраиваемы; дефолты обоснованы эмпирикой на кэше ohangaron) ──
DEFAULTS = dict(
    tau_rel      = 0.20,   # |остаток связи|/range > tau → связь сломана (healthy вибро ~0.03-0.11)
    spike_mad    = 8.0,    # |ΔV| > spike_mad·MAD(ΔV_train) → аппаратный скачок
    stuck_len    = 12,     # ≥ N подряд одинаковых при работе (12×5мин = 1ч) → залипание
    persist_k    = 3,      # K из N подряд для устойчивого разрыва связи
    persist_n    = 5,
    level_eps    = 0.10,   # |факт − healthy-медиана|/range > eps → «уровень уехал» (для РЕЖИМ)
    spe_defect   = 0.30,   # доля SPE>предел на окне > 0.30 → структурный разрыв (unit-level)
    episode_win  = 48,     # окно эпизода (48×5мин = 4ч)
    episode_frac = 0.25,   # доля точек класса в окне > frac → эпизод-тревога (healthy ~2%)
    sentinels    = (-9999, 9999, -999, -32767, 32767),
)


def _persist(mask: np.ndarray, k: int, n: int) -> np.ndarray:
    """K-из-N в трейлинг-окне: гасит одиночные, пропускает устойчивые кластеры."""
    if k <= 1:
        return mask
    cnt = pd.Series(mask.astype(int)).rolling(n, min_periods=1).sum().values
    return mask & (cnt >= k)


def _stuck_mask(y: np.ndarray, running: np.ndarray, min_len: int) -> np.ndarray:
    """Константа ≥ min_len подряд при работающем агрегате → залипание."""
    s = pd.Series(y)
    grp = (s.diff().abs() > 1e-12).cumsum()      # блоки постоянного значения
    size = s.groupby(grp).transform("size").values
    return (size >= min_len) & running


def classify_points(
    y: np.ndarray,                    # факт (сырой сигнал датчика)
    pred_rel: np.ndarray,             # предикт по СОСЕДЯМ (relationship model); NaN где нет
    sensor_range: float,
    running: np.ndarray,              # агрегат работает (steady)
    *,
    limits: tuple | None = None,      # (lo, hi) физические границы или None
    healthy_median: float | None = None,   # замороженная healthy-медиана режима (для «уровень уехал»)
    dv_mad_train: float | None = None,      # MAD(ΔV) на train (шумовой масштаб для выброса)
    regime_changed: np.ndarray | None = None,  # bool: сменился regime_key на этой точке
    fleet_common: np.ndarray | bool | None = None,   # подъём есть у всего парка (сезон/режим)
    unit_specific: np.ndarray | bool | None = None,  # подъём уникален для этого ГПА
    spe_exceed_frac: float | None = None,   # доля SPE>предел на окне (unit-level структура)
    cfg: dict | None = None,
) -> tuple[np.ndarray, dict]:
    """Возвращает (labels[str], diag). Лестница приоритетов — см. док модуля.

    Пред- и пост-условия:
      • pred_rel=NaN (нет модели связи) → r не участвует, режим/дефект решаются по
        уровню+флоту+SPE (деградация к self-band поведению, но БЕЗ ложного дефекта).
      • fleet_common/unit_specific=None (неполный парк) → большой r не может быть подтверждён
        как режим → помечаем ДЕФЕКТ + fleet_unconfirmed (честно, без глушения).
    """
    c = {**DEFAULTS, **(cfg or {})}
    n = len(y)
    labels = np.full(n, NORM, dtype=object)
    y = np.asarray(y, float)
    sr = float(sensor_range) if sensor_range and sensor_range > 0 else 1.0
    running = np.asarray(running, bool)

    # ── 1. НЕ_ФИЗ: sentinel / вне границ / не-конечное (только на работающем — на стоянке молчим) ──
    nonphys = ~np.isfinite(y)
    for sv in c["sentinels"]:
        nonphys |= (np.abs(y - sv) < 1e-6)
    if limits is not None:
        lo, hi = limits
        if lo is not None:
            nonphys |= (y < lo)
        if hi is not None:
            nonphys |= (y > hi)
    nonphys &= running
    labels[nonphys] = NONPHYS

    free = running & (labels == NORM)   # точки, ещё не классифицированные, на работе

    # ── 2. ЗАЛИПАНИЕ ──
    stuck = _stuck_mask(y, running, c["stuck_len"]) & free
    labels[stuck] = STUCK
    free &= (labels == NORM)

    # остаток связи (доля range); NaN-безопасно
    r = np.full(n, np.nan)
    if pred_rel is not None:
        pr = np.asarray(pred_rel, float)
        ok = np.isfinite(pr) & np.isfinite(y)
        r[ok] = np.abs(y[ok] - pr[ok]) / sr
    rel_break = np.isfinite(r) & (r > c["tau_rel"])
    rel_break_persist = _persist(rel_break, c["persist_k"], c["persist_n"]) & free

    # ── 3. ВЫБРОС: одиночный скачок ΔV ≫ шума, НЕ переходящий в устойчивый разрыв связи ──
    spike = np.zeros(n, bool)
    if dv_mad_train and dv_mad_train > 0:
        dv = np.abs(np.concatenate([[0.0], np.diff(y)]))
        spike_raw = dv > c["spike_mad"] * dv_mad_train
        spike = spike_raw & ~rel_break_persist & free   # одиночный, не устойчивый разрыв
    labels[spike] = SPIKE
    free &= (labels == NORM)

    # ── 4/5. РЕЖИМ vs ДЕФЕКТ ──
    def _as_arr(x):
        if x is None:
            return None
        if np.isscalar(x):
            return np.full(n, bool(x))
        return np.asarray(x, bool)
    fc = _as_arr(fleet_common)
    us = _as_arr(unit_specific)
    regch = _as_arr(regime_changed)
    spe_hi = (spe_exceed_frac is not None) and (spe_exceed_frac > c["spe_defect"])

    level_moved = np.zeros(n, bool)
    if healthy_median is not None:
        level_moved = np.abs(y - float(healthy_median)) / sr > c["level_eps"]

    # большой устойчивый остаток связи = кандидат в дефект → гейтим флотом/сменой режима
    cand = rel_break_persist.copy()
    fleet_unconfirmed = np.zeros(n, bool)
    for i in np.where(cand & free)[0]:
        is_fleet   = (fc is not None and fc[i])
        is_regch   = (regch is not None and regch[i])
        is_unit    = (us is not None and us[i])
        if is_fleet or is_regch:
            labels[i] = REGIME                    # согласовано с парком / рабочая точка сменилась
        elif is_unit or spe_hi:
            labels[i] = DEFECT                    # уникально для агрегата / структура сломана
        else:
            labels[i] = DEFECT                    # флот недоступен → консервативно дефект,
            fleet_unconfirmed[i] = True           #   но честно помечаем «не подтверждён флотом»
    free &= (labels == NORM)

    # связь держится (r мал), но уровень уехал → РЕЖИМ (согласованный сдвиг рабочей точки)
    rel_holds = free & (~np.isfinite(r) | (r <= c["tau_rel"]))
    labels[rel_holds & level_moved] = REGIME

    diag = dict(
        r_median=float(np.nanmedian(r)) if np.isfinite(r).any() else None,
        n_rel_break=int(rel_break_persist.sum()),
        fleet_unconfirmed=int(fleet_unconfirmed.sum()),
        counts={k: int((labels == k).sum()) for k in (NORM, NONPHYS, STUCK, SPIKE, REGIME, DEFECT)},
    )
    return labels, diag


def episodes(labels: np.ndarray, index: pd.DatetimeIndex, cfg: dict | None = None) -> list[dict]:
    """Сворачивает поточечные метки в ЭПИЗОДЫ (конец точечного спама).
    Эпизод = непрерывный участок одного не-НОРМА класса; тревога поднимается, только если
    доля класса в трейлинг-окне превышает порог (для датчик-классов — сразу, они дискретны)."""
    c = {**DEFAULTS, **(cfg or {})}
    out = []
    n = len(labels)
    if n == 0:
        return out
    # эпизодный гейт только для РЕЖИМ/ДЕФЕКТ (стохастика); датчик-классы дискретны и проходят как есть
    i = 0
    while i < n:
        cls = labels[i]
        if cls == NORM:
            i += 1
            continue
        j = i
        while j < n and labels[j] == cls:
            j += 1
        seg_len = j - i
        # для физ-режимных классов требуем плотность (гасим одиночные хвосты conformal ~2%)
        keep = True
        if cls in (REGIME, DEFECT):
            keep = seg_len >= max(1, int(c["episode_frac"] * min(c["episode_win"], seg_len))) \
                   and seg_len >= c["persist_k"]
        if keep:
            out.append(dict(cls=cls, start=index[i], end=index[j - 1], n=seg_len))
        i = j
    return out
