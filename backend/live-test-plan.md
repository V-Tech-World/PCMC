# VoiceCare LK -- Live-Call Test Plan (all steps, minimum cost)

One checklist for the **whole app**, tested against real Zernio calls.
Every "phone" entry below means **one dialed call = one billed minute**, so
the plan is ordered so that each call verifies as many things as possible.

**Never run this plan "just to check"** -- the offline suite
(`backend/.venv/Scripts/python.exe -m pytest backend/tests -q`, 141 tests)
already proves the logic. A live call only proves the provider integration.

## 0. One-time setup (no calls, no cost)

| # | Do | Expected |
|---|----|----------|
| 0.1 | `cp backend/.env.example backend/.env`, fill `ZERNIO_API_KEY`, `FROM_NUMBER` (toll-free), `CALLS_API_KEY`, run `ngrok http 8000`, set `PUBLIC_WSS_URL=wss://<host>/media-stream` | `GET /health` returns 200 and shows masked keys |
| 0.2 | Fill the WhatsApp sandbox vars: `ZERNIO_INBOX_ACCOUNT_ID`, `ZERNIO_ALERT_CONVERSATION_ID` (from your Zernio inbox thread with the sandbox number **+1 202 908 7457**) | startup log shows `WhatsApp alerts: enabled (sandbox conversation ...)` |
| 0.3 | Seed one test patient (use YOUR phone so real calls reach you): `POST /records/patients` header `X-Api-Key`, body `{"patient_code":"P-TEST","name":"Test Patient","phone_number":"+94...","diagnosis_category":"cardiac"}` | 201 created; `GET /records/patients` lists it |
| 0.4 | Start backend: `backend/.venv/Scripts/python -m uvicorn app.main:app --port 8000` | log shows DB line + no warnings |

Cost note so far: **0 calls.**

## 1. Step 1-2 (transport + recording) -- 1 call

**Call A (benign answers, hang up early).**
`POST /calls` `{"phone_number":"+94...","diagnosis_category":"general"}` then
answer nothing and hang up after ~10 s.

| Test | Expected outcome |
|------|------------------|
| Call is dialed and bridges | your phone rings, audio connects |
| WAV saved | `backend/recordings/call_<id>/` contains call + agent WAVs |
| No dialogue crash in logs | `event=stop` handled; `Call flow finished` in logs |
| DB row exists | `GET /records/calls` shows 1 row, `ended_reason` = hangup text, `risk_level` = low (no answers) |

## 2. Step 3 (dialogue, TTS, turn-taking) -- 1 call

**Call B (benign answers: "yes", "no", "no", then silence for the final
question).** Dial with the patient: `POST /calls` `{"patient_code":"P-TEST"}`.

| Test | Expected outcome |
|------|------------------|
| Voice quality | questions sound clear (not "underwater"/"off-station") |
| Yes/no flow | medication -> pain -> category asked in order; final open question last |
| Turn detection | your answer is captured ~1.2 s after you stop talking |
| No-speech repeat | stay silent once -> "Sorry, I did not hear that" + repeat, exactly once |
| Closing + stop | neutral closing, call ends by itself after the final window |
| DB row | `risk_level=low`, 4 answers stored, `alert_status=skipped` |

## 3. Step 4+5 (NLP, running risk, urgent ack) -- 1 call

**Call C (red-flag answers, cardiac patient):** "no I forgot my dose" /
"yes" -> "severe" / "yes I have chest pain" / "the pain is very bad now".

| Test | Expected outcome |
|------|------------------|
| Urgent ack (TC2) | right after the chest-pain answer you hear the urgent acknowledgment -- once only |
| Running risk logged | logs show `Running risk after turn=3: level=high` |
| Urgent closing | final goodbye is the urgent variant, not the neutral one |
| Final open question (60 s window) | you can pause mid-sentence without being cut off; call closes after the window even if you never hang up |
| DB row | `risk_level=high`, red-flag finding stored, `alert_status=sent (<id>)` |
| **WhatsApp alert arrives** | your WhatsApp (connected to the sandbox +1 202 908 7457) receives the alert with patient code P-TEST, HIGH risk, chest pain, and your answers |

## 4. Cost rails -- 0-1 extra calls (prefer log/config checks)

