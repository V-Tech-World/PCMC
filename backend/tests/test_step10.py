"""
Step 10 tests: staff-targeted email alerts for HIGH-risk calls (5 Oct 2026).

A HIGH-risk call is emailed to the patient's ASSIGNED care team -- the
nurse(s) and doctor(s) picked when the patient is registered (Patients
screen), in addition to the existing WhatsApp alert, which is unchanged:

    patient has an assigned care team -> those nurses AND doctors (always)
    patient has nobody assigned       -> NO email; an explicit
                                         `no_assignment` note lands on the
                                         call row

There is no score threshold: who to wake up is a care-team decision made at
registration, not a cut-off. Admins are never emailed. An assigned account
that has since been deleted or deactivated is recorded per person as
`no_route` instead of being dropped silently.

    TC1  routing  -- only the assigned team is emailed (nurses AND doctors,
                      always); nobody assigned -> a visible note, never
                      silence; unknown/deactivated/admin assignments are
                      reported per person, not dropped
    TC1b assign   -- the Patients API stores/validates the care team on
                      create AND edit (unknown role, unknown user, inactive
                      account all rejected; clearing works)
    TC2  content  -- the message names the patient, the risk and the symptoms
    TC3  delivery -- one mail per recipient, and one bad address neither stops
                     the others nor loses the alert itself
    TC4  record   -- who was alerted (and why not) lands on the call row and
                     reaches the Calls / Dashboard payloads
    TC5  accounts -- an account with no mailbox is refused by the API, and
                     existing ones are purged at boot (super-admin kept)

Nothing here opens a socket: `send_email` is monkeypatched in every test that
reaches it, and conftest pins EMAIL_ALERTS_ENABLED=false with empty
SENDER_EMAIL / GOOGLE_APP_PASSWORD.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.core import rate_limiter
from app.core.config import Settings, get_settings
from app.db import service as db_service
from app.db.engine import _engine_for, init_db, new_session
from app.db.models import StaffUser
from app.services import email_alerts
from app.services.nlp import assess_conversation

API_KEY_HEADERS = {"X-Api-Key": "test-calls-key"}


@pytest.fixture(autouse=True)
def fresh_state():
    """Isolated in-memory DB + clean counters per test (as in Step 9)."""
    _engine_for.cache_clear()
    get_settings.cache_clear()
    init_db()
    rate_limiter.reset()
    yield
    _engine_for.cache_clear()
    get_settings.cache_clear()
    rate_limiter.reset()


@pytest.fixture()
def sent(monkeypatch):
    """Capture every outbound email instead of opening an SMTP connection."""
    messages: list[dict] = []

    def _fake_send(to_email, subject, text_body, html_body, settings=None):
        messages.append(
            {"to": to_email, "subject": subject,
             "text": text_body, "html": html_body}
        )

    monkeypatch.setattr(email_alerts, "send_email", _fake_send)
    return messages


@pytest.fixture()
def configured_mailbox(monkeypatch):
    """Let the *app's own* settings see a usable mailbox.

    Endpoint tests go through get_settings(), not a Settings object built here,
    so the credentials have to exist in the environment for them too.
    """
    monkeypatch.setenv("EMAIL_ALERTS_ENABLED", "true")
    monkeypatch.setenv("SENDER_EMAIL", "alerts@voicecare.test")
    monkeypatch.setenv("GOOGLE_APP_PASSWORD", "app-password-not-real")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(**overrides) -> Settings:
    """Settings with a usable mailbox unless a test overrides it."""
    defaults = dict(
        email_alerts_enabled=True,
        sender_email="alerts@voicecare.test",
        google_app_password="app-password-not-real",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _staff(role: str, username: str, email: str = ""):
    return db_service.create_staff(
        username=username, password=f"{username}-pass-10", role=role,
        display_name=username, email=email,
    )


def _patient(code: str = "P-7070", team: list[str] | None = None):
    """A persisted patient with (by default) a nurse + doctor care team.

    Pass team=[] for "nobody assigned", or team=None to use the default
    two-person team. Returns the fresh row.
    """
    members = ["nurse10", "doc10"] if team is None else list(team)
    _action, patient = db_service.upsert_patient(
        patient_code=code,
        name=f"Patient {code}",
        phone_number="+94777007070",
        diagnosis_category="cardiac",
        assigned_staff=members,
    )
    return patient


def _deactivate(username: str) -> None:
    with new_session() as session:
        user = session.exec(
            select(StaffUser).where(StaffUser.username == username)
        ).first()
        assert user is not None
        user.active = False
        session.add(user)
        session.commit()


def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


class _FakeCall:
    """Duck-typed CallRecord: only what the alert builder reads."""

    id = 77
    patient_code = "P-7070"
    phone_number = "+94777007070"
    diagnosis_category = "cardiac"
    provider_call_id = "ca_10"
    ended_reason = "dialogue finished"
    duration_sec = 95.0
    started_at = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
    created_at = started_at

    _answers = [
        {"question_id": "medication", "kind": "yes_no",
         "interpretation": True, "transcript": "no I forgot my morning dose"},
        {"question_id": "category_cardiac", "kind": "yes_no",
         "interpretation": True, "transcript": "yes chest pain"},
    ]
    _findings = [
        {"id": "chest_pain", "label": "chest pain", "severity": "severe",
         "red_flag": True, "points": 5, "matched_text": "chest pain",
         "source": "text"},
    ]
    _reasons = ["chest pain (severe)"]

    def __init__(self, score: float = 7.5, level: str = "high"):
        self.risk_score = score
        self.risk_level = level

    def get_answers(self):
        return self._answers

    def get_findings(self):
        return self._findings

    def get_risk_reasons(self):
        return self._reasons


class _FakeDialogue:
    """Just enough of CallDialogue for record_call()."""

    diagnosis_category = "cardiac"

    def __init__(self, answers):
        self._answers = answers

    def summary(self):
        return self._answers


def _seed_high_call_row() -> int:
    """A real persisted HIGH-risk row, for the storage + API tests.

    The row is linked to a persisted patient (same phone number) carrying
    the default nurse + doctor team, exactly like a real call: the alert
    path looks the team up off the patient row, never off thin air.
    """
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    _staff("doctor", "doc10", "doc10@hospital.test")
    _patient(code="P-7070")
    answers = [
        {"question_id": "medication", "kind": "yes_no",
         "interpretation": True, "transcript": "no I forgot"},
        {"question_id": "category_cardiac", "kind": "yes_no",
         "interpretation": True, "transcript": "yes chest pain"},
    ]
    record = db_service.record_call(
        dialogue=_FakeDialogue(answers),
        assessment=assess_conversation(answers),
        provider_call_id="ca_10",
        to_number="+94777007070",
        ended_reason="dialogue finished",
        duration_sec=95.0,
    )
    assert record is not None and record.id is not None
    assert record.risk_level == "high"
    return record.id


# --------------------------------------------------------------- TC1: routing


def test_assigned_team_is_emailed_nurses_and_doctors_always(sent):
    """TC1: the assigned nurse(s) AND doctor(s) all hear, every time."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    _staff("nurse", "nurse11", "nurse11@hospital.test")
    _staff("doctor", "doc10", "doc10@hospital.test")
    _staff("admin", "adm10", "adm10@hospital.test")  # never emailed
    patient = _patient(team=["nurse10", "nurse11", "doc10"])

    outcome = email_alerts.deliver_alert_emails(_FakeCall(), patient, _settings())

    # Doctors first, then nurses, then by employee ID. Admins never routed.
    assert [r["username"] for r in outcome] == ["doc10", "nurse10", "nurse11"]
    assert [r["role"] for r in outcome] == ["doctor", "nurse", "nurse"]
    assert [m["to"] for m in sent] == [
        "doc10@hospital.test",
        "nurse10@hospital.test",
        "nurse11@hospital.test",
    ]


