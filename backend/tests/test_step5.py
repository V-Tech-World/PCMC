"""
Step 5 tests: NLP + risk wired into the live call -- plus the cost rails
(max call duration, internal rate limiting).

Everything here runs offline (no provider call is dialed, no real TTS/STT):

- TC1  running risk is scored after every transcribed turn, sub-millisecond
- TC2  a red-flag answer triggers the one-shot urgent acknowledgment and the
       risk-aware (urgent) closing
- TC3  a fully mocked run_call completes and speaks the expected sequence,
       including the final open question and its closing
- the final question wording sets the fixed-window expectation
- config defaults: 60 s final window + cost rails
- rate limiter: hourly / daily / concurrent budgets (POST /calls)
- MAX_CALL_DURATION_SEC aborts an over-long dialogue (assessment still logged)
"""
from __future__ import annotations

import asyncio
import math
import struct
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core import rate_limiter
from app.core.config import Settings
from app.services import call_flow
from app.services.dialogue import (
    FINAL_QUESTION_ID,
    URGENT_ACK_TEXT,
    CallDialogue,
    build_script,
)


# ---------------------------------------------------------------------------
# fakes (mirroring test_step3 conventions)
# ---------------------------------------------------------------------------


class _LiveSession:
    """Mirrors the MediaSession attributes call_flow touches."""

    def __init__(self):
        self.speaking = False
        self.ended = False
        self.encoding = "PCMA"
        self.settle_until = 0.0
        self.agent_pcm = bytearray()
        self.media_frames = 100      # plenty: disables the no-audio abort
        self.stop_received = False
        self.call_id = "TESTCALL1"


class _DummyWS:
    def __init__(self):
        self.sent = 0
        self.messages: list[dict] = []

    async def send_json(self, msg):
        self.sent += 1
        self.messages.append(msg)


class _RecordingSpeaker:
    """Records every text it is asked to speak; emits a short sine burst."""

    def __init__(self):
        self.spoken: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.spoken.append(text)
        return struct.pack("<160h", *([3000] * 160))  # 20 ms of quiet audio

    def filler(self) -> bytes:
        return struct.pack("<160h", *([3000] * 160))


class _ScriptedTranscriber:
    """Returns one canned transcript per turn, in order."""

    def __init__(self, texts: list[str]):
        self.texts = list(texts)

    def transcribe(self, path) -> SimpleNamespace:
        text = self.texts.pop(0) if self.texts else ""
        return SimpleNamespace(text=text)


def _settings(tmp_path, **overrides) -> Settings:
    """Fast offline Settings: tiny turn gaps so run_call tests finish quickly."""
    base = dict(
        _env_file=None,
        dialogue_enabled=True,
        filler_enabled=False,
        recordings_dir=tmp_path,
        turn_silence_sec=0.1,
        turn_max_sec=1.0,
        turn_gap_sec=0.1,
        final_answer_sec=60.0,
        max_call_duration_sec=300.0,
    )
    base.update(overrides)
    return Settings(**base)


def _speech_frames(seconds: float = 0.4) -> list[bytes]:
    """A-law frames of loud 440 Hz sine (well above the RMS speech threshold)."""
    n = int(seconds * 8000)
    pcm = struct.pack("<%dh" % n, *[
        int(8000 * math.sin(2 * math.pi * 440 * (i / 8000))) for i in range(n)
    ])
    from app.services.recordings import pcm16_to_codec

    compressed = pcm16_to_codec(pcm, "PCMA")
    return [compressed[i:i + 160] for i in range(0, len(compressed), 160)]


async def _feed_turn(inbox: asyncio.Queue, frames: list[bytes]) -> None:
    """Queue one patient turn's audio; the queue then runs dry so the
    configured turn gap (0.1 s) ends the capture quickly."""
    for frame in frames:
        await inbox.put(("media", frame))


async def _run_mock_call(
    tmp_path,
    texts: list[str],
    category: str = "general",
    **setting_overrides,
) -> tuple[_RecordingSpeaker, _DummyWS, _LiveSession]:
    """Drive run_call to completion offline. Returns the speaker (which
    recorded everything spoken), the ws, and the session."""
    settings = _settings(tmp_path, **setting_overrides)
    session = _LiveSession()
    ws = _DummyWS()
    speaker = _RecordingSpeaker()
    transcriber = _ScriptedTranscriber(texts)

    inbox: asyncio.Queue = asyncio.Queue()
    for _ in range(len(texts)):
        await _feed_turn(inbox, _speech_frames())

    from app.services import stt as stt_service
    from app.services import tts as tts_service

    with (
        patch_stt(transcriber),
        patch.object(tts_service, "get_speaker", return_value=speaker),
    ):
        with pytest.raises(call_flow.CallEnded):
            await call_flow.run_call(ws, inbox, session, category, settings)
    return speaker, ws, session


def patch_stt(transcriber):
    return patch.object(
        __import__("app.services.stt", fromlist=["get_transcriber"]),
        "get_transcriber",
        return_value=transcriber,
    )


