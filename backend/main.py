"""
FastAPI backend for CS Monitor AI dashboard.
Multi-station architecture: serves any registered station via /api/stations/{id}/*.
Backward-compat aliases (/api/sensors, /api/stats, /api/events, /api/heatmap)
delegate to the default station (ohangaron).
"""
from __future__ import annotations
import json, logging, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import psycopg2.extras
from psycopg2 import sql as pgsql
import numpy as np
import pandas as pd
import joblib
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from station_config import (
    load_station_config, list_stations, StationConfig, get_db_connection,
    journal_table_name,
)
from data_loader import PostgresDataLoader
from anomaly_types import CODE_TO_KIND, KIND_SEVERITY as _KIND_SEV, HEALTH_OK, HEALTH_STOPPED, max_severity as _max_sev

# Файловые логи с ротацией (logs/api.log) — logger.* вызовы не теряются
from logging_config import setup as _log_setup
_log_setup("api", tee_stdout=False)

BASE_DIR = Path(__file__).parent
DEFAULT_STATION = "ohangaron"

app = FastAPI(title="CS Monitor AI", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup_ensure_indexes():
    """Опционально: создание индексов БД (CS_ENSURE_INDEXES=1). DDL на общей БД
    выполняется только при явном включении флага."""
    import os
    if os.environ.get("CS_ENSURE_INDEXES") != "1":
        return
    try:
        from ensure_indexes import ensure_indexes
        ensure_indexes(DEFAULT_STATION)
    except Exception:
        logger.exception("ensure_indexes failed on startup (non-fatal)")

# ── Caches: {station_id → (data, mtime)} ────────────────────────────────────
_state_cache: dict[str, tuple[dict, float]] = {}
_meta_cache:  dict[str, tuple[dict, float]] = {}


def _get_live_state(station_id: str) -> dict:
    """Reads state/{station_id}_live_state.json with mtime-based caching."""
    global _state_cache
    try:
        cfg = load_station_config(station_id)
        state_path = cfg.state_path
    except FileNotFoundError:
        return {}

    if not state_path.exists():
        return {}

    mtime = state_path.stat().st_mtime
    cached = _state_cache.get(station_id)
    if cached and cached[1] == mtime:
        return cached[0]

    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        _state_cache[station_id] = (state, mtime)
        return state
    except Exception:
        logger.warning("Не прочитан state-файл %s — отдаю кеш", state_path, exc_info=True)
        return cached[0] if cached else {}


def _get_metadata(station_id: str) -> dict:
    """Reads models/{station_id}/metadata.json with mtime-based caching."""
    try:
        cfg = load_station_config(station_id)
        meta_path = cfg.models_path / "metadata.json"
        if not meta_path.exists():
            return {}
        mtime = meta_path.stat().st_mtime
        cached = _meta_cache.get(station_id)
        if cached and cached[1] == mtime:
            return cached[0]
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        _meta_cache[station_id] = (meta, mtime)
        return meta
    except Exception:
        logger.warning("Не прочитана metadata станции %s — отдаю кеш", station_id, exc_info=True)
        cached = _meta_cache.get(station_id)
        return cached[0] if cached else {}


# ── Pydantic models ──────────────────────────────────────────────────────────
class StationInfo(BaseModel):
    id: str
    display_name: str
    enabled: bool
    units: list[str]
    live_data: bool
    last_updated: Optional[str]


class SensorMeta(BaseModel):
    id: str
    name: str
    gpa: str
    tag: str
    r2: float
    mae: float
    cur: Optional[float] = None
    anomaly_count: int
    anomaly_count_30d: Optional[int] = None
    anomaly_types: list[str]
    severity: str
    subsystem: str
    # ── аналитика research-методологии (опц.; заполняется из metadata после переобучения) ──
    nmae: Optional[float] = None
    rmse: Optional[float] = None
    r2_val: Optional[float] = None
    r2_insample: Optional[float] = None
    best_model: Optional[str] = None
    drift_score: Optional[float] = None
    # реальная пер-сенсорная аналитика (DetailPanel: Дрейф/Калибровка/Доменные индексы)
    drift: Optional[dict] = None
    calibration: Optional[dict] = None
    domain: Optional[dict] = None
    # ── v2 (опц.): режим детекции и текущий режим работы ──
    detector_mode: Optional[str] = None     # ml_corridor | univariate_only | legacy
    regime: Optional[str] = None            # текущий regime_key (steady|mainline|L0 и т.п.)


class EventItem(BaseModel):
    id: str
    timestamp: str
    ts_end: Optional[str] = None
    points: Optional[int] = None
    sensor_id: str
    sensor_name: str
    gpa: str
    kind: str
    severity: str
    value: Optional[float] = None
    deviation: Optional[float] = None
    description: str
    acked: bool = False


class StatsResponse(BaseModel):
    total_sensors: int
    crit_count: int
    warn_count: int
    info_count: int
    ok_count: int
    ml_count: int
    frozen_count: int
    neg_count: int
    regime_count: int
    roc_count: int
    seasonal_count: int
    cross_count: int
    drift_count: int = 0
    total_anomalies: int
    last_updated: str


class TimeSeriesPoint(BaseModel):
    t: str
    v: float
    p: Optional[float] = None
    lo: Optional[float] = None
    hi: Optional[float] = None
    lo2: Optional[float] = None  # альтернативный коридор (hybrid) для UI-тумблера conformal↔hybrid
    hi2: Optional[float] = None
    e: Optional[float] = None    # эпистемическая неопр. u_epi (детектор-2, фиолет-полоса)


class SensorChartResponse(BaseModel):
    sensor_id: str
    tag: str
    r2: float
    mae: float
    current: Optional[float]
    predicted: Optional[float]
    deviation: Optional[float]
    train_ts: Optional[str] = None   # граница обучения (локальное naive ISO); прогноз — только после неё
    series: list[TimeSeriesPoint]
    anomalies: list[dict]
    epistemic_thr: Optional[float] = None   # порог новизны κ·1.5 (линия на полосе эпистемики); None если нет эталона
    corridor_mode: Optional[str] = None     # режим активного коридора (lo/hi): 'conformal'|'hybrid'; lo2/hi2 = альт.


class MultiSeriesItem(BaseModel):
    sensor_id: str
    name: str
    tag: str
    gpa: str
    unit: Optional[str] = None
    range_min: Optional[float] = None
    range_max: Optional[float] = None
    series: list[dict]   # [{t, v}] — только факт, без предикта (лёгкий ответ)


class HeatmapCell(BaseModel):
    sensor_id: str
    name: str
    gpa: str
    severity: str
    anomaly_count: int


class AnomalyRecord(BaseModel):
    id: int
    sensor_id: str
    event_ts: str
    anomaly_type: int
    severity: Optional[str]
    value: Optional[float]
    deviation: Optional[float]
    created_at: str


class NotificationItem(BaseModel):
    id: int
    station_id: str
    sensor_id: str
    point: Optional[str] = None
    gpa: Optional[str] = None
    event_ts: str
    anomaly_type: int
    kind: Optional[str] = None
    severity: Optional[str] = None
    value: Optional[float] = None
    deviation: Optional[float] = None
    message: str
    status: str
    created_at: str


class SensorHealthSummary(BaseModel):
    sensor_id: str
    point: Optional[str] = None
    evaluated: int          # точек с health != NULL
    ok: int                 # health = '0'
    stopped: int            # health = '8' (ГПА остановлен)
    anomalous: int          # health с кодами аномалий (1..7)
    code_counts: dict       # {код: число точек}
    anomaly_episodes: int   # эпизодов в anomalies
    last_event_ts: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────
def _sensors_list(station_id: str, gpa_filter: Optional[str] = None) -> list[dict]:
    state = _get_live_state(station_id)
    _used_state = bool(state.get("sensors"))
    if _used_state:
        sensors = list(state["sensors"].values())
    else:
        meta = _get_metadata(station_id)
        sensors = []
        for key, info in meta.get("models", {}).items():
            parts = key.rsplit("__", 1)
            name  = parts[0]
            gpa   = parts[1] if len(parts) > 1 else "GPA1"
            sub   = name.split("_")[0].upper() if "_" in name else name[:4].upper()
            sensors.append({
                "id": key, "name": name, "gpa": gpa,
                "tag": info.get("tag", key),
                "r2":  float(info.get("r2_val", info.get("r2_train", 0.0)) or 0.0),
                "mae": float(info.get("mae_val", info.get("mae_train", 0.0)) or 0.0),
                "anomaly_count": 0, "anomaly_types": [],
                "severity": "ok", "subsystem": sub,
                # аналитика для DetailPanel «Качество модели»
                "nmae":        info.get("nmae_val"),
                "rmse":        info.get("rmse_val"),
                "r2_val":      info.get("r2_val"),
                "r2_insample": info.get("r2_insample"),
                "best_model":  info.get("best_model"),
            })
    # В metadata моделей tag часто 'N/A' (не заполняется при обучении), но реальный
    # SCADA-point есть в name_to_tag — он и нужен /chart для запроса raw_data по point.
    # Резолвим здесь централизованно → график, /chart/multi и заголовок получают тег.
    n2t = _get_metadata(station_id).get("name_to_tag") or {}
    for s in sensors:
        real = n2t.get(s["id"])
        if real and (not s.get("tag") or s["tag"] in ("N/A", s["id"])):
            s["tag"] = real

    # Полнота теплокарты «из БД»: добавляем физически существующие, но НЕмоделируемые
    # датчики тех же типов (напр. регулируемая темп. oil_temp_out_st, выпавшая как
    # 'frozen' на одном ГПА) — нейтральной клеткой. Источник наличия — реальные
    # SCADA-теги name_to_tag (что есть в raw_data), а не только обученные модели,
    # чтобы карта отражала реальный состав станции, а не пробелы обучения.
    have = {s["id"] for s in sensors}
    modeled_bases = {sid.rsplit("__", 1)[0] for sid in have}
    for nm, tag in n2t.items():
        if nm in have:
            continue
        base, sep, gpa = nm.rpartition("__")
        if not sep or not gpa.upper().startswith("GPA") or base not in modeled_bases:
            continue
        sub = base.split("_")[0].upper() if "_" in base else base[:4].upper()
        sensors.append({
            "id": nm, "name": base, "gpa": gpa, "tag": tag,
            "r2": 0.0, "mae": 0.0, "anomaly_count": 0, "anomaly_types": [],
            "severity": "ok", "subsystem": sub,
            "nmae": None, "rmse": None, "r2_val": None, "r2_insample": None,
            "best_model": None, "unmodeled": True,
        })
        have.add(nm)

    # Входные (conditioning) сигналы — обороты/давления/температуры/расход. Моделями не
    # являются (это входы), но имеют реальный тег в raw_data → их МОЖНО строить графиком
    # (напр. клик по параметру в «Двигателе»). Помечаем input=True: исключаются из
    # тепловой карты (там только аномалии моделей), но доступны для выбора/графика.
    _INPUT_PREFIXES = ("rpm_", "gas_pressure_", "gas_temp_", "fuel_gas_", "ambient", "anti_surge", "pressure_ratio")
    for nm, tag in n2t.items():
        if nm in have:
            continue
        base, sep, gpa = nm.rpartition("__")
        if not sep or not gpa.upper().startswith("GPA"):
            continue
        if not any(base == p or base.startswith(p) for p in _INPUT_PREFIXES):
            continue
        sub = base.split("_")[0].upper() if "_" in base else base[:4].upper()
        sensors.append({
            "id": nm, "name": base, "gpa": gpa, "tag": tag,
            "r2": 0.0, "mae": 0.0, "anomaly_count": 0, "anomaly_types": [],
            "severity": "ok", "subsystem": sub,
            "nmae": None, "rmse": None, "r2_val": None, "r2_insample": None,
            "best_model": None, "input": True,
        })
        have.add(nm)

    # DB-overlay severity/anomaly_count (+доменные индексы) из anomalies_t/domain — для
    # turnkey без state.json. auto: накладываем, когда state нет (чистый хост); db: всегда
    # (БД authoritative); state: не трогаем (как раньше).
    _src = _dashboard_source()
    if _src == "db" or (_src == "auto" and not _used_state):
        _overlay_sensor_db_agg(station_id, sensors)

    if gpa_filter:
        sensors = [s for s in sensors if s["gpa"].lower() == gpa_filter.lower()]
    return sensors


def _overlay_sensor_db_agg(station_id: str, sensors: list[dict]) -> None:
    """Накладывает severity (макс за 24ч) + anomaly_count (24ч/30д) из anomalies_t и
    доменные индексы из domain на список датчиков (in-place). Для DB-only дашборда."""
    cfg = _require_station(station_id)
    schema = cfg.db["schema"]
    agg: dict = {}
    try:
        sql = pgsql.SQL(
            "SELECT sensor_id,"
            " array_agg(severity) FILTER (WHERE event_ts >= NOW() - interval '24 hours') AS sev24,"
            " count(*) FILTER (WHERE event_ts >= NOW() - interval '24 hours') AS c24,"
            " count(*) AS c30"
            " FROM {s}.anomalies_t WHERE event_ts >= NOW() - interval '30 days'"
            " GROUP BY sensor_id"
        ).format(s=pgsql.Identifier(schema))
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                for sid, sev24, c24, c30 in cur.fetchall():
                    agg[sid] = (list(sev24 or []), int(c24 or 0), int(c30 or 0))
    except Exception:
        logger.debug("sensor agg from anomalies_t failed", exc_info=True)
    dom = _domain_latest_from_db(cfg, station_id)
    for s in sensors:
        a = agg.get(s["id"])
        if a is not None:
            sev24, c24, c30 = a
            s["severity"] = _max_sev(sev24) if sev24 else "ok"
            s["anomaly_count"] = c24
            s["anomaly_count_30d"] = c30
        if dom and s.get("gpa") in dom:
            s["domain"] = dom[s["gpa"]]


def _domain_latest_from_db(cfg: StationConfig, station_id: str) -> dict:
    """Последние доменные индексы по ГПА из {schema}.domain → {'GPA2': {feat: val}}.
    Best-effort: при несовпадении колонок/отсутствии таблицы — {}."""
    meta = _get_metadata(station_id)
    hidx = [c for c in (meta.get("health_index", []) or [])]
    if not hidx:
        return {}
    schema = cfg.db["schema"]
    try:
        # только реально существующие колонки domain (иначе SQL-ошибка)
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='domain'", (schema,))
                have_cols = {r[0] for r in cur.fetchall()}
                cols = [c for c in hidx if c in have_cols]
                if not cols:
                    return {}
                colsql = pgsql.SQL(", ").join(pgsql.Identifier(c) for c in cols)
                cur.execute(pgsql.SQL(
                    "SELECT DISTINCT ON (gpa) gpa, {cols} FROM {s}.domain ORDER BY gpa, datetime DESC"
                ).format(cols=colsql, s=pgsql.Identifier(schema)))
                rows = cur.fetchall()
        out: dict = {}
        for row in rows:
            gpa_num, vals = row[0], row[1:]
            d = {cols[i]: round(float(v), 4) for i, v in enumerate(vals) if v is not None}
            out[f"GPA{gpa_num}"] = d
        return out
    except Exception:
        logger.debug("domain latest from db failed", exc_info=True)
        return {}


def _require_station(station_id: str) -> StationConfig:
    try:
        return load_station_config(station_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Station '{station_id}' not found")


# ── Station endpoints ─────────────────────────────────────────────────────────
@app.get("/api/stations", response_model=list[StationInfo])
def list_station_infos():
    result = []
    for sid in list_stations():
        try:
            cfg   = load_station_config(sid)
            state = _get_live_state(sid)
            result.append(StationInfo(
                id=sid,
                display_name=cfg.display_name,
                enabled=True,
                units=cfg.units,
                live_data=bool(state.get("sensors")),
                last_updated=state.get("last_updated"),
            ))
        except Exception:
            logger.exception("Станция %s не загружена — исключена из списка", sid)
    return result


@app.get("/api/stations/{station_id}/sensors", response_model=list[SensorMeta])
def station_sensors(station_id: str, gpa: Optional[str] = Query(None), response: Response = None):
    _require_station(station_id)
    if response:
        response.headers["Cache-Control"] = "public, max-age=25"
    return [SensorMeta(**s) for s in _sensors_list(station_id, gpa)]


@app.get("/api/stations/{station_id}/sensors/{sensor_id}", response_model=SensorMeta)
def station_sensor(station_id: str, sensor_id: str):
    _require_station(station_id)
    sensors = {s["id"]: s for s in _sensors_list(station_id)}
    if sensor_id not in sensors:
        raise HTTPException(status_code=404, detail="Sensor not found")
    return SensorMeta(**sensors[sensor_id])


def _count_regime_transitions(cfg_obj: StationConfig, days: int = 7) -> int:
    """Count STATES_GTD.5 transitions (start/stop events) from raw_data."""
    schema   = cfg_obj.db["schema"]
    table    = cfg_obj.data["table"]
    dt_col   = cfg_obj.data["datetime_col"]
    pt_col   = cfg_obj.data["point_col"]
    val_col  = cfg_obj.data["value_col"]
    sql = pgsql.SQL("""
        WITH ordered AS (
            SELECT {dt}, {pt}, {val},
                LAG({val}) OVER (PARTITION BY {pt} ORDER BY {dt}) AS prev_val
            FROM {schema}.{table}
            WHERE {pt} LIKE '%%.STATES_GTD.5'
              AND {dt} >= NOW() - make_interval(days => %(days)s)
        )
        SELECT COUNT(*) FROM ordered
        WHERE prev_val IS NOT NULL AND {val} <> prev_val
    """).format(
        dt=pgsql.Identifier(dt_col),
        pt=pgsql.Identifier(pt_col),
        val=pgsql.Identifier(val_col),
        schema=pgsql.Identifier(schema),
        table=pgsql.Identifier(table),
    )
    try:
        with get_db_connection(cfg_obj) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"days": int(days)})
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception:
        logger.exception("_count_regime_transitions failed")
        return 0


