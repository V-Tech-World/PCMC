# Step 5 -- Wire NLP + Risk into the Live Call + Cost Rails -- COMPLETE

Step 5 of the VoiceCare backend: the Step 4 risk engine now drives the live
call, **the final open question was redesigned as a fixed 60-second capture
window**, and the backend gained **cost rails** (max call duration + internal
rate limiting) so a runaway call or dialing loop can never run up provider
charges. No real call was dialed while building this -- everything below was
verified offline with mocked sockets/TTS/STT.

## What was built

### 1. Final question redesign (the "anything else" question)

- The final question is an **open invitation** (kind `open`), not a yes/no, so
  it reads nothing like the structured questions before it:
  *"Final question. Please tell me anything else concerning you about your
  recovery. Please speak clearly, and when you are done, hang up. We will get
  back to you soon."*
- It is captured by **`capture_final_answer()`** with a fixed
  **`FINAL_ANSWER_SEC` (60 s) window** instead of the 1.2 s silence detector,
  so patients who pause mid-sentence are never cut off. The call closes after
  the window either way ("we will get back to you soon" replaces a
  conversational goodbye that would never be heard).

### 2. `backend/app/services/call_flow.py` -- risk in the live loop

- **TC1** -- after every transcribed turn the running risk is scored
  (`dialogue.assess_risk()`) and logged. The rule-based NLP is sub-millisecond,
  so the next question starts right after STT with **no added dead air**.
- **TC2** -- when the running risk reaches HIGH, the one-shot
  `pop_urgent_acknowledgment()` text is spoken **once** before the next
  question (never repeated -- `_urgent_announced` guards it), and
  `dialogue.closing_text` switches to the **urgent variant**.
- `_log_assessment()` logs the assessment uniformly on all exit paths --
  normal finish, patient hangup mid-turn, hangup after an answer, and the new
  max-duration abort. A hangup can never lose the risk decision.
- **Max call duration (cost rail)** -- a wall-clock cap
  (`MAX_CALL_DURATION_SEC`, default 300 s). When the cap hits, the dialogue is
  aborted, the assessment is still computed and logged, and `CallEnded` ends
  the call. `0` disables the cap (tests).

### 3. `backend/app/core/rate_limiter.py` (new) -- internal rate limiting

Pure in-process guards, evaluated **before** the provider is contacted (a
refused dial costs nothing):

- **Hourly sliding window** (`MAX_CALLS_PER_HOUR`, default 30) using monotonic
  timestamps.
- **Daily counter** (`MAX_CALLS_PER_DAY`, default 200), resets at local
  midnight.
- **Concurrent stream cap** (`MAX_CONCURRENT_CALLS`, default 3) -- counts the
  live media sessions in `media_stream.active_sessions`.
- `0` disables any limit; `RateLimitExceeded` carries a `retry_after_sec`
  hint that becomes a `Retry-After` header.
- Wired into `POST /calls`: **429** with `Retry-After` before dialing;
  successful dials are counted via `record_dial()`.

### 4. `backend/app/api/media_stream.py`

- New `active_sessions` registry (identity-based list) -- sessions are added
  when the stream starts and removed in the `finally` block, so the
  concurrency cap counts real live streams.

### 5. `backend/app/core/config.py` + `.env.example`

- `final_answer_sec: float = 60.0` (the Step 5 final-question window).
- `max_call_duration_sec: float = 300.0`, `max_concurrent_calls: int = 3`,
  `max_calls_per_hour: int = 30`, `max_calls_per_day: int = 200`.
- `.env.example` documents all of them.

### 6. `backend/tests/test_step5.py` (new) -- 14 tests, all offline

## Change log -- 2 Oct 2026 (live call: beep cue, dead air, silent-patient exit)

Four changes, all from live feedback on that day's calls.

### 1. The final question now promises a beep -- and the beep exists

> "Final question. Please tell me anything else concerning you about your
> recovery. **Please speak clearly after the beep**, and when you are done, hang
> up. We will get back to you soon."

