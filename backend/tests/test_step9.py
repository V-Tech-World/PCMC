"""
Step 9 tests: staff auth (JWT, 3 roles) + the dashboard API behind it.

TC1  login works for nurse/doctor/admin and each gets the allowed actions
     from the README role matrix (verified against real endpoint gating)
TC2  the dashboard payload carries the risk-coded call list the UI renders
TC3  backend side of the manual "Call Now" button: staff token required,
     patient number dialed (provider call mocked -- nothing is placed here)

Also covers: generic 401s, inactive accounts, admin seeding, the 503 when
auth is unconfigured, and X-Api-Key still working for machine callers.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.core import rate_limiter
from app.core.config import get_settings
from app.db import service as db_service
from app.db.engine import _engine_for, init_db
from app.db.models import StaffUser
from app.services.nlp import assess_conversation


@pytest.fixture(autouse=True)
def fresh_state():
    """Isolated in-memory DB + clean rate-limit counters per test."""
    _engine_for.cache_clear()
    init_db()
    rate_limiter.reset()
    yield
    _engine_for.cache_clear()
    rate_limiter.reset()


@pytest.fixture()
def staff():
    """One account per role (the three logins TC1 is about)."""
    return {
        role: db_service.create_staff(
            username=user, password=f"{role}-pass-9", role=role, display_name=user
        )
        for role, user in (
            ("nurse", "nur901"),
            ("doctor", "doc901"),
            ("admin", "adm901"),
        )
    }


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _login(client, username: str, password: str) -> tuple[dict, dict]:
    resp = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body


# ----------------------------------------------------------------- TC1

def test_login_works_for_each_role(staff):
    """TC1: nurse, doctor and admin each log in and get their own identity."""
    client = _client()
    for role, user in (("nurse", "nur901"), ("doctor", "doc901"), ("admin", "adm901")):
        headers, body = _login(client, user, f"{role}-pass-9")
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["user"]["role"] == role

        me = client.get("/auth/me", headers=headers).json()
        assert me["via"] == "staff"
        assert me["role"] == role
        assert me["username"] == user


def test_role_matrix_matches_the_readme(staff):
    """The permission table the frontend uses to hide/show actions."""
    perms = _client().get("/auth/roles").json()["permissions"]
    assert perms["view_dashboard"] == ["nurse", "doctor", "admin"]
    assert perms["call_patient"] == ["nurse", "doctor", "admin"]
    assert perms["review_call"] == ["nurse", "doctor", "admin"]
    assert perms["close_case"] == ["doctor", "admin"]      # nurse excluded
    assert perms["manage_patients"] == ["admin"]
    assert perms["manage_staff"] == ["admin"]
    assert perms["manage_scheduler"] == ["admin"]


# ----------------------------------------------------------------- rejections

def test_bad_credentials_and_inactive_accounts_are_rejected(staff):
    client = _client()
    wrong = client.post(
        "/auth/login", json={"username": "nur901", "password": "wrong-pass"}
    )
    unknown = client.post(
        "/auth/login", json={"username": "ghost901", "password": "whatever"}
    )
    assert wrong.status_code == unknown.status_code == 401
    # one generic message: the form must not reveal which field was wrong
    assert wrong.json()["detail"] == unknown.json()["detail"]

    with db_service.new_session() as session:
        row = session.exec(
            select(StaffUser).where(StaffUser.username == "nur901")
        ).first()
        row.active = False
        session.add(row)
        session.commit()
    disabled = client.post(
        "/auth/login", json={"username": "nur901", "password": "nurse-pass-9"}
    )
    assert disabled.status_code == 401


def test_login_is_503_when_auth_is_unconfigured(monkeypatch):
    """No secret anywhere -> refuse to mint tokens (never 'open')."""
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("CALLS_API_KEY", "")
    get_settings.cache_clear()
    resp = _client().post(
        "/auth/login", json={"username": "anyone", "password": "anything"}
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


def test_api_key_still_works_for_machine_callers(staff):
    """Scripts + the live-test plan keep using X-Api-Key (ops, not people)."""
    client = _client()
    key = {"X-Api-Key": "test-calls-key"}
    me = client.get("/auth/me", headers=key).json()
    assert me["via"] == "api_key"
    assert me["role"] == "service"
    # API-key callers are unrestricted -- they pass every role check.
    assert client.get("/auth/staff", headers=key).status_code == 200


# ----------------------------------------------------------------- role gating

def test_only_admin_manages_staff_accounts(staff):
    client = _client()
    nurse_h, _ = _login(client, "nur901", "nurse-pass-9")
    payload = {"username": "x901", "password": "secret-901", "role": "nurse"}
    assert client.post("/auth/staff", json=payload, headers=nurse_h).status_code == 403
    assert client.get("/auth/staff", headers=nurse_h).status_code == 403

    admin_h, _ = _login(client, "adm901", "admin-pass-9")
    created = client.post(
        "/auth/staff",
        json={
            "username": "nur902", "password": "secret-902",
            "role": "nurse", "display_name": "Nurse Two",
        },
        headers=admin_h,
    )
    assert created.status_code == 201
    assert created.json()["staff"]["role"] == "nurse"
    # duplicate employee id -> 409, unknown role -> 422
    assert client.post(
        "/auth/staff", json={"username": "nur902", "password": "secret-902"},
        headers=admin_h,
    ).status_code == 409
    assert client.post(
        "/auth/staff", json={"username": "zed901", "password": "secret-903",
                             "role": "janitor"},
        headers=admin_h,
    ).status_code == 422
    assert client.get("/auth/staff", headers=admin_h).json()["count"] == 4


def test_only_admin_upserts_patients_everyone_can_read(staff):
    client = _client()
    payload = {
        "patient_code": "P-901", "name": "Kamal Perera",
        "phone_number": "+94771000901", "diagnosis_category": "cardiac",
    }
    nurse_h, _ = _login(client, "nur901", "nurse-pass-9")
    assert client.post(
        "/records/patients", json=payload, headers=nurse_h
    ).status_code == 403
    assert client.get("/records/patients", headers=nurse_h).json()["count"] == 0

    admin_h, _ = _login(client, "adm901", "admin-pass-9")
    assert client.post(
        "/records/patients", json=payload, headers=admin_h
    ).status_code == 201
    # now the nurse's read (the call list join) sees it
    assert client.get("/records/patients", headers=nurse_h).json()["count"] == 1


def test_admin_can_edit_an_existing_patient(staff):
    """The dashboard's Edit action: same endpoint, status 'updated', no dupe.

    Editing re-posts the (pre-filled) record, so the code must update the row
    it already matches -- including flipping category to one of the newer
    discharge types -- and never create a second patient.
    """
    client = _client()
    admin_h, _ = _login(client, "adm901", "admin-pass-9")
    nurse_h, _ = _login(client, "nur901", "nurse-pass-9")

    created = client.post(
        "/records/patients",
        json={
            "patient_code": "P-902", "name": "Nimal Silva",
            "phone_number": "+94771000902", "diagnosis_category": "general",
            "discharge_date": "2026-09-01", "notes": "first draft",
        },
        headers=admin_h,
    )
    assert created.status_code == 201
    assert created.json()["status"] == "created"

    edited = client.post(
        "/records/patients",
        json={
            "patient_code": "P-902", "name": "Nimal K. Silva",
            "phone_number": "+94771000903", "diagnosis_category": "respiratory",
            "discharge_date": "2026-09-02", "notes": "corrected number",
            "language_pref": "ta", "active": False,
        },
        headers=admin_h,
    )
    assert edited.status_code == 201
    body = edited.json()
    assert body["status"] == "updated"
    assert body["patient"]["name"] == "Nimal K. Silva"
    assert body["patient"]["phone_number"] == "+94771000903"
    assert body["patient"]["diagnosis_category"] == "respiratory"
    assert body["patient"]["language_pref"] == "ta"
    assert body["patient"]["active"] is False

    listed = client.get("/records/patients", headers=nurse_h).json()
    assert listed["count"] == 1                       # edited in place
    assert listed["patients"][0]["name"] == "Nimal K. Silva"

    # A nurse may read but never write records.
    assert client.post(
        "/records/patients",
        json={"patient_code": "P-903", "phone_number": "+94771000904"},
        headers=nurse_h,
    ).status_code == 403


def test_patient_category_must_be_a_known_discharge_type(staff):
    client = _client()
    admin_h, _ = _login(client, "adm901", "admin-pass-9")
    for category in ("general", "surgical", "cardiac", "respiratory", "diabetic"):
        assert client.post(
            "/records/patients",
            json={
                "patient_code": f"P-{category}", "name": category,
                "phone_number": "+94771000905", "diagnosis_category": category,
            },
            headers=admin_h,
        ).status_code == 201

    rejected = client.post(
        "/records/patients",
        json={
            "patient_code": "P-905", "phone_number": "+94771000905",
            "diagnosis_category": "dentistry",
        },
        headers=admin_h,
    )
    assert rejected.status_code == 422
    assert "diabetic" in rejected.json()["detail"]      # the valid list is listed


# ----------------------------------------------------------------- TC3

def _fake_place_call(monkeypatch) -> list[dict]:
    """Replace the provider dial with a recorder (no network, no cost)."""
    calls: list[dict] = []

    def fake(to_number, settings=None, config=None, amd=None):
        calls.append({"to": to_number, "config": config or {}})
        return SimpleNamespace(
            provider_call_id="ca_manual9",
            to_number=to_number,
            provider_status="dialing",
            forward_to_host="wss://example/media-stream",
            greeting="Hello",
        )

    monkeypatch.setattr("app.services.outbound_call.place_call", fake)
    return calls


def test_call_now_requires_staff_token_and_dials_the_patient(staff, monkeypatch):
    """TC3 (backend): the dashboard's Call Now button -- browser never carries
    the machine API key; the staff token is enough, and the patient's
    registered number is the one dialed (dial itself mocked)."""
    client = _client()
    db_service.upsert_patient(
        patient_code="P-901", name="Kamal Perera",
        phone_number="+94771000901", diagnosis_category="cardiac",
    )
    calls = _fake_place_call(monkeypatch)

    # no credentials -> 401
    assert client.post(
        "/calls", json={"patient_code": "P-901", "diagnosis_category": "cardiac"}
    ).status_code == 401

    nurse_h, _ = _login(client, "nur901", "nurse-pass-9")
    resp = client.post(
        "/calls",
        json={"patient_code": "P-901", "diagnosis_category": "cardiac"},
        headers=nurse_h,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "dialing"
    assert calls[0]["to"] == "+94771000901"
    assert calls[0]["config"]["patient_code"] == "P-901"

    # unknown patient -> 404 and NO dial is attempted
    before = len(calls)
    assert client.post(
        "/calls", json={"patient_code": "P-404", "diagnosis_category": "cardiac"},
        headers=nurse_h,
    ).status_code == 404
    assert len(calls) == before


# ------------------------------------------------------- review / close (roles)

class _FakeDialogue:
    """Just enough of CallDialogue for record_call()."""

    diagnosis_category = "cardiac"

    def __init__(self, answers):
        self._answers = answers

    def summary(self):
        return self._answers


def _seed_high_call() -> int:
    answers = [
        {"question_id": "medication", "kind": "yes_no",
         "interpretation": True, "transcript": "no I forgot"},
        {"question_id": "category_cardiac", "kind": "yes_no",
         "interpretation": True, "transcript": "yes chest pain"},
    ]
    record = db_service.record_call(
        dialogue=_FakeDialogue(answers),
        assessment=assess_conversation(answers),
        provider_call_id="ca_rev9",
        to_number="+94771000901",
        ended_reason="dialogue finished",
        # Backdate: a call made TODAY would satisfy (complete) the due slot and
        # the dashboard's calls_due card would legitimately drop to 0.
        started_at=datetime.now() - timedelta(days=4),
        duration_sec=60.0,
    )
    assert record is not None and record.id is not None
    return record.id


def test_review_and_close_follow_the_role_table(staff):
    client = _client()
    call_id = _seed_high_call()

    assert client.patch(
        f"/records/calls/{call_id}", json={"reviewed": True}
    ).status_code == 401

    nurse_h, _ = _login(client, "nur901", "nurse-pass-9")
    ok = client.patch(
        f"/records/calls/{call_id}",
        json={"reviewed": True, "nurse_note": "Spoke to the family"},
        headers=nurse_h,
    )
    assert ok.status_code == 200
    assert ok.json()["call"]["reviewed"] is True
    assert ok.json()["call"]["nurse_note"] == "Spoke to the family"

    # nurse may review but NOT close an escalated case
    denied = client.patch(
        f"/records/calls/{call_id}", json={"close_case": True}, headers=nurse_h
    )
    assert denied.status_code == 403

    # doctor may close -- and the closure is attributed to them
    doc_h, _ = _login(client, "doc901", "doctor-pass-9")
    closed = client.patch(
        f"/records/calls/{call_id}", json={"close_case": True}, headers=doc_h
    )
    assert closed.status_code == 200
    body = closed.json()["call"]
    assert body["closed_by"] == "doc901"
    assert body["reviewed"] is True
    assert body["closed_at"] is not None

    assert client.patch(
        "/records/calls/99999", json={"reviewed": True}, headers=doc_h
    ).status_code == 404


# ---------------------------------------------------------- dashboard payload

def test_dashboard_summary_feeds_the_cards_and_call_list(staff):
    """TC2 data: the payload behind the 6 cards + the risk-coded call list."""
    client = _client()
    db_service.upsert_patient(
        patient_code="P-901", name="Kamal Perera",
        phone_number="+94771000901", diagnosis_category="cardiac",
        discharge_date=(date.today() - timedelta(days=4)).isoformat(),
    )
    _seed_high_call()

    assert client.get("/dashboard/summary").status_code == 401  # auth required

    nurse_h, _ = _login(client, "nur901", "nurse-pass-9")
    data = client.get("/dashboard/summary", headers=nurse_h).json()

    cards = data["cards"]
    assert cards["calls_total"] == 1
    assert cards["calls_by_risk"]["high"] == 1
    assert cards["patients_total"] == 1
    assert cards["alerts_open"] == 1            # high + not yet reviewed
    assert cards["calls_due"] >= 1              # discharged 4d ago -> due now

    recent = data["recent_calls"]
    assert recent and recent[0]["risk_level"] == "high"
    assert recent[0]["patient_code"] == "P-901"
    assert recent[0]["alert_status"] in ("not_sent", "skipped", "sent", "failed")

    assert data["scheduler"]["enabled"] is False
    assert data["due_patients"][0]["patient_code"] == "P-901"
    assert "max_call_duration_sec" in data["cost_rails"]
    # Step 7 rework: the shipped mode prepares the alert in-dashboard.
    assert data["alerts"]["delivery"] == "ready"
    assert data["alerts"]["channel"] == "prepared in-dashboard (manual delivery)"


def test_activity_is_the_compact_risk_list(staff):
    client = _client()
    _seed_high_call()
    nurse_h, _ = _login(client, "nur901", "nurse-pass-9")

    data = client.get("/dashboard/activity?limit=5", headers=nurse_h).json()
    assert data["count"] == 1
    row = data["calls"][0]
    assert row["risk_level"] == "high"
    assert row["risk_score"] >= 5.0
    assert row["risk_reasons"]                 # the reasons render in the UI
    # transcript bodies stay out of the list (detail view's job)
    assert "answers" not in row

    assert client.get("/dashboard/activity").status_code == 401


# ----------------------------------------------------------------- admin seed

def test_default_admin_is_seeded_and_can_log_in():
    """How the demo gets its first login (no signup flow)."""
    result = db_service.ensure_default_admin(
        username="root9", password="root-pass-9",
        display_name="The Admin", hospital="Test Hospital",
    )
    assert result == "created"
    # re-running never overwrites the existing account
    assert db_service.ensure_default_admin(
        username="root9", password="other-pass-9"
    ) == "exists"
    # no credentials configured -> skipped, no account created
    assert db_service.ensure_default_admin(username="", password="") == "skipped"

    client = _client()
    headers, body = _login(client, "root9", "root-pass-9")
    assert body["user"]["role"] == "admin"
    assert client.get("/auth/staff", headers=headers).status_code == 200


# ------------------------------------------------------------- password reset

def test_forgot_password_self_service_reset(staff):
    """No admin, no third party: employee ID + new password + confirm."""
    client = _client()

    # unknown id -> generic 404; mismatch -> 422; too short -> 422 (min 6)
    assert client.post("/auth/reset-password", json={
        "username": "ghost901", "new_password": "brand-new-pass",
        "confirm_password": "brand-new-pass",
    }).status_code == 404
    assert client.post("/auth/reset-password", json={
        "username": "nur901", "new_password": "brand-new-pass",
        "confirm_password": "different-pass",
    }).status_code == 422
    assert client.post("/auth/reset-password", json={
        "username": "nur901", "new_password": "abc",
        "confirm_password": "abc",
    }).status_code == 422

    # the real flow: NO auth headers at all (the caller is logged out)
    ok = client.post("/auth/reset-password", json={
        "username": "NUR901",               # case/whitespace insensitive
        "new_password": "brand-new-pass",
        "confirm_password": "brand-new-pass",
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "password_updated"

    # the old password is dead (still a generic 401), the new one logs in
    assert client.post("/auth/login", json={
        "username": "nur901", "password": "nurse-pass-9",
    }).status_code == 401
    _, body = _login(client, "nur901", "brand-new-pass")
    assert body["user"]["role"] == "nurse"


def test_reset_password_refuses_deactivated_accounts(staff):
    client = _client()
    with db_service.new_session() as session:
        row = session.exec(
            select(StaffUser).where(StaffUser.username == "nur901")
        ).first()
        row.active = False
        session.add(row)
        session.commit()

    resp = client.post("/auth/reset-password", json={
        "username": "nur901", "new_password": "brand-new-pass",
        "confirm_password": "brand-new-pass",
    })
    assert resp.status_code == 404          # same generic answer as unknown
    # ...and they still cannot log in with the attempted password
    assert client.post("/auth/login", json={
        "username": "nur901", "password": "brand-new-pass",
    }).status_code == 401