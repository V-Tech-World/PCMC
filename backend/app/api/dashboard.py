"""
Dashboard API (Step 9): one aggregated payload for the landing screen.

The frontend dashboard sketch is a card grid (total calls, open alerts,
patients, calls due) plus a recent-activity table. Rather than have the browser
fan out to five endpoints on load, `/dashboard/summary` returns everything the
landing screen needs in one round trip.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends

from app.core import rate_limiter
from app.core.config import get_settings
from app.core.security import AuthContext, require_auth
from app.db.models import iso_utc
from app.db import service as db_service
from app.services import alerts as alerts_service
from app.services import email_alerts as email_alerts_service
from app.services import scheduler

logger = logging.getLogger("voicecare.dashboard")

router = APIRouter(prefix="/dashboard")


def _recent_call_row(record) -> dict:
    """Compact row for the activity table (no transcript body -- that's the
    detail view's job, GET /records/calls/{id})."""
    return {
        "id": record.id,
        "patient_code": record.patient_code,
        "phone_number": record.phone_number,
        "diagnosis_category": record.diagnosis_category,
        "started_at": iso_utc(record.started_at),
        "duration_sec": record.duration_sec,
        "ended_reason": record.ended_reason,
        "risk_level": record.risk_level,
        "risk_score": record.risk_score,
        "risk_reasons": record.get_risk_reasons(),
        "symptom_count": len(record.get_findings()),
        "alert_status": record.alert_status,
        "alert_recipients": record.get_alert_recipients(),
        "reviewed": record.reviewed,
        "closed_by": record.closed_by,
    }


@router.get("/summary")
def summary(context: AuthContext = Depends(require_auth)) -> dict:
    """Everything the dashboard landing screen renders, in one call."""
    settings = get_settings()
    stats = db_service.stats()

    patients = db_service.list_patients()
    last_calls = db_service.last_calls_by_patient()
    schedules = [
        scheduler.compute_schedule(p, last_calls.get(p.patient_code), settings=settings)
        for p in patients
    ]
    due = [
        s for s in schedules
        if s["status"] in (scheduler.STATUS_DUE, scheduler.STATUS_OVERDUE)
    ]
    # "Due" is everything whose slot is open; "callable" is only what the
    # scheduler may actually dial (grace window still open).
    callable_due = [
        s for s in due
        if s["days_overdue"] <= settings.schedule_grace_days
    ]

    recent = db_service.list_calls(limit=10)
    open_alerts = [
        _recent_call_row(r)
        for r in db_service.list_calls(risk_level="high", reviewed=False, limit=20)
    ]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cards": {
            "calls_total": stats["calls_total"],
            "calls_by_risk": stats["calls_by_risk"],
            "calls_reviewed": stats["calls_reviewed"],
            "alerts_sent": stats["alerts_sent"],
            "alerts_ready": stats["alerts_ready"],
            "alerts_open": len(open_alerts),
            "patients_total": stats["patients_total"],
            "patients_active": stats["patients_active"],
            "calls_due": len(callable_due),
            "calls_due_total": len(due),
            "last_call_at": stats["last_call_at"],
        },
        "recent_calls": [_recent_call_row(r) for r in recent],
        "open_alerts": open_alerts[:10],
        "due_patients": [
            {
                "patient_code": s["patient_code"],
                "name": s["name"],
                "phone_number": s["phone_number"],
                "next_call_at": s["next_call_at"],
                "status": s["status"],
                "days_overdue": s["days_overdue"],
            }
            for s in sorted(callable_due, key=lambda s: s["days_overdue"], reverse=True)
        ],
        "scheduler": scheduler.status(settings),
        "alerts": {
            "enabled": bool(settings.alerts_enabled),
            "delivery": alerts_service.delivery_mode(settings),
            "channel": (
                "prepared in-dashboard (manual delivery)"
                if alerts_service.delivery_mode(settings)
                == alerts_service.DELIVERY_READY
                else "whatsapp (Zernio inbox)"
            ),
            "conversation_configured": bool(settings.alert_conversation_id),
            # 4 Oct 2026: the second channel -- named staff, routed to each
            # patient's assigned care team. Surfaced here so the UI can show
            # when it is off.
            "email_enabled": bool(settings.email_alerts_enabled),
            "email_configured": email_alerts_service.email_alerts_configured(settings),
            "email_sender": settings.sender_email or "",
        },
        "cost_rails": {
            "max_call_duration_sec": settings.max_call_duration_sec,
            "max_calls_per_hour": settings.max_calls_per_hour,
            "max_calls_per_day": settings.max_calls_per_day,
            "max_concurrent_calls": settings.max_concurrent_calls,
            "live_streams": rate_limiter.concurrent_calls(),
        },
    }


@router.get("/activity")
def activity(
    limit: int = 50, context: AuthContext = Depends(require_auth)
) -> dict:
    """Recent call activity only (used by the table's refresh button)."""
    rows = db_service.list_calls(limit=max(1, min(limit, 200)))
    return {"count": len(rows), "calls": [_recent_call_row(r) for r in rows]}
