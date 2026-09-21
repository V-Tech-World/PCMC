"""
Timer resolution for real-time audio pacing (Windows).

Windows' default scheduler tick is ~15.6 ms, so ``asyncio.sleep(0.02)`` really
sleeps ~31 ms. That made our audio stream ~1.5x slower than real time, starving
the provider's jitter buffer -- heard live as "an old radio missing the
frequency". Raising the timer resolution to 1 ms makes the same sleep accurate
to <1 ms.

``timeBeginPeriod`` is process-wide and must be paired with ``timeEndPeriod``;
we reference-count it so repeated start/stop cycles (tests, reloads) are safe.
On non-Windows platforms this is a no-op -- asyncio sleeps are already precise.
"""

from __future__ import annotations

import logging
import sys
import threading

logger = logging.getLogger("voicecare.timing")

_IS_WINDOWS = sys.platform.startswith("win")
_PERIOD_MS = 1
_lock = threading.Lock()
_depth = 0  # nested acquire/release pairs


def _winmm():  # pragma: no cover - Windows only
    import ctypes

    return ctypes.WinDLL("winmm")


def acquire_fine_timer() -> bool:
    """Ask Windows for 1 ms timer resolution. Returns True if the call succeeded."""
    global _depth
    if not _IS_WINDOWS:
        return False
    with _lock:
        if _depth == 0:
            try:
                result = _winmm().timeBeginPeriod(_PERIOD_MS)
            except Exception as exc:  # pragma: no cover - platform specific
                logger.warning("timeBeginPeriod failed: %s", exc)
                return False
            if result != 0:
                logger.warning("timeBeginPeriod(%d) returned %d", _PERIOD_MS, result)
                return False
            logger.info("Windows timer resolution raised to %d ms (real-time audio)", _PERIOD_MS)
        _depth += 1
    return True


def release_fine_timer() -> None:
    """Undo acquire_fine_timer once the matching count reaches zero."""
    global _depth
    if not _IS_WINDOWS:
        return
    with _lock:
        if _depth == 0:
            return
        _depth -= 1
        if _depth == 0:
            try:
                _winmm().timeEndPeriod(_PERIOD_MS)
            except Exception:  # pragma: no cover - platform specific
                pass


def sleep_resolution_warning() -> str:
    """Human-readable note about the pacing timer, for logs/health output."""
    if not _IS_WINDOWS:
        return "native"
    return f"fine ({_PERIOD_MS} ms)" if _depth else "coarse (sleep ~15 ms)"
