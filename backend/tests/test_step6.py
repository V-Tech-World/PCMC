"""
Step 6 tests: every call persisted (SQLModel + SQLite), linked to the right
patient, and durable across a restart. All offline -- each test uses its own
tmp SQLite file (TC3 "survives a restart" is simulated by dropping the cached
engine and reopening the same file).
"""
from __future__ import annotations

import pytest

from app.db import service as db_service
from app.db.engine import _engine_for, init_db, resolve_database_url
from app.db.models import CallRecord, Patient
from app.services.nlp import assess_conversation


@pytest.fixture()
def db_url(tmp_path):
    """Fresh SQLite file per test; clear the cached engine first."""
    _engine_for.cache_clear()
    url = f"sqlite:///{(tmp_path / 'voicecare.db').as_posix()}"
    init_db(url)
    yield url
    _engine_for.cache_clear()


def _seed_patient(db_url, code="P-0001", phone="+94777000001", **kw) -> int:
    with db_service.new_session(db_url) as session:
        patient = Patient(
            patient_code=code, name="Kamal Perera",
            phone_number=phone, diagnosis_category=kw.pop("category", "cardiac"),
            **kw,
        )
        session.add(patient)
        session.commit()
        session.refresh(patient)
        return patient.id


class _FakeDialogue:
    """Just enough of CallDialogue for record_call()."""

    diagnosis_category = "cardiac"

    def __init__(self, answers):
        self._answers = answers

    def summary(self):
        return self._answers


def _high_assessment():
    answers = [
        {"question_id": "medication", "kind": "yes_no",
         "interpretation": True, "transcript": "no I forgot"},
        {"question_id": "category_cardiac", "kind": "yes_no",
         "interpretation": True, "transcript": "yes I have chest pain"},
    ]
    return assess_conversation(answers), answers


# ----------------------------------------------------------------- TC1


def test_record_call_persists_transcript_risk_and_timestamps(db_url):
    """TC1: one row per call with the transcript, risk and timestamps."""
    assessment, answers = _high_assessment()
    record = db_service.record_call(
        dialogue=_FakeDialogue(answers),
        assessment=assessment,
        provider_call_id="call_abc",
        to_number="+94777000001",
        ended_reason="dialogue finished",
        duration_sec=95.5,
        database_url=db_url,
    )
    assert record is not None and record.id is not None

    rows = db_service.list_calls(database_url=db_url)
    assert len(rows) == 1
    row = rows[0]
    assert row.provider_call_id == "call_abc"
    assert row.risk_level == "high"
    assert row.risk_score >= 5.0
    assert row.ended_reason == "dialogue finished"
    assert abs(row.duration_sec - 95.5) < 0.01
    assert row.started_at is not None and row.finished_at is not None
    stored = row.get_answers()
    assert [a["question_id"] for a in stored] == ["medication", "category_cardiac"]
    assert stored[1]["transcript"] == "yes I have chest pain"
    assert any("chest pain" in r for r in row.get_risk_reasons())
    assert row.get_findings()  # the red flag was persisted


# ----------------------------------------------------------------- TC2


def test_call_links_to_patient_by_code(db_url):
    """TC2: patient_code links the row to the right patient record."""
    patient_id = _seed_patient(db_url)
    assessment, answers = _high_assessment()
    record = db_service.record_call(
        dialogue=_FakeDialogue(answers), assessment=assessment,
        patient_code="P-0001", to_number="+94777000001",
        ended_reason="dialogue finished", database_url=db_url,
    )
    assert record.patient_id == patient_id
    assert record.patient_code == "P-0001"
    assert record.phone_number == "+94777000001"


def test_call_links_to_patient_by_phone_when_code_unknown(db_url):
    """Dials placed without a patient_code still link via the dialed number."""
    _seed_patient(db_url, phone="+94777000009")
    assessment, answers = _high_assessment()
    record = db_service.record_call(
        dialogue=_FakeDialogue(answers), assessment=assessment,
        to_number="+94777000009", ended_reason="dialogue finished",
        database_url=db_url,
    )
    assert record.patient_id is not None
    assert record.patient_code == "P-0001"


