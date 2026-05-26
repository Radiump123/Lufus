#!/usr/bin/env python3
import logging
import sys
import os
import atexit
import tempfile

LOG_FILE = os.path.join(os.path.expanduser("~"), ".local", "share", "lufus", "lufus.log")

_FMT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_setup_done = False


def setup_logging() -> None:
    global _setup_done
    if _setup_done:
        return
    _setup_done = True

    root = logging.getLogger("lufus")
    root.setLevel(logging.DEBUG)

    plain = logging.Formatter(_FMT, _DATEFMT)

    log_file = LOG_FILE
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8", delay=False)
    except OSError:
        log_file = os.path.join(tempfile.gettempdir(), "lufus.log")
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8", delay=False)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(plain)

    root.addHandler(fh)

    def _crash_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        root.critical(
            "Unhandled exception — process is about to crash",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        fh.flush()

    sys.excepthook = _crash_hook
    atexit.register(fh.flush)
    print(f"[lufus] Log file: {log_file}", flush=True)
    root.debug("Logging initialised — log file: %s", log_file)


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    if not name.startswith("lufus"):
        name = f"lufus.{name}"
    return logging.getLogger(name)
