"""
Tables (Step 6): patients + one row per call.

Design notes:
- A call row is created at the END of a call (the dialogue object holds the
  answers while the call is live); "one row per call" is the storage contract
  the dashboard (Step 9) and alerts (Step 7) build on.
- The transcript is stored as a JSON list of per-question summaries
  ({question_id, kind, interpretation, transcript}) -- exactly what
  CallDialogue.summary() produces, plus the findings/risk from the Step 4
  engine, so no information is ever lost between the raw audio (WAV files,
  Step 3) and the structured row.
- patient_code is denormalised onto CallRecord so a dashboard listing never
  needs a join, and a deleted/moved patient still leaves an auditable trail.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import Column, Field, SQLModel, Text


def _utcnow() -> datetime:
    """Timezone-aware UTC.

    SQLModel >= 0.0.30 types datetime columns as UTCDateTime, which REJECTS a
    naive value on write ("Datetime values must have timezone information").
    Under the .venv (sqlmodel 0.0.47) that made `record_call` raise and the row
    was never saved (live 4 Oct 2026). So: always aware in, aware out.
    """
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    """Serialise a stored (naive-UTC) datetime as an explicit-offset ISO string.

    Naive datetimes read back from SQLite carry no offset, and `new Date(...)`
    in the browser would then treat them as *local* time -- silently shifting
    every timestamp by the viewer's UTC offset. Re-attaching UTC keeps the API
    honest about what it stores.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _aware_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to aware UTC for SQLModel's UTCDateTime columns.

    Callers legitimately pass naive datetimes -- the scheduler works in local
    wall-clock time (`datetime.now()`, `datetime.combine(day, time)`) and stores
    those as ISO strings, and any naive value reaching a datetime column raises
    "Datetime values must have timezone information" and loses the row.
    Naive input is therefore interpreted as UTC, which is what it has always
    meant everywhere else in this codebase.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serialisable: {type(value)!r}")


class Patient(SQLModel, table=True):
    """A registered post-discharge patient (seeded by the hospital)."""

    id: int | None = Field(default=None, primary_key=True)
    patient_code: str = Field(unique=True, index=True)     # e.g. "P-0001"
    name: str = ""
    # Demographics (10 Oct 2026): shown on the Patient Details screen and in
    # the info card. All optional -- rows created before this keep ""/None
    # until an admin edits them (schema sync adds the columns as NULL/"").
    title: str = ""           # Mr | Mrs | Miss ("" = not given)
    gender: str = ""          # male | female | other ("" = not given)
    age: int | None = None    # None = unknown
    nic_number: str = ""      # national identity card number
    phone_number: str = ""                                  # E.164, e.g. +9477...
    diagnosis_category: str = "general"                     # dialogue.CATEGORIES key
    discharge_date: str = ""                                # ISO date, free-form
    notes: str = ""
    language_pref: str = "en"                               # en|ta|si (Step 10)
    active: bool = True                                     # false = discharged/archived
    # Care team for HIGH-risk email alerts (assignment routing): the staff
    # usernames (nurse/doctor) this patient's alerts are emailed to, as a JSON
    # list, e.g. ["nurse10", "doc10"]. Empty = nobody is emailed (the call row
    # then carries an explicit no_assignment note -- WhatsApp still sends).
    assigned_staff_json: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_utcnow)

    def set_assigned_staff(self, usernames: list[str]) -> None:
        self.assigned_staff_json = json.dumps(usernames, ensure_ascii=False)

    def get_assigned_staff(self) -> list[str]:
        return json.loads(self.assigned_staff_json) if self.assigned_staff_json else []


class StaffUser(SQLModel, table=True):
    """A dashboard login (Step 9). Three roles, no granular permissions."""

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)          # the "employee ID"
    display_name: str = ""
    role: str = "nurse"                                    # nurse|doctor|admin
    hospital: str = ""
    # Where this person is alerted. Required at creation time -- without it a
    # HIGH-risk call silently skips them, and a nurse who never hears about
    # an escalation is worse than no account at all.
    email: str = ""
    password_hash: str = ""
    password_salt: str = ""
    active: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    last_login_at: datetime | None = None


