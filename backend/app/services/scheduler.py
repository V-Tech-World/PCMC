"""
Call scheduling (Step 8): follow-up check-in reminders.

The hospital gives each patient a discharge date; the follow-up check-in calls
are then due at fixed offsets after that date (SCHEDULE_CHECKIN_DAYS, default
3/7/14/30). This module owns three things:

1. ``compute_schedule()`` -- pure maths. Given a patient, the last call we made
   to them and "now", it returns the whole due timeline plus the next call
   time. Used by the dashboard column AND by the automatic tick, so what the
   nurse sees is exactly what the scheduler would do.
2. ``plan_due()`` -- which patients have a check-in due right now (inside the
   grace window) that has not already been satisfied by a call.
3. ``run_tick()`` -- the actual dial, guarded by the same cost rails as a
   manual call (hourly/daily budget, concurrency cap) plus
   SCHEDULE_MAX_DIALS_PER_TICK.

SCHEDULE_CALLS_ENABLED is the master switch (default **false**). With it off
this module still computes and reports the schedule -- it just never dials.
That is the demo behaviour asked for: manual calls from the frontend, with the
cronjob feature present but dormant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from app.core import rate_limiter
from app.core.config import Settings, get_settings

logger = logging.getLogger("voicecare.scheduler")

# Statuses a patient's check-in slot can be in.
STATUS_DUE = "due"
STATUS_OVERDUE = "overdue"
STATUS_SCHEDULED = "scheduled"
STATUS_COMPLETED = "completed"
STATUS_NO_DATE = "no_discharge_date"
STATUS_NO_NUMBER = "no_phone_number"
STATUS_INACTIVE = "inactive"


@dataclass
class DueItem:
    """One patient whose check-in slot is due (or overdue)."""

    patient_code: str
    name: str
    phone_number: str
    diagnosis_category: str
    slot: date                 # the calendar day the check-in was due
    status: str
    days_overdue: int
    cooldown_hours: float = 0.0  # hours until this patient may be auto-dialed again (0 = now)
    content: dict[str, Any] = field(default_factory=dict)


@dataclass
class TickResult:
    """What one scheduler tick did (also returned by POST /schedule/run-now)."""

    ran_at: str
    enabled: bool
    dry_run: bool
    dialed: int = 0
    skipped: int = 0
    planned: list[dict[str, Any]] = field(default_factory=list)
    outcomes: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-patient automatic-call cooldown (24h window)
# ---------------------------------------------------------------------------

DIAL_STAMP_PREFIX = "schedule_last_dial:"


def _stamp_key(patient_code: str) -> str:
    return f"{DIAL_STAMP_PREFIX}{(patient_code or '').strip()}"


def _coerce_naive(value: datetime) -> datetime:
    """SQLite returns naive datetimes; tolerate aware ones from callers."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def record_dial_stamp(
    patient_code: str,
    at: datetime | None = None,
    database_url: str | None = None,
) -> None:
    """Remember that a patient was just dialed (manual AND scheduled calls).

    Bookkeeping must never break a dial: any DB problem is logged and swallowed.
    """
    if not (patient_code or "").strip():
        return
    from app.db import service as db_service

    try:
        db_service.set_setting(
            _stamp_key(patient_code),
            _coerce_naive(at or datetime.now()).isoformat(timespec="seconds"),
            database_url,
        )
    except Exception:
        logger.exception("Could not stamp last-dial time for %s", patient_code)