def _events_from_db(cfg: StationConfig, severity=None, gpa=None, kind=None,
                    days=None, limit=None) -> list[dict]:
    """Лента событий из anomalies_t (= таблица эпизодов; карточка = эпизод). Фильтры в SQL,
    время → локаль станции (Etc/GMT-5 naive). id детерминированный (как live) для ack-матча."""
    schema = cfg.db["schema"]
    clauses, params = [], {}
    if days is not None:
        clauses.append("event_ts >= NOW() - make_interval(days => %(days)s)"); params["days"] = int(days)
    if severity:
        clauses.append("severity = %(sev)s"); params["sev"] = severity
    if kind:
        clauses.append("kind = %(kind)s"); params["kind"] = kind
    if gpa:
        clauses.append("gpa ILIKE %(gpa)s"); params["gpa"] = gpa
    where = pgsql.SQL("".join(" AND " + c for c in clauses))
    params["lim"] = int(limit) if limit else 500
    sql = pgsql.SQL(
        "SELECT sensor_id, sensor_name, gpa, kind, severity, value, deviation, message, points,"
        " event_ts AT TIME ZONE 'Etc/GMT-5' AS ts_l, ts_end AT TIME ZONE 'Etc/GMT-5' AS tse_l"
        " FROM {s}.anomalies_t WHERE 1=1 {where} ORDER BY event_ts DESC LIMIT %(lim)s"
    ).format(s=pgsql.Identifier(schema), where=where)
    out: list[dict] = []
    with get_db_connection(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for sid, sname, g, knd, sev, val, dev, msg, pts, ts_l, tse_l in cur.fetchall():
                t_iso = ts_l.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(ts_l, "strftime") else str(ts_l)[:19]
                tse = tse_l.strftime("%Y-%m-%dT%H:%M:%S") if (tse_l is not None and hasattr(tse_l, "strftime")) else None
                out.append({
                    "id": f"{sid}__{knd}__{t_iso}", "timestamp": t_iso, "ts_end": tse,
                    "points": int(pts) if pts is not None else None,
                    "sensor_id": sid, "sensor_name": sname or sid, "gpa": g or "",
                    "kind": knd or "ml", "severity": sev or "info",
                    "value": float(val) if val is not None else None,
                    "deviation": float(dev) if dev is not None else None,
                    "description": msg or "",
                })
    return out


def _apply_event_filters(events: list[dict], severity, gpa, kind, days, limit) -> list[dict]:
    """Фильтры события (для state-пути; DB-путь фильтрует в SQL)."""
    if days is not None:
        now_local = datetime.now(timezone.utc).astimezone(_TZ5).replace(tzinfo=None)
        cutoff = (now_local - timedelta(days=days)).isoformat()
        events = [e for e in events if e.get("timestamp", "") >= cutoff]
    if severity:
        events = [e for e in events if e["severity"] == severity]
    if gpa:
        events = [e for e in events if e["gpa"].lower() == gpa.lower()]
    if kind:
        events = [e for e in events if e["kind"] == kind]
    if limit is not None:
        events = events[:limit]
    return events


def _kind_counts_from_db(cfg: StationConfig, days: int = 30) -> dict:
    """{kind: count} из anomalies_t за окно — для stats."""
    schema = cfg.db["schema"]
    kc = {k: 0 for k in ("ml", "frozen", "neg", "regime", "roc", "seasonal", "cross", "drift")}
    try:
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(pgsql.SQL(
                    "SELECT kind, count(*) FROM {s}.anomalies_t "
                    "WHERE event_ts >= NOW() - make_interval(days => %s) GROUP BY kind"
                ).format(s=pgsql.Identifier(schema)), (int(days),))
                for k, c in cur.fetchall():
                    if k in kc:
                        kc[k] = int(c)
    except Exception:
        logger.debug("kind_counts from db failed", exc_info=True)
    return kc


@app.get("/api/stations/{station_id}/stats", response_model=StatsResponse)
def station_stats(station_id: str, response: Response = None):
    cfg_obj = _require_station(station_id)
    if response:
        response.headers["Cache-Control"] = "public, max-age=25"
    state   = _get_live_state(station_id)
    sensors = _sensors_list(station_id)

    sev_counts  = {"crit": 0, "warn": 0, "info": 0, "ok": 0}
    for s in sensors:
        sev_counts[s["severity"]] = sev_counts.get(s["severity"], 0) + 1

    # Счётчики по типам. Источник — anomalies_t (turnkey без state.json); фолбэк на
    # state['events']. 'drift' учитываем (его эмитит live_predict, _MASK_TO_KIND).
    _src = _dashboard_source()
    _state_events = state.get("events", [])
    if _src == "db" or (_src == "auto" and not _state_events):
        kind_counts = _kind_counts_from_db(cfg_obj, days=30)
    else:
        kind_counts = {k: 0 for k in ("ml", "frozen", "neg", "regime", "roc", "seasonal", "cross", "drift")}
        for e in _state_events:
            k = e.get("kind", "")
            if k in kind_counts:
                kind_counts[k] += 1

    # Fallback: count STATES_GTD.5 transitions from raw_data when live_predict hasn't run
    if kind_counts["regime"] == 0:
        kind_counts["regime"] = _count_regime_transitions(cfg_obj, days=7)

    total_anomalies = sum(kind_counts.values())

    return StatsResponse(
        total_sensors=len(sensors),
        crit_count=sev_counts["crit"],
        warn_count=sev_counts["warn"],
        info_count=sev_counts["info"],
        ok_count=sev_counts["ok"],
        ml_count=kind_counts["ml"],
        frozen_count=kind_counts["frozen"],
        neg_count=kind_counts["neg"],
        regime_count=kind_counts["regime"],
        roc_count=kind_counts["roc"],
        seasonal_count=kind_counts["seasonal"],
        cross_count=kind_counts["cross"],
        drift_count=kind_counts["drift"],
        total_anomalies=total_anomalies,
        last_updated=state.get("last_updated") or pd.Timestamp.now().isoformat(),
    )


@app.get("/api/stations/{station_id}/events", response_model=list[EventItem])
def station_events(
    station_id: str,
    severity: Optional[str] = Query(None),
    gpa:      Optional[str] = Query(None),
    kind:     Optional[str] = Query(None),
    limit:    Optional[int] = Query(None),
    days:     Optional[int] = Query(None),
    response: Response = None,
):
    cfg = _require_station(station_id)
    if response:
        response.headers["Cache-Control"] = "public, max-age=25"
    state = _get_live_state(station_id)
    _src = _dashboard_source()
    _state_events = state.get("events", [])
    # Источник событий — anomalies_t (turnkey без state.json); фолбэк на state['events'].
    if _src == "db" or (_src == "auto" and not _state_events):
        try:
            events = _events_from_db(cfg, severity, gpa, kind, days, limit)
        except Exception:
            logger.debug("events from db failed → state-фолбэк", exc_info=True)
            events = _apply_event_filters(list(_state_events), severity, gpa, kind, days, limit)
    else:
        events = _apply_event_filters(list(_state_events), severity, gpa, kind, days, limit)
    # Серверный статус ack из журнала (cross-machine): помечаем событие acked, если для
    # его (sensor_id, момент) есть квитированная строка журнала. Best-effort (БД может
    # быть недоступна) — тогда остаётся локальный ack фронта.
    acked = _acked_event_epochs(cfg, station_id)
    if acked:
        out = []
        for e in events:
            ep = _event_epoch(e.get("timestamp", ""))
            if ep is not None and (e.get("sensor_id"), ep) in acked:
                e = {**e, "acked": True}
            out.append(e)
        events = out
    return [EventItem(**e) for e in events]


class AckEventBody(BaseModel):
    sensor_id: str
    timestamp: str           # EventItem.timestamp — локальная naive станции (Etc/GMT-5)
    kind: Optional[str] = None


@app.post("/api/stations/{station_id}/events/ack")
def ack_event(station_id: str, body: AckEventBody, status: str = Query("ack")):
    """Квитирование показываемого события (из live_state) → статус соответствующей строки
    журнала по детерминированному ключу (sensor_id, момент = UTC(timestamp)). Делает ack
    видимым всем операторам/машинам. Best-effort: 0 совпадений (нет журнальной записи) —
    НЕ ошибка, вернёт acked=0 (фронт сохраняет локальный оптимистичный ack)."""
    cfg = _require_station(station_id)
    ep = _event_epoch(body.timestamp)
    if ep is None:
        raise HTTPException(status_code=400, detail="Bad timestamp")
    try:
        schema = cfg.db["schema"]
        table  = journal_table_name()
        sql = pgsql.SQL(
            "UPDATE {schema}.{table} SET status = %(status)s "
            "WHERE station_id = %(sid)s AND sensor_id = %(sensor)s "
            "AND extract(epoch FROM event_ts)::bigint = %(ep)s"
        ).format(schema=pgsql.Identifier(schema), table=pgsql.Identifier(table))
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"status": status, "sid": station_id,
                                  "sensor": body.sensor_id, "ep": ep})
                updated = cur.rowcount
            conn.commit()
        return {"acked": int(updated), "status": status}
    except Exception:
        logger.exception("ack_event failed")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")