def test_only_the_assigned_team_hears_not_everyone_on_staff(sent):
    """TC1: unassigned staff are NOT emailed -- the team owns this patient."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    _staff("nurse", "bystander", "bystander@hospital.test")
    _staff("doctor", "doc10", "doc10@hospital.test")
    _staff("doctor", "stranger", "stranger@hospital.test")
    patient = _patient(team=["nurse10"])

    outcome = email_alerts.deliver_alert_emails(_FakeCall(), patient, _settings())

    assert [r["username"] for r in outcome] == ["nurse10"]
    assert [m["to"] for m in sent] == ["nurse10@hospital.test"]


def test_deactivated_assignee_is_reported_not_dropped(sent):
    """TC1: an assigned nurse who has left is named on the row, not skipped."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    _staff("nurse", "nurse11", "nurse11@hospital.test")
    _deactivate("nurse11")
    patient = _patient(team=["nurse10", "nurse11"])

    outcome = email_alerts.deliver_alert_emails(_FakeCall(), patient, _settings())

    assert [r["username"] for r in outcome] == ["nurse10", "nurse11"]
    assert outcome[0]["status"] == email_alerts.STATUS_SENT
    assert outcome[1]["status"].startswith("no_route:")
    assert [m["to"] for m in sent] == ["nurse10@hospital.test"]


