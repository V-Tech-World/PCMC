"""
Transcribe a saved call recording (Step 2 manual test tool).

Thin CLI over app/services/stt.py -- all real logic lives there so the
same code path is reused for live transcription later.

Setup: faster-whisper comes via requirements.txt. The first run downloads
the model (default "base", ~75 MB) into the HuggingFace cache; later runs
are instant.

Run (from backend/):
    .venv\\Scripts\\python transcribe_call.py recordings\\call_xxxxxxxxxx.wav
    .venv\\Scripts\\python transcribe_call.py path\\to\\any.wav --language en --model small
"""

from __future__ import annotations

import argparse
import json
import sys

from app.core.logging_setup import setup_logging
from app.services.stt import TranscriptionError, get_transcriber


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe a call recording with faster-whisper")
    parser.add_argument("audio_path", help="Path to the .wav file to transcribe")
    parser.add_argument("--language", default=None, help="Language code, e.g. en (default from .env)")
    parser.add_argument("--model", default=None, help="Model size override: tiny/base/small/...")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")
    args = parser.parse_args()

    setup_logging("WARNING")  # keep CLI output clean
    try:
        result = get_transcriber().transcribe(args.audio_path, language=args.language)
    except TranscriptionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "text": result.text,
                    "language": result.language,
                    "duration_sec": result.duration_sec,
                    "segments": [
                        {"start": s.start, "end": s.end, "text": s.text} for s in result.segments
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("--- TRANSCRIPT ---")
        print(result.text or "(no speech detected)")
        print("------------------")
        for seg in result.segments:
            print(f"  [{seg.start:6.2f} - {seg.end:6.2f}] {seg.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