The question is long, so without a cue the patient either talked over the last
words or sat waiting for a prompt that never came. `call_flow.beep()` plays two
short 1 kHz tones (`recordings.beep_pcm16`, raised-cosine fades so it doesn't
click on the line) right **before** the 60 s capture window opens -- so the
promise in the wording is true, and the tone is never recorded as speech.
Tunable: `BEEP_ENABLED`, `BEEP_FREQUENCY_HZ`, `BEEP_DURATION_MS`. Tested on the
wire: the same mocked call with the beep off sends exactly N fewer frames.

### 2. Filler on by default (the "waiting few seconds" complaint)

`FILLER_ENABLED` was `false` because the base STT model is quick. On live calls
the patient was left in 1-2 s of dead air after answering -- which reads as
*"did it hear me?"* and made some answer a second time into the gap. The filler
("One moment, please.") is now sent **immediately** when their turn ends, so
STT + next-question TTS happen underneath it.

### 3. A patient we cannot hear is no longer walked through the script

Live bug: with one repeat, the loop recorded an *empty* answer and moved on, so
someone whose audio we never heard got **medication -> pain -> category ->
final in a few seconds**. Every silent capture now counts
(`MAX_SILENT_ATTEMPTS`, default 2 = ask once + one repeat); after that the call
closes politely:

> "Sorry, I cannot hear you. I will end this call now. If you would like to
> talk to someone, please call the hospital. Goodbye."

`ended_reason` records `no patient audio after N attempts`. This also protects
the cost rails -- we stop burning a billed minute on someone who is not there.
(The counter counts the *repeat* too: it was originally counted per question,
which meant the cap could never be reached.)

### 4. Alerts send by default (see step-6-7-readme)

`ALERT_DELIVERY` now defaults to `whatsapp`, and any HIGH-risk call that ended
up `ready`/`failed` can be alerted afterwards from the dashboard
("Send now" -> `POST /records/calls/{id}/alert`).

**Gotcha worth remembering:** `get_settings()` is cached at startup, so **all of
this needs a backend restart** -- a running process keeps the old `.env` and the
old code. That is why the first attempt at the alert still showed `ready`.

**Tests:** `test_final_question_is_open_and_sets_window_expectation` (now
asserts "after the beep"), `test_beep_pcm16_*`,
`test_run_call_plays_the_beep_before_the_final_answer` (frame-count diff on the
wire), `test_beep_is_called_once_per_call_*`,
`test_silent_patient_is_not_walked_down_the_whole_script`,
`test_ended_reason_says_where_the_leg_died`, and the updated defaults in
`test_audio_settings_defaults_are_the_fixed_ones`.

### Later the same day: the leg died right after the final question

A general patient with severe answers: `ended_reason = "stream closed while
speaking"`, `anything_else` transcript empty, duration 76.7 s -- they heard the
filler, the final question (ending "we will get back to you"), and then the line
went dead, so the beep and the goodbye never made it.

**Nothing in this codebase closes the leg.** `session.ended` only flips on the
provider's `stop` event or a socket disconnect, and `MAX_CALL_DURATION_SEC`
(300 s) was nowhere near. So the provider dropped it ~1 s after the last
question started.

The alert and the risk decision were unaffected -- the row still scored **HIGH 8**
(meds not taken +2, ungradeable pain +2, "Fever" found in free text +3, pain +1)
and the WhatsApp alert **was sent** from the `except CallEnded` path. That
fail-safe is the whole point of persisting on every exit path.

Two changes so the *next* one is diagnosable instead of guesswork:

1. **File logging.** `setup_logging()` now also appends to
   `backend/logs/voicecare.log` (rotating 2 MB x 5, git-ignored -- it can contain
   patient speech). Before this, the only record of *why* a call ended lived in
   the terminal window, which is closed by the time you ask.