CHART_TARGET_POINTS = 1500


def _chart_bucket_seconds(range_s: float) -> int:
    """Адаптивный бакет: <=CHART_TARGET_POINTS точек, кратен 5 мин (сетке данных)."""
    import math
    return max(300, math.ceil(range_s / CHART_TARGET_POINTS / 300) * 300)


def _fetch_raw_db_series(
    cfg_obj: StationConfig,
    tag: str,
    days: Optional[int] = None,
    t0: Optional[datetime] = None,
    t1: Optional[datetime] = None,
) -> tuple[list[dict], int]:
    """Bucket-агрегированная серия из raw_data: avg(value) по адаптивному бакету.

    Даунсемплинг выполняется в БД (GROUP BY) — по сети уходит <=1500 строк.
    days-режим использует DB-side NOW() (защита от clock skew);
    t0/t1 — aware datetime (UTC) для произвольного окна.
    Возвращает (points, bucket_s).
    """
    schema  = cfg_obj.db["schema"]
    table   = cfg_obj.data["table"]
    dt_col  = cfg_obj.data["datetime_col"]
    pt_col  = cfg_obj.data["point_col"]
    val_col = cfg_obj.data["value_col"]

    if t0 is not None and t1 is not None:
        range_s = max((t1 - t0).total_seconds(), 300.0)
        bucket_s = _chart_bucket_seconds(range_s)
        params: dict = {"tag": tag, "bucket": bucket_s, "t0": t0, "t1": t1,
                        "row_limit": 50000}
        where_time = pgsql.SQL("AND {dt} >= %(t0)s AND {dt} < %(t1)s").format(
            dt=pgsql.Identifier(dt_col))
    else:
        eff_days = int(days) if days else 30
        bucket_s = _chart_bucket_seconds(eff_days * 86400)
        params = {"tag": tag, "bucket": bucket_s, "days": eff_days,
                  "row_limit": 50000}
        where_time = pgsql.SQL("AND {dt} >= NOW() - make_interval(days => %(days)s)").format(
            dt=pgsql.Identifier(dt_col))

    sql = pgsql.SQL(
        "SELECT to_timestamp(floor(extract(epoch FROM {dt}) / %(bucket)s) * %(bucket)s) AS ts,"
        "       avg({val}) AS v"
        " FROM {schema}.{table}"
        " WHERE {pt} = %(tag)s {where_time}"
        " GROUP BY 1 ORDER BY 1 LIMIT %(row_limit)s"
    ).format(
        dt=pgsql.Identifier(dt_col),
        val=pgsql.Identifier(val_col),
        schema=pgsql.Identifier(schema),
        table=pgsql.Identifier(table),
        pt=pgsql.Identifier(pt_col),
        where_time=where_time,
    )
    try:
        with get_db_connection(cfg_obj) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except Exception:
        logger.exception("_fetch_raw_db_series failed for tag=%r", tag)
        return [], bucket_s

    if not rows:
        return [], bucket_s

    df = pd.DataFrame(rows, columns=["ts", "val"])
    df["ts"]  = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Etc/GMT-5").dt.tz_localize(None)
    df["val"] = pd.to_numeric(df["val"], errors="coerce")
    df = df.dropna(subset=["val"]).sort_values("ts")

    return (
        [{"t": r.ts.strftime("%Y-%m-%dT%H:%M:%S"), "v": round(float(r.val), 4)}
         for r in df.itertuples()],
        bucket_s,
    )


def _dashboard_source() -> str:
    """Источник данных дашборда: 'db' | 'state' | 'auto' (default). auto: БД, фолбэк state."""
    import os
    return (os.environ.get("CS_DASHBOARD_SOURCE", "auto") or "auto").strip().lower()


