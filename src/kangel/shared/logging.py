"""进程安全的统一日志初始化与延迟记录器。"""

import logging
import os
import sys
import threading
from typing import Optional

_init_lock = threading.Lock()
_initialized_pids: set[int] = set()


def _get_log_level() -> int:
    try:
        from config.settings import settings
        return getattr(logging, settings.server.log_level.upper(), logging.INFO)
    except Exception:
        return logging.INFO


def initialize_logger(force: bool = False) -> logging.Logger:
    current_pid = os.getpid()
    if not force and current_pid in _initialized_pids:
        return logging.getLogger("kangel")
    with _init_lock:
        if not force and current_pid in _initialized_pids:
            return logging.getLogger("kangel")
        root_logger = logging.getLogger()
        if not root_logger.handlers or force:
            logging.basicConfig(
                level=_get_log_level(),
                format="[%(levelname)s] | %(asctime)s | %(message)s",
                datefmt="%H:%M:%S",
                stream=sys.stderr,
                force=True,
            )
        _initialized_pids.add(current_pid)
        return logging.getLogger("kangel")


def get_logger(name: Optional[str] = None) -> logging.Logger:
    initialize_logger()
    return logging.getLogger(name or "kangel")


class LazyLogger:
    def __init__(self) -> None:
        self._logger: Optional[logging.Logger] = None

    def _get_logger(self) -> logging.Logger:
        if self._logger is None:
            self._logger = get_logger()
        return self._logger

    def __getattr__(self, name: str):
        return getattr(self._get_logger(), name)


logger = LazyLogger()

__all__ = ["LazyLogger", "get_logger", "initialize_logger", "logger"]