2. **`ended_reason` says where the leg died.** Every `send_pcm`/`speak`/`beep`
   passes a `what` label, so the row now reads *"stream closed while playing
   question 5 (anything_else)"* or *"… while playing the beep"* instead of an
   ambiguous *"while speaking"*. That single string is usually the difference
   between "the provider cut us off mid-question" and "our beep killed the
   stream".

The beep was also turned up (volume 0.35 -> 0.45): on a narrowband phone line it
has to be unmistakably the loudest thing in the call, since it is the only cue
the patient gets.

### The real damage: the filler was deleting the patient's last answer

The next live call (general patient, `BYPwOdNFpKYw`, also ~77 s) exposed the
consequence. The row said `MEDIUM 4` = moderate pain (2) + missed dose (2), and
the final question's answer was simply **missing** -- no `turn_05*.wav`, no
`anything_else` entry, even though the patient had plainly said "severe
headache". With that answer the score is 2 + 2 + headache(1 + severe 2) = **7 =
HIGH**, so the call was under-triaged by exactly the symptom that mattered.

The log explained it:

```
23:33:11  Speaking 179 chars as 13.6s: 'Final question...'
23:33:25  Playing 1000 Hz beep -- the patient's cue to speak
23:33:36  event=stop        (~11 s of patient speech in the final window)
row: ended_reason = "stream closed while playing audio"   <-- 'audio' = a send_pcm
                                                                   with no `what` label
```

`~29.2 s` of inbound audio arrived, but the four turn WAVs only account for
~17.5 s: the missing ~11 s was the headache answer. It had been **captured**,
then discarded, because the filler is sent *between* capture and transcription:

```python
if had_speech:
    await send_pcm(filler)        # leg died HERE -> CallEnded
transcript = await _transcribe_turn(...)   # never ran
```

So a courtesy line ("One moment, please.") could delete the most valuable
turn in the call. The filler is now wrapped: if the leg closes there, the turn
is transcribed, recorded, scored and persisted **first**, and only then does
the call end (`leg_died` is re-raised after the running-risk log). Verified by
`test_answer_survives_a_leg_that_dies_on_the_filler`, which reproduces the
incident end to end and asserts the recovered call scores HIGH.

### 4 Oct 2026: two calls, and a much quieter problem

**Call 11** (`bIufGxglE3Ow`, 25 s) -- the patient could not get to every question
in time; the flow ended politely at `call ended by patient mid-turn` after
pain = No. LOW 0. That is the designed behaviour, nothing to fix.

**Call 12** (`7k_egAUXucJw`, 94 s) -- the patient had time to answer, and the
call still came out nearly empty:

| question | transcript | audio captured |
|---|---|---|
| medication | `""` | 0.4 s (silence) |
| pain | `"Yes."` | 0.4 s |
| pain_severity | `""` | 0.0 s |
| category_general | `"for me."` | 0.5 s |
| anything_else | `""` | -- |

`frames=68` for the whole call: **1.4 s of patient audio across 94 seconds**,
against the ~29 s the previous call received. Result: `MEDIUM 3`, which reads
exactly like a calm patient. Two things were wrong, and only one was ours.

**What we could not see.** `event=stop` never arrived on this call -- the
provider just closed the socket -- and those are the frames that carried
`echo_frames` and `max_frame_gap`. So the one number that would have settled
"did they send it and we dropped it, or did they never send it?" was logged
**only** when the provider remembered to be polite. `MediaSession
.log_stream_stats()` now runs on every close and reports both halves:

```
Inbound audio (stream closed): received=68 of ~4700 expected over 94.1s
(1% delivered) -> kept=68 (~1.4s, 0 dropped as our own echo, 0 wrong track)
```

`received` counts everything the provider sent (kept + dropped-as-echo +
wrong-track), which splits the diagnosis cleanly: `received ~= expected,
kept ~= 0` means we threw it away; `received ~= 0` means the leg died. The
capture windows log their own numbers too (`Capture window (turn): 6.1s open,
0 frames, 0.0s of audio`), so a dead window warns at the turn instead of being
described as "no speech detected".

