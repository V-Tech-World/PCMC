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

