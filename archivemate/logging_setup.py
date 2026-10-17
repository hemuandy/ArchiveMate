"""日志：写文件 + 转发到界面。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from . import app_paths

LOGGER_NAME = "archivemate"
_MAX_BYTES = 1 * 1024 * 1024
_BACKUPS = 3

_configured = False


class CallbackHandler(logging.Handler):
    """把日志行推给 GUI。"""

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(self.format(record))
        except Exception:
            pass


def setup(level: int = logging.INFO, *, log_dir: Path | None = None) -> logging.Logger:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if _configured:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    directory = log_dir or app_paths.logs_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / "archivemate.log", maxBytes=_MAX_BYTES,
            backupCount=_BACKUPS, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass

    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    logger.propagate = False
    _configured = True
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def attach_ui(callback: Callable[[str], None]) -> logging.Handler:
    handler = CallbackHandler(callback)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    get_logger().addHandler(handler)
    return handler


def detach_ui(handler: logging.Handler) -> None:
    get_logger().removeHandler(handler)


def log_file_path() -> Path:
    return app_paths.logs_dir() / "archivemate.log"
