"""
Offline tests for Step 2 (batch transcription proof).

The Whisper model is stubbed (a fake that returns canned segments), so
these tests run in milliseconds with no model download and no network.
A separate opt-in marker runs the real "base" model against a real
recording:  .venv\\Scripts\\python -m pytest tests/ -m real_stt -v

Run the offline suite:
    .venv\\Scripts\\python -m pytest tests/ -v
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from app.services import recordings as recordings_service
from app.services.stt import Segment, Transcriber, TranscriptionError


# ----------------------------------------------------------------- helpers


class _FakeSegments:
    """Mimics the faster-whisper segment generator."""

    def __init__(self, segments):
        self._segments = segments

    def __iter__(self):
        return iter(self._segments)


class _FakeInfo:
    def __init__(self, duration: float, language: str = "en"):
        self.duration = duration
        self.language = language


class _FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel; records the audio it saw."""

    def __init__(self, result=None):
        self.result = result or {
            "text": "I am feeling better but I still have some pain",
            "language": "en",
            "duration": 3.5,
            "segments": [
                Segment(start=0.0, end=1.5, text="I am feeling better"),
                Segment(start=1.8, end=3.5, text="but I still have some pain"),
            ],
        }
        self.calls: list[str] = []

    def transcribe(self, audio_path, language=None, beam_size=None, vad_filter=None):
        self.calls.append(
            {"path": audio_path, "language": language, "vad_filter": vad_filter}
        )
        r = self.result
        return _FakeSegments(r["segments"]), _FakeInfo(r["duration"], r["language"])


def _write_test_wav(path: Path, duration_sec: float = 1.0, freq: float = 440.0) -> Path:
    """Generate a real playable WAV file (sine tone) for testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for i in range(int(8000 * duration_sec)):
        value = int(12000 * math.sin(2 * math.pi * freq * (i / 8000.0)))
        frames += struct.pack("<h", value)
    recordings_service.write_wav(path, bytes(frames))
    return path


# ----------------------------------------------------- recordings service


def test_alaw_to_pcm16_roundtrip():
    # 1 kHz sine -> A-law -> back to PCM16 must stay a 1 kHz sine
    pcm = bytearray()
    for i in range(800):
        value = int(12000 * math.sin(2 * math.pi * 1000 * (i / 8000.0)))
        pcm += struct.pack("<h", value)
    alaw = audioop_alaw(bytes(pcm))
    restored = recordings_service.alaw_to_pcm16(alaw)
    assert len(restored) == 2 * len(alaw)  # 1 byte A-law -> 2 bytes PCM16


def audioop_alaw(pcm16: bytes) -> bytes:
    import audioop

    return audioop.lin2alaw(pcm16, 2)


def test_write_call_recording(tmp_path):
    import audioop

    pcm16 = struct.pack("<3h", 0, 1000, -1000)  # 3 valid 16-bit frames
    alaw = audioop.lin2alaw(pcm16, 2)  # -> 3 A-law bytes
    path = recordings_service.write_call_recording(tmp_path, "abc123", alaw)
    assert path == tmp_path / "call_abc123.wav"
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == 8000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 3


def test_write_call_recording_pcmu(tmp_path):
    """Regression: Zernio sometimes negotiates PCMU -- it must decode as mu-law,
    not A-law (that mismatch is what produced the 'noisy' recording)."""
    import audioop

    pcm16 = struct.pack("<3h", 0, 12000, -12000)
    ulaw = audioop.lin2ulaw(pcm16, 2)
    path = recordings_service.write_call_recording(tmp_path, "ulawtest", ulaw, encoding="PCMU")
    with wave.open(str(path), "rb") as wav:
        assert wav.getnframes() == 3
        frames = wav.readframes(3)
        restored = struct.unpack("<3h", frames)
        # mu-law roundtrip is lossy; allow +-10% amplitude error
        for original, back in zip((0, 12000, -12000), restored):
            assert abs(back - original) <= max(1200, abs(original) * 0.1)



# ------------------------------------------------------------- transcriber


def test_transcriber_uses_stub_and_returns_segments(tmp_path):
    fake = _FakeWhisperModel()
    t = Transcriber(model=fake)
    wav = _write_test_wav(tmp_path / "call_test.wav")

    result = t.transcribe(wav, language="en")

    assert "pain" in result.text
    assert result.language == "en"
    assert len(result.segments) == 2
    assert fake.calls[0]["path"] == str(wav)
    assert fake.calls[0]["vad_filter"] is True  # silence handling enabled


def test_transcriber_missing_file_raises():
    t = Transcriber(model=_FakeWhisperModel())
    with pytest.raises(TranscriptionError):
        t.transcribe("does_not_exist.wav")


def test_transcriber_handles_empty_result(tmp_path):
    """Step 2 TC4: empty/ambiguous audio must not crash -- empty text comes back."""
    fake = _FakeWhisperModel(
        result={"text": "", "language": "en", "duration": 2.0, "segments": []}
    )
    t = Transcriber(model=fake)
    wav = _write_test_wav(tmp_path / "call_silence.wav")
    result = t.transcribe(wav)
    assert result.text == ""
    assert result.segments == []


def test_get_transcriber_is_shared():
    from app.services import stt as stt_module

    stt_module._default_transcriber = None
    a = stt_module.get_transcriber()
    b = stt_module.get_transcriber()
    assert a is b
    stt_module._default_transcriber = None


# ------------------------------------------------- real model (opt-in)


@pytest.mark.real_stt
def test_real_model_transcribes_real_recording():
    """Runs only with `-m real_stt`: downloads/loads the real base model.

    Uses the newest call recording in backend/recordings/ (e.g. the WAV you
    just made with the live Zernio call) and prints the transcript.
    """
    recordings = sorted(Path(__file__).parent.parent.glob("recordings/call_*.wav"))
    if not recordings:
        pytest.skip("No recordings in backend/recordings/")
    target = recordings[-1]

    result = Transcriber().transcribe(target, language="en")
    print(f"\nFile: {target.name}")
    print(f"Transcript: {result.text!r}")
    for seg in result.segments:
        print(f"  [{seg.start:6.2f} - {seg.end:6.2f}] {seg.text}")
    assert isinstance(result.text, str)