def _fetch_pred_db_series(
    cfg_obj: StationConfig, sensor_id: str,
    days: Optional[int] = None, t0: Optional[datetime] = None,
    t1: Optional[datetime] = None, bucket_s: int = 300,
) -> dict:
    """Серия прогноза/коридора из {schema}.predictions, бакет-агрегация в БД:
    p=avg, lo=min, hi=max (коридор не сужается). Те же бакет-границы и TZ-конверсия,
    что _fetch_raw_db_series → ключи совпадают с raw_pts['t']. Возвращает
    {t_iso_local: {p,lo,hi}}; {} при отсутствии данных/таблицы (→ фолбэк на state)."""
    schema = cfg_obj.db["schema"]
    if t0 is not None and t1 is not None:
        params: dict = {"sid": sensor_id, "bucket": bucket_s, "t0": t0, "t1": t1, "row_limit": 50000}
        where_time = pgsql.SQL("AND datetime >= %(t0)s AND datetime < %(t1)s")
    else:
        eff_days = int(days) if days else 30
        params = {"sid": sensor_id, "bucket": bucket_s, "days": eff_days, "row_limit": 50000}
        where_time = pgsql.SQL("AND datetime >= NOW() - make_interval(days => %(days)s)")
    # e (эпистемика u_epi) — avg по бакету, как p; e_thr (порог, ~константа) — max по бакету.
    # lo2/hi2 — альтернативный коридор (hybrid) для UI-тумблера: min/max, как lo/hi (не сужаем).
    sql = pgsql.SQL(
        "SELECT to_timestamp(floor(extract(epoch FROM datetime) / %(bucket)s) * %(bucket)s) AS ts,"
        "       avg(prediction) AS p, min(lo) AS lo, max(hi) AS hi,"
        "       min(lo2) AS lo2, max(hi2) AS hi2,"
        "       avg(e) AS e, max(e_thr) AS e_thr"
        " FROM {schema}.predictions"
        " WHERE sensor_id = %(sid)s {where_time}"
        " GROUP BY 1 ORDER BY 1 LIMIT %(row_limit)s"
    ).format(schema=pgsql.Identifier(schema), where_time=where_time)
    try:
        with get_db_connection(cfg_obj) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except Exception:
        logger.debug("_fetch_pred_db_series failed for %r", sensor_id, exc_info=True)
        return {}
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["ts", "p", "lo", "hi", "lo2", "hi2", "e", "e_thr"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Etc/GMT-5").dt.tz_localize(None)
    out: dict = {}
    for r in df.itertuples():
        out[r.ts.strftime("%Y-%m-%dT%H:%M:%S")] = {
            "p":  None if pd.isna(r.p)  else round(float(r.p), 4),
            "lo": None if pd.isna(r.lo) else round(float(r.lo), 4),
            "hi": None if pd.isna(r.hi) else round(float(r.hi), 4),
            "lo2": None if pd.isna(r.lo2) else round(float(r.lo2), 4),
            "hi2": None if pd.isna(r.hi2) else round(float(r.hi2), 4),
            "e":  None if pd.isna(r.e)  else round(float(r.e), 6),
            "e_thr": None if pd.isna(r.e_thr) else round(float(r.e_thr), 6),
        }
    return out


def _fetch_raw_db_multi(
    cfg_obj: StationConfig, tags: list[str],
    days: Optional[int] = None, t0: Optional[datetime] = None, t1: Optional[datetime] = None,
) -> dict:
    """Батч-выборка факта для нескольких тегов ОДНИМ запросом (point = ANY + GROUP BY
    point,bucket) — вместо N отдельных _fetch_raw_db_series в /chart/multi (N round-trip'ов).
    Возвращает {tag: [{t,v}]} (t — local-naive ISO, как _fetch_raw_db_series)."""
    if not tags:
        return {}
    schema  = cfg_obj.db["schema"]; table = cfg_obj.data["table"]
    dt_col  = cfg_obj.data["datetime_col"]; pt_col = cfg_obj.data["point_col"]; val_col = cfg_obj.data["value_col"]
    if t0 is not None and t1 is not None:
        bucket_s = _chart_bucket_seconds(max((t1 - t0).total_seconds(), 300.0))
        params: dict = {"tags": list(tags), "bucket": bucket_s, "t0": t0, "t1": t1,
                        "row_limit": 50000 * max(1, len(tags))}
        where_time = pgsql.SQL("AND {dt} >= %(t0)s AND {dt} < %(t1)s").format(dt=pgsql.Identifier(dt_col))
    else:
        eff_days = int(days) if days else 30
        bucket_s = _chart_bucket_seconds(eff_days * 86400)
        params = {"tags": list(tags), "bucket": bucket_s, "days": eff_days,
                  "row_limit": 50000 * max(1, len(tags))}
        where_time = pgsql.SQL("AND {dt} >= NOW() - make_interval(days => %(days)s)").format(dt=pgsql.Identifier(dt_col))
    sql = pgsql.SQL(
        "SELECT {pt} AS p,"
        " to_timestamp(floor(extract(epoch FROM {dt}) / %(bucket)s) * %(bucket)s) AS ts,"
        " avg({val}) AS v"
        " FROM {schema}.{table} WHERE {pt} = ANY(%(tags)s) {where_time}"
        " GROUP BY {pt}, 2 ORDER BY {pt}, 2 LIMIT %(row_limit)s"
    ).format(dt=pgsql.Identifier(dt_col), val=pgsql.Identifier(val_col),
             schema=pgsql.Identifier(schema), table=pgsql.Identifier(table),
             pt=pgsql.Identifier(pt_col), where_time=where_time)
    try:
        with get_db_connection(cfg_obj) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except Exception:
        logger.exception("_fetch_raw_db_multi failed (%d tags)", len(tags))
        return {}
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["p", "ts", "v"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("Etc/GMT-5").dt.tz_localize(None)
    df["v"] = pd.to_numeric(df["v"], errors="coerce")
    df = df.dropna(subset=["v"]).sort_values(["p", "ts"])
    out: dict = {}
    for tag, grp in df.groupby("p", sort=False):
        out[tag] = [{"t": r.ts.strftime("%Y-%m-%dT%H:%M:%S"), "v": round(float(r.v), 4)} for r in grp.itertuples()]
    return out


_TZ5 = timezone(timedelta(hours=5))   # Etc/GMT-5 — зона naive-времён фронта и state


def _event_epoch(ts_local: str) -> Optional[int]:
    """Epoch-секунды (UTC) из локальной naive-метки станции (Etc/GMT-5). None при мусоре.
    Тот же момент, что live_predict пишет в журнал (event_ts UTC) → ключ для матча ack."""
    try:
        return int(datetime.fromisoformat(str(ts_local)).replace(tzinfo=_TZ5).timestamp())
    except Exception:
        return None


def _acked_event_epochs(cfg: StationConfig, station_id: str) -> set:
    """Множество (sensor_id, epoch_сек UTC) квитированных строк журнала — чтобы пометить
    показываемые события acked СЕРВЕРНО (видно всем операторам/машинам). Best-effort:
    при недоступной БД возвращает пустое множество (фид /events не ломается, остаётся
    локальный ack фронта)."""
    try:
        schema = cfg.db["schema"]
        table = journal_table_name()
        sql = pgsql.SQL(
            "SELECT sensor_id, extract(epoch FROM event_ts)::bigint AS ep "
            "FROM {schema}.{table} WHERE station_id = %(sid)s AND status <> 'new'"
        ).format(schema=pgsql.Identifier(schema), table=pgsql.Identifier(table))
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"sid": station_id})
                rows = cur.fetchall()
        return {(r[0], int(r[1])) for r in rows}
    except Exception:
        logger.debug("acked epochs lookup failed (non-fatal)", exc_info=True)
        return set()


