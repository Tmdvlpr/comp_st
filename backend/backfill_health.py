"""
Однократный бэкафилл raw_data.health по всей истории.

Прогоняет те же детекторы, что и онлайн-цикл (live_predict._compute_results),
окнами по --window-days с перекрытием (для lag/rolling/roc), и пишет коды
аномалий ("1,4") либо "0" (оценено, аномалий нет) в raw_data.health.
Точки, которые детектор не смог оценить (нет фич / простой+подавление), остаются NULL.

Семантика по периодам (модель обучена до last_train_timestamp):
  • точки ПОСЛЕ обучения — полноценная детекция (совпадает с live_predict);
  • точки обучающего периода — масок нет (is_live-гейт) → "0" (нормальный
    базис, на котором обучалась модель).

Запуск:
    python backfill_health.py --station ohangaron
    python backfill_health.py --station ohangaron --from 2026-02-01 --to 2026-06-12 --window-days 30
    python backfill_health.py --station ohangaron --with-journal     # ещё и в журнал/anomalies
Идемпотентно: повторный запуск перезаписывает те же health теми же значениями.
"""
from __future__ import annotations
import argparse
import gc
import logging

import numpy as np
import pandas as pd

import live_predict as lp
from data_loader import PostgresDataLoader
from station_config import get_db_connection

logger = logging.getLogger("backfill_health")


def _collect_window_health(results, metadata, a_loc, b_loc):
    """health-строки для точек в [a_loc, b_loc) (локальное naive время).
    Коды сработавших масок → "1,4", иначе "0". UTC-бакет ±2.5 мин."""
    name_to_tag = metadata.get("name_to_tag", {})
    rows = []
    for s, r in results.items():
        point = name_to_tag.get(s)
        if not point:
            continue
        times = r["times"]
        if len(times) == 0:
            continue
        masks = {kind: np.asarray(r.get(mk)).astype(bool)
                 for mk, kind in lp._MASK_TO_KIND.items() if r.get(mk) is not None}
        running = np.asarray(r.get('running', np.ones(len(times))), dtype=float)
        for i in range(len(times)):
            ti = times[i]
            if ti < a_loc or ti >= b_loc:
                continue
            codes = sorted(lp.KIND_TO_CODE[k] for k, arr in masks.items()
                           if i < len(arr) and arr[i])
            # приоритет: аномалии → остановлен (HEALTH_STOPPED) → норма (HEALTH_OK)
            if codes:
                health = ",".join(str(c) for c in codes)
            elif i < len(running) and running[i] < 0.5:
                health = lp.HEALTH_STOPPED
            else:
                health = lp.HEALTH_OK
            utc = lp._local_naive_to_utc(ti)
            rows.append({
                "point":  point,
                "health": health,
                "t0":     (utc - lp._HEALTH_BUCKET).to_pydatetime(),
                "t1":     (utc + lp._HEALTH_BUCKET).to_pydatetime(),
            })
    return rows


def _collect_window_notifications(results, metadata, a_loc, b_loc, station_id):
    """Эпизоды аномалий → записи журнала (компактная эпизодизация: разрыв <=2 тика
    = один эпизод). Только для точек в [a_loc, b_loc)."""
    name_to_tag = metadata.get("name_to_tag", {})
    notif = []
    for s, r in results.items():
        point = name_to_tag.get(s)
        times = r["times"]
        if len(times) == 0:
            continue
        parts = s.rsplit("__", 1)
        name = parts[0]
        gpa = parts[1] if len(parts) > 1 else "GPA1"
        reality = np.asarray(r["reality"], dtype=float)
        prediction = np.asarray(r["prediction"], dtype=float)
        in_win = np.asarray((times >= a_loc) & (times < b_loc))
        for mk, kind in lp._MASK_TO_KIND.items():
            m = r.get(mk)
            if m is None:
                continue
            arr = np.asarray(m).astype(bool) & in_win
            if not arr.any():
                continue
            idx = np.where(arr)[0]
            breaks = np.where(np.diff(idx) > 2)[0]
            for ep in np.split(idx, breaks + 1):
                if len(ep) == 0:
                    continue
                i0 = int(ep[0])
                seg_resid = np.abs(reality[ep] - prediction[ep])
                ipk = int(ep[int(np.argmax(seg_resid))])
                val = round(float(reality[ipk]), 3) if ipk < len(reality) else None
                prd = round(float(prediction[ipk]), 3) if ipk < len(prediction) else None
                dev = round((val - prd) / abs(prd) * 100, 2) if (val and prd and abs(prd) > 1e-9) else None
                ev_utc = lp._local_naive_to_utc(times[i0]).to_pydatetime()
                label = lp._KIND_LABEL_RU.get(kind, kind)
                msg = f"{name.replace('_', ' ')} (ГПА-{gpa.replace('GPA', '')}): {label}"
                if val is not None:
                    msg += f", значение {val}"
                if dev is not None:
                    msg += f", отклонение {dev}%"
                notif.append({
                    "station_id": station_id, "sensor_id": s, "sensor_name": name,
                    "point": point, "gpa": gpa, "event_ts": ev_utc, "timestamp": ev_utc,
                    "kind": kind, "severity": lp._KIND_SEVERITY[kind],
                    "value": val, "deviation": dev, "message": msg, "status": "new",
                })
    return notif


