"""
Step 7 tests: the HIGH-risk WhatsApp alert via the Zernio sandbox inbox.

All offline: requests.post is mocked (no real WhatsApp message is ever sent
from the suite). Covers README TC1/TC2/TC3 plus the config gate.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.services import alerts


def _alert_settings(**overrides) -> Settings:
    defaults = dict(
        zernio_api_key="sk_test",
        alerts_enabled=True,
        inbox_account_id="acct-123",
        alert_conversation_id="conv-456",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


class _FakeRecord:
    """Duck-typed CallRecord for format/gate tests."""

    id = 42
    patient_code = "P-0001"
    phone_number = "+94777000001"
    diagnosis_category = "cardiac"
    provider_call_id = "call_abc"
    risk_level = "high"
    risk_score = 7.0
    ended_reason = "dialogue finished"
    started_at = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 9, 21, 9, 1, 35, tzinfo=timezone.utc)
    created_at = finished_at
    alert_status = "not_sent"
    alert_detail = ""
    _answers = [
        {"question_id": "medication", "kind": "yes_no",
         "interpretation": True, "transcript": "no I forgot my morning dose"},
        {"question_id": "category_cardiac", "kind": "yes_no",
         "interpretation": True, "transcript": "yes I have chest pain"},
    ]
    _findings = [
        {"id": "chest_pain", "label": "chest pain", "severity": "severe",
         "red_flag": True, "points": 5, "matched_text": "chest pain",
         "source": "text"},
    ]

    def get_answers(self):
        return self._answers

    def get_findings(self):
        return self._findings


# ------------------------------------------------------------- TC3: content


def test_alert_message_contains_patient_risk_and_symptoms():
    """TC3: patient code, risk level and key symptoms are all in the text."""
    message = alerts.format_alert_message(_FakeRecord())
    assert "P-0001" in message
    assert "HIGH" in message
    assert "chest pain" in message
    assert "severe" in message
    # Every detail of the call is included:
    assert "+94777000001" in message
    assert "cardiac" in message
    assert "no I forgot my morning dose" in message
    assert "yes I have chest pain" in message
    assert "call_abc" in message


# ------------------------------------------------------------- TC2: gate


def test_low_and_medium_risk_do_not_alert(monkeypatch):
    """TC2: only HIGH risk pages the care team -- no HTTP call is made."""
    called = {"n": 0}

    def _fail(*a, **k):
        called["n"] += 1
        raise AssertionError("must not call the provider for low risk")

    monkeypatch.setattr(alerts.requests, "post", _fail)
    record = _FakeRecord()
    record.risk_level = "low"
    assert alerts.maybe_send_alert(record, _alert_settings()) == "skipped"
    record.risk_level = "medium"
    assert alerts.maybe_send_alert(record, _alert_settings()) == "skipped"
    assert called["n"] == 0


# ------------------------------------------------------------- TC1: send


def test_high_risk_sends_whatsapp_via_sandbox_conversation(monkeypatch):
    """TC1: a HIGH-risk call posts the message to the sandbox thread."""
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        resp = type("R", (), {})()
        resp.status_code = 200
        resp.json = lambda: {"id": "msg_9", "success": True}
        return resp

    monkeypatch.setattr(alerts.requests, "post", _fake_post)
    status = alerts.maybe_send_alert(_FakeRecord(), _alert_settings())
    assert status.startswith("sent")
    assert "msg_9" in status
    assert captured["url"].endswith("/inbox/conversations/conv-456/messages")
    assert captured["headers"]["Authorization"] == "Bearer sk_test"
    assert captured["json"]["accountId"] == "acct-123"
    assert "P-0001" in captured["json"]["message"]


def test_provider_rejection_is_reported_not_raised(monkeypatch):
    def _fail_post(url, headers=None, json=None, timeout=None):
        resp = type("R", (), {})()
        resp.status_code = 500
        resp.json = lambda: {"error": "boom"}
        return resp

    monkeypatch.setattr(alerts.requests, "post", _fail_post)
    status = alerts.maybe_send_alert(_FakeRecord(), _alert_settings())
    assert status.startswith("failed")


def test_not_configured_high_risk_does_not_call_provider(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("must not call the provider when unconfigured")

    monkeypatch.setattr(alerts.requests, "post", _fail)
    settings = _alert_settings(inbox_account_id="", alert_conversation_id="")
    assert alerts.maybe_send_alert(_FakeRecord(), settings) == "not_configured"


def test_alerts_disabled_switches_everything_off(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("must not call the provider when disabled")

    monkeypatch.setattr(alerts.requests, "post", _fail)
    settings = _alert_settings(alerts_enabled=False)
    assert alerts.maybe_send_alert(_FakeRecord(), settings) == "not_configured"
    with pytest.raises(alerts.AlertError):
        alerts.send_whatsapp_alert("x", settings)


# ------------------------------------------------ live-path integration


def test_run_call_high_risk_fires_alert_and_updates_row(tmp_path, monkeypatch):
    """Full flow: mocked high-risk run_call -> row persisted + alert 'sent'."""
    import asyncio

    from unittest.mock import patch

    from app.db import service as db_service
    from app.db.engine import _engine_for, init_db
    from tests.test_step5 import _run_mock_call

    def _fake_post(url, headers=None, json=None, timeout=None):
        assert "/inbox/conversations/conv-456/messages" in url
        resp = type("R", (), {})()
        resp.status_code = 200
        resp.json = lambda: {"id": "msg_live"}
        return resp

    _engine_for.cache_clear()
    url = f"sqlite:///{(tmp_path / 'alert.db').as_posix()}"
    init_db(url)
    try:
        with patch.object(alerts.requests, "post", _fake_post):
            asyncio.run(_run_mock_call(
                tmp_path,
                ["yes", "no", "yes I have chest pain", "it is severe"],
                category="cardiac",
                database_url=url,
                alerts_enabled=True,
                inbox_account_id="acct-123",
                alert_conversation_id="conv-456",
            ))
        row = db_service.list_calls(database_url=url)[0]
        assert row.risk_level == "high"
        assert row.alert_status.startswith("sent")
        assert "msg_live" in row.alert_status
    finally:
        _engine_for.cache_clear()


def test_run_call_low_risk_skips_alert(tmp_path):
    """Full flow: benign answers -> row persisted, alert_status 'skipped'."""
    import asyncio

    from app.db import service as db_service
    from app.db.engine import _engine_for, init_db
    from tests.test_step5 import _run_mock_call

    _engine_for.cache_clear()
    url = f"sqlite:///{(tmp_path / 'noalert.db').as_posix()}"
    init_db(url)
    try:
        asyncio.run(_run_mock_call(
            tmp_path,
            ["yes", "no", "no", "nothing else"],
            category="general",
            database_url=url,
            alerts_enabled=True,
            inbox_account_id="acct-123",
            alert_conversation_id="conv-456",
        ))
        row = db_service.list_calls(database_url=url)[0]
        assert row.risk_level in ("low", "medium")
        assert row.alert_status == "skipped"
    finally:
        _engine_for.cache_clear()