def _parse_chart_ts(value: str, name: str) -> datetime:
    """ISO-строка (naive = Etc/GMT-5) -> aware UTC datetime; 422 при мусоре."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {name}: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ5)
    return dt.astimezone(timezone.utc)


def _resample_state_series(raw_series_state: list[dict], bucket_s: int) -> dict:
    """Ресемплинг 5-мин state-серии (p/lo/hi) в бакеты: p=avg, lo=min, hi=max
    (коридор не сужается). Ключ — int(epoch // bucket_s)."""
    agg: dict[int, dict] = {}
    for p in raw_series_state:
        try:
            k = int(datetime.fromisoformat(p["t"]).replace(tzinfo=_TZ5).timestamp() // bucket_s)
        except (ValueError, KeyError):
            continue
        a = agg.setdefault(k, {"p_sum": 0.0, "p_n": 0, "lo": None, "hi": None})
        if p.get("p") is not None:
            a["p_sum"] += p["p"]; a["p_n"] += 1
        if p.get("lo") is not None:
            a["lo"] = p["lo"] if a["lo"] is None else min(a["lo"], p["lo"])
        if p.get("hi") is not None:
            a["hi"] = p["hi"] if a["hi"] is None else max(a["hi"], p["hi"])
    return {
        k: {"p": round(a["p_sum"] / a["p_n"], 4) if a["p_n"] else None,
            "lo": a["lo"], "hi": a["hi"]}
        for k, a in agg.items()
    }


@app.get("/api/stations/{station_id}/sensors/{sensor_id}/chart", response_model=SensorChartResponse)
def station_sensor_chart(
    station_id: str,
    sensor_id: str,
    days: int = Query(0, ge=0),
    t0: Optional[str] = Query(None),
    t1: Optional[str] = Query(None),
    response: Response = None,
):
    cfg_obj = _require_station(station_id)
    sensors = {s["id"]: s for s in _sensors_list(station_id)}
    if sensor_id not in sensors:
        raise HTTPException(status_code=404, detail="Sensor not found")

    info  = sensors[sensor_id]
    state = _get_live_state(station_id)
    raw_series_state = state.get("series", {}).get(sensor_id, [])

    # Произвольное окно (зум/кастомный период) или days-режим (DB-side NOW(),
    # защита от clock skew). Даунсемплинг — в БД, <=1500 точек на любой диапазон.
    t0_dt = _parse_chart_ts(t0, "t0") if t0 else None
    t1_dt = _parse_chart_ts(t1, "t1") if t1 else None
    if t0_dt and not t1_dt:
        t1_dt = datetime.now(timezone.utc)
    if t0_dt and t1_dt and t0_dt >= t1_dt:
        raise HTTPException(status_code=422, detail="t0 must be before t1")

    effective_days = days if days > 0 else 30
    raw_pts, bucket_s = _fetch_raw_db_series(
        cfg_obj, info["tag"],
        days=None if t0_dt else effective_days,
        t0=t0_dt, t1=t1_dt,
    )

    # Фолбэк для РАСЧЁТНЫХ индексов (polytropic_head/η_p/shaft/… — нет сырого тега в
    # raw_data, tag='N/A' → выборка по тегу пуста). Берём ряд из state.json (там есть
    # вычисленные v/p/lo/hi), фильтруем по окну. Так график/датпикер работают и для них.
    if not raw_pts and raw_series_state:
        pts = raw_series_state
        if t0_dt or t1_dt:
            lo_s = t0_dt.astimezone(_TZ5).strftime("%Y-%m-%dT%H:%M:%S") if t0_dt else ""
            hi_s = t1_dt.astimezone(_TZ5).strftime("%Y-%m-%dT%H:%M:%S") if t1_dt else "9999"
            pts = [p for p in pts if lo_s <= p["t"] <= hi_s]
        elif pts:
            cutoff = (datetime.fromisoformat(pts[-1]["t"]) - timedelta(days=effective_days)).isoformat()
            pts = [p for p in pts if p["t"] >= cutoff]
        # ограничим payload (state — поминутный ряд до 30 дн)
        if len(pts) > 2000:
            stride = len(pts) // 2000 + 1
            pts = pts[::stride]
        raw_pts = [{"t": p["t"], "v": p["v"]} for p in pts]
        bucket_s = 60   # поминутно: _state_for найдёт p/lo/hi по минуте

    # Кеш: исторические окна иммутабельны (raw_data append-only)
    if response is not None:
        is_historical = t1_dt is not None and \
            t1_dt < datetime.now(timezone.utc) - timedelta(hours=1)
        response.headers["Cache-Control"] = \
            "public, max-age=3600" if is_historical else "public, max-age=25"

    # p/lo/hi (прогноз+коридор): источник — БД predictions (turnkey-развёртывание без
    # локального state.json); фолбэк на state-JSON (переходный период / CS_DASHBOARD_SOURCE=state).
    _src = _dashboard_source()
    pred_map: dict = {}
    if _src in ("db", "auto"):
        pred_map = _fetch_pred_db_series(
            cfg_obj, sensor_id,
            days=None if t0_dt else effective_days, t0=t0_dt, t1=t1_dt, bucket_s=bucket_s)
    if pred_map:
        def _state_for(pt_t: str) -> dict:
            return pred_map.get(pt_t, {})
    elif _src == "db":
        def _state_for(pt_t: str) -> dict:      # db-only: серии ещё нет в БД
            return {}
    elif bucket_s > 300:
        bucket_map = _resample_state_series(raw_series_state, bucket_s)

        def _state_for(pt_t: str) -> dict:
            k = int(datetime.fromisoformat(pt_t).replace(tzinfo=_TZ5).timestamp() // bucket_s)
            return bucket_map.get(k, {})
    else:
        minute_map = {p["t"][:16]: p for p in raw_series_state}

        def _state_for(pt_t: str) -> dict:
            return minute_map.get(pt_t[:16], {})

    # Граница обучения: на обучающем периоде (t <= train_ts) прогноз = in-sample,
    # неинформативен → не отдаём p/lo/hi (только факт). Это и уменьшает payload.
    train_ts_iso: Optional[str] = None
    meta = _get_metadata(station_id)
    ltt = meta.get("last_train_timestamp")
    if ltt:
        try:
            train_ts_iso = pd.to_datetime(ltt, utc=True).tz_convert("Etc/GMT-5") \
                             .tz_localize(None).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            train_ts_iso = None

    # Эпистемика (u_epi) + альтернативный коридор (lo2/hi2, hybrid): БД predictions хранит их
    # (после reprocess); фолбэк на live-state.series по минуте (переходный период / db ещё нет).
    state_map = {p["t"][:16]: p for p in raw_series_state} if raw_series_state else {}

    EPS_REL = 0.005   # коридор уже 0.5% значения — пренебрежимо узкий, не шлём lo/hi
    series = []
    db_e_thr = None   # порог эпистемики из БД (последний непустой) — приоритетнее state
    for pt in raw_pts:
        # fail-closed: если границу обучения определить не удалось (train_ts_iso=None),
        # НЕ отдаём прогноз/коридор (иначе утечка in-sample прогноза на обучающий период).
        _post_train = (train_ts_iso is not None and pt["t"] > train_ts_iso)
        st = _state_for(pt["t"]) if _post_train else {}
        # fail-closed: state-фолбэк ТОЖЕ гейтим train-границей (state.series пишется без маски по
        # train — иначе in-sample lo2/hi2/e протекут на обучающий период мимо гейта st).
        sfb = state_map.get(pt["t"][:16], {}) if _post_train else {}
        p, lo, hi = st.get("p"), st.get("lo"), st.get("hi")
        if p is not None and lo is not None and hi is not None:
            if max(abs(hi - p), abs(p - lo)) <= max(abs(p) * EPS_REL, 1e-6):
                lo = hi = None    # коридор пренебрежимо узкий → экономим payload
        # альт.коридор (hybrid): приоритет БД, фолбэк state; EPS_REL НЕ применяем — hybrid
        # надёжный универсальный, показываем везде, где посчитан (в т.ч. на OOD, где он расширен).
        lo2, hi2 = st.get("lo2"), st.get("hi2")
        if lo2 is None and hi2 is None:
            lo2, hi2 = sfb.get("lo2"), sfb.get("hi2")
        # эпистемика: приоритет БД (predictions.e — по всей истории, turnkey), фолбэк state.
        e_val = st.get("e")
        if e_val is None:
            e_val = sfb.get("e")
        if st.get("e_thr") is not None:
            db_e_thr = st["e_thr"]
        series.append(TimeSeriesPoint(t=pt["t"], v=pt["v"], p=p, lo=lo, hi=hi, lo2=lo2, hi2=hi2, e=e_val))

    def _nt(ts: str) -> str:
        return ts[:16].replace("T", " ")

    anomalies: list[dict] = []
    if series:
        series_ts_map = {_nt(p.t): p for p in series}
        try:
            schema = cfg_obj.db["schema"]
            # Маркеры — из anomalies_t (проектная таблица: ЭПИЗОДЫ, без легаси-бэкфилла
            # 11.06 → 1 маркер = 1 эпизод, чисто по построению). event_ts в UTC (TIMESTAMPTZ);
            # границы передаём как aware-метки зоны станции (Etc/GMT-5=UTC+5), вывод в той же
            # зоне → маркеры на той же оси X, что и серия (независимо от session TZ сервера БД).
            sql = pgsql.SQL("""
                SELECT event_ts AT TIME ZONE 'Etc/GMT-5' AS ts, anomaly_type, severity, value
                FROM {schema}.anomalies_t
                WHERE sensor_id = %(sid)s
                  AND event_ts >= %(t0)s AND event_ts <= %(t1)s
                ORDER BY event_ts
            """).format(schema=pgsql.Identifier(schema))
            q_t0 = datetime.fromisoformat(series[0].t).replace(tzinfo=_TZ5) - timedelta(minutes=5)
            q_t1 = datetime.fromisoformat(series[-1].t).replace(tzinfo=_TZ5) + timedelta(minutes=5)
            with get_db_connection(cfg_obj) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, {"sid": sensor_id, "t0": q_t0, "t1": q_t1})
                    rows = cur.fetchall()
            for ts_dt, atype, sev, val in rows:
                ts_str = ts_dt.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(ts_dt, "strftime") else str(ts_dt)[:19]
                ts_key = _nt(ts_str)
                sp = series_ts_map.get(ts_key)
                anomalies.append({
                    "t":        sp.t if sp else ts_str,
                    "v":        val if val is not None else (sp.v if sp else 0.0),
                    "kind":     CODE_TO_KIND.get(atype, "ml"),
                    "severity": sev or _KIND_SEV.get(CODE_TO_KIND.get(atype, "ml"), "crit"),
                })
        except Exception:
            chart_anoms = state.get("chart_anomalies", {}).get(sensor_id)
            if chart_anoms is not None:
                series_ts = set(series_ts_map)
                anomalies = [a for a in chart_anoms if _nt(a["t"]) in series_ts]

    cur_v  = series[-1].v if series else None
    pred_v = series[-1].p if series else None
    if cur_v is not None and pred_v is not None and abs(pred_v) > 1e-9:
        dev: Optional[float] = round((cur_v - pred_v) / abs(pred_v) * 100, 2)
    else:
        dev = None

    epi_thr = db_e_thr
    if epi_thr is None:
        epi_thr = (state.get("sensors", {}).get(sensor_id) or {}).get("epistemic_thr")

    # какой режим = активный коридор (lo/hi); альтернативный (lo2/hi2) = противоположный.
    # ИСТОЧНИК обязан совпадать с live_predict (иначе conf/hyb перепутаны местами во фронте):
    # env CS_CORRIDOR_MODE > cfg.methodology.corridor_mode > metadata > 'conformal'.
    import os as _os
    corridor_mode = (_os.environ.get("CS_CORRIDOR_MODE")
                     or (getattr(cfg_obj, "methodology", None) or {}).get("corridor_mode")
                     or meta.get("corridor_mode") or "conformal").lower()
    # self-conformal датчики (univariate_band): один коридор = нормальный диапазон по режиму,
    # НЕ conformal/hybrid. Помечаем режим 'self' → фронт подпишет отдельно и скроет тумблер.
    if info.get("detector_mode") == "univariate_band":
        corridor_mode = "self"

    return SensorChartResponse(
        sensor_id=sensor_id, tag=info["tag"], r2=info["r2"], mae=info["mae"],
        current=cur_v, predicted=pred_v, deviation=dev, train_ts=train_ts_iso,
        series=series, anomalies=anomalies, epistemic_thr=epi_thr, corridor_mode=corridor_mode,
    )


# ── «Важные признаки»: локальная атрибуция аномалии (SHAP) ──────────────────────
class ExplainContributor(BaseModel):
    name: str
    contrib: float
    series: list[dict]   # [{t, v}] ряд параметра-вкладчика вокруг события


class SensorExplainResponse(BaseModel):
    sensor_id: str
    event_ts: str
    actual: Optional[float] = None
    expected: Optional[float] = None
    contributors: list[ExplainContributor]
    # ряд самого датчика на окне (для наложения «цель vs драйверы» в region-SHAP)
    target_series: Optional[list[dict]] = None
    # выбранный участок (для подсветки на графике вкладчиков)
    region: Optional[dict] = None


def _explain_persisted(cfg, sensor_id: str, t_local, dfw, gsuf: str, hours: int):
    """Читает сохранённый SHAP из anomalies_t (ближайшая аномалия к t_local, ±30 мин)
    и строит ответ: вкладчики из persisted shap_top, их ряды — из свежего dfw. Это
    атрибуция «как на момент тревоги» (переживает переобучение). None — если записи нет."""
    try:
        schema = cfg.db["schema"]
        # t_local (Etc/GMT-5 naive) -> aware UTC для запроса по TIMESTAMPTZ
        t_utc = pd.Timestamp(t_local).tz_localize("Etc/GMT-5").tz_convert("UTC").to_pydatetime()
        q0 = t_utc - timedelta(minutes=30)
        q1 = t_utc + timedelta(minutes=30)
        sql = pgsql.SQL("""
            SELECT event_ts AT TIME ZONE 'Etc/GMT-5' AS ev_local, value, expected, shap_top
            FROM {schema}.anomalies_t
            WHERE sensor_id = %(sid)s AND shap_top IS NOT NULL
              AND event_ts >= %(t0)s AND event_ts <= %(t1)s
            ORDER BY abs(extract(epoch FROM (event_ts - %(tc)s)))
            LIMIT 1
        """).format(schema=pgsql.Identifier(schema))
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"sid": sensor_id, "t0": q0, "t1": q1, "tc": t_utc})
                row = cur.fetchone()
        if not row:
            return None
        ev_local, value, expected, shap_top = row
        if not shap_top or not isinstance(shap_top, list):
            return None        # пусто или повреждённый jsonb (не массив) → fallback on-demand
        ev_ts = pd.Timestamp(ev_local)          # naive local
        win0 = (ev_ts - pd.Timedelta(hours=hours)).to_pydatetime()
        win1 = (ev_ts + pd.Timedelta(hours=1)).to_pydatetime()
        out = []
        for c in list(shap_top)[:5]:
            fname = c.get("name")
            if not fname:
                continue
            col = fname if fname in dfw.columns else (fname + gsuf if (fname + gsuf) in dfw.columns else None)
            ser = []
            if col is not None:
                sub = dfw[col].loc[win0:win1].dropna()
                ser = [{"t": pd.Timestamp(ix).isoformat(), "v": round(float(v), 4)}
                       for ix, v in sub.items()][-400:]
            out.append(ExplainContributor(name=fname,
                                          contrib=round(float(c.get("contrib", 0.0)), 5),
                                          series=ser))
        if not out:
            return None
        return SensorExplainResponse(
            sensor_id=sensor_id, event_ts=ev_ts.isoformat(),
            actual=(round(float(value), 4) if value is not None else None),
            expected=(round(float(expected), 4) if expected is not None else None),
            contributors=out)
    except Exception:
        logger.debug("explain from anomalies_t failed", exc_info=True)
        return None