def backfill(station, from_date, to_date, window_days, overlap_days, with_journal,
             should_stop=None):
    """should_stop (опц.) — callable() -> bool: если вернёт True, цикл досрочно
    завершается после текущего окна. Нужен для кооперативной остановки догона на
    старте (catch_up_missing) по Ctrl+C, чтобы не висеть до kill-таймаута."""
    # Не пересоздаём глобалы, если станция уже инициализирована (catch_up_missing
    # вызывает backfill из уже поднятого процесса). CLI стартует с нуля → init.
    if lp._station_cfg is None or lp._station_cfg.station_id != station:
        lp._init_station(station)
    metadata, models = lp.load_models_and_metadata()
    cfg = lp._station_cfg
    loader: PostgresDataLoader = lp._loader
    tag_to_name = metadata["tag_to_name"]

    # Границы: from_date по умолчанию — минимум datetime в БД
    if from_date:
        start = pd.Timestamp(from_date, tz="UTC")
    else:
        with get_db_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(f'SELECT min(datetime) FROM {cfg.db["schema"]}.raw_data')
                start = pd.Timestamp(cur.fetchone()[0]).tz_convert("UTC")
    end = pd.Timestamp(to_date, tz="UTC") if to_date else pd.Timestamp.now(tz="UTC")
    win = pd.Timedelta(days=window_days)
    overlap = pd.Timedelta(days=max(overlap_days, 1))

    print(f"🔁 Бэкафилл health: {start} → {end}, окно {window_days}д, "
          f"перекрытие {overlap_days}д, журнал={'да' if with_journal else 'нет'}", flush=True)

    a = start
    total_health = total_notif = total_windows = 0
    while a < end:
        if should_stop is not None and should_stop():
            print("⏹  Догон прерван по запросу остановки (завершаю на текущей границе)", flush=True)
            break
        b = min(a + win, end)
        load_from = a - overlap
        try:
            raw = loader.fetch_raw_window(load_from.isoformat(), b.isoformat())
        except Exception:
            logger.exception("окно %s..%s: загрузка не удалась — пропуск", a.date(), b.date())
            a = b
            continue
        if raw.empty:
            print(f"  • {a.date()}..{b.date()}: данных нет", flush=True)
            a = b
            continue

        # Тяжёлые шаги (wide + детекторы + запись) — под try/except: сбой/MemoryError
        # одного окна НЕ должен ронять весь бэкафилл (требование устойчивости к OOM).
        # Онлайн-цикл run_continuous ловит MemoryError так же.
        df_wide = results = None
        try:
            df_wide = lp.prepare_wide_data(raw, tag_to_name)
            if not df_wide.empty:
                results = lp._compute_results(df_wide, metadata, models)

                a_loc = a.tz_convert("Etc/GMT-5").tz_localize(None)
                b_loc = b.tz_convert("Etc/GMT-5").tz_localize(None)

                health_rows = _collect_window_health(results, metadata, a_loc, b_loc)
                if health_rows:
                    loader.update_health(health_rows)
                    total_health += len(health_rows)

                notif = []
                if with_journal:
                    notif = _collect_window_notifications(results, metadata, a_loc, b_loc, cfg.station_id)
                    if notif:
                        loader.save_anomalies(notif)
                        loader.save_notifications(notif)
                        total_notif += len(notif)

                total_windows += 1
                print(f"  ✓ {a.date()}..{b.date()}: health={len(health_rows)}"
                      + (f", журнал={len(notif)}" if with_journal else ""), flush=True)
        except MemoryError:
            logger.exception("окно %s..%s: MemoryError — пропуск окна, продолжаю", a.date(), b.date())
        except Exception:
            logger.exception("окно %s..%s: обработка не удалась — пропуск", a.date(), b.date())
        finally:
            # освобождаем память окна перед следующей итерацией (устойчивость к OOM)
            df_wide = results = raw = None
            gc.collect()
        a = b

    print(f"🎉 Готово. Окон: {total_windows}, health-точек: {total_health}"
          + (f", уведомлений: {total_notif}" if with_journal else ""), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Бэкафилл raw_data.health по истории")
    parser.add_argument("--station", default="ohangaron")
    parser.add_argument("--from", dest="from_date", default=None, help="нижняя граница ISO (default: min в БД)")
    parser.add_argument("--to", dest="to_date", default=None, help="верхняя граница ISO (default: сейчас)")
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--overlap-days", type=int, default=1)
    parser.add_argument("--with-journal", action="store_true", help="писать также в anomalies и журнал")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from logging_config import setup as _log_setup
    _log_setup("backfill_health")

    backfill(args.station, args.from_date, args.to_date,
             args.window_days, args.overlap_days, args.with_journal)
