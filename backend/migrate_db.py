"""
Идемпотентные миграции схемы БД станции.
По образцу ensure_indexes.py: StationConfig, get_db_connection, psycopg2.sql,
логирование, CLI `--station` (default ohangaron).

Делает:
  1. raw_data.health → TEXT (недеструктивно): boolean переименовывается в
     health_legacy_bool, создаётся новый health TEXT. Существующий TEXT — noop.
  2. {schema}."journal notifications" — журнал уведомлений (имя из global.yaml,
     всегда через sql.Identifier). Битый/пустой артефакт (0 столбцов) пересоздаётся.
  3. Индексы журнала + (point, datetime) через ensure_indexes.

Запуск:  python migrate_db.py --station ohangaron
         (повторный запуск не меняет результат)
"""
from __future__ import annotations
import argparse
import logging

from psycopg2 import sql as pgsql

from station_config import load_station_config, get_db_connection, journal_table_name
import ensure_indexes as _ensure_indexes

logger = logging.getLogger(__name__)

# Столбцы журнала уведомлений (раздел 4 dock). Имя таблицы — из global.yaml.
_JOURNAL_COLUMNS = [
    ("id",           "BIGSERIAL PRIMARY KEY"),
    ("station_id",   "TEXT NOT NULL"),
    ("sensor_id",    "TEXT NOT NULL"),
    ("point",        "TEXT"),
    ("gpa",          "TEXT"),
    ("event_ts",     "TIMESTAMPTZ NOT NULL"),
    ("anomaly_type", "SMALLINT NOT NULL"),
    ("kind",         "TEXT"),
    ("severity",     "TEXT"),
    ("value",        "DOUBLE PRECISION"),
    ("deviation",    "DOUBLE PRECISION"),
    ("message",      "TEXT NOT NULL"),
    ("status",       "TEXT NOT NULL DEFAULT 'new'"),
    ("created_at",   "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    # Учёт ответственности (смены): кому уведомление «принадлежит» и кто его принял.
    ("owner",        "TEXT"),            # username оператора, активного на момент event_ts (чья смена)
    ("seen_at",      "TIMESTAMPTZ"),     # когда уведомление впервые показано владельцу
    ("acked_by",     "TEXT"),            # username того, кто фактически принял
    ("acked_at",     "TIMESTAMPTZ"),     # когда принято
]
# Минимальный набор столбцов, по которому судим, что таблица «настоящая».
# НЕ включает owner/acked_* — иначе существующая непустая прод-таблица без них была
# бы ошибочно признана «битой». Новые столбцы добавляются отдельным ALTER (idempotent).
_JOURNAL_REQUIRED = {"id", "sensor_id", "event_ts", "anomaly_type", "message", "status"}
# Столбцы ответственности — добавляются к уже существующей таблице через ADD COLUMN IF NOT EXISTS.
_JOURNAL_EXTRA = [
    ("owner",   "TEXT"),
    ("seen_at", "TIMESTAMPTZ"),
    ("acked_by", "TEXT"),
    ("acked_at", "TIMESTAMPTZ"),
]


def _column_type(cur, schema: str, table: str, column: str) -> str | None:
    """Тип столбца из information_schema или None, если столбца нет."""
    cur.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
        (schema, table, column),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _table_columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return {r[0] for r in cur.fetchall()}


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f'{schema}.{_quote(schema, table)}',))
    return cur.fetchone()[0] is not None


def _quote(schema: str, table: str) -> str:
    """Имя таблицы с пробелом для to_regclass: schema."table name"."""
    return '"' + table.replace('"', '""') + '"'


# ── health TEXT (недеструктивно) ──────────────────────────────────────────────

