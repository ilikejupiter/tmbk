# -*- coding: utf-8 -*-
"""
MyXL package init.

Modern & stable logging principles:
- Do NOT unexpectedly change global logging configuration on import.
- Provide an idempotent `setup_logging()` users/CLI can call explicitly.
- Allow env-driven configuration without breaking defaults.
- Avoid duplicate handlers across re-imports / tests.

Python: 3.9.18 compatible (stdlib only).
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

__version__ = "1.1.1"  # bump: safer logging init, no side-effects on import
__author__ = "MyXL Team"

__all__ = ["setup_logging", "__version__", "__author__"]

# A unique attribute marker so we can detect our own handlers reliably.
_HANDLER_MARK = "myxl_handler"


def _parse_bool_env(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip().lower()
    return v not in ("0", "false", "no", "off", "")


def _parse_int_env(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return int(v.strip())
    except Exception:
        return default


def _get_level() -> int:
    level_name = os.environ.get("MYXL_LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, level_name, logging.INFO)


def _already_configured(root: logging.Logger) -> bool:
    # Detect if any handlers we created exist.
    for h in root.handlers:
        if getattr(h, _HANDLER_MARK, False):
            return True
    return False


def setup_logging(
    *,
    logger_name: str = "",
    log_dir: Optional[str] = None,
    log_file: Optional[str] = None,
    level: Optional[int] = None,
) -> None:
    """
    Setup logging (idempotent).

    Defaults (can be overridden by env):
      - File logging: enabled unless MYXL_LOG_TO_FILE=0
      - Console logging: enabled unless MYXL_LOG_TO_CONSOLE=0
      - Log level: MYXL_LOG_LEVEL (default INFO)
      - Log dir : MYXL_LOG_DIR (default "logs")
      - Log file: MYXL_LOG_FILE (default "myxl_app.log")
      - RotatingFileHandler max bytes : MYXL_LOG_MAX_BYTES (default 1MB)
      - RotatingFileHandler backup count: MYXL_LOG_BACKUP_COUNT (default 5)

    Notes:
    - We configure either the root logger (logger_name="") or a named logger.
    - We avoid messing with existing handlers unless they are ours.
    """
    target = logging.getLogger(logger_name) if logger_name else logging.getLogger()

    # If we've already configured this logger with our handlers, just update level and return.
    if _already_configured(target):
        target.setLevel(level if level is not None else _get_level())
        return

    # Determine config
    effective_level = level if level is not None else _get_level()
    to_console = _parse_bool_env("MYXL_LOG_TO_CONSOLE", True)
    to_file = _parse_bool_env("MYXL_LOG_TO_FILE", True)

    effective_log_dir = log_dir or os.environ.get("MYXL_LOG_DIR", "logs")
    effective_log_file = log_file or os.environ.get("MYXL_LOG_FILE", "myxl_app.log")

    max_bytes = _parse_int_env("MYXL_LOG_MAX_BYTES", 1_048_576)  # 1 MB
    backup_count = _parse_int_env("MYXL_LOG_BACKUP_COUNT", 5)

    # Set the logger level; do not force disable propagation unless requested.
    target.setLevel(effective_level)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler (rotating)
    if to_file:
        try:
            os.makedirs(effective_log_dir, exist_ok=True)
            path = os.path.join(effective_log_dir, effective_log_file)
            fh = RotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setLevel(effective_level)
            fh.setFormatter(fmt)
            setattr(fh, _HANDLER_MARK, True)
            target.addHandler(fh)
        except Exception:
            # If file logging fails (permissions, readonly FS, etc), we don't crash app.
            # Console handler (if enabled) will still work.
            target.exception("Failed to initialize file logging. Continuing without file handler.")

    # Console handler
    if to_console:
        sh = logging.StreamHandler()
        sh.setLevel(effective_level)
        sh.setFormatter(fmt)
        setattr(sh, _HANDLER_MARK, True)
        target.addHandler(sh)

    # Make sure repeated logs aren't duplicated upstream unexpectedly.
    # For root logger, propagation is irrelevant; for named logger it matters.
    # Default: keep propagate True unless user opts out.
    if logger_name:
        target.propagate = _parse_bool_env("MYXL_LOG_PROPAGATE", True)