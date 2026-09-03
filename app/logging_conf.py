"""Logging setup: every signal (accepted or rejected) gets a timestamped record,
written to both console and a rotating log file."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import settings

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def get_logger(name: str = "tv_webhook") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = RotatingFileHandler(
        settings.log_path, maxBytes=5_000_000, backupCount=5
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.propagate = False
    return logger


signal_logger = get_logger("signals")