def cooldown_state(
    patient_code: str,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> tuple[float, float | None]:
    """Return ``(hours_left, hours_since_last_dial)`` for automatic dialing.

    ``hours_left`` is 0 when the patient may be auto-dialed right now;
    ``hours_since_last_dial`` is None when the patient was never dialed.
    Manual calls always start the window, but the window never *blocks* a
    manual call -- only ``run_tick()`` consults it.
    """
    from app.db import service as db_service

    settings = settings or get_settings()
    window = max(0.0, float(settings.schedule_min_hours_between_calls or 0))
    moment = _coerce_naive(now or datetime.now())
    if window <= 0:
        return 0.0, None
    try:
        raw = db_service.get_setting(_stamp_key(patient_code))
    except Exception:
        logger.exception("Could not read last-dial time for %s", patient_code)
        return 0.0, None
    if not raw:
        return 0.0, None
    try:
        last = _coerce_naive(datetime.fromisoformat(raw))
    except ValueError:
        logger.warning("Ignoring malformed last-dial stamp %r", raw)
        return 0.0, None
    since = max(0.0, (moment - last).total_seconds() / 3600.0)
    return max(0.0, window - since), since


def cooldown_hours_left(
    patient_code: str,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> float:
    """Hours until this patient may be auto-dialed again (0 = right now)."""
    left, _ = cooldown_state(patient_code, now=now, settings=settings)
    return left


def _parse_discharge(patient) -> date | None:
    raw = (getattr(patient, "discharge_date", "") or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


def _slot_hour(settings: Settings) -> time:
    hour = max(0, min(23, settings.schedule_hour))
    minute = max(0, min(59, settings.schedule_minute))
    return time(hour=hour, minute=minute)


def compute_schedule(
    patient,
    last_call_at: datetime | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return the full check-in timeline for one patient.

    Also the body of one entry in GET /schedule, so the dashboard column and
    the automatic tick can never disagree.
    """
    settings = settings or get_settings()
    now = now or datetime.now()
    offsets = settings.checkin_day_offsets
    discharge = _parse_discharge(patient)

    base: dict[str, Any] = {
        "patient_code": getattr(patient, "patient_code", ""),
        "name": getattr(patient, "name", ""),
        "phone_number": getattr(patient, "phone_number", ""),
        "diagnosis_category": getattr(patient, "diagnosis_category", "general"),
        "discharge_date": (getattr(patient, "discharge_date", "") or ""),
        "offsets_days": offsets,
        "slots": [],
        "next_call_at": None,
        "last_call_at": last_call_at.isoformat() if last_call_at else None,
        "status": STATUS_SCHEDULED,
        "days_overdue": 0,
        "scheduler_enabled": bool(settings.schedule_calls_enabled),
        "will_dial_automatically": bool(settings.schedule_calls_enabled),
    }

    if not getattr(patient, "active", True):
        base["status"] = STATUS_INACTIVE
        return base
    if discharge is None:
        base["status"] = STATUS_NO_DATE
        return base
    if not (getattr(patient, "phone_number", "") or "").strip():
        base["status"] = STATUS_NO_NUMBER
        return base

    due_time = _slot_hour(settings)
    call_day = last_call_at.date() if last_call_at else None
    slots: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    for offset in offsets:
        slot_day = discharge + timedelta(days=offset)
        slot_at = datetime.combine(slot_day, due_time)
        if call_day is not None and call_day >= slot_day:
            status = STATUS_COMPLETED
        elif now >= slot_at:
            status = STATUS_OVERDUE if now.date() > slot_day else STATUS_DUE
        else:
            status = STATUS_SCHEDULED
        pending_now = status in (STATUS_DUE, STATUS_OVERDUE)
        entry = {
            "date": slot_day.isoformat(),
            "due_at": slot_at.isoformat(timespec="minutes"),
            "status": status,
            "days_overdue": max(0, (now.date() - slot_day).days) if pending_now else 0,
        }
        slots.append(entry)
        if pending_now:
            pending.append(entry)

    base["slots"] = slots

    if pending:
        first = pending[0]
        base["status"] = first["status"]
        base["days_overdue"] = first["days_overdue"]
        base["next_call_at"] = first["due_at"]
    else:
        upcoming = [s for s in slots if s["status"] == STATUS_SCHEDULED]
        base["status"] = STATUS_SCHEDULED if upcoming else STATUS_COMPLETED
        base["next_call_at"] = upcoming[0]["due_at"] if upcoming else None
    return base


def plan_due(
    patients: list | None = None,
    last_calls: dict[str, datetime] | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> list[DueItem]:
    """Patients whose check-in slot has come due inside the grace window."""
    from app.db import service as db_service

    settings = settings or get_settings()
    now = now or datetime.now()
    patients = patients if patients is not None else db_service.list_patients()
    last_calls = (
        last_calls if last_calls is not None else db_service.last_calls_by_patient()
    )

    due: list[DueItem] = []
    for patient in patients:
        schedule = compute_schedule(
            patient, last_calls.get(patient.patient_code), now=now, settings=settings
        )
        if schedule["status"] not in (STATUS_DUE, STATUS_OVERDUE):
            continue
        # Only inside the catch-up window: an old slot must not fire weeks
        # later (that would look like a surprise call to the patient).
        if schedule["days_overdue"] > settings.schedule_grace_days:
            continue
        left, since = cooldown_state(
            patient.patient_code, now=now, settings=settings
        )
        due.append(
            DueItem(
                patient_code=patient.patient_code,
                name=patient.name,
                phone_number=patient.phone_number,
                diagnosis_category=patient.diagnosis_category or "general",
                slot=date.fromisoformat(schedule["next_call_at"][:10]),
                status=schedule["status"],
                days_overdue=schedule["days_overdue"],
                cooldown_hours=left,
                content={
                    **schedule,
                    "hours_since_last_dial": (
                        round(since, 1) if since is not None else None
                    ),
                    "cooldown_hours": round(left, 1),
                },
            )
        )
    due.sort(key=lambda item: (item.days_overdue, item.patient_code), reverse=True)
    return due


def run_tick(
    now: datetime | None = None,
    settings: Settings | None = None,
) -> TickResult:
    """One scheduler pass: find due patients and dial them (or just plan).

    The master switch is the ONLY thing that causes an automatic dial. When it
    is off, the tick still returns the full plan so the dashboard/ops endpoint
    can show "these would have been called".

    Per-patient cooldown (SCHEDULE_MIN_HOURS_BETWEEN_CALLS, default 24h): a
    patient dialed less than one window ago -- manually OR automatically --
    stays in ``planned`` but is skipped with a "cooldown" outcome instead of
    being dialed. Successful dials stamp the window start.
    """
    settings = settings or get_settings()
    now = now or datetime.now()
    result = TickResult(
        ran_at=now.isoformat(timespec="seconds"),
        enabled=bool(settings.schedule_calls_enabled),
        dry_run=bool(settings.schedule_dry_run) or not settings.schedule_calls_enabled,
    )
    due = plan_due(now=now, settings=settings)
    result.planned = [
        {
            "patient_code": item.patient_code,
            "name": item.name,
            "phone_number": item.phone_number,
            "slot": item.slot.isoformat(),
            "status": item.status,
            "days_overdue": item.days_overdue,
            "diagnosis_category": item.diagnosis_category,
            "cooldown_hours": round(item.cooldown_hours, 1),
        }
        for item in due
    ]

    if not due:
        logger.debug("Scheduler tick: nothing due.")
        return result

    if not settings.schedule_calls_enabled:
        logger.info(
            "Scheduler tick: %d patient(s) due but SCHEDULE_CALLS_ENABLED=false "
            "-- reporting only, no call placed.",
            len(due),
        )
        result.skipped = len(due)
        return result

    if settings.schedule_dry_run:
        logger.info(
            "Scheduler tick: DRY RUN -- would dial %d patient(s): %s",
            len(due),
            ", ".join(item.patient_code for item in due),
        )
        result.skipped = len(due)
        return result

    window = float(settings.schedule_min_hours_between_calls or 0)
    dial_queue = [item for item in due if item.cooldown_hours <= 0]
    for item in due:
        if item.cooldown_hours > 0:
            detail = (
                f"Auto-dialed {item.content.get('hours_since_last_dial', '?')}h ago; "
                f"next automatic dial in {round(item.cooldown_hours, 1)}h "
                f"(SCHEDULE_MIN_HOURS_BETWEEN_CALLS={window:g})."
            )
            logger.info("Scheduler tick: %s skipped by 24h cooldown: %s", item.patient_code, detail)
            result.outcomes.append(
                {
                    "patient_code": item.patient_code,
                    "status": "cooldown",
                    "detail": detail,
                }
            )
            result.skipped += 1

    budget = settings.schedule_max_dials_per_tick or len(dial_queue)
    for item in dial_queue[:budget]:
        outcome = _dial(item, settings)
        result.outcomes.append(outcome)
        if outcome.get("status") == "dialing":
            result.dialed += 1
            # The dial went out: this moment starts the patient's next 24h
            # window (manual dials stamp the same way in POST /calls).
            record_dial_stamp(
                item.patient_code, at=now, database_url=settings.database_url
            )
        else:
            result.skipped += 1
    if len(dial_queue) > budget:
        result.skipped += len(dial_queue) - budget
        logger.info(
            "Scheduler tick: %d more due patient(s) deferred to the next tick "
            "(SCHEDULE_MAX_DIALS_PER_TICK=%s).",
            len(dial_queue) - budget,
            budget,
        )
    return result


def _dial(item: DueItem, settings: Settings) -> dict[str, Any]:
    """Place one scheduled check-in call through the shared cost rails.

    Number validation, provider config checks and the dial itself all happen
    inside place_call(), so a scheduled call is rejected exactly like a manual
    one -- no second code path that could outspend the manual route.
    """
    from app.services import outbound_call
    from app.services.outbound_call import OutboundCallError

    try:
        rate_limiter.check_can_dial()
        rate_limiter.check_stream_slot()
    except rate_limiter.RateLimitExceeded as exc:
        logger.warning(
            "Scheduled call for %s skipped by cost rail: %s",
            item.patient_code,
            exc.reason,
        )
        return {
            "patient_code": item.patient_code,
            "status": "rate_limited",
            "detail": exc.reason,
        }

    try:
        result = outbound_call.place_call(
            item.phone_number,
            settings=settings,
            config={
                "diagnosis_category": item.diagnosis_category,
                "to_number": item.phone_number,
                "patient_code": item.patient_code,
                "scheduled": True,
                "scheduled_slot": item.slot.isoformat(),
            },
        )
    except OutboundCallError as exc:
        logger.error("Scheduled call for %s failed: %s", item.patient_code, exc)
        return {
            "patient_code": item.patient_code,
            "status": "failed",
            "detail": str(exc),
        }

    rate_limiter.record_dial()
    logger.info(
        "Scheduled call placed for %s (slot %s, provider id %s)",
        item.patient_code,
        item.slot.isoformat(),
        result.provider_call_id,
    )
    return {
        "patient_code": item.patient_code,
        "status": "dialing",
        "provider_call_id": result.provider_call_id,
        "to": result.to_number,
    }


# ---------------------------------------------------------------------------
# In-process cron (APScheduler)
# ---------------------------------------------------------------------------

_scheduler = None  # BackgroundScheduler once started


def start(settings: Settings | None = None, *, tick_now: bool = True):
    """Start the interval job when the master switch is on.

    Returns the scheduler (or None when disabled). The job is deliberately
    low-frequency (SCHEDULE_INTERVAL_MINUTES) and every tick re-checks the cost
    rails, so a forgotten switch cannot run up a bill:
    SCHEDULE_MAX_DIALS_PER_TICK caps each tick, and the hourly/daily budgets cap
    the day.

    ``tick_now`` runs one pass straight away (startup behaviour: a restart
    picks up due patients). The dashboard toggle passes ``tick_now=False`` so
    that ARMING THE SWITCH NEVER DIALS BY ITSELF.
    """
    global _scheduler
    settings = settings or get_settings()
    if not settings.schedule_calls_enabled:
        logger.info(
            "Scheduler off (SCHEDULE_CALLS_ENABLED=false): the backend will not "
            "dial any follow-up call automatically. Manual calls via POST /calls "
            "still work."
        )
        return None
    if _scheduler is not None:
        return _scheduler

    from apscheduler.schedulers.background import BackgroundScheduler

    minutes = max(1, settings.schedule_interval_minutes)
    _scheduler = BackgroundScheduler(timezone=None, daemon=True)
    _scheduler.add_job(
        run_tick,
        trigger="interval",
        minutes=minutes,
        id="voicecare-checkins",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=minutes * 60,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started: check-in tick every %d min (slots %02d:%02d local, "
        "offsets %s days, max %s dial(s)/tick, min %sg between calls, dry_run=%s)",
        minutes,
        settings.schedule_hour,
        settings.schedule_minute,
        settings.checkin_day_offsets,
        settings.schedule_max_dials_per_tick,
        settings.schedule_min_hours_between_calls,
        settings.schedule_dry_run,
    )
    if tick_now:
        # Run one tick straight away so a backend restart picks up due patients
        # without waiting for the first interval.
        run_tick(settings=settings)
    return _scheduler


def stop() -> None:
    """Stop the interval job (startup/shutdown + tests)."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
    except Exception:  # pragma: no cover - shutdown must never raise
        logger.exception("Error while stopping the scheduler.")
    finally:
        _scheduler = None


def status(settings: Settings | None = None) -> dict[str, Any]:
    """Scheduler state for /health and GET /schedule/status."""
    settings = settings or get_settings()
    job = None
    if _scheduler is not None:
        try:
            job = _scheduler.get_job("voicecare-checkins")
        except Exception:  # pragma: no cover
            job = None
    next_run = getattr(job, "next_run_time", None)
    return {
        "enabled": bool(settings.schedule_calls_enabled),
        "running": _scheduler is not None,
        "dry_run": bool(settings.schedule_dry_run),
        "interval_minutes": max(1, settings.schedule_interval_minutes),
        "checkin_offsets_days": settings.checkin_day_offsets,
        "slot_local_time": f"{settings.schedule_hour:02d}:{settings.schedule_minute:02d}",
        "grace_days": settings.schedule_grace_days,
        "max_dials_per_tick": settings.schedule_max_dials_per_tick,
        "min_hours_between_calls": float(settings.schedule_min_hours_between_calls or 0),
        "next_tick_at": next_run.isoformat() if next_run else None,
    }


# ---------------------------------------------------------------------------
# Runtime master switch (toggled from the dashboard, persisted in the DB)
# ---------------------------------------------------------------------------

SWITCH_KEY = "schedule_calls_enabled"


def set_switch(enabled: bool, settings: Settings | None = None) -> dict[str, Any]:
    """Turn the master switch on/off at runtime and persist the choice.

    Arming starts the interval job WITHOUT an immediate tick -- flipping the
    switch must never place a call by itself; due patients are dialed on the
    next tick or an explicit run-now, through the usual cost rails.
    """
    from app.db import service as db_service

    settings = settings or get_settings()
    settings.schedule_calls_enabled = bool(enabled)
    db_service.set_setting(SWITCH_KEY, "true" if enabled else "false")
    if enabled:
        start(settings, tick_now=False)
    else:
        stop()
    logger.info(
        "Scheduler switch turned %s (persisted to the DB)",
        "ON" if enabled else "OFF",
    )
    return status(settings)


def apply_saved_switch(settings: Settings | None = None) -> bool:
    """Re-apply a persisted switch after a restart (startup path).

    Returns the effective enabled state: the saved DB row wins when present,
    otherwise the .env SCHEDULE_CALLS_ENABLED default stands (fresh database).
    """
    from app.db import service as db_service

    settings = settings or get_settings()
    saved = db_service.get_setting(SWITCH_KEY)
    if saved is None:
        return bool(settings.schedule_calls_enabled)
    settings.schedule_calls_enabled = saved == "true"
    logger.info(
        "Scheduler switch restored from the database: %s",
        "ON" if settings.schedule_calls_enabled else "OFF",
    )
    return settings.schedule_calls_enabled

