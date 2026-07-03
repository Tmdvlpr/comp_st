"""
PostgresDataLoader — универсальный загрузчик данных из PostgreSQL.
Работает с любой станцией через StationConfig.
"""
from __future__ import annotations
import logging
import os
import re
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
from psycopg2 import sql as pgsql

from station_config import StationConfig, get_db_connection, journal_table_name
from anomaly_types import KIND_TO_CODE, KIND_SEVERITY

logger = logging.getLogger(__name__)


class PostgresDataLoader:
    def __init__(self, cfg: StationConfig):
        self.cfg = cfg
        self._schema = cfg.db["schema"]
        self._table = cfg.data["table"]
        self._dt_col = cfg.data["datetime_col"]
        self._pt_col = cfg.data["point_col"]
        self._val_col = cfg.data["value_col"]

    def _fetch_df(self, query: pgsql.Composable, params: Optional[tuple] = None) -> pd.DataFrame:
        """Execute a SELECT and return a DataFrame."""
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
        return pd.DataFrame(rows, columns=cols)

    def _fetch_df_streaming(self, query: pgsql.Composable,
                            params: Optional[tuple] = None,
                            itersize: int = 200_000,
                            retries: int = 3) -> pd.DataFrame:
        """Потоковая выборка через server-side курсор: тянет батчами, не буферизуя
        весь результат сразу. Для больших исторических загрузок (млн строк) —
        меньше пиковая память сервера и активная (не простаивающая) сеть.
        Сеть до БД нестабильна (удалённый сервер) → ретраи с backoff: при обрыве
        соединения битый коннект выбрасывается из пула, попытка повторяется."""
        import time as _time
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                with get_db_connection(self.cfg) as conn:
                    with conn.cursor(name="cs4_stream") as cur:
                        cur.itersize = itersize
                        cur.execute(query, params)
                        # У named-курсора cur.description = None до первого fetch —
                        # читаем колонки после первого батча.
                        cols: list | None = None
                        rows: list = []
                        while True:
                            batch = cur.fetchmany(itersize)
                            if cols is None:
                                cols = [d[0] for d in cur.description] if cur.description else []
                            if not batch:
                                break
                            rows.extend(batch)
                return pd.DataFrame(rows, columns=cols)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last_err = e
                # выбросить битый пул — следующая попытка создаст свежие коннекты
                try:
                    from station_config import reset_pool
                    reset_pool(self.cfg)
                except Exception:
                    pass
                if attempt < retries:
                    backoff = 5 * attempt
                    logger.warning("streaming-fetch попытка %d/%d упала (%s) — повтор через %ds",
                                   attempt, retries, e, backoff)
                    _time.sleep(backoff)
        raise last_err  # type: ignore[misc]

    def _write_batches(self, sql: pgsql.Composable, rows: list[dict],
                       chunk: int = 2000, page_size: int = 500,
                       retries: int = 3) -> int:
        """Батчевая запись с почанковым коммитом и устойчивостью к обрыву сети:
        завершённые чанки сохраняются; на сбое коннекта пул сбрасывается и
        ТЕКУЩИЙ чанк повторяется (операции идемпотентны — UPDATE health /
        INSERT ON CONFLICT). Прогресс не теряется.

        ГАРД: при env CS_DISABLE_DB_WRITE запись подавляется (no-op) — единственная точка,
        через которую идут ВСЕ записи аномалий/health/журнала, поэтому тесты/валидация на
        реальной станции не могут загрязнить боевую БД (инцидент 2026-06-25 не повторится)."""
        import time as _time
        import psycopg2.errors as _pgerr
        if os.environ.get("CS_DISABLE_DB_WRITE"):
            logger.warning("CS_DISABLE_DB_WRITE: запись %d строк подавлена (read-only режим)", len(rows))
            return 0
        processed = 0
        skipped = 0
        i = 0
        attempts = 0
        while i < len(rows):
            batch = rows[i : i + chunk]
            try:
                with get_db_connection(self.cfg) as conn:
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_batch(cur, sql, batch, page_size=page_size)
                    conn.commit()
                processed += len(batch)
                i += chunk
                attempts = 0
            except (_pgerr.QueryCanceled, _pgerr.LockNotAvailable) as e:
                # таймаут запроса / блокировка строки (конкуренция с ingestion) —
                # best-effort: пропускаем чанк, не виснем и не падаем (повтор на след. цикле)
                logger.warning("write-batch: чанк %d пропущен (timeout/lock)", i // chunk)
                skipped += len(batch)
                i += chunk
                attempts = 0
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                attempts += 1
                from station_config import reset_pool
                try:
                    reset_pool(self.cfg)
                except Exception:
                    pass
                if attempts >= retries:
                    logger.error("write-batch: чанк %d не записан после %d попыток (%s)",
                                 i // chunk, retries, e)
                    raise
                logger.warning("write-batch: чанк %d упал (%s) — повтор %d/%d",
                               i // chunk, e, attempts, retries)
                _time.sleep(5 * attempts)
        if skipped:
            logger.warning("write-batch: пропущено %d строк из-за блокировок/таймаутов (повтор на след. цикле)", skipped)
        return processed

    # ── Данные для обучения ───────────────────────────────────────────────────

    def fetch_training_data(self, cutoff_date: Optional[str] = None,
                            from_date: Optional[str] = None) -> pd.DataFrame:
        """
        Возвращает широкий DataFrame (строки = временные метки, столбцы = теги).
        cutoff_date: верхняя граница ('2026-06-12'); from_date: нижняя граница.
        from_date ограничивает объём передачи по медленной/нестабильной сети до БД.
        """
        schema = pgsql.Identifier(self._schema)
        table  = pgsql.Identifier(self._table)
        dt     = pgsql.Identifier(self._dt_col)
        pt     = pgsql.Identifier(self._pt_col)
        val    = pgsql.Identifier(self._val_col)

        # ORDER BY НЕ нужен: _to_wide пересортирует (df.sort_values + sort_index),
        # а server-side sort на млн строк — лишняя нагрузка/память (роняла коннект).
        conds, params_list = [], []
        if from_date:
            conds.append(pgsql.SQL("{dt} >= %s").format(dt=dt))
            params_list.append(from_date)
        if cutoff_date:
            conds.append(pgsql.SQL("{dt} <= %s").format(dt=dt))
            params_list.append(cutoff_date)
        where = pgsql.SQL(" WHERE ") + pgsql.SQL(" AND ").join(conds) if conds else pgsql.SQL("")
        query = pgsql.SQL("SELECT {dt}, {pt}, {val} FROM {schema}.{table}{where}").format(
            dt=dt, pt=pt, val=val, schema=schema, table=table, where=where)
        params = tuple(params_list) if params_list else None

        df = self._fetch_df_streaming(query, params)
        return self._to_wide(df)

    def fetch_raw_window(self, from_ts: str, to_ts: str) -> pd.DataFrame:
        """LONG-формат (datetime, point, value) за окно [from_ts, to_ts).
        Для бэкафилла истории окнами. Потоковая выборка с ретраями."""
        schema = pgsql.Identifier(self._schema)
        table  = pgsql.Identifier(self._table)
        dt     = pgsql.Identifier(self._dt_col)
        pt     = pgsql.Identifier(self._pt_col)
        val    = pgsql.Identifier(self._val_col)
        query = pgsql.SQL(
            "SELECT {dt}, {pt}, {val} FROM {schema}.{table}"
            " WHERE {dt} >= %s AND {dt} < %s"
        ).format(dt=dt, pt=pt, val=val, schema=schema, table=table)
        return self._fetch_df_streaming(query, (from_ts, to_ts))

    # ── Живые данные для предиктора ───────────────────────────────────────────

    def fetch_live_data(self, hours: int = 2) -> pd.DataFrame:
        """Возвращает данные за последние `hours` часов в широком формате."""
        schema = pgsql.Identifier(self._schema)
        table  = pgsql.Identifier(self._table)
        dt     = pgsql.Identifier(self._dt_col)
        pt     = pgsql.Identifier(self._pt_col)
        val    = pgsql.Identifier(self._val_col)

        query = pgsql.SQL(
            "SELECT {dt}, {pt}, {val} FROM {schema}.{table}"
            " WHERE {dt} >= NOW() - %s::interval ORDER BY {dt}"
        ).format(dt=dt, pt=pt, val=val, schema=schema, table=table)

        df = self._fetch_df(query, (f"{int(hours)} hours",))
        return self._to_wide(df)

    def _to_wide(self, df: pd.DataFrame) -> pd.DataFrame:
        """Приводит long-format DataFrame к широкому формату."""
        if df.empty:
            return df
        dt  = self._dt_col
        pt  = self._pt_col
        val = self._val_col

        df[dt] = pd.to_datetime(df[dt], errors="coerce")
        df = df.dropna(subset=[dt])
        df[val] = pd.to_numeric(df[val], errors="coerce")
        df[dt] = df[dt].dt.round("5min")
        df = df.sort_values(dt).drop_duplicates([dt, pt])

        wide = (df.pivot_table(index=dt, columns=pt, values=val)
                  .sort_index()
                  .ffill(limit=2))
        wide.columns.name = None
        return wide

    # ── Маппинг тегов ─────────────────────────────────────────────────────────

    def build_tag_mapping(self) -> dict[str, str]:
        """Строит словарь {tag (point) → feature_name} из DISTINCT point в БД."""
        query = pgsql.SQL(
            "SELECT DISTINCT {pt} FROM {schema}.{table}"
        ).format(
            pt=pgsql.Identifier(self._pt_col),
            schema=pgsql.Identifier(self._schema),
            table=pgsql.Identifier(self._table),
        )
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                points = [r[0] for r in cur.fetchall()]

        tag_to_name: dict[str, str] = {}
        unrecognized = 0
        for tag in points:
            name = self._resolve_name(tag)
            if name:
                tag_to_name[tag] = name
            else:
                unrecognized += 1
        if unrecognized:
            logger.debug("build_tag_mapping: %d unrecognized tag formats skipped", unrecognized)
        return tag_to_name

    def _resolve_name(self, tag: str) -> Optional[str]:
        """Имя фичи для тега: сначала СЕМАНТИЧЕСКИЙ маппинг станции (суффикс → фича,
        напр. PD.PV → gas_pressure_out_gpa), иначе механический _normalize_tag."""
        mapping = getattr(self.cfg, "tag_mapping", None) or {}
        if mapping:
            m = re.match(r'(?:GPA|GT)-(\d+)\.(?:GPA|GT)-\1\.(.+)', tag, re.IGNORECASE)
            if m:
                gpa_num, suffix = m.group(1), m.group(2).strip()
                # маппинг кейс-нечувствителен по суффиксу
                feat = (mapping.get(suffix) or mapping.get(suffix.upper())
                        or self._mapping_ci(mapping, suffix))
                if feat:
                    return f"{feat}__GPA{gpa_num}"
        return self._normalize_tag(tag)

    @staticmethod
    def _mapping_ci(mapping: dict, suffix: str) -> Optional[str]:
        su = suffix.upper()
        for k, v in mapping.items():
            if k.upper() == su:
                return v
        return None

    @staticmethod
    def _normalize_tag(tag: str) -> Optional[str]:
        """GPA-1.GPA-1.SENSOR_NAME → sensor_name__GPA1"""
        m = re.match(r'GPA-(\d+)\.GPA-\1\.(.+)', tag, re.IGNORECASE)
        if not m:
            m = re.match(r'GT-(\d+)\.GT-\1\.(.+)', tag, re.IGNORECASE)
        if not m:
            return None
        gpa_num    = m.group(1)
        sensor_raw = m.group(2).lower().replace(' ', '_').replace('-', '_')
        sensor_raw = re.sub(r'_+', '_', sensor_raw).strip('_')
        return f"{sensor_raw}__GPA{gpa_num}"

    # ── Аномалии → БД ─────────────────────────────────────────────────────────

    def ensure_anomalies_table(self) -> None:
        """Создаёт таблицу аномалий, если не существует."""
        ddl = pgsql.SQL("""
        CREATE TABLE IF NOT EXISTS {schema}.anomalies (
            id            BIGSERIAL PRIMARY KEY,
            sensor_id     TEXT NOT NULL,
            event_ts      TIMESTAMPTZ NOT NULL,
            anomaly_type  SMALLINT NOT NULL,
            severity      TEXT,
            value         DOUBLE PRECISION,
            deviation     DOUBLE PRECISION,
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT anomalies_dedup
                UNIQUE (sensor_id, event_ts, anomaly_type)
        );
        """).format(schema=pgsql.Identifier(self._schema))
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()

    def save_anomalies(self, records: list[dict]) -> int:
        """
        Сохраняет аномалии в {schema}.anomalies.
        ON CONFLICT DO NOTHING — дедупликация на уровне БД.
        Возвращает количество реально вставленных строк.
        """
        if not records:
            return 0

        insert_sql = pgsql.SQL("""
        INSERT INTO {schema}.anomalies
            (sensor_id, event_ts, anomaly_type, severity, value, deviation)
        VALUES
            (%(sensor_id)s, %(event_ts)s, %(anomaly_type)s,
             %(severity)s,  %(value)s,    %(deviation)s)
        ON CONFLICT ON CONSTRAINT anomalies_dedup DO NOTHING
        """).format(schema=pgsql.Identifier(self._schema))

        rows = []
        for r in records:
            kind = r.get("kind", "ml")
            rows.append({
                "sensor_id":    r["sensor_id"],
                # event_ts — приоритет UTC-метке (как в журнале); timestamp (локальная
                # naive) только как fallback для legacy-вызовов. Запись TIMESTAMPTZ всегда
                # по исходному UTC → согласованность anomalies ↔ journal (инвариант ТЗ).
                "event_ts":     r.get("event_ts") or r.get("timestamp"),
                "anomaly_type": KIND_TO_CODE.get(kind, 1),
                "severity":     r.get("severity") or KIND_SEVERITY.get(kind, "info"),
                "value":        r.get("value"),
                "deviation":    r.get("deviation"),
            })

        return self._write_batches(insert_sql, rows)

    # ── anomalies_t: ПОЛНАЯ запись по аномалии (+ SHAP) ───────────────────────
    # Отдельная таблица (не трогаем legacy `anomalies`): здесь — всё про каждую
    # аномалию, в т.ч. локальная атрибуция SHAP (jsonb), коридор, z-score, метрики
    # модели на момент срабатывания. Дедуп тот же ключ (sensor_id, event_ts,
    # anomaly_type) → JOIN с anomalies/journal по этим полям возможен напрямую.
    def ensure_anomalies_t(self) -> None:
        """Создаёт расширенную таблицу аномалий anomalies_t (+ индексы), если нет."""
        sch = pgsql.Identifier(self._schema)
        ddl = pgsql.SQL("""
        CREATE TABLE IF NOT EXISTS {schema}.anomalies_t (
            id            BIGSERIAL PRIMARY KEY,
            station_id    TEXT,
            sensor_id     TEXT NOT NULL,
            sensor_name   TEXT,
            point         TEXT,
            gpa           TEXT,
            subsystem     TEXT,
            event_ts      TIMESTAMPTZ NOT NULL,
            ts_end        TIMESTAMPTZ,
            peak_ts       TIMESTAMPTZ,
            anomaly_type  SMALLINT NOT NULL,
            kind          TEXT,
            severity      TEXT,
            value         DOUBLE PRECISION,
            expected      DOUBLE PRECISION,
            deviation     DOUBLE PRECISION,
            residual      DOUBLE PRECISION,
            corridor_lo   DOUBLE PRECISION,
            corridor_hi   DOUBLE PRECISION,
            z_score       DOUBLE PRECISION,
            points        INTEGER,
            duration_min  DOUBLE PRECISION,
            r2_val        DOUBLE PRECISION,
            mae_val       DOUBLE PRECISION,
            nmae_val      DOUBLE PRECISION,
            n_sigma_cal   DOUBLE PRECISION,
            model_type    TEXT,
            shap_top      JSONB,
            message       TEXT,
            status        TEXT DEFAULT 'new',
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT anomalies_t_dedup
                UNIQUE (sensor_id, event_ts, anomaly_type)
        );
        """).format(schema=sch)
        idx = [
            pgsql.SQL("CREATE INDEX IF NOT EXISTS anomalies_t_station_ts_idx "
                      "ON {schema}.anomalies_t (station_id, event_ts DESC)").format(schema=sch),
            pgsql.SQL("CREATE INDEX IF NOT EXISTS anomalies_t_sensor_ts_idx "
                      "ON {schema}.anomalies_t (sensor_id, event_ts DESC)").format(schema=sch),
            pgsql.SQL("CREATE INDEX IF NOT EXISTS anomalies_t_kind_idx "
                      "ON {schema}.anomalies_t (kind)").format(schema=sch),
            pgsql.SQL("CREATE INDEX IF NOT EXISTS anomalies_t_severity_idx "
                      "ON {schema}.anomalies_t (severity)").format(schema=sch),
        ]
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                for q in idx:
                    cur.execute(q)
            conn.commit()

    # Колонки anomalies_t в порядке INSERT (без id/created_at — БД проставит сама).
    _ANOM_T_COLS = (
        "station_id", "sensor_id", "sensor_name", "point", "gpa", "subsystem",
        "event_ts", "ts_end", "peak_ts", "anomaly_type", "kind", "severity",
        "value", "expected", "deviation", "residual", "corridor_lo", "corridor_hi",
        "z_score", "points", "duration_min", "r2_val", "mae_val", "nmae_val",
        "n_sigma_cal", "model_type", "shap_top", "message", "status",
    )

    def save_anomalies_t(self, records: list[dict]) -> int:
        """Сохраняет полные записи аномалий (+ SHAP jsonb) в {schema}.anomalies_t.
        ON CONFLICT DO NOTHING по (sensor_id, event_ts, anomaly_type)."""
        if not records:
            return 0

        cols = self._ANOM_T_COLS
        col_sql = pgsql.SQL(", ").join(pgsql.Identifier(c) for c in cols)
        val_sql = pgsql.SQL(", ").join(pgsql.Placeholder(c) for c in cols)
        insert_sql = pgsql.SQL(
            "INSERT INTO {schema}.anomalies_t ({cols}) VALUES ({vals}) "
            "ON CONFLICT ON CONSTRAINT anomalies_t_dedup DO NOTHING"
        ).format(schema=pgsql.Identifier(self._schema), cols=col_sql, vals=val_sql)

        rows = []
        skipped_no_ts = 0
        for r in records:
            kind = r.get("kind", "ml")
            shap = r.get("shap_top")
            event_ts = r.get("event_ts") or r.get("timestamp")
            if event_ts is None:
                # event_ts NOT NULL: пропускаем битую запись, чтобы один плохой
                # элемент не уронил весь execute_batch-чанк (теряя валидные).
                skipped_no_ts += 1
                continue
            rows.append({
                "station_id":   r.get("station_id"),
                "sensor_id":    r["sensor_id"],
                "sensor_name":  r.get("sensor_name"),
                "point":        r.get("point"),
                "gpa":          r.get("gpa"),
                "subsystem":    r.get("subsystem"),
                "event_ts":     event_ts,
                "ts_end":       r.get("ts_end"),
                "peak_ts":      r.get("peak_ts"),
                "anomaly_type": KIND_TO_CODE.get(kind, 1),
                "kind":         kind,
                "severity":     r.get("severity") or KIND_SEVERITY.get(kind, "info"),
                "value":        r.get("value"),
                "expected":     r.get("expected"),
                "deviation":    r.get("deviation"),
                "residual":     r.get("residual"),
                "corridor_lo":  r.get("corridor_lo"),
                "corridor_hi":  r.get("corridor_hi"),
                "z_score":      r.get("z_score"),
                "points":       r.get("points"),
                "duration_min": r.get("duration_min"),
                "r2_val":       r.get("r2_val"),
                "mae_val":      r.get("mae_val"),
                "nmae_val":     r.get("nmae_val"),
                "n_sigma_cal":  r.get("n_sigma_cal"),
                "model_type":   r.get("model_type"),
                # jsonb: psycopg2.extras.Json адаптирует list/dict; None → SQL NULL
                "shap_top":     psycopg2.extras.Json(shap) if shap is not None else None,
                "message":      r.get("message"),
                "status":       r.get("status", "new"),
            })

        if skipped_no_ts:
            logger.warning("save_anomalies_t: пропущено %d записей без event_ts", skipped_no_ts)
        return self._write_batches(insert_sql, rows)

    # ── Доменные фичи ({schema}.domain, WIDE: datetime, gpa, фичи) ──────────────
    def ensure_domain(self, feats: list[str]) -> None:
        """Создаёт/дополняет {schema}.domain (datetime, gpa + колонки фич + PK). Идемпотентно."""
        sch = pgsql.Identifier(self._schema)
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(pgsql.SQL(
                    "CREATE TABLE IF NOT EXISTS {s}.domain (datetime TIMESTAMPTZ, gpa SMALLINT)"
                ).format(s=sch))
                for f in feats:
                    cur.execute(pgsql.SQL('ALTER TABLE {s}.domain ADD COLUMN IF NOT EXISTS {c} DOUBLE PRECISION')
                                .format(s=sch, c=pgsql.Identifier(f)))
                cur.execute("SELECT 1 FROM pg_constraint WHERE conrelid=(%s||'.domain')::regclass AND contype='p'",
                            (self._schema,))
                if not cur.fetchone():
                    cur.execute(pgsql.SQL("ALTER TABLE {s}.domain ADD CONSTRAINT domain_pk PRIMARY KEY (datetime, gpa)")
                                .format(s=sch))
            conn.commit()

    def save_domain(self, rows: list[tuple], feats: list[str]) -> int:
        """Upsert WIDE-строк доменных фич: (datetime_utc, gpa, *значения_feats).
        ON CONFLICT (datetime, gpa) DO NOTHING. Гард CS_DISABLE_DB_WRITE (read-only в тестах)."""
        if not rows:
            return 0
        if os.environ.get("CS_DISABLE_DB_WRITE"):
            logger.warning("CS_DISABLE_DB_WRITE: save_domain %d строк подавлено", len(rows))
            return 0
        cols = pgsql.SQL(", ").join([pgsql.Identifier("datetime"), pgsql.Identifier("gpa")]
                                    + [pgsql.Identifier(f) for f in feats])
        insert = pgsql.SQL("INSERT INTO {s}.domain ({cols}) VALUES %s "
                           "ON CONFLICT (datetime, gpa) DO NOTHING").format(
            s=pgsql.Identifier(self._schema), cols=cols)
        written = 0
        for i in range(0, len(rows), 5000):
            chunk = rows[i:i + 5000]
            for attempt in range(3):
                try:
                    with get_db_connection(self.cfg) as conn:
                        with conn.cursor() as cur:
                            psycopg2.extras.execute_values(cur, insert.as_string(conn), chunk, page_size=1000)
                        conn.commit()
                    written += len(chunk)
                    break
                except (psycopg2.OperationalError, psycopg2.InterfaceError):
                    from station_config import reset_pool
                    try: reset_pool(self.cfg)
                    except Exception: pass
                    if attempt == 2:
                        raise
        return written

    # ── Серия прогноза/коридора ({schema}.predictions) ─────────────────────────
    def ensure_predictions(self) -> None:
        """Создаёт {schema}.predictions (серия модели: p/lo/hi на 5-мин сетке). Идемпотентно.
        PK (sensor_id, datetime) = уникальность + индекс под оконное чтение графика;
        отдельный (datetime) — под ретеншн-DELETE."""
        sch = pgsql.Identifier(self._schema)
        ddl = pgsql.SQL("""
        CREATE TABLE IF NOT EXISTS {s}.predictions (
            station_id TEXT,
            sensor_id  TEXT NOT NULL,
            datetime   TIMESTAMPTZ NOT NULL,
            prediction DOUBLE PRECISION,
            lo         DOUBLE PRECISION,
            hi         DOUBLE PRECISION,
            lo2        DOUBLE PRECISION,
            hi2        DOUBLE PRECISION,
            e          DOUBLE PRECISION,
            e_thr      DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT predictions_pk PRIMARY KEY (sensor_id, datetime)
        );
        """).format(s=sch)
        idx = pgsql.SQL("CREATE INDEX IF NOT EXISTS predictions_dt_idx ON {s}.predictions (datetime)").format(s=sch)
        # эпистемика (e/e_thr) + альтернативный коридор (lo2/hi2, hybrid для UI-тумблера) —
        # добавляются к уже существующей таблице прода без пересоздания (история p/lo/hi не трогается).
        alters = [
            pgsql.SQL("ALTER TABLE {s}.predictions ADD COLUMN IF NOT EXISTS lo2 DOUBLE PRECISION").format(s=sch),
            pgsql.SQL("ALTER TABLE {s}.predictions ADD COLUMN IF NOT EXISTS hi2 DOUBLE PRECISION").format(s=sch),
            pgsql.SQL("ALTER TABLE {s}.predictions ADD COLUMN IF NOT EXISTS e DOUBLE PRECISION").format(s=sch),
            pgsql.SQL("ALTER TABLE {s}.predictions ADD COLUMN IF NOT EXISTS e_thr DOUBLE PRECISION").format(s=sch),
        ]
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute(idx)
                for a in alters:
                    cur.execute(a)
            conn.commit()

    def save_predictions(self, rows: list[tuple]) -> int:
        """Upsert серии: (station_id, sensor_id, datetime_utc, prediction, lo, hi, lo2, hi2, e, e_thr).
        ON CONFLICT (sensor_id, datetime) DO UPDATE — хвост корректируется (гашение
        могло «доехать» позже). p/lo/hi/lo2/hi2/e=None → NULL (стоянка/OOD/train). lo/hi = активный
        коридор, lo2/hi2 = альтернативный (hybrid, UI-тумблер), e = u_epi, e_thr = порог κ·1.5.
        Гард CS_DISABLE_DB_WRITE."""
        if not rows:
            return 0
        if os.environ.get("CS_DISABLE_DB_WRITE"):
            logger.warning("CS_DISABLE_DB_WRITE: save_predictions %d строк подавлено", len(rows))
            return 0
        insert = pgsql.SQL(
            "INSERT INTO {s}.predictions (station_id, sensor_id, datetime, prediction, lo, hi, lo2, hi2, e, e_thr) VALUES %s "
            "ON CONFLICT (sensor_id, datetime) DO UPDATE SET "
            "prediction = EXCLUDED.prediction, lo = EXCLUDED.lo, hi = EXCLUDED.hi, "
            "lo2 = EXCLUDED.lo2, hi2 = EXCLUDED.hi2, e = EXCLUDED.e, e_thr = EXCLUDED.e_thr"
        ).format(s=pgsql.Identifier(self._schema))
        written = 0
        for i in range(0, len(rows), 5000):
            chunk = rows[i:i + 5000]
            for attempt in range(3):
                try:
                    with get_db_connection(self.cfg) as conn:
                        with conn.cursor() as cur:
                            psycopg2.extras.execute_values(cur, insert.as_string(conn), chunk, page_size=1000)
                        conn.commit()
                    written += len(chunk)
                    break
                except (psycopg2.OperationalError, psycopg2.InterfaceError):
                    from station_config import reset_pool
                    try: reset_pool(self.cfg)
                    except Exception: pass
                    if attempt == 2:
                        raise
        return written

    def get_last_prediction_ts(self, sensor_ids=None) -> dict:
        """{sensor_id: max(datetime) aware-UTC} — курсор инкрементальной записи серии.
        Пустой dict, если таблицы/строк ещё нет (первый прогон)."""
        try:
            if sensor_ids:
                sql = pgsql.SQL("SELECT sensor_id, max(datetime) FROM {s}.predictions "
                                "WHERE sensor_id = ANY(%s) GROUP BY sensor_id").format(s=pgsql.Identifier(self._schema))
                params: tuple = (list(sensor_ids),)
            else:
                sql = pgsql.SQL("SELECT sensor_id, max(datetime) FROM {s}.predictions GROUP BY sensor_id").format(
                    s=pgsql.Identifier(self._schema))
                params = None
            with get_db_connection(self.cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
            out: dict = {}
            for sid, ts in rows:
                if ts is not None:
                    t = pd.Timestamp(ts)
                    out[sid] = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
            return out
        except Exception:
            logger.debug("get_last_prediction_ts: таблицы predictions ещё нет", exc_info=True)
            return {}

    def prune_predictions(self, retention_days: int = 60) -> int:
        """Ретеншн: удалить серию старше retention_days. Гард CS_DISABLE_DB_WRITE."""
        if os.environ.get("CS_DISABLE_DB_WRITE"):
            return 0
        sql = pgsql.SQL("DELETE FROM {s}.predictions WHERE datetime < NOW() - make_interval(days => %s)").format(
            s=pgsql.Identifier(self._schema))
        try:
            with get_db_connection(self.cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (int(retention_days),))
                    n = cur.rowcount
                conn.commit()
            return n
        except Exception:
            logger.warning("prune_predictions не выполнен", exc_info=True)
            return 0

    # ── Системное структурное здоровье агрегата ({schema}.system_health_t) ──────
    def ensure_system_health(self) -> None:
        """Создаёт {schema}.system_health_t — unit-level индекс структурного здоровья
        (PCA/SPE-T² монитор). Одна строка на (агрегат, момент прогона). Идемпотентно."""
        sch = pgsql.Identifier(self._schema)
        ddl = pgsql.SQL("""
        CREATE TABLE IF NOT EXISTS {s}.system_health_t (
            station_id        TEXT,
            gpa_id            TEXT NOT NULL,
            ts                TIMESTAMPTZ NOT NULL,
            spe_exceed        DOUBLE PRECISION,
            t2_exceed         DOUBLE PRECISION,
            spe_ratio         DOUBLE PRECISION,
            excess_over_fleet DOUBLE PRECISION,
            verdict           TEXT,
            top_contrib       TEXT,
            n_recent          INTEGER,
            created_at        TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT system_health_t_pk PRIMARY KEY (gpa_id, ts)
        );
        """).format(s=sch)
        idx = pgsql.SQL("CREATE INDEX IF NOT EXISTS system_health_t_ts_idx "
                        "ON {s}.system_health_t (ts)").format(s=sch)
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute(idx)
            conn.commit()

    def save_system_health(self, rows: list[tuple]) -> int:
        """Upsert unit-level здоровья: (station_id, gpa_id, ts_utc, spe_exceed, t2_exceed,
        spe_ratio, excess_over_fleet, verdict, top_contrib_json, n_recent).
        ON CONFLICT (gpa_id, ts) DO UPDATE. Гард CS_DISABLE_DB_WRITE."""
        if not rows:
            return 0
        if os.environ.get("CS_DISABLE_DB_WRITE"):
            logger.warning("CS_DISABLE_DB_WRITE: save_system_health %d строк подавлено", len(rows))
            return 0
        self.ensure_system_health()
        insert = pgsql.SQL(
            "INSERT INTO {s}.system_health_t (station_id, gpa_id, ts, spe_exceed, t2_exceed, "
            "spe_ratio, excess_over_fleet, verdict, top_contrib, n_recent) VALUES %s "
            "ON CONFLICT ON CONSTRAINT system_health_t_pk DO UPDATE SET "
            "spe_exceed=EXCLUDED.spe_exceed, t2_exceed=EXCLUDED.t2_exceed, spe_ratio=EXCLUDED.spe_ratio, "
            "excess_over_fleet=EXCLUDED.excess_over_fleet, verdict=EXCLUDED.verdict, "
            "top_contrib=EXCLUDED.top_contrib, n_recent=EXCLUDED.n_recent"
        ).format(s=pgsql.Identifier(self._schema))
        for attempt in range(3):
            try:
                with get_db_connection(self.cfg) as conn:
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_values(cur, insert.as_string(conn), rows, page_size=100)
                    conn.commit()
                return len(rows)
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                from station_config import reset_pool
                try: reset_pool(self.cfg)
                except Exception: pass
                if attempt == 2:
                    raise
        return 0

    # ── Пользовательские наборы графиков ({schema}.set_of_graphs) ───────────────
    def ensure_set_of_graphs(self) -> None:
        """Создаёт таблицу сохранённых наборов датчиков (+ индекс), если её нет.
        Набор уникален в рамках (станция, владелец, имя)."""
        sch = pgsql.Identifier(self._schema)
        ddl = pgsql.SQL("""
        CREATE TABLE IF NOT EXISTS {schema}.set_of_graphs (
            id          BIGSERIAL PRIMARY KEY,
            station_id  TEXT,
            owner       TEXT NOT NULL,
            name        TEXT NOT NULL,
            sensor_ids  JSONB NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT set_of_graphs_uq UNIQUE (station_id, owner, name)
        );
        """).format(schema=sch)
        idx = pgsql.SQL("CREATE INDEX IF NOT EXISTS set_of_graphs_owner_idx "
                        "ON {schema}.set_of_graphs (station_id, owner)").format(schema=sch)
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute(idx)
            conn.commit()

    def list_graph_sets(self, station_id: str, owner: str) -> list[dict]:
        self.ensure_set_of_graphs()
        sql = pgsql.SQL(
            "SELECT id, name, sensor_ids, updated_at::text FROM {schema}.set_of_graphs "
            "WHERE station_id = %s AND owner = %s ORDER BY lower(name)"
        ).format(schema=pgsql.Identifier(self._schema))
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (station_id, owner))
                rows = cur.fetchall()
        return [{"id": int(r[0]), "name": r[1], "sensor_ids": r[2] or [], "updated_at": r[3]} for r in rows]

    def save_graph_set(self, station_id: str, owner: str, name: str, sensor_ids: list) -> int:
        """Создаёт/обновляет набор (upsert по station+owner+name). Возвращает id."""
        self.ensure_set_of_graphs()
        sql = pgsql.SQL(
            "INSERT INTO {schema}.set_of_graphs (station_id, owner, name, sensor_ids) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT ON CONSTRAINT set_of_graphs_uq "
            "DO UPDATE SET sensor_ids = EXCLUDED.sensor_ids, updated_at = NOW() RETURNING id"
        ).format(schema=pgsql.Identifier(self._schema))
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (station_id, owner, name, psycopg2.extras.Json(list(sensor_ids))))
                new_id = cur.fetchone()[0]
            conn.commit()
        return int(new_id)

    def delete_graph_set(self, station_id: str, owner: str, set_id: int) -> bool:
        self.ensure_set_of_graphs()
        sql = pgsql.SQL(
            "DELETE FROM {schema}.set_of_graphs WHERE id = %s AND station_id = %s AND owner = %s"
        ).format(schema=pgsql.Identifier(self._schema))
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (set_id, station_id, owner))
                deleted = cur.rowcount
            conn.commit()
        return deleted > 0

    # ── raw_data.health ───────────────────────────────────────────────────────

    def get_last_health_ts(self) -> Optional[pd.Timestamp]:
        """Метка последнего РАЗМЕЧЕННОГО предикта: max(datetime) где health IS NOT NULL.

        Это сплошной курсор «докуда уже посчитано» — в отличие от max(event_ts)
        в anomalies, куда пишутся ТОЛЬКО точки с аномалиями (норма не пишется, и
        как указатель прогресса он бы промахнулся). Используется catch_up_missing
        на старте, чтобы догнать пропуск ОКОННЫМИ запросами от этой метки до now.
        Возвращает aware UTC Timestamp или None (health ещё нигде не размечен).

        ВАЖНО: считаем только НАСТОЯЩИЕ ML-метки — '0' (норма), 'S' (остановлен)
        или цифровые коды детекторов ('1', '4', '1,4'). В raw_data попадает легаси-
        значение 'true' (исходная BOOLEAN-колонка health, мигрированная в TEXT;
        ingestion продолжает писать его в свежие строки) — оно НЕ означает, что
        предикт посчитан, и без фильтра курсор всегда был бы ≈ now() → догон не
        срабатывал бы никогда. Регэксп `^(S|[0-9]+(,[0-9]+)*)$` отсекает true/false.
        """
        sql = pgsql.SQL(
            "SELECT max({dt}) FROM {schema}.{table} "
            "WHERE {health} ~ '^(S|[0-9]+(,[0-9]+)*)$'"
        ).format(
            dt=pgsql.Identifier(self._dt_col),
            schema=pgsql.Identifier(self._schema),
            table=pgsql.Identifier(self._table),
            health=pgsql.Identifier("health"),
        )
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
        if not row or row[0] is None:
            return None
        ts = pd.Timestamp(row[0])
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    def db_now(self) -> pd.Timestamp:
        """Текущее время сервера БД (aware UTC). Верхнюю границу догона берём
        отсюда, а не с часов ноутбука: единый лаунчер крутится на ноутбуке, чьи
        часы могут плыть/отставать, а вся выборка идёт по оси datetime сервера."""
        with get_db_connection(self.cfg) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT now()")
                row = cur.fetchone()
        ts = pd.Timestamp(row[0])
        return ts.tz_convert("UTC") if ts.tzinfo is not None else ts.tz_localize("UTC")

    def update_health(self, rows: list[dict]) -> int:
        """
        Пишет коды аномалий в {schema}.raw_data.health.

        rows: список dict {point, health, t0, t1}, где
          - health: строка (отсортированные коды через запятую "1,4", "0"=норма);
          - t0/t1: aware UTC datetime — границы 5-мин бакета [t0, t1)
            (исходные метки raw_data нерегулярны/суб-минутны, поэтому матчим
            диапазоном, а не точным datetime).
        Возвращает число обработанных строк-запросов (не строк БД).
        """
        if not rows:
            return 0

        update_sql = pgsql.SQL(
            "UPDATE {schema}.{table} SET {health} = %(health)s"
            " WHERE {pt} = %(point)s AND {dt} >= %(t0)s AND {dt} < %(t1)s"
        ).format(
            schema=pgsql.Identifier(self._schema),
            table=pgsql.Identifier(self._table),
            health=pgsql.Identifier("health"),
            pt=pgsql.Identifier(self._pt_col),
            dt=pgsql.Identifier(self._dt_col),
        )

        # Почанковый коммит + устойчивость к обрыву сети (медленная удалённая БД).
        return self._write_batches(update_sql, rows)

    # ── Журнал уведомлений → БД ─────────────────────────────────────────────────

    def save_notifications(self, records: list[dict]) -> int:
        """
        Сохраняет уведомления в {schema}."journal notifications".
        Имя таблицы — из global.yaml через sql.Identifier (может содержать пробел).
        ON CONFLICT DO NOTHING — дедуп по (sensor_id, event_ts, anomaly_type).
        records: dict с ключами station_id, sensor_id, point, gpa, event_ts,
                 anomaly_type, kind, severity, value, deviation, message, status.
        Возвращает число обработанных записей.
        """
        if not records:
            return 0

        table = journal_table_name()
        insert_sql = pgsql.SQL("""
        INSERT INTO {schema}.{table}
            (station_id, sensor_id, point, gpa, event_ts, anomaly_type,
             kind, severity, value, deviation, message, status)
        VALUES
            (%(station_id)s, %(sensor_id)s, %(point)s, %(gpa)s, %(event_ts)s,
             %(anomaly_type)s, %(kind)s, %(severity)s, %(value)s, %(deviation)s,
             %(message)s, %(status)s)
        ON CONFLICT ON CONSTRAINT journal_notifications_dedup DO NOTHING
        """).format(
            schema=pgsql.Identifier(self._schema),
            table=pgsql.Identifier(table),
        )

        rows = []
        for r in records:
            kind = r.get("kind", "ml")
            rows.append({
                "station_id":   r.get("station_id") or self.cfg.station_id,
                "sensor_id":    r["sensor_id"],
                "point":        r.get("point"),
                "gpa":          r.get("gpa"),
                "event_ts":     r.get("event_ts") or r.get("timestamp"),
                "anomaly_type": KIND_TO_CODE.get(kind, 1),
                "kind":         kind,
                "severity":     r.get("severity") or KIND_SEVERITY.get(kind, "info"),
                "value":        r.get("value"),
                "deviation":    r.get("deviation"),
                "message":      r.get("message") or r.get("description") or kind,
                "status":       r.get("status", "new"),
            })

        return self._write_batches(insert_sql, rows)