def _explain_region(cfg, meta, info, sensor_id, t0, t1, hours, LP, Pool, v0=None, v1=None):
    """Region SHAP: средний |вклад| признаков по строкам участка → top-5 драйверов
    расчётного значения + их ряды (контекст ±1ч). Участок задаётся по времени [t0,t1]
    и (опционально) по диапазону значений [v0,v1] датчика (вертикальное выделение).
    Знак вклада — средний по участку. Всегда on-demand под текущей моделью."""
    a = pd.Timestamp(t0); b = pd.Timestamp(t1)
    a_loc = a.tz_convert("Etc/GMT-5").tz_localize(None) if a.tzinfo is not None else a
    b_loc = b.tz_convert("Etc/GMT-5").tz_localize(None) if b.tzinfo is not None else b
    if b_loc < a_loc:
        a_loc, b_loc = b_loc, a_loc
    vlo = min(v0, v1) if (v0 is not None and v1 is not None) else None
    vhi = max(v0, v1) if (v0 is not None and v1 is not None) else None
    gsuf = "__GPA" + sensor_id.rsplit("__GPA", 1)[-1]
    n2t = meta.get("name_to_tag") or {}
    gpa_points = [tag for nm, tag in n2t.items() if str(nm).endswith(gsuf)] or None
    ctx = pd.Timedelta(hours=1)
    raw = LP.fetch_data_from_db(since_timestamp=(a_loc - ctx).isoformat(),
                                until_timestamp=(b_loc + ctx).isoformat(), points=gpa_points)
    dfw = LP.prepare_wide_data(raw, meta["tag_to_name"])
    if dfw.empty or sensor_id not in dfw.columns:
        raise HTTPException(status_code=503, detail="no data for region")

    wrapper = joblib.load(str(cfg.models_path / info["model_file"]))
    mdl = wrapper["model"]
    feat_raw = list(wrapper.get("feat_cols") or info.get("feat_cols", []))
    resolved = {}
    for f in feat_raw:
        col = f if f in dfw.columns else (f + gsuf if (f + gsuf) in dfw.columns else None)
        if col is None:
            raise HTTPException(status_code=503, detail="features unavailable")
        resolved[f] = col
    Xdf = dfw[list(resolved.values())].rename(columns={v: k for k, v in resolved.items()})[feat_raw]

    in_reg = (dfw.index >= a_loc) & (dfw.index <= b_loc)
    # вертикальное выделение: ограничиваем строки диапазоном значений датчика [vlo,vhi]
    if vlo is not None and vhi is not None and sensor_id in dfw.columns:
        sv_col = dfw[sensor_id]
        in_reg = in_reg & (sv_col >= vlo) & (sv_col <= vhi)
    Xreg = Xdf[in_reg]
    if Xreg.empty:        # участок уже сетки — берём ближайшую строку к центру
        pos = int(dfw.index.get_indexer([a_loc + (b_loc - a_loc) / 2], method="nearest")[0])
        Xreg = Xdf.iloc[[max(pos, 0)]]
    if len(Xreg) > 1500:  # подвыборка для скорости SHAP на длинном участке
        Xreg = Xreg.iloc[:: int(np.ceil(len(Xreg) / 1500))]
    if wrapper.get("needs_impute"):
        Xreg = Xreg.fillna(pd.Series(wrapper.get("impute_median", {}) or {})).fillna(0.0)

    sv = np.asarray(mdl.get_feature_importance(Pool(Xreg), type="ShapValues"))
    if sv.ndim == 1:      # одиночная строка вернулась 1D → приводим к (1, F+1)
        sv = sv.reshape(1, -1)
    if sv.ndim == 3:      # (n, outputs, F+1) → выход mean
        sv = sv[:, 0, :]
    contrib_abs = np.mean(np.abs(sv[:, :-1]), axis=0)     # средний |вклад| — ранжирование
    contrib_signed = np.mean(sv[:, :-1], axis=0)          # средний знак — для подписи
    order = np.argsort(contrib_abs)[::-1][:5]

    win0 = (a_loc - ctx).to_pydatetime(); win1 = (b_loc + ctx).to_pydatetime()
    out = []
    for i in order:
        fname = feat_raw[int(i)]
        sub = dfw[resolved[fname]].loc[win0:win1].dropna()
        ser = [{"t": pd.Timestamp(ix).isoformat(), "v": round(float(v), 4)} for ix, v in sub.items()][-400:]
        out.append(ExplainContributor(name=fname, contrib=round(float(contrib_signed[int(i)]), 5), series=ser))

    reg_real = dfw[sensor_id].loc[a_loc:b_loc].dropna()
    _predreg = np.asarray(mdl.predict(Xreg), float)
    pmean = float(np.mean(_predreg[:, 0] if _predreg.ndim == 2 else _predreg))
    # ряд самого датчика на окне (с контекстом) — для наложения «цель vs драйверы»
    tgt_sub = dfw[sensor_id].loc[win0:win1].dropna()
    tgt_series = [{"t": pd.Timestamp(ix).isoformat(), "v": round(float(v), 4)} for ix, v in tgt_sub.items()][-400:]
    return SensorExplainResponse(
        sensor_id=sensor_id, event_ts=a_loc.isoformat(),
        actual=(round(float(reg_real.mean()), 4) if not reg_real.empty else None),
        expected=round(pmean, 4), contributors=out,
        target_series=tgt_series,
        region={"t0": a_loc.isoformat(), "t1": b_loc.isoformat(),
                "v0": (round(float(vlo), 4) if vlo is not None else None),
                "v1": (round(float(vhi), 4) if vhi is not None else None)})


@app.get("/api/stations/{station_id}/sensors/{sensor_id}/explain",
         response_model=SensorExplainResponse)
def sensor_explain(station_id: str, sensor_id: str,
                   t: str = Query(None, description="event_ts аномалии (ISO, локальное Etc/GMT-5)"),
                   t0: str = Query(None, description="начало участка (region SHAP)"),
                   t1: str = Query(None, description="конец участка (region SHAP)"),
                   v0: float = Query(None, description="нижняя граница значений (вертик. выделение)"),
                   v1: float = Query(None, description="верхняя граница значений (вертик. выделение)"),
                   hours: int = Query(6, ge=1, le=72)):
    """Топ параметров-вкладчиков через SHAP CatBoost. Два режима:
    • одиночная аномалия (t) — SHAP на строке события (persisted из anomalies_t или on-demand);
    • участок (t0..t1) — средний |SHAP| по всем строкам окна → top-5 вкладчиков в расчёт.
    + ряды вкладчиков за окно. Источник «Важных признаков». Требует БД и CatBoost-модели."""
    cfg = _require_station(station_id)
    meta = _get_metadata(station_id)
    info = (meta.get("models") or {}).get(sensor_id)
    if not info:
        raise HTTPException(status_code=404, detail="sensor not modelled")
    try:
        import live_predict as LP
        from catboost import Pool
        LP._init_station(station_id)

        # ── РЕЖИМ УЧАСТКА (region SHAP): t0..t1 заданы → средний |SHAP| по окну ──
        if t0 and t1:
            return _explain_region(cfg, meta, info, sensor_id, t0, t1, hours, LP, Pool, v0=v0, v1=v1)
        if not t:
            raise HTTPException(status_code=422, detail="требуется t либо (t0,t1)")

        t_ts = pd.Timestamp(t)
        # привести метку к naive-локали (Etc/GMT-5) — ось X графика и индекса dfw
        t_local = t_ts.tz_convert("Etc/GMT-5").tz_localize(None) if t_ts.tzinfo is not None else t_ts
        gsuf = "__GPA" + sensor_id.rsplit("__GPA", 1)[-1]
        # только сырые теги нужного ГПА — полный набор входов для фич и доменных
        # индексов этого датчика; включает индекс (point, datetime) на сервере и режет
        # трафик ~3×. ambient_temp приходит из погоды (prepare_wide_data), не из raw.
        n2t = meta.get("name_to_tag") or {}
        gpa_points = [tag for nm, tag in n2t.items() if str(nm).endswith(gsuf)] or None
        # окно [t-hours, t+1ч] — ограничиваем СВЕРХУ, иначе для старой аномалии
        # подтянулись бы все данные от since до сейчас (дни вместо часов → таймаут)
        since = (t_local - pd.Timedelta(hours=hours)).isoformat()
        until = (t_local + pd.Timedelta(hours=1)).isoformat()
        raw = LP.fetch_data_from_db(since_timestamp=since, until_timestamp=until, points=gpa_points)
        dfw = LP.prepare_wide_data(raw, meta["tag_to_name"])
        if dfw.empty or sensor_id not in dfw.columns:
            raise HTTPException(status_code=503, detail="no data for window")

        # 1) Персистнутый SHAP из anomalies_t (атрибуция «как при тревоге», стабильна
        #    после переобучения). Ряды строим из свежего dfw.
        persisted = _explain_persisted(cfg, sensor_id, t_local, dfw, gsuf, hours)
        if persisted is not None:
            return persisted

        # 2) Фолбэк: считаем SHAP on-demand под текущей моделью.
        wrapper = joblib.load(str(cfg.models_path / info["model_file"]))
        mdl = wrapper["model"]
        feat_raw = list(wrapper.get("feat_cols") or info.get("feat_cols", []))
        resolved = {}
        for f in feat_raw:
            col = f if f in dfw.columns else (f + gsuf if (f + gsuf) in dfw.columns else None)
            if col is None:
                raise HTTPException(status_code=503, detail="features unavailable")
            resolved[f] = col
        Xdf = dfw[list(resolved.values())].rename(columns={v: k for k, v in resolved.items()})[feat_raw]

        pos = int(dfw.index.get_indexer([t_local], method="nearest")[0])
        if pos < 0:        # пустой индекс/нет совпадения — данных в окне нет
            raise HTTPException(status_code=503, detail="timestamp not in data window")
        ev_t = dfw.index[pos]
        Xrow = Xdf.iloc[[pos]]

        sv = np.asarray(mdl.get_feature_importance(Pool(Xrow), type="ShapValues"))
        row = sv[0]
        if row.ndim == 2:        # multi-output (uncertainty): берём выход mean
            row = row[0]
        contribs = row[:-1]      # последний столбец — bias
        order = np.argsort(np.abs(contribs))[::-1][:5]

        win0 = (ev_t - pd.Timedelta(hours=hours)).to_pydatetime()
        win1 = (ev_t + pd.Timedelta(hours=1)).to_pydatetime()
        out = []
        for i in order:
            fname = feat_raw[int(i)]
            col = resolved[fname]
            sub = dfw[col].loc[win0:win1].dropna()
            ser = [{"t": pd.Timestamp(ix).isoformat(), "v": round(float(v), 4)} for ix, v in sub.items()]
            out.append(ExplainContributor(name=fname, contrib=round(float(contribs[int(i)]), 5),
                                          series=ser[-400:]))

        _pred = np.asarray(mdl.predict(Xrow), float)
        pmean = float(_pred[0, 0] if _pred.ndim == 2 else _pred[0])
        return SensorExplainResponse(
            sensor_id=sensor_id, event_ts=pd.Timestamp(ev_t).isoformat(),
            actual=round(float(dfw[sensor_id].iloc[pos]), 4), expected=round(pmean, 4),
            contributors=out)
    except HTTPException:
        raise
    except Exception:
        logger.exception("sensor_explain failed")
        raise HTTPException(status_code=503, detail="explain temporarily unavailable")


MULTI_CHART_MAX = 40   # верхний предел серий на канвасе (защита, не жёсткий UX-лимит)


