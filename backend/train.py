"""
Единый пайплайн обучения моделей здоровья ГПА — residual-based fault detection.
Версия: v23.0-norm-conformal. ВСЯ методология обучения в одном файле.

Метод (как в визуализации «Anomaly»): модель учится на ИСПРАВНОМ оборудовании предсказывать
рабочий сигнал; в эксплуатации аномалия = расхождение факта с предиктом, нормированное на
неопределённость. Два детектора:

  1. НОРМАЛИЗОВАННЫЙ (Mondrian) КОНФОРМНЫЙ КОРИДОР (гибрид):
     нонконформити-скор = |факт − предикт| / σ,  σ — полная неопределённость из ВИРТУАЛЬНОГО
     АНСАМБЛЯ CatBoost (σ² = u_epi + u_ale). block-Mondrian conformal по режимам даёт пер-режимный
     квантиль q̂ (безразмерный). Коридор в live: предикт ± q̂(режим)·σ. Узкий и калиброванный в
     знакомом режиме, САМ раздувается там, где модель не уверена (σ велик).
  2. ЭПИСТЕМИЧЕСКАЯ НЕОПРЕДЕЛЁННОСТЬ — отдельный флаг новизны: u_epi(x) > κ → «модель в незнакомом
     режиме». κ — эмпирический по здоровым данным (p95).

Механизм неопределённости (виртуальные ансамбли, два флага CatBoost ВМЕСТЕ):
  - posterior_sampling=True (SGLB) → траектория бустинга нарезается на K виртуальных моделей →
    эпистемика u_epi = разброс средних μ_k между ними;
  - loss=RMSEWithUncertainty → каждая виртуальная модель выдаёт (μ_k, σ²_k) → алеаторика
    u_ale = среднее σ²_k. Без этого лосса u_ale взять неоткуда.
  - virtual_ensembles_predict(prediction_type='TotalUncertainty') → [μ̄, u_epi, u_ale]
    (проверено на CatBoost 1.2.10: столбцы 0=mean, 1=u_epi, 2=u_ale).

Прочее: обучение только на healthy-steady точках; block-Mondrian (НЕ ACI) лечит автокорреляцию
(n_eff=n/L, L из ACF); медленную деградацию (EWMA/CUSUM) ловит инференс (detection_methods.py).
Модель train-once (≤ cutoff), переживает рестарты; освежается только калибровка.

Запуск:
    python train.py --station ohangaron
    python train.py --station ohangaron --cutoff-date 2026-06-12
    python train.py --station ohangaron --gpa 3          # переобучить ОДИН ГПА (merge в metadata)
    python train.py --station ohangaron --dry-run
"""
from __future__ import annotations

import logging
import os
import dataclasses
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import joblib

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════════
#  СЕКЦИЯ 1. ЧИСТКА ДАННЫХ И ВЕРДИКТ ПО ДАТЧИКАМ
#  Работает с СЕМАНТИЧЕСКИ именованными колонками ОДНОГО ГПА (без суффикса __GPAn).
# ════════════════════════════════════════════════════════════════════════════════

DEAD_NUNIQUE = 2          # n_unique ≤ 2 → "dead"
LOW_VAR_EPS = 1e-9        # минимальная дисперсия «живого» сигнала
UNRELIABLE_NA = 0.40      # доля пропусков > 40% → "unreliable"
FROZEN_HOURS = 6.0        # макс. серия-константа при работе > 6ч → "frozen"
CORR_REDUNDANT = 0.97     # |r| > 0.97 → избыточная пара
STEP_HOURS = 5 / 60       # шаг сетки (5 мин)
SENTINELS = (-9999, 9999, -999, -32767, 32767)
LEAK_PREFIX = "gas_leak"  # загазованность — битая калибровка, исключаем целиком
EXOGENOUS = {"ambient_temp"}   # ffill-погода: константа в пределах часа — не «frozen»
UNMODELABLE = {"dead", "frozen", "leak", "dead_train_alive_holdout"}  # как таргет нельзя

# Дискретные флаги режима (для steady_running_mask)
IDLE_FLAGS = {
    "gtd_running": "is_gtd_status_running",
    "blowdown": "is_emergency_stop_with_venting",
    "mode_ring": "is_mode_ring",
    "mode_main": "is_mode_mainline",
}


def continuous_cols(df: pd.DataFrame) -> list[str]:
    """Непрерывные сигналы: числовые колонки, кроме булевых флагов (is_*, states_*)."""
    out = []
    for c in df.columns:
        if c.startswith("is_") or c.startswith("states_"):
            continue
        if pd.api.types.is_bool_dtype(df[c]):
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            out.append(c)
    return out


def physically_clean(df: pd.DataFrame, limits: dict, sentinels=SENTINELS) -> pd.DataFrame:
    """sentinel-коды и (если задано в limits) выход за границы → NaN. Без заводских уставок."""
    out = df.copy()
    for c in continuous_cols(out):
        s = out[c]
        bad = s.isin(sentinels)
        lo, hi = limits.get(c, (None, None))
        if lo is not None:
            bad = bad | (s < lo)
        if hi is not None:
            bad = bad | (s > hi)
        out.loc[bad, c] = np.nan
    return out


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


def spike_mask(s: pd.Series, train_idx, k: float = 30.0) -> pd.Series:
    """Data-driven спайк: |Δ| > k·MAD(Δ на train). Статистика самих данных, не заводское число."""
    d = s.diff().abs()
    md = robust_std(d[train_idx].values)
    if not np.isfinite(md) or md <= 0:
        return pd.Series(False, index=s.index)
    return (d > k * md).fillna(False)


def local_freeze_points(s: pd.Series, running: pd.Series, min_run_steps: int) -> pd.Series:
    grp = (s != s.shift()).cumsum()
    runlen = s.groupby(grp).transform("size")
    return (runlen > min_run_steps) & running.fillna(False) & s.notna()


def steady_running_mask(df: pd.DataFrame, warmup: int = 6) -> pd.Series:
    """Маска установившегося рабочего режима: дискретные флаги (если есть) + rpm fallback + warmup.
    Исключает стравливание/останов и переключения режима кольцо↔магистраль."""
    idx = df.index
    run = None
    gr = IDLE_FLAGS.get("gtd_running")
    if gr and gr in df.columns:
        st = df[gr].fillna(0) > 0.5
        if st.sum() > 0.15 * len(st):
            run = st
    if run is None:
        if "rpm_tvd" in df.columns:
            rpm = df["rpm_tvd"]
        else:
            rpm_cols = [c for c in df.columns if c.startswith("rpm_")]
            rpm = df[rpm_cols[0]] if rpm_cols else pd.Series(1.0, index=idx)
        rng = max(float(rpm.quantile(0.99) - rpm.quantile(0.01)), 1.0)
        run = rpm > 0.05 * rng
    bd = IDLE_FLAGS.get("blowdown")
    if bd and bd in df.columns:
        run = run & ~(df[bd].fillna(0) > 0.5)
    for fk in ("mode_ring", "mode_main"):
        col = IDLE_FLAGS.get(fk)
        if col and col in df.columns:
            switch = df[col].ffill().diff().abs() > 0
            sw = switch.copy()
            for k in range(1, 4):
                sw = sw | switch.shift(k).fillna(False) | switch.shift(-k).fillna(False)
            run = run & ~sw.fillna(False)
    stop = ~run.fillna(False)
    stop_ext = stop.copy()
    for k in range(1, warmup + 1):
        stop_ext = stop_ext | stop.shift(k).fillna(True)
    return (~stop_ext).reindex(idx).fillna(False)


def column_verdict(df_gpa: pd.DataFrame, train_cutoff, data_end=None) -> pd.DataFrame:
    """Авто-вердикт по каждому непрерывному сигналу ОДНОГО ГПА (df уже physically_clean).
    Возвращает DataFrame [sensor, na_pct, n_unique, std, std_holdout, max_const_h, verdict]."""
    run = steady_running_mask(df_gpa)
    tr = df_gpa.index <= train_cutoff
    ho_run = run & (df_gpa.index > train_cutoff)
    if data_end is not None:
        ho_run = ho_run & (df_gpa.index <= data_end)
    rows = []
    for c in continuous_cols(df_gpa):
        s = df_gpa.loc[tr, c]
        na = float(s.isna().mean())
        nu = int(s.nunique(dropna=True))
        sd = float(s.std(skipna=True) or 0)
        sd_ho = float(df_gpa.loc[ho_run, c].std(skipna=True) or 0)
        sv = s.where(run[tr].fillna(False))
        if sv.notna().any():
            grp = (sv != sv.shift()).cumsum()
            mr = int(sv.dropna().groupby(grp).transform("size").max() or 0)
        else:
            mr = 0
        mrh = round(mr * STEP_HOURS, 1)
        is_dead = nu <= DEAD_NUNIQUE or sd < LOW_VAR_EPS
        if c.startswith(LEAK_PREFIX):
            v = "leak"
        elif is_dead:
            v = "dead_train_alive_holdout" if sd_ho > 10 * LOW_VAR_EPS else "dead"
        elif mrh > FROZEN_HOURS and c not in EXOGENOUS:
            v = "frozen" if sd_ho <= 10 * LOW_VAR_EPS else "ok"
        elif na > UNRELIABLE_NA:
            v = "unreliable"
        else:
            v = "ok"
        rows.append(dict(sensor=c, na_pct=round(100 * na, 1), n_unique=nu,
                         std=round(sd, 4), std_holdout=round(sd_ho, 4),
                         max_const_h=mrh, verdict=v))
    return pd.DataFrame(rows)


def keep_exclude(verdict_df: pd.DataFrame, all_cols: list[str]) -> tuple[list[str], set[str]]:
    """Из вердикта → (KEEP, EXCLUDE). KEEP = ok-сигналы; EXCLUDE = всё остальное."""
    exclude = set(verdict_df[verdict_df.verdict != "ok"].sensor)
    keep = [c for c in all_cols if c not in exclude]
    return keep, exclude


def redundant_pairs(df_gpa: pd.DataFrame, cols: list[str], thr: float = CORR_REDUNDANT) -> list[tuple]:
    """Пары |r|>thr на рабочем режиме. Возвращает [(a, b, r)] — кандидаты на отбрасывание дубля."""
    cols = [c for c in cols if c in df_gpa.columns]
    if len(cols) < 2:
        return []
    run = steady_running_mask(df_gpa)
    corr = df_gpa.loc[run, cols].corr().abs()
    pairs = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            r = corr.loc[a, b]
            if np.isfinite(r) and r > thr:
                pairs.append((a, b, round(float(r), 4)))
    return pairs


# ════════════════════════════════════════════════════════════════════════════════
#  СЕКЦИЯ 2. ДОМЕННЫЕ (ФИЗИЧЕСКИ ОБОСНОВАННЫЕ) ФИЧИ И ИНДЕКСЫ ЗДОРОВЬЯ
#  Источник газовых констант — реальный состав газа КС «Ахангаран».
# ════════════════════════════════════════════════════════════════════════════════

GAS_COMPOSITION = {  # мол.% и свойства компонентов
    "CH4":    dict(x=94.29, M=16.043, cp=35.69),
    "C2H6":   dict(x=2.18,  M=30.070, cp=52.49),
    "iC4H10": dict(x=0.55,  M=58.122, cp=97.20),
    "iC5H12": dict(x=1.09,  M=72.149, cp=120.1),
    "N2":     dict(x=1.27,  M=28.014, cp=29.12),
    "CO2":    dict(x=0.62,  M=44.010, cp=37.11),
}
R_UNIVERSAL = 8.31446
_x = {k: v["x"] / 100 for k, v in GAS_COMPOSITION.items()}
GAS_M = sum(_x[k] * GAS_COMPOSITION[k]["M"] for k in _x)          # г/моль
_cp = sum(_x[k] * GAS_COMPOSITION[k]["cp"] for k in _x)
GAS_K = _cp / (_cp - R_UNIVERSAL)                                 # cp/cv ≈ 1.2735
GAS_R = R_UNIVERSAL / (GAS_M / 1000)                             # Дж/(кг·К) ≈ 445.67
GAS_Z = 0.94                                                     # коэф. сжимаемости (средняя оценка)
P_ATM_MPA = 0.101325
T_K0 = 273.15
T_REF_K = 288.15                                                 # ~15°C — реф. температура для приведения


def configure_gas(consts: dict | None) -> None:
    """Переопределить газовые константы из конфига станции (опционально)."""
    if not consts:
        return
    global GAS_K, GAS_R, GAS_Z, P_ATM_MPA, T_REF_K
    GAS_K = float(consts.get("k", GAS_K))
    GAS_R = float(consts.get("R", GAS_R))
    GAS_Z = float(consts.get("Z", GAS_Z))
    P_ATM_MPA = float(consts.get("p_atm_mpa", P_ATM_MPA))
    T_REF_K = float(consts.get("t_ref_k", T_REF_K))


def _abs(p):
    return p + P_ATM_MPA


def _K(t):
    return t + T_K0


def poly_nm1n(P1, T1, P2, T2):
    """Политропный показатель (n-1)/n = ln(T2/T1)/ln(P2/P1) на абсолютных P,T."""
    P1a, P2a, T1k, T2k = _abs(P1), _abs(P2), _K(T1), _K(T2)
    valid = (P1a > 0) & (P2a > P1a) & (T1k > 0) & (T2k > T1k)
    return (np.log(T2k / T1k) / np.log(P2a / P1a)).where(valid)


def polytropic_eff(P1, T1, P2, T2):
    """Политропный КПД η_p = [(k-1)/k] / [(n-1)/n], доля 0..1.5."""
    nm1n = poly_nm1n(P1, T1, P2, T2)
    eta = ((GAS_K - 1) / GAS_K) / nm1n
    return eta.where(np.isfinite(eta) & (eta > 0) & (eta < 1.5))


