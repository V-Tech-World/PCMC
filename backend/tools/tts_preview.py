"""
Preview exactly what the patient will hear (offline TTS quality check).

Renders text through the same chain a live call uses -- synthesize -> resample
to 8 kHz -> telephony band-pass + level match -> encode to the call codec ->
decode back again -- and writes it to a WAV you can play. This is the fastest
way to pick a voice and confirm audio quality WITHOUT placing a real call.

Run (from backend/):
    .venv\\Scripts\\python tools\\tts_preview.py --list-voices
    .venv\\Scripts\\python tools\\tts_preview.py --script surgical --voice Zira
    .venv\\Scripts\\python tools\\tts_preview.py --codec pcma --out preview_pcma.wav
    .venv\\Scripts\\python tools\\tts_preview.py --text "Please answer yes or no."
    .venv\\Scripts\\python tools\\tts_preview.py --script general --no-bandpass
    .venv\\Scripts\\python tools\\tts_preview.py --measure

`--measure` prints the engineering numbers behind the audio-quality fixes
(band energy before/after shaping, codec-mismatch penalty, sleep resolution)
without writing anything except one shaped reference WAV.

Notes:
- The AI/robot voice is the Windows SAPI voice; changing it needs a different
  installed voice (Windows Settings > Time & language > Speech) or TTS_VOICE.
- The opening greeting is NOT here: Zernio speaks that on their own audio path.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import struct
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.logging_setup import setup_logging  # noqa: E402
from app.services import recordings as recordings_service  # noqa: E402
from app.services import tts as tts_service  # noqa: E402
from app.services.dialogue import CATEGORIES, DEFAULT_CATEGORY, build_script  # noqa: E402

_GAP_SECONDS = 0.4  # silence between questions, like a real call


def _resolve_codec(requested: str | None) -> str:
    if requested:
        return "PCMA" if requested.upper() == "PCMA" else "PCMU"
    configured = (get_settings().speak_codec or "pcmu").strip().lower()
    return "PCMA" if configured in ("pcma", "alaw") else "PCMU"


def _band_energy(pcm16: bytes, low_hz: float, high_hz: float) -> float:
    """Fraction of spectral energy in [low_hz, high_hz) Hz, 0..1 (8 kHz audio)."""
    import numpy as np

    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    if samples.size < 64:
        return 0.0
    windowed = samples * np.hanning(samples.size)
    spectrum = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / 8000)
    total = spectrum.sum()
    if total <= 0:
        return 0.0
    return float(spectrum[(freqs >= low_hz) & (freqs < high_hz)].sum() / total)


def _pcm_stats(pcm16: bytes) -> dict:
    import audioop

    return {
        "seconds": len(pcm16) / 2 / 8000,
        "peak": audioop.max(pcm16, 2),
        "rms": audioop.rms(pcm16, 2),
        "band": _band_energy(pcm16, 300, 3400),
        "low": _band_energy(pcm16, 0, 300),
    }


def _snr_db(reference: bytes, test: bytes) -> float:
    """SNR of `test` against `reference` (same length), in dB."""
    count = min(len(reference), len(test)) // 2
    ref = struct.unpack(f"<{count}h", reference[: count * 2])
    tst = struct.unpack(f"<{count}h", test[: count * 2])
    signal = sum(v * v for v in ref)
    noise = sum((a - b) ** 2 for a, b in zip(ref, tst))
    if noise == 0 or signal == 0:
        return math.inf
    return 10 * math.log10(signal / noise)


async def _avg_sleep_ms(frame_ms: int = 20, rounds: int = 25) -> float:
    start = time.monotonic()
    for _ in range(rounds):
        await asyncio.sleep(frame_ms / 1000)
    return (time.monotonic() - start) / rounds * 1000


def _measure(args) -> int:
    """Print the engineering numbers behind the audio-quality fixes."""
    from app.core.timing import acquire_fine_timer, release_fine_timer

    setup_logging("WARNING")
    # Render the full default script so the numbers cover real call audio.
    speaker = tts_service.Speaker(voice=args.voice, bandpass=False)
    full_text = " ".join(q.text for q in build_script(DEFAULT_CATEGORY))
    raw = speaker.synthesize(full_text)
    shaped = tts_service.apply_telephony_chain(raw)

    print(f"text={len(full_text)} chars  voice={args.voice or 'system default'}")
    for label, pcm in (("before shaping", raw), ("after shaping ", shaped)):
        stats = _pcm_stats(pcm)
        print(
            f"{label}: {stats['seconds']:5.1f}s peak={stats['peak']:5d} "
            f"rms={stats['rms']:5d} | 300-3400Hz={stats['band'] * 100:5.1f}% "
            f"<300Hz={stats['low'] * 100:5.1f}%"
        )

    # Codec-mismatch penalty: A-law bytes decoded as u-law (the old live bug)
    # vs. a correctly matched round-trip.
    matched = recordings_service.decode_chunk(
        "PCMA", recordings_service.pcm16_to_codec(shaped, "PCMA")
    )
    mismatch = recordings_service.decode_chunk(
        "PCMU", recordings_service.pcm16_to_codec(shaped, "PCMA")
    )
    print(f"\nSNR matched  (PCMA sent, PCMA decoded): {_snr_db(shaped, matched):5.1f} dB")
    print(f"SNR MISMATCH (PCMA sent, PCMU decoded): {_snr_db(shaped, mismatch):5.1f} dB")

    coarse = asyncio.run(_avg_sleep_ms())
    acquire_fine_timer()
    try:
        fine = asyncio.run(_avg_sleep_ms())
    finally:
        release_fine_timer()
    print(f"\nper-20ms-sleep coarse timer: {coarse:5.2f} ms (drift {coarse - 20:+.2f} ms)")
    print(f"per-20ms-sleep fine timer  : {fine:5.2f} ms (drift {fine - 20:+.2f} ms)")

    out = get_settings().recordings_dir / "previews" / "agent_preview_shaped.wav"
    recordings_service.write_wav(out, shaped)
    print(f"\nwrote {out} -> 8000 Hz, 1 ch, 16-bit, {len(shaped) / 2 / 8000:.1f}s")
    return 0


def _texts(args) -> list[str]:
    if args.text:
        return list(args.text)
    if args.script:
        return [q.text for q in build_script(args.script)]
    if args.all:
        lines: list[str] = []
        for category in sorted(CATEGORIES):
            lines.append(f"<category {category}>")
            lines.extend(q.text for q in build_script(category))
        return lines
    lines = [q.text for q in build_script(DEFAULT_CATEGORY)]
    lines.append("(default script -- pass --script/--all/--text to change)")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render our call audio offline, exactly as the patient receives it"
    )
    parser.add_argument("--text", action="append", help="One line to render (repeatable)")
    parser.add_argument("--script", choices=sorted(CATEGORIES), help="Render a whole category script")
    parser.add_argument("--all", action="store_true", help="Render every category script")
    parser.add_argument("--codec", choices=("pcmu", "pcma"), help="Call codec (default SPEAK_CODEC)")
    parser.add_argument("--voice", default=None, help="Voice name substring, e.g. Zira")
    parser.add_argument("--rate", type=int, default=None, help="Speech rate (words/min)")
    parser.add_argument("--no-bandpass", action="store_true", help="Skip band-pass/level match")
    parser.add_argument("--list-voices", action="store_true", help="List installed voices and exit")
    parser.add_argument("--out", default=None, help="Output WAV path")
    args = parser.parse_args()

    setup_logging("WARNING")  # keep CLI output clean

    if args.list_voices:
        for name in tts_service.list_voices():
            print(name)
        return 0

    codec = _resolve_codec(args.codec)
    texts = [t for t in _texts(args) if t]
    speaker = tts_service.Speaker(
        rate=args.rate, voice=args.voice, bandpass=not args.no_bandpass
    )

    out_path = Path(args.out) if args.out else (
        get_settings().recordings_dir / "previews" / f"agent_preview_{codec.lower()}.wav"
    )

    gap = b"\x00" * int(8000 * 2 * _GAP_SECONDS)
    pcm_out = bytearray()
    print(f"codec={codec}  voice={args.voice or 'system default'}  bandpass={not args.no_bandpass}")
    print("-" * 72)
    for text in texts:
        started = time.monotonic()
        pcm16 = speaker.synthesize(text)
        synth_sec = time.monotonic() - started
        # Round-trip through the call codec: this is what the wire carries.
        on_wire = recordings_service.pcm16_to_codec(pcm16, codec)
        heard = recordings_service.decode_chunk(codec, on_wire)
        pcm_out.extend(heard)
        pcm_out.extend(gap)
        print(f"{len(pcm16) / 2 / 8000:5.2f}s synth={synth_sec:4.2f}s | {text}")

    heard_path = recordings_service.write_wav(out_path, bytes(pcm_out))
    total_sec = len(pcm_out) / 2 / 8000
    print("-" * 72)
    print(f"{len(texts)} line(s), {total_sec:.1f}s total -> {heard_path}")
    print("This is byte-for-byte what we send on the call (minus the network).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
