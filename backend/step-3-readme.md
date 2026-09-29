# VoiceCare LK -- backend (Step 3: turn-based structured dialogue)

**Previous steps:** `step-1-readme.md` (telephony), `step-2-readme.md`
(batch transcription).

```
backend/
|-- app/
|   |-- api/                 # health, calls, media_stream (thin WS layer)
|   |-- core/                # config, logging, media_auth
|   `-- services/
|       |-- dialogue.py      # NEW: question-flow state machine (README sec.5)
|       |-- tts.py           # NEW: text -> 8 kHz PCM (offline SAPI backend)
|       |-- turn_detector.py # NEW: end-of-turn detection (energy + caps)
|       |-- call_flow.py     # NEW: the per-call driver (speak -> listen -> STT)
|       |-- stt.py / recordings.py / outbound_call.py
`-- recordings/              # call_<id>.wav + call_<id>/turn_<n>_<q>.wav
```

## What a call does now

1. `POST /calls` takes an optional `diagnosis_category`
   (`general` | `surgical` | `cardiac` | `respiratory` | `diabetic`,
   default general). The category rides on the call's one-time stream token.
   `dialogue.CATEGORIES` is the single source of truth: the question wording,
   the `POST /calls` validation and the `POST /records/patients` validation all
   read the same dict, so adding a discharge type is one entry here plus the
   matching `category_<type>` rule in `nlp.assess_conversation`.
2. Zernio answers -> plays the greeting (privacy line) -> streams audio to us.
3. The call-flow driver **speaks the first question** (offline TTS) and
   waits. **Only one question is asked at a time** (Step 3 TC1).
4. The patient answers; end-of-turn is detected by energy silence
   (default 1.2 s), a no-frame gap (2.5 s), or the 10 s cap.
5. That turn's audio is saved **separately**:
   `recordings/call_<id>/turn_<n>_<question>.wav` (Step 3 TC2).
6. A filler ("One moment, please.") plays instantly while faster-whisper
   transcribes the turn in a worker thread (README sec. 4 latency trick).
   **Off by default** (`FILLER_ENABLED=false`) -- STT is faster than the filler.
7. The transcript goes into the dialogue state machine, which asks the next
   question in order (Step 3 TC3). A full call covers every question exactly
   once, with the pain-severity follow-up only if pain = yes (Step 3 TC4).
   If no speech was heard, the question is repeated once.
8. Closing line, summary logged, whole-call WAV saved as before.

**Script order (main flow):** medication adherence -> pain (yes/no) ->
[severity only if yes] -> category question -> open "anything else" ->
closing. Yes/no interpretation is a keyword placeholder until the real NLP
engine lands in Step 4.

## Manual test (README Step 3, TC1-TC4)

**Step 0 -- check the audio offline first (no call, no cost).** This is the
fastest way to judge whether the questions will be intelligible, and to pick a
better voice:

```powershell
# from backend\:
.venv\Scripts\python tools\tts_preview.py --list-voices
.venv\Scripts\python tools\tts_preview.py --script surgical
# -> recordings\previews\agent_preview_pcmu.wav  (play it; it is byte-for-byte
#    what the call carries). Try --voice Zira, --codec pcma, --no-bandpass to
#    hear each change.
```

```powershell
# terminal 1:  ngrok http 8000
# terminal 2, from backend\:
.venv\Scripts\python -m uvicorn app.main:app --port 8000
# terminal 3, from backend\:
$key = (Get-Content .env | Select-String '^CALLS_API_KEY=').Line.Split('=')[1]
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/calls `
  -Headers @{ "X-Api-Key" = $key } `
  -ContentType "application/json" `
  -Body '{"phone_number": "+94766697286", "diagnosis_category": "surgical"}'
