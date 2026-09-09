"""
Centralised logging so every run leaves a complete, shareable log file behind.

Each module already creates its own logger via logging.getLogger(name); attaching a
FileHandler to the ROOT logger here captures all of them at once. We attach the handler
explicitly rather than relying on logging.basicConfig(), because the individual modules
call basicConfig() at import time and only the first such call has any effect -- so import
order would otherwise decide whether file logging happened at all.
"""
import sys
import logging
from pathlib import Path
from datetime import datetime

LOG_DIR = Path(__file__).parent.resolve() / "logs"
_current_log_file = None


def setup_logging(level: int = logging.INFO) -> Path:
    global _current_log_file
    if _current_log_file is not None:
        return _current_log_file

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        root.addHandler(console)

    def _log_uncaught(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("Uncaught").critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _log_uncaught
    _current_log_file = log_path
    logging.getLogger("Logging").info(f"Logging to: {log_path}")
    return log_path


def get_log_file() -> Path:
    return _current_log_file
