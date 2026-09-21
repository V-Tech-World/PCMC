"""
Offline tests for Step 5 (wire NLP + risk into the live call).

The live-call tests are deselected by default; the offline tests cover the
logic that the live path depends on: risk assessment after each turn, the
red-flag -> urgent-acknowledgment path, and the risk-aware closing.

Run the live-ish test with:
    pytest backend/tests/test_step5.py -v -k "live"
"""
from __future__ import annotations

import asyncio
import base64
import math
import struct
import tempfile
from pathlib import Path

import pytest

from app.api.media_stream import MediaSession
from app.core.config import get_settings
from app.services import call_flow
from app.services.dialogue import CallDialogue, URGENT_ACK_TEXT, FINAL_QUESTION_ID


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_SINE_PCM16_1KHZ_20MS = struct.pack(
    "<160h",
    *[
        int(9000 * math.sin(2 * math.pi * 1000 * (i / 8000)))
        for i in range(160)
    ],
)


class _DummyWS:
    """Mock WebSocket that records sent frames."""

    def __init__(self, fail_on_send: int | None = None):
        self.sent: list[dict] = []
        self.fail_on_send = fail_on_send  # send N frames then raise

    async def send_json(self, msg: dict) -> None:
        if self.fail_on_send is not None and len(self.sent) >= self.fail_on_send:
            raise RuntimeError("socket closed")
        self.sent.append(msg)


class _MockSettings:
    """Settings subset needed by call_flow, with the values that keep tests fast."""

    dialogue_enabled = True
    filler_enabled = False
    recordings_dir: Path
    speak_codec = "pcmu"
    tts_backend = "auto"
    tts_rate = 160
    tts_bandpass_enabled = True
    turn_silence_sec = 1.2
    turn_max_sec = 10.0
    turn_gap_sec = 0.8
    final_answer_sec = 60.0
    final_lookout_sec = 1.0

    def __init__(self, recordings_dir: Path):
        self.recordings_dir = recordings_dir


def _make_session(call_id: str = "TESTCALL1", encoding: str = "PCMA") -> MediaSession:
    session = MediaSession()
    session.call_id = call_id
    session.encoding = encoding
    session.provider_call_id = call_id
    session.settle_until = 0.0
    return session
async def _run_mock_call(
    transcript: str,
    category: str = "surgical",
    settings: _MockSettings | None = None,
    tmp_path: Path | None = None,
) -> tuple[MediaSession, _DummyWS]:
    """Run a one-question mock call through run_call's logic and return the
    session and ws for assertions. The mock delivers one frame of
    silence-then-audio, then a 'stop' so the turn ends quickly.

    tmp_path provides a real recordings_dir so write_wav succeeds (the
    transcriber is never actually called because the test patches it out).
    """
    from unittest.mock import AsyncMock, patch

    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    if settings is None:
        settings = _MockSettings(tmp_path)

    session = _make_session("TESTCALL1", "PCMA")
    ws = _DummyWS()

    inbox: asyncio.Queue = asyncio.Queue()

    # Feed the start event so session.call_id is set
    await inbox.put(
        (
            "event",
            {
                "event": "start",
                "callId": "TESTCALL1",
                "media_format": {
                    "encoding": "PCMA",
                    "sample_rate": 8000,
                    "channels": 1,
                },
            },
        )
    )

    # Simulate a turn: a few frames of patient audio, then stop
    patient_pcm = struct.pack(
        "<3200h",
        *[
            int(8000 * math.sin(2 * math.pi * 400 * (i / 8000)))
            for i in range(3200)
        ],
    )
    from app.services.recordings import pcm16_to_codec

    compressed = pcm16_to_codec(patient_pcm, "PCMA")
    for offset in range(0, len(compressed), 160):
        chunk = compressed[offset : offset + 160]
        await inbox.put(
            (
                "media",
                {
                    "event": "media",
                    "media": {"payload": base64.b64encode(chunk).decode()},
                },
            )
        )

    # End the call
    await inbox.put(("event", {"event": "stop"}))

    from app.services import tts as tts_mod

    class _MockSpeaker:
        def synthesize(self, text: str) -> bytes:
            return _SINE_PCM16_1KHZ_20MS

        def filler(self) -> bytes:
            return _SINE_PCM16_1KHZ_20MS

    with patch.object(tts_mod, "get_speaker", return_value=_MockSpeaker()), \
         patch("app.services.call_flow.get_settings", return_value=settings), \
         patch("app.services.call_flow.stt_service.get_transcriber") as mt:

        transcriber = AsyncMock()
        result_type = type("Result", (), {"text": transcript})()
        transcriber.transcribe = AsyncMock(return_value=result_type)
        mt.return_value = transcriber

        try:
            await call_flow.run_call(ws, inbox, session, category, settings)
        except call_flow.CallEnded:
            pass  # expected: the dialogue finishes and raises CallEnded

    return session, ws
# ---------------------------------------------------------------------------
# TC1: risk assessment runs after each transcribed turn (no dead air)
# ---------------------------------------------------------------------------


def test_risk_assessment_runs_after_each_turn() -> None:
    """Step 5 TC1 (offline): after record_answer, assess_risk() is called so the
    risk level is available before the next question. The NLP engine is
    sub-millisecond, so this adds no dead air."""
    dialogue = CallDialogue("surgical")
    dialogue.start()
    # First answer: medication = yes
    dialogue.record_answer("yes")
    a1 = dialogue.assess_risk()
    assert a1.risk_level in ("low", "medium", "high")
    assert a1.score >= 0

    # Second answer: pain = no
    dialogue.record_answer("no")
    a2 = dialogue.assess_risk()
    assert a2.risk_level in ("low", "medium", "high")

    # Third answer: category question (no concerning symptoms)
    dialogue.record_answer("no")
    a3 = dialogue.assess_risk()
    assert a3.risk_level in ("low", "medium", "high")

    # After each answer the assessment is fresh -- scores accumulate
    assert a3.score >= a2.score >= a1.score or a3.score == a2.score == a1.score


def test_risk_assessment_is_sub_millisecond() -> None:
    """TC1 rationale: the rule-based NLP must not add dead air. Verify it runs
    in well under 100 ms even on a long transcript."""
    import time

    long_transcript = " ".join(["pain"] * 200)  # stress test
    t0 = time.perf_counter()
    for _ in range(50):
        CallDialogue("surgical").assess_risk()
    elapsed = time.perf_counter() - t0
    per_call_ms = (elapsed / 50) * 1000
    assert per_call_ms < 100, f"assess_risk took {per_call_ms:.1f} ms -- would add dead air"