class CallRecord(SQLModel, table=True):
    """One row per call (Step 6 TC1/TC2)."""

    id: int | None = Field(default=None, primary_key=True)
    provider_call_id: str = Field(default="", index=True)   # Zernio call id
    patient_id: int | None = Field(default=None, foreign_key="patient.id")
    patient_code: str = Field(default="", index=True)       # denormalised
    phone_number: str = ""                                  # dialed number
    diagnosis_category: str = "general"

    started_at: datetime = Field(default_factory=_utcnow)   # call-flow start
    finished_at: datetime | None = None
    duration_sec: float = 0.0                               # wall clock of dialogue
    ended_reason: str = ""                                  # finished|hangup|max_duration|error

    answers_json: str = Field(default="", sa_column=Column(Text))    # CallDialogue.summary()
    risk_level: str = "unknown"                             # low|medium|high|unknown
    risk_score: float = 0.0
    risk_reasons_json: str = Field(default="", sa_column=Column(Text))
    findings_json: str = Field(default="", sa_column=Column(Text))

    alert_status: str = "not_sent"   # not_sent|ready|sent|failed|skipped|not_configured
    alert_detail: str = ""           # why, or the provider message id
    # The alert text itself, prepared at the end of every HIGH-risk call. Kept
    # on the row so the dashboard can show/copy it even when nothing is sent
    # (ALERT_DELIVERY=ready -- see app/services/alerts.py).
    alert_message: str = Field(default="", sa_column=Column(Text))
    # WHO was alerted about this call, per-recipient delivery result, so the
    # dashboard can answer "did the right people hear?". A list of
    # {username, display_name, role, email, status}.
    alert_recipients_json: str = Field(default="", sa_column=Column(Text))

    # -- Dashboard workflow (Step 9) -------------------------------------------
    reviewed: bool = False           # nurse marked the alert/case reviewed
    nurse_note: str = Field(default="", sa_column=Column(Text))  # short case note
    closed_by: str = ""              # doctor who closed an escalated case
    closed_at: datetime | None = None

    created_at: datetime = Field(default_factory=_utcnow)

    # -- JSON helpers (avoid raw string juggling at call sites) --------------

    def set_answers(self, answers: list[dict]) -> None:
        self.answers_json = json.dumps(answers, ensure_ascii=False, default=_json_default)

    def get_answers(self) -> list[dict]:
        return json.loads(self.answers_json) if self.answers_json else []

    def set_risk_reasons(self, reasons: list[str]) -> None:
        self.risk_reasons_json = json.dumps(reasons, ensure_ascii=False)

    def get_risk_reasons(self) -> list[str]:
        return json.loads(self.risk_reasons_json) if self.risk_reasons_json else []

    def set_findings(self, findings: list[dict]) -> None:
        self.findings_json = json.dumps(findings, ensure_ascii=False, default=_json_default)

    def get_findings(self) -> list[dict]:
        return json.loads(self.findings_json) if self.findings_json else []

    def set_alert_recipients(self, recipients: list[dict]) -> None:
        """Record who this call alerted, and each person's delivery status.

        Stored on the row rather than only in the logs, because a missed
        escalation is the failure mode that actually hurts a patient: the
        dashboard has to be able to answer "did the nurse hear?".
        A list of {username, display_name, role, email, status}.
        """
        self.alert_recipients_json = json.dumps(recipients, ensure_ascii=False)

    def get_alert_recipients(self) -> list[dict]:
        return (
            json.loads(self.alert_recipients_json)
            if self.alert_recipients_json
            else []
        )


class AppSetting(SQLModel, table=True):
    """Tiny key/value store for dashboard-controlled runtime flags.

    Lets the Schedule screen turn the Step 8 master switch on/off without
    editing backend/.env -- .env stays the default for a fresh database, and a
    saved row wins after an admin has toggled it (so the choice survives a
    backend restart).
    """

    key: str = Field(primary_key=True)
    value: str = ""

