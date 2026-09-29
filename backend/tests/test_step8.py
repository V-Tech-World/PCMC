"""
Step 8 tests: follow-up check-in scheduler (the cronjob).

The master switch SCHEDULE_CALLS_ENABLED defaults to FALSE -- that is the demo
contract: the frontend places calls manually, the cron exists but sleeps. This
suite proves BOTH sides:

- switch off  -> the dial plan is reported but no provider call is ever made
- switch on   -> a due patient is dialed (the dial itself is mocked)  [TC1]
- a patient whose slot has not arrived is never planned/dialed         [TC2]
- the next scheduled call time is computed from DISCHARGE_DATE          [TC3]

Nothing in this file dials a real call.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import rate_limiter
from app.core.config import Settings
from app.db import service as db_service
from app.db.engine import _engine_for, init_db
from app.db.models import Patient
from app.services import scheduler


@pytest.fixture(autouse=True)
def fresh_state():
    """Isolated in-memory DB + clean rate-limit counters per test."""
    _engine_for.cache_clear()
    init_db()
    rate_limiter.reset()
    yield
    scheduler.stop()
    _engine_for.cache_clear()
    rate_limiter.reset()


def _settings(**overrides) -> Settings:
    """Real Settings with only the scheduler knobs overridden (kwargs beat
    env, so the backend .env never leaks into a test)."""
    base = dict(
        schedule_calls_enabled=False,
        schedule_dry_run=False,
        schedule_checkin_days="3,7,14,30",
        schedule_hour=9,
        schedule_minute=0,
        schedule_grace_days=2,
        schedule_max_dials_per_tick=2,
        schedule_interval_minutes=30,
    )
    base.update(overrides)
    return Settings(**base)


def _patient(code="P-801", discharge="2026-06-01", phone="+94777000801", **kw):
    action, patient = db_service.upsert_patient(
        patient_code=code,
        name=kw.pop("name", "Soma Silva"),
        phone_number=phone,
        diagnosis_category=kw.pop("category", "cardiac"),
        discharge_date=discharge,
        **kw,
    )
    assert action in ("created", "updated")
    return patient


def _fake_place_call(monkeypatch) -> list[dict]:
    """Replace the provider dial with a recorder (no network, no cost)."""
    calls: list[dict] = []

    def fake(to_number, settings=None, config=None, amd=None):
        calls.append({"to": to_number, "config": config or {}})
        return SimpleNamespace(
            provider_call_id="ca_sched_test",
            to_number=to_number,
            provider_status="dialing",
            forward_to_host="wss://example.invalid/media-stream",
            greeting="",
        )

    monkeypatch.setattr("app.services.outbound_call.place_call", fake)
    return calls


# ----------------------------------------------------------------- TC3

def test_next_call_time_computed_from_discharge_date():
    """TC3: next_call_at = discharge + first unsatisfied offset at SCHEDULE_HOUR."""
    settings = _settings()
    patient = _patient(discharge="2026-06-01")
    row = scheduler.compute_schedule(
        patient, None, now=datetime(2026, 6, 3, 10, 0), settings=settings
    )
    assert row["status"] == scheduler.STATUS_SCHEDULED
    assert row["next_call_at"] == "2026-06-04T09:00"
    assert [s["date"] for s in row["slots"]] == [
        "2026-06-04", "2026-06-08", "2026-06-15", "2026-07-01",
    ]


def test_due_then_overdue_progression():
    settings = _settings()
    patient = _patient(discharge="2026-06-01")
    due = scheduler.compute_schedule(
        patient, None, now=datetime(2026, 6, 4, 9, 30), settings=settings
    )
    assert due["status"] == scheduler.STATUS_DUE
    assert due["days_overdue"] == 0
    assert due["next_call_at"] == "2026-06-04T09:00"

    overdue = scheduler.compute_schedule(
        patient, None, now=datetime(2026, 6, 5, 10, 0), settings=settings
    )
    assert overdue["status"] == scheduler.STATUS_OVERDUE
    assert overdue["days_overdue"] == 1


def test_completed_slot_is_skipped_and_next_offset_promoted():
    settings = _settings()
    patient = _patient(discharge="2026-06-01")
    last_call = datetime(2026, 6, 4, 12, 0)  # called on the slot day
    row = scheduler.compute_schedule(
        patient, last_call, now=datetime(2026, 6, 5, 10, 0), settings=settings
    )
    assert row["slots"][0]["status"] == scheduler.STATUS_COMPLETED
    assert row["next_call_at"] == "2026-06-08T09:00"
    assert row["status"] == scheduler.STATUS_SCHEDULED


# ----------------------------------------------------------------- TC2

def test_patient_not_yet_due_is_never_planned_or_dialed(monkeypatch):
    """TC2: even with the switch ON, a future slot produces zero work."""
    settings = _settings(schedule_calls_enabled=True)
    _patient(discharge="2026-06-01")
    calls = _fake_place_call(monkeypatch)

    result = scheduler.run_tick(now=datetime(2026, 6, 3, 10, 0), settings=settings)
    assert result.planned == []
    assert result.dialed == 0
    assert calls == []


def test_slot_older_than_the_grace_window_is_not_dialed(monkeypatch):
    """Catch-up window: a slot missed by more than SCHEDULE_GRACE_DAYS is
    never dialed late (a surprise call weeks later would alarm patients)."""
    settings = _settings(schedule_calls_enabled=True, schedule_grace_days=2)
    _patient(discharge="2026-06-01")   # first slot 2026-06-04 09:00
    calls = _fake_place_call(monkeypatch)

    # 3 days overdue > grace 2 -> excluded from the plan entirely.
    result = scheduler.run_tick(now=datetime(2026, 6, 7, 9, 30), settings=settings)
    assert result.planned == []
    assert calls == []

    # 2 days overdue = still inside grace -> planned and dialed.
    result2 = scheduler.run_tick(now=datetime(2026, 6, 6, 9, 30), settings=settings)
    assert len(result2.planned) == 1
    assert result2.dialed == 1
    assert len(calls) == 1


# ----------------------------------------------------------------- TC1

def test_enabled_scheduler_dials_the_due_patient(monkeypatch):
    """TC1: due patient is called automatically when the master switch is on
    (the provider dial itself is mocked -- no real call is placed)."""
    settings = _settings(schedule_calls_enabled=True)
    _patient(discharge="2026-06-01", phone="+94777000801")
    calls = _fake_place_call(monkeypatch)

    result = scheduler.run_tick(now=datetime(2026, 6, 4, 9, 30), settings=settings)
    assert result.enabled is True and result.dry_run is False
    assert result.dialed == 1
    assert calls[0]["to"] == "+94777000801"
    assert calls[0]["config"]["patient_code"] == "P-801"
    assert calls[0]["config"]["scheduled"] is True
    assert calls[0]["config"]["scheduled_slot"] == "2026-06-04"
    assert result.outcomes[0]["status"] == "dialing"


def test_master_switch_off_reports_the_plan_but_never_dials(monkeypatch):
    """The demo safety property: SCHEDULE_CALLS_ENABLED=false means the tick
    still SHOWS what it would do (dashboard column) but dials nothing."""
    settings = _settings(schedule_calls_enabled=False)
    _patient(discharge="2026-06-01")
    calls = _fake_place_call(monkeypatch)

    result = scheduler.run_tick(now=datetime(2026, 6, 4, 9, 30), settings=settings)
    assert result.enabled is False
    assert result.dry_run is True          # disabled == forced dry run
    assert len(result.planned) == 1        # plan IS reported (TC3 visibility)
    assert result.dialed == 0
    assert calls == []


def test_dry_run_reports_without_dialing(monkeypatch):
    settings = _settings(schedule_calls_enabled=True, schedule_dry_run=True)
    _patient(discharge="2026-06-01")
    calls = _fake_place_call(monkeypatch)

    result = scheduler.run_tick(now=datetime(2026, 6, 4, 9, 30), settings=settings)
    assert result.dry_run is True
    assert len(result.planned) == 1
    assert result.dialed == 0
    assert calls == []


def test_max_dials_per_tick_caps_the_tick(monkeypatch):
    """Cost rail: one tick never dials more than SCHEDULE_MAX_DIALS_PER_TICK;
    the rest is deferred to the next tick."""
    settings = _settings(
        schedule_calls_enabled=True,
        schedule_max_dials_per_tick=1,
        schedule_checkin_days="3",
    )
    _patient(code="P-811", phone="+94771000011")
    _patient(code="P-812", phone="+94771000012")
    _patient(code="P-813", phone="+94771000013")
    calls = _fake_place_call(monkeypatch)

    result = scheduler.run_tick(now=datetime(2026, 6, 4, 9, 30), settings=settings)
    assert len(result.planned) == 3
    assert result.dialed == 1
    assert len(calls) == 1
    assert result.skipped >= 2


def test_rate_limit_rail_blocks_a_scheduled_dial(monkeypatch):
    """Scheduled dials share the manual cost rails -- a budget rejection never
    reaches the provider."""
    settings = _settings(schedule_calls_enabled=True)
    _patient(discharge="2026-06-01")
    calls = _fake_place_call(monkeypatch)

    def boom():
        raise rate_limiter.RateLimitExceeded("Daily call limit reached (1/day)", 60.0)

    monkeypatch.setattr(rate_limiter, "check_can_dial", boom)
    result = scheduler.run_tick(now=datetime(2026, 6, 4, 9, 30), settings=settings)
    assert result.dialed == 0
    assert calls == []
    assert result.outcomes[0]["status"] == "rate_limited"


# ----------------------------------------------------------------- cron wiring

def test_cron_is_dormant_when_the_switch_is_off():
    assert scheduler.start(_settings(schedule_calls_enabled=False)) is None
    state = scheduler.status(_settings())
    assert state["running"] is False
    assert state["enabled"] is False


def test_cron_starts_ticks_and_stops_when_enabled(monkeypatch):
    calls = _fake_place_call(monkeypatch)          # no patients -> no dials
    settings = _settings(schedule_calls_enabled=True, schedule_interval_minutes=30)
    job = scheduler.start(settings)
    try:
        assert job is not None
        state = scheduler.status(settings)
        assert state["running"] is True
        assert state["enabled"] is True
        assert state["next_tick_at"] is not None
        assert calls == []
    finally:
        scheduler.stop()
    assert scheduler.status(settings)["running"] is False


# ----------------------------------------------------------------- API

def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _headers_for(username: str, password: str, role: str) -> dict:
    db_service.create_staff(
        username=username, password=password, role=role, display_name=username
    )
    resp = _client().post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _due_patient_relative_to_today():
    """A discharge 4 days ago => the 3-day slot was due yesterday (inside the
    2-day grace window) no matter when the suite runs."""
    discharge = (date.today() - timedelta(days=4)).isoformat()
    return _patient(discharge=discharge)


def test_schedule_api_reports_the_plan_without_dialing():
    key = {"X-Api-Key": "test-calls-key"}
    _due_patient_relative_to_today()
    client = _client()

    board = client.get("/schedule", headers=key).json()
    assert board["scheduler"]["enabled"] is False
    assert board["count"] == 1
    assert board["due_count"] == 1
    assert board["patients"][0]["next_call_at"]  # TC3: shown to the dashboard
    assert board["patients"][0]["will_dial_automatically"] is False
    assert board["patients"][0]["cooldown_hours"] == 0.0

    due = client.get("/schedule/due", headers=key).json()
    assert due["count"] == 1
    assert due["scheduler_enabled"] is False

    status = client.get("/schedule/status", headers=key).json()
    assert status["enabled"] is False and status["running"] is False
    assert "OFF" in status["note"]


def test_run_now_requires_admin_and_never_dials_when_disabled():
    key = {"X-Api-Key": "test-calls-key"}
    _due_patient_relative_to_today()
    client = _client()

    nurse = _headers_for("nur8", "nurse-pass-8", "nurse")
    assert client.post("/schedule/run-now", json={}, headers=nurse).status_code == 403

    admin = _headers_for("adm8", "admin-pass-8", "admin")
    resp = client.post("/schedule/run-now", json={}, headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["planned_count"] == 1
    assert body["dialed"] == 0
    assert "no call placed" in body["message"]


def test_run_now_bad_timestamp_is_rejected():
    key = {"X-Api-Key": "test-calls-key"}
    resp = _client().post(
        "/schedule/run-now", json={"at": "not-a-date"}, headers=key
    )
    assert resp.status_code == 422


def test_health_reports_automatic_calls_state():
    data = _client().get("/health").json()
    assert data["automatic_calls_enabled"] is False
    assert data["scheduler_running"] is False
    assert data["auth_configured"] is True


# ------------------------------------------------------- runtime master switch

def test_switch_can_be_flipped_from_the_dashboard_api(monkeypatch):
    """POST /schedule/enabled arms/disarms the cron from the UI.

    Three properties matter: admin-only, persisted (survives restart), and
    ARMING NEVER DIALS BY ITSELF -- a due patient exists the whole time and
    the provider recorder stays empty (dial mocked, nothing is placed).
    """
    calls = _fake_place_call(monkeypatch)
    _due_patient_relative_to_today()          # someone IS due right now
    client = _client()
    key = {"X-Api-Key": "test-calls-key"}

    # auth: anonymous -> 401, nurse/doctor -> 403, admin -> 200
    assert client.post("/schedule/enabled", json={"enabled": True}).status_code == 401
    nurse = _headers_for("nur8t", "nurse-pass-8t", "nurse")
    assert client.post(
        "/schedule/enabled", json={"enabled": True}, headers=nurse
    ).status_code == 403

    admin = _headers_for("adm8t", "admin-pass-8t", "admin")
    on = client.post("/schedule/enabled", json={"enabled": True}, headers=admin)
    assert on.status_code == 200, on.text
    body = on.json()
    assert body["enabled"] is True and body["running"] is True
    assert body["next_tick_at"] is not None
    assert "ON" in body["note"]
    assert calls == []                        # arming placed NO call
    assert db_service.get_setting("schedule_calls_enabled") == "true"

    # every reader agrees now
    status = client.get("/schedule/status", headers=key).json()
    assert status["enabled"] is True and status["running"] is True
    board = client.get("/schedule", headers=key).json()
    assert board["scheduler"]["enabled"] is True
    assert board["patients"][0]["will_dial_automatically"] is True

    # an EXPLICIT tick now really dials (provider mocked)
    tick = client.post("/schedule/run-now", json={}, headers=admin)
    assert tick.json()["dialed"] == 1
    assert len(calls) == 1

    # turn it off again: cron stops, choice saved, tick goes plan-only
    off = client.post("/schedule/enabled", json={"enabled": False}, headers=admin)
    assert off.status_code == 200
    assert off.json()["enabled"] is False and off.json()["running"] is False
    assert db_service.get_setting("schedule_calls_enabled") == "false"
    assert scheduler.status()["running"] is False
    tick2 = client.post("/schedule/run-now", json={}, headers=admin)
    assert tick2.json()["enabled"] is False
    assert tick2.json()["dialed"] == 0
    assert len(calls) == 1


# ------------------------------------------------------- 24h per-patient cooldown

def test_cooldown_skips_a_patient_dialed_in_the_last_24h(monkeypatch):
    """Patient A was called at 2 PM: an automatic tick the same afternoon must
    skip them (planned, "cooldown" outcome, provider untouched) but still list
    them -- and the manual Call Now button is never gated by the window."""
    from datetime import datetime

    settings = _settings(schedule_calls_enabled=True)
    now = datetime(2026, 6, 4, 14, 0)           # "today at 2 PM"
    patient = _patient(discharge="2026-06-01")  # 3-day slot due today 09:00
    calls = _fake_place_call(monkeypatch)

    # First tick dials once and stamps the 24h window start.
    first = scheduler.run_tick(now=now, settings=settings)
    assert first.dialed == 1
    assert len(calls) == 1
    assert first.planned[0]["cooldown_hours"] == 0.0

    # Same afternoon: still planned, but skipped by the cooldown rail.
    second = scheduler.run_tick(now=now + timedelta(hours=4), settings=settings)
    assert second.dialed == 0
    assert len(calls) == 1                        # provider was NOT called
    assert len(second.planned) == 1               # still listed, honestly
    assert second.planned[0]["cooldown_hours"] == 20.0
    assert second.outcomes[0]["status"] == "cooldown"
    assert second.outcomes[0]["patient_code"] == patient.patient_code

    # The board says the same thing in the same words.
    rows = scheduler.plan_due(now=now + timedelta(hours=4), settings=settings)
    assert [r.patient_code for r in rows] == [patient.patient_code]
    assert rows[0].cooldown_hours == 20.0

    # Full window elapsed: dialling resumes on its own (2 PM the next day).
    third = scheduler.run_tick(now=now + timedelta(hours=24), settings=settings)
    assert third.dialed == 1
    assert len(calls) == 2


def test_manual_call_now_starts_the_cooldown_but_is_never_gated(monkeypatch):
    """A manual Call Now dial is never blocked, yet starts the same 24h window
    for the automatic tick -- the single knob the user asked for."""
    client = _client()
    calls = _fake_place_call(monkeypatch)
    db_service.upsert_patient(
        patient_code="P-820", name="Manual Coolan", phone_number="+94771000820",
        diagnosis_category="cardiac",
        discharge_date=(date.today() - timedelta(days=4)).isoformat(),
    )
    nurse = _headers_for("nur8c", "nurse-pass-8c", "nurse")

    manual = client.post(
        "/calls",
        json={"patient_code": "P-820", "diagnosis_category": "cardiac"},
        headers=nurse,
    )
    assert manual.status_code == 201, manual.text
    assert len(calls) == 1                       # manual dial is never gated

    # The very same minute: an automatic tick skips this patient (cooldown).
    admin = _headers_for("adm8c", "admin-pass-8c", "admin")
    assert client.post(
        "/schedule/enabled", json={"enabled": True}, headers=admin
    ).status_code == 200
    tick = client.post("/schedule/run-now", json={}, headers=admin).json()
    assert tick["dialed"] == 0
    assert len(calls) == 1                       # still exactly one dial
    assert [p["patient_code"] for p in tick["planned"]] == ["P-820"]
    assert [o["status"] for o in tick["outcomes"]] == ["cooldown"]


def test_saved_switch_wins_over_env_after_a_restart():
    """.env is only the fresh-DB default; a dashboard toggle survives restarts."""
    # nothing saved yet -> the .env/default value stands
    settings = _settings(schedule_calls_enabled=False)
    assert scheduler.apply_saved_switch(settings) is False
    assert settings.schedule_calls_enabled is False

    # admin flipped it ON in the UI -> a restart keeps it ON
    db_service.set_setting("schedule_calls_enabled", "true")
    settings = _settings(schedule_calls_enabled=False)   # .env says false
    assert scheduler.apply_saved_switch(settings) is True
    assert settings.schedule_calls_enabled is True

    # ...and OFF stays OFF even if .env would say true
    db_service.set_setting("schedule_calls_enabled", "false")
    settings = _settings(schedule_calls_enabled=True)
    assert scheduler.apply_saved_switch(settings) is False
    assert settings.schedule_calls_enabled is False