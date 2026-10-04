"""
Recording helpers: converting and writing call audio.

The telephony stream arrives as raw A-law (PCMA) bytes at 8 kHz. For
playback and transcription (Step 2) we convert it once to 16-bit linear
PCM and write a standard WAV file that any player or library can read.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

try:
    import audioop  # stdlib on Python <=3.12; audioop-lts provides it on 3.13+
except ImportError:  # pragma: no cover - depends on environment
    audioop = None  # type: ignore[assignment]

logger = logging.getLogger("voicecare.recordings")

SAMPLE_RATE_HZ = 8000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM


def _decode(codec_encoding: str, compressed: bytes) -> bytes:
    """Decode provider audio bytes (PCMA=A-law or PCMU=mu-law) to PCM16."""
    if audioop is None:  # pragma: no cover - depends on environment
        raise RuntimeError("audioop unavailable -- run: pip install audioop-lts")
    if codec_encoding.upper() == "PCMU":
        return audioop.ulaw2lin(compressed, SAMPLE_WIDTH_BYTES)
    return audioop.alaw2lin(compressed, SAMPLE_WIDTH_BYTES)


def decode_chunk(codec_encoding: str, compressed: bytes) -> bytes:
    """Public alias: one provider frame (or more) -> PCM16 bytes."""
    return _decode(codec_encoding, compressed)


def pcm16_to_codec(pcm16: bytes, codec_encoding: str) -> bytes:
    """Encode PCM16 audio into the provider's codec (PCMA or PCMU)."""
    if audioop is None:  # pragma: no cover - depends on environment
        raise RuntimeError("audioop unavailable -- run: pip install audioop-lts")
    if codec_encoding.upper() == "PCMU":
        return audioop.lin2ulaw(pcm16, SAMPLE_WIDTH_BYTES)
    return audioop.lin2alaw(pcm16, SAMPLE_WIDTH_BYTES)



def alaw_to_pcm16(alaw_bytes: bytes) -> bytes:
    """Convert raw A-law audio to 16-bit little-endian linear PCM."""
    if audioop is None:  # pragma: no cover - depends on environment
        raise RuntimeError("audioop unavailable -- run: pip install audioop-lts")
    return audioop.alaw2lin(alaw_bytes, SAMPLE_WIDTH_BYTES)


def beep_pcm16(
    frequency_hz: float = 1000.0,
    duration_ms: float = 350.0,
    volume: float = 0.45,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
) -> bytes:
    """A short "speak now" tone as 16-bit PCM mono at 8 kHz.

    The final open question ends with "...after the beep", so the beep IS the
    cue -- without it the patient either starts talking over the question or
    waits in silence for a prompt that never comes. Two short beeps separated
    by a gap read as a deliberate signal over a phone line (and are harder to
    mistake for echo or line noise than one long tone).

    Short raised-cosine fades at both ends: a hard-edged square wave clicks,
    which on a phone line sounds like a fault rather than a prompt.
    """
    import math
    import struct

    if duration_ms <= 0:
        return b""
    half_ms = max(40.0, duration_ms / 2.0)
    gap_ms = max(30.0, duration_ms / 4.0)
    total_ms = half_ms * 2 + gap_ms
    count = int(sample_rate_hz * total_ms / 1000.0)
    samples: list[int] = []
    for i in range(count):
        t_ms = i * 1000.0 / sample_rate_hz
        # Which part of the beep-beep are we in?
        if t_ms < half_ms:
            local = t_ms
        elif t_ms < half_ms + gap_ms:
            continue
        elif t_ms < half_ms * 2 + gap_ms:
            local = t_ms - half_ms - gap_ms
        else:
            break
        phase = 2.0 * math.pi * frequency_hz * local / 1000.0
        # Raised-cosine fade over the first/last 15 ms of each beep.
        fade_ms = 15.0
        envelope = 1.0
        if local < fade_ms:
            envelope = 0.5 - 0.5 * math.cos(math.pi * local / fade_ms)
        elif local > half_ms - fade_ms:
            remaining = half_ms - local
            envelope = 0.5 - 0.5 * math.cos(math.pi * remaining / fade_ms)
        value = int(32767 * volume * envelope * math.sin(phase))
        samples.append(value)
    return struct.pack(f"<{len(samples)}h", *samples)


def write_wav(path: Path, pcm16_bytes: bytes) -> Path:
    """Write 16-bit PCM mono audio to a WAV file, creating parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(SAMPLE_RATE_HZ)
        wav_file.writeframes(pcm16_bytes)
    logger.info("Saved recording: %s", path)
    return path


def write_call_recording(
    recordings_dir: Path,
    call_id: str,
    compressed_bytes: bytes,
    encoding: str = "PCMA",
) -> Path:
    """Decode buffered call audio (PCMA or PCMU) and save recordings/call_<id>.wav."""
    pcm16 = _decode(encoding, compressed_bytes)
    return write_wav(Path(recordings_dir) / f"call_{call_id}.wav", pcm16)

