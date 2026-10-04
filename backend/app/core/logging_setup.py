"""
Console + file logging setup shared by the app and CLI helper scripts.

Console-only logging (the original behaviour) means a live call leaves no trace
once the terminal is closed -- and "why did the leg die 1 s after the final
question?" is exactly the question you ask *after* the call. Every run also
appends to backend/logs/voicecare.log (rotating, 2 MB x 5), git-ignored because
it can contain transcripts of patient speech.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "voicecare.log"
_LOG_FORMAT = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)


def log_dir() -> Path:
    """Where the log file lives (backend/logs by default)."""
    from app.core.config import BACKEND_DIR

    return BACKEND_DIR / LOG_DIR_NAME


def setup_logging(level: str = "INFO", to_file: bool = True) -> None:
    """Configure the 'voicecare' logger tree once, idempotently."""
    global _CONFIGURED
    root = logging.getLogger("voicecare")
    if _CONFIGURED:
        root.setLevel(level.upper())
        return

    root.setLevel(level.upper())
    root.propagate = False  # keep uvicorn's default handlers out of ours

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_LOG_FORMAT)
    root.addHandler(console)

    if to_file:
        try:
            directory = log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                directory / LOG_FILE_NAME,
                maxBytes=2_000_000,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(_LOG_FORMAT)
            root.addHandler(file_handler)
        except OSError as exc:  # logging must never break the call flow
            root.warning("Could not open the log file (%s) -- console only", exc)

    _CONFIGURED = True
