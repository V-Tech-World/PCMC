"""
In-process rate limiting / call-budget guards (cost rails).

Every dialed call is a billed provider minute, so the backend enforces its
own limits before placing a call -- independent of what the provider allows:

- hourly sliding window (max_calls_per_hour)
- daily counter (max_calls_per_day, resets at local midnight)
- concurrent stream cap (max_concurrent_calls)

The state is deliberately in-process: this backend is a single instance, and
no external store (Redis etc.) is in the project's dependency set. The
`reset()` hook exists for tests.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import date

from app.core.config import get_settings

logger = logging.getLogger("voicecare.rate_limiter")

_lock = threading.Lock()
_hourly: deque[float] = deque()   # timestamps of dials inside the last hour
_day_key: date | None = None
_day_count: int = 0


class RateLimitExceeded(Exception):
    """Raised when a dial would exceed a configured call budget."""

    def __init__(self, reason: str, retry_after_sec: float = 0.0):
        super().__init__(reason)
        self.reason = reason
        self.retry_after_sec = retry_after_sec


def _roll_day_locked(now_ts: float) -> None:
    """Advance the daily counter if the local calendar day changed."""
    global _day_key, _day_count
    today = date.today()
    if _day_key != today:
        _day_key = today
        _day_count = 0


def check_can_dial() -> None:
    """Raise RateLimitExceeded if placing a call now would break a budget.

    Called BEFORE the provider API is hit -- a rejected dial costs nothing.
    """
    settings = get_settings()
    now = time.monotonic()

    with _lock:
        # Hourly sliding window (monotonic timestamps are immune to clock
        # changes; window comparisons stay in monotonic space).
        window = 3600.0
        while _hourly and now - _hourly[0] >= window:
            _hourly.popleft()

        limit_h = settings.max_calls_per_hour
        if limit_h and len(_hourly) >= limit_h:
            retry = window - (now - _hourly[0])
            raise RateLimitExceeded(
                f"Hourly call limit reached ({limit_h}/hour)", retry_after_sec=retry
            )

        limit_d = settings.max_calls_per_day
        if limit_d:
            _roll_day_locked(now)
            if _day_count >= limit_d:
                raise RateLimitExceeded(
                    f"Daily call limit reached ({limit_d}/day)", retry_after_sec=60.0
                )


def record_dial() -> None:
    """Count a dial that was actually placed (call every successful dial)."""
    with _lock:
        _hourly.append(time.monotonic())
        _roll_day_locked(time.monotonic())
        global _day_count
        _day_count += 1
        logger.info(
            "Dial recorded: hour=%d/%s day=%d/%s",
            len(_hourly),
            get_settings().max_calls_per_hour,
            _day_count,
            get_settings().max_calls_per_day,
        )


def concurrent_calls() -> int:
    """Number of live media streams tracked by the API layer."""
    try:
        from app.api.media_stream import active_sessions

        return len(active_sessions)
    except Exception:  # pragma: no cover - defensive; API module always imports
        return 0


def check_stream_slot() -> None:
    """Raise RateLimitExceeded if the concurrent-stream cap is full."""
    settings = get_settings()
    cap = settings.max_concurrent_calls
    if not cap:
        return
    live = concurrent_calls()
    if live >= cap:
        raise RateLimitExceeded(
            f"Concurrent call limit reached ({live}/{cap})", retry_after_sec=5.0
        )


def reset() -> None:
    """Clear all counters (tests)."""
    global _day_key, _day_count
    with _lock:
        _hourly.clear()
        _day_key = None
        _day_count = 0
