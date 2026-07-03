#!/usr/bin/env python
"""
Единый запуск CS Monitor AI с одного ноутбука: фронт + бэк + ML-ядро.

Поднимает два дочерних процесса и держит их жизненный цикл вместе:
  1. backend/run_system.py — FastAPI (:8000) + ML-цикл live_predict. На старте
     ML-ядро само сверяет now с меткой последнего предикта и догоняет пропуск в
     БД оконными запросами (live_predict.catch_up_missing), затем — обычный цикл.
  2. frontend (Vite) — UI оператора.

Ctrl+C гасит оба (graceful: CTRL_BREAK на Windows → SIGBREAK, который ловит
run_system). Если один процесс падает — гасим второй и выходим с кодом 1.

Запуск:
    python run.py
    python run.py --station ohangaron
    python run.py --no-frontend          # только бэк+ML
    python run.py --frontend preview     # собранный dist вместо dev-сервера
"""
from __future__ import annotations
import argparse
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"


def _python() -> str:
    """venv-питон, если есть, иначе текущий интерпретатор."""
    if sys.platform == "win32":
        venv = ROOT / "venv" / "Scripts" / "python.exe"
    else:
        venv = ROOT / "venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _npm(*args: str) -> list[str]:
    """npm на Windows — батник (npm.cmd) → запускаем через cmd /c."""
    if sys.platform == "win32":
        return ["cmd", "/c", "npm", *args]
    return ["npm", *args]


def _popen(cmd: list[str], cwd: Path) -> subprocess.Popen:
    kwargs: dict = {"cwd": str(cwd)}
    # новая группа процессов на Windows → Ctrl+C консоли не убивает детей напрямую,
    # а мы шлём им CTRL_BREAK адресно при остановке.
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, **kwargs)


def _hard_kill(pr: subprocess.Popen) -> None:
    """Жёсткий снос процесса. На Windows — со всем деревом (cmd→npm→node), т.к.
    pr.kill() (TerminateProcess) убивает только сам cmd.exe и оставляет vite-node
    держать порт."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pr.pid)],
                           capture_output=True)
        else:
            pr.kill()
    except Exception:
        pass


def main() -> None:
    # utf-8 stdout/stderr: эмодзи-маркеры не должны ронять запуск на cp1252-консоли/при редиректе (Windows)
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description="Единый запуск CS Monitor AI (фронт+бэк+ML)")
    p.add_argument("--station", default="ohangaron")
    p.add_argument("--api-port", type=int, default=8000)
    p.add_argument("--no-frontend", action="store_true", help="не поднимать фронт")
    p.add_argument("--no-backend", action="store_true", help="не поднимать бэк+ML")
    p.add_argument("--frontend", default="dev", choices=["dev", "preview"],
                   help="dev = Vite dev-сервер, preview = предпросмотр собранного dist")
    args = p.parse_args()

    procs: list[tuple[str, subprocess.Popen]] = []
    crashed = False
    _stopped = False

    def _shutdown() -> None:
        nonlocal _stopped
        if _stopped:
            return
        _stopped = True
        print("\n⏹  Останавливаю все процессы...", flush=True)
        for _name, pr in procs:
            if pr.poll() is not None:
                continue
            try:
                if sys.platform == "win32":
                    pr.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    pr.terminate()
            except Exception:
                pass
        deadline = time.time() + 20
        for _name, pr in procs:
            try:
                pr.wait(timeout=max(0.1, deadline - time.time()))
            except Exception:
                _hard_kill(pr)
        print("✅ Остановлено.", flush=True)

    print("=" * 60, flush=True)
    print("🚀 CS Monitor AI — единый запуск (фронт + бэк + ML)", flush=True)
    print("=" * 60, flush=True)

    try:
        # ── Бэкенд + ML (FastAPI + live_predict с догоном пропуска на старте) ──
        if not args.no_backend:
            cmd = [_python(), "run_system.py", "--station", args.station,
                   "--api-port", str(args.api_port)]
            procs.append(("backend+ml", _popen(cmd, BACKEND)))
            print(f"🌐 backend+ML: http://localhost:{args.api_port}  (cwd={BACKEND})", flush=True)

        # ── Фронтенд (Vite) ──
        if not args.no_frontend:
            if shutil.which("npm") is None or not FRONTEND.exists():
                print("⚠️  npm не найден в PATH или нет каталога frontend — "
                      "поднимаю только бэк+ML", flush=True)
            else:
                start_fe = True
                if not (FRONTEND / "node_modules").exists():
                    print("📦 frontend/node_modules нет — npm install...", flush=True)
                    if subprocess.call(_npm("install"), cwd=str(FRONTEND)) != 0:
                        print("❌ npm install не удался — фронт пропущен", flush=True)
                        start_fe = False
                if start_fe and args.frontend == "preview":
                    print("🏗️  Сборка фронта (npm run build)...", flush=True)
                    if subprocess.call(_npm("run", "build"), cwd=str(FRONTEND)) != 0:
                        print("❌ npm run build не удался — фронт пропущен", flush=True)
                        start_fe = False
                if start_fe:
                    procs.append(("frontend", _popen(_npm("run", args.frontend), FRONTEND)))
                    print(f"🖥️  frontend: npm run {args.frontend}  (cwd={FRONTEND})", flush=True)

        if not procs:
            print("❌ Нечего запускать (--no-backend и --no-frontend вместе "
                  "или фронт недоступен).", flush=True)
            sys.exit(1)

        print("\n✅ Запущено. Ctrl+C — остановить всё.\n", flush=True)

        # ── Супервизор: падение любого ребёнка → гасим остальные ──
        while True:
            time.sleep(0.5)
            dead = [(n, pr) for n, pr in procs if pr.poll() is not None]
            if dead:
                for n, pr in dead:
                    rc = pr.returncode
                    if rc not in (0, None):
                        crashed = True
                    print(f"⚠️  Процесс «{n}» завершился (код {rc}) — гашу остальные.", flush=True)
                break
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()

    sys.exit(1 if crashed else 0)


if __name__ == "__main__":
    main()