| Test | Do | Expected |
|------|----|----------|
| Rate limit 429 | set `MAX_CALLS_PER_HOUR=1` in `.env`, restart, `POST /calls` twice | second POST returns 429 + `Retry-After`; no call dialed |
| Max duration cap | optional: set `MAX_CALL_DURATION_SEC=60`, make a rambling call | dialogue stops asking after 60 s; row still saved with assessment; alert still sent if high |
| Concurrency cap | set `MAX_CONCURRENT_CALLS=1`, try two POSTs during one live call | second is refused (429), first call unaffected |

(Each config change = backend restart, no calls unless you choose the
optional duration-cap call.)

## 5. Step 7 alert edge cases -- reuse Call C's row, 0 extra calls

| Test | Do | Expected |
|------|----|----------|
| Low risk = silent (TC2) | covered by Call B | its row shows `alert_status=skipped`, no WhatsApp message arrived |
| Alert content (TC3) | read the Call C WhatsApp message | contains patient code, name/phone, risk level + score, key symptoms with severity, every answer + transcript, call id |
| Alert failure handling | set `ZERNIO_ALERT_CONVERSATION_ID` empty, trigger a high call | row shows `alert_status=not_configured`; call still completes; error in logs |

## 6. Full restart durability -- 0 calls

| Test | Do | Expected |
|------|----|----------|
| TC3 survive restart | stop uvicorn, start it again, `GET /records/calls` | all rows still there, patient links intact |

## 7. Steps 8 + 9 (scheduler + dashboard) -- 0 extra calls

Prereqs: backend restarted with the Step 8/9 `.env` block
(`SCHEDULE_CALLS_ENABLED=false`, `ADMIN_PASSWORD` set -> admin seeded) and
the frontend running (`cd frontend && npm run dev` -> http://localhost:5173).

| # | Do | Expected |
|---|----|----------|
| Step 8 (all zero-cost) | Schedule tab: read the banner; as admin click "Run one tick now" and confirm | Banner says **Automatic calls: OFF** with the real next check-in time per patient (TC3); tick result: `planned=N dialed=0`, message "no call placed" |
| TC1 -- three-role login (0 calls) | Login as `admin` -> Staff tab -> create a nurse (e.g. `nur001`) and a doctor (`doc001`) -> logout -> log in as each. Also try a wrong password | Each role lands on the dashboard with its own name/role in the top bar. Nurse/doctor: **no Staff tab, no "+ Add patient", no "Run tick"**. Nurse: expanded call shows no "Close case" (doctor sees it). Wrong password -> one generic error (never reveals which field) |
| TC2 -- risk colours (0 calls) | Calls tab, click through the filter chips | High = red badge, Medium = amber, Low = green; "Open alerts" = unreviewed high-risk rows; clicking a row expands transcript + risk reasons |
| TC4 -- responsive (0 calls) | Shrink the window below 1024px (or dev-tools mobile view) | Sidebar becomes a hamburger drawer; cards reflow 3 -> 2 -> 1; tables scroll sideways |
| TC3 -- Call Now (**this IS Call B**) | Patients tab -> "Call now" on the test patient -> read the confirm dialog -> confirm | Dialog names the number and warns about billing; the phone rings, the dialogue runs, a row appears with its risk colour, low risk -> no WhatsApp. **Do not also fire Call B via curl** -- same call, placed from the dashboard instead |

Optional (only if you ever WANT the cron live): set
`SCHEDULE_CALLS_ENABLED=true` and restart -- the banner flips to ON and a
due patient will be dialed on the next tick. Keep it `false` for demos.

## Budget summary

| Call | Purpose | Answers style | ~Cost |
|------|---------|---------------|-------|
| A | transport + recording + DB row | silence, hang up early | < 1 min |
| B | dialogue/TTS/turns/low-risk skip **+ Step 9 TC3 (placed from the dashboard)** | benign, finish naturally | ~2 min |
| C | NLP/risk/urgent ack/60 s window/WhatsApp alert | red flags | ~2 min |
| (opt) D | max-duration cap | rambling | ~1 min |

Total: **3-4 real calls (~5-6 billed minutes)** cover every step's TCs.
Everything else is proven by the offline suite.

## After each call: 30-second verification recipe

1. `GET /records/calls?limit=3` (with `X-Api-Key`) -- check the newest row's
   risk_level, answers count, ended_reason, alert_status.
2. Backend logs -- `Risk assessment: level=...`, `Call persisted: id=...`,
   `WhatsApp alert delivered: id=...` (or the skip reason).
3. Your WhatsApp -- the alert text matches the row.
