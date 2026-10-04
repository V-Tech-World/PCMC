# VoiceCare -- Live-Call Test Plan (all steps, minimum cost)

One checklist for the **whole app**, tested against real Zernio calls.
Every "phone" entry below means **one dialed call = one billed minute**, so
the plan is ordered so that each call verifies as many things as possible.

**Never run this plan "just to check"** -- the offline suite
(`python -m pytest backend/tests -q` from the repo root, 224 tests)
already proves the logic. A live call only proves the provider integration.

## 0. One-time setup (no calls, no cost)

| # | Do | Expected |
|---|----|----------|
| 0.1 | `cp backend/.env.example backend/.env`, fill `ZERNIO_API_KEY`, `FROM_NUMBER` (toll-free), `CALLS_API_KEY`, run `ngrok http 8000`, set `PUBLIC_WSS_URL=wss://<host>/media-stream` | `GET /health` returns 200 and shows masked keys |
| 0.2 | **Alerts:** `.env` is already set to `ALERT_DELIVERY=whatsapp` with your care-team conversation. A HIGH-risk call posts the **short** alert (6 lines) into that WhatsApp thread and the full text lands on the call row. If a send fails because the 24-hour window closed: `cd backend && python get-info.py` -> paste the new `conversationId` into `.env` -> restart | startup log: `Alerts: enabled (delivery=whatsapp, conversation=6aaadf93...)`; a HIGH call logs `WhatsApp alert delivered: id=wamid...` and the row shows `alert_status=sent (wamid...)` |
| 0.3 | Seed one test patient (use YOUR phone so real calls reach you): `POST /records/patients` header `X-Api-Key`, body `{"patient_code":"P-TEST","name":"Test Patient","phone_number":"+94...","diagnosis_category":"cardiac"}`. Valid types: `general`, `surgical`, `cardiac`, `respiratory`, `diabetic`. You can also create it from the dashboard **Patients** page (and correct it later with **Edit**) | 201 created; `GET /records/patients` lists it |
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
"yes" -> **"it is very bad"** (deliberately *not* the word "severe") /
"yes I have chest pain" / "the pain is very bad now".

| Test | Expected outcome |
|------|------------------|
| Urgent ack (TC2) | right after the chest-pain answer you hear the urgent acknowledgment -- once only |
| Running risk logged | logs show `Running risk after turn=3: level=high` |
| Severity from lay wording (29 Sep fix) | logs show `Choice answer 'it is very bad' read as 'severe' by the NLP severity fallback` |
| Urgent closing | final goodbye is the urgent variant, not the neutral one |
| Final open question (60 s window) | you can pause mid-sentence without being cut off; call closes after the window even if you never hang up. **You hear two short beeps, then you speak** -- that is the cue the question names |
| Dead air after answering (2 Oct fix) | after each answer you hear "One moment, please." instead of a 1-2 s silence, then the next question |
| Silent patient (2 Oct fix) | answer nothing at all (mute / walk away) | it asks once, repeats once, then says "Sorry, I cannot hear you..." and ends -- it must NOT fire the whole script at you |
| DB row | `risk_level=high`, red-flag finding stored, `alert_status=sent (wamid...)` and `alert_message` holding the full alert text |
| **WhatsApp arrives** | your care-team WhatsApp gets the **6-line** alert (patient code, HIGH, score, key symptoms with [RED FLAG], "call them back now") -- not the 20-line full version |
| **Alert message in the dashboard** | open the row in the dashboard (Calls tab), amber panel "Alert message (ready to send)" with the patient code, HIGH, chest pain and every answer/transcript; **Copy** still works when delivery is off |

**Call C-2 (optional, same call if you want to combine):** answer medication
"yes", pain "yes" and then give an ungradeable severity ("it comes and goes").
Expected: `level=medium` with the reason line
`pain reported but severity not established (+2)` -> no alert at all (alerts
are HIGH only), row shows MEDIUM for nurse triage.

**Call C-3 (optional, 1 extra call, new patient type):** seed
`{"patient_code":"P-TEST-R","diagnosis_category":"respiratory", ...}` (or
`diabetic`) and answer: medication "yes", pain "no", category question
"yes I have a new cough" (resp.) / "yes my foot is sore and it is not healing"
(diabetic), final "nothing else". Expected: the **type-specific question is
asked**, the answer is read as **yes** (the leading answer word wins over the
"not healing" wording), and the row ends at MEDIUM (resp.) / HIGH (diabetic
foot sore).

## 4. Cost rails -- 0-1 extra calls (prefer log/config checks)

| Test | Do | Expected |
|------|----|----------|
| Rate limit 429 | set `MAX_CALLS_PER_HOUR=1` in `.env`, restart, `POST /calls` twice | second POST returns 429 + `Retry-After`; no call dialed |
| Max duration cap | optional: set `MAX_CALL_DURATION_SEC=60`, make a rambling call | dialogue stops asking after 60 s; row still saved with assessment; alert message still prepared if high |
| Concurrency cap | set `MAX_CONCURRENT_CALLS=1`, try two POSTs during one live call | second is refused (429), first call unaffected |

(Each config change = backend restart, no calls unless you choose the
optional duration-cap call.)

## 5. Step 7 alert edge cases -- reuse Call C's row, 0 extra calls

