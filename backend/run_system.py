"""
Единый запуск системы CS Monitor AI одним файлом:
  1. Миграции БД (migrate_db.migrate) + индексы (ensure_indexes).
  2. Проверка моделей (models/<station>/metadata.json); опц. обучение.
  3. Параллельно: FastAPI (uvicorn, поток) + онлайн-детекция
     (live_predict.run_continuous — НЕ --once, реальный 5-мин цикл, поток).
  4. Graceful shutdown по SIGINT/SIGTERM — гасит оба.

CLI:
    python run_system.py --station ohangaron
    python run_system.py --station ohangaron --no-api          # только ML-цикл
    python run_system.py --station ohangaron --no-ml           # только API
    python run_system.py --station ohangaron --once            # один проход ML вместо цикла
    python run_system.py --station ohangaron --train-if-missing
"""
from __future__ import annotations
import argparse
import logging
import os
import subprocess
import sys
import threading
import time

logger = logging.getLogger("run_system")

_HERE = os.path.dirname(os.path.abspath(__file__))


def _models_ready(station: str) -> bool:
    from station_config import load_station_config
    cfg = load_station_config(station)
    return (cfg.models_path / "metadata.json").exists()


def _health_loop(station: str, interval_h: float, recent_days: float, stop: threading.Event):
    """Периодический (по умолч. раз в сутки) СИСТЕМНЫЙ мониторинг структурного здоровья агрегатов.
    Запускает system_monitor.py (unit-level PCA/SPE) и drift_monitor.py (per-sensor drill-down) в
    ИЗОЛИРОВАННЫХ подпроцессах — тяжёлый PCA не мешает 5-мин ML-циклу и не может его уронить.
    Пишут system_health.json / drift_alerts.json в models-dir (API/дашборд их отдаёт оператору)."""
    interval_s = max(600.0, interval_h * 3600.0)
    while not stop.is_set():
        for script in ("system_monitor.py", "drift_monitor.py"):
            if stop.is_set():
                break
            # system_monitor в проде ПИШЕТ unit-level здоровье в БД (--write-db); drift — JSON drill-down
            extra = ["--write-db"] if script == "system_monitor.py" else []
            try:
                r = subprocess.run(
                    [sys.executable, os.path.join(_HERE, script),
                     "--station", station, "--recent-days", str(recent_days), *extra],
                    cwd=_HERE, capture_output=True, text=True, timeout=1800)
                tag = script.replace(".py", "")
                if r.returncode == 0:
                    logger.info("%s ok", tag)
                else:
                    logger.warning("%s rc=%s: %s", tag, r.returncode, (r.stderr or "")[-400:])
            except Exception as e:                       # изоляция: не роняем систему
                logger.warning("%s исключение: %s", script, e)
        stop.wait(interval_s)


