"""
End-of-turn detection (v1): energy-based silence + hard caps.

Turn detection is an open README decision; this is the simplest honest v1
(no ML): a turn ends when the patient has spoken and then stays silent for
`silence_duration_sec`, or when the turn exceeds `max_turn_sec`
(README section 4: answers are capped at ~8-10 s to keep clips quick).

Timing is SAMPLE-based, not wall-clock: every fed chunk advances the clock
by its own duration (samples / 8000). That makes the detector deterministic
and correct whether chunks arrive in real time or in a burst.

The caller also handles the no-frames case: if the provider simply stops
streaming for `turn_gap_sec`, the turn is over (silence can look like that
over some providers).
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import audioop
except ImportError:  # pragma: no cover - depends on environment
    audioop = None  # type: ignore[assignment]

SAMPLE_RATE_HZ = 8000


@dataclass
class TurnConfig:
    silence_threshold: int = 400    # RMS amplitude below this counts as silence
    silence_duration_sec: float = 1.2
    max_turn_sec: float = 10.0
    min_speech_sec: float = 0.3     # ignore sub-blip noise bursts


class TurnDetector:
    """Feed PCM16 chunks (any size); read .state until it says 'ended'."""

    def __init__(self, config: TurnConfig | None = None) -> None:
        self.config = config or TurnConfig()
        self.state = "waiting"  # waiting | speech | ended
        self.has_speech = False
        self._samples_seen = 0
        self._speech_samples = 0
        self._silence_samples = 0

    def reset(self) -> None:
        self.state = "waiting"
        self.has_speech = False
        self._samples_seen = 0
        self._speech_samples = 0
        self._silence_samples = 0

    def feed(self, pcm16: bytes) -> str:
        """Consume one PCM16 chunk; returns the current state."""
        if audioop is None:  # pragma: no cover - depends on environment
            raise RuntimeError("audioop unavailable -- run: pip install audioop-lts")

        samples = len(pcm16) // 2
        if samples == 0:
            return self.state
        self._samples_seen += samples

        if audioop.rms(pcm16, 2) >= self.config.silence_threshold:
            self._speech_samples += samples
            self._silence_samples = 0
            if self.state == "waiting":
                self.state = "speech"
            self.has_speech = True
        elif self.state == "speech":
            self._silence_samples += samples
            if self._silence_samples / SAMPLE_RATE_HZ >= self.config.silence_duration_sec:
                self.state = "ended"

        # Hard cap on the whole turn, whether or not speech was detected.
        if self.state != "ended" and self._samples_seen / SAMPLE_RATE_HZ >= self.config.max_turn_sec:
            self.state = "ended"

        return self.state

    @property
    def speech_duration_sec(self) -> float:
        return self._speech_samples / SAMPLE_RATE_HZ

    @property
    def had_min_speech(self) -> bool:
        """True if we heard at least min_speech_sec of sound (not just a blip)."""
        return self.speech_duration_sec >= self.config.min_speech_sec