def polytropic_head(P1, T1, P2, T2):
    """Политропный напор H_p, кДж/кг."""
    P1a, P2a, T1k = _abs(P1), _abs(P2), _K(T1)
    nm1n = poly_nm1n(P1, T1, P2, T2)
    Hp = GAS_Z * GAS_R * T1k * (1.0 / nm1n) * ((P2a / P1a) ** nm1n - 1.0) / 1000.0
    return Hp.where(np.isfinite(Hp) & (Hp > 0))


def reduced_rpm(n, t1_c):
    """Приведённые обороты n_пр = n / sqrt(T1_K / T_REF_K), T1 = ambient (°C)."""
    return n / np.sqrt(_K(t1_c) / T_REF_K)


def fit_shaft_line(x, y, deg=2):
    """Полиномиальный baseline y=f(x) на здоровых train-точках; коэффициенты или None."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 50:
        return None
    return np.polyfit(x[m], y[m], deg)


def shaft_predict(coef, x):
    return np.polyval(coef, x) if coef is not None else np.full_like(np.asarray(x, float), np.nan)


# Доменные фичи conditioning-типа (можно в предикторы откликов)
COND_DOMAIN_ALL = ["rpm_tvd_red", "rpm_tnd_red", "rpm_st_red", "pressure_ratio"]
# Доменные индексы здоровья (мониторим напрямую; НЕ кладём в conditioning)
HEALTH_INDEX_ALL = ["polytropic_eff", "polytropic_head", "dT_disch", "dT_cooler", "avo_approach",
                    "specific_fuel", "shaft_resid_tnd", "shaft_ratio", "shaft_resid_st", "dT_bearings"]
# Все доменные колонки, которые может создать add_domain_features_gpa
DOMAIN_COLS = ["pressure_ratio", "polytropic_eff", "polytropic_head", "dT_disch",
               "dT_cooler", "avo_approach", "rpm_tvd_red", "rpm_tnd_red", "rpm_st_red",
               "shaft_resid_tnd", "shaft_ratio", "shaft_resid_st", "specific_fuel", "dT_bearings"]


def add_domain_features_gpa(df_gpa: pd.DataFrame, running_mask=None, train_cutoff=None) -> pd.DataFrame:
    """Добавляет доменные фичи к данным ОДНОГО ГПА (семантические имена без суффикса)."""
    df = df_gpa.copy()

    def col(name):
        return df[name] if name in df.columns else None

    P1, T1 = col("gas_pressure_in_gpa"), col("gas_temp_in_gpa")
    P2, T2 = col("gas_pressure_out_gpa"), col("gas_temp_out_gpa")

    # 4.1 термодинамика нагнетателя
    if all(v is not None for v in (P1, T1, P2, T2)):
        df["pressure_ratio"] = (_abs(P2) / _abs(P1)).where(_abs(P1) > 0)
        df["polytropic_eff"] = polytropic_eff(P1, T1, P2, T2)
        df["polytropic_head"] = polytropic_head(P1, T1, P2, T2)
        df["dT_disch"] = T2 - T1

    # 4.2 АВО: range и approach
    if "gas_temp_out_gpa" in df and "gas_temp_out_avo" in df:
        df["dT_cooler"] = df["gas_temp_out_gpa"] - df["gas_temp_out_avo"]
    if "gas_temp_out_avo" in df and "ambient_temp" in df:
        df["avo_approach"] = df["gas_temp_out_avo"] - df["ambient_temp"]

    # 4.3 приведённые обороты + shaft mismatch
    if "ambient_temp" in df:
        amb = df["ambient_temp"]
        for n in ("rpm_tvd", "rpm_tnd", "rpm_st"):
            if n in df:
                df[n + "_red"] = reduced_rpm(df[n], amb)
        if {"rpm_tvd_red", "rpm_tnd_red"}.issubset(df.columns):
            if running_mask is not None:
                tr = running_mask.reindex(df.index, fill_value=False)
            else:
                tr = pd.Series(True, index=df.index)
            if train_cutoff is not None:
                tr = tr & (df.index <= train_cutoff)
            coef = fit_shaft_line(df.loc[tr, "rpm_tvd_red"].values,
                                  df.loc[tr, "rpm_tnd_red"].values, deg=2)
            df["shaft_resid_tnd"] = df["rpm_tnd_red"] - shaft_predict(coef, df["rpm_tvd_red"].values)
            if "rpm_tvd" in df and "rpm_tnd" in df:
                df["shaft_ratio"] = df["rpm_tnd"] / df["rpm_tvd"].replace(0, np.nan)
            if "rpm_st_red" in df:
                from sklearn.linear_model import LinearRegression as _LR
                X = df.loc[tr, ["rpm_tvd_red", "rpm_tnd_red"]]
                y = df.loc[tr, "rpm_st_red"]
                ok = X.notna().all(axis=1) & y.notna()
                if ok.sum() > 50:
                    lr = _LR().fit(X[ok], y[ok])
                    Xa = df[["rpm_tvd_red", "rpm_tnd_red"]]
                    pred = pd.Series(lr.predict(Xa.fillna(Xa.median())), index=df.index)
                    df["shaft_resid_st"] = df["rpm_st_red"] - pred

    # 4.4 удельный расход топлива (прокси полезной работы — H_p)
    if "fuel_gas_flow_rate_sec" in df and "polytropic_head" in df:
        df["specific_fuel"] = df["fuel_gas_flow_rate_sec"] / df["polytropic_head"].replace(0, np.nan)

    # 4.7 температурные дельты подшипников
    if {"temp_front_bearing_pads", "temp_rear_bearing_pads"}.issubset(df.columns):
        df["dT_bearings"] = df["temp_front_bearing_pads"] - df["temp_rear_bearing_pads"]

    return df


def add_domain_features_wide(wide: pd.DataFrame, gpa_ids, running_by_gpa=None,
                             train_cutoff=None) -> pd.DataFrame:
    """Прод-обёртка: для каждого ГПА берёт `<feature>__GPAn`-колонки, считает доменные фичи
    и возвращает их с тем же суффиксом `__GPAn`, домёрженные в исходный wide."""
    out = wide.copy()
    for g in gpa_ids:
        gnum = str(g).replace("GPA", "")
        suf = f"__GPA{gnum}"
        sub_cols = [c for c in wide.columns if c.endswith(suf)]
        if not sub_cols:
            continue
        sub = wide[sub_cols].copy()
        sub.columns = [c[: -len(suf)] for c in sub_cols]  # снять суффикс → семантические имена
        rmask = None
        if running_by_gpa:
            rmask = running_by_gpa.get(g)
            if rmask is None:
                rmask = running_by_gpa.get(f"GPA{gnum}")
            if rmask is None:
                rmask = running_by_gpa.get(str(gnum))
        dom = add_domain_features_gpa(sub, running_mask=rmask, train_cutoff=train_cutoff)
        new_cols = [c for c in dom.columns if c not in sub.columns]
        for c in new_cols:
            out[c + suf] = dom[c].values
    return out


# ════════════════════════════════════════════════════════════════════════════════
#  СЕКЦИЯ 3. РЕЖИМ РАБОТЫ ГПА И ВЫБОР КАЛИБРОВОЧНОГО ОКНА
#  Фундамент: корректная метка режима держит (1) бины Mondrian, (2) выбор healthy∩steady
#  точек для conformal-калибровки, (3) OOD-флаг незнакомого режима.
# ════════════════════════════════════════════════════════════════════════════════

# Метки режима (грубые, дискретные)
STOP = "stop"
TRANSITION = "transition"
WARMUP = "warmup"
STEADY = "steady"            # ТОЛЬКО он кормит ML-коридор

MAINLINE = "mainline"
RING = "ring"

CALIB_OK = "ok"
CALIB_STARVED = "univariate_only"
CALIB_PER_UNIT = "per_unit_cutoff"


@dataclass
class RegimeConfig:
    """Параметры определения режима. Всё конфигурируемо — без хардкода в логике."""
    run_rpm_frac: float = 0.05
    warmup_steps: int = 6
    transition_pad: int = 2
    asv_ring_threshold: float = 50.0
    load_axis_priority: tuple = ("rpm_tvd", "pressure_ratio", "rpm_tvd_red",
                                 "fuel_gas_flow_rate_sec")
    min_load_cv: float = 0.05
    n_load_bins: int = 3
    n_min_calib: int = 300
    n_calib_max: int = 6000


def _running_mask(df: pd.DataFrame, cfg: RegimeConfig) -> pd.Series:
    """Bool «агрегат в работе»: дискретный флаг gtd_running, иначе fallback по оборотам ТВД."""
    idx = df.index
    gr = IDLE_FLAGS.get("gtd_running")
    if gr and gr in df.columns:
        st = df[gr].fillna(0) > 0.5
        if st.sum() > 0.15 * len(st):
            return st
    if "rpm_tvd" in df.columns:
        rpm = df["rpm_tvd"]
    else:
        rpm_cols = [c for c in df.columns if c.startswith("rpm_")]
        rpm = df[rpm_cols[0]] if rpm_cols else pd.Series(1.0, index=idx)
    rng = max(float(rpm.quantile(0.99) - rpm.quantile(0.01)), 1.0)
    return rpm > cfg.run_rpm_frac * rng


def label_regime(df_gpa: pd.DataFrame, cfg: RegimeConfig | None = None) -> pd.Series:
    """4-значная метка режима: stop / transition / warmup / steady. STEADY — канонически из
    steady_running_mask (единый источник правды), остальные — описательное разложение."""
    cfg = cfg or RegimeConfig()
    idx = df_gpa.index
    running = _running_mask(df_gpa, cfg)
    steady = steady_running_mask(df_gpa, warmup=cfg.warmup_steps).reindex(idx).fillna(False)

    sw = running.astype(int).diff().abs() > 0
    trans = sw.copy()
    for k in range(1, cfg.transition_pad + 1):
        trans = trans | sw.shift(k).fillna(False) | sw.shift(-k).fillna(False)
    restart = running.astype(int).diff() > 0
    warm = restart.rolling(cfg.warmup_steps, min_periods=1).max().fillna(0).astype(bool)

    lab = pd.Series(STOP, index=idx, dtype=object)
    lab[running.fillna(False)] = TRANSITION
    lab[warm & running.fillna(False)] = WARMUP
    lab[steady] = STEADY
    return lab


def sub_mode(df_gpa: pd.DataFrame, cfg: RegimeConfig | None = None) -> pd.Series:
    """Дискретный под-режим mainline/ring (флаги is_mode_* или положение анти-помпажного клапана)."""
    cfg = cfg or RegimeConfig()
    idx = df_gpa.index
    if "is_mode_ring" in df_gpa.columns:
        ring = df_gpa["is_mode_ring"].fillna(0) > 0.5
        return pd.Series(np.where(ring, RING, MAINLINE), index=idx, dtype=object)
    if "anti_surge_valve_pos" in df_gpa.columns:
        ring = df_gpa["anti_surge_valve_pos"] > cfg.asv_ring_threshold
        return pd.Series(np.where(ring.fillna(False), RING, MAINLINE), index=idx, dtype=object)
    return pd.Series(MAINLINE, index=idx, dtype=object)


def _load_axis(df_gpa: pd.DataFrame, cfg: RegimeConfig) -> Optional[pd.Series]:
    for name in cfg.load_axis_priority:
        if name in df_gpa.columns and df_gpa[name].notna().sum() > 10:
            return df_gpa[name].rename(name)
    return None


@dataclass
class LoadBinning:
    """Результат адаптивной нарезки по нагрузке (переиспользуется в live по edges)."""
    axis: Optional[str]
    edges: Optional[list]
    cv: float
    n_bins: int


def fit_load_bins(df_gpa: pd.DataFrame, steady_mask: pd.Series,
                  cfg: RegimeConfig | None = None) -> LoadBinning:
    """Адаптивная нарезка нагрузки на STEADY-точках. CV оси < min_load_cv → ОДИН бин."""
    cfg = cfg or RegimeConfig()
    ax = _load_axis(df_gpa, cfg)
    if ax is None:
        return LoadBinning(axis=None, edges=None, cv=0.0, n_bins=1)
    s = ax[steady_mask.reindex(ax.index).fillna(False)].dropna()
    if len(s) < cfg.n_min_calib:
        return LoadBinning(axis=ax.name, edges=None, cv=float("nan"), n_bins=1)
    cv = float(s.std() / (abs(s.mean()) + 1e-9))
    if cv < cfg.min_load_cv:
        return LoadBinning(axis=ax.name, edges=None, cv=cv, n_bins=1)
    qs = np.linspace(0, 1, cfg.n_load_bins + 1)[1:-1]
    edges = [float(s.quantile(q)) for q in qs]
    edges = sorted(set(round(e, 6) for e in edges))
    return LoadBinning(axis=ax.name, edges=edges or None, cv=cv,
                       n_bins=(len(edges) + 1) if edges else 1)


def load_bin_labels(df_gpa: pd.DataFrame, binning: LoadBinning) -> pd.Series:
    """Метка бина нагрузки по сохранённым edges (для train и live одинаково)."""
    idx = df_gpa.index
    if binning.axis is None or not binning.edges or binning.axis not in df_gpa.columns:
        return pd.Series("all", index=idx, dtype=object)
    bounds = [-np.inf, *binning.edges, np.inf]
    cats = pd.cut(df_gpa[binning.axis], bins=bounds, labels=[f"L{i}" for i in range(len(bounds) - 1)])
    return cats.astype(object).fillna("all")


def regime_key(label: pd.Series, submode: pd.Series, loadbin: pd.Series) -> pd.Series:
    """Композитный ключ режима: для не-steady точек ключ = метка; для steady — steady|sub|бин."""
    out = label.astype(object).copy()
    steady = label == STEADY
    out[steady] = (STEADY + "|" + submode[steady].astype(str) + "|" + loadbin[steady].astype(str))
    return out


def verified_healthy_mask(df_gpa: pd.DataFrame, limits: dict,
                          flagged_mask: pd.Series | None = None,
                          cfg: RegimeConfig | None = None) -> pd.Series:
    """Маска «проверенно-здоровая steady-точка» для калибровки/обучения:
       steady ∧ в физических лимитах ∧ не помечена детекторами (если переданы)."""
    cfg = cfg or RegimeConfig()
    lab = label_regime(df_gpa, cfg)
    healthy = (lab == STEADY)
    cleaned = physically_clean(df_gpa, limits)
    for c in continuous_cols(df_gpa):
        if c in cleaned.columns:
            healthy = healthy & cleaned[c].notna().reindex(df_gpa.index).fillna(True)
    if flagged_mask is not None:
        healthy = healthy & ~flagged_mask.reindex(df_gpa.index).fillna(False)
    return healthy


def resolve_unit_cutoff(df_gpa: pd.DataFrame, global_cutoff: pd.Timestamp,
                        limits: dict, cfg: RegimeConfig | None = None,
                        allow_per_unit: bool = True) -> dict:
    """Вариант B: пер-юнитный cutoff как fallback для агрегата, голодного по здоровым steady-точкам
    ПОСЛЕ глобального cutoff. allow_per_unit=False (ЕДИНЫЙ МЕТОД) → голодный юнит уходит в univariate."""
    cfg = cfg or RegimeConfig()
    healthy = verified_healthy_mask(df_gpa, limits, cfg=cfg)
    idx = df_gpa.index

    post = healthy & (idx > global_cutoff)
    if int(post.sum()) >= cfg.n_min_calib:
        return dict(unit_cutoff=global_cutoff, mode=CALIB_OK,
                    n_post=int(post.sum()), pre_fault_check_required=False)

    if not allow_per_unit:
        return dict(unit_cutoff=None, mode=CALIB_STARVED, n_post=int(post.sum()),
                    reason="unified (per-unit cutoff disabled)", pre_fault_check_required=False)

    h_idx = idx[healthy.values]
    if len(h_idx) < cfg.n_min_calib:
        return dict(unit_cutoff=None, mode=CALIB_STARVED,
                    n_post=int(post.sum()), n_healthy_total=int(len(h_idx)),
                    pre_fault_check_required=False)

    reserve = min(cfg.n_calib_max, max(cfg.n_min_calib, len(h_idx) // 2))
    last = h_idx[-reserve:]
    win_start = pd.Timestamp(last[0])
    step = _median_step(idx)
    unit_cutoff = win_start - step
    return dict(unit_cutoff=unit_cutoff, mode=CALIB_PER_UNIT,
                n_post=int(post.sum()), calib_from=str(win_start), calib_to=str(last[-1]),
                n_calib=int(len(last)), pre_fault_check_required=True)


def select_calibration(df_gpa: pd.DataFrame, unit_cutoff: pd.Timestamp, limits: dict,
                       binning: LoadBinning | None = None,
                       cfg: RegimeConfig | None = None) -> dict:
    """Событийный выбор калибровочных точек ПО РЕЖИМАМ из региона (unit_cutoff, now].
    {regime_key: {idx, n, decision}}: ≥ N_min здоровых steady → CALIB_OK; иначе CALIB_STARVED."""
    cfg = cfg or RegimeConfig()
    healthy = verified_healthy_mask(df_gpa, limits, cfg=cfg)
    post = healthy & (df_gpa.index > unit_cutoff)
    lab = label_regime(df_gpa, cfg)
    sm = sub_mode(df_gpa, cfg)
    if binning is None:
        binning = fit_load_bins(df_gpa, lab == STEADY, cfg)
    lb = load_bin_labels(df_gpa, binning)
    rk = regime_key(lab, sm, lb)

    out: dict = {}
    for key in sorted(set(rk[post].tolist())):
        pts = df_gpa.index[(rk == key).values & post.values]
        pts = pd.DatetimeIndex(pts).sort_values()
        if len(pts) >= cfg.n_min_calib:
            out[key] = dict(idx=pts[:cfg.n_calib_max], n=int(len(pts)), decision=CALIB_OK)
        else:
            out[key] = dict(idx=pts, n=int(len(pts)), decision=CALIB_STARVED)
    return out


def _median_step(idx: pd.DatetimeIndex) -> pd.Timedelta:
    if len(idx) < 2:
        return pd.Timedelta(minutes=5)
    d = pd.Series(idx).diff().dropna()
    return pd.Timedelta(d.median()) if len(d) else pd.Timedelta(minutes=5)


# ════════════════════════════════════════════════════════════════════════════════
#  СЕКЦИЯ 4. НОРМАЛИЗОВАННЫЙ block-Mondrian CONFORMAL
#  Нонконформити-скор = |факт − предикт| / σ (σ из виртуального ансамбля). block-bootstrap
#  по режимам сохраняет автокорреляцию (n_eff=n/L). Пер-режимный q̂ — безразмерный множитель;
#  коридор в live = предикт ± q̂(режим)·σ. ПЕРЕАНКЕРИВАЕМО (re-calibrate без рефита модели).
# ════════════════════════════════════════════════════════════════════════════════

DEFAULT_ALPHA = 0.02       # целевое покрытие 98% (n_eff_min≈49 при автокорреляции остатков)
DEFAULT_N_BOOT = 200       # ресэмплов moving-block bootstrap
N_EFF_MIN = 49             # ~1/α − 1 при α=0.02 — пол для конечного квантиля
# Перцентиль bootstrap-распределения квантиля, берущийся как ПОРОГ. 0.5 (медиана) = наивный
# split-квантиль → автокорреляция НЕ расширяет q̂ (баг: покрытие уходит ниже 1−α). 0.9 = верхний
# доверительный край: чем сильнее автокорреляция (больше разброс bootstrap-квантилей q_boot_std),
# тем шире q̂ → покрытие держится у номинала. Ключевой рычаг честного покрытия (см. AUDIT_2026-07-03).
DEFAULT_CONSERVATIVE_Q = 0.9


def autocorr_block_len(scores, max_lag: int = 300, thresh: float | None = None) -> int:
    """Длина блока L = первый лаг, где ACF скоров падает ниже порога (по умолч. 1/e).
    Блочный ресэмплинг сохраняет структуру зависимости ряда."""
    x = np.asarray(scores, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 20:
        return 1
    x = x - x.mean()
    var = float(np.dot(x, x) / n)
    if var <= 0:
        return 1
    thr = (1.0 / np.e) if thresh is None else float(thresh)
    L = 1
    for lag in range(1, min(max_lag, n - 1)):
        ac = float(np.dot(x[:-lag], x[lag:]) / (n * var))
        if ac < thr:
            return max(1, lag)
        L = lag
    return max(1, int(L))


def split_conformal_q(scores, alpha: float = DEFAULT_ALPHA) -> float:
    """Наивный split-conformal квантиль (база/сравнение): ⌈(n+1)(1−α)⌉-я порядковая статистика."""
    a = np.sort(np.abs(np.asarray(scores, float)))
    a = a[np.isfinite(a)]
    n = len(a)
    if n < 2:
        return float("nan")
    k = int(np.ceil((n + 1) * (1 - alpha))) - 1
    return float(a[min(max(k, 0), n - 1)])


def block_conformal_threshold(scores, alpha: float = DEFAULT_ALPHA,
                              n_boot: int = DEFAULT_N_BOOT, block_len: int | None = None,
                              conservative_q: float = DEFAULT_CONSERVATIVE_Q, rng_seed: int = 42) -> dict:
    """Порог q̂ для НОРМАЛИЗОВАННОГО скора |r|/σ, устойчивый к автокорреляции (moving-block bootstrap).
    Скор уже неотрицателен; abs() безвреден. Возвращает threshold(=q̂), block_len, n, n_eff, q_naive,
    q_boot_std (нестабильность хвоста)."""
    a = np.abs(np.asarray(scores, float))
    a = a[np.isfinite(a)]
    n = len(a)
    out = dict(threshold=float("nan"), block_len=1, n=n, n_eff=0,
               q_naive=float("nan"), q_boot_std=float("nan"))
    if n < 20:
        return out
    L = int(block_len) if block_len else autocorr_block_len(a)
    n_eff = max(1, n // L)
    q_naive = split_conformal_q(a, alpha)

    rng = np.random.default_rng(rng_seed)
    n_blocks = int(np.ceil(n / L))
    starts_max = n - L
    qs = np.empty(n_boot, float)
    for b in range(n_boot):
        if starts_max <= 0:
            sample = a
        else:
            starts = rng.integers(0, starts_max + 1, size=n_blocks)
            sample = np.concatenate([a[s:s + L] for s in starts])[:n]
        qs[b] = split_conformal_q(sample, alpha)
    out.update(threshold=float(np.nanpercentile(qs, conservative_q * 100)),
               block_len=int(L), n_eff=int(n_eff),
               q_naive=float(q_naive), q_boot_std=float(np.nanstd(qs)))
    return out


def pre_fault_sanity(scores, slope_z_thresh: float = 3.0, mag_frac_thresh: float = 0.5) -> dict:
    """Проверка окна на скрытую рампу деградации. suspect ТОЛЬКО при СОВПАДЕНИИ двух условий:
      • статзначимость тренда: z = slope/se > slope_z_thresh;
      • практическая ВЕЛИЧИНА: суммарный рост за окно (slope·n) больше mag_frac_thresh от типичного
        уровня скора (медианы) → тренд реально что-то добавляет, а не микро-дрейф.
    КРИТИЧНО: при больших окнах (n до 6000) z ∝ slope·n^1.5 → почти любой сезонный микро-тренд значим;
    без порога на величину гейт массово отключал бы режимы (AUDIT_2026-07-03, fix #1)."""
    a = np.abs(np.asarray(scores, float))
    a = a[np.isfinite(a)]
    n = len(a)
    if n < 20:
        return dict(suspect=False, z=0.0, slope=0.0, mag_frac=0.0, reason="too_few")
    t = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(t, a, 1)
    line = a - (slope * t + intercept)
    denom = np.sqrt(np.sum((t - t.mean()) ** 2)) + 1e-12
    se = np.sqrt(np.sum(line ** 2) / max(n - 2, 1)) / denom
    z = float(slope / (se + 1e-12))
    level = float(np.median(a)) + 1e-12
    mag_frac = float(abs(slope) * (n - 1) / level)          # рост за окно относительно уровня скора
    suspect = bool(z > slope_z_thresh and mag_frac > mag_frac_thresh)
    return dict(suspect=suspect, z=z, slope=float(slope), mag_frac=round(mag_frac, 3))


def enbpi_oob_residuals(X, y, fit_fn, B: int = 20, seed: int = 42):
    """OOB-остатки EnbPI (Xu&Xie 2021), data-efficient. fit_fn(Xb,yb)→модель с .predict.
    Опция (не дефолт): возврат при доказанной старвации калибровки."""
    X = np.asarray(X, float); y = np.asarray(y, float); n = len(y)
    rng = np.random.default_rng(seed)
    preds = np.full((B, n), np.nan); inbag = np.zeros((B, n), bool)
    for b in range(B):
        idx = rng.integers(0, n, size=n)
        inbag[b, idx] = True
        mdl = fit_fn(X[idx], y[idx])
        p = np.asarray(mdl.predict(X), float)
        preds[b] = (p[:, 0] if p.ndim == 2 else p).reshape(n)
    oob = np.full(n, np.nan)
    for i in range(n):
        m = ~inbag[:, i]
        if m.any():
            oob[i] = np.mean(preds[m, i])
    return np.abs(y - oob)


def enbpi_threshold(X, y, fit_fn, alpha: float = DEFAULT_ALPHA, B: int = 20,
                    block_len: int | None = None, seed: int = 42, **kw) -> dict:
    """Порог EnbPI: block-conformal квантиль OOB-остатков."""
    resid = enbpi_oob_residuals(X, y, fit_fn, B=B, seed=seed)
    resid = resid[np.isfinite(resid)]
    bc = block_conformal_threshold(resid, alpha=alpha, block_len=block_len, **kw)
    bc["n_oob"] = int(len(resid))
    return bc


@dataclass
class CalibrationArtifact:
    """Пер-режимные q̂ нормализованного коридора. Сериализуется в metadata.json. ПЕРЕАНКЕРИВАЕМЫЙ:
    recalibrate() пересчитывает один режим по свежим нормализованным скорам БЕЗ рефита модели.
    mode='normalized' — q̂ это МНОЖИТЕЛЬ на σ (коридор = предикт ± q̂·σ)."""
    alpha: float = DEFAULT_ALPHA
    n_eff_min: int = N_EFF_MIN
    conservative_q: float = DEFAULT_CONSERVATIVE_Q
    mode: str = "normalized"
    by_regime: dict = field(default_factory=dict)   # regime_key -> dict(threshold=q̂, n, n_eff, ...)

    def threshold_for(self, regime_key: str) -> Optional[float]:
        """q̂_norm (МНОЖИТЕЛЬ на σ) для режима — коридор ГИБРИД (предикт ± q̂_norm·σ).
        None → режим не откалиброван (univariate-fallback)."""
        rc = self.by_regime.get(regime_key)
        if not rc or rc.get("decision") != "ok":
            return None
        return rc.get("threshold")

    def threshold_abs_for(self, regime_key: str) -> Optional[float]:
        """q̂_abs (АБСОЛЮТНЫЙ порог на |факт−предикт|) — коридор ПЛОСКИЙ КОНФОРМНЫЙ (предикт ± q̂_abs).
        None → режим не откалиброван. Хранится рядом с q̂_norm (переключатель в проде, без рефита)."""
        rc = self.by_regime.get(regime_key)
        if not rc or rc.get("decision") != "ok":
            return None
        return rc.get("threshold_abs")

    def decision_for(self, regime_key: str) -> str:
        rc = self.by_regime.get(regime_key)
        return rc.get("decision", "univariate_only") if rc else "univariate_only"

    def recalibrate(self, regime_key: str, fresh_scores, **kw) -> dict:
        """Переанкеривание ОДНОГО режима на свежих нормализованных скорах (без модели)."""
        rc = _calibrate_one(fresh_scores, self.alpha, self.n_eff_min, self.conservative_q, **kw)
        self.by_regime[regime_key] = rc
        return rc

    def to_dict(self) -> dict:
        return dict(alpha=self.alpha, n_eff_min=self.n_eff_min,
                    conservative_q=self.conservative_q, mode=self.mode, by_regime=self.by_regime)

    @classmethod
    def from_dict(cls, d: dict) -> "CalibrationArtifact":
        return cls(alpha=d.get("alpha", DEFAULT_ALPHA),
                   n_eff_min=d.get("n_eff_min", N_EFF_MIN),
                   conservative_q=d.get("conservative_q", DEFAULT_CONSERVATIVE_Q),
                   mode=d.get("mode", "normalized"),
                   by_regime=d.get("by_regime", {}))


def _calibrate_one(scores, alpha: float, n_eff_min: int, conservative_q: float, **kw) -> dict:
    bc = block_conformal_threshold(scores, alpha=alpha, conservative_q=conservative_q, **kw)
    pf = pre_fault_sanity(scores)
    thr = bc["threshold"]
    _force = os.environ.get("CS_FORCE_CORRIDOR") == "1"
    ok = np.isfinite(thr) and bc["n_eff"] >= n_eff_min
    forced = False
    # pre_fault ГЕЙТ: значимый растущий тренд скора в «здоровом» окне = вероятная рампа деградации
    # до планового останова. Калибровать коридор на ней НЕЛЬЗЯ — порог раздуется и замаскирует
    # зарождающийся дефект. Режим уходит в univariate_only (self-band/донор), помечается на проверку.
    # FORCE может переопределить (осознанный ручной режим), но по умолчанию ретрейн идёт БЕЗ FORCE.
    if pf.get("suspect") and not _force:
        ok = False
    # CS_FORCE_CORRIDOR=1: строим полосу даже при n_eff<min. Малая выборка → квантиль ненадёжен →
    # КОНСЕРВАТИВНО расширяем ×1.5. Базируемся на НАИВНОМ квантиле q_naive, а НЕ на уже-консервативном
    # p90-пороге (иначе двойная консервативность conservative_q×1.5 — fix #4, AUDIT_2026-07-03).
    if (not ok) and np.isfinite(thr) and _force:
        _qn = bc.get("q_naive")
        thr = float(_qn) * 1.5 if (_qn is not None and np.isfinite(_qn)) else float(thr) * 1.5
        ok, forced = True, True
    return dict(threshold=thr, n=bc["n"], n_eff=bc["n_eff"],
                block_len=bc["block_len"], q_naive=bc["q_naive"],
                q_boot_std=bc["q_boot_std"],
                decision=("ok" if ok else "univariate_only"),
                forced=forced, pre_fault=pf,
                pre_fault_gated=bool(pf.get("suspect") and not _force))


def mondrian_calibrate(scores_by_regime: dict, alpha: float = DEFAULT_ALPHA,
                       n_eff_min: int = N_EFF_MIN, conservative_q: float = DEFAULT_CONSERVATIVE_Q,
                       **kw) -> CalibrationArtifact:
    """Калибровка ПО РЕЖИМАМ на НОРМАЛИЗОВАННЫХ скорах |r|/σ.
    scores_by_regime: {regime_key: array нормализованных скоров (healthy steady)}.
    Режим с n_eff < n_eff_min → decision='univariate_only' (единый fallback)."""
    art = CalibrationArtifact(alpha=alpha, n_eff_min=n_eff_min, conservative_q=conservative_q)
    for key, scores in scores_by_regime.items():
        art.by_regime[key] = _calibrate_one(scores, alpha, n_eff_min, conservative_q, **kw)
    return art


def build_self_candidate(df_gpa, sub, target, unit_cutoff, limits, binning, regime_cfg, calib_cfg,
                         force_build: bool = False):
    """SELF-CONFORMAL кандидат: центр = healthy-медиана режима, скор |значение−центр| через ТОТ ЖЕ
    block-Mondrian калибратор. Строится для ЛЮБОГО датчика как альтернативный предиктор (роутинг по
    MAE / live corridor_mode='self' выбирает его там, где кросс-сенсорный коридор слаб — R²<0 / вибро).
    force_build=True → строить единообразно всем (снят прежний гейт CS_SELF_BAND).
    Возврат (centers, art_self_dict) или (None, None)."""
    if not force_build and os.environ.get("CS_SELF_BAND") != "1":
        return None, None
    try:
        sel_s = select_calibration(df_gpa, unit_cutoff, limits, binning, regime_cfg)
        centers, scores_self = {}, {}
        for rk, info in sel_s.items():
            if info["decision"] != CALIB_OK:
                continue
            idx = pd.DatetimeIndex(info["idx"]).intersection(sub.index)
            if len(idx) < 50:
                continue
            yc = sub.loc[idx, target].values.astype(float)
            c = float(np.median(yc))
            centers[rk] = round(c, 6)
            scores_self[rk] = np.abs(yc - c)   # |значение − центр| → conformal-порог self-полосы
        if scores_self:
            art_self = mondrian_calibrate(scores_self, **calib_cfg)
            if any(rc.get("decision") == "ok" for rc in art_self.by_regime.values()):
                art_self.mode = "self"
                return centers, art_self.to_dict()
    except Exception as e:
        logger.debug("self-band %s: %s", target, e)
    return None, None


def _self_band_mae(df_gpa, target, eval_idx, binning, regime_cfg, unit_cutoff, limits) -> float:
    """ЧЕСТНЫЙ MAE-baseline для роутинга genuine-vs-self (fix mae_self-leakage, AUDIT_2026-07-03).
    Центр self-band для СРАВНЕНИЯ считается СТРОГО на train (≤ cutoff) — как обучается модель — и
    оценивается на ТЕХ ЖЕ eval-точках (полный eval_idx с глобальным train-fallback), что и mae_val.
    Раньше центр брался из пост-cutoff калибровки, пересекающейся с eval (in-sample) → mae_self занижен
    → genuine-модели ложно демотировались. NaN если train/eval < 30 точек."""
    try:
        lab = label_regime(df_gpa, regime_cfg)
        sm = sub_mode(df_gpa, regime_cfg)
        lb = load_bin_labels(df_gpa, binning)
        rk = regime_key(lab, sm, lb)
        y = df_gpa[target].astype(float)
        healthy = verified_healthy_mask(df_gpa, limits, cfg=regime_cfg).reindex(df_gpa.index).fillna(False)
        train_m = healthy & (lab == STEADY) & (df_gpa.index <= unit_cutoff) & y.notna()
        if int(train_m.sum()) < 30:
            return float("nan")
        gmed = float(np.median(y[train_m]))                       # глобальный train-медиан (fallback)
        cen_by_rk = {}                                            # per-режимный train-медиан (≥30 точек)
        for k in pd.unique(rk[train_m]):
            v = y[train_m & (rk == k)]
            if len(v) >= 30:
                cen_by_rk[k] = float(np.median(v))
        idx = pd.DatetimeIndex(eval_idx).intersection(df_gpa.index)
        rk_e = rk.reindex(idx)
        cen = rk_e.map(lambda k: cen_by_rk.get(k, gmed)).astype(float)   # support = ВЕСЬ eval (как mae_val)
        resid = (y.reindex(idx) - cen).abs()
        resid = resid[np.isfinite(resid)]
        return float(resid.mean()) if len(resid) >= 30 else float("nan")
    except Exception as e:
        logger.debug("self-band MAE %s: %s", target, e)
        return float("nan")


# ════════════════════════════════════════════════════════════════════════════════
#  СЕКЦИЯ 5. ОБУЧЕНИЕ МОДЕЛИ ОДНОГО ДАТЧИКА
# ════════════════════════════════════════════════════════════════════════════════

# Непересекающиеся окна горизонта (дни от cutoff). Граница (lo, hi] в днях; None=∞.
HORIZON_WINDOWS = [("0-3д", 0, 3), ("3-7д", 3, 7), ("7-15д", 7, 15),
                   ("15-30д", 15, 30), ("30д+", 30, None)]

_MAPE_OK_KEYS = ("temp", "oil_temp", "pressure", "rpm", "gas_temp", "polytropic_head")
_MAPE_BAD_KEYS = ("shift", "shaft", "resid", "vibro")

# Число виртуальных моделей в ансамбле (K). 10–20 стабильно; больше — дороже инференс.
# Нужно достаточно деревьев: K фактически ограничивается tree_count//2 (CatBoost берёт хвост
# траектории после прогрева). Тот же K используется в live (детерминирован по tree_count).
VE_COUNT = 10


def _safe_ve_count(n_trees: int, requested: int = VE_COUNT) -> int:
    """K виртуальных ансамблей, безопасный для данного числа деревьев (после прогрева делить
    почти нечего, если деревьев мало). tree_count фиксирован → live получит тот же K."""
    return int(max(1, min(requested, n_trees // 2)))


def ensemble_sigma_uepi(mdl, X, n_trees: int, ve_count: int = VE_COUNT):
    """σ=sqrt(u_epi+u_ale) и u_epi (эпистемика) из виртуального ансамбля CatBoost.
    Надёжно: u_ale всегда доступна из predict()[:,1] (RMSEWithUncertainty); u_epi — из
    virtual_ensembles_predict(TotalUncertainty) когда деревьев хватает, иначе 0.
    TotalUncertainty: [mean, u_epi(knowledge), u_ale(data)] (проверено CatBoost 1.2.x).
    ИСПОЛЬЗУЕТСЯ И В ОБУЧЕНИИ, И В live_predict — единый источник σ (иначе покрытие невалидно)."""
    p = np.asarray(mdl.predict(X), float)
    u_ale = np.clip(p[:, 1], 0.0, None) if (p.ndim == 2 and p.shape[1] >= 2) else np.zeros(len(p))
    u_epi = np.zeros(len(u_ale))
    K = _safe_ve_count(n_trees, ve_count)
    if n_trees >= 4 and K >= 2:
        try:
            ve = np.asarray(mdl.virtual_ensembles_predict(
                X, prediction_type="TotalUncertainty", virtual_ensembles_count=K), float)
            if ve.ndim == 2 and ve.shape[1] >= 2:
                u_epi = np.clip(ve[:, 1], 0.0, None)
                if ve.shape[1] >= 3:
                    u_ale = np.clip(ve[:, 2], 0.0, None)   # u_ale ансамбля согласован с u_epi
        except Exception as e:
            logger.debug("virtual ensembles K=%d failed: %s", K, e)
    sigma = np.sqrt(np.clip(u_epi + u_ale, 1e-12, None))
    return sigma, u_epi


@dataclass
class SensorModel:
    target: str
    gpa_id: str
    feat_cols: list
    unit_cutoff: Optional[str]
    cutoff_mode: str                 # ok | per_unit_cutoff | univariate_only
    detector_mode: str               # ml_corridor | univariate_band | univariate_only
    n_trees: int = 0
    self_centers: dict = field(default_factory=dict)   # {regime_key: healthy-медиана} — центр self-conformal полосы (univariate_band ИЛИ self-кандидат)
    self_calibration: dict = field(default_factory=dict)  # CalibrationArtifact self-полосы как КАНДИДАТ (corridor_mode='self' в live выбирает его)
    r2_eval: float = float("nan")
    r2_baseline: float = float("nan")
    sensor_range: float = float("nan")
    mae_val: float = float("nan")
    mae_self: float = float("nan")   # MAE self-band медианы на held-out (MAE-baseline роутинга)
    rmse_val: float = float("nan")
    nmae_val: float = float("nan")
    residual_std_val: float = float("nan")
    residual_mean_val: float = float("nan")
    corridor_quality: str = "n/a"    # genuine (модель бьёт self по MAE) | steady_band | self_band | n/a
    feat_ranges: dict = field(default_factory=dict)
    metrics_windows: dict = field(default_factory=dict)
    load_binning: dict = field(default_factory=dict)
    calibration: dict = field(default_factory=dict)      # CalibrationArtifact.to_dict() (normalized)
    epistemic_ref: dict = field(default_factory=dict)     # κ=know_p95 + know_med (детектор 2)
    feature_importance: list = field(default_factory=list)
    pre_fault: dict = field(default_factory=dict)
    note: str = ""
    pooled: bool = False                                   # pooled cross-GPA модель (термо/мех)
    norm: dict = field(default_factory=dict)               # z-norm параметры ЭТОГО ГПА (для pooled)
    model_file_override: str = ""                          # общий файл pooled-модели (один на имя датчика)


def _metrics(y, p):
    y, p = np.asarray(y, float), np.asarray(p, float)
    m = np.isfinite(y) & np.isfinite(p)
    y, p = y[m], p[m]
    if len(y) < 2:
        return dict(n=int(len(y)), mae=None, rmse=None, r2=None)
    mae = float(np.mean(np.abs(y - p)))
    rmse = float(np.sqrt(np.mean((y - p) ** 2)))
    ss = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1 - np.sum((y - p) ** 2) / ss) if ss > 1e-12 else float("nan")
    return dict(n=int(len(y)), mae=round(mae, 6), rmse=round(rmse, 6),
                r2=round(r2, 4) if np.isfinite(r2) else None)


def _mape(y, p, target: str, eps_frac: float = 0.02, sensor_range: float = 1.0):
    """MAPE С ЗАЩИТОЙ: только для строго-положительных таргетов; маска |y|>ε·range. Иначе None."""
    t = target.lower()
    if any(k in t for k in _MAPE_BAD_KEYS) or not any(k in t for k in _MAPE_OK_KEYS):
        return None
    y, p = np.asarray(y, float), np.asarray(p, float)
    eps = eps_frac * max(sensor_range, 1e-9)
    m = np.isfinite(y) & np.isfinite(p) & (np.abs(y) > eps)
    if m.sum() < 10:
        return None
    return round(float(np.mean(np.abs((y[m] - p[m]) / y[m])) * 100), 3)


def train_sensor(df_gpa: pd.DataFrame, target: str, feat_cols: list,
                 global_cutoff: pd.Timestamp, limits: dict, gpa_id: str,
                 regime_cfg: RegimeConfig | None = None,
                 calib_cfg: dict | None = None,
                 es_val_frac: float = 0.15, es_rounds: int = 50,
                 max_iters: int = 1500, per_unit: bool = False,
                 ve_count: int = 10) -> SensorModel:
    """Обучает модель одного датчика (виртуально-ансамблевый CatBoost) и калибрует
    НОРМАЛИЗОВАННЫЙ block-Mondrian conformal-коридор. Возвращает SensorModel (+ ._model)."""
    from catboost import CatBoostRegressor
    regime_cfg = regime_cfg or RegimeConfig()
    calib_cfg = calib_cfg or dict(alpha=DEFAULT_ALPHA, n_eff_min=N_EFF_MIN, n_boot=DEFAULT_N_BOOT)

    feats = [f for f in feat_cols if f in df_gpa.columns and f != target]
    base = SensorModel(target=target, gpa_id=gpa_id, feat_cols=feats,
                       unit_cutoff=None, cutoff_mode="", detector_mode="univariate_only")
    if target not in df_gpa.columns:
        base.note = "нет таргета"
        return base
    if len(feats) < 2:
        # НЕТ кросс-фич → self-band-only (центр = healthy-медиана режима, без CatBoost). fix #3a
        # (AUDIT_2026-07-03): раньше внешний цикл делал continue → датчик МОЛЧА выпадал из мониторинга.
        base.note = "нет кросс-фич — self-band-only"
        try:
            _res = resolve_unit_cutoff(df_gpa, global_cutoff, limits, regime_cfg, allow_per_unit=per_unit)
            _uc = _res.get("unit_cutoff") or global_cutoff
            base.unit_cutoff = str(_uc); base.cutoff_mode = _res.get("mode", "")
            _bin = fit_load_bins(df_gpa, label_regime(df_gpa, regime_cfg) == STEADY, regime_cfg)
            base.load_binning = dict(axis=_bin.axis, edges=_bin.edges, cv=_bin.cv, n_bins=_bin.n_bins)
            _hm = verified_healthy_mask(df_gpa, limits, cfg=regime_cfg).reindex(df_gpa.index).fillna(False)
            _sub = df_gpa.loc[_hm, [target]].dropna(subset=[target])
            _c, _art = build_self_candidate(df_gpa, _sub, target, _uc, limits, _bin, regime_cfg,
                                            calib_cfg, force_build=True)
            if _art:
                base.self_centers = _c; base.self_calibration = _art; base.calibration = _art
                base.detector_mode = "univariate_band"; base.corridor_quality = "self_band"
                base.sensor_range = float(np.nanmax(df_gpa[target]) - np.nanmin(df_gpa[target])) or 1.0
        except Exception as e:
            logger.debug("self-band-only %s: %s", target, e)
        return base

    # ── 1. cutoff (единый глобальный; пер-юнитный B — опц.) ──
    res = resolve_unit_cutoff(df_gpa, global_cutoff, limits, regime_cfg, allow_per_unit=per_unit)
    base.cutoff_mode = res["mode"]
    base.pre_fault = {"check_required": res.get("pre_fault_check_required", False)}
    if res["unit_cutoff"] is None:
        base.note = f"нет здоровых steady-точек ({res['mode']}) → univariate_only"
        return base
    unit_cutoff = pd.Timestamp(res["unit_cutoff"])
    base.unit_cutoff = str(unit_cutoff)

    # Обучаем на STEADY-RUNNING healthy точках (как eval, как live с running-гейтом).
    healthy = verified_healthy_mask(df_gpa, limits, cfg=regime_cfg)
    hmask = healthy.reindex(df_gpa.index).fillna(False)
    sub = df_gpa.loc[hmask, [target] + feats].dropna(subset=[target])
    if len(sub) < 250:
        base.note = f"мало healthy steady-точек ({len(sub)}) → univariate_only"
        return base
    sr = float(np.nanpercentile(sub[target], 99) - np.nanpercentile(sub[target], 1))
    base.sensor_range = round(sr, 6)

    # ── 2. хронологический сплит (без утечки) ──
    tr = sub[sub.index <= unit_cutoff]
    ho = sub[sub.index > unit_cutoff]
    base.feat_ranges = {f: (round(float(tr[f].quantile(0.01)), 6), round(float(tr[f].quantile(0.99)), 6))
                        for f in feats if f in tr.columns and tr[f].notna().any()}
    if len(tr) < 200 or len(ho) < 50:
        base.note = f"мало данных (train={len(tr)}, holdout={len(ho)}) → univariate_only"
        return base
    n_inner = int(len(tr) * (1 - es_val_frac))
    tr_inner, es_val = tr.iloc[:n_inner], tr.iloc[n_inner:]
    if len(es_val) < 30:
        k = max(30, len(tr) // 10)
        tr_inner, es_val = tr.iloc[:-k], tr.iloc[-k:]

    Xi, yi = tr_inner[feats], tr_inner[target]
    Xv, yv = es_val[feats], es_val[target]

    # ── 3. ВИРТУАЛЬНО-АНСАМБЛЕВЫЙ CatBoost: posterior_sampling (u_epi) + RMSEWithUncertainty (u_ale) ──
    mdl = CatBoostRegressor(iterations=max_iters, depth=6, learning_rate=0.05,
                            l2_leaf_reg=20, loss_function="RMSEWithUncertainty",
                            posterior_sampling=True, bootstrap_type="Bernoulli",
                            subsample=0.8, random_seed=42, logging_level="Silent",
                            thread_count=2)
    try:
        mdl.fit(Xi, yi, eval_set=(Xv, yv), early_stopping_rounds=es_rounds, use_best_model=True)
    except Exception as e:
        base.note = f"catboost fit error: {e}"
        return base
    base.n_trees = int(mdl.tree_count_)
    base._model = mdl  # type: ignore[attr-defined]

    def _pred_mean(X):
        p = np.asarray(mdl.predict(X), float)
        return p[:, 0] if p.ndim == 2 else p

    def _sigma_uepi(X):
        return ensemble_sigma_uepi(mdl, X, base.n_trees, ve_count)

    # ── 4. detector gate: R²_eval (диагностика) + бьёт ли модель режимный baseline ──
    _force = os.environ.get("CS_FORCE_CORRIDOR") == "1"   # одна методичка: кросс-сенсорный коридор ВСЕМ, не смотря на метрики
    eval_idx = ho.index
    beats = False
    mae_median_base = float("nan")   # MAE healthy-МЕДИАНЫ на held-out (fallback-baseline роутинга)
    if len(eval_idx) >= 30:
        Xe, ye = sub.loc[eval_idx, feats], sub.loc[eval_idx, target]
        pe = _pred_mean(Xe)
        me = _metrics(ye, pe)
        base.r2_eval = me["r2"] if me["r2"] is not None else float("nan")   # только диагностика
        _r = ye.values - pe
        base.mae_val = round(float(np.mean(np.abs(_r))), 6)
        base.rmse_val = round(float(np.sqrt(np.mean(_r ** 2))), 6)
        base.nmae_val = round(base.mae_val / sr, 5) if sr > 1e-9 else None
        base.residual_std_val = round(float(np.std(_r)), 6)
        base.residual_mean_val = round(float(np.mean(np.abs(_r))), 6)   # =MAE (масштаб для live drift-детектора; НЕ менять смысл)
        # baseline = healthy-МЕДИАНА (= центр self-band), НЕ train-среднее. Сравнение на ТОМ ЖЕ eval,
        # что mae_val → нет рассинхрона «train-среднее vs holdout-среднее», из-за которого R²<0
        # модели ложно «били baseline». Решение по MAE, R² не участвует.
        base_pred = np.full(len(ye), float(np.median(yi)))
        mb = _metrics(ye, base_pred)
        base.r2_baseline = mb["r2"] if mb["r2"] is not None else float("nan")
        mae_median_base = float(mb["mae"]) if mb["mae"] is not None else float("nan")
        beats = (me["mae"] is not None and np.isfinite(mae_median_base) and me["mae"] < mae_median_base)
        # провизорно ml_corridor если бьёт медиану; ФИНАЛЬНОЕ решение — сравнение с self-band ниже.
        # CS_FORCE_CORRIDOR=1 строит ml-калибровку всем как КАНДИДАТ, но НЕ навязывает её центр:
        # если self-band не хуже по MAE, служится self (см. роутинг в §6b).
        base.detector_mode = "ml_corridor" if (beats or _force) else "univariate_only"
    else:
        base.note = f"мало healthy eval-точек ({len(eval_idx)})"
        base.detector_mode = "ml_corridor" if _force else "univariate_only"

    # ── 5. эпистемический эталон (детектор 2): κ = p95(u_epi) на healthy eval ──
    try:
        if len(eval_idx) >= 30:
            _sig_e, know = _sigma_uepi(sub.loc[eval_idx, feats])
            base.epistemic_ref = dict(know_med=round(float(np.median(know)), 8),
                                      know_p95=round(float(np.percentile(know, 95)), 8))
    except Exception as e:
        logger.debug("epistemic ref %s: %s", target, e)

    # нарезка нагрузки — считаем всегда и сохраняем (инференс берёт те же границы)
    binning = fit_load_bins(df_gpa, label_regime(df_gpa, regime_cfg) == STEADY, regime_cfg)
    base.load_binning = dict(axis=binning.axis, edges=binning.edges,
                             cv=binning.cv, n_bins=binning.n_bins)

    # ── 6. block-Mondrian conformal по режимам — ОБА коридора (переключаемы в проде):
    #    ГИБРИД  q̂_norm на скоре |факт−предикт|/σ  → коридор предикт ± q̂_norm·σ
    #    ПЛОСКИЙ q̂_abs  на скоре |факт−предикт|     → коридор предикт ± q̂_abs
    #    Оба калибруются по ОДНИМ healthy-точкам; хранятся рядом → переключение без рефита. ──
    if base.detector_mode == "ml_corridor":
        sel = select_calibration(df_gpa, unit_cutoff, limits, binning, regime_cfg)
        scores_norm, scores_abs, pf_any = {}, {}, False
        for rk, info in sel.items():
            if not _force and info["decision"] != CALIB_OK:
                continue                              # FORCE: используем и «голодные» режимы
            idx = pd.DatetimeIndex(info["idx"]).intersection(sub.index)
            if len(idx) < 50:
                continue
            Xc = sub.loc[idx, feats]
            yc = sub.loc[idx, target].values
            sig, _ = _sigma_uepi(Xc)
            resid = np.abs(yc - _pred_mean(Xc))
            scores_norm[rk] = resid / np.maximum(sig, 1e-12)   # |факт − предикт| / σ → q̂_norm (гибрид)
            scores_abs[rk] = resid                              # |факт − предикт|     → q̂_abs (плоский)
        if scores_norm:
            art = mondrian_calibrate(scores_norm, **calib_cfg)       # primary: нормализованный
            art_abs = mondrian_calibrate(scores_abs, **calib_cfg)    # плоский conformal на |r|
            for rk, rc in art.by_regime.items():
                _ra = art_abs.by_regime.get(rk) or {}
                rc["threshold_abs"] = _ra.get("threshold")
                rc["n_eff_abs"] = _ra.get("n_eff")
                rc["block_len_abs"] = _ra.get("block_len")
            art.mode = "dual"   # хранит оба порога: threshold(=q̂_norm) + threshold_abs(=q̂_abs)
            base.calibration = art.to_dict()
            pf_any = any(v.get("pre_fault", {}).get("suspect") for v in art.by_regime.values())
            base.pre_fault["suspect_any"] = pf_any
            if not any(rc.get("decision") == "ok" for rc in art.by_regime.values()):
                base.detector_mode = "univariate_only"
                base.note = (base.note + "; полоса не построена (n_eff<min во всех режимах)").strip("; ")
        else:
            base.detector_mode = "univariate_only"
            base.note = (base.note + "; нет режимов для калибровки").strip("; ")

    # ── 6b. SELF-CONFORMAL — строим ВСЕГДА (единообразно, гейт CS_SELF_BAND снят) как self_centers/
    #    self_calibration. Затем РОУТИНГ ПО MAE: модель служится основной, только если её MAE на
    #    held-out МЕНЬШЕ, чем у простой healthy-медианы режима (self-band). Иначе (в т.ч. R²<0 вибро
    #    под FORCE — смещённый центр) ОСНОВНОЙ становится self-band. R² в решении не участвует. ──
    _centers, _art_self = build_self_candidate(df_gpa, sub, target, unit_cutoff, limits, binning, regime_cfg,
                                               calib_cfg, force_build=True)
    if _art_self:
        base.self_calibration = _art_self
        base.self_centers = _centers
    mae_self = _self_band_mae(df_gpa, target, eval_idx, binning, regime_cfg, unit_cutoff, limits)
    base.mae_self = round(mae_self, 6) if np.isfinite(mae_self) else float("nan")
    # baseline сравнения: per-режимная self-band медиана (или глобальная медиана, если по-режимной нет)
    _cmp = mae_self if np.isfinite(mae_self) else mae_median_base
    _model_beats_self = (base.detector_mode == "ml_corridor" and np.isfinite(base.mae_val)
                         and np.isfinite(_cmp) and base.mae_val < _cmp)
    if base.detector_mode == "ml_corridor" and _model_beats_self:
        base.corridor_quality = "genuine"          # модель предсказательнее self-band по MAE
    elif _art_self:
        base.calibration = _art_self               # self-band ОСНОВНОЙ (даже если ml-коридор построен)
        base.detector_mode = "univariate_band"
        base.corridor_quality = "self_band"
        base.note = (base.note + "; self-band основной (модель не бьёт медиану по MAE)").strip("; ")
    elif base.detector_mode == "ml_corridor":
        base.corridor_quality = "steady_band"      # ml есть, self не построился — честная метка

    # ── 7. метрики на НЕПЕРЕСЕКАЮЩИХСЯ окнах горизонта ──
    pe_all = _pred_mean(ho[feats])
    yo = ho[target].values
    for label, lo, hi in HORIZON_WINDOWS:
        left = unit_cutoff + pd.Timedelta(days=lo)
        right = ho.index.max() + pd.Timedelta(seconds=1) if hi is None else unit_cutoff + pd.Timedelta(days=hi)
        m = np.asarray((ho.index > left) & (ho.index <= right))
        if m.sum() == 0:
            base.metrics_windows[label] = dict(n=0)
            continue
        mm = _metrics(yo[m], pe_all[m])
        mm["nmae"] = round(mm["mae"] / sr, 5) if (mm["mae"] is not None and sr > 1e-9) else None
        mm["mape"] = _mape(yo[m], pe_all[m], target, sensor_range=sr)
        base.metrics_windows[label] = mm

    # ── 8. важность фич (драйверы уровня) ──
    try:
        imp = mdl.get_feature_importance()
        order = np.argsort(imp)[::-1][:8]
        base.feature_importance = [{"name": feats[i], "importance": round(float(imp[i]), 3)}
                                   for i in order if imp[i] > 0]
    except Exception:
        pass
    return base


# ════════════════════════════════════════════════════════════════════════════════
#  POOLED cross-GPA модель (термо/механика): z-norm по ГПА + одна модель на пуле.
#  Вибрация остаётся per-unit (train_sensor) — механ.подпись агрегата уникальна.
# ════════════════════════════════════════════════════════════════════════════════
def is_pooled_target(name: str, meth: dict | None = None) -> bool:
    """МАСКА маршрутизации датчика: pooled (кросс-ГПА) vs per-unit. Конфигурируется через
    config.methodology.pooling (подстроки имён), дефолт — вибрация per-unit, термо/механика pooled
    (доказано экспериментом: pooled выигрывает на термо/мех по всем 3 ГПА, проигрывает на вибро).

    pooling:
      enabled: true            # false → ВСЁ per-unit (отключить pooled-путь целиком)
      per_unit: ["vibro"]      # подстроки → принудительно per-unit
      pooled:   []             # подстроки → принудительно pooled (приоритет над per_unit)
    Иначе (нет совпадений) → pooled.
    """
    p = ((meth or {}).get("pooling") or {})
    if not p.get("enabled", True):
        return False
    nl = str(name).lower()
    if any(str(s).lower() in nl for s in (p.get("pooled") or [])):
        return True
    if any(str(s).lower() in nl for s in (p.get("per_unit") or ["vibro"])):
        return False
    return True


class _PooledAdapter:
    """Обёртка общей pooled-модели (обучена на z-нормализованных данных) под интерфейс обычной
    RMSEWithUncertainty-модели в СЫРЫХ единицах КОНКРЕТНОГО ГПА: нормализует вход параметрами ГПА,
    де-нормализует выход (mean*sd+mu; дисперсии *sd²). Тогда калибровка/инференс работают как есть."""
    def __init__(self, model, feats, fmu, fsd, tmu, tsd):
        self.model = model
        self.feats = list(feats)
        self.fmu, self.fsd = fmu, fsd            # pandas Series по feats
        self.tmu, self.tsd = float(tmu), float(tsd)
        self.tree_count_ = int(getattr(model, "tree_count_", 0) or 0)

    def _norm(self, X):
        Xx = X[self.feats] if hasattr(X, "columns") else pd.DataFrame(np.asarray(X), columns=self.feats)
        return (Xx - self.fmu) / self.fsd

    def predict(self, X):
        p = np.asarray(self.model.predict(self._norm(X)), float)
        if p.ndim == 2 and p.shape[1] >= 2:
            return np.column_stack([p[:, 0] * self.tsd + self.tmu, p[:, 1] * self.tsd ** 2])
        return p * self.tsd + self.tmu

    def virtual_ensembles_predict(self, X, prediction_type="TotalUncertainty", virtual_ensembles_count=10):
        ve = np.asarray(self.model.virtual_ensembles_predict(
            self._norm(X), prediction_type=prediction_type,
            virtual_ensembles_count=virtual_ensembles_count), float)
        out = ve.astype(float).copy()
        if out.ndim == 2 and out.shape[1] >= 1:
            out[:, 0] = ve[:, 0] * self.tsd + self.tmu
            if out.shape[1] >= 2:
                out[:, 1] = ve[:, 1] * self.tsd ** 2     # u_epi — дисперсия → *sd²
            if out.shape[1] >= 3:
                out[:, 2] = ve[:, 2] * self.tsd ** 2     # u_ale — дисперсия → *sd²
        return out

    def get_feature_importance(self):
        return self.model.get_feature_importance()


def _finalize_calibration(base: SensorModel, mdl, df_gpa, sub, tr, ho, target, feats,
                          unit_cutoff, sr, regime_cfg, calib_cfg, limits, ve_count):
    """Шаги 4–8 (gate / эпистемика / бины / block-Mondrian dual-коридор / метрики / важность)
    для УЖЕ обученной модели mdl (реальной или _PooledAdapter). Идентично хвосту train_sensor."""
    def _pred_mean(X):
        p = np.asarray(mdl.predict(X), float)
        return p[:, 0] if p.ndim == 2 else p

    def _sigma_uepi(X):
        return ensemble_sigma_uepi(mdl, X, base.n_trees, ve_count)

    _force = os.environ.get("CS_FORCE_CORRIDOR") == "1"   # одна методичка: коридор ВСЕМ (pooled тоже)
    eval_idx = ho.index
    beats = False
    mae_median_base = float("nan")   # MAE healthy-МЕДИАНЫ на held-out (baseline роутинга)
    if len(eval_idx) >= 30:
        Xe, ye = sub.loc[eval_idx, feats], sub.loc[eval_idx, target]
        pe = _pred_mean(Xe)
        me = _metrics(ye, pe)
        base.r2_eval = me["r2"] if me["r2"] is not None else float("nan")   # только диагностика
        _r = ye.values - pe
        base.mae_val = round(float(np.mean(np.abs(_r))), 6)
        base.rmse_val = round(float(np.sqrt(np.mean(_r ** 2))), 6)
        base.nmae_val = round(base.mae_val / sr, 5) if sr > 1e-9 else None
        base.residual_std_val = round(float(np.std(_r)), 6)
        base.residual_mean_val = round(float(np.mean(np.abs(_r))), 6)   # =MAE (масштаб для live)
        base_pred = np.full(len(ye), float(np.median(tr[target])))   # baseline = healthy-МЕДИАНА (не среднее)
        mb = _metrics(ye, base_pred)
        base.r2_baseline = mb["r2"] if mb["r2"] is not None else float("nan")
        mae_median_base = float(mb["mae"]) if mb["mae"] is not None else float("nan")
        beats = (me["mae"] is not None and np.isfinite(mae_median_base) and me["mae"] < mae_median_base)
        base.detector_mode = "ml_corridor" if (beats or _force) else "univariate_only"
    else:
        base.note = (base.note + f"; мало healthy eval ({len(eval_idx)})").strip("; ")
        base.detector_mode = "ml_corridor" if _force else "univariate_only"

    try:
        if len(eval_idx) >= 30:
            _sig_e, know = _sigma_uepi(sub.loc[eval_idx, feats])
            base.epistemic_ref = dict(know_med=round(float(np.median(know)), 8),
                                      know_p95=round(float(np.percentile(know, 95)), 8))
    except Exception as e:
        logger.debug("epistemic ref %s: %s", target, e)

    binning = fit_load_bins(df_gpa, label_regime(df_gpa, regime_cfg) == STEADY, regime_cfg)
    base.load_binning = dict(axis=binning.axis, edges=binning.edges, cv=binning.cv, n_bins=binning.n_bins)

    if base.detector_mode == "ml_corridor":
        sel = select_calibration(df_gpa, unit_cutoff, limits, binning, regime_cfg)
        scores_norm, scores_abs = {}, {}
        for rk, info in sel.items():
            if not _force and info["decision"] != CALIB_OK:
                continue                              # FORCE: используем и «голодные» режимы (pooled)
            idx = pd.DatetimeIndex(info["idx"]).intersection(sub.index)
            if len(idx) < 50:
                continue
            Xc = sub.loc[idx, feats]
            yc = sub.loc[idx, target].values
            sig, _ = _sigma_uepi(Xc)
            resid = np.abs(yc - _pred_mean(Xc))
            scores_norm[rk] = resid / np.maximum(sig, 1e-12)
            scores_abs[rk] = resid
        if scores_norm:
            art = mondrian_calibrate(scores_norm, **calib_cfg)
            art_abs = mondrian_calibrate(scores_abs, **calib_cfg)
            for rk, rc in art.by_regime.items():
                _ra = art_abs.by_regime.get(rk) or {}
                rc["threshold_abs"] = _ra.get("threshold")
                rc["n_eff_abs"] = _ra.get("n_eff")
                rc["block_len_abs"] = _ra.get("block_len")
            art.mode = "dual"
            base.calibration = art.to_dict()
            base.pre_fault["suspect_any"] = any(v.get("pre_fault", {}).get("suspect")
                                                for v in art.by_regime.values())
            if not any(rc.get("decision") == "ok" for rc in art.by_regime.values()):
                base.detector_mode = "univariate_only"
                base.note = (base.note + "; полоса не построена").strip("; ")
        else:
            base.detector_mode = "univariate_only"
            base.note = (base.note + "; нет режимов для калибровки").strip("; ")

    # self-conformal кандидат — строим ВСЕГДА (pooled тоже: масло/подшипники — частые self-победители).
    # Роутинг по MAE: модель служится, только если бьёт self-band медиану на held-out; иначе self основной.
    _centers, _art_self = build_self_candidate(df_gpa, sub, target, unit_cutoff, limits, binning, regime_cfg,
                                               calib_cfg, force_build=True)
    if _art_self:
        base.self_calibration = _art_self
        base.self_centers = _centers
    mae_self = _self_band_mae(df_gpa, target, eval_idx, binning, regime_cfg, unit_cutoff, limits)
    base.mae_self = round(mae_self, 6) if np.isfinite(mae_self) else float("nan")
    _cmp = mae_self if np.isfinite(mae_self) else mae_median_base
    _model_beats_self = (base.detector_mode == "ml_corridor" and np.isfinite(base.mae_val)
                         and np.isfinite(_cmp) and base.mae_val < _cmp)
    if base.detector_mode == "ml_corridor" and _model_beats_self:
        base.corridor_quality = "genuine"
    elif _art_self:
        base.calibration = _art_self
        base.detector_mode = "univariate_band"
        base.corridor_quality = "self_band"
        base.note = (base.note + "; self-band основной (модель не бьёт медиану по MAE)").strip("; ")
    elif base.detector_mode == "ml_corridor":
        base.corridor_quality = "steady_band"

    pe_all = _pred_mean(ho[feats])
    yo = ho[target].values
    for label, lo, hi in HORIZON_WINDOWS:
        left = unit_cutoff + pd.Timedelta(days=lo)
        right = ho.index.max() + pd.Timedelta(seconds=1) if hi is None else unit_cutoff + pd.Timedelta(days=hi)
        m = np.asarray((ho.index > left) & (ho.index <= right))
        if m.sum() == 0:
            base.metrics_windows[label] = dict(n=0)
            continue
        mm = _metrics(yo[m], pe_all[m])
        mm["nmae"] = round(mm["mae"] / sr, 5) if (mm["mae"] is not None and sr > 1e-9) else None
        mm["mape"] = _mape(yo[m], pe_all[m], target, sensor_range=sr)
        base.metrics_windows[label] = mm

    try:
        imp = mdl.get_feature_importance()
        order = np.argsort(imp)[::-1][:8]
        base.feature_importance = [{"name": feats[i], "importance": round(float(imp[i]), 3)}
                                   for i in order if imp[i] > 0]
    except Exception:
        pass
    return base


def train_sensor_pooled(dfu_by_gpa: dict, target: str, feats: list, global_cutoff: pd.Timestamp,
                        limits: dict, gpa_ids: list, regime_cfg: RegimeConfig | None = None,
                        calib_cfg: dict | None = None, es_val_frac: float = 0.15,
                        max_iters: int = 1500, ve_count: int = VE_COUNT):
    """POOLED cross-GPA модель для НЕ-вибро датчика. z-norm по каждому ГПА (фичи+таргет на healthy
    ≤cutoff), обучение ОДНОЙ CatBoost(RMSEWithUncertainty+posterior_sampling) на пуле всех ГПА.
    Коридор/эпистемика/гейт — ПЕР-ГПА (через _PooledAdapter на своих остатках; ключи режимов/cutoff
    свои). Возвращает (shared_model | None, {gid: SensorModel})."""
    from catboost import CatBoostRegressor
    regime_cfg = regime_cfg or RegimeConfig()
    calib_cfg = calib_cfg or dict(alpha=DEFAULT_ALPHA, n_eff_min=N_EFF_MIN, n_boot=DEFAULT_N_BOOT)

    # общий набор фич — присутствуют во ВСЕХ ГПА (идентичные агрегаты; вердикт мог отличаться)
    feats_c = [f for f in feats
               if all((dfu_by_gpa.get(g) is not None and f in dfu_by_gpa[g].columns) for g in gpa_ids)]
    out: dict = {}
    if len(feats_c) < 2:
        for g in gpa_ids:
            b = SensorModel(target=target, gpa_id=g, feat_cols=feats_c, unit_cutoff=None,
                            cutoff_mode="", detector_mode="univariate_only", pooled=True,
                            note="pooled: <2 общих фич")
            out[g] = b
        return None, out

    # ── per-GPA: healthy≤cutoff, z-norm параметры, нормализованный вклад в пул ──
    parts, prep = [], {}
    for g in gpa_ids:
        dfu = dfu_by_gpa.get(g)
        if dfu is None or target not in dfu.columns:
            continue
        healthy = verified_healthy_mask(dfu, limits, cfg=regime_cfg).reindex(dfu.index).fillna(False)
        sub = dfu.loc[healthy, [target] + feats_c].dropna(subset=[target])
        tr = sub[sub.index <= pd.Timestamp(global_cutoff)]
        if len(tr) < 200:
            continue
        fmu = tr[feats_c].mean()
        fsd = tr[feats_c].std().replace(0, np.nan).fillna(1.0)
        fsd[fsd < 1e-9] = 1.0
        tmu = float(tr[target].mean())
        tsd = float(tr[target].std() or 1.0)
        tsd = tsd if tsd > 1e-9 else 1.0
        trn = (tr[feats_c] - fmu) / fsd
        trn[target] = (tr[target].values - tmu) / tsd
        parts.append(trn[feats_c + [target]])
        prep[g] = dict(fmu=fmu, fsd=fsd, tmu=tmu, tsd=tsd)

    if not parts:
        for g in gpa_ids:
            out[g] = SensorModel(target=target, gpa_id=g, feat_cols=feats_c, unit_cutoff=None,
                                 cutoff_mode="", detector_mode="univariate_only", pooled=True,
                                 note="pooled: нет train ни у одного ГПА")
        return None, out

    pool = pd.concat(parts, ignore_index=True).dropna()
    # ES-валидация: СЛУЧАЙНЫЕ 15% пула (хронологический сплит не имеет смысла на смеси ГПА;
    # это убирает ES-артефакт «нерепрезентативный хвост»).
    rng = np.random.default_rng(42)
    vmask = rng.random(len(pool)) < es_val_frac
    if vmask.sum() < 30 or (~vmask).sum() < 100:
        vmask = np.zeros(len(pool), bool)
        vmask[-max(30, len(pool) // 10):] = True
    shared = CatBoostRegressor(iterations=max_iters, depth=6, learning_rate=0.05, l2_leaf_reg=20,
                               loss_function="RMSEWithUncertainty", posterior_sampling=True,
                               bootstrap_type="Bernoulli", subsample=0.8, random_seed=42,
                               logging_level="Silent", thread_count=2)
    try:
        shared.fit(pool.loc[~vmask, feats_c], pool.loc[~vmask, target],
                   eval_set=(pool.loc[vmask, feats_c], pool.loc[vmask, target]),
                   early_stopping_rounds=50, use_best_model=True)
    except Exception as e:
        for g in gpa_ids:
            out[g] = SensorModel(target=target, gpa_id=g, feat_cols=feats_c, unit_cutoff=None,
                                 cutoff_mode="", detector_mode="univariate_only", pooled=True,
                                 note=f"pooled fit error: {e}")
        return None, out

    # ── per-GPA финализация через адаптер (де-норм в сырые единицы ГПА) ──
    for g in gpa_ids:
        b = SensorModel(target=target, gpa_id=g, feat_cols=feats_c, unit_cutoff=None,
                        cutoff_mode="", detector_mode="univariate_only", pooled=True)
        out[g] = b
        if g not in prep:
            b.note = "pooled: мало train у ГПА → univariate"
            continue
        dfu = dfu_by_gpa[g]
        res = resolve_unit_cutoff(dfu, pd.Timestamp(global_cutoff), limits, regime_cfg, allow_per_unit=False)
        b.cutoff_mode = res["mode"]
        b.pre_fault = {"check_required": res.get("pre_fault_check_required", False)}
        if res["unit_cutoff"] is None:
            b.note = f"нет healthy steady ({res['mode']}) → univariate"
            continue
        unit_cutoff = pd.Timestamp(res["unit_cutoff"])
        b.unit_cutoff = str(unit_cutoff)
        healthy = verified_healthy_mask(dfu, limits, cfg=regime_cfg).reindex(dfu.index).fillna(False)
        sub = dfu.loc[healthy, [target] + feats_c].dropna(subset=[target])
        sr = float(np.nanpercentile(sub[target], 99) - np.nanpercentile(sub[target], 1))
        b.sensor_range = round(sr, 6)
        tr = sub[sub.index <= unit_cutoff]
        ho = sub[sub.index > unit_cutoff]
        b.feat_ranges = {f: (round(float(tr[f].quantile(0.01)), 6), round(float(tr[f].quantile(0.99)), 6))
                         for f in feats_c if f in tr.columns and tr[f].notna().any()}
        if len(tr) < 200 or len(ho) < 50:
            b.note = f"мало данных (train={len(tr)}, ho={len(ho)}) → univariate"
            continue
        p = prep[g]
        adapter = _PooledAdapter(shared, feats_c, p["fmu"], p["fsd"], p["tmu"], p["tsd"])
        b.n_trees = adapter.tree_count_
        b._model = adapter   # type: ignore[attr-defined]
        b.norm = dict(feat=feats_c,
                      feat_mu={k: round(float(p["fmu"][k]), 8) for k in feats_c},
                      feat_sd={k: round(float(p["fsd"][k]), 8) for k in feats_c},
                      tgt_mu=round(p["tmu"], 8), tgt_sd=round(p["tsd"], 8))
        _finalize_calibration(b, adapter, dfu, sub, tr, ho, target, feats_c,
                              unit_cutoff, sr, regime_cfg, calib_cfg, limits, ve_count)
    return shared, out


# ── Сериализация SensorModel → metadata.json (назад-совместимая) ─────────────────
def _legacy_conformal_thr(calibration: dict) -> Optional[float]:
    """Скалярная legacy-проекция (для region-SHAP/диагностики). q̂ нормализован → это множитель,
    не абсолютный порог; берём опорный mainline или медиану ok-порогов."""
    by = (calibration or {}).get("by_regime", {})
    oks = [v["threshold"] for v in by.values()
           if v.get("decision") == "ok" and v.get("threshold") is not None
           and np.isfinite(v.get("threshold", float("nan")))]
    if not oks:
        return None
    for k, v in by.items():
        if v.get("decision") == "ok" and "mainline" in k and v.get("threshold") is not None \
                and np.isfinite(v["threshold"]):
            return round(float(v["threshold"]), 6)
    return round(float(np.median(oks)), 6)


def _none_if_nan(x, nd=4):
    return None if (x is None or not np.isfinite(x)) else round(float(x), nd)


def sensor_to_meta(m: SensorModel, name_to_tag: dict | None = None) -> dict:
    """SensorModel → metadata['models'][key]: legacy-поля (контракт live/API/БД/фронт) + поля метода."""
    name_to_tag = name_to_tag or {}
    safe = m.target.replace(" ", "_").replace("/", "_")
    # pooled: ОБЩИЙ файл модели на имя датчика (один на 3 ГПА); per-unit: свой файл на ГПА.
    model_file = m.model_file_override if (m.pooled and m.model_file_override) else f"{safe}__GPA{m.gpa_id}.joblib"
    r2 = _none_if_nan(m.r2_eval)
    return {
        # ── legacy (совместимость) ──
        "tag": name_to_tag.get(m.target, "N/A"), "gpa_id": m.gpa_id, "name": m.target,
        "feat_cols": m.feat_cols, "predictors": m.feat_cols,
        "model_file": model_file, "model_type": "CatBoostUnc-v2", "best_model": "CatBoostUnc-v2",
        # ── pooled (кросс-ГПА): флаг + z-norm параметры ЭТОГО ГПА (для норм/де-норм на инференсе) ──
        "pooled": bool(m.pooled), "norm": (m.norm or None),
        "mae_val": _none_if_nan(m.mae_val, 6), "mae_self": _none_if_nan(m.mae_self, 6),
        "rmse_val": _none_if_nan(m.rmse_val, 6),
        "nmae_val": _none_if_nan(m.nmae_val, 5), "mae_train": _none_if_nan(m.mae_val, 6),
        "r2_val": r2, "r2_train": r2, "r2_insample": None,
        "sensor_range": _none_if_nan(m.sensor_range, 6),
        "calib_scale": _none_if_nan(m.residual_std_val, 6),
        "residual_std_val": _none_if_nan(m.residual_std_val, 6),
        "residual_mean_val": _none_if_nan(m.residual_mean_val, 6),
        "conformal_thr": _legacy_conformal_thr(m.calibration), "pot_thr": None,
        "top_features": m.feature_importance, "eval_windows": m.metrics_windows,
        # ── метод ──
        "schema": "v2", "last_train_ts": m.unit_cutoff, "cutoff_mode": m.cutoff_mode,
        "detector_mode": m.detector_mode, "calibration": m.calibration,
        "self_centers": (m.self_centers or None),   # центры self-conformal полосы по режимам (univariate_band / self-кандидат)
        "self_calibration": (m.self_calibration or None),  # self-полоса как КАНДИДАТ (corridor_mode='self')
        "load_binning": m.load_binning, "epistemic_ref": m.epistemic_ref, "ve_count": VE_COUNT,
        "n_trees": m.n_trees, "r2_eval": r2, "r2_baseline": _none_if_nan(m.r2_baseline),
        "corridor_quality": m.corridor_quality, "feat_ranges": m.feat_ranges,
        "metrics_windows": m.metrics_windows, "pre_fault": m.pre_fault, "note": m.note,
    }


def build_metadata(station_id, gpa_ids, tag_to_name, name_to_tag, models_meta,
                   regime_cfg: RegimeConfig, global_cutoff, extra_root: dict | None = None) -> dict:
    """Полный metadata.json (schema_version=v2 + regime_config + сохранённые корневые поля)."""
    md = {
        "station_id": station_id, "schema_version": "v2", "model_version": "v23.0-norm-conformal",
        "methodology": "normalized-conformal (|r|/σ, virtual-ensembles) / block-Mondrian / epistemic-novelty",
        # ПЕРЕКЛЮЧАТЕЛЬ КОРИДОРА в проде (без рефита; хранятся ОБА порога q̂_norm + q̂_abs):
        #   "conformal" — плоский конформный: предикт ± q̂_abs (фикс. порог на |факт−предикт|)
        #   "hybrid"    — нормализованный: предикт ± q̂_norm·σ (раздувается с σ из ансамбля)
        # Переопределяется env CS_CORRIDOR_MODE или station-config methodology.corridor_mode.
        "corridor_mode": "conformal",
        "trained_at": None, "last_train_timestamp": pd.Timestamp(global_cutoff).isoformat(),
        "cutoff_date": str(pd.Timestamp(global_cutoff).date()),
        "gpa_ids": list(gpa_ids), "tag_to_name": tag_to_name, "name_to_tag": name_to_tag,
        "regime_config": dataclasses.asdict(regime_cfg), "models": models_meta,
    }
    if extra_root:
        md.update(extra_root)
    return md


# ════════════════════════════════════════════════════════════════════════════════
#  СЕКЦИЯ 6. ПРОД-ДРАЙВЕР: ЗАГРУЗКА ИЗ PostgreSQL + ОБУЧЕНИЕ ВСЕХ ДАТЧИКОВ
# ════════════════════════════════════════════════════════════════════════════════

def _load_wide_windowed(loader, from_date=None, window_days: int = 20):
    """Оконная загрузка обучающих данных в wide-формат. ПАМЯТЬ: полный стриминг всей истории
    рвёт удалённый коннект; оконные запросы (fetch_raw_window, с ретраями) — ок."""
    from station_config import get_db_connection
    sch, tbl, dt = loader._schema, loader._table, loader._dt_col
    with get_db_connection(loader.cfg) as conn:
        with conn.cursor() as cur:
            if from_date:
                cur.execute(f'SELECT max("{dt}") FROM "{sch}"."{tbl}"')
                lo_db, hi_db = None, cur.fetchone()[0]
            else:
                cur.execute(f'SELECT min("{dt}"), max("{dt}") FROM "{sch}"."{tbl}"')
                lo_db, hi_db = cur.fetchone()
    if hi_db is None:
        return pd.DataFrame()
    lo = pd.Timestamp(from_date) if from_date else pd.Timestamp(lo_db)
    hi = pd.Timestamp(hi_db)
    if getattr(lo, "tz", None) is not None:
        lo = lo.tz_localize(None)
    if getattr(hi, "tz", None) is not None:
        hi = hi.tz_localize(None)
    frames, cur_lo = [], lo
    n_win = 0
    while cur_lo <= hi:
        cur_hi = min(cur_lo + pd.Timedelta(days=window_days), hi + pd.Timedelta(seconds=1))
        df_win = loader.fetch_raw_window(str(cur_lo), str(cur_hi))
        if len(df_win):
            frames.append(df_win)
        n_win += 1
        print(f"    окно {n_win}: {cur_lo.date()}..{cur_hi.date()} → {len(df_win)} строк", flush=True)
        cur_lo = cur_hi
    if not frames:
        return pd.DataFrame()
    return loader._to_wide(pd.concat(frames, ignore_index=True))


def train_all(station: str, cutoff_date=None, from_date=None,
              dry_run: bool = False, output_dir: str | None = None,
              windowed: bool = True, window_days: int = 20,
              cache_wide: str | None = None, gpa: str | None = None,
              per_unit_cutoff: bool = False, sensors: list | None = None) -> dict:
    """Прод-драйвер: грузит из PostgreSQL, готовит данные, обучает train_sensor по каждому отклику,
    пишет metadata.json + .joblib. gpa — обучить ТОЛЬКО этот ГПА и СМЁРЖИТЬ в существующий metadata."""
    import os, json
    from station_config import load_station_config
    from data_loader import PostgresDataLoader
    import weather as W

    cfg = load_station_config(station)
    meth = cfg.methodology or {}
    models_dir = output_dir or str(cfg.models_path)
    configure_gas(meth.get("gas"))
    cutoff_ts = pd.Timestamp(cutoff_date or meth.get("train_cutoff"))
    limits = {k: tuple(v) for k, v in (meth.get("limits") or {}).items()}
    conditioning = list(meth.get("conditioning", []))
    cond_domain = list(meth.get("cond_domain", []))
    regression_targets = list(dict.fromkeys(
        list(meth.get("targets_dashboard", [])) + list(meth.get("soft_targets", []))
        + list(meth.get("axial_targets", [])) + list(meth.get("extra_response", []))))
    soft_excl = {k: set(v) for k, v in (meth.get("soft_input_excl") or {}).items()}

    loader = PostgresDataLoader(cfg)
    gpa_ids = [u.replace("GPA", "") for u in cfg.units]
    if gpa:
        gpa_ids = [str(gpa).replace("GPA", "")]
    tag_to_name = loader.build_tag_mapping()
    name_to_tag = {v: k for k, v in tag_to_name.items()}
    if cache_wide and os.path.exists(cache_wide):
        print(f"  wide из кэша: {cache_wide}", flush=True)
        raw = pd.read_pickle(cache_wide)
        # from_date применяем и к кэшу (иначе перебор точки входа на общем кэше игнорировал бы её)
        if from_date is not None:
            raw = raw.loc[raw.index >= pd.Timestamp(from_date)]
            print(f"  from_date={from_date} → {raw.shape[0]} строк из кэша", flush=True)
    else:
        if windowed:
            raw = _load_wide_windowed(loader, from_date=from_date, window_days=window_days)
        else:
            raw = loader.fetch_training_data(cutoff_date=None, from_date=from_date)
        raw = raw.rename(columns=tag_to_name).sort_index()
        if getattr(raw.index, "tz", None) is not None:
            raw.index = raw.index.tz_convert("Etc/GMT-5").tz_localize(None)
        if cache_wide:
            try:
                raw.to_pickle(cache_wide)
                print(f"  wide закэширован: {cache_wide} {raw.shape}", flush=True)
            except Exception as _e:
                print(f"  кэш не сохранён: {_e}", flush=True)
    try:
        amb = W.get_ambient_series(cfg, raw.index)
    except Exception:
        amb = None

    regime_cfg = RegimeConfig()
    if not dry_run:
        os.makedirs(models_dir, exist_ok=True)
    models_meta: dict = {}
    if gpa:  # merge-режим: сохраняем модели ДРУГИХ ГПА, перезаписываем только этот
        try:
            with open(os.path.join(models_dir, "metadata.json"), encoding="utf-8") as f:
                _ex = json.load(f).get("models", {})
            # merge: убираем PER-UNIT записи этого ГПА (переобучим их); pooled-записи СОХРАНЯЕМ
            # (pooled нельзя переобучить по одному ГПА — нужен полный прогон всех ГПА).
            models_meta = {k: v for k, v in _ex.items()
                           if not (k.endswith(f"__GPA{gpa_ids[0]}") and not v.get("pooled"))}
            print(f"  merge: сохранено {len(models_meta)} записей (pooled ГПА-{gpa_ids[0]} оставлены; "
                  f"перезапишу per-unit ГПА-{gpa_ids[0]})", flush=True)
        except Exception:
            models_meta = {}
    # ── общий препроцессинг по ГПА (один раз; pooled нужен доступ ко всем ГПА сразу) ──
    dfu_by_gpa, keep_by_gpa, verd_by_gpa = {}, {}, {}
    for gid in gpa_ids:
        suf = f"__GPA{gid}"
        cols = [c for c in raw.columns if c.endswith(suf)]
        if len(cols) < 3:
            continue
        dfu = raw[cols].copy()
        dfu.columns = [c[:-len(suf)] for c in cols]
        dfu = dfu.ffill(limit=2)
        if amb is not None:
            dfu["ambient_temp"] = amb.reindex(dfu.index).values
        dfu = physically_clean(dfu, limits)
        run = steady_running_mask(dfu)
        dfu = add_domain_features_gpa(dfu, running_mask=run, train_cutoff=cutoff_ts)
        vdf = column_verdict(dfu, cutoff_ts)
        keep, _excl = keep_exclude(vdf, continuous_cols(dfu))
        dfu_by_gpa[gid] = dfu
        keep_by_gpa[gid] = set(keep)
        verd_by_gpa[gid] = {r.sensor: r.verdict for r in vdf.itertuples()}

    # per-unit семейства (подстроки из config pooling.per_unit, напр. ["vibro"]) и «рабочая точка».
    _pu_subs = [s.lower() for s in ((meth.get("pooling", {}) or {}).get("per_unit", []) or [])]
    _OP_SUBS = ("rpm", "pressure_ratio", "ambient", "fuel_gas_flow", "avo_approach", "load")

    def _is_pu_family(name: str) -> bool:
        return any(s in name.lower() for s in _pu_subs)

    def _feats_per_unit(gid, tgt):
        dfu, keep = dfu_by_gpa[gid], keep_by_gpa[gid]
        # ВИБРАЦИЯ (и др. per-unit семейства): предсказываем по ФИЗИЧЕСКИМ СОСЕДЯМ (другие каналы
        # того же семейства того же ГПА) + РАБОЧАЯ ТОЧКА (обороты/нагрузка/ratio/ambient). Газовый
        # тракт НЕ используем — он не предсказывает механику (R²<0 → смещённый центр → ложные
        # аномалии; доказано AUDIT_2026-07-03). Соседи дают R²>0 → остаток связи = дискриминатор
        # режим/дефект; common-mode ловит Д3/SPE+флот.
        if _pu_subs and _is_pu_family(tgt):
            siblings = [c for c in dfu.columns if _is_pu_family(c) and c != tgt and c in keep]
            op = [c for c in (conditioning + cond_domain)
                  if c != tgt and c in dfu.columns and any(s in c.lower() for s in _OP_SUBS)
                  and (c in cond_domain or c in keep)]
            fa = list(dict.fromkeys(siblings + op))
            return [f for f in fa if f not in soft_excl.get(tgt, set())]
        # прочие per-unit датчики — как раньше (полный conditioning-пул)
        fa = ([c for c in conditioning if c in dfu.columns and c in keep]
              + [c for c in cond_domain if c in dfu.columns])
        return [f for f in fa if f != tgt and f not in soft_excl.get(tgt, set())]

    def _save_model(fn, model, fc):
        if not dry_run and model is not None:
            joblib.dump({"model": model, "model_type": "CatBoostUnc-v2", "feat_cols": fc,
                         "needs_impute": False, "uncertainty": True}, os.path.join(models_dir, fn))

    for tgt in regression_targets:
        if sensors and tgt not in sensors:
            continue
        pooled = is_pooled_target(tgt, meth)
        if pooled and not gpa:
            # ── POOLED: одна кросс-ГПА модель (термо/механика). Нужен полный прогон. ──
            cand = [c for c in (conditioning + cond_domain)
                    if c != tgt and c not in soft_excl.get(tgt, set())
                    and all(g in dfu_by_gpa and c in dfu_by_gpa[g].columns
                            and (c in cond_domain or c in keep_by_gpa[g]) for g in gpa_ids)]
            gids_ok = [g for g in gpa_ids if g in dfu_by_gpa and tgt in dfu_by_gpa[g].columns
                       and verd_by_gpa[g].get(tgt, "ok") not in UNMODELABLE]
            if not gids_ok or len(cand) < 2:
                continue
            shared, per = train_sensor_pooled(dfu_by_gpa, tgt, cand, cutoff_ts, limits, gids_ok, regime_cfg)
            safe = tgt.replace(" ", "_").replace("/", "_")
            shared_fn = f"{safe}__POOLED.joblib"
            _save_model(shared_fn, shared, per[gids_ok[0]].feat_cols if gids_ok else cand)
            for g, m in per.items():
                m.model_file_override = shared_fn
                models_meta[f"{tgt}__GPA{g}"] = sensor_to_meta(m, name_to_tag)
            print(f"  {tgt} [POOLED]: " + " ".join(f"GPA{g}={per[g].detector_mode}" for g in per), flush=True)
        elif not pooled:
            # ── PER-UNIT (вибрация; работает и в merge-режиме --gpa) ──
            for gid in gpa_ids:
                if gid not in dfu_by_gpa:
                    continue
                dfu = dfu_by_gpa[gid]
                if tgt not in dfu.columns or verd_by_gpa[gid].get(tgt, "ok") in UNMODELABLE:
                    continue
                feats = _feats_per_unit(gid, tgt)
                # <2 кросс-фич больше НЕ пропускаем: train_sensor построит self-band-only (fix #3a),
                # датчик остаётся в мониторинге с полосой нормального диапазона, а не выпадает молча.
                m = train_sensor(dfu, tgt, feats, cutoff_ts, limits, gid, regime_cfg, per_unit=per_unit_cutoff)
                key = f"{tgt}__GPA{gid}"
                models_meta[key] = sensor_to_meta(m, name_to_tag)
                _save_model(models_meta[key]["model_file"], getattr(m, "_model", None), m.feat_cols)
                print(f"  {key}: {m.detector_mode} n_trees={m.n_trees} R²_eval={m.r2_eval}", flush=True)
        # pooled target в merge-режиме (--gpa) → пропуск (старая pooled-запись сохранена фильтром выше)

    th = meth.get("thresholds", {})
    md = build_metadata(
        cfg.station_id, gpa_ids, tag_to_name, name_to_tag, models_meta, regime_cfg, cutoff_ts,
        extra_root={
            "health_index": list(meth.get("health_index", [])),
            "conditioning": conditioning + cond_domain, "response_targets": regression_targets,
            "anomaly_n_sigma": float(th.get("n_sigma", 5.0)),
            "min_buffer_pct": float(th.get("min_buffer_pct", 0.04)),
            "var_smoothing": int(th.get("var_smoothing", 24)),
            "gas_constants": dict(k=round(GAS_K, 5), R=round(GAS_R, 3), Z=GAS_Z,
                                  M=round(GAS_M, 4), t_ref_k=T_REF_K),
        })
    if not dry_run:
        with open(os.path.join(models_dir, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(md, f, ensure_ascii=False, indent=2, default=str)
    return md


# ════════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    import argparse
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf8")

    parser = argparse.ArgumentParser(description="Обучение моделей здоровья ГПА (normalized-conformal)")
    parser.add_argument("--station", help="ID компрессорной станции")
    parser.add_argument("--cutoff-date", help="Граница train (ISO); переопределяет config.train_cutoff")
    parser.add_argument("--from-date", help="Нижняя граница окна загрузки (ISO)")
    parser.add_argument("--dry-run", action="store_true", help="Проверить данные без обучения")
    parser.add_argument("--list-stations", action="store_true", help="Список станций и выход")
    parser.add_argument("--output-dir", help="Куда писать модели/metadata (по умолч. прод-папка)")
    parser.add_argument("--gpa", help="Обучить ТОЛЬКО этот ГПА и смёржить в metadata")
    parser.add_argument("--per-unit-cutoff", action="store_true",
                        help="Пер-юнитный cutoff (variant B) для standby. По умолч. ВЫКЛ (единый cutoff).")
    parser.add_argument("--cache-wide", default=None,
                        help="pickle-кэш wide (читать если есть, иначе создать) — ускоряет повторные прогоны")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from logging_config import setup as _log_setup
    _log_setup("train")

    if args.list_stations:
        from station_config import list_stations
        stations = list_stations()
        print("Доступные станции:", ", ".join(stations) if stations else "(нет)")
        sys.exit(0)
    if not args.station:
        print("❌ Укажите --station или используйте --list-stations", file=sys.stderr)
        sys.exit(1)

    print("▶ Методология: normalized-conformal (|факт−предикт|/σ, virtual ensembles) + epistemic-novelty",
          flush=True)
    train_all(args.station, cutoff_date=args.cutoff_date, from_date=args.from_date,
              dry_run=args.dry_run, output_dir=args.output_dir,
              gpa=args.gpa, per_unit_cutoff=args.per_unit_cutoff, cache_wide=args.cache_wide)