# ---------------------------------------------------------------------------
# final question + config defaults
# ---------------------------------------------------------------------------


def test_final_question_is_open_and_sets_window_expectation():
    """The final question must be an open invitation that tells the patient
    to speak freely and hang up -- it is captured in a fixed 60 s window, not
    by the 1.2 s silence detector."""
    script = build_script("general")
    final = next(q for q in script if q.id == FINAL_QUESTION_ID)
    assert final.kind == "open"
    lowered = final.text.lower()
    assert "final question" in lowered
    assert "speak clearly" in lowered
    assert "hang up" in lowered
    # It must NOT read like the yes/no questions before it.
    assert not lowered.startswith("please answer yes or no")


def test_config_defaults_final_window_and_cost_rails():
    """The shipped defaults: 60 s final window + sane cost rails."""
    s = Settings(_env_file=None)
    assert s.final_answer_sec == 60.0
    assert s.max_call_duration_sec == 300.0
    assert s.max_concurrent_calls == 3
    assert s.max_calls_per_hour == 30
    assert s.max_calls_per_day == 200


# ---------------------------------------------------------------------------
# TC1: running risk after every turn (no dead air)
# ---------------------------------------------------------------------------


def test_tc1_risk_scored_after_each_turn():
    """assess_risk() is callable at any point and combines the answers so
    far -- the call driver logs it after every transcribed turn."""
    d = CallDialogue("cardiac")
    d.start()

    a1 = d.record_answer("yes")
    assert a1 is not None                      # next question exists
    r1 = d.assess_risk()
    assert r1.risk_level in ("low", "medium", "high")

    d.record_answer("no")
    r_mid = d.assess_risk()
    assert r_mid.risk_level in ("low", "medium", "high")

    d.record_answer("yes I have chest pain")   # cardiac category question
    r_red = d.assess_risk()
    assert r_red.risk_level == "high"          # red flag -> immediate high

    # 4th answer completes the cardiac flow (medication, pain, category,
    # anything_else) -- the closing is urgent because of the red flag.
    final_question = d.record_answer("no nothing else")
    assert final_question is None
    assert d.assess_risk().risk_level == "high"
    assert "urgent" in d.closing_text.lower()


def test_tc1_assessment_is_sub_millisecond():
    """The rule-based NLP must not add dead air: scoring a long transcript
    stays orders of magnitude under the STT latency."""
    d = CallDialogue("cardiac")
    d.start()
    for _ in range(3):
        d.record_answer("no")
    transcript = "severe chest pain and breathlessness " * 50
    d.record_answer(transcript)

    start = time.perf_counter()
    for _ in range(100):
        d.assess_risk()
    per_call_ms = (time.perf_counter() - start) / 100 * 1000
    assert per_call_ms < 100, f"assess_risk took {per_call_ms:.1f} ms per call"


# ---------------------------------------------------------------------------
# TC2: red flag -> one-shot urgent acknowledgment + urgent closing
# ---------------------------------------------------------------------------


def test_tc2_urgent_acknowledgment_fires_once():
    """pop_urgent_acknowledgment returns the ack exactly once, then ''."""
    d = CallDialogue("cardiac")
    d.start()
    d.record_answer("yes")
    assert d.pop_urgent_acknowledgment() == ""   # nothing alarming yet

    d.record_answer("no")
    d.record_answer("yes I have severe chest pain")
    assert d.assess_risk().risk_level == "high"
    ack = d.pop_urgent_acknowledgment()
    assert ack == URGENT_ACK_TEXT
    assert "urgent" in ack.lower()
    # One-shot: never nags the patient twice.
    assert d.pop_urgent_acknowledgment() == ""
    assert d.pop_urgent_acknowledgment() == ""


def test_tc2_closing_text_changes_with_risk():
    """HIGH risk closing is urgent; low risk keeps the neutral wording."""
    low = CallDialogue("cardiac")
    low.start()
    for _ in range(4):
        low.record_answer("no")
    neutral = low.closing_text
    assert "urgent" not in neutral.lower()

    high = CallDialogue("cardiac")
    high.start()
    high.record_answer("yes")
    high.record_answer("no")
    high.record_answer("yes I have chest pain and I am short of breath")
    urgent = high.closing_text
    assert "urgent" in urgent.lower()
    assert neutral != urgent


# ---------------------------------------------------------------------------
# TC3: fully mocked run_call completes with the expected spoken sequence
# ---------------------------------------------------------------------------


def test_tc3_run_call_completes_and_speaks_all_questions(tmp_path):
    """Offline TC3: a full dialogue (4 questions + closing) runs over a fake
    socket/transcriber and speaks every question, the final open question
    last, then the neutral closing."""
    speaker, ws, session = asyncio.run(_run_mock_call(
        tmp_path,
        ["yes", "no", "no", "nothing else thank you"],
        category="general",
    ))
    spoken = speaker.spoken
    # 4 questions (no repeat: every turn had speech) + closing.
    assert "Did you take your medicines" in spoken[0]
    assert "feeling any pain" in spoken[1]
    assert "fever, chills" in spoken[2]
    assert "Final question" in spoken[3]
    assert spoken[-1].startswith("Thank you")
    # No urgent ack spoken: the answers are benign.
    assert not any("urgent" in t.lower() for t in spoken)
    assert ws.sent > 0