def test_unlinked_call_keeps_number_and_empty_patient(db_url):
    assessment, answers = _high_assessment()
    record = db_service.record_call(
        dialogue=_FakeDialogue(answers), assessment=assessment,
        to_number="+94777009999", ended_reason="dialogue finished",
        database_url=db_url,
    )
    assert record.patient_id is None
    assert record.patient_code == ""
    assert record.phone_number == "+94777009999"


# ----------------------------------------------------------------- TC3


def test_data_survives_a_backend_restart(db_url):
    """TC3: drop the cached engine (simulated restart) and reopen the file --
    the row and its patient link are still there."""
    patient_id = _seed_patient(db_url)
    assessment, answers = _high_assessment()
    record = db_service.record_call(
        dialogue=_FakeDialogue(answers), assessment=assessment,
        patient_code="P-0001", ended_reason="dialogue finished",
        database_url=db_url,
    )
    assert record.id is not None

    # "Restart": forget the engine, open the same file fresh.
    _engine_for.cache_clear()
    init_db(db_url)
    rows = db_service.list_calls(database_url=db_url)
    assert len(rows) == 1
    assert rows[0].id == record.id
    assert rows[0].patient_id == patient_id
    assert rows[0].risk_level == "high"
    assert rows[0].get_answers()[0]["transcript"] == "no I forgot"


# ------------------------------------------------- persistence edge cases


def test_record_call_survives_unknown_code(db_url):
    """patient_code=None (passthrough dial) still produces a row."""
    assessment, answers = _high_assessment()
    record = db_service.record_call(
        dialogue=_FakeDialogue(answers), assessment=assessment,
        ended_reason="call ended by patient after answer", database_url=db_url,
    )
    assert record.ended_reason == "call ended by patient after answer"


def test_record_call_low_risk_is_persisted_too(db_url):
    answers = [
        {"question_id": "medication", "kind": "yes_no",
         "interpretation": False, "transcript": "no"},
        {"question_id": "category_cardiac", "kind": "yes_no",
         "interpretation": False, "transcript": "no nothing"},
    ]
    record = db_service.record_call(
        dialogue=_FakeDialogue(answers), assessment=assess_conversation(answers),
        ended_reason="dialogue finished", database_url=db_url,
    )
    assert record.risk_level in ("low", "medium")


def test_resolve_database_url_defaults_to_file():
    assert resolve_database_url("").startswith("sqlite:///")
    assert resolve_database_url(None).startswith("sqlite:///")
    assert resolve_database_url("sqlite://") == "sqlite://"
    custom = "sqlite:////tmp/x.db"
    assert resolve_database_url(custom) == custom


# -------------------------------------------------- API + full-flow integration


def test_records_api_upserts_patients(db_url):
    """POST /records/patients creates then updates; GET lists them."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    key = {"X-Api-Key": "test-calls-key"}

    resp = client.post("/records/patients", json={
        "patient_code": "P-777", "name": "Nimali",
        "phone_number": "+94777000777", "diagnosis_category": "surgical",
    }, headers=key)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "created"

    # Upsert again -> update, still one row.
    resp = client.post("/records/patients", json={
        "patient_code": "P-777", "name": "Nimali F.",
        "phone_number": "+94777000777", "diagnosis_category": "surgical",
    }, headers=key)
    assert resp.status_code == 201
    assert resp.json()["status"] == "updated"
    patients = client.get("/records/patients", headers=key).json()
    assert patients["count"] == 1
    assert patients["patients"][0]["name"] == "Nimali F."


def test_records_api_requires_api_key():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/records/calls").status_code == 401


def test_run_call_persists_a_row(tmp_path):
    """Full-flow integration: the mocked Step 5 run_call now writes a row."""
    import asyncio

    from tests.test_step5 import _run_mock_call

    _engine_for.cache_clear()
    url = f"sqlite:///{(tmp_path / 'flow.db').as_posix()}"
    init_db(url)
    try:
        asyncio.run(_run_mock_call(
            tmp_path,
            ["yes", "no", "yes I have chest pain", "it is severe"],
            category="cardiac",
            database_url=url,
        ))
        rows = db_service.list_calls(database_url=url)
        assert len(rows) == 1
        row = rows[0]
        assert row.risk_level == "high"
        assert row.diagnosis_category == "cardiac"
        assert len(row.get_answers()) == 4
        assert row.ended_reason == "dialogue finished"
    finally:
        _engine_for.cache_clear()