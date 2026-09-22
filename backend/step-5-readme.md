# Step 5 -- Wire NLP + Risk into the Live Call + Cost Rails -- COMPLETE

Step 5 of the VoiceCare LK backend: the Step 4 risk engine now drives the live
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

- Final-question wording (open invitation, "speak clearly", "hang up").
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

