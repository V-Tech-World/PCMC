"""
Offline tests for Step 3 (turn-based structured dialogue).

No model load and no real TTS in the dialogue/turn tests (pure logic).
One optional test exercises the real offline TTS backend when available.
"""

from __future__ import annotations

import math
import struct

import pytest

from app.core.config import get_settings
from app.services import call_flow
from app.services.dialogue import (
    FINAL_QUESTION_ID,
    CallDialogue,
    build_script,
    extract_choice,
    interpret_yes_no,
)
from app.services.turn_detector import TurnConfig, TurnDetector


# ------------------------------------------------------------- script shape


def test_script_order_general():
    script = build_script("general")
    ids = [q.id for q in script]
    # pain_severity is a CONDITIONAL follow-up, not part of the main flow
    assert ids == ["medication", "pain", "category_general", "anything_else"]
    by_id = {q.id: q for q in script}
    assert by_id["anything_else"].kind == "open"
    assert by_id["pain"].kind == "yes_no"
    assert by_id["pain"].follow_up_id == "pain_severity"


def test_script_varies_only_category_question():
    general = {q.id: q.text for q in build_script("general")}
    surgical = {q.id: q.text for q in build_script("surgical")}
    cardiac = {q.id: q.text for q in build_script("cardiac")}
    for qid in ("medication", "pain", "anything_else"):
        assert general[qid] == surgical[qid] == cardiac[qid]
    assert general["category_general"] != surgical["category_surgical"]
    assert "wound" in surgical["category_surgical"].lower()
    assert "breathlessness" in cardiac["category_cardiac"].lower()



def test_new_discharge_types_have_their_own_question():
    """Respiratory + diabetic (added 29 Sep 2026) behave like every other type:
    identical core flow, exactly one type-specific question, and a matching
    question id that the risk scorer keys off ('category_<type>')."""
    from app.services.dialogue import CATEGORIES

    general = {q.id: q.text for q in build_script("general")}
    respiratory = {q.id: q.text for q in build_script("respiratory")}
    diabetic = {q.id: q.text for q in build_script("diabetic")}

    for qid in ("medication", "pain", "anything_else"):
        assert general[qid] == respiratory[qid] == diabetic[qid]
    assert "wheezing" in respiratory["category_respiratory"].lower()
    assert "foot" in diabetic["category_diabetic"].lower()

    for category in CATEGORIES:
        ids = {q.id for q in build_script(category)}
        assert ids == {"medication", "pain", f"category_{category}",
                       FINAL_QUESTION_ID}


def test_diabetic_flow_asks_the_category_question_and_grades_the_answer():
    """A full diabetic call: four questions, one answer each, scored."""
    dialogue = CallDialogue("diabetic")
    dialogue.start()
    asked = []
    for answer in ("yes I took them", "no pain", "yes my foot is sore", "no"):
        asked.append(dialogue.current_question.id)
        dialogue.record_answer(answer)
    assert asked == ["medication", "pain", "category_diabetic",
                     FINAL_QUESTION_ID]
    assert dialogue.is_complete
    assert dialogue.answers["category_diabetic"].interpretation is True
    assert dialogue.assess_risk().risk_level in {"medium", "high"}


def test_unknown_category_rejected():
    with pytest.raises(ValueError):
        build_script("dentistry")
    with pytest.raises(ValueError):
        CallDialogue(diagnosis_category="dentistry")

# ------------------------------------------------- state machine behavior


def test_full_call_no_repeats_or_skips():
    """Step 3 TC3/TC4: every question asked exactly once, in order; the
    severity follow-up appears exactly once because pain = yes."""
    d = CallDialogue("surgical")
    q = d.start()
    asked = [q.id]
    answers = iter(["yes", "yes", "severe", "no", "nothing else"])
    #              meds    pain   sev      cat   open
    while q is not None:
        q = d.record_answer(next(answers))
        if q is not None:
            asked.append(q.id)
    assert asked == ["medication", "pain", "pain_severity", "category_surgical", "anything_else"]
    assert d.is_complete
    assert set(d.answers) == set(asked)



def test_pain_follow_up_only_when_yes():
    d = CallDialogue("general")
    d.start()
    q = d.record_answer("no I forgot today")     # meds "no" -> straight to pain
    assert q.id == "pain"
    q = d.record_answer("no pain at all")        # pain "no" -> severity SKIPPED
    assert q.id == "category_general"

    d2 = CallDialogue("general")
    d2.start()
    d2.record_answer("yes")
    q = d2.record_answer("yes, a lot")           # pain "yes" -> severity asked
    assert q.id == "pain_severity"
    assert d2.answers["pain"].interpretation is True