@app.get("/api/stations/{station_id}/chart/multi", response_model=list[MultiSeriesItem])
def station_chart_multi(
    station_id: str,
    sensors: str = Query(..., description="CSV из sensor_id (любые ГПА/типы)"),
    days: int = Query(0, ge=0),
    t0: Optional[str] = Query(None),
    t1: Optional[str] = Query(None),
    response: Response = None,
):
    """Несколько датчиков (в т.ч. разные ГПА/типы) на одном канвасе: только факт,
    даунсемплинг в БД (≤CHART_TARGET_POINTS точек/серию). Без предикта — ответ лёгкий."""
    cfg_obj = _require_station(station_id)
    # защита от DoS: валидируем размер до split (иначе огромная строка → MemoryError)
    if len(sensors) > 8000:
        raise HTTPException(status_code=413, detail="Слишком длинный список датчиков")
    meta_by_id = {s["id"]: s for s in _sensors_list(station_id)}

    ids, seen = [], set()
    for raw in sensors.split(","):
        sid = raw.strip()
        if sid and sid not in seen and sid in meta_by_id:
            seen.add(sid)
            ids.append(sid)
        if len(ids) >= MULTI_CHART_MAX:
            break
    if not ids:
        raise HTTPException(status_code=404, detail="No known sensors in 'sensors'")

    t0_dt = _parse_chart_ts(t0, "t0") if t0 else None
    t1_dt = _parse_chart_ts(t1, "t1") if t1 else None
    if t0_dt and not t1_dt:
        t1_dt = datetime.now(timezone.utc)
    if t0_dt and t1_dt and t0_dt >= t1_dt:
        raise HTTPException(status_code=422, detail="t0 must be before t1")
    effective_days = days if days > 0 else 30

    state = _get_live_state(station_id)
    # Батч: факт всех тегов ОДНИМ запросом (point = ANY) вместо N round-trip'ов.
    _tags = [meta_by_id[s].get("tag") for s in ids
             if meta_by_id[s].get("tag") and meta_by_id[s]["tag"] != "N/A"]
    multi_raw = _fetch_raw_db_multi(
        cfg_obj, _tags, days=None if t0_dt else effective_days, t0=t0_dt, t1=t1_dt)
    out: list[MultiSeriesItem] = []
    for sid in ids:
        info = meta_by_id[sid]
        pts = multi_raw.get(info.get("tag"), [])
        # Фолбэк для РАСЧЁТНЫХ индексов (polytropic_head/η_p/shaft/… — нет сырого
        # тега в raw_data → выборка пуста). Берём ряд из state.json, фильтруем по окну.
        if not pts:
            sp = state.get("series", {}).get(sid, [])
            if sp:
                if t0_dt or t1_dt:
                    lo_s = t0_dt.astimezone(_TZ5).strftime("%Y-%m-%dT%H:%M:%S") if t0_dt else ""
                    hi_s = t1_dt.astimezone(_TZ5).strftime("%Y-%m-%dT%H:%M:%S") if t1_dt else "9999"
                    sp = [p for p in sp if lo_s <= p["t"] <= hi_s]
                else:
                    cutoff = (datetime.fromisoformat(sp[-1]["t"]) - timedelta(days=effective_days)).isoformat()
                    sp = [p for p in sp if p["t"] >= cutoff]
                if len(sp) > 2000:
                    sp = sp[:: len(sp) // 2000 + 1]
                pts = [{"t": p["t"], "v": p["v"]} for p in sp]
        vs = [p["v"] for p in pts]
        out.append(MultiSeriesItem(
            sensor_id=sid, name=info.get("name", sid), tag=info["tag"],
            # unit — физическая единица измерения. В metadata её нет (по принципу проекта
            # единицы берутся только из паспорта, не угадываются), а subsystem — это НЕ
            # единица, поэтому не подставляем её сюда (раньше unit="AX20" — обрезок тега).
            gpa=info.get("gpa", ""), unit=None,
            range_min=min(vs) if vs else None, range_max=max(vs) if vs else None,
            series=pts,
        ))

    if response is not None:
        is_historical = t1_dt is not None and \
            t1_dt < datetime.now(timezone.utc) - timedelta(hours=1)
        response.headers["Cache-Control"] = \
            "public, max-age=3600" if is_historical else "public, max-age=25"
    return out


@app.get("/api/stations/{station_id}/heatmap", response_model=list[HeatmapCell])
def station_heatmap(station_id: str, gpa: Optional[str] = Query(None), response: Response = None):
    _require_station(station_id)
    if response:
        response.headers["Cache-Control"] = "public, max-age=25"
    return [
        HeatmapCell(
            sensor_id=s["id"], name=s["name"], gpa=s["gpa"],
            severity=s["severity"], anomaly_count=s["anomaly_count"],
        )
        for s in _sensors_list(station_id, gpa)
        if not s.get("input")        # входные сигналы (обороты/давления) — не в карте аномалий
    ]


@app.get("/api/stations/{station_id}/anomalies", response_model=list[AnomalyRecord])
def station_anomalies_db(
    station_id: str,
    limit: int = Query(200, le=1000),
    sensor_id: Optional[str] = Query(None),
):
    """Anomalies from PostgreSQL (persistent, survives predictor restarts)."""
    cfg = _require_station(station_id)
    try:
        loader = PostgresDataLoader(cfg)
        schema = cfg.db["schema"]
        # Журнал аномалий — из anomalies_t (проектная таблица, без легаси-бэкфилла).
        where  = " AND sensor_id = %(sid)s" if sensor_id else ""
        sql    = pgsql.SQL("""
            SELECT id, sensor_id, event_ts::text, anomaly_type,
                   severity, value, deviation, created_at::text
            FROM {schema}.anomalies_t
            WHERE 1=1 {where}
            ORDER BY event_ts DESC
            LIMIT %(limit)s
        """).format(schema=pgsql.Identifier(schema), where=pgsql.SQL(where))
        params: dict = {"limit": limit}
        if sensor_id:
            params["sid"] = sensor_id

        with get_db_connection(cfg) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [AnomalyRecord(**dict(r)) for r in rows]
    except Exception:
        logger.exception("station_anomalies_db failed")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")


# ── Сохранённые наборы графиков (set_of_graphs) ───────────────────────────────

class GraphSet(BaseModel):
    id: int
    name: str
    sensor_ids: list[str]
    updated_at: Optional[str] = None


class GraphSetIn(BaseModel):
    name: str
    sensor_ids: list[str]


def _owner(o: Optional[str]) -> str:
    o = (o or "").strip()
    return (o[:64] or "operator")


@app.get("/api/stations/{station_id}/graph-sets", response_model=list[GraphSet])
def graph_sets_list(station_id: str, owner: str = Query("operator")):
    """Наборы датчиков пользователя (готовые подборки для сравнения)."""
    cfg = _require_station(station_id)
    try:
        loader = PostgresDataLoader(cfg)
        return [GraphSet(**s) for s in loader.list_graph_sets(station_id, _owner(owner))]
    except Exception:
        logger.exception("graph_sets_list failed")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")


@app.post("/api/stations/{station_id}/graph-sets", response_model=GraphSet)
def graph_sets_create(station_id: str, body: GraphSetIn, owner: str = Query("operator")):
    """Создать/обновить набор (upsert по имени для данного пользователя)."""
    cfg = _require_station(station_id)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Имя набора не задано")
    if len(name) > 80:
        raise HTTPException(status_code=422, detail="Имя набора слишком длинное")
    # дедуп + лимит на размер набора
    seen: set[str] = set()
    ids = [s for s in (str(x) for x in (body.sensor_ids or [])) if s and not (s in seen or seen.add(s))][:60]
    if not ids:
        raise HTTPException(status_code=422, detail="Набор пуст")
    try:
        loader = PostgresDataLoader(cfg)
        new_id = loader.save_graph_set(station_id, _owner(owner), name, ids)
        return GraphSet(id=new_id, name=name, sensor_ids=ids)
    except Exception:
        logger.exception("graph_sets_create failed")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")


@app.delete("/api/stations/{station_id}/graph-sets/{set_id}")
def graph_sets_delete(station_id: str, set_id: int, owner: str = Query("operator")):
    cfg = _require_station(station_id)
    try:
        loader = PostgresDataLoader(cfg)
        return {"deleted": loader.delete_graph_set(station_id, _owner(owner), set_id)}
    except Exception:
        logger.exception("graph_sets_delete failed")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")


# ── Журнал уведомлений ────────────────────────────────────────────────────────

@app.get("/api/stations/{station_id}/notifications", response_model=list[NotificationItem])
def station_notifications(
    station_id: str,
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    sensor_id: Optional[str] = Query(None),
    days: int = Query(0, ge=0),
    limit: int = Query(200, le=2000),
    response: Response = None,
):
    """Уведомления из {schema}.\"journal notifications\" с фильтрами."""
    cfg = _require_station(station_id)
    if response is not None:
        response.headers["Cache-Control"] = "public, max-age=15"
    try:
        schema = cfg.db["schema"]
        table  = journal_table_name()
        conds = [pgsql.SQL("1=1")]
        params: dict = {"limit": limit}
        if status:
            conds.append(pgsql.SQL("status = %(status)s")); params["status"] = status
        if severity:
            conds.append(pgsql.SQL("severity = %(sev)s")); params["sev"] = severity
        if sensor_id:
            conds.append(pgsql.SQL("sensor_id = %(sid)s")); params["sid"] = sensor_id
        if days > 0:
            conds.append(pgsql.SQL("event_ts >= NOW() - make_interval(days => %(days)s)"))
            params["days"] = days
        sql = pgsql.SQL("""
            SELECT id, station_id, sensor_id, point, gpa, event_ts::text,
                   anomaly_type, kind, severity, value, deviation, message,
                   status, created_at::text
            FROM {schema}.{table}
            WHERE {where}
            ORDER BY event_ts DESC
            LIMIT %(limit)s
        """).format(
            schema=pgsql.Identifier(schema), table=pgsql.Identifier(table),
            where=pgsql.SQL(" AND ").join(conds),
        )
        with get_db_connection(cfg) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [NotificationItem(**dict(r)) for r in rows]
    except Exception:
        logger.exception("station_notifications failed")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")


@app.post("/api/stations/{station_id}/notifications/{nid}/ack")
def ack_notification(station_id: str, nid: int, status: str = Query("ack")):
    """Сменить статус уведомления (по умолчанию -> 'ack')."""
    cfg = _require_station(station_id)
    try:
        schema = cfg.db["schema"]
        table  = journal_table_name()
        sql = pgsql.SQL(
            "UPDATE {schema}.{table} SET status = %(status)s WHERE id = %(id)s"
        ).format(schema=pgsql.Identifier(schema), table=pgsql.Identifier(table))
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"status": status, "id": nid})
                updated = cur.rowcount
            conn.commit()
        if not updated:
            raise HTTPException(status_code=404, detail="Notification not found")
        return {"id": nid, "status": status}
    except HTTPException:
        raise
    except Exception:
        logger.exception("ack_notification failed")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")