| Test | Do | Expected |
|------|----|----------|
| Low risk = silent (TC2) | covered by Call B | its row shows `alert_status=skipped` and an empty `alert_message` |
| Alert content (TC3) | read the stored `alert_message` (dashboard Copy, or `GET /records/calls/{id}`) | contains patient code, name/phone, risk level + score, key symptoms with severity, every answer + transcript, call id. The **WhatsApp** copy is the 6-line `format_alert_brief` |
| Delivery switch | set `ALERT_DELIVERY=ready` (or blank the conversation ids), restart, make a HIGH call | row shows `alert_status=ready` and nothing is sent; switch back to `whatsapp` to resume |
| Closed 24h window | leave the conversation id stale, make a HIGH call | row shows `alert_status=failed: ...` + "re-open it with `python get-info.py`"; **the message is still stored**, and the call is unaffected |
| Unknown mode | set `ALERT_DELIVERY=carrier-pigeon`, restart, trigger a high call | warning in logs, falls back to `ready` (never sends) |

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
| TC2 -- risk colours (0 calls) | Calls tab, click through the filter chips | High = red badge, Medium = amber, Low = green; "Open alerts" = unreviewed high-risk rows; clicking a row expands transcript + risk reasons, and a HIGH row also shows the prepared alert message with Copy |
| Dark mode (0 calls) | Toggle the theme (top bar) and walk every screen, expanding a call row and opening the patient form | No light-mode leftovers: the expanded call row, the risk/symptom panels, the alert chip, the schedule/status pills and the patient status pills all stay dark; the login card stays light on purpose |
| Edit a patient (0 calls) | As admin: Patients tab -> **Edit** on the test patient -> change the phone number and the discharge type -> Save; then log in as nurse and try to edit | Form comes back pre-filled with the code read-only, toast says "updated", the row shows the new values and no duplicate exists. Nurse: no Edit button (and a direct POST is refused with 403) |
| Re-send an alert (0 calls) | Open the Oct-2 HIGH row that still says "Ready to send" -> click **Send now** | Chip flips to **Sent** with the `wamid...` id, the care team's WhatsApp gets the 6-line alert, and the full message stays on the row. On a LOW/MEDIUM row the button is not shown (and a direct POST returns 409) |
| TC4 -- responsive (0 calls) | Shrink the window below 1024px (or dev-tools mobile view) | Sidebar becomes a hamburger drawer; cards reflow 3 -> 2 -> 1; tables scroll sideways |
| TC3 -- Call Now (**this IS Call B**) | Patients tab -> "Call now" on the test patient -> read the confirm dialog -> confirm | Dialog names the number and warns about billing; the phone rings, the dialogue runs, a row appears with its risk colour, low risk -> no alert at all. **Do not also fire Call B via curl** -- same call, placed from the dashboard instead |
| Scheduler switch (0 calls) | Schedule tab: flip the switch ON, read the danger confirm, confirm, then flip it straight back OFF | Banner + "Auto-dial active/inactive" label follow the switch; toast says it is saved. **Do not leave it ON** during the rest of the demo; toggling itself never places a call |
| 24h auto-dial cooldown (0–1 calls) | Call patient A (Call Now or an enabled tick), then try to call them again the same day from an enabled tick | Second dial is skipped with a "cooldown" outcome (still listed, `next auto-dial in ~Nh` on the Schedule tab); manual Call Now still works |
| Forgot password (0 calls) | On the login screen click "Forgot password?" -> set a new password for a test nurse -> log back in with it (try the old password once first) | Reset form asks only for employee ID + new password + confirm; green success banner; old password -> generic error, new password -> dashboard |

Optional (only if you ever WANT the cron live): set
`SCHEDULE_CALLS_ENABLED=true` and restart -- the banner flips to ON and a
due patient will be dialed on the next tick. Keep it `false` for demos.

## Budget summary

| Call | Purpose | Answers style | ~Cost |
|------|---------|---------------|-------|
| A | transport + recording + DB row | silence, hang up early | < 1 min |
| B | dialogue/TTS/turns/low-risk skip **+ Step 9 TC3 (placed from the dashboard)** | benign, finish naturally | ~2 min |
| C | NLP/risk/urgent ack/60 s window/prepared alert | red flags | ~2 min |
| C-3 (opt) | new discharge type (respiratory / diabetic) | type-specific answer | ~2 min |
| (opt) D | max-duration cap | rambling | ~1 min |

Total: **3-4 real calls (~5-6 billed minutes)** cover every step's TCs
(5-6 with the optional new-type call). Everything else is proven by the
offline suite.

## After each call: 30-second verification recipe

1. `GET /records/calls?limit=3` (with `X-Api-Key`) -- check the newest row's
   risk_level, answers count, **ended_reason**, alert_status and
   alert_message (a HIGH call should carry the full alert text; the WhatsApp
   copy is the 6-line brief).
   - `ended_reason = "stream closed while playing <what>"` -> the provider
     dropped the leg at that point (`<what>` names question N, the beep, the
     closing...). `GET /calls/<provider_call_id>/status` shows the provider's
     own view of the same call; if it keeps dying at the same duration, it is
     a Zernio leg limit, not us.
2. `backend/logs/voicecare.log` (written by every run, rotating) -- the exact
   `stop` event / disconnect, the risk reasons and the alert outcome survive
   after the terminal is closed.
3. Dashboard: open the row -- badge, "Why this risk level", symptoms, and the
   alert message you can Copy or re-send.