```

Then on the phone: answer each question out loud. Watch the backend log for
`Audio codecs: inbound=... outbound=...`, `Dialogue started`, one
`Speaking <n> chars as <x>s of <codec> audio: '...'` line per question,
per-turn `transcript=...` lines, and `Call complete` with the full answer
summary. Each turn's WAV lands in `recordings/call_<id>/`, along with
`agent_audio.wav` (what the patient heard from us).

**If the questions still sound wrong, the two files decide who is at fault:**

| `agent_audio.wav` | live call | conclusion | action |
|---|---|---|---|
| sounds bad | sounds bad | our TTS chain | tweak `TTS_VOICE` / `TTS_RATE` / band-pass, re-run `tts_preview.py` |
| sounds fine | sounds bad | the call path distorted it | raise with the provider, sending `agent_audio.wav` as evidence |
| sounds fine | sounds fine but robotic | it's just the SAPI voice | install a better Windows voice or use a neural TTS |

The format we send back is provider-documented (Telnyx-style
`{"event": "media", "media": {"payload": "<base64>"}}` frames, 20 ms each,
PCMU/8 kHz) and questions are audibly reaching the caller, so this is no
longer an unknown.

## Call audio quality & hangup handling (live fixes, 2026-09-19/20)

### The question voice sounded like "an old radio missing the frequency"

Three separate defects stacked up. All three are fixed, each with a
regression test so it cannot come back silently.

**1. Wrong outbound codec (the big one).** We encoded our questions using the
**inbound** codec from the `start` event's `media_format`. Zernio documents the
bidirectional (agent -> caller) media codec as a *separate, fixed* setting:
**PCMU / G.711 u-law at 8 kHz in both directions** ("Zernio fixes 3 of that
protocol's settings ... `stream_bidirectional_codec PCMU`"). On calls whose
inbound track was PCMA, every question we sent was A-law bytes decoded as
u-law -- pure distortion -- while the provider's own greeting stayed perfect
(different audio path). Fixed by `resolve_tx_codec()` in `call_flow.py`:
`SPEAK_CODEC=pcmu` (default), with `pcma` and `auto` (mirror inbound) kept for
A/B testing.

**2. Sub-300 Hz rumble the phone network cannot carry.** A large share of raw
SAPI energy sits below 300 Hz. A real phone line filters that out; handing it
straight to the codec wastes the narrowband channel and muddies/over-drives
the handset speaker. Measured over the whole rendered script:

| Path | Energy 300-3400 Hz | Energy < 300 Hz | RMS | Peak |
|---|---|---|---|---|
| before (no shaping) | 61.0% | 38.7% | 2870 | 31 000 (limiter) |
| after (band-pass + level match) | **98.4%** | **1.6%** | 2337 | 24 000 |

Measured with `tools/tts_preview.py`, then FFT'd band energy on the WAV it
writes (single questions vary; 14-39% of raw energy is below 300 Hz depending
on the sentence).

And the codec error, measured as a G.711 decode of the same audio:

```
correct decode  (PCMU bytes -> u-law): RMS 2339
wrong   decode  (PCMA bytes -> u-law): RMS 7155   <- 3.1x too loud, distortion
```

A 3x level jump into the handset is exactly what "an old radio off its
frequency" sounds like.

`apply_telephony_chain()` band-limits to 300-3400 Hz (zero-phase Butterworth
via `sosfiltfilt`) and level-matches to ~-20 dBFS RMS with a hard 24 000 peak
ceiling, so G.711 encoding never clips.

**3. Pacing ran ~1.5x slower than real time.** Windows' default scheduler tick
is ~15.6 ms, so `await asyncio.sleep(0.02)` actually slept **31 ms**, starving
the provider's jitter buffer -- heard as a stretched, watery voice. Measured
before/after on this machine:

```
before: 31.03 ms per 20 ms sleep   (55% too slow)
after : 20.62 ms per 20 ms sleep   (3% error)
```

`app/core/timing.py` raises the Windows timer to 1 ms via `timeBeginPeriod`
(reference-counted, released on shutdown; no-op on other platforms); `main.py`
acquires it at startup and logs `Sleep/pacing timer: fine (1 ms)`. Frame
pacing stays clock-based (frame *n* at `start + n*20 ms`), so a slow send
catches up in a burst instead of drifting.

**Diagnosing a bad-sounding call next time** -- two things make it objective
instead of guesswork:

- `python tools/tts_preview.py --script surgical` renders exactly what we put
  on the wire (synthesize -> resample -> band-pass -> level match -> codec
  round-trip) to a playable WAV, with **no phone call and no cost**.
  `--list-voices` lists the installed SAPI voices, `--voice Zira` picks one,
  `--codec pcma` A/B tests the codec.
- `recordings/call_<id>/agent_audio.wav` is the audio we actually transmitted.
  If it sounds bad too -> our TTS chain. If it sounds fine -> the call path
  distorted it, and that file is the evidence to raise with the provider.

### Script wording: instruction before the question

Live feedback: "Please say yes or no" *trailing* a question meant the patient
started answering on hearing the question, then heard the instruction play
over their own voice -- confusing, and it felt like they answered too early.
Every yes/no question now **leads** with the instruction
(`Please answer yes or no. <question>`); the severity follow-up carries its
options *inside* the question (`Mild, moderate, or severe?`) instead of a
trailing command; and the final open question no longer opens with "Lastly,"
which was being mistaken for the yes/no pattern. Every question is logged with
its audio length and codec (`Speaking 58 chars as 5.6s of PCMU audio: '...'`),
so a silent or truncated question is obvious in the log.

### The filler is off by default

`FILLER_ENABLED=false`. README section 4's "mm-hmm, one moment" trick exists to
cover slow STT, but with the `base` model transcription takes 0.6-1.0 s --
shorter than the filler itself, so it only delayed the next question. Turn it
on if you move to `small`/`medium`.

**Patient hangup mid-call -- caught and escaped cleanly.** The receiver
sets `session.ended` the moment the stream dies (disconnect or `stop`);
the driver checks it every 20 ms frame while speaking, so we stop pushing
audio into a dead call immediately. If the patient hangs up after (partly)
answering, the captured audio is still saved + transcribed and recorded
as a partial answer before the call ends. All hangup paths converge on
`CallEnded`, which the WS handler turns into: stop receiver -> save the
whole-call recording -> revoke the token -> clean log line. This is the
behavior the scheduler (Step 8) will rely on.
A short "echo-settle" window after each question also drops mic input so
the tail of our own voice is never captured as the start of an answer.

## Unanswered calls & voicemail (seen live 2026-09-19)

One test call never rang the phone (dashboard: *unanswered*), but Zernio
still connected our media stream ~42 s after dialing and routed it to an
announcement box -- our dialogue then transcribed the operator's prompt
("If you are satisfied with your message, please hang up.") as a patient
answer. Diagnosis: the dial request was accepted with the exact payload
shape that rang successfully before, so **the failed ring itself is
provider/carrier-side** -- but we were blind to it, and then talked into
a voicemail. Fixes now in place:

- **Answering-machine detection** is **off by default** (`CALL_AMD=false`).
  Zernio's `amd` defers the bridge until human-vs-machine is known, but a
  live test showed it can **block the bridge entirely** when the callee
  answers and stays silent (phone rang, no stream, 31 s, ended). Re-enable
  selectively: per call with `{"amd": true}` in the POST /calls body, or
  globally via `CALL_AMD=true` in `.env` once the provider behavior is
  validated. The voicemail case it guards against is instead mitigated by
  the runaway-dialogue guard below.
- **`GET /calls/{provider_call_id}/status`** (X-Api-Key protected):
  asks Zernio for the real call lifecycle (dialing/answered/ended/failed
  + duration). Use it whenever a call doesn't ring -- it shows whether
  the leg failed provider-side.
- The POST /calls response now includes `provider_status` and the full
  Zernio creation response is logged.
- **Dial-to-connect latency** is logged when the stream opens; >60 s
  triggers a warning (long ring = likely ring-no-answer routing).
- **Runaway-dialogue guard**: if two full turns produce zero patient
  audio, the dialogue aborts instead of continuing to talk to nobody.
- **Idempotency-Key** is sent with every dial (docs: safe retries, no
  double dial/bill on retry).

Also per the docs: outbound calls are capped per rolling hour (HTTP 429
would surface as a 502 from POST /calls) -- don't hammer test calls.

## New .env keys (Step 3)

| Variable | Default | Meaning |
|---|---|---|
| `TTS_BACKEND` | `auto` | `auto` / `pyttsx3` (offline, used here) / `gtts` (needs ffmpeg + internet) |
| `TTS_RATE` | `160` | pyttsx3 speech rate (words/min); lower = slower/clearer |
| `TTS_VOICE` | *(empty)* | voice name substring, e.g. `Zira` / `Hazel` / `David`. Empty = system default. Available names are logged at startup |
| `TTS_BANDPASS_ENABLED` | `true` | 300-3400 Hz band-pass + level match (the telephony chain) |
| `SPEAK_CODEC` | `pcmu` | codec for audio WE send; `pcmu` = the provider's documented bidirectional codec. `pcma` / `auto` for A/B testing |
| `FILLER_ENABLED` | `false` | "One moment, please." before transcribing; only worth it with a big STT model |
| `DIALOGUE_ENABLED` | `true` | `false` = Step 1/2 passthrough (record only) |
| `TURN_SILENCE_SEC` | `1.2` | silence that ends a spoken answer |
| `TURN_MAX_SEC` | `10` | hard cap per answer (README: 8-10 s) |
| `TURN_GAP_SEC` | `2.5` | no media frames this long -> turn over |
| `CALL_AMD` | `false` | Zernio answering-machine detection (opt-in; may block the bridge) |

Note: gTTS is NOT in requirements.txt (it conflicts with the `click` pin
that faster-whisper needs, and it's MP3-only which needs ffmpeg anyway).
pyttsx3 (Windows SAPI) is fully offline and saves WAV directly.

### Voices installed on this machine

```
python tools/tts_preview.py --list-voices
Microsoft David Desktop - English (United States)     <- the default, most robotic
Microsoft Hazel Desktop - English (Great Britain)
Microsoft Zira Desktop - English (United States)      <- noticeably less robotic
```

If the questions sound like an AI/robot, that is the SAPI voice, not a
distortion bug: set `TTS_VOICE=Zira` (or `Hazel`) in `.env` and re-run
`tools/tts_preview.py` to compare. A better voice needs a different installed
Windows voice (**Settings > Time & language > Speech**) or a cloud/neural TTS
engine in a later step.

## Tests

```powershell
.venv\Scripts\python -m pytest tests/ -v   # 61 offline tests (no real calls)
```

Step 3 additions cover: script order/shape, category variations, unknown
category rejection, full-flow no-repeats/no-skips, pain follow-up only on
yes, "no pain" not read as yes, choice extraction, state reset, turn
detector (silence-end, duration cap, silence-only), TTS 8 kHz synthesis
(real backend), codec round-trip, hangup-abort paths, the `CALL_AMD`
default/override (the knob that blocked a live call), the outbound-codec fix
(`resolve_tx_codec`), telephony shaping (sub-300 Hz removed, voice band kept,
level matched without clipping), the transmitted-audio recording, and the
script wording rules (instruction leads; no trailing command can be spoken
over an answer). The dialogue/turn logic is sample-time based so tests don't
need real-time pacing.

## Known limitations (deliberate, next steps fix them)

- Yes/no + choice detection is keyword matching -- Step 4 (NLP engine)
  replaces it with spaCy + negation-aware rules.
- Whole-call WAV contains only patient speech (our questions are dropped
  as echo). For a full call recording, we'd need provider-side mixing or
  dual-track capture.
- The provider may pause media during silence; if turns feel like they
  end too early, raise `TURN_GAP_SEC` / lower `TURN_SILENCE_SEC`.
- No DB yet (Step 6): answers live in memory and the log only.

## Next: Step 4 (NLP + risk engine, offline first)

Replace the keyword interpretation with the real NLP pipeline:
symptom extraction, negation ("no pain"), severity, and the transparent
rule-based risk scorer -- all tested against sample transcripts before it
touches a live call.