**The safety half.** Even when audio is lost, an unanswered question must not
look like a "no". `assess_risk()` takes an `unanswered` count and states it in
the reasons -- with **no** extra points, because a transport failure is not
evidence of risk:

> `3 question(s) answered with no usable audio -- risk may be understated;
> check the transcript before closing this call`

### The filler really did stutter

Measured on this machine (SAPI), `"One moment, please."` renders a **0.63 s**
dead gap between the two words -- it is the comma, stretched by the voice --
plus a 940 ms silent tail. The patient heard a stutter immediately after
answering, which is the worst possible moment for one. Fixed on both counts:
`FILLER_TEXT = "One moment please."` (same gap, 0.15 s) and `trim_silence()`
applied once to the cached filler. The filler went from **2.44 s to 0.88 s**,
so it also stops eating a second of the patient's time per answer.

### 4 Oct 2026, later: the final question had no room, and the goodbye was wrong

Same day, a third live call (general patient). Two things the patient reported,
both real:

**The bot talked over the final answer.** The docstring promised a fixed
`FINAL_ANSWER_SEC` window, but the loop also carried the frame-gap timeout from
`capture_turn`:

```python
except asyncio.TimeoutError:
    break          # <- 2.5 s of silence ended a 60 s window
```

So after the beep the patient got ~2 s before the closing line started -- on the
one question that carries free-text symptoms, and the slowest one to answer.
Silence no longer ends the window. Two rules now:

- before any sound: give up after `FINAL_ANSWER_PATIENCE_SEC` (15 s), because a
  patient who is never going to answer must not hold a billed line for a minute;
- after any sound: **silence never ends the window early.** They may pause
  mid-sentence; only the full `FINAL_ANSWER_SEC` does.

**The goodbye told the patient to call the hospital.** Both closings ended with
"please call the hospital" / "please contact the hospital", as did the no-audio
goodbye. Removed from all of them. On a post-discharge follow-up the patient has
*already* called, the care team owns the escalation, and repeating it on every
call reads as though nothing were happening. The closings now only say what we
did:

> Thank you. Your answers have been recorded and your care team will review
> them. Goodbye.

> Thank you. I have noted your symptoms as urgent, and your care team will
> contact you as soon as possible. Goodbye.

**The patient now ends their own call.** After the closing line we say nothing
and wait up to `HANGUP_GRACE_SEC` (60 s) for them to hang up; if they are still
there we disconnect for them, because the line is billed either way. The row now
records which happened: `dialogue finished -- patient hung up 12s after the
closing line` vs `... -- auto-disconnected 60s after the closing line`.

**All five discharge types** share the closing, the final window and the grace
period, so only the one category question differs. Each is now covered
end-to-end by `test_every_discharge_type_asks_its_own_question_then_closes`
(general / surgical / cardiac / respiratory / diabetic), since only `general`
had been tested live.

### 4 Oct 2026, 11:22: a call that connected fine and still went silent

Live call to a **second** number (+94782586272). The log looks healthy right up
to the point it matters:

```
event=start ... media_format={'channels': 1, 'encoding': 'PCMA', 'sample_rate': 8000}
media format OK: PCMA / 8000 Hz / mono
Audio codecs: inbound=PCMA outbound=PCMU (SPEAK_CODEC=pcmu)
Speaking 73 chars as 5.8s of PCMU audio: 'Please answer yes or no...'
Capture window (turn): 3.0s open, 130 frames, 2.6s of audio, speech=True
ERROR | Call flow crashed
  TypeError: open() got an unexpected keyword argument 'metadata_errors'
```

Nothing is wrong with that number. Two separate faults, both environmental:

