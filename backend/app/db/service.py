"""
Persistence repositories (Step 6): save one row per call, link patients.

`record_call()` is the single entry point used by the call-flow driver: it
creates the row from the (already computed) dialogue + assessment and links
the patient by patient_code first, then by phone number, so the TC2
"call linked to the right patient record" holds even when only the dialed
number is known.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import select

from app.db.engine import init_db, new_session
from app.db.models import CallRecord, Patient, StaffUser

logger = logging.getLogger("voicecare.db")

# Monotonic-ish clock the call flow reports (wall seconds of the dialogue).


def find_patient(
    patient_code: str | None = None,
    phone_number: str | None = None,
    database_url: str | None = None,
) -> Patient | None:
    """Look up a patient by code first, then by phone number."""
    init_db(database_url)
    with new_session(database_url) as session:
        if patient_code:
            patient = session.exec(
                select(Patient).where(Patient.patient_code == patient_code)
            ).first()
            if patient:
                return patient
        if phone_number:
            return session.exec(
                select(Patient).where(Patient.phone_number == phone_number)
            ).first()
    return None


def record_call(
    *,
    dialogue,
    assessment,
    provider_call_id: str = "",
    to_number: str = "",
    patient_code: str | None = None,
    ended_reason: str,
    started_at: datetime | None = None,
    duration_sec: float = 0.0,
    database_url: str | None = None,
) -> CallRecord | None:
    """Create the CallRecord row for a finished call. Returns None (and logs)
    instead of raising -- a database problem must never hide the call's risk
    decision, which is also in the logs."""
    init_db(database_url)
    category = getattr(dialogue, "diagnosis_category", "general")
    try:
        patient = find_patient(patient_code, to_number or None, database_url)
        record = CallRecord(
            provider_call_id=provider_call_id or "",
            patient_id=patient.id if patient else None,
            patient_code=(patient.patient_code if patient else (patient_code or "")),
            phone_number=(patient.phone_number if patient else to_number),
            diagnosis_category=category,
            started_at=started_at or datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            duration_sec=round(duration_sec, 2),
            ended_reason=ended_reason,
            risk_level=assessment.risk_level,
            risk_score=round(assessment.score, 1),
        )
        record.set_answers(dialogue.summary())
        record.set_risk_reasons(list(assessment.reasons))
        record.set_findings(
            [
                {
                    "id": f.symptom_id, "label": f.label, "severity": f.severity,
                    "red_flag": f.red_flag, "points": f.points,
                    "matched_text": f.matched_text, "source": f.source,
                }
                for f in assessment.findings
            ]
        )
        with new_session(database_url) as session:
            session.add(record)
            session.commit()
            session.refresh(record)
        logger.info(
            "Call persisted: id=%d patient=%s risk=%s answers=%d reason=%s",
            record.id, record.patient_code or "-",
            record.risk_level, len(record.get_answers()), ended_reason,
        )
        return record
    except Exception:
        logger.exception("Could not persist the call record (risk is still in the logs)")
        return None


def attach_alert(
    record_id: int,
    status: str,
    detail: str = "",
    database_url: str | None = None,
) -> None:
    """Update alert_status / alert_detail after the Step 7 send attempt."""
    try:
        with new_session(database_url) as session:
            record = session.get(CallRecord, record_id)
            if record:
                record.alert_status = status
                record.alert_detail = detail[:500]
                session.add(record)
                session.commit()
    except Exception:
        logger.exception("Could not update alert status for record id=%s", record_id)


def list_calls(
    limit: int = 100,
    risk_level: str | None = None,
    patient_code: str | None = None,
    reviewed: bool | None = None,
    database_url: str | None = None,
) -> list[CallRecord]:
    """Newest-first call rows (dashboard / verification endpoint)."""
    init_db(database_url)
    with new_session(database_url) as session:
        statement = select(CallRecord)
        if risk_level:
            statement = statement.where(CallRecord.risk_level == risk_level)
        if patient_code:
            statement = statement.where(CallRecord.patient_code == patient_code)
        if reviewed is not None:
            statement = statement.where(CallRecord.reviewed == reviewed)
        statement = statement.order_by(CallRecord.id.desc()).limit(limit)
        rows = session.exec(statement).all()
        # Detach fully: access lazy attrs inside the session scope.
        for row in rows:
            _ = (row.get_answers(), row.get_findings(), row.get_risk_reasons())
        session.expunge_all()
        return list(rows)


def get_call(record_id: int, database_url: str | None = None) -> CallRecord | None:
    """One call row by primary key (dashboard detail view)."""
    init_db(database_url)
    with new_session(database_url) as session:
        row = session.get(CallRecord, record_id)
        if row is None:
            return None
        _ = (row.get_answers(), row.get_findings(), row.get_risk_reasons())
        session.expunge(row)
        return row


def update_call(
    record_id: int, database_url: str | None = None, **fields
) -> CallRecord | None:
    """Patch dashboard-column fields on a call row (review flag, note, close)."""
    allowed = {"reviewed", "nurse_note", "closed_by", "closed_at"}
    patch = {k: v for k, v in fields.items() if k in allowed}
    if not patch:
        return get_call(record_id, database_url)
    try:
        with new_session(database_url) as session:
            row = session.get(CallRecord, record_id)
            if row is None:
                return None
            for key, value in patch.items():
                setattr(row, key, value)
            session.add(row)
            session.commit()
            session.refresh(row)
            _ = (row.get_answers(), row.get_findings(), row.get_risk_reasons())
            session.expunge(row)
            return row
    except Exception:
        logger.exception("Could not update call record id=%s", record_id)
        return None


def last_calls_by_patient(database_url: str | None = None) -> dict[str, datetime]:
    """Latest call start time per patient_code (scheduler due-date maths)."""
    init_db(database_url)
    result: dict[str, datetime] = {}
    with new_session(database_url) as session:
        rows = session.exec(
            select(CallRecord.patient_code, CallRecord.started_at).order_by(
                CallRecord.id.desc()
            )
        ).all()
    for patient_code, started_at in rows:
        if patient_code and patient_code not in result and started_at is not None:
            result[patient_code] = started_at
    return result


def stats(database_url: str | None = None) -> dict:
    """Headline counters for the dashboard cards."""
    init_db(database_url)
    with new_session(database_url) as session:
        calls = session.exec(select(CallRecord)).all()
        patients = session.exec(select(Patient)).all()
        staff = session.exec(select(StaffUser)).all()
        for row in calls:
            _ = row.get_answers()
    by_risk = {"low": 0, "medium": 0, "high": 0, "unknown": 0}
    alerts_sent = 0
    reviewed = 0
    for row in calls:
        by_risk[row.risk_level if row.risk_level in by_risk else "unknown"] += 1
        if row.alert_status == "sent":
            alerts_sent += 1
        if row.reviewed:
            reviewed += 1
    last_call_at = max((row.started_at for row in calls if row.started_at), default=None)
    return {
        "calls_total": len(calls),
        "calls_by_risk": by_risk,
        "alerts_sent": alerts_sent,
        "alerts_open": max(0, by_risk["high"] - reviewed),
        "calls_reviewed": reviewed,
        "patients_total": len(patients),
        "patients_active": sum(1 for p in patients if p.active),
        "staff_total": len(staff),
        "last_call_at": last_call_at.isoformat() if last_call_at else None,
    }


def list_patients(
    include_inactive: bool = True, database_url: str | None = None
) -> list[Patient]:
    init_db(database_url)
    with new_session(database_url) as session:
        statement = select(Patient)
        if not include_inactive:
            statement = statement.where(Patient.active == True)  # noqa: E712
        rows = session.exec(statement.order_by(Patient.patient_code)).all()
        session.expunge_all()
        return list(rows)


def upsert_patient(
    *,
    patient_code: str,
    name: str = "",
    phone_number: str = "",
    diagnosis_category: str = "general",
    discharge_date: str = "",
    notes: str = "",
    language_pref: str = "en",
    active: bool = True,
    database_url: str | None = None,
) -> tuple[str, Patient]:
    """Create or update the patient with this code. Returns (action, patient)."""
    init_db(database_url)
    with new_session(database_url) as session:
        existing = session.exec(
            select(Patient).where(Patient.patient_code == patient_code)
        ).first()
        if existing:
            existing.name = name
            existing.phone_number = phone_number
            existing.diagnosis_category = diagnosis_category
            existing.discharge_date = discharge_date
            existing.notes = notes
            existing.language_pref = language_pref
            existing.active = active
            session.add(existing)
            session.commit()
            session.refresh(existing)
            logger.info("Patient updated: %s", existing.patient_code)
            return "updated", existing
        patient = Patient(
            patient_code=patient_code,
            name=name,
            phone_number=phone_number,
            diagnosis_category=diagnosis_category,
            discharge_date=discharge_date,
            notes=notes,
            language_pref=language_pref,
            active=active,
        )
        session.add(patient)
        session.commit()
        session.refresh(patient)
        logger.info("Patient created: %s", patient.patient_code)
        return "created", patient


def delete_patient(patient_code: str, database_url: str | None = None) -> bool:
    """Remove a patient record (admin 'discharge'). Call history is kept."""
    init_db(database_url)
    try:
        with new_session(database_url) as session:
            patient = session.exec(
                select(Patient).where(Patient.patient_code == patient_code)
            ).first()
            if patient is None:
                return False
            session.delete(patient)
            session.commit()
            logger.info("Patient deleted: %s", patient_code)
            return True
    except Exception:
        logger.exception("Could not delete patient %s", patient_code)
        return False


# ---------------------------------------------------------------------------
# Staff accounts (Step 9)
# ---------------------------------------------------------------------------

def get_staff_by_username(
    username: str, database_url: str | None = None
) -> StaffUser | None:
    init_db(database_url)
    with new_session(database_url) as session:
        row = session.exec(
            select(StaffUser).where(StaffUser.username == username.strip().lower())
        ).first()
        if row is None:
            return None
        session.expunge(row)
        return row


def list_staff(database_url: str | None = None) -> list[StaffUser]:
    init_db(database_url)
    with new_session(database_url) as session:
        rows = session.exec(select(StaffUser).order_by(StaffUser.username)).all()
        session.expunge_all()
        return list(rows)


def create_staff(
    *,
    username: str,
    password: str,
    role: str = "nurse",
    display_name: str = "",
    hospital: str = "",
    database_url: str | None = None,
) -> StaffUser:
    """Create a staff login. Caller hashes the password (app.core.security)."""
    from app.core.security import hash_password

    init_db(database_url)
    password_hash, password_salt = hash_password(password)
    with new_session(database_url) as session:
        user = StaffUser(
            username=username.strip().lower(),
            display_name=display_name or username,
            role=role,
            hospital=hospital,
            password_hash=password_hash,
            password_salt=password_salt,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("Staff created: %s (%s)", user.username, user.role)
        return user


def touch_last_login(username: str, database_url: str | None = None) -> None:
    try:
        with new_session(database_url) as session:
            user = session.exec(
                select(StaffUser).where(StaffUser.username == username.strip().lower())
            ).first()
            if user is None:
                return
            user.last_login_at = datetime.now(timezone.utc)
            session.add(user)
            session.commit()
    except Exception:  # never block a login on bookkeeping
        logger.exception("Could not update last_login_at for %s", username)


def ensure_default_admin(
    *,
    username: str,
    password: str,
    display_name: str = "",
    hospital: str = "",
    database_url: str | None = None,
) -> str:
    """Seed the first admin when the staff table is empty.

    Returns "created", "exists", or "skipped" (no username/password given).
    This is how the demo gets a login without a signup flow.
    """
    if not username or not password:
        return "skipped"
    init_db(database_url)
    with new_session(database_url) as session:
        count = len(session.exec(select(StaffUser)).all())
    if count:
        return "exists"
    create_staff(
        username=username,
        password=password,
        role="admin",
        display_name=display_name or username,
        hospital=hospital,
        database_url=database_url,
    )
    return "created"