def _migrate_health(cur, schema: str) -> list[str]:
    done: list[str] = []
    htype = _column_type(cur, schema, "raw_data", "health")

    if htype is None:
        stmt = pgsql.SQL("ALTER TABLE {sch}.raw_data ADD COLUMN health TEXT").format(
            sch=pgsql.Identifier(schema))
        cur.execute(stmt)
        done.append("ADD COLUMN raw_data.health TEXT")
        return done

    if htype == "text":
        logger.info("raw_data.health уже TEXT — пропускаю")
        return done

    # health есть, но не TEXT (напр. boolean). Недеструктивно: переименовать в
    # health_legacy_bool (если ещё нет) и создать новый health TEXT.
    legacy = "health_legacy_bool"
    has_legacy = _column_type(cur, schema, "raw_data", legacy) is not None
    if not has_legacy:
        cur.execute(pgsql.SQL(
            "ALTER TABLE {sch}.raw_data RENAME COLUMN health TO {legacy}"
        ).format(sch=pgsql.Identifier(schema), legacy=pgsql.Identifier(legacy)))
        done.append(f"RENAME raw_data.health ({htype}) -> {legacy}")
        cur.execute(pgsql.SQL(
            "ALTER TABLE {sch}.raw_data ADD COLUMN IF NOT EXISTS health TEXT"
        ).format(sch=pgsql.Identifier(schema)))
        done.append("ADD COLUMN raw_data.health TEXT")
    else:
        # legacy уже занят — значит миграция уже была. Гарантируем новый health TEXT.
        if _column_type(cur, schema, "raw_data", "health") is None:
            cur.execute(pgsql.SQL(
                "ALTER TABLE {sch}.raw_data ADD COLUMN health TEXT"
            ).format(sch=pgsql.Identifier(schema)))
            done.append("ADD COLUMN raw_data.health TEXT (legacy уже был)")
        else:
            logger.info("raw_data.health и %s уже существуют — пропускаю", legacy)
    return done


# ── журнал уведомлений ────────────────────────────────────────────────────────

def _create_journal(cur, schema: str, table: str) -> None:
    cols_sql = pgsql.SQL(", ").join(
        pgsql.SQL("{name} " + ctype).format(name=pgsql.Identifier(name))
        for name, ctype in _JOURNAL_COLUMNS
    )
    cur.execute(pgsql.SQL(
        "CREATE TABLE {sch}.{tbl} ({cols}, "
        "CONSTRAINT journal_notifications_dedup UNIQUE (sensor_id, event_ts, anomaly_type))"
    ).format(sch=pgsql.Identifier(schema), tbl=pgsql.Identifier(table), cols=cols_sql))


def _journal_add_columns(cur, schema: str, table: str) -> list[str]:
    """Идемпотентно добавляет столбцы ответственности (owner/seen_at/acked_by/acked_at)
    к уже существующей таблице журнала. Данные не трогаются (ADD COLUMN IF NOT EXISTS)."""
    done: list[str] = []
    existing = _table_columns(cur, schema, table)
    for name, ctype in _JOURNAL_EXTRA:
        if name in existing:
            continue
        cur.execute(pgsql.SQL(
            "ALTER TABLE {sch}.{tbl} ADD COLUMN IF NOT EXISTS {col} " + ctype
        ).format(sch=pgsql.Identifier(schema), tbl=pgsql.Identifier(table),
                 col=pgsql.Identifier(name)))
        done.append(f'ADD COLUMN "{table}".{name} {ctype}')
    return done