**1. `faster-whisper` vs PyAV.** PyAV 14 removed the `metadata_errors` kwarg that
`faster_whisper.audio.decode_audio()` still passes to `av.open()`. Every call
died on its **first** answer -- after the greeting, with the patient already on
the line. There is no faster-whisper release that works with `av>=14`, so the
ceiling is ours to hold:

```
av>=12,<14
```

**2. `sqlmodel` vs SQLite timestamps.** The `.venv` restart at 11:04 also
brought `sqlmodel` 0.0.47, which types datetime columns as `UTCDateTime` and
*rejects a naive value*. `record_call` catches that deliberately (a DB problem
must not hide the risk decision) -- which meant a call that ran perfectly left
**no row at all** and the dashboard simply showed nothing. `_aware_utc()` now
coerces naive input to UTC instead of letting the write die, and `iso_utc()`
guarantees the API still emits an explicit offset (otherwise the browser reads
a UTC timestamp as local time and shifts every call).

**Why this was invisible:** both faults only fire on a *live* call, after a real
patient answers. So `STT_VERIFY_ON_START` (default on) now transcribes a 1 s
tone at boot and logs `STT self-test: OK`, and `/health` carries
`stt_pipeline_ok`. A broken stack is now visible *before* you dial anyone:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | Select-Object stt_pipeline_ok, stt_pipeline_error
```

### Also worth knowing

`backend/.venv` is **empty** (pillow, requests, dotenv -- no pydantic, no
fastapi). The running backend uses the system Python 3.13, which has every
dependency plus `audioop-lts`. The documented
`.venv\Scripts\python.exe -m uvicorn app.main:app` will fail; use
`python -m uvicorn app.main:app --port 8000` until the venv is rebuilt.
- Config defaults: 60 s window + the four cost rails.
- **TC1**: risk scored on partial dialogues, red flag -> immediate HIGH, and
  sub-millisecond scoring (no dead air).
- **TC2**: one-shot urgent ack (fires once, then `''`), urgent closing, and
  the same over the mocked live path (exactly one ack spoken, urgent closing).
- **TC3**: a fully mocked `run_call` completes: 4 questions + closing, no
  urgent ack on benign answers, final question spoken last; plus the
  max-duration abort test (a tiny cap stops the dialogue before the final
  question) and the 429 endpoint test.
- Rate limiter: hourly/daily blocking with `retry_after_sec`, unlimited at 0,
  concurrent-slot logic, and `POST /calls` returning **429** before dialing.

## Verification

```
backend/.venv/Scripts/python.exe -m pytest backend/tests -q
122 passed, 1 deselected  (108 pre-existing + 14 new)
```

(The 1 deselected test is the Step 3 opt-in TTS test; the Step 5 suite is
fully offline. One Step 3 timing test is load-sensitive and can fail under
heavy parallel load -- it passes in isolation and in the full run above.)

## Deliberate design decisions

- **Cost rails are in-process** -- this backend is a single instance and no
  external store (Redis etc.) is in the dependency set. Multi-instance
  deployments would need a shared store (a Step 6+ concern).
- **The cap applies to the dialogue** -- the dialogue stops asking questions
  once the cap is reached; the assessment is still logged.
- **429 before dial** -- rate limiting happens before
  `outbound_call.place_call`, so refused requests never reach (or bill) the
  provider.
- **One-shot ack** -- `pop_urgent_acknowledgment()` returns `URGENT_ACK_TEXT`
  exactly once (first time risk hits HIGH), then `''` forever -- the patient
  is never nagged twice.
- **No live dialing** -- per the operator's instruction, no call was placed;
  every path was exercised with mocked WS/TTS/STT, and dialing stays disabled
  until explicitly requested.

## Notes / limitations

- Risk results are still logged-only; persistence (per-call rows) is Step 6.
- The hourly window is monotonic-clock based; a system sleep larger than the
  window would under-count (irrelevant for a server that doesn't sleep).
- `MAX_CALL_DURATION_SEC` starts when the dialogue starts, not when the
  provider dial started (ring time is billed too, but is short and out of the
  dialogue's control).

