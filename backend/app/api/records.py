"""
Records API (Step 6): read the persisted calls + manage patient records.

Auth accepts either the machine X-Api-Key (scripts, the live-test plan) or a
staff bearer token from the dashboard (Step 9). Role rules follow the README
role table:

    review / annotate a call -> nurse | doctor | admin
    close a case             -> doctor | admin
    manage patients          -> admin
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.security import AuthContext, require_auth, require_roles
from app.db.models import iso_utc
from app.db import service as db_service
from app.db.models import Patient
from app.services import alerts as alerts_service
from app.services import email_alerts as email_alerts_service
from app.services.dialogue import CATEGORIES as DIAGNOSIS_CATEGORIES

logger = logging.getLogger("voicecare.records")

router = APIRouter(prefix="/records")


class PatientUpsert(BaseModel):
    """Create or update a patient (matched on patient_code)."""

    patient_code: str = Field(min_length=1, max_length=32)
    name: str = ""
    phone_number: str = ""
    diagnosis_category: str = "general"
    discharge_date: str = ""          # ISO date: YYYY-MM-DD
    notes: str = ""
    language_pref: str = "en"         # en | ta | si (Step 10)
    active: bool = True
    # Care team for HIGH-risk alert emails: staff usernames (nurse/doctor).
    # None = leave the current assignment unchanged; [] = clear it.
    assigned_staff: list[str] | None = None


def _validate_assigned_staff(usernames: list[str]) -> list[str]:
    """Normalise the care-team list and refuse unknown/non-nurse-doctor accounts.

    A typo here would silently mean "nobody gets the alert", so it fails at
    save time (422) instead of at the next HIGH-risk call. Active nurse and
    doctor accounts only -- admins are never emailed and a deactivated account
    can not be reached. Returns lower-cased, de-duplicated usernames.
    """
    cleaned: list[str] = []
    for raw in usernames:
        name = (raw or "").strip().lower()
        if name and name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        return []
    known = {u.username: u for u in db_service.list_staff()}
    invalid: list[str] = []
    for name in cleaned:
        user = known.get(name)
        if user is None:
            invalid.append(f"{name!r}: no such staff account")
        elif not user.active:
            invalid.append(f"{name!r}: account is deactivated")
        elif user.role not in ("nurse", "doctor"):
            invalid.append(f"{name!r}: role {user.role!r} cannot be assigned (nurse/doctor only)")
    if invalid:
        raise HTTPException(
            status_code=422,
            detail="Invalid assigned_staff: " + "; ".join(invalid),
        )
    return cleaned


class CallPatch(BaseModel):
    """Dashboard workflow fields a nurse/doctor may set on a call row."""

    reviewed: bool | None = None
    nurse_note: str | None = None
    close_case: bool = False          # true = doctor closes an escalated case


def _record_to_dict(r) -> dict:
    return {
        "id": r.id,
        "provider_call_id": r.provider_call_id,
        "patient_code": r.patient_code,
        "phone_number": r.phone_number,
        "diagnosis_category": r.diagnosis_category,
        "started_at": iso_utc(r.started_at),
        "finished_at": iso_utc(r.finished_at),
        "duration_sec": r.duration_sec,
        "ended_reason": r.ended_reason,
        "answers": r.get_answers(),
        "risk_level": r.risk_level,
        "risk_score": r.risk_score,
        "risk_reasons": r.get_risk_reasons(),
        "findings": r.get_findings(),
        "alert_status": r.alert_status,
        "alert_detail": r.alert_detail,
        "alert_message": r.alert_message,
        "alert_recipients": r.get_alert_recipients(),
        "reviewed": r.reviewed,
        "nurse_note": r.nurse_note,
        "closed_by": r.closed_by,
        "closed_at": iso_utc(r.closed_at),
    }


def _patient_to_dict(p: Patient) -> dict:
    return {
        "id": p.id,
        "patient_code": p.patient_code,
        "name": p.name,
        "phone_number": p.phone_number,
        "diagnosis_category": p.diagnosis_category,
        "discharge_date": p.discharge_date,
        "notes": p.notes,
        "language_pref": p.language_pref,
        "active": p.active,
        "assigned_staff": p.get_assigned_staff(),
        "created_at": iso_utc(p.created_at),
    }


@router.get("/calls")
def get_call_records(
    limit: int = 100,
    risk_level: str | None = Query(default=None, pattern="^(low|medium|high)$"),
    patient_code: str | None = None,
    reviewed: bool | None = None,
    context: AuthContext = Depends(require_auth),
) -> dict:
    """Persisted call rows, newest first (Step 6 TC1 verification)."""
    rows = db_service.list_calls(
        limit=max(1, min(limit, 500)),
        risk_level=risk_level,
        patient_code=patient_code,
        reviewed=reviewed,
    )
    return {"count": len(rows), "calls": [_record_to_dict(r) for r in rows]}


@router.get("/calls/{record_id}")
def get_call_record(
    record_id: int, context: AuthContext = Depends(require_auth)
) -> dict:
    """One call in full (transcript + assessment) -- the detail view."""
    row = db_service.get_call(record_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No call record with id {record_id}.")
    return _record_to_dict(row)


@router.patch("/calls/{record_id}")
def patch_call_record(
    record_id: int,
    body: CallPatch,
    context: AuthContext = Depends(require_roles("nurse", "doctor", "admin")),
) -> dict:
    """Update the dashboard workflow fields on a call row.

    Marking a case reviewed/annotated is a nurse+ action; closing the case is
    reserved for a doctor (or admin) per the README role table.
    """
    if db_service.get_call(record_id) is None:
        raise HTTPException(status_code=404, detail=f"No call record with id {record_id}.")

    fields: dict = {}
    if body.reviewed is not None:
        fields["reviewed"] = body.reviewed
    if body.nurse_note is not None:
        fields["nurse_note"] = body.nurse_note
    if body.close_case:
        if not context.allows(("doctor", "admin")):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Closing a case requires doctor or admin "
                    f"(your role: {context.role})."
                ),
            )
        fields["closed_by"] = context.subject or context.name or "api-key"
        fields["closed_at"] = datetime.now(timezone.utc)
        fields["reviewed"] = True

    row = db_service.update_call(record_id, **fields)
    if row is None:
        raise HTTPException(status_code=500, detail="Could not update the call record.")
    logger.info(
        "Call %s updated by %s: %s", record_id, context.subject or "api-key", list(fields)
    )
    return {"status": "updated", "call": _record_to_dict(row)}


@router.post("/calls/{record_id}/alert")
def send_call_alert(
    record_id: int,
    context: AuthContext = Depends(require_roles("nurse", "doctor", "admin")),
) -> dict:
    """Re-run the alert for a finished HIGH-risk call and send it now.

    2 Oct 2026: alerts are sent automatically at the end of a call
    (ALERT_DELIVERY=whatsapp), but a call can still end up with
    alert_status='ready'/'failed' -- the conversation was closed, the backend
    was still starting up with the old config, the delivery was off. The
    message is already on the row, so sending it later is just re-running the
    prepare+deliver step instead of re-calling the patient.
    """
    settings = get_settings()
    row = db_service.get_call(record_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No call record with id {record_id}.")
    if row.risk_level != "high":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Only HIGH-risk calls alert (this one is {row.risk_level})."
            ),
        )
    patient = db_service.find_patient(patient_code=row.patient_code or None)
    outcome = alerts_service.prepare_alert(row, patient, settings)
    # Email is the second channel (see app/services/email_alerts.py): the
    # patient's ASSIGNED care team is re-emailed too, so a case that failed
    # earlier reaches the same people a fresh call would have.
    recipients = email_alerts_service.deliver_alert_emails(row, patient, settings)
    db_service.attach_alert(
        record_id,
        outcome.status,
        detail=outcome.detail,
        message=outcome.message,
        recipients=recipients,
    )
    logger.info(
        "Alert re-sent for record %s by %s: %s (%d email recipient(s))",
        record_id, context.subject or "api-key", outcome.status, len(recipients),
    )
    updated = db_service.get_call(record_id)
    return {"status": outcome.status, "detail": outcome.detail,
            "call": _record_to_dict(updated) if updated else None}


@router.get("/patients")
def get_patients(
    include_inactive: bool = True, context: AuthContext = Depends(require_auth)
) -> dict:
    rows = db_service.list_patients(include_inactive=include_inactive)
    return {"count": len(rows), "patients": [_patient_to_dict(p) for p in rows]}


@router.post("/patients", status_code=201)
def upsert_patient(
    body: PatientUpsert, context: AuthContext = Depends(require_roles("admin"))
) -> dict:
    """Create or update a patient record (matched on patient_code).

    The dashboard's "Edit" action is this same endpoint: the form comes back
    pre-filled from GET /records/patients and posting it updates the existing
    row (status "updated") instead of creating a duplicate.
    """
    if body.diagnosis_category not in DIAGNOSIS_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=(
                "diagnosis_category must be one of: "
                + ", ".join(sorted(DIAGNOSIS_CATEGORIES))
            ),
        )
    assigned = (
        _validate_assigned_staff(body.assigned_staff)
        if body.assigned_staff is not None
        else None
    )
    action, patient = db_service.upsert_patient(
        patient_code=body.patient_code.strip(),
        name=body.name,
        phone_number=body.phone_number,
        diagnosis_category=body.diagnosis_category,
        discharge_date=body.discharge_date,
        notes=body.notes,
        language_pref=body.language_pref,
        active=body.active,
        assigned_staff=assigned,
    )
    return {"status": action, "patient": _patient_to_dict(patient)}


@router.delete("/patients/{patient_code}")
def delete_patient(
    patient_code: str, context: AuthContext = Depends(require_roles("admin"))
) -> dict:
    """Remove a patient record. Existing call history is kept."""
    if not db_service.delete_patient(patient_code):
        raise HTTPException(
            status_code=404, detail=f"No patient with code {patient_code!r}."
        )
    return {"status": "deleted", "patient_code": patient_code}


@router.get("/stats")
def get_stats(context: AuthContext = Depends(require_auth)) -> dict:
    """Aggregate counters (dashboard cards)."""
    return db_service.stats()
