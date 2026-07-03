"""Smoke-тесты: импорты, single_instance, health-логика. Запуск: pytest tests/"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_imports():
    import main            # noqa: F401 — FastAPI app собирается без ошибок
    import station_config  # noqa: F401
    import data_loader     # noqa: F401
    import anomaly_types   # noqa: F401
    import logging_config  # noqa: F401


def test_anomaly_types_consistency():
    from anomaly_types import CODE_TO_KIND, KIND_TO_CODE, KIND_SEVERITY
    assert set(CODE_TO_KIND.values()) == set(KIND_TO_CODE.keys())
    for kind in CODE_TO_KIND.values():
        assert kind in KIND_SEVERITY


def test_station_id_validation():
    import pytest
    from station_config import load_station_config
    with pytest.raises(ValueError):
        load_station_config("../etc/passwd")
    with pytest.raises(ValueError):
        load_station_config("a;DROP TABLE")


def test_single_instance_lock():
    from logging_config import single_instance, _lock_handles
    assert single_instance("_pytest_lock") is True
    # повторный захват в том же процессе того же lock — отказ или идемпотентность,
    # главное: хендл существует
    assert "_pytest_lock" in _lock_handles


def test_severity_rank():
    from live_predict import _severity_rank
    assert _severity_rank(["ml"]) == "crit"
    assert _severity_rank(["frozen"]) == "warn"
    assert _severity_rank(["seasonal"]) == "info"
    assert _severity_rank([]) == "ok"


# ── Новые модули (Ф1/Ф4/Ф12) ──────────────────────────────────────────────────

def test_new_modules_import():
    import migrate_db        # noqa: F401
    import backfill_health   # noqa: F401
    import run_system        # noqa: F401


def test_global_config_journal_table():
    from station_config import journal_table_name, load_global_config
    assert journal_table_name() == load_global_config()["journal_table"]
    assert isinstance(journal_table_name(), str) and journal_table_name()


# ── Часовые пояса: локальное naive (Etc/GMT-5) ↔ UTC (Ф2, золотое правило #4) ──

def test_local_naive_to_utc():
    import pandas as pd
    from live_predict import _local_naive_to_utc
    u = _local_naive_to_utc(pd.Timestamp("2026-06-13 12:00:00"))
    assert str(u) == "2026-06-13 07:00:00+00:00"   # +5 зона → −5ч в UTC


# ── Сериализация health-кодов + 5-мин бакет (Ф2) ──────────────────────────────

def test_collect_health_rows_serialization():
    import numpy as np, pandas as pd
    from live_predict import _collect_health_rows
    idx = pd.date_range("2026-06-13 11:00", periods=4, freq="5min")  # naive local
    r = {
        "times": idx,
        "ml_mask":  pd.Series([False, True, False, False], index=idx),
        "roc_mask": pd.Series([False, True, False, False], index=idx),
    }
    meta = {"last_train_timestamp": "2026-06-13 10:00:00+05:00",
            "name_to_tag": {"sensor__GPA1": "GPA-1.GPA-1.X.PV"}}
    rows = _collect_health_rows({"sensor__GPA1": r}, meta)
    assert len(rows) == 4
    healths = [row["health"] for row in rows]
    assert healths[1] == "1,4"                         # ml(1)+roc(4), отсортировано
    assert set(healths) - {"1,4"} == {"0"}             # остальные — норма
    # бакет ±2.5 мин и UTC-aware
    span = (rows[0]["t1"] - rows[0]["t0"]).total_seconds()
    assert span == 300 and rows[0]["t0"].tzinfo is not None


def test_collect_health_rows_stopped_code():
    """Остановленный ГПА (running<0.5, без кодов) → HEALTH_STOPPED ('8'),
    приоритет: аномалии → остановлен → норма."""
    import numpy as np, pandas as pd
    from live_predict import _collect_health_rows, HEALTH_STOPPED, HEALTH_OK
    idx = pd.date_range("2026-06-13 11:00", periods=3, freq="5min")
    r = {
        "times": idx,
        "ml_mask": pd.Series([True, False, False], index=idx),   # 0: аномалия даже на стопе
        "running": np.array([0.0, 0.0, 1.0]),                    # 0,1 стоп; 2 работа
    }
    meta = {"last_train_timestamp": "2026-06-13 10:00:00+05:00",
            "name_to_tag": {"s__GPA1": "GPA-1.GPA-1.X.PV"}}
    healths = [x["health"] for x in _collect_health_rows({"s__GPA1": r}, meta)]
    assert healths == ["1", HEALTH_STOPPED, HEALTH_OK]   # аномалия > стоп > норма


# ── sensor_id ↔ point (Ф2/Ф3) ─────────────────────────────────────────────────

def test_sensor_id_to_point_mapping():
    from data_loader import PostgresDataLoader
    # tag → feature_name: GPA-тег распознан и заканчивается на __GPA<n>, мусор → None
    norm = PostgresDataLoader._normalize_tag("GPA-1.GPA-1.PD.PV")
    assert norm is not None and norm.endswith("__GPA1")
    assert norm.split("__")[0] == norm.split("__")[0].lower()   # имя сенсора в нижнем регистре
    assert PostgresDataLoader._normalize_tag("garbage") is None
    # _build_notifications кладёт point из name_to_tag
    from live_predict import _build_notifications
    meta = {"name_to_tag": {"pd__GPA1": "GPA-1.GPA-1.PD.PV"}, "station_id": "ohangaron"}
    ev = {"sensor_id": "pd__GPA1", "sensor_name": "pd", "gpa": "GPA1", "kind": "ml",
          "timestamp": "2026-06-13T12:00:00", "severity": "crit", "value": 1.0, "deviation": 5.0}
    notif = _build_notifications([ev], meta)
    assert notif and notif[0]["point"] == "GPA-1.GPA-1.PD.PV"
    assert str(notif[0]["event_ts"]) == "2026-06-13 07:00:00+00:00"   # local→UTC
    assert notif[0]["message"]                                        # человекочитаемый текст есть


# ── Срез прогноза по train_ts (Ф8): ISO-строки сравнимы хронологически ─────────

def test_train_ts_iso_lexicographic_order():
    # эндпоинт сравнивает строки t (%Y-%m-%dT%H:%M:%S) с train_ts — лексикографика
    # обязана совпадать с хронологией
    assert ("2026-06-12T10:00:00" > "2026-06-12T09:55:00")
    assert not ("2026-06-12T09:00:00" > "2026-06-12T10:00:00")


# ── Даунсемплинг графиков ≤ CHART_TARGET_POINTS (Ф9/Ф10) ──────────────────────

def test_chart_bucket_caps_points():
    import main
    cap = main.CHART_TARGET_POINTS
    # для диапазонов 1ч..365д бакет обязан удерживать число точек <= cap
    for days in (0.04, 1, 7, 30, 90, 365):
        range_s = days * 86400
        bucket = main._chart_bucket_seconds(range_s)
        assert bucket >= 300 and bucket % 300 == 0
        assert range_s / bucket <= cap + 1


def test_chart_multi_points_le_1500_DB():
    import pytest
    if not _db_available():
        pytest.skip("БД недоступна")
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)
    sensors = main._sensors_list("ohangaron")
    if len(sensors) < 2:
        pytest.skip("мало датчиков")
    ids = ",".join(s["id"] for s in sensors[:4])
    resp = client.get(f"/api/stations/ohangaron/chart/multi", params={"sensors": ids, "days": 30})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list) and data
    for item in data:
        assert len(item["series"]) <= main.CHART_TARGET_POINTS


# ── Интеграция (нужна БД): миграция → live cycle → согласованность ─────────────

def _db_available():
    try:
        from station_config import load_station_config, get_db_connection
        cfg = load_station_config("ohangaron")
        with get_db_connection(cfg, acquire_retries=1) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() is not None
    except Exception:
        return False


def test_integration_health_consistency():
    import pytest
    if not _db_available():
        pytest.skip("БД недоступна — интеграционный тест пропущен")
    import migrate_db
    from station_config import load_station_config, get_db_connection
    migrate_db.migrate("ohangaron")
    cfg = load_station_config("ohangaron")
    schema = cfg.db["schema"]
    with get_db_connection(cfg) as conn:
        with conn.cursor() as cur:
            # health стал TEXT
            cur.execute("SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name='raw_data' AND column_name='health'",
                        (schema,))
            assert cur.fetchone()[0] == "text"
            # журнал существует с нужными столбцами
            cur.execute("SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name=%s", (schema, "journal notifications"))
            assert cur.fetchone()[0] >= 10
