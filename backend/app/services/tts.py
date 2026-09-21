"""
Text-to-speech for call audio (Step 3).

Turns the dialogue manager's question text into 8 kHz mono PCM16 -- the
format the media stream speaks -- so it can be encoded to the negotiated
codec (PCMA/PCMU) and sent back over the WebSocket.

The voice chain (in order) is what makes the questions intelligible on a
phone call:
  1. synthesize            -> whatever rate the backend produces (SAPI: 22 kHz)
  2. resample to 8 kHz     -> with a real anti-aliasing filter (scipy)
  3. telephony band-pass   -> 300-3400 Hz, the band a phone line actually carries
  4. level match           -> ~-20 dBFS RMS with a peak ceiling, so G.711
                              encoding never clips (clipping = harsh "old radio"
                              distortion on a handset speaker)
Steps 3 and 4 matter: raw TTS audio contains energy outside the phone band and
is usually too hot, which sounds distorted however good the voice is.

Backends (picked by TTS_BACKEND, default auto):
- pyttsx3 (Windows SAPI / espeak): fully offline, saves WAV directly.
  Primary choice here -- no ffmpeg required.
- gtts: nicer voice, but outputs MP3 which needs ffmpeg to decode, and
  needs internet at call time. Only auto-selected if ffmpeg is present.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import wave

try:
    import audioop  # stdlib on Python <=3.12; audioop-lts provides it on 3.13+
except ImportError:  # pragma: no cover - depends on environment
    audioop = None  # type: ignore[assignment]

from app.core.config import get_settings

logger = logging.getLogger("voicecare.tts")

TARGET_RATE_HZ = 8000
FILLER_TEXT = "One moment, please."  # README section 4: plays while STT runs
_PEAK_LIMIT = 31000                 # headroom so codec encoding never clips

# Telephony shaping (applied after resampling, before encoding).
_BAND_LOW_HZ = 300.0                # below the phone band: rumble, DC
_BAND_HIGH_HZ = 3400.0              # above the phone band: hiss, sibilance
_FILTER_ORDER = 6                   # Butterworth band sections
_TARGET_RMS = 3200                  # ~-20 dBFS: a comfortable G.711 level
_PEAK_CEILING = 24000               # never let the codec encoder clip
_MIN_FILTER_SAMPLES = 64            # shorter than this, filtering is moot


class TTSBackendError(RuntimeError):
    """No usable TTS backend is available for this text."""


def _read_wav_pcm16(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as wav:
        return wav.readframes(wav.getnframes()), wav.getframerate()


def _resample_to_8k(pcm16: bytes, in_rate: int) -> bytes:
    """Resample to 8 kHz with a real anti-aliasing filter, then peak-limit.

    A naive `audioop.ratecv` downsample (linear interpolation, no filter)
    aliases everything above 4 kHz into the voice band -- that is the
    "underwater" garbling heard on the live call. scipy's resample_poly
    applies a proper windowed filter before decimating.
    """
    if len(pcm16) % 2:  # codec helpers require whole 16-bit frames
        pcm16 = pcm16[:-1]
    if in_rate != TARGET_RATE_HZ:
        try:
            import numpy as np
            from scipy.signal import resample_poly
        except ImportError:
            if audioop is None:
                raise TTSBackendError(
                    "Install numpy+scipy (requirements.txt) for clean resampling."
                ) from None
            logger.warning(
                "scipy unavailable -- falling back to ratecv; audio will be aliased"
            )
            pcm16, _ = audioop.ratecv(pcm16, 2, 1, in_rate, TARGET_RATE_HZ, None)
        else:
            import math

            g = math.gcd(in_rate, TARGET_RATE_HZ)
            samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
            resampled = resample_poly(samples, TARGET_RATE_HZ // g, in_rate // g)
            pcm16 = (
                np.clip(np.round(resampled), -32768, 32767).astype(np.int16).tobytes()
            )

    # Peak-limit so ulaw/alaw encoding never clips harshly.
    if audioop is not None and pcm16:
        peak = audioop.max(pcm16, 2)
        if peak > _PEAK_LIMIT:
            pcm16 = audioop.mul(pcm16, 2, _PEAK_LIMIT / peak)
    return pcm16


def _bandpass_telephony(pcm16: bytes) -> bytes:
    """Keep only the 300-3400 Hz band a phone line actually carries.

    Zero-phase filtering (sosfiltfilt) so the speech keeps its natural timing.
    Falls back to the unfiltered audio (with a warning) if scipy is missing --
    the audio is still intelligible, just harsher.
    """
    if len(pcm16) // 2 < _MIN_FILTER_SAMPLES:
        return pcm16
    try:
        import numpy as np
        from scipy.signal import butter, sosfiltfilt
    except ImportError:  # pragma: no cover - depends on environment
        logger.warning("scipy unavailable -- sending unfiltered (harsher) TTS audio")
        return pcm16

    sos = butter(
        _FILTER_ORDER,
        [_BAND_LOW_HZ, _BAND_HIGH_HZ],
        btype="bandpass",
        fs=TARGET_RATE_HZ,
        output="sos",
    )
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    filtered = sosfiltfilt(sos, samples)
    return np.clip(np.round(filtered), -32768, 32767).astype(np.int16).tobytes()


def _match_level(pcm16: bytes) -> bytes:
    """Normalize to a comfortable telephony level, never clipping.

    G.711 on a handset speaker distorts badly when the signal is hot, so aim
    for ~-20 dBFS RMS and hard-ceiling the peaks below the codec limit.
    """
    if audioop is None or not pcm16:
        return pcm16
    rms = audioop.rms(pcm16, 2)
    if rms == 0:
        return pcm16
    gain = min(max(_TARGET_RMS / rms, 0.05), 8.0)  # never amplify noise wildly
    out = audioop.mul(pcm16, 2, gain)
    peak = audioop.max(out, 2)
    if peak > _PEAK_CEILING:
        out = audioop.mul(out, 2, _PEAK_CEILING / peak)
    return out


def apply_telephony_chain(pcm16: bytes) -> bytes:
    """Public: band-limit + level-match already-8 kHz PCM16 mono audio."""
    return _match_level(_bandpass_telephony(pcm16))


def list_voices() -> list[str]:
    """Names of the installed pyttsx3/SAPI voices (for TTS_VOICE selection)."""
    import pyttsx3

    engine = pyttsx3.init()
    try:
        return [str(v.name) for v in engine.getProperty("voices") or []]
    finally:
        del engine


def _select_voice(engine, wanted: str):
    """Return the voice id whose name contains `wanted` (case-insensitive)."""
    voices = engine.getProperty("voices") or []
    needle = wanted.strip().lower()
    for voice in voices:
        name = str(getattr(voice, "name", "") or "")
        if needle and needle in name.lower():
            return voice
    logger.warning(
        "TTS_VOICE=%r not found; using the system default. Available: %s",
        wanted,
        ", ".join(str(getattr(v, "name", "?")) for v in voices) or "<none>",
    )
    return None


def _synth_pyttsx3(text: str, rate: int, voice: str = "") -> bytes:
    import pyttsx3

    fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    os.unlink(out_path)  # pyttsx3 wants to create the file itself
    try:
        # A fresh engine per call: runAndWait is unreliable on a reused engine.
        engine = pyttsx3.init()
        engine.setProperty("rate", rate)
        if voice.strip():
            selected = _select_voice(engine, voice)
            if selected is not None:
                engine.setProperty("voice", selected.id)
                logger.info("Using TTS voice: %s", selected.name)
        engine.save_to_file(text, out_path)
        engine.runAndWait()
    finally:
        pass
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise TTSBackendError("pyttsx3 produced no audio")
    pcm16, in_rate = _read_wav_pcm16(out_path)
    os.unlink(out_path)
    return _resample_to_8k(pcm16, in_rate)


def _synth_gtts(text: str) -> bytes:
    if not shutil.which("ffmpeg"):
        raise TTSBackendError("gtts backend needs ffmpeg on PATH to decode MP3")
    from gtts import gTTS

    with tempfile.TemporaryDirectory() as tmp:
        mp3_path = os.path.join(tmp, "speech.mp3")
        wav_path = os.path.join(tmp, "speech.wav")
        gTTS(text=text, lang="en").save(mp3_path)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3_path,
             "-ar", str(TARGET_RATE_HZ), "-ac", "1", wav_path],
            check=True, capture_output=True,
        )
        pcm16, in_rate = _read_wav_pcm16(wav_path)
    return _resample_to_8k(pcm16, in_rate)


def _pick_backend(preference: str) -> str:
    if preference != "auto":
        return preference
    try:
        import pyttsx3  # noqa: F401
        return "pyttsx3"
    except ImportError:
        pass
    if shutil.which("ffmpeg"):
        return "gtts"
    raise TTSBackendError(
        "No TTS backend available. Install pyttsx3 (pip install pyttsx3) "
        "or install ffmpeg for the gtts backend."
    )


class Speaker:
    """Synthesizes text to 8 kHz mono PCM16. Thread-safe via a lock."""

    def __init__(
        self,
        backend: str | None = None,
        rate: int | None = None,
        voice: str | None = None,
        bandpass: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.backend = _pick_backend(backend or settings.tts_backend)
        self.rate = rate or settings.tts_rate
        self.voice = voice if voice is not None else settings.tts_voice
        self.bandpass = (
            settings.tts_bandpass_enabled if bandpass is None else bandpass
        )
        self._lock = threading.Lock()
        self._filler_pcm: bytes | None = None
        logger.info(
            "TTS backend: %s (rate=%d, voice=%r, telephony_chain=%s)",
            self.backend,
            self.rate,
            self.voice or "system default",
            self.bandpass,
        )
        if self.backend == "pyttsx3" and not self.voice:
            # Log the installed voices once so TTS_VOICE is easy to set.
            try:
                logger.info("Available voices: %s", ", ".join(list_voices()))
            except Exception as exc:  # pragma: no cover - depends on environment
                logger.warning("Could not list TTS voices: %s", exc)

    def synthesize(self, text: str) -> bytes:
        """text -> 8 kHz mono PCM16 bytes, shaped for a phone line."""
        if self.backend == "gtts":
            synth = lambda: _synth_gtts(text)  # noqa: E731
        else:
            synth = lambda: _synth_pyttsx3(text, self.rate, self.voice)  # noqa: E731
        with self._lock:
            pcm16 = synth()
        if self.bandpass:
            pcm16 = apply_telephony_chain(pcm16)
        return pcm16

    def filler(self) -> bytes:
        """Cached short filler audio to play while STT runs (README section 4)."""
        if self._filler_pcm is None:
            self._filler_pcm = self.synthesize(FILLER_TEXT)
        return self._filler_pcm


_default_speaker: Speaker | None = None


def get_speaker() -> Speaker:
    """Process-wide shared speaker (backend initialized once)."""
    global _default_speaker
    if _default_speaker is None:
        _default_speaker = Speaker()
    return _default_speaker
