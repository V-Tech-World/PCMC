"""
Step 7 tests: HIGH-risk alerts (reworked) -- message prepared + stored, and
optionally delivered over WhatsApp via the Zernio sandbox inbox.

All offline: requests.post is mocked (no real WhatsApp message is ever sent
from the suite). Covers README TC1/TC2/TC3 for both delivery modes:

- ALERT_DELIVERY=ready    (the default) -> full message prepared and returned
  for the call row, nothing leaves the backend
- ALERT_DELIVERY=whatsapp               -> the original sandbox transport
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.services import alerts


def _alert_settings(**overrides) -> Settings:
    """Settings for the OPT-IN WhatsApp transport."""
    defaults = dict(
        zernio_api_key="sk_test",
        alerts_enabled=True,
        alert_delivery="whatsapp",
        inbox_account_id="acct-123",
        alert_conversation_id="conv-456",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def _ready_settings(**overrides) -> Settings:
    """The shipped default: prepare + store, deliver nothing."""
    defaults = dict(alerts_enabled=True, alert_delivery="ready")
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


# ------------------------------------------------- reworked: prepare + store


def test_default_delivery_is_ready_and_sends_nothing(monkeypatch):
    """The shipped default prepares the alert text and posts nothing."""
    def _fail(*a, **k):
        raise AssertionError("ready mode must not call the provider")

    monkeypatch.setattr(alerts.requests, "post", _fail)
    assert Settings(_env_file=None).alert_delivery == "ready"

    outcome = alerts.prepare_alert(_FakeRecord(), None, _ready_settings())
    assert outcome.status == "ready"
    assert outcome.message.startswith("\U0001F6A8")
    assert "P-0001" in outcome.message
    assert "HIGH" in outcome.message
    assert "chest pain" in outcome.message
    assert "ready" in outcome.detail


def test_unknown_delivery_mode_falls_back_to_ready(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("a typo in ALERT_DELIVERY must not send")

    monkeypatch.setattr(alerts.requests, "post", _fail)
    settings = _ready_settings(alert_delivery="carrier-pigeon")
    assert alerts.delivery_mode(settings) == alerts.DELIVERY_READY
    assert alerts.prepare_alert(_FakeRecord(), None, settings).status == "ready"


# ------------------------------------------------------------- TC2: gate


def test_low_and_medium_risk_do_not_alert(monkeypatch):
    """TC2: only HIGH risk produces an alert -- no HTTP call, no message."""
    called = {"n": 0}

    def _fail(*a, **k):
        called["n"] += 1
        raise AssertionError("must not call the provider for low risk")

    monkeypatch.setattr(alerts.requests, "post", _fail)
    record = _FakeRecord()
    for level in ("low", "medium"):
        record.risk_level = level
        for settings in (_alert_settings(), _ready_settings()):
            outcome = alerts.prepare_alert(record, None, settings)
            assert outcome.status == "skipped"
            assert outcome.message == ""
    assert called["n"] == 0


# ------------------------------------------------------------- TC1: send


def test_high_risk_sends_whatsapp_via_sandbox_conversation(monkeypatch):
    """TC1 (opt-in transport): a HIGH-risk call posts to the sandbox thread."""
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        resp = type("R", (), {})()
        resp.status_code = 200
        resp.json = lambda: {"id": "msg_9", "success": True}
        return resp

    monkeypatch.setattr(alerts.requests, "post", _fake_post)
    outcome = alerts.prepare_alert(_FakeRecord(), None, _alert_settings())
    assert outcome.status.startswith("sent")
    assert "msg_9" in outcome.status
    assert "msg_9" in outcome.detail
    assert captured["url"].endswith("/inbox/conversations/conv-456/messages")
    assert captured["headers"]["Authorization"] == "Bearer sk_test"
    assert captured["json"]["accountId"] == "acct-123"
    assert "P-0001" in captured["json"]["message"]
    # The stored text is the same message that went out.
    assert outcome.message == captured["json"]["message"]


def test_provider_rejection_is_reported_not_raised(monkeypatch):
    def _fail_post(url, headers=None, json=None, timeout=None):
        resp = type("R", (), {})()
        resp.status_code = 500
        resp.json = lambda: {"error": "boom"}
        return resp

    monkeypatch.setattr(alerts.requests, "post", _fail_post)
    outcome = alerts.prepare_alert(_FakeRecord(), None, _alert_settings())
    assert outcome.status.startswith("failed")
    assert "boom" in outcome.status
    # Even a failed delivery keeps the message on the row.
    assert "P-0001" in outcome.message


def test_not_configured_transport_still_prepares_the_message(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("must not call the provider when unconfigured")

    monkeypatch.setattr(alerts.requests, "post", _fail)
    settings = _alert_settings(inbox_account_id="", alert_conversation_id="")
    outcome = alerts.prepare_alert(_FakeRecord(), None, settings)
    assert outcome.status == "not_configured"
    assert "P-0001" in outcome.message      # nothing is lost
    assert "ZERNIO_INBOX_ACCOUNT_ID" in outcome.detail


def test_alerts_disabled_switches_the_transport_off(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("must not call the provider when disabled")

    monkeypatch.setattr(alerts.requests, "post", _fail)
    settings = _alert_settings(alerts_enabled=False)
    assert alerts.prepare_alert(_FakeRecord(), None, settings).status == (
        "not_configured"
    )
    with pytest.raises(alerts.AlertError):
        alerts.send_whatsapp_alert("x", settings)


# ------------------------------------------------ live-path integration


def test_run_call_high_risk_stores_the_ready_message(tmp_path, monkeypatch):
    """Full flow, default mode: high risk -> row 'ready' + message on the row.

    No HTTP is allowed to happen: this is the shipped configuration, so the
    alert has to be fully prepared and visible in the dashboard without any
    delivery channel.
    """
    import asyncio

    from unittest.mock import patch

    from app.db import service as db_service
    from app.db.engine import _engine_for, init_db
    from tests.test_step5 import _run_mock_call

    def _fail_post(*a, **k):
        raise AssertionError("ALERT_DELIVERY=ready must not deliver anything")

    _engine_for.cache_clear()
    url = f"sqlite:///{(tmp_path / 'ready.db').as_posix()}"
    init_db(url)
    try:
        with patch.object(alerts.requests, "post", _fail_post):
            asyncio.run(_run_mock_call(
                tmp_path,
                ["no I forgot my dose", "yes", "it is severe",
                 "yes I have chest pain", "it is severe"],
                category="cardiac",
                database_url=url,
                alert_delivery="ready",
            ))
        row = db_service.list_calls(database_url=url)[0]
        assert row.risk_level == "high"
        assert row.alert_status == "ready"
        assert "ready" in row.alert_detail
        # Everything the nurse needs is in the stored message.
        assert row.alert_message.startswith("\U0001F6A8")
        assert "HIGH" in row.alert_message
        assert "chest pain" in row.alert_message
        assert "no I forgot my dose" in row.alert_message
    finally:
        _engine_for.cache_clear()


def test_run_call_high_risk_fires_whatsapp_when_selected(tmp_path, monkeypatch):
    """Full flow, opt-in transport: high risk -> row 'sent (<id>)'."""
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
                alert_delivery="whatsapp",
                inbox_account_id="acct-123",
                alert_conversation_id="conv-456",
            ))
        row = db_service.list_calls(database_url=url)[0]
        assert row.risk_level == "high"
        assert row.alert_status.startswith("sent")
        assert "msg_live" in row.alert_status
        assert "chest pain" in row.alert_message
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
        assert row.alert_message == ""      # nothing prepared for a calm call
    finally:
        _engine_for.cache_clear()