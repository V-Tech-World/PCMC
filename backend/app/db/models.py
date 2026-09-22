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
    return datetime.now(timezone.utc)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serialisable: {type(value)!r}")


class Patient(SQLModel, table=True):
    """A registered post-discharge patient (seeded by the hospital)."""

    id: int | None = Field(default=None, primary_key=True)
    patient_code: str = Field(unique=True, index=True)     # e.g. "P-0001"
    name: str = ""
    phone_number: str = ""                                  # E.164, e.g. +9477...
    diagnosis_category: str = "general"                     # general|surgical|cardiac
    discharge_date: str = ""                                # ISO date, free-form
    notes: str = ""
    language_pref: str = "en"                               # en|ta|si (Step 10)
    active: bool = True                                     # false = discharged/archived
    created_at: datetime = Field(default_factory=_utcnow)


class StaffUser(SQLModel, table=True):
    """A dashboard login (Step 9). Three roles, no granular permissions."""

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)          # the "employee ID"
    display_name: str = ""
    role: str = "nurse"                                    # nurse|doctor|admin
    hospital: str = ""
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

    alert_status: str = "not_sent"   # not_sent|sent|failed|skipped|not_applicable
    alert_detail: str = ""           # provider message id or the failure reason

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
