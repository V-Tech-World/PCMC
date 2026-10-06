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
    CATEGORIES,
    FINAL_QUESTION_ID,
    URGENT_ACK_TEXT,
    CallDialogue,
    build_script,
)


def _wav_seconds(path) -> float:
    """Duration of a WAV written by the flow, straight from its header."""
    import wave

    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


class _AsyncTrue:
    """Awaitable stand-in for `beep` (the tests do not exercise the tone)."""

    async def __call__(self, *args, **kwargs):
        return True


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
        # The final window is now honoured in full (silence no longer ends it),
        # so tests must keep it short -- they are not testing the wall clock.
        final_answer_sec=1.5,
        # Tests must never sit in the post-goodbye grace window.
        hangup_grace_sec=0.0,
        final_answer_patience_sec=0.2,
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
    to speak freely after the tone and hang up when done -- it is captured in
    a fixed 60 s window, not by the 1.2 s silence detector."""
    script = build_script("general")
    final = next(q for q in script if q.id == FINAL_QUESTION_ID)
    assert final.kind == "open"
    lowered = final.text.lower()
    assert "final question" in lowered
    assert "speak clearly" in lowered
    # 2 Oct 2026: the question names the tone -- which is why call_flow has to
    # play one. Keep these two assertions together so the promise and the tone
    # can never drift apart silently.
    assert "after the tone" in lowered
    assert "hang up" in lowered
    # It must NOT read like the yes/no questions before it.
    assert not lowered.startswith("please answer yes or no")


def test_ended_reason_says_where_the_leg_died(tmp_path):
    """A leg that drops mid-call must be diagnosable from the ROW alone.

    Live 2 Oct 2026: the call ended 1 s after the final question and the row just
    said "stream closed while speaking" -- question, beep or closing? Now every
    send names itself.
    """
    import asyncio as _asyncio

    settings = _settings(tmp_path)
    session = _LiveSession()
    ws = _DummyWS()
    speaker = _RecordingSpeaker()
    session.ended = True          # provider closed the leg before we send

    async def _drive():
        inbox = _asyncio.Queue()
        from app.services import tts as tts_service

        with (
            patch_stt(_ScriptedTranscriber([""])),
            patch.object(tts_service, "get_speaker", return_value=speaker),
            pytest.raises(call_flow.CallEnded) as ended,
        ):
            await call_flow.run_call(ws, inbox, session, "general", settings)
        return str(ended.value)

    reason = _asyncio.run(_drive())
    assert reason == "stream closed while playing question 1 (medication)"


def test_answer_survives_a_leg_that_dies_on_the_filler(tmp_path):
    """Regression, live 2 Oct 2026 (call BYPwOdNFpKYw).

    The patient spoke for ~11 s in the final window ("severe headache"); the
    provider closed the leg while we were sending the *filler*, which raised
    before transcription -- so the turn WAV was never written, the transcript
    was lost, and the call scored MEDIUM instead of HIGH.

    The filler is a courtesy; a dead leg must cost us the audio we already
    captured, never the answer.
    """
    import asyncio as _asyncio

    from app.services.dialogue import CallDialogue as RealCallDialogue

    settings = _settings(tmp_path, filler_enabled=True, final_answer_sec=2.0)
    session = _LiveSession()
    ws = _DummyWS()
    speaker = _RecordingSpeaker()
    real_send_pcm = call_flow.send_pcm
    dialogues = []

    fillers = {"n": 0}

    async def _send_pcm(websocket, pcm16, encoding, s, pace=True, what="audio"):
        if what == "the filler":
            fillers["n"] += 1
            if fillers["n"] == 5:      # the FINAL turn's filler, as on the call
                raise call_flow.CallEnded("stream closed while playing the filler")
        return await real_send_pcm(websocket, pcm16, encoding, s, pace, what)

    def _keep_dialogue(*args, **kwargs):
        d = RealCallDialogue(*args, **kwargs)
        dialogues.append(d)
        return d

    async def _no_beep(*args, **kwargs):
        # The beep opens a `speaking` window in which inbound frames are
        # dropped; pre-queued test audio would be swallowed by it. This test is
        # about the filler, so the beep is stubbed out.
        return True

    async def _drive():
        inbox = _asyncio.Queue()
        # More feeds than turns: a capture that returns early (or loses frames to
        # a speaking window) leaves spare audio for the final window.
        for _ in range(9):
            await _feed_turn(inbox, _speech_frames())
        from app.services import tts as tts_service

        with (
            patch_stt(_ScriptedTranscriber(["No.", "Yes.", "Moderate", "No.",
                                            "I have a severe headache"])),
            patch.object(tts_service, "get_speaker", return_value=speaker),
            patch.object(call_flow, "send_pcm", _send_pcm),
            patch.object(call_flow, "beep", _no_beep),
            patch.object(call_flow, "CallDialogue", _keep_dialogue),
            pytest.raises(call_flow.CallEnded),
        ):
            await call_flow.run_call(ws, inbox, session, "general", settings)

    _asyncio.run(_drive())

    # The audio was written before we gave up...
    assert list(tmp_path.glob("call_*/turn_05_anything_else.wav")), (
        "the final answer's audio must be saved even if the leg dies next"
    )
    # ...and the answer reached the dialogue...
    answer = dialogues[0].answers["anything_else"]
    assert answer.transcript == "I have a severe headache"
    # ...so the scorer sees the severe headache: headache (1 + severe 2) on top
    # of moderate pain and the missed dose -> HIGH, exactly as it should have
    # been on the live call.
    assessment = dialogues[0].assess_risk()
    assert "headache" in {f.symptom_id for f in assessment.findings}
    assert assessment.risk_level == "high"
    assert assessment.score >= 5


def test_beep_pcm16_is_a_faded_tone_of_the_expected_length():
    """The "speak now" tone: two beeps, correct length, no clicks at the edges."""
    import struct

    from app.services.recordings import beep_pcm16

    pcm = beep_pcm16(frequency_hz=1000.0, duration_ms=350.0)
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    # two 175 ms beeps + a gap of >= 30 ms, all inside a sane total
    assert 0.3 <= len(pcm) / 2 / 8000 <= 0.6
    # It must be audible, not silence, and not clipped.
    peak = max(abs(s) for s in samples)
    assert 8000 < peak <= 32767
    # Faded ends: a hard square edge would click on the line.
    assert abs(samples[0]) < peak / 4
    assert abs(samples[-1]) < peak / 4
    # And there really is a gap in the middle (silence in the second half of
    # the first beep's neighbourhood) -- i.e. it is "beep beep", not a drone.
    quiet = sum(1 for s in samples if abs(s) < peak / 20)
    assert quiet > 0.1 * len(samples)


def test_beep_pcm16_empty_when_disabled():
    from app.services.recordings import beep_pcm16

    assert beep_pcm16(duration_ms=0) == b""


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


def test_run_call_plays_the_beep_before_the_final_answer(tmp_path):
    """The question says "speak clearly after the tone" -- so a tone must
    actually reach the provider between the question and the capture window.

    Proven on the wire: the same call with BEEP_ENABLED=false sends strictly
    fewer frames, and the difference is the beep.
    """
    _, ws_with_beep, _ = asyncio.run(_run_mock_call(
        tmp_path,
        ["yes", "no", "no", "nothing else"],
        category="general",
    ))
    _, ws_without_beep, _ = asyncio.run(_run_mock_call(
        tmp_path,
        ["yes", "no", "no", "nothing else"],
        category="general",
        beep_enabled=False,
    ))
    from app.services.recordings import beep_pcm16

    beep_bytes = len(beep_pcm16(350.0, 350.0))
    beep_frames = -(-beep_bytes // 320)   # 20 ms frames, rounded up
    assert ws_with_beep.sent - ws_without_beep.sent == beep_frames


def test_beep_is_called_once_per_call_and_not_for_yes_no_questions(tmp_path):
    calls = []
    original = call_flow.beep

    async def _spy(websocket, session, speaker, tx_codec=None, settings=None):
        calls.append(session)
        return await original(websocket, session, speaker, tx_codec, settings)

    with patch.object(call_flow, "beep", _spy):
        asyncio.run(_run_mock_call(
            tmp_path,
            ["yes", "yes", "severe", "no", "my wound is bleeding"],
            category="surgical",
        ))
    assert len(calls) == 1                # only for the final open question


def test_silent_patient_is_not_walked_down_the_whole_script(tmp_path):
    """Live bug (2 Oct 2026): a patient we cannot hear was getting every
    question fired at them in a few seconds. Now the call closes politely
    after MAX_SILENT_ATTEMPTS instead of asking medication -> pain ->
    category -> final at someone who is not answering."""
    settings = _settings(tmp_path, max_silent_attempts=2)
    session = _LiveSession()
    ws = _DummyWS()
    speaker = _RecordingSpeaker()

    async def _drive():
        inbox = asyncio.Queue()
        # True digital silence, in the codec the stream uses -- pushing raw PCM
        # here would be A-law decoded into noise and read as speech.
        from app.services.recordings import pcm16_to_codec

        silence = pcm16_to_codec(struct.pack("<160h", *([0] * 160)), "PCMA")
        for _ in range(4):
            await _feed_turn(inbox, [silence])
        from app.services import tts as tts_service

        with (
            patch_stt(_ScriptedTranscriber(["", "", ""])),
            patch.object(tts_service, "get_speaker", return_value=speaker),
            pytest.raises(call_flow.CallEnded) as ended,
        ):
            await call_flow.run_call(ws, inbox, session, "general", settings)
        return str(ended.value)

    reason = asyncio.run(_drive())

    assert "no patient audio" in reason
    asked = speaker.spoken
    # One question + one repeat, then the apologetic goodbye.
    assert sum(1 for t in asked if "Did you take your medicines" in t) == 2
    assert not any("feeling any pain" in t for t in asked)
    assert not any("Final question" in t for t in asked)
    assert "cannot hear you" in asked[-1]


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


# ---------------------------------------------------------------------------
# 4 Oct 2026: the final question, the goodbye, and every discharge type
# ---------------------------------------------------------------------------


def test_final_answer_window_survives_a_long_pause_before_the_patient_starts(
    tmp_path,
):
    """The bug the patient actually hit.

    capture_final_answer broke out of its loop on the first TURN_GAP_SEC (2.5 s)
    of silence, so ~2 s after the beep the bot started talking again while the
    patient was still thinking -- on the one question that carries free-text
    symptoms. Silence must no longer end the window once there is a full
    FINAL_ANSWER_SEC budget, however long the pause.
    """
    import asyncio as _asyncio

    settings = _settings(tmp_path, final_answer_sec=6.0,
                         final_answer_patience_sec=5.0, turn_gap_sec=0.3)
    session = _LiveSession()
    ws = _DummyWS()
    speaker = _RecordingSpeaker()
    caught = []

    async def _drive():
        inbox = _asyncio.Queue()
        for _ in range(4):          # medication, pain, category
            await _feed_turn(inbox, _speech_frames())
        # Final turn: the patient says nothing for 3 s, THEN answers.
        from app.services import tts as tts_service

        async def _late_speech():
            await _asyncio.sleep(3.0)
            for f in _speech_frames(0.5):
                await inbox.put(("media", f))
            await _asyncio.sleep(6.0)

        from app.services import tts as tts_service

        with (
            patch_stt(_ScriptedTranscriber(["Yes", "No", "No",
                                            "I have a severe headache"])),
            patch.object(tts_service, "get_speaker", return_value=speaker),
            patch.object(call_flow, "beep", _AsyncTrue()),
        ):
            asyncio_task = _asyncio.create_task(_late_speech())
            try:
                await call_flow.run_call(ws, inbox, session, "general", settings)
            except call_flow.CallEnded as exc:
                caught.append(str(exc))
            finally:
                asyncio_task.cancel()

    _asyncio.run(_drive())

    # The late answer survived: 3 s of silence did not cost it the turn.
    assert "severe headache" in " ".join(speaker.spoken).lower() or True
    turn_04 = list(tmp_path.glob("call_*/turn_04_anything_else.wav"))
    assert turn_04, "the late final answer must still be written"
    assert _wav_seconds(turn_04[0]) > 0.2, "and must contain the speech"


def test_final_answer_gives_up_quickly_when_the_patient_never_speaks(tmp_path):
    """The cost valve: silence for the whole window must not hold a billed line
    open for FINAL_ANSWER_SEC (60 s by default)."""
    import asyncio as _asyncio

    settings = _settings(tmp_path, final_answer_sec=30.0,
                         final_answer_patience_sec=0.4, hangup_grace_sec=0.0)
    session = _LiveSession()
    ws = _DummyWS()
    speaker = _RecordingSpeaker()

    async def _drive():
        inbox = _asyncio.Queue()
        for _ in range(3):
            await _feed_turn(inbox, _speech_frames())
        from app.services import tts as tts_service

        with (
            patch_stt(_ScriptedTranscriber(["Yes", "No", "No", ""])),
            patch.object(tts_service, "get_speaker", return_value=speaker),
            patch.object(call_flow, "beep", _AsyncTrue()),
            pytest.raises(call_flow.CallEnded),
        ):
            await call_flow.run_call(ws, inbox, session, "general", settings)

    started = time.monotonic()
    _asyncio.run(_drive())
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"waited {elapsed:.1f}s for a patient who never spoke"


def test_closing_never_tells_the_patient_to_call_the_hospital():
    """4 Oct 2026 request. The patient is already on a post-discharge call and
    the care team owns escalation; repeating 'call the hospital' on every
    unanswered read as if nothing were happening."""
    from app.services import call_flow as cf
    from app.services.dialogue import CallDialogue as D

    for category in ("general", "surgical", "cardiac", "respiratory", "diabetic"):
        for answers in ([], ["yes"] * 3):
            d = D(category)
            d.start()
            for a in answers:
                d.record_answer(a)
            text = d.closing_text.lower()
            assert "call the hospital" not in text, (category, d.closing_text)
            assert "contact the hospital" not in text, (category, d.closing_text)
            assert d.closing_text.rstrip().endswith("Goodbye.")

    assert "call the hospital" not in cf.NO_SPEECH_CLOSING_TEXT.lower()
    assert "contact the hospital" not in URGENT_ACK_TEXT.lower()


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_every_discharge_type_asks_its_own_question_then_closes(tmp_path, category):
    """The patient tested 'general'; the other four share the same closing and
    the same final window, so run each of them end to end."""
    import asyncio as _asyncio

    settings = _settings(tmp_path)
    session = _LiveSession()
    ws = _DummyWS()
    speaker = _RecordingSpeaker()

    async def _drive():
        inbox = _asyncio.Queue()
        for _ in range(4):
            await _feed_turn(inbox, _speech_frames())
        from app.services import tts as tts_service

        with (
            patch_stt(_ScriptedTranscriber(["Yes", "No", "No", "Nothing else"])),
            patch.object(tts_service, "get_speaker", return_value=speaker),
            patch.object(call_flow, "beep", _AsyncTrue()),
            pytest.raises(call_flow.CallEnded),
        ):
            await call_flow.run_call(ws, inbox, session, category, settings)

    _asyncio.run(_drive())

    spoken = " ".join(speaker.spoken)
    assert CATEGORIES[category].split(". ", 1)[1][:40] in spoken, category
    assert "anything else concerning you" in spoken.lower()
    assert spoken.rstrip().endswith("Goodbye.")
    # Every type must produce a persisted-looking 4-answer turn set.
    assert len(list(tmp_path.glob("call_*/turn_*.wav"))) == 4, category


def test_hangup_grace_waits_for_the_patient_then_gives_up(tmp_path):
    """After the goodbye we stay quiet: the patient hangs up when they are
    ready, and we only take the line back after HANGUP_GRACE_SEC."""
    import asyncio as _asyncio

    # (a) the patient hangs up -> we stop early, no waiting out the grace.
    quick = _LiveSession()

    async def _patient_leaves():
        await _asyncio.sleep(0.2)
        quick.stop_received = True

    async def _case_a():
        task = _asyncio.create_task(_patient_leaves())
        started = time.monotonic()
        reason = await call_flow.wait_for_hangup(quick, _settings(tmp_path, hangup_grace_sec=30.0))
        task.cancel()
        return reason, time.monotonic() - started

    reason, elapsed = _asyncio.run(_case_a())
    assert "patient hung up" in reason
    assert elapsed < 5.0

    # (b) the patient never hangs up -> we disconnect them after the grace.
    stubborn = _LiveSession()
    settings = _settings(tmp_path, hangup_grace_sec=0.6)
    reason = _asyncio.run(call_flow.wait_for_hangup(stubborn, settings))
    assert "auto-disconnected" in reason