@app.get("/api/stations/{station_id}/sensors/{sensor_id}/health", response_model=SensorHealthSummary)
def station_sensor_health(station_id: str, sensor_id: str, days: int = Query(30, ge=1)):
    """Агрегат здоровья датчика: распределение кодов в raw_data.health + эпизоды
    из anomalies за последние `days` дней."""
    cfg = _require_station(station_id)
    meta_by_id = {s["id"]: s for s in _sensors_list(station_id)}
    if sensor_id not in meta_by_id:
        raise HTTPException(status_code=404, detail="Sensor not found")
    point = meta_by_id[sensor_id].get("tag")
    try:
        schema = cfg.db["schema"]
        evaluated = ok = stopped = anomalous = episodes = 0
        code_counts: dict = {}
        last_event_ts: Optional[str] = None
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                if point:
                    cur.execute(pgsql.SQL(
                        "SELECT health, count(*) FROM {schema}.raw_data "
                        "WHERE point = %(pt)s AND health IS NOT NULL "
                        "AND datetime >= NOW() - make_interval(days => %(days)s) "
                        "GROUP BY health"
                    ).format(schema=pgsql.Identifier(schema)),
                        {"pt": point, "days": days})
                    for health_val, cnt in cur.fetchall():
                        evaluated += cnt
                        if health_val == HEALTH_OK:
                            ok += cnt
                        elif health_val == HEALTH_STOPPED:
                            stopped += cnt
                        else:
                            anomalous += cnt
                            for code in str(health_val).split(","):
                                code_counts[code] = code_counts.get(code, 0) + cnt
                cur.execute(pgsql.SQL(
                    "SELECT count(*), max(event_ts)::text FROM {schema}.anomalies_t "
                    "WHERE sensor_id = %(sid)s "
                    "AND event_ts >= NOW() - make_interval(days => %(days)s)"
                ).format(schema=pgsql.Identifier(schema)),
                    {"sid": sensor_id, "days": days})
                row = cur.fetchone()
                episodes = row[0] or 0
                last_event_ts = row[1]
        return SensorHealthSummary(
            sensor_id=sensor_id, point=point, evaluated=evaluated, ok=ok,
            stopped=stopped, anomalous=anomalous, code_counts=code_counts,
            anomaly_episodes=episodes, last_event_ts=last_event_ts,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("station_sensor_health failed")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")


# ── Health ────────────────────────────────────────────────────────────────────
_STALE_DEGRADED_SEC = 15 * 60     # ml_engine считается протухшим
_STALE_DOWN_SEC     = 60 * 60     # ml_engine считается мёртвым
_db_check_cache: dict = {}   # station_id -> {"t": float, "ok": bool|None}


def _check_db(cfg: StationConfig) -> bool:
    """SELECT 1 с кешем 30с — health не молотит мёртвую БД на каждый запрос.
    Кеш разделён по station_id: станции могут жить на РАЗНЫХ БД, поэтому результат
    одной не должен применяться к другим (мультистанционная корректность)."""
    now = time.time()
    sid = cfg.station_id
    ent = _db_check_cache.get(sid)
    if ent and ent["ok"] is not None and now - ent["t"] < 30:
        return ent["ok"]
    ok = False
    try:
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                ok = cur.fetchone() is not None
    except Exception:
        logger.warning("health: БД недоступна (%s)", sid, exc_info=True)
    _db_check_cache[sid] = {"t": now, "ok": ok}
    return ok


@app.get("/api/health")
def health(response: Response):
    """status: ok | degraded (state >15 мин или БД лежит) | down (state >1 ч).
    degraded/down -> HTTP 503, чтобы внешний чекер ловил проблему."""
    result: dict = {"status": "ok", "timestamp": time.time(), "stations": {}}
    worst_age: Optional[float] = None
    db_ok = True
    for sid in list_stations():
        state = _get_live_state(sid)
        age: Optional[float] = None
        try:
            cfg = load_station_config(sid)
            if cfg.state_path.exists():
                age = time.time() - cfg.state_path.stat().st_mtime
            db_ok = _check_db(cfg) and db_ok
        except Exception:
            logger.exception("health: станция %s не прочитана", sid)
        if age is not None:
            worst_age = age if worst_age is None else max(worst_age, age)
        result["stations"][sid] = {
            "live_data": bool(state.get("sensors")),
            "last_updated": state.get("last_updated"),
            "state_age_seconds": round(age) if age is not None else None,
            "model_drift": state.get("model_drift"),
        }

    result["db"] = "ok" if db_ok else "error"
    result["state_age_seconds"] = round(worst_age) if worst_age is not None else None
    if worst_age is None or worst_age > _STALE_DOWN_SEC:
        result["status"] = "down"
        result["ml_engine"] = "down"
    elif worst_age > _STALE_DEGRADED_SEC or not db_ok:
        result["status"] = "degraded"
        result["ml_engine"] = "stale" if worst_age > _STALE_DEGRADED_SEC else "ok"
    else:
        result["ml_engine"] = "ok"

    if result["status"] != "ok" and response is not None:
        response.status_code = 503
    return result


# ── Backward-compat aliases → ohangaron ──────────────────────────────────────
@app.get("/api/sensors", response_model=list[SensorMeta])
def compat_sensors(gpa: Optional[str] = Query(None)):
    return station_sensors(DEFAULT_STATION, gpa)


@app.get("/api/sensors/{sensor_id}", response_model=SensorMeta)
def compat_sensor(sensor_id: str):
    return station_sensor(DEFAULT_STATION, sensor_id)


@app.get("/api/sensors/{sensor_id}/chart", response_model=SensorChartResponse)
def compat_sensor_chart(sensor_id: str, days: int = Query(0, ge=0)):
    return station_sensor_chart(DEFAULT_STATION, sensor_id, days)


@app.get("/api/stats", response_model=StatsResponse)
def compat_stats():
    return station_stats(DEFAULT_STATION)


@app.get("/api/events", response_model=list[EventItem])
def compat_events(
    severity: Optional[str] = Query(None),
    gpa:      Optional[str] = Query(None),
    kind:     Optional[str] = Query(None),
    limit:    Optional[int] = Query(None),
    days:     Optional[int] = Query(None),
):
    return station_events(DEFAULT_STATION, severity, gpa, kind, limit, days)


@app.get("/api/heatmap", response_model=list[HeatmapCell])
def compat_heatmap(gpa: Optional[str] = Query(None)):
    return station_heatmap(DEFAULT_STATION, gpa)


@app.get("/api/chart/multi", response_model=list[MultiSeriesItem])
def compat_chart_multi(sensors: str = Query(...), days: int = Query(0, ge=0),
                       t0: Optional[str] = Query(None), t1: Optional[str] = Query(None)):
    return station_chart_multi(DEFAULT_STATION, sensors, days, t0, t1)


@app.get("/api/notifications", response_model=list[NotificationItem])
def compat_notifications(status: Optional[str] = Query(None), severity: Optional[str] = Query(None),
                         sensor_id: Optional[str] = Query(None), days: int = Query(0, ge=0),
                         limit: int = Query(200, le=2000)):
    return station_notifications(DEFAULT_STATION, status, severity, sensor_id, days, limit)


@app.post("/api/notifications/{nid}/ack")
def compat_ack_notification(nid: int, status: str = Query("ack")):
    return ack_notification(DEFAULT_STATION, nid, status)


@app.post("/api/events/ack")
def compat_ack_event(body: AckEventBody, status: str = Query("ack")):
    return ack_event(DEFAULT_STATION, body, status)


@app.get("/api/stations/{station_id}/pvsnapshot")
def station_pvsnapshot(station_id: str):
    """Latest tag→value snapshot from raw_data DB for the interactive diagram."""
    cfg   = _require_station(station_id)
    state = _get_live_state(station_id)

    # severity from ML live_state (keyed by SCADA tag)
    sev_map: dict[str, str] = {
        s.get("tag", ""): s.get("severity", "ok")
        for s in state.get("sensors", {}).values()
        if s.get("tag")
    }

    schema  = cfg.db["schema"]
    table   = cfg.data["table"]
    dt_col  = cfg.data["datetime_col"]
    pt_col  = cfg.data["point_col"]
    val_col = cfg.data["value_col"]

    tags: dict = {}
    try:
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                # ── Main query: last 24 h — covers stopped units (GPA-1) ──────
                cur.execute(pgsql.SQL("""
                    SELECT DISTINCT ON ({pt}) {pt}, {val}
                    FROM {schema}.{table}
                    WHERE {dt} >= NOW() - INTERVAL '24 hours'
                    ORDER BY {pt}, {dt} DESC
                """).format(
                    pt=pgsql.Identifier(pt_col), val=pgsql.Identifier(val_col),
                    dt=pgsql.Identifier(dt_col),
                    schema=pgsql.Identifier(schema), table=pgsql.Identifier(table),
                ))
                for tag, val in cur.fetchall():
                    if val is None:
                        continue
                    try:
                        tags[tag] = {
                            "v":   round(float(val), 4),
                            "sev": sev_map.get(tag, "ok"),
                        }
                    except (ValueError, TypeError):
                        pass

                # ── tg-sheet station sensors: GC_BFTG / GC_FG7001 / GC_UPTG ──
                # Single physical device; find whichever GPA has data and
                # replicate to the others so the diagram always shows a value.
                station_tg = ["GC_BFTG", "GC_FG7001", "GC_UPTG"]
                for sensor in station_tg:
                    src_val = next(
                        (tags[f"GPA-{g}.GPA-{g}.{sensor}.PV"]
                         for g in ("1", "2", "3")
                         if f"GPA-{g}.GPA-{g}.{sensor}.PV" in tags),
                        None,
                    )
                    if src_val is None:
                        continue
                    for g in ("1", "2", "3"):
                        alias = f"GPA-{g}.GPA-{g}.{sensor}.PV"
                        if alias not in tags:
                            tags[alias] = {**src_val}

                # ── Synthesise STATES bits from nСТ only if not in raw_data ─
                for g in ("1", "2", "3"):
                    # Skip if real STATES values already arrived from SCADA
                    if f"GPA-{g}.GPA-{g}.STATES_GTD.5" in tags:
                        continue
                    rpm_tag = f"GT-{g}.GT-{g}.GT01.CTRL.IN[2].VALUE"
                    if rpm_tag not in tags:
                        continue
                    is_running = tags[rpm_tag]["v"] > 1000
                    tags[f"GPA-{g}.GPA-{g}.STATES_GTD.5"] = {
                        "v": 1.0 if is_running else 0.0, "sev": "ok",
                    }
                    tags[f"GPA-{g}.GPA-{g}.STATES_GTD.0"] = {
                        "v": 0.0 if is_running else 1.0, "sev": "ok",
                    }
                    for bit in range(8):
                        val = 1.0 if (is_running and bit == 7) or (
                            not is_running and bit == 0) else 0.0
                        tags[f"GPA-{g}.GPA-{g}.STATES_GPA.{bit}"] = {
                            "v": val, "sev": "ok",
                        }

    except Exception:
        # не раскрываем внутренние детали БД клиенту (CWE-209); полное — в лог
        logger.exception("pvsnapshot failed")
        raise HTTPException(status_code=503, detail="Снимок SCADA временно недоступен")

    return {"tags": tags, "ts": state.get("last_updated", "")}


@app.get("/api/pvsnapshot")
def compat_pvsnapshot():
    return station_pvsnapshot(DEFAULT_STATION)


if __name__ == "__main__":
    import os
    import uvicorn
    # По умолчанию слушаем только localhost (без auth/TLS наружу выставлять нельзя).
    # Переопределяется env CS_API_HOST при осознанном развёртывании за reverse-proxy.
    host = os.environ.get("CS_API_HOST", "127.0.0.1")
    port = int(os.environ.get("CS_API_PORT", "8010"))
    uvicorn.run("main:app", host=host, port=port, workers=4)
