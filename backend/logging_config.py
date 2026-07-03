"""
Единая инфраструктура логирования и диагностики процессов.

setup(name)          — root-логгер: RotatingFileHandler logs/{name}.log + консоль;
                       плюс tee stdout/stderr в тот же файл с таймстампами,
                       чтобы существующие print() не терялись при фоновом запуске.
single_instance(name)— файловый lock от двойного запуска (отпускается ОС при
                       смерти процесса); возвращает False, если уже запущено.
install_signal_handlers(flag_setter) — graceful shutdown по SIGINT/SIGTERM.
"""
from __future__ import annotations
import io
import logging
import logging.handlers
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

LOGS_DIR = Path(__file__).parent / "logs"

_lock_handles: dict[str, object] = {}   # держим хендлы живыми до конца процесса


class _Tee(io.TextIOBase):
    """Дублирует поток в файл, добавляя таймстамп в начало каждой строки."""

    def __init__(self, orig, fh):
        self._orig = orig
        self._fh = fh
        self._at_line_start = True

    def write(self, s: str) -> int:
        try:
            self._orig.write(s)
        except Exception:
            pass
        try:
            for chunk in s.splitlines(keepends=True):
                if self._at_line_start and chunk.strip():
                    self._fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S "))
                self._fh.write(chunk)
                self._at_line_start = chunk.endswith("\n")
            self._fh.flush()
        except Exception:
            pass
        return len(s)

    def flush(self) -> None:
        try:
            self._orig.flush()
        except Exception:
            pass
        try:
            self._fh.flush()
        except Exception:
            pass


def setup(name: str, tee_stdout: bool = True) -> logging.Logger:
    """Настраивает root-логгер с ротацией. Идемпотентно."""
    LOGS_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_cs4_configured", False):
        return root
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        LOGS_DIR / f"{name}.log", maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.__stderr__)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    if tee_stdout:
        # print()-вывод (35+ вызовов в live_predict) и трейсбеки — в тот же лог
        out = open(LOGS_DIR / f"{name}.out.log", "a", encoding="utf-8", errors="replace")
        sys.stdout = _Tee(sys.stdout, out)
        sys.stderr = _Tee(sys.stderr, out)

    root._cs4_configured = True
    return root


def single_instance(name: str) -> bool:
    """Эксклюзивный lock {logs}/{name}.lock. False — экземпляр уже работает.
    Lock снимается ОС при завершении процесса (зомби не блокируют)."""
    LOGS_DIR.mkdir(exist_ok=True)
    path = LOGS_DIR / f"{name}.lock"
    try:
        fh = open(path, "a+")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        _lock_handles[name] = fh
        return True
    except OSError:
        return False


def install_signal_handlers(on_shutdown) -> None:
    """SIGINT/SIGTERM/SIGBREAK -> on_shutdown() (установка флага выхода из цикла).
    SIGBREAK нужен на Windows: единый лаунчер run.py гасит детей через
    CTRL_BREAK_EVENT (ОС доставляет его как SIGBREAK) — без этого обработчика
    graceful shutdown не сработает и процесс пришлось бы жёстко убивать."""
    def _handler(signum, frame):
        logging.getLogger(__name__).info("Получен сигнал %s — graceful shutdown", signum)
        on_shutdown()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass   # не главный поток / не поддерживается
