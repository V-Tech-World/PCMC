"""
Speech-to-text: faster-whisper wrapper (batch / offline mode, Step 2).

Scope for Step 2: transcribe a saved call recording (.wav) into text.
The live mid-call use (Step 5) will reuse this class on short turn
recordings -- it is deliberately load-once / call-many.

faster-whisper runs the same Whisper models ~4x faster on CPU than the
original openai-whisper implementation. The model file is downloaded once
(~75 MB for "base") and cached in the user's HuggingFace cache dir.

Empty or silence-only audio is not an error: it simply yields empty text
(handled without crashing -- Step 2 TC4).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger("voicecare.stt")


class TranscriptionError(RuntimeError):
    """Raised when a file cannot be transcribed."""


@dataclass
class Segment:
    """One timestamped stretch of speech in the transcript."""

    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    text: str
    language: str | None
    duration_sec: float
    segments: list[Segment] = field(default_factory=list)


class Transcriber:
    """Lazy-loading faster-whisper transcriber."""

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
        model=None,  # test seam: inject a stub instead of loading faster-whisper
    ) -> None:
        settings = get_settings()
        self.model_size = model_size or settings.stt_model
        self.device = device or settings.stt_device
        self.compute_type = compute_type or settings.stt_compute_type
        self._model = model

    def _ensure_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise TranscriptionError(
                    "faster-whisper is not installed. Run: "
                    ".venv\\Scripts\\python -m pip install -r requirements.txt"
                ) from exc
            logger.info(
                "Loading STT model %r (device=%s, compute=%s) -- first run downloads it...",
                self.model_size,
                self.device,
                self.compute_type,
            )
            started = time.monotonic()
            self._model = WhisperModel(
                self.model_size, device=self.device, compute_type=self.compute_type
            )
            logger.info("STT model loaded in %.1fs", time.monotonic() - started)
        return self._model

    def transcribe(
        self,
        audio_path: str | Path,
        language: str | None = None,
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> TranscriptionResult:
        """Transcribe a WAV (or other audio) file into text + segments.

        vad_filter drops silence so short pauses don't become hallucinated
        filler words -- important for phone audio with dead air.
        """
        path = Path(audio_path)
        if not path.is_file():
            raise TranscriptionError(f"Audio file not found: {path}")

        model = self._ensure_model()
        settings = get_settings()
        started = time.monotonic()
        segments_iter, info = model.transcribe(
            str(path),
            language=language or settings.stt_language,
            beam_size=beam_size,
            vad_filter=vad_filter,
        )

        segments: list[Segment] = []
        texts: list[str] = []
        for seg in segments_iter:
            text = seg.text.strip()
            if text:
                segments.append(Segment(start=seg.start, end=seg.end, text=text))
                texts.append(text)

        result = TranscriptionResult(
            text=" ".join(texts).strip(),
            language=getattr(info, "language", None),
            duration_sec=float(getattr(info, "duration", 0.0) or 0.0),
            segments=segments,
        )
        logger.info(
            "Transcribed %s (%.1fs audio) in %.1fs -> %d chars, %d segments",
            path.name,
            result.duration_sec,
            time.monotonic() - started,
            len(result.text),
            len(result.segments),
        )
        return result


_default_transcriber: Transcriber | None = None


def get_transcriber() -> Transcriber:
    """Process-wide shared transcriber (model loaded once, reused)."""
    global _default_transcriber
    if _default_transcriber is None:
        _default_transcriber = Transcriber()
    return _default_transcriber