def _migrate_journal(cur, schema: str, table: str) -> list[str]:
    done: list[str] = []
    if not _table_exists(cur, schema, table):
        _create_journal(cur, schema, table)
        done.append(f'CREATE TABLE {schema}."{table}"')
        return done

    cols = _table_columns(cur, schema, table)
    if _JOURNAL_REQUIRED.issubset(cols):
        logger.info('журнал "%s" уже имеет нужные столбцы — пропускаю', table)
        return done

    # Таблица существует, но битая/неполная (напр. 0 столбцов). Дропаем ТОЛЬКО
    # если пустая — рабочие данные не теряем.
    cur.execute(pgsql.SQL("SELECT count(*) FROM {sch}.{tbl}").format(
        sch=pgsql.Identifier(schema), tbl=pgsql.Identifier(table)))
    n = cur.fetchone()[0]
    if n > 0:
        raise RuntimeError(
            f'журнал {schema}."{table}" существует с неполной схемой и НЕ пуст '
            f'({n} строк) — миграция остановлена во избежание потери данных. '
            f'Исправьте схему вручную.')
    cur.execute(pgsql.SQL("DROP TABLE {sch}.{tbl}").format(
        sch=pgsql.Identifier(schema), tbl=pgsql.Identifier(table)))
    _create_journal(cur, schema, table)
    done.append(f'DROP+CREATE битого пустого журнала {schema}."{table}"')
    return done


def _journal_indexes(cur, schema: str, table: str) -> list[str]:
    done: list[str] = []
    stmts = [
        pgsql.SQL(
            "CREATE INDEX IF NOT EXISTS idx_journal_notif_ts "
            "ON {sch}.{tbl} (event_ts DESC)"
        ).format(sch=pgsql.Identifier(schema), tbl=pgsql.Identifier(table)),
        pgsql.SQL(
            "CREATE INDEX IF NOT EXISTS idx_journal_notif_status "
            "ON {sch}.{tbl} (status, event_ts DESC)"
        ).format(sch=pgsql.Identifier(schema), tbl=pgsql.Identifier(table)),
    ]
    for stmt in stmts:
        cur.execute(stmt)
        done.append(stmt.as_string(cur))
    return done


# ── оркестрация ───────────────────────────────────────────────────────────────

def migrate(station_id: str = "ohangaron") -> list[str]:
    """Применяет все миграции для станции. Возвращает список выполненных шагов.
    Идемпотентно: повторный запуск не меняет результат."""
    cfg = load_station_config(station_id)
    schema = cfg.db["schema"]
    journal = journal_table_name()

    done: list[str] = []
    with get_db_connection(cfg) as conn:
        with conn.cursor() as cur:
            done += _migrate_health(cur, schema)
            done += _migrate_journal(cur, schema, journal)
            done += _journal_add_columns(cur, schema, journal)   # owner/seen_at/acked_by/acked_at
            done += _journal_indexes(cur, schema, journal)
        conn.commit()

    # Таблица anomalies (идемпотентный DDL, источник — data_loader) ДО создания
    # индексов на ней: на свежей БД migrate выполняется раньше live_predict, который
    # обычно и создаёт таблицу, иначе CREATE INDEX ON anomalies молча пропускался.
    try:
        from data_loader import PostgresDataLoader
        loader = PostgresDataLoader(cfg)
        loader.ensure_anomalies_table()
        done.append("ensure anomalies table")
        # anomalies_t — расширенная запись по аномалии (+ SHAP jsonb); отдельная
        # таблица, legacy anomalies не трогаем.
        loader.ensure_anomalies_t()
        done.append("ensure anomalies_t table")
        # predictions — серия прогноза/коридора (p/lo/hi) для DB-backed дашборда (turnkey)
        loader.ensure_predictions()
        done.append("ensure predictions table")
    except Exception:
        logger.exception("ensure_anomalies(_t)/predictions в миграции не выполнен")

    # (point, datetime) и индексы anomalies — общий модуль
    try:
        done += _ensure_indexes.ensure_indexes(station_id)
    except Exception:
        logger.exception("ensure_indexes в миграции не выполнен (best-effort)")

    for step in done:
        logger.info("migrate: %s", step)
    return done


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Идемпотентные миграции схемы БД станции")
    parser.add_argument("--station", default="ohangaron", help="ID станции (default ohangaron)")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    steps = migrate(args.station)
    if steps:
        logger.info("Готово. Выполнено шагов: %d", len(steps))
    else:
        logger.info("Готово. Изменений не требовалось (всё актуально).")