def test_deleted_assignee_is_reported_not_dropped(sent):
    """TC1: an assigned doctor deleted after registration still shows up."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    patient = _patient(team=["nurse10", "ghostdoc"])

    outcome = email_alerts.deliver_alert_emails(_FakeCall(), patient, _settings())

    assert [r["username"] for r in outcome] == ["nurse10", "ghostdoc"]
    assert outcome[0]["status"] == email_alerts.STATUS_SENT
    assert outcome[1]["status"].startswith("no_route:")
    assert [m["to"] for m in sent] == ["nurse10@hospital.test"]


def test_patient_with_no_team_records_a_visible_note(sent):
    """TC1: nobody assigned -> a no_assignment note ON THE ROW, never silent.

    [] here is exactly the bug being fixed: the dashboard shows nothing while
    WhatsApp shows "sent", so nobody knows the escalation died.
    """
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    patient = _patient(team=[])

    outcome = email_alerts.deliver_alert_emails(_FakeCall(), patient, _settings())

    assert len(outcome) == 1
    assert outcome[0]["status"].startswith("no_assignment")
    assert sent == []


def test_patient_without_a_row_records_a_visible_note(sent):
    """TC1: an unknown patient (no row to carry a team) is equally explicit."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")

    outcome = email_alerts.deliver_alert_emails(_FakeCall(), None, _settings())

    assert len(outcome) == 1
    assert outcome[0]["status"].startswith("no_assignment")
    assert sent == []


