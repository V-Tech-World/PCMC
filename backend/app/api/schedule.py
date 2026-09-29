"""
Scheduler API (Step 8): the follow-up check-in plan, read + manual tick.

    GET  /schedule          -- every patient with their full check-in timeline
    GET  /schedule/due      -- just the patients due right now (no dialing)
    GET  /schedule/status   -- is the automatic dialer on? when is the next tick?
    POST /schedule/run-now  -- run one tick immediately (admin)
    POST /schedule/enabled  -- turn the automatic dialer on/off (admin, persisted)

`run-now` exists so the cronjob can be demonstrated without waiting for the
interval -- and it is the honest way to prove the safety property: with
SCHEDULE_CALLS_ENABLED=false it returns the dial plan and places **no** call.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.security import AuthContext, require_auth, require_roles
from app.db import service as db_service
from app.services import scheduler

logger = logging.getLogger("voicecare.schedule")

router = APIRouter(prefix="/schedule")


class RunNowRequest(BaseModel):
    """Optional: pretend it is a different moment (testing the due logic)."""

    at: str | None = None            # ISO datetime, e.g. "2025-06-11T09:30"
    dry_run_override: bool | None = None


@router.get("")
def get_schedule(context: AuthContext = Depends(require_auth)) -> dict:
    """Check-in timeline per patient -- powers the dashboard's schedule column."""
    settings = get_settings()
    patients = db_service.list_patients()
    last_calls = db_service.last_calls_by_patient()
    rows = [
        scheduler.compute_schedule(p, last_calls.get(p.patient_code), settings=settings)
        for p in patients
    ]
    # One-shot cooldown annotation per patient (24h auto-dial window): the
    # board shows each due row with its hours_until_next_autodial, while the
    # underlying slots/timeline maths stay untouched. A cooled-down patient no
    # longer counts as "will dial automatically" this tick.
    cooldown = {
        code: round(scheduler.cooldown_hours_left(code, settings=settings), 1)
        for code in {row["patient_code"] for row in rows}
    }
    for row in rows:
        left = cooldown.get(row["patient_code"], 0.0)
        row["cooldown_hours"] = left
        if left > 0:
            row["will_dial_automatically"] = False
    due_now = [r for r in rows if r["status"] in (scheduler.STATUS_DUE, scheduler.STATUS_OVERDUE)]
    return {
        "scheduler": scheduler.status(settings),
        "count": len(rows),
        "due_count": len(due_now),
        "patients": rows,
    }


@router.get("/due")
def get_due(context: AuthContext = Depends(require_auth)) -> dict:
    """Patients whose check-in window is open now (planning only, never dials)."""
    settings = get_settings()
    due = scheduler.plan_due(settings=settings)
    return {
        "count": len(due),
        "scheduler_enabled": bool(settings.schedule_calls_enabled),
        "gate_days": settings.schedule_grace_days,
        "due": [
            {
                "patient_code": item.patient_code,
                "name": item.name,
                "phone_number": item.phone_number,
                "diagnosis_category": item.diagnosis_category,
                "slot": item.slot.isoformat(),
                "status": item.status,
                "days_overdue": item.days_overdue,
                "cooldown_hours": round(item.cooldown_hours, 1),
            }
            for item in due
        ],
    }


def _note(settings) -> str:
    return (
        "Automatic follow-up calls are OFF: the backend will not dial on its own. "
        "Manual calls from the dashboard still work."
        if not settings.schedule_calls_enabled
        else "Automatic follow-up calls are ON and will be placed when a patient is due."
    )


class SwitchRequest(BaseModel):
    """Turn the automatic dialer on (true) or off (false)."""

    enabled: bool


@router.get("/status")
def get_status(context: AuthContext = Depends(require_auth)) -> dict:
    """Is the automatic dialer running, and when does it next wake up?"""
    settings = get_settings()
    state = scheduler.status(settings)
    state["note"] = _note(settings)
    return state


@router.post("/enabled")
def set_enabled(
    body: SwitchRequest,
    context: AuthContext = Depends(require_roles("admin")),
) -> dict:
    """Turn the automatic dialer ON/OFF from the dashboard (admin).

    The choice is persisted (AppSetting), so it survives a backend restart --
    SCHEDULE_CALLS_ENABLED in .env stays the default for a fresh database.
    Safe by construction: arming starts the interval job WITHOUT an immediate
    tick, so no call is ever placed by flipping the switch; due patients are
    dialed on the next tick (or an explicit run-now), through the normal cost
    rails.
    """
    settings = get_settings()
    state = scheduler.set_switch(body.enabled, settings=settings)
    state["note"] = _note(settings)
    state["message"] = (
        f"Automatic follow-up calls turned {'ON' if body.enabled else 'OFF'} (saved)."
    )
    logger.info(
        "Scheduler switch turned %s by %s",
        "ON" if body.enabled else "OFF",
        context.subject or "api-key",
    )
    return state


@router.post("/run-now")
def run_now(
    body: RunNowRequest | None = None,
    context: AuthContext = Depends(require_roles("admin")),
) -> dict:
    """Run one scheduler tick immediately (admin).

    Safe by construction: with SCHEDULE_CALLS_ENABLED=false this only reports
    what a tick *would* do. With it true it goes through the normal cost rails.
    """
    settings = get_settings()
    from datetime import datetime

    now = None
    if body is not None and body.at:
        try:
            now = datetime.fromisoformat(body.at)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="'at' must be an ISO datetime, e.g. 2025-06-11T09:30"
            ) from exc

    result = scheduler.run_tick(now=now, settings=settings)
    logger.info(
        "Manual scheduler tick by %s: planned=%d dialed=%d skipped=%d",
        context.subject or "api-key",
        len(result.planned),
        result.dialed,
        result.skipped,
    )
    return {
        "ran_at": result.ran_at,
        "enabled": result.enabled,
        "dry_run": result.dry_run,
        "planned_count": len(result.planned),
        "dialed": result.dialed,
        "skipped": result.skipped,
        "planned": result.planned,
        "outcomes": result.outcomes,
        "message": (
            "Automatic calls are OFF -- plan reported, no call placed."
            if not result.enabled
            else "Tick complete."
        ),
    }