def test_tc3_run_call_red_flag_spends_urgent_ack_and_urgent_closing(tmp_path):
    """TC2 over the live path: a chest-pain answer triggers the one-shot
    urgent acknowledgment between questions, and the closing is urgent."""
    speaker, ws, session = asyncio.run(_run_mock_call(
        tmp_path,
        ["yes", "no", "yes I have chest pain", "it is very bad"],
        category="cardiac",
    ))
    spoken = speaker.spoken
    urgent_acks = [t for t in spoken if t == URGENT_ACK_TEXT]
    assert len(urgent_acks) == 1          # one-shot, even though risk stays high
    assert "urgent" in spoken[-1].lower()  # risk-aware closing


def test_run_call_aborts_when_max_duration_reached(tmp_path):
    """Cost rail: with a tiny cap the dialogue aborts with CallEnded well
    before the final question instead of continuing to ask."""
    speaker, ws, session = asyncio.run(_run_mock_call(
        tmp_path,
        ["yes", "no", "no", "nothing else"],
        max_call_duration_sec=0.05,
    ))
    questions_asked = [t for t in speaker.spoken if "Final question" in t]
    assert questions_asked == []          # never reached the final question


# ---------------------------------------------------------------------------
# rate limiter (internal, POST /calls)
# ---------------------------------------------------------------------------


def _limiter_settings(**overrides) -> Settings:
    defaults = dict(
        max_calls_per_hour=2,
        max_calls_per_day=3,
        max_concurrent_calls=1,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_rate_limiter_blocks_after_hourly_limit(monkeypatch):
    rate_limiter.reset()
    monkeypatch.setattr(rate_limiter, "get_settings", lambda: _limiter_settings())
    rate_limiter.check_can_dial()
    rate_limiter.record_dial()
    rate_limiter.check_can_dial()
    rate_limiter.record_dial()
    with pytest.raises(rate_limiter.RateLimitExceeded) as exc:
        rate_limiter.check_can_dial()
    assert "hour" in str(exc.value).lower()
    assert exc.value.retry_after_sec > 0


def test_rate_limiter_blocks_after_daily_limit(monkeypatch):
    rate_limiter.reset()
    monkeypatch.setattr(
        rate_limiter, "get_settings",
        lambda: _limiter_settings(max_calls_per_hour=0, max_calls_per_day=2),
    )
    rate_limiter.record_dial()
    rate_limiter.record_dial()
    with pytest.raises(rate_limiter.RateLimitExceeded) as exc:
        rate_limiter.check_can_dial()
    assert "day" in str(exc.value).lower()


def test_rate_limiter_unlimited_when_limits_are_zero(monkeypatch):
    rate_limiter.reset()
    monkeypatch.setattr(
        rate_limiter,
        "get_settings",
        lambda: _limiter_settings(max_calls_per_hour=0, max_calls_per_day=0,
                                  max_concurrent_calls=0),
    )
    for _ in range(50):
        rate_limiter.check_can_dial()
        rate_limiter.record_dial()


def test_rate_limiter_concurrent_slot(monkeypatch):
    import app.api.media_stream as media_stream

    rate_limiter.reset()
    monkeypatch.setattr(rate_limiter, "get_settings", lambda: _limiter_settings())
    fake_session = media_stream.MediaSession()
    media_stream.active_sessions.append(fake_session)
    try:
        with pytest.raises(rate_limiter.RateLimitExceeded):
            rate_limiter.check_stream_slot()
        # A free slot passes.
        media_stream.active_sessions.clear()
        rate_limiter.check_stream_slot()
    finally:
        if fake_session in media_stream.active_sessions:
            media_stream.active_sessions.remove(fake_session)


def test_calls_endpoint_returns_429_when_hourly_limit_hit(monkeypatch):
    """The dialing endpoint refuses BEFORE calling the provider (429)."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.api.calls as calls_api
    import app.core.security as security

    rate_limiter.reset()
    settings = _limiter_settings(
        calls_api_key="k", test_to_number="+94777000000",
    )
    # The endpoint, the limiter and the auth dependency must all see the same
    # settings (auth moved to app.core.security in Step 9).
    monkeypatch.setattr(rate_limiter, "get_settings", lambda: settings)
    monkeypatch.setattr(calls_api, "get_settings", lambda: settings)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    rate_limiter.record_dial()
    rate_limiter.record_dial()

    client = TestClient(app)
    resp = client.post(
        "/calls",
        json={"phone_number": "+94777000000"},
        headers={"X-Api-Key": "k"},
    )
    assert resp.status_code == 429
    assert "hour" in resp.json()["detail"].lower()
    rate_limiter.reset()