def test_non_high_risk_is_never_emailed(sent):
    """Step 7 TC2 still holds for the new channel: low/medium stay silent."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    patient = _patient()
    for level in ("low", "medium", "unknown"):
        assert email_alerts.deliver_alert_emails(
            _FakeCall(level=level), patient, _settings()
        ) == []
    assert sent == []


# --------------------------------------- TC1b: the assignment API itself


def _admin_client():
    """A TestClient logged in as an admin (owns patient management).

    Creates the account directly (the suite runs on a fresh in-memory DB
    per test, so no seeded admin exists here) and logs in through the
    real /auth/login endpoint.
    """
    _staff("admin", "admin10", "admin10@hospital.test")
    client = _client()
    resp = client.post(
        "/auth/login",
        json={"username": "admin10", "password": "admin10-pass-10"},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _patient_payload(code: str, team: list[str] | None) -> dict:
    payload: dict = {
        "patient_code": code,
        "name": f"Patient {code}",
        "phone_number": "+94777007070",
        "diagnosis_category": "cardiac",
        "discharge_date": "",
        "notes": "",
        "language_pref": "en",
        "active": True,
    }
    if team is not None:
        payload["assigned_staff"] = team
    return payload


def test_create_patient_with_care_team_stores_it():
    """TC1b: assigning nurse(s) + doctor(s) at registration persists."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    _staff("doctor", "doc10", "doc10@hospital.test")
    client = _admin_client()

    resp = client.post(
        "/records/patients",
        json=_patient_payload("P-7070", ["nurse10", "doc10"]),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["patient"]["assigned_staff"] == ["nurse10", "doc10"]
    listed = client.get("/records/patients").json()["patients"]
    assert [p for p in listed if p["patient_code"] == "P-7070"][0][
        "assigned_staff"
    ] == ["nurse10", "doc10"]


def test_edit_patient_can_change_the_care_team():
    """TC1b: the SAME endpoint updates the team (Edit button in the UI)."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    _staff("nurse", "nurse11", "nurse11@hospital.test")
    _staff("doctor", "doc10", "doc10@hospital.test")
    client = _admin_client()
    assert client.post(
        "/records/patients", json=_patient_payload("P-7070", ["nurse10"])
    ).status_code == 201

    resp = client.post(
        "/records/patients",
        json=_patient_payload("P-7070", ["nurse11", "doc10"]),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "updated"
    assert resp.json()["patient"]["assigned_staff"] == ["nurse11", "doc10"]


def test_edit_patient_without_a_team_leaves_it_untouched():
    """TC1b: omitting the field on edit must NOT wipe the team."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    client = _admin_client()
    assert client.post(
        "/records/patients", json=_patient_payload("P-7070", ["nurse10"])
    ).status_code == 201

    resp = client.post(
        "/records/patients", json=_patient_payload("P-7070", None)
    )

    assert resp.status_code == 201, resp.text
    assert "assigned_staff" not in _patient_payload("P-7070", None)
    assert resp.json()["patient"]["assigned_staff"] == ["nurse10"]


def test_edit_patient_can_clear_the_care_team():
    """TC1b: [] explicitly clears (patient opts out of email alerts)."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    client = _admin_client()
    assert client.post(
        "/records/patients", json=_patient_payload("P-7070", ["nurse10"])
    ).status_code == 201

    resp = client.post("/records/patients", json=_patient_payload("P-7070", []))

    assert resp.status_code == 201, resp.text
    assert resp.json()["patient"]["assigned_staff"] == []


def test_assign_unknown_or_wrong_role_or_inactive_is_rejected():
    """TC1b: typos fail at SAVE time (422), not at the next HIGH-risk call."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    _staff("admin", "adm10", "adm10@hospital.test")
    _staff("nurse", "gone10", "gone10@hospital.test")
    _deactivate("gone10")
    client = _admin_client()

    for bad_team in (["ghost"], ["adm10"], ["gone10"], ["nurse10", "ghost"]):
        resp = client.post(
            "/records/patients",
            json=_patient_payload("P-7070", bad_team),
        )
        assert resp.status_code == 422, f"{bad_team} was accepted"
        assert "assigned_staff" in resp.json()["detail"].lower()


# -------------------------------------------------- TC2: what the email says


def test_email_carries_patient_risk_and_symptoms(sent):
    """TC2: patient code, risk level, symptoms and the answers are all there."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    patient = _patient()

    email_alerts.deliver_alert_emails(_FakeCall(), patient, _settings())

    assert len(sent) == 1
    mail = sent[0]
    assert mail["to"] == "nurse10@hospital.test"
    assert "P-7070" in mail["subject"]
    assert "High-risk" in mail["subject"]
    assert "7.5" in mail["subject"]

    for body in (mail["text"], mail["html"]):
        assert "P-7070" in body            # patient code
        assert "HIGH" in body              # risk level
        assert "7.5" in body               # risk score
        assert "chest pain" in body        # key symptom
        assert "severe" in body
        assert "+94777007070" in body      # phone, so the nurse can call back
        assert "no I forgot my morning dose" in body   # the transcript

    # The HTML is a real styled document, and it is addressed to the recipient.
    assert mail["html"].startswith("<!doctype html>")
    assert "Hello nurse10" in mail["html"]
    # The plain-text part is a working fallback, not a stub.
    assert mail["text"].startswith("URGENT post-discharge check-in alert")


def test_email_html_escapes_patient_supplied_text():
    """A transcript is free text from a patient -- escape it into the HTML."""
    record = _FakeCall()
    record._answers = [
        {"question_id": "note", "kind": "free_text",
         "interpretation": None, "transcript": "<b>chest</b> & pain"},
    ]
    _text, html = email_alerts.format_email_body(record)
    assert "<b>chest</b>" not in html
    assert "&lt;b&gt;chest&lt;/b&gt;" in html
    assert "&amp;" in html


# ------------------------------------------------------- TC3: delivery rules


def test_every_recipient_gets_their_own_message(sent):
    """One mail per person -- no CC/BCC, so a bad address cannot hide others."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    _staff("nurse", "nurse11", "nurse11@hospital.test")
    _staff("doctor", "doc10", "doc10@hospital.test")
    patient = _patient(team=["nurse10", "nurse11", "doc10"])

    outcome = email_alerts.deliver_alert_emails(_FakeCall(), patient, _settings())

    assert sorted(m["to"] for m in sent) == sorted(
        ["nurse10@hospital.test", "nurse11@hospital.test", "doc10@hospital.test"]
    )
    assert all(r["status"] == email_alerts.STATUS_SENT for r in outcome)


def test_one_bad_address_does_not_stop_the_rest(monkeypatch):
    """TC3: a failed send is isolated and reported per recipient."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    _staff("nurse", "nurse11", "nurse11@hospital.test")
    patient = _patient(team=["nurse10", "nurse11"])
    attempted: list[str] = []

    def _flaky(to_email, subject, text_body, html_body, settings=None):
        attempted.append(to_email)
        if to_email == "nurse10@hospital.test":
            raise email_alerts.EmailAlertError("smtp is having a bad day")
        return None

    monkeypatch.setattr(email_alerts, "send_email", _flaky)
    outcome = email_alerts.deliver_alert_emails(_FakeCall(), patient, _settings())

    # The second recipient was still attempted after the first one failed.
    assert attempted == ["nurse10@hospital.test", "nurse11@hospital.test"]
    assert outcome[0]["username"] == "nurse10"
    assert outcome[0]["status"].startswith("failed: ")
    assert "bad day" in outcome[0]["status"]
    assert outcome[1]["status"] == email_alerts.STATUS_SENT


def test_staff_without_a_mailbox_is_reported_not_dropped(sent):
    """An account we cannot reach is named on the row, not silently skipped."""
    _staff("nurse", "nurse10")                      # no email
    _staff("nurse", "nurse11", "nurse11@hospital.test")
    patient = _patient(team=["nurse10", "nurse11"])

    outcome = email_alerts.deliver_alert_emails(_FakeCall(), patient, _settings())

    assert [r["username"] for r in outcome] == ["nurse10", "nurse11"]
    assert outcome[0]["status"] == email_alerts.STATUS_NO_EMAIL
    assert outcome[1]["status"] == email_alerts.STATUS_SENT
    assert [m["to"] for m in sent] == ["nurse11@hospital.test"]


def test_unconfigured_mailbox_records_recipients_and_sends_nothing(sent):
    """No credentials -> nobody is emailed, but the row still says who should
    have been. This is the mode where an alert goes missing quietly, so it is
    recorded loudly."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    patient = _patient(team=["nurse10"])

    outcome = email_alerts.deliver_alert_emails(
        _FakeCall(), patient,
        _settings(sender_email="", google_app_password=""),
    )

    assert sent == []
    assert [r["status"] for r in outcome] == [email_alerts.STATUS_NOT_CONFIGURED]
    assert outcome[0]["email"] == "nurse10@hospital.test"


def test_disabled_switch_records_recipients_and_sends_nothing(sent):
    """EMAIL_ALERTS_ENABLED=false turns the channel off, not the bookkeeping."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    patient = _patient(team=["nurse10"])

    outcome = email_alerts.deliver_alert_emails(
        _FakeCall(), patient, _settings(email_alerts_enabled=False)
    )

    assert sent == []
    assert [r["status"] for r in outcome] == [email_alerts.STATUS_DISABLED]


@pytest.mark.parametrize(
    "value,ok",
    [
        ("nurse@hospital.lk", True),
        ("nurse+alerts@ward1.hospital.lk", True),
        (" Nurse@Hospital.test ", True),
        ("nurse", False),
        ("nurse@", False),
        ("@hospital.test", False),
        ("nurse@hospital", False),
        ("nurse@hospital. test", False),
        ("", False),
        (None, False),
    ],
)
def test_email_shape_check(value, ok):
    """Permissive on purpose: it only rejects what can never be delivered."""
    assert email_alerts.looks_like_email(value) is ok


# ------------------------------------------- TC4: recorded on the call row


def test_recipients_are_stored_on_the_call_row(sent):
    """TC4: the row answers "did the right people hear?"."""
    call_id = _seed_high_call_row()
    record = db_service.get_call(call_id)
    # The seeded row dialed the patient's number, so the lookup links it.
    patient = db_service.find_patient(phone_number=record.phone_number)

    recipients = email_alerts.deliver_alert_emails(record, patient, _settings())
    db_service.attach_alert(
        call_id, "ready", detail="prepared", message="alert text",
        recipients=recipients,
    )

    stored = db_service.get_call(call_id)
    rows = stored.get_alert_recipients()
    assert [r["username"] for r in rows] == ["doc10", "nurse10"]
    assert [r["role"] for r in rows] == ["doctor", "nurse"]
    assert all(r["status"] == email_alerts.STATUS_SENT for r in rows)
    assert all(r["email"] for r in rows)
    # Storing recipients must not disturb the alert text it sits next to.
    assert stored.alert_message == "alert text"
    assert stored.alert_status == "ready"


def test_recipient_json_survives_a_round_trip():
    """A row with no recipients reads back as an empty list, not a crash."""
    call_id = _seed_high_call_row()
    assert db_service.get_call(call_id).get_alert_recipients() == []
    db_service.attach_alert(call_id, "ready", detail="prepared")
    assert db_service.get_call(call_id).get_alert_recipients() == []


def test_call_detail_endpoint_exposes_recipients(sent, configured_mailbox):
    """The Calls screen reads `alert_recipients` off the payload."""
    call_id = _seed_high_call_row()

    client = _client()
    re_sent = client.post(f"/records/calls/{call_id}/alert", headers=API_KEY_HEADERS)
    assert re_sent.status_code == 200, re_sent.text
    payload = re_sent.json()["call"]

    assert [r["username"] for r in payload["alert_recipients"]] == [
        "doc10",
        "nurse10",
    ]
    assert all(
        r["status"] == email_alerts.STATUS_SENT
        for r in payload["alert_recipients"]
    )
    assert {
        r["username"]: r["email"] for r in payload["alert_recipients"]
    } == {
        "doc10": "doc10@hospital.test",
        "nurse10": "nurse10@hospital.test",
    }

    # ...and the detail + list endpoints carry it too.
    detail = client.get(f"/records/calls/{call_id}", headers=API_KEY_HEADERS).json()
    assert [r["username"] for r in detail["alert_recipients"]] == ["doc10", "nurse10"]
    listed = client.get("/records/calls?limit=5", headers=API_KEY_HEADERS).json()
    assert [r["username"] for r in listed["calls"][0]["alert_recipients"]] == [
        "doc10",
        "nurse10",
    ]


def test_dashboard_summary_reports_the_email_channel():
    """The Dashboard can show whether the second channel is on, and from where."""
    summary = _client().get("/dashboard/summary", headers=API_KEY_HEADERS)
    assert summary.status_code == 200
    alerts = summary.json()["alerts"]
    assert "email_enabled" in alerts
    assert "email_configured" in alerts
    assert "doctor_score_threshold" not in alerts  # assignment routing has no cut-off
    # conftest pins the credentials empty, so the channel is not configured.
    assert alerts["email_configured"] is False


# ------------------------------------------------------- TC5: staff accounts


def test_staff_account_is_refused_without_a_usable_email():
    """TC5: an account that can never be alerted is refused up front."""
    client = _client()
    created = client.post(
        "/auth/staff",
        json={"username": "nur1001", "password": "secret-1001", "role": "nurse",
              "email": "nur1001@hospital.test"},
        headers=API_KEY_HEADERS,
    )
    assert created.status_code == 201, created.text
    assert created.json()["staff"]["email"] == "nur1001@hospital.test"

    for index, bad in enumerate(
        ("", "nurse", "nurse@", "@hospital.test", "nurse@hospital")
    ):
        resp = client.post(
            "/auth/staff",
            json={"username": f"bad10{index}", "password": "secret-1001",
                  "role": "nurse", "email": bad},
            headers=API_KEY_HEADERS,
        )
        assert resp.status_code == 422, f"{bad!r} was accepted"
        assert "email" in resp.json()["detail"].lower()


def test_boot_purge_drops_unreachable_staff_but_keeps_the_super_admin():
    """TC5: a nurse/doctor with no mailbox is removed at boot (idempotent);
    the super-admin is exempt because it is never assigned to a patient."""
    _staff("admin", "admin10")                                  # no email: kept
    _staff("nurse", "legacy10")                                 # no email: removed
    _staff("doctor", "legacy11")                                # no email: removed
    _staff("nurse", "nurse10", "nurse10@hospital.test")         # reachable: kept

    removed = db_service.purge_staff_without_email()

    assert sorted(removed) == ["legacy10", "legacy11"]
    assert db_service.purge_staff_without_email() == []         # idempotent
    assert {u.username for u in db_service.list_staff()} == {"admin10", "nurse10"}


def test_purge_never_touches_a_reachable_account():
    """The purge and the routing agree on what "reachable" means."""
    _staff("nurse", "nurse10", "nurse10@hospital.test")
    assert db_service.purge_staff_without_email() == []
    assert [u.username for u in db_service.list_staff()] == ["nurse10"]