def main():
    # utf-8 для stdout/stderr: эмодзи-маркеры (🌐🔬🩺) НЕ должны ронять главный поток при редиректе
    # в файл или на cp1252-консоли Windows (иначе UnicodeEncodeError убьёт запуск ML/health-шагов).
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Единый запуск CS Monitor AI")
    parser.add_argument("--station", default="ohangaron")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--no-api", action="store_true", help="не поднимать FastAPI")
    parser.add_argument("--no-ml", action="store_true", help="не запускать ML-цикл")
    parser.add_argument("--once", action="store_true", help="один проход ML вместо непрерывного цикла")
    parser.add_argument("--train-if-missing", action="store_true",
                        help="обучить модели, если metadata.json отсутствует")
    parser.add_argument("--no-health", action="store_true",
                        help="не запускать периодический системный монитор структурного здоровья")
    parser.add_argument("--health-interval-hours", type=float, default=24.0,
                        help="период системного монитора (ч), по умолч. 24")
    parser.add_argument("--health-recent-days", type=float, default=14.0,
                        help="ширина свежего окна системного монитора (дни), по умолч. 14")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from logging_config import setup, single_instance, install_signal_handlers
    setup("run_system")

    if not single_instance("run_system"):
        print("❌ run_system уже запущен (lock занят) — второй экземпляр не стартует", flush=True)
        sys.exit(1)

    station = args.station
    print("=" * 60, flush=True)
    print(f"🚀 CS Monitor AI — единый запуск (станция {station})", flush=True)
    print("=" * 60, flush=True)

    # ── Шаг 1: миграции + индексы ─────────────────────────────────────────────
    try:
        import migrate_db
        steps = migrate_db.migrate(station)
        print(f"🗄️  Миграции БД: выполнено шагов {len(steps)}", flush=True)
    except Exception:
        logger.exception("Миграции БД не выполнены — продолжаю (БД могла быть недоступна)")

    # ── Шаг 2: проверка моделей ───────────────────────────────────────────────
    if not _models_ready(station):
        if args.train_if_missing:
            print("🧠 metadata.json нет — обучаю модели...", flush=True)
            import subprocess
            rc = subprocess.call([sys.executable, "train.py", "--station", station],
                                 cwd=os.path.dirname(os.path.abspath(__file__)))
            if rc != 0 or not _models_ready(station):
                print("❌ Обучение не удалось — без моделей ML-цикл не стартует", flush=True)
                if args.no_api:
                    sys.exit(1)
        else:
            print(f"❌ Модели не найдены (models/{station}/metadata.json). "
                  f"Обучите train.py или запустите с --train-if-missing.", flush=True)
            if not args.no_ml and args.no_api:
                sys.exit(1)

    threads: list[threading.Thread] = []
    server = None

    # ── Шаг 3: FastAPI (поток) ────────────────────────────────────────────────
    if not args.no_api:
        import uvicorn
        config = uvicorn.Config("main:app", host="0.0.0.0", port=args.api_port,
                                log_level="info", loop="asyncio")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None   # сигналы держит run_system
        t_api = threading.Thread(target=server.run, name="uvicorn", daemon=True)
        t_api.start()
        threads.append(t_api)
        print(f"🌐 API: http://0.0.0.0:{args.api_port} (поток запущен)", flush=True)

    # ── Шаг 3: онлайн-ML (поток) ──────────────────────────────────────────────
    import live_predict as lp
    if not args.no_ml:
        if _models_ready(station):
            # отдельный ML-lock: блокирует параллельный live_predict.py --mode live
            if not single_instance("live_predict"):
                print("⚠️ live_predict уже запущен отдельно — ML-цикл в run_system пропущен", flush=True)
            else:
                lp._init_station(station)
                if args.once:
                    t_ml = threading.Thread(target=lp.run_once, name="ml-once", daemon=True)
                else:
                    t_ml = threading.Thread(target=lp.run_continuous, name="ml-loop", daemon=True)
                t_ml.start()
                threads.append(t_ml)
                print(f"🔬 Онлайн-детекция: {'один проход' if args.once else 'непрерывный цикл'} (поток запущен)", flush=True)
        else:
            print("⚠️ Модели отсутствуют — ML-цикл не запущен", flush=True)

    if not threads:
        print("❌ Нечего запускать (--no-api и --no-ml вместе или нет моделей).", flush=True)
        sys.exit(1)

    # ── Шаг 4: graceful shutdown ──────────────────────────────────────────────
    stop = threading.Event()

    def _shutdown():
        logger.info("Останавливаю систему...")
        stop.set()

    install_signal_handlers(_shutdown)

    # ── Системный монитор структурного здоровья (daemon, раз в сутки; НЕ в списке threads,
    #    чтобы не мешать выходу из --once). Изолированные подпроцессы. ──
    if not args.no_health and not args.once and _models_ready(station):
        t_health = threading.Thread(
            target=_health_loop,
            args=(station, args.health_interval_hours, args.health_recent_days, stop),
            name="health-loop", daemon=True)
        t_health.start()
        print(f"🩺 Системный монитор здоровья: раз в {args.health_interval_hours:g}ч "
              f"(окно {args.health_recent_days:g}д, поток запущен)", flush=True)

    try:
        while not stop.is_set():
            time.sleep(0.5)
            # если все рабочие потоки умерли (напр. --once завершился) — выходим
            if not any(t.is_alive() for t in threads):
                break
    except KeyboardInterrupt:
        _shutdown()

    print("\n⏹  Завершение: останавливаю API и ML-цикл...", flush=True)
    lp._request_shutdown()
    if server is not None:
        server.should_exit = True
    for t in threads:
        t.join(timeout=20)
    print("✅ Остановлено.", flush=True)


if __name__ == "__main__":
    main()
