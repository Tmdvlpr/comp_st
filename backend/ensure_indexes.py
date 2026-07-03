"""
Создание индексов БД для производительности API-запросов.
Идемпотентно (IF NOT EXISTS). Вызывается из main.py при старте (best-effort)
или вручную: python ensure_indexes.py [station_id]
"""
from __future__ import annotations
import logging
import sys

from psycopg2 import sql as pgsql

from station_config import load_station_config, get_db_connection

logger = logging.getLogger(__name__)


def ensure_indexes(station_id: str = "ohangaron") -> list[str]:
    """Создаёт индексы, возвращает список выполненных DDL."""
    cfg = load_station_config(station_id)
    schema = cfg.db["schema"]
    table = cfg.data["table"]
    dt_col = cfg.data["datetime_col"]
    pt_col = cfg.data["point_col"]

    statements = [
        # pvsnapshot (DISTINCT ON point ORDER BY datetime DESC) и chart (WHERE point=...)
        pgsql.SQL(
            "CREATE INDEX IF NOT EXISTS idx_raw_data_point_dt "
            "ON {schema}.{table} ({pt}, {dt} DESC)"
        ).format(
            schema=pgsql.Identifier(schema), table=pgsql.Identifier(table),
            pt=pgsql.Identifier(pt_col), dt=pgsql.Identifier(dt_col),
        ),
        # журнал аномалий: ORDER BY event_ts DESC LIMIT N
        pgsql.SQL(
            "CREATE INDEX IF NOT EXISTS idx_anomalies_ts "
            "ON {schema}.anomalies (event_ts DESC)"
        ).format(schema=pgsql.Identifier(schema)),
        # выборка по датчику
        pgsql.SQL(
            "CREATE INDEX IF NOT EXISTS idx_anomalies_sensor_ts "
            "ON {schema}.anomalies (sensor_id, event_ts DESC)"
        ).format(schema=pgsql.Identifier(schema)),
        # курсор «докуда посчитано»: max(datetime) WHERE health IS NOT NULL
        # (catch_up_missing на старте). Частичный индекс → без full-scan по raw_data.
        pgsql.SQL(
            "CREATE INDEX IF NOT EXISTS idx_raw_data_health_dt "
            "ON {schema}.{table} ({dt} DESC) WHERE health IS NOT NULL"
        ).format(
            schema=pgsql.Identifier(schema), table=pgsql.Identifier(table),
            dt=pgsql.Identifier(dt_col),
        ),
        # anomalies_t — дашборд читает активно (DB-backed): маркеры графика, /explain
        # (sensor_id + shap_top + окно), события (ORDER BY event_ts DESC), агрегация
        # severity/count по датчикам. Базовый ensure_anomalies_t даёт (station_id,event_ts)/
        # kind/severity — добавляем per-sensor и чистый event_ts.
        pgsql.SQL(
            "CREATE INDEX IF NOT EXISTS idx_anomalies_t_sensor_ts "
            "ON {schema}.anomalies_t (sensor_id, event_ts DESC)"
        ).format(schema=pgsql.Identifier(schema)),
        pgsql.SQL(
            "CREATE INDEX IF NOT EXISTS idx_anomalies_t_ts "
            "ON {schema}.anomalies_t (event_ts DESC)"
        ).format(schema=pgsql.Identifier(schema)),
    ]

    done = []
    with get_db_connection(cfg) as conn:
        with conn.cursor() as cur:
            for stmt in statements:
                ddl = stmt.as_string(cur)
                try:
                    cur.execute(stmt)
                    conn.commit()
                    done.append(ddl)
                except Exception:
                    conn.rollback()
                    logger.exception("Index DDL failed: %s", ddl)
    return done


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sid = sys.argv[1] if len(sys.argv) > 1 else "ohangaron"
    for ddl in ensure_indexes(sid):
        logger.info("OK: %s", ddl)
