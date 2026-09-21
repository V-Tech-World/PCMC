"""Console logging setup shared by the app and CLI helper scripts."""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """Configure the 'voicecare' logger tree once, idempotently."""
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger("voicecare").setLevel(level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    root = logging.getLogger("voicecare")
    root.setLevel(level.upper())
    root.addHandler(handler)
    root.propagate = False  # keep uvicorn's default handlers out of ours
    _CONFIGURED = True
