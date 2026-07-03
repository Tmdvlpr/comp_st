"""
Загрузка конфигурации компрессорных станций из YAML-файлов.
Поддерживает интерполяцию ${ENV_VAR:default}.
"""
from __future__ import annotations
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import yaml
import psycopg2
from psycopg2 import pool as pg_pool
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
_STATION_ID_RE = re.compile(r'^[a-z0-9_-]+$')

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
STATIONS_DIR = BASE_DIR / "config" / "stations"


@dataclass
class StationConfig:
    station_id:   str
    display_name: str
    db:           dict
    data:         dict
    units:        list[str]
    models_dir:   str
    state_file:   str
    # Семантический маппинг суффикса SCADA-тега → фича (PD.PV → gas_pressure_out_gpa).
    # Пусто → fallback на механический _normalize_tag.
    tag_mapping:  dict = field(default_factory=dict)
    # Параметры research-методологии: gas-константы, LIMITS, conditioning/response,
    # пороги детекции, train_cutoff. Пусто → дефолты модулей.
    methodology:  dict = field(default_factory=dict)
    # Геолокация станции для внешней погоды (ambient_temp): {lat, lon, timezone}.
    location:     dict = field(default_factory=dict)

    @property
    def models_path(self) -> Path:
        return BASE_DIR / self.models_dir

    @property
    def state_path(self) -> Path:
        return BASE_DIR / self.state_file


def _interpolate(value: str) -> str:
    """Заменяет ${VAR:default} на значение из окружения или default."""
    def replace(m: re.Match) -> str:
        var, _, default = m.group(1).partition(":")
        return os.environ.get(var, default)
    return re.sub(r'\$\{([^}]+)\}', replace, value)


def _interpolate_dict(obj):
    """Рекурсивно интерполирует строки в dict/list."""
    if isinstance(obj, dict):
        return {k: _interpolate_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_dict(v) for v in obj]
    if isinstance(obj, str):
        return _interpolate(obj)
    return obj


_config_cache: dict[str, StationConfig] = {}


def load_station_config(station_id: str) -> StationConfig:
    if not _STATION_ID_RE.match(station_id):
        raise ValueError(f"Invalid station_id: {station_id!r}")
    if station_id in _config_cache:
        return _config_cache[station_id]
    path = STATIONS_DIR / f"{station_id}.yaml"
    # Guard against path traversal (redundant after regex, but defence-in-depth)
    if not path.resolve().is_relative_to(STATIONS_DIR.resolve()):
        raise ValueError(f"Invalid station_id: {station_id!r}")
    if not path.exists():
        raise FileNotFoundError(f"Station config not found: {station_id}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = _interpolate_dict(raw)
    db = cfg["db"]
    db["port"] = int(db.get("port", 5432))
    result = StationConfig(
        station_id=cfg["station_id"],
        display_name=cfg["display_name"],
        db=db,
        data=cfg["data"],
        units=cfg.get("units", []),
        models_dir=cfg["models_dir"],
        state_file=cfg["state_file"],
        tag_mapping=cfg.get("tag_mapping", {}) or {},
        methodology=cfg.get("methodology", {}) or {},
        location=cfg.get("location", {}) or {},
    )
    _config_cache[station_id] = result
    return result


def list_stations() -> list[str]:
    """Возвращает список доступных station_id (без шаблонов _*)."""
    if not STATIONS_DIR.exists():
        return []
    return sorted(
        p.stem for p in STATIONS_DIR.glob("*.yaml")
        if not p.stem.startswith("_")
    )


_GLOBAL_CONFIG_PATH = BASE_DIR / "config" / "global.yaml"
_global_config_cache: dict | None = None


def load_global_config() -> dict:
    """Читает config/global.yaml (default_station, anomaly_table, journal_table).
    Кешируется на процесс. Безопасно: при отсутствии файла — дефолты."""
    global _global_config_cache
    if _global_config_cache is not None:
        return _global_config_cache
    defaults = {
        "default_station": "ohangaron",
        "anomaly_table":   "anomalies",
        "journal_table":   "journal notifications",
    }
    try:
        with open(_GLOBAL_CONFIG_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        defaults.update({k: v for k, v in raw.items() if v is not None})
    except FileNotFoundError:
        logger.warning("global.yaml не найден (%s) — использую дефолты", _GLOBAL_CONFIG_PATH)
    _global_config_cache = defaults
    return _global_config_cache


def journal_table_name() -> str:
    """Имя таблицы журнала уведомлений (может содержать пробел → sql.Identifier!)."""
    return load_global_config()["journal_table"]


_pools: dict[str, pg_pool.ThreadedConnectionPool] = {}
_pools_lock = threading.Lock()


def _get_pool(cfg: StationConfig) -> pg_pool.ThreadedConnectionPool:
    sid = cfg.station_id
    if sid in _pools:
        return _pools[sid]
    with _pools_lock:
        if sid not in _pools:   # double-checked locking
            db = cfg.db
            _pools[sid] = pg_pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=10,
                host=db["host"],
                port=int(db.get("port", 5432)),
                dbname=db["name"],
                user=db["user"],
                password=db.get("password", ""),
                connect_timeout=30,  # удалённая БД по медленной/нестабильной сети
                # statement_timeout: зависший запрос на нестабильном канале не
                # блокирует процесс навечно — сервер прервёт его, код сделает retry.
                # statement_timeout: реально зависший запрос не блокирует навечно.
                # lock_timeout: запись health в raw_data может ждать блокировку →
                # не ждём строку дольше 5с, лучше пропустить чанк (health — best-effort).
                # idle_in_transaction_session_timeout: если процесс убили посреди
                # транзакции, сервер сам откатит её через 60с и снимет блокировки
                # (иначе остаётся «idle in transaction»-зомби, держащий RowExclusiveLock).
                options='-c statement_timeout=300000 -c lock_timeout=5000 '
                        '-c idle_in_transaction_session_timeout=60000',
                # TCP keepalive: сеть до БД роняет простаивающие соединения —
                # держим пул живым; idle=15с — быстрее ловим «мёртвый» сокет после
                # долгого простоя (напр. во время тяжёлого compute без запросов)
                keepalives=1,
                keepalives_idle=15,
                keepalives_interval=5,
                keepalives_count=3,
            )
    return _pools[sid]


@contextmanager
def get_db_connection(cfg: StationConfig, acquire_retries: int = 3):
    """Выдаёт соединение из пула и возвращает его после использования.
    Удалённая БД по нестабильной сети → получение коннекта повторяется с backoff:
    при таймауте/обрыве пул сбрасывается и пересоздаётся."""
    import time as _time
    pool = None
    conn = None
    last_err: Exception | None = None
    for attempt in range(1, acquire_retries + 1):
        try:
            pool = _get_pool(cfg)
            conn = pool.getconn()
            break
        except Exception as e:
            last_err = e
            reset_pool(cfg)
            if attempt < acquire_retries:
                _time.sleep(3 * attempt)
    if conn is None:
        raise last_err  # type: ignore[misc]
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            pass


def reset_pool(cfg: StationConfig) -> None:
    """Закрывает и выбрасывает пул станции из кеша — следующий get_db_connection
    создаст свежий. Нужно при обрыве сети до удалённой БД (битые коннекты)."""
    sid = cfg.station_id
    with _pools_lock:
        pool = _pools.pop(sid, None)
    if pool is not None:
        try:
            pool.closeall()
        except Exception:
            logger.debug("reset_pool: closeall не выполнен", exc_info=True)
