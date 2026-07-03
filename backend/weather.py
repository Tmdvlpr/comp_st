"""
Внешняя температура воздуха (ambient_temp) для станции — Open-Meteo.

Нужна для доменных фич: приведённые обороты (n/√(T/T_ref)), shaft mismatch, avo_approach.
Open-Meteo отдаёт ПОЧАСОВЫЕ значения; на 5-мин сетку SCADA приводим ЛИНЕЙНОЙ интерполяцией
по времени (гладкая температура каждые 5 минут, не ступенька).

Источник:
- archive-api (история, надёжно для дат старше ~5 дней),
- forecast-api (свежий хвост past_days, т.к. archive отстаёт).
Кэш — CSV (config.location.weather_cache); при устаревании дотягивается хвост.
Время — локальное станции (Asia/Tashkent = UTC+5), naive — совпадает с сеткой пайплайна (Etc/GMT-5 naive).
"""
from __future__ import annotations

import logging
import time as _time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_START = "2026-01-01"
BASE_DIR = Path(__file__).parent


def _get(url: str, params: dict, max_retries: int = 6) -> dict | None:
    """GET с экспоненциальным backoff на 429/сетевые ошибки. None при неудаче."""
    import requests
    delay = 10
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                logger.warning("Open-Meteo 429 (попытка %d/%d) — ждём %ds", attempt, max_retries, delay)
                _time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            logger.warning("Open-Meteo HTTP %d: %s", r.status_code, r.text[:200])
            return None
        except Exception as e:
            logger.warning("Open-Meteo сеть (попытка %d/%d): %s", attempt, max_retries, e)
            _time.sleep(delay)
            delay = min(delay * 2, 120)
    return None


def _hourly_df(payload: dict | None) -> pd.DataFrame:
    if not payload or "hourly" not in payload:
        return pd.DataFrame(columns=["time", "temperature_2m"])
    h = payload["hourly"]
    df = pd.DataFrame({"time": h.get("time", []), "temperature_2m": h.get("temperature_2m", [])})
    df["time"] = pd.to_datetime(df["time"])
    return df.dropna()


def fetch_ambient_hourly(lat: float, lon: float, tz: str, start: str, end: str) -> pd.DataFrame:
    """Почасовая ambient за [start, end] локального времени: archive + forecast-хвост, объединение."""
    frames = []
    arch = _hourly_df(_get(ARCHIVE_URL, {
        "latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
        "hourly": "temperature_2m", "timezone": tz}))
    if not arch.empty:
        frames.append(arch)
    # свежий хвост (archive отстаёт ~5 дней) — forecast с past_days
    rec = _hourly_df(_get(FORECAST_URL, {
        "latitude": lat, "longitude": lon, "past_days": 14, "forecast_days": 1,
        "hourly": "temperature_2m", "timezone": tz}))
    if not rec.empty:
        frames.append(rec)
    if not frames:
        return pd.DataFrame(columns=["time", "temperature_2m"])
    out = (pd.concat(frames, ignore_index=True)
           .drop_duplicates(subset="time", keep="last")
           .sort_values("time").reset_index(drop=True))
    return out


def _cache_path(cfg) -> Path | None:
    loc = getattr(cfg, "location", {}) or {}
    rel = loc.get("weather_cache")
    return (BASE_DIR / rel) if rel else None


def update_cache(cfg, end: str | None = None) -> pd.DataFrame:
    """Загружает кэш погоды, дотягивает свежие данные из Open-Meteo, сохраняет. Возвращает почасовой df."""
    loc = getattr(cfg, "location", {}) or {}
    lat, lon = loc.get("lat"), loc.get("lon")
    tz = loc.get("timezone", "Asia/Tashkent")
    if lat is None or lon is None:
        logger.warning("location.lat/lon не заданы — ambient недоступен")
        return pd.DataFrame(columns=["time", "temperature_2m"])

    end = end or pd.Timestamp.now().normalize().date().isoformat()
    cache = _cache_path(cfg)
    cached = pd.DataFrame(columns=["time", "temperature_2m"])
    if cache and cache.exists():
        try:
            cached = pd.read_csv(cache, parse_dates=["time"])
        except Exception:
            logger.warning("кэш погоды повреждён — перезагрузка")

    # нужно ли тянуть: пусто или хвост старше суток
    need = cached.empty or (pd.Timestamp(end) - cached["time"].max()) > pd.Timedelta(days=1)
    if need:
        start = DEFAULT_START if cached.empty else (cached["time"].max() - pd.Timedelta(days=2)).date().isoformat()
        fresh = fetch_ambient_hourly(lat, lon, tz, start, end)
        if not fresh.empty:
            cached = (pd.concat([cached, fresh], ignore_index=True)
                      .drop_duplicates(subset="time", keep="last")
                      .sort_values("time").reset_index(drop=True))
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cached.to_csv(cache, index=False)
                logger.info("ambient кэш обновлён: %d часов до %s", len(cached), cached["time"].max())
    return cached


def get_ambient_series(cfg, index: pd.DatetimeIndex, seed_csv: str | None = None) -> pd.Series:
    """ambient_temp, выровненная на 5-мин (любой) `index` ЛИНЕЙНОЙ интерполяцией по времени.

    index — naive локальное время станции (Etc/GMT-5 = UTC+5 = Asia/Tashkent), как и кэш.
    Возвращает Series с тем же index (NaN там, где погода вне диапазона кэша).
    """
    df = update_cache(cfg)
    if df.empty and seed_csv:
        try:
            df = pd.read_csv(seed_csv, parse_dates=["time"])
        except Exception:
            pass
    if df.empty:
        return pd.Series(index=index, dtype=float, name="ambient_temp")
    s = pd.to_numeric(df.set_index("time")["temperature_2m"], errors="coerce").sort_index()
    s = s[~s.index.duplicated(keep="last")].dropna()
    # почасовая → объединяем с целевой 5-мин сеткой → интерполируем по времени → берём целевые точки
    union = s.index.union(index)
    out = s.reindex(union).interpolate(method="time", limit_direction="both").reindex(index)
    out.name = "ambient_temp"
    return out