def test_no_pain_is_not_yes():
    """Negated phrasing must not be read as yes (feeds the severity slot)."""
    assert interpret_yes_no("no pain") is False
    assert interpret_yes_no("not really") is False
    assert interpret_yes_no("yes") is True
    assert interpret_yes_no("") is None
    assert interpret_yes_no("banana") is None


def test_choice_extraction():
    assert extract_choice("it is severe doctor", ("mild", "moderate", "severe")) == "severe"
    assert extract_choice("mild only", ("mild", "moderate", "severe")) == "mild"
    assert extract_choice("uhh", ("mild", "moderate", "severe")) is None


def test_restart_resets_state():
    d = CallDialogue("general")
    first = d.start()
    d.record_answer("yes")
    again = d.start()
    assert again.id == first.id == "medication"
    assert d.current_question.id == "medication"
    assert not d.is_complete

# ----------------------------------------------------------- turn detector

_PCM_LOUD = struct.pack("<160h", *([9000] * 160))   # one 20 ms loud frame
_PCM_QUIET = struct.pack("<160h", *([0] * 160))     # one 20 ms silent frame


def _feed_frames(detector: TurnDetector, frames, pcm):
    for _ in range(frames):
        detector.feed(pcm)


def test_turn_detector_silence_ends_turn():
    cfg = TurnConfig(silence_duration_sec=0.4, max_turn_sec=10.0)
    d = TurnDetector(cfg)
    _feed_frames(d, 20, _PCM_LOUD)   # ~0.4s of speech
    assert d.has_speech
    assert d.state != "ended"
    _feed_frames(d, 30, _PCM_QUIET)  # ~0.6s of silence > 0.4s
    assert d.state == "ended"


def test_turn_detector_caps_max_duration():
    cfg = TurnConfig(silence_duration_sec=1.2, max_turn_sec=0.6)
    d = TurnDetector(cfg)
    _feed_frames(d, 40, _PCM_LOUD)   # 0.8s continuous speech > cap
    assert d.state == "ended"


def test_turn_detector_silence_only_has_no_speech():
    cfg = TurnConfig(silence_duration_sec=0.2, max_turn_sec=0.6)
    d = TurnDetector(cfg)
    _feed_frames(d, 40, _PCM_QUIET)
    assert d.state == "ended"
    assert d.has_speech is False


# ------------------------------------------------------------------- TTS


def test_tts_synthesize_8k_mono_if_backend_available():
    """Runs the real offline backend (pyttsx3 on Windows); skipped if absent."""
    try:
        from app.services.tts import Speaker

        speaker = Speaker(backend="pyttsx3")
    except Exception:  # pragma: no cover - depends on environment
        pytest.skip("pyttsx3 backend unavailable in this environment")

    import audioop

    pcm = speaker.synthesize("Yes or no?")
    assert len(pcm) > 1000
    assert len(pcm) % 2 == 0  # 16-bit samples
    rms = audioop.rms(pcm, 2)
    assert 100 < rms < 30000  # audible but not clipping
    assert speaker.filler() == speaker.filler()  # cached


# ------------------------------------------------ call audio (TTS -> wire)


