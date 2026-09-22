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
        due.append(
            DueItem(
                patient_code=patient.patient_code,
                name=patient.name,
                phone_number=patient.phone_number,
                diagnosis_category=patient.diagnosis_category or "general",
                slot=date.fromisoformat(schedule["next_call_at"][:10]),
                status=schedule["status"],
                days_overdue=schedule["days_overdue"],
                content=schedule,
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

    budget = settings.schedule_max_dials_per_tick or len(due)
    for item in due[:budget]:
        outcome = _dial(item, settings)
        result.outcomes.append(outcome)
        if outcome.get("status") == "dialing":
            result.dialed += 1
        else:
            result.skipped += 1
    if len(due) > budget:
        result.skipped += len(due) - budget
        logger.info(
            "Scheduler tick: %d more due patient(s) deferred to the next tick "
            "(SCHEDULE_MAX_DIALS_PER_TICK=%s).",
            len(due) - budget,
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


def start(settings: Settings | None = None):
    """Start the interval job when SCHEDULE_CALLS_ENABLED=true.

    Returns the scheduler (or None when disabled). The job is deliberately
    low-frequency (SCHEDULE_INTERVAL_MINUTES) and every tick re-checks the cost
    rails, so a forgotten switch cannot run up a bill:
    SCHEDULE_MAX_DIALS_PER_TICK caps each tick, and the hourly/daily budgets cap
    the day.
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
        "offsets %s days, max %s dial(s)/tick, dry_run=%s)",
        minutes,
        settings.schedule_hour,
        settings.schedule_minute,
        settings.checkin_day_offsets,
        settings.schedule_max_dials_per_tick,
        settings.schedule_dry_run,
    )
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
        "next_tick_at": next_run.isoformat() if next_run else None,
    }
