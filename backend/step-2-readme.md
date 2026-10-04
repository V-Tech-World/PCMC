# VoiceCare -- backend (Step 2: batch transcription proof)

**Previous step:** see `step-1-readme.md` (telephony connectivity --
verified live: call placed, audio streamed, WAV saved).

```
backend/
|-- app/
|   |-- main.py              # FastAPI app (uvicorn app.main:app)
|   |-- api/                 # routers: health, calls, media_stream
|   |-- core/                # config (.env), logging, media_auth (WS tokens)
|   `-- services/
|       |-- outbound_call.py # Zernio client
|       |-- recordings.py    # A-law -> PCM16 WAV writing
|       `-- stt.py           # faster-whisper wrapper (NEW in Step 2)
|-- transcribe_call.py       # CLI over services/stt.py (NEW in Step 2)
|-- tests/                   # offline pytest suite; real-model tests opt-in
`-- recordings/              # call_<id>.wav from Step 1 (Step 2's input)
```

## What Step 2 adds

1. **`app/services/stt.py`** -- a `Transcriber` class wrapping
   **faster-whisper** (same Whisper models, ~4x faster on CPU than
   openai-whisper). Load-once / call-many, configured via `.env`
   (`STT_MODEL`, `STT_DEVICE`, `STT_COMPUTE_TYPE`, `STT_LANGUAGE`).
2. **`transcribe_call.py`** -- point it at a saved `recordings/call_*.wav`
   and get a transcript (plain text or `--json` with timestamps).
3. VAD (voice activity detection) filtering is on by default, so silent
   stretches don't turn into hallucinated filler text.

## Manual test (README Step 2, TC1-TC4)

TC1/TC2 already passed in Step 1 (WAV created after each call; yours
played back clearly). Now:

1. Transcribe your real recording (first run downloads the `base` model,
   ~75 MB):

```powershell
cd backend
.venv\Scripts\python transcribe_call.py recordings\call_tHwiUPCcbqsQ.wav
```

   - **TC3**: the transcript should match what was actually said on the call.
   - **TC4**: a full sentence should come out without being cut off
     mid-word (check the timestamped segments with `--json`).

2. Want a new recording? Make a call with `POST /calls` (see
   `step-1-readme.md`) and transcribe the new WAV.

Model choices (set `STT_MODEL` in `.env`): `tiny` (fastest, rough),
`base` (default), `small` (better accuracy, slower on CPU). If `base`
mishears phone-quality speech, try `small` before worrying about anything
else.

## Tests

```powershell
.venv\Scripts\python -m pytest tests/ -v            # offline, stubbed model, ~1s
.venv\Scripts\python -m pytest tests/ -m real_stt -v  # real model (slow, downloads once)
```

The offline suite covers: A-law->PCM conversion and WAV writing; the
transcriber returning text + timestamped segments; missing-file errors;
and empty/silence audio returning empty text **without crashing**
(TC4). The `real_stt` marker transcribes your actual newest recording.

## Server

Unchanged from Step 1 except: new `connected` event handled in
`/media-stream` (Zernio sends it before `start`; seen in the live test),
and the app is now a package -- run it with:

```powershell
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

## New .env keys (Step 2)

| Variable | Default | Meaning |
|---|---|---|
| `STT_MODEL` | `base` | Whisper model size: tiny/base/small/medium |
| `STT_DEVICE` | `cpu` | `cpu`, or `cuda` on an NVIDIA GPU |
| `STT_COMPUTE_TYPE` | `int8` | int8 = fast CPU inference |
| `STT_LANGUAGE` | `en` | Forced language (English first, per the plan) |

## Security notes (unchanged posture, new surface)

- Transcription happens **locally** -- no audio or text leaves the machine.
  That matters for the survey's privacy concern (health data staying in
  the hospital's control).
- The model runs in-process; a hostile/corrupt WAV can only fail the
  transcription call, which is wrapped in `TranscriptionError`.
- Recordings keep accumulating in `recordings/` -- real patient audio is
  health data, so this folder is git-ignored and should be cleared or
  encrypted before any deployment outside the demo.

## Codec note (from live testing, 2026-09-19)

Zernio chooses the codec **per call** and declares it in the `start`
event's `media_format`. Two calls back to back negotiated differently
(`PCMA` then `PCMU`). The stream handler now reads the declared codec and
decodes with the matching law -- the earlier version always decoded as
A-law, so a PCMU call produced a garbled/noisy WAV even though nothing
was wrong with the audio itself. `PCMU` and `PCMA` have **nothing to do
with the spoken language** -- it is purely the transport codec.

The `event=stop` log line now also prints `max frame gap` -- how long the
longest silence between media frames was. That is the number to compare
against the Zernio dashboard's call duration when investigating the
"dashboard says 5 min, user hung up at 20 s" discrepancy: our recording
only contained a few seconds of media even though the call ran longer,
so either the provider pauses streaming during silence, or the call leg
stays up until the WebSocket closes / their timeout. Next live call,
compare: (a) our `event=stop` wall time, (b) the recording's audio
length, (c) the dashboard duration, and (d) `max frame gap`.

## Sinhala / Tamil STT -- research pointers (not implemented yet)

Per the README's plan, English first; Tamil second; Sinhala is the known
hard case. Options to benchmark (all local/offline unless noted):

- **faster-whisper (current)**: multilingual Whisper models include
  `si` (Sinhala) and `ta` (Tamil) tokens, but quality on `si` is weak,
  especially on 8 kHz phone audio. Try `small`/`medium` before dismissing.
  Set `STT_LANGUAGE` to `si`/`ta` instead of `en`.
- **AI4Bharat IndicWhisper / IndicConformer**: strong for Tamil and other
  Indic languages; Sinhala support is partial -- benchmark anyway.
- **Meta MMS (massively multilingual Speech)**: covers Sinhala
  explicitly; heavier to run on CPU.
- **Commercial STT for comparison** (cloud, not local): Google Cloud
  STT supports `si-LK` and `ta-IN/ta-LK`; useful as a quality reference
  even if the demo stays offline.

Practical next experiment for Step 10: record the same sentences in
Sinhala and Tamil over a real call, transcribe with (1) whisper small,
(2) whisper medium, (3) an Indic/MMS model, and compare word error rate
by hand. Keep phone-audio realism (8 kHz) in mind -- most benchmarks use
clean 16 kHz audio, so expect worse numbers than published.

## Next: Step 3 (turn-based structured dialogue)

One question at a time, per-turn audio captured separately, dialogue state
machine (`dialogue.py`). The Transcriber from this step will be reused on
short per-turn recordings instead of whole calls.