def test_resample_to_8k_length():
    """1 s of 22 050 Hz audio must become ~1 s of 8 kHz audio."""
    from app.services.tts import _resample_to_8k

    rate = 22050
    pcm = struct.pack(
        f"<{rate}h",
        *[int(12000 * math.sin(2 * math.pi * 1000 * (i / rate))) for i in range(rate)],
    )
    out = _resample_to_8k(pcm, rate)
    assert abs(len(out) // 2 - 8000) <= 40  # within 5 ms


def test_resample_antialias_suppresses_out_of_band():
    """Regression for the 'underwater' voice: a 7 kHz tone (outside the 8 kHz
    Nyquist band) must be FILTERED OUT, not aliased into the voice band.
    The old naive ratecv resampler left a loud ~1 kHz alias behind."""
    try:
        import numpy  # noqa: F401
        import scipy.signal  # noqa: F401
    except ImportError:
        pytest.skip("scipy not installed")
    from app.services.tts import _resample_to_8k

    import audioop

    rate = 22050
    tone = [int(12000 * math.sin(2 * math.pi * 7000 * (i / rate))) for i in range(rate)]
    pcm = struct.pack(f"<{rate}h", *tone)
    out = _resample_to_8k(pcm, rate)
    assert audioop.rms(out, 2) < 0.1 * 12000  # near-silence, not a loud alias


def test_resample_peak_limits():
    """Codec encoding must never clip: peaks are limited to 31 000."""
    import audioop

    from app.services.tts import _resample_to_8k

    pcm = struct.pack("<1000h", *([32000] * 1000))
    out = _resample_to_8k(pcm, 8000)  # same rate -> only the peak limit applies
    assert audioop.max(out, 2) <= 31000


# ------------------------------------------------ hangup escape (send path)


class _EndedSession:
    speaking = False
    ended = True
    encoding = "PCMA"


class _LiveSession:
    def __init__(self):
        self.speaking = False
        self.ended = False
        self.encoding = "PCMA"
        self.settle_until = 0.0
        self.agent_pcm = bytearray()  # mirrors MediaSession: audio we transmitted


class _DummyWS:
    def __init__(self, fail=False):
        self.sent = 0
        self.messages: list[dict] = []  # raw frames, for codec assertions
        self.fail = fail

    async def send_json(self, msg):
        if self.fail:
            raise RuntimeError("Cannot call 'send' once a close message has been sent.")
        self.sent += 1
        self.messages.append(msg)


class _FixedSpeaker:
    """Stand-in speaker: one frame of 1 kHz audio, no TTS engine involved."""

    def synthesize(self, text: str) -> bytes:
        return _sine_pcm16(1000, seconds=0.04)

    def filler(self) -> bytes:
        return _sine_pcm16(1000, seconds=0.04)


def test_send_pcm_aborts_immediately_when_stream_ended():
    import asyncio

    ws = _DummyWS()
    with pytest.raises(call_flow.CallEnded):
        asyncio.run(
            call_flow.send_pcm(ws, b"\x00" * 3200, "PCMA", _EndedSession(), pace=False)
        )
    assert ws.sent == 0  # nothing was pushed to a dead call


def test_send_pcm_maps_dead_socket_to_call_ended():
    import asyncio

    with pytest.raises(call_flow.CallEnded):
        asyncio.run(
            call_flow.send_pcm(
                _DummyWS(fail=True), b"\x00" * 320, "PCMA", _LiveSession(), pace=False
            )
        )


def test_send_pcm_encodes_frames_and_resets_flags():
    import asyncio
    import base64 as b64

    session = _LiveSession()
    ws = _DummyWS()
    pcm = struct.pack("<1600h", *([800] * 1600))  # 10 frames of 20 ms
    asyncio.run(call_flow.send_pcm(ws, pcm, "PCMA", session, pace=False))
    assert len(ws.sent and [1] * ws.sent) == 10  # one frame per 20 ms chunk
    assert session.speaking is False
    assert session.settle_until > 0  # echo-settle window armed after speaking


def test_send_pcm_records_transmitted_audio():
    """The agent's audio must be mirrored to the session so a bad-sounding call
    can be diagnosed offline (recordings/call_<id>/agent_audio.wav)."""
    import asyncio

    session = _LiveSession()
    pcm = struct.pack("<1600h", *([800] * 1600))
    asyncio.run(call_flow.send_pcm(_DummyWS(), pcm, "PCMU", session, pace=False))
    assert bytes(session.agent_pcm) == pcm  # byte-for-byte what went on the wire


def test_media_session_saves_agent_recording(tmp_path):
    """MediaSession.save_agent_recording writes the patient-facing audio so
    "our audio is bad" vs "the call path distorted it" is decidable."""
    from app.api.media_stream import MediaSession

    session = MediaSession()
    session.call_id = "TESTCALL1"
    session.agent_pcm.extend(struct.pack("<800h", *([500] * 800)))
    path = session.save_agent_recording(tmp_path)
    assert path is not None and path.name == "agent_audio.wav"
    assert path.parent.name == "call_TESTCALL1"
    import wave

    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == 8000
        assert wav.getnchannels() == 1
        assert wav.getnframes() == 800


# ------------------------------------------------ call_flow helpers (pure)


def test_pcm_to_codec_roundtrip():
    from app.services.recordings import decode_chunk, pcm16_to_codec

    pcm = struct.pack("<160h", *[int(8000 * math.sin(i * 0.1)) for i in range(160)])
    for codec in ("PCMA", "PCMU"):
        compressed = pcm16_to_codec(pcm, codec)
        assert len(compressed) == 160  # 1 byte per sample
        restored = decode_chunk(codec, compressed)
        assert len(restored) == 320


def test_call_ended_propagates():
    assert issubclass(call_flow.CallEnded, RuntimeError)


# ------------------------------------------- outbound codec (live-call fix)


class _CodecSession(_LiveSession):
    encoding = "PCMA"  # inbound codec, as seen in the provider's start event


class _CodecSettings:
    speak_codec = "pcmu"


def test_tx_codec_defaults_to_pcmu_not_inbound_codec():
    """The provider documents PCMU (G.711 u-law) as its bidirectional media
    codec, while the `start` event describes the INBOUND track. Encoding our
    questions with the inbound PCMA here is what distorted them live."""
    assert call_flow.resolve_tx_codec(_CodecSession(), _CodecSettings()) == "PCMU"


def test_tx_codec_honours_overrides():
    settings = _CodecSettings()
    settings.speak_codec = "pcma"
    assert call_flow.resolve_tx_codec(_CodecSession(), settings) == "PCMA"
    settings.speak_codec = "PCMU"
    assert call_flow.resolve_tx_codec(_CodecSession(), settings) == "PCMU"
    # Unknown values must fall back to the provider's codec, never crash a call.
    settings.speak_codec = "bogus"
    assert call_flow.resolve_tx_codec(_CodecSession(), settings) == "PCMU"


def test_tx_codec_auto_mirrors_inbound_for_ab_testing():
    settings = _CodecSettings()
    settings.speak_codec = "auto"
    assert call_flow.resolve_tx_codec(_CodecSession(), settings) == "PCMA"


# ---------------------------------------- telephony shaping (call clarity)


def _sine_pcm16(freq_hz: int, seconds: float = 0.5, amp: int = 9000) -> bytes:
    rate = 8000
    count = int(rate * seconds)
    return struct.pack(
        f"<{count}h",
        *[int(amp * math.sin(2 * math.pi * freq_hz * (i / rate))) for i in range(count)],
    )


def test_telephony_chain_removes_sub_300hz_rumble():
    """Regression for the muddy 'off-station radio' voice: energy the phone
    line cannot carry (below 300 Hz) must be filtered out before encoding."""
    import audioop

    from app.services.tts import apply_telephony_chain

    rumble = _sine_pcm16(80)
    assert audioop.rms(rumble, 2) > 6000  # input is loud
    out = apply_telephony_chain(rumble)
    assert audioop.rms(out, 2) < 0.1 * audioop.rms(rumble, 2)


def test_telephony_chain_keeps_the_voice_band():
    """The speech band (300-3400 Hz) must survive: a 1 kHz tone stays audible."""
    import audioop

    from app.services.tts import apply_telephony_chain

    tone = _sine_pcm16(1000)
    out = apply_telephony_chain(tone)
    assert audioop.rms(out, 2) > 0.4 * audioop.rms(tone, 2)


def test_telephony_chain_never_clips_and_matches_level():
    """Level match targets ~-20 dBFS and hard-ceilings peaks below 24 000 so
    G.711 encoding never clips (harsh distortion on a handset speaker)."""
    import audioop

    from app.services.tts import apply_telephony_chain

    quiet = apply_telephony_chain(_sine_pcm16(1000, amp=300))
    hot = apply_telephony_chain(_sine_pcm16(1000, amp=32000))
    for pcm in (quiet, hot):
        assert audioop.max(pcm, 2) <= 24000  # hard peak ceiling held
        assert 1500 < audioop.rms(pcm, 2) < 6000  # audible, not blown out
    # A hot input must be brought down, a quiet one brought up.
    assert audioop.rms(hot, 2) < audioop.rms(_sine_pcm16(1000, amp=32000), 2)
    assert audioop.rms(quiet, 2) > 300


def test_yes_no_instruction_leads_the_question():
    """Live feedback fix: the instruction must come BEFORE the question.

    When "Please say yes or no" trailed the question, the patient started
    answering while the instruction was still playing -- confusing, and it
    felt like they had answered too early.
    """
    for question in build_script("surgical"):
        lowered = question.text.lower()
        if question.kind == "yes_no":
            assert lowered.startswith("please answer yes or no."), question.id
        # No trailing instruction anywhere: it would be spoken over the answer.
        assert "please say yes or no" not in lowered, question.id
        assert not lowered.endswith("please say yes or no."), question.id


def test_severity_options_are_inside_the_question():
    """The follow-up must invite the choice, not command it after the fact."""
    from app.services.dialogue import _follow_up_questions

    severity = _follow_up_questions()["pain_severity"]
    lowered = severity.text.lower()
    assert severity.text.endswith("Mild, moderate, or severe?")
    assert "mild" in lowered and "moderate" in lowered and "severe" in lowered
    assert "please say" not in lowered
    assert severity.choices == ("mild", "moderate", "severe")


def test_open_question_is_not_mistaken_for_yes_no():
    """'Lastly, ...' read like the previous yes/no pattern; the open question
    must be clearly an invitation instead."""
    open_q = {q.id: q for q in build_script("general")}["anything_else"]
    assert open_q.kind == "open"
    assert not open_q.text.lower().startswith("lastly")
    assert "anything else" in open_q.text.lower()
    assert "yes or no" not in open_q.text.lower()


def test_speaker_bandpass_toggle_and_config_defaults():
    """The telephony chain is on by default and can be switched off for A/B."""
    from app.services.tts import Speaker

    settings = get_settings()
    assert settings.tts_bandpass_enabled is True
    assert settings.speak_codec.lower() == "pcmu"  # provider's bidirectional codec
    assert settings.filler_enabled is True         # covers the STT/TTS gap

    try:
        plain = Speaker(backend="pyttsx3", bandpass=False)
    except Exception:  # pragma: no cover - depends on environment
        pytest.skip("pyttsx3 backend unavailable in this environment")
    shaped = Speaker(backend="pyttsx3", bandpass=True)
    assert plain.bandpass is False and shaped.bandpass is True


# ---------------------------------------- outbound codec (call clarity)


def test_speak_encodes_with_the_tx_codec_not_the_inbound_codec():
    """`speak()` must encode with the resolved outbound codec.

    Live bug: questions were encoded with the inbound track's codec (PCMA on
    that call) while the provider decodes the bidirectional track as PCMU --
    the patient heard pure distortion. This locks the wiring in place.
    """
    import asyncio
    import base64

    from app.services.recordings import pcm16_to_codec

    session = _CodecSession()                     # inbound track is PCMA
    ws = _DummyWS()
    asyncio.run(call_flow.speak(ws, "hello", session, _FixedSpeaker(), "PCMU"))
    assert ws.messages, "speak() sent nothing"
    payload = base64.b64decode(ws.messages[0]["media"]["payload"])
    expected_pcm = _FixedSpeaker().synthesize("hello")[:320]  # first 20 ms frame
    assert payload == pcm16_to_codec(expected_pcm, "PCMU")
    assert payload != pcm16_to_codec(expected_pcm, "PCMA")


def test_send_pcm_buffers_the_audio_the_patient_heard():
    """send_pcm must buffer exactly what we transmitted, so a bad-sounding call
    can be diagnosed offline from recordings/call_<id>/agent_audio.wav."""
    import asyncio

    session = _LiveSession()
    assert isinstance(session.agent_pcm, bytearray)
    pcm = struct.pack("<1600h", *([500] * 1600))
    asyncio.run(call_flow.send_pcm(_DummyWS(), pcm, "PCMU", session, pace=False))
    assert bytes(session.agent_pcm) == pcm


def test_agent_audio_recording_is_saved_next_to_turns(tmp_path):
    """The transmitted audio must land in recordings/call_<id>/agent_audio.wav
    so it can be compared against the live call (our chain vs the call path)."""
    import wave

    from app.api.media_stream import MediaSession

    session = MediaSession(call_id="abc123")
    assert session.save_agent_recording(tmp_path) is None  # nothing sent yet

    session.agent_pcm.extend(_sine_pcm16(1000, seconds=0.1))
    path = session.save_agent_recording(tmp_path)
    assert path == tmp_path / "call_abc123" / "agent_audio.wav"
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == 8000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 800


# --------------------------------------------- pacing timer (real-time audio)


def test_fine_timer_is_reference_counted():
    """acquire/release must nest safely (startup + tests + reloads)."""
    from app.core import timing

    before = timing._depth
    assert timing.acquire_fine_timer() == timing._IS_WINDOWS
    assert timing._depth == before + 1
    timing.acquire_fine_timer()
    assert timing._depth == before + 2
    timing.release_fine_timer()
    timing.release_fine_timer()
    assert timing._depth == before  # balanced pairs leave no residue
    timing.release_fine_timer()  # extra release must be harmless
    assert timing._depth == before


def test_sleep_resolution_note_reflects_timer_state():
    from app.core import timing

    if not timing._IS_WINDOWS:
        assert timing.sleep_resolution_warning() == "native"
        return
    timing.acquire_fine_timer()
    try:
        assert timing.sleep_resolution_warning().startswith("fine")
    finally:
        timing.release_fine_timer()


def test_sleep_is_accurate_with_the_fine_timer():
    """Regression for the starved jitter buffer: 20 ms frames must pace at
    20 ms, not ~31 ms. Windows only -- other platforms sleep precisely."""
    import asyncio
    import time

    from app.core import timing

    if not timing._IS_WINDOWS:
        pytest.skip("Windows-only pacing fix")

    sample_ms = 20

    async def measure() -> float:
        best = 999.0
        for _ in range(5):
            started = time.monotonic()
            await asyncio.sleep(sample_ms / 1000)
            best = min(best, (time.monotonic() - started) * 1000)
        return best

    timing.acquire_fine_timer()
    try:
        # Wall-clock measurement: transient system load (builds, editor, ...)
        # can skew a whole attempt, so retry up to 3x. A genuine regression
        # (coarse ~31 ms timer) fails EVERY attempt and still fails below.
        slept_ms = 999.0
        for _attempt in range(3):
            slept_ms = asyncio.run(measure())
            if slept_ms < sample_ms * 1.3:
                break
    finally:
        timing.release_fine_timer()
    # Coarse resolution overshoots by ~55%; allow generous headroom for CI noise.
    assert slept_ms < sample_ms * 1.3, f"sleep was {slept_ms:.1f} ms for a 20 ms frame"


# --------------------------------------------------- config defaults (audio)


def test_audio_settings_defaults_are_the_fixed_ones():
    """The values that fixed live call distortion must stay the defaults."""
    from app.core.config import Settings

    settings = Settings(_env_file=None)
    assert settings.speak_codec == "pcmu"        # provider's bidirectional codec
    assert settings.tts_bandpass_enabled is True
    # 2 Oct 2026: filler flipped ON. Live calls left the patient in dead air
    # for ~1-2 s after answering ("did it hear me?"), and covering that gap
    # with a voice is worth more than the ~1 s the filler itself takes.
    assert settings.filler_enabled is True
    assert settings.max_silent_attempts == 2      # never fire the whole script
    assert settings.beep_enabled is True           # the question promises it
    assert settings.beep_frequency_hz == 1000.0
    assert settings.beep_duration_ms == 350.0
    # Alerts are SENT by default; 'ready' is the store-only fallback.
    assert Settings.model_fields["alert_delivery"].default == "whatsapp"
    # The conftest fixture pins TTS_BACKEND for determinism, so check the model
    # default itself rather than the ambient value.
    assert Settings.model_fields["tts_backend"].default == "auto"
    assert Settings.model_fields["tts_rate"].default == 160  # slower = clearer




def _tone(seconds: float, freq: float = 440.0, amp: int = 8000) -> bytes:
    import struct as _struct
    n = int(seconds * 8000)
    return b"".join(
        _struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / 8000)))
        for i in range(n)
    )


def test_trim_silence_removes_dead_air_but_keeps_the_speech():
    """SAPI pads the filler with ~130 ms of lead-in and ~940 ms of tail, so the
    patient sat through a second of nothing after 'please' (live 4 Oct 2026)."""
    from app.services.tts import trim_silence

    padded = b"\x00\x00" * int(0.2 * 8000) + _tone(1.0) + b"\x00\x00" * int(0.5 * 8000)
    trimmed = trim_silence(padded)

    assert len(trimmed) < len(padded) - int(0.4 * 8000 * 2)
    # The speech itself survives: energy is still there and the tone is intact.
    assert max(abs(v) for v in struct.unpack(f"<{len(trimmed)//2}h", trimmed)) > 5000
    assert len(trimmed) >= int(0.9 * 8000 * 2)


def test_filler_text_has_no_comma():
    """The comma is what SAPI stretches into a 0.63 s pause before 'please'."""
    from app.services.tts import FILLER_TEXT

    assert "," not in FILLER_TEXT
    assert FILLER_TEXT.strip().lower().endswith("please.")


def test_trim_silence_keeps_digital_silence_usable():
    from app.services.tts import trim_silence

    assert trim_silence(b"\x00\x00" * 4000)  # never returns empty
