# Step 6 + 7 -- Database (SQLModel + SQLite) + HIGH-risk alerts -- COMPLETE

Steps 6 and 7 of the VoiceCare backend, built together: every call is now
persisted in SQLite (patients table + one row per call) and every **HIGH-risk
call produces an alert message that is stored on the row** (delivered to
WhatsApp only when `ALERT_DELIVERY=whatsapp`). 19 new offline tests; full
suite: **224 passed, 1 deselected**.

## Step 6 -- Database

### `backend/app/db/` (new)

- **`models.py`** -- `Patient` (patient_code unique, name, phone, category,
  discharge date) and `CallRecord` (one row per call): provider_call_id,
  patient link (FK + denormalised patient_code), phone, category, started /
  finished timestamps, duration, ended_reason, full answers JSON (the
  per-question transcript + interpretation), risk_level / score / reasons /
  findings JSON, alert_status. JSON helpers keep call sites clean.
- **`engine.py`** -- cached engine per `DATABASE_URL` (default
  `backend/voicecare.db`), `init_db()` creates the schema (idempotent),
  in-memory `sqlite://` supported for tests (StaticPool).
- **`service.py`** -- `record_call()` (the single persistence entry point),
  `find_patient()` (by code, then by phone), `attach_alert()`,
  `list_calls()` / `list_patients()`. **Never raises**: a DB problem cannot
  hide the call's risk decision (which is also in the logs).

### Wiring

- `call_flow.run_call(...)` persists + alerts on **every** exit path (normal
  finish, hangup mid-turn, hangup after an answer, max-duration cap) via
  `_persist_and_alert()` (runs in a worker thread, never raises).
- `POST /calls` accepts an optional **`patient_code`**: the patient's
  registered number is dialed and the code rides the one-time stream token
  into `/media-stream`, so the row links to the right patient (TC2). A call
  dialed without a code still links by phone number.
- New **`/records` API** (`app/api/records.py`, same `X-Api-Key` guard):
  `GET /records/calls` (newest first, full JSON), `GET /records/patients`,
  `POST /records/patients` (upsert -- the dashboard in Step 9 wraps these).
- `init_db()` runs at startup (`main.py` lifespan).

## Step 7 -- HIGH-risk alert message (+ optional WhatsApp transport)

> Reworked 29 Sep 2026 -- see the change log at the end of this file. The
> section below is the original Step 7 build, with the gate corrected to match
> the current behaviour (prepare first, deliver on request).

### `backend/app/services/alerts.py`

- **Message content** (TC3, unchanged): patient code, name, phone, category,
  risk level + score, ended reason, key symptoms (label + severity + red-flag
  mark), every answer with transcript and interpretation, provider call id,
  timestamp.
- **Gate** (`prepare_alert`, was `maybe_send_alert`): only `high` risk produces
  anything (TC2: low/medium -> `skipped`, no message, no HTTP call).
  HIGH -> `ready` (message built + stored, **nothing sent**) by default; with
  `ALERT_DELIVERY=whatsapp` the message is also POSTed, giving
  `sent (<id>)` / `failed: ...` / `not_configured`. Unconfigured or disabled
  still **stores the message** -- it is only the transport that is missing.
- **Transport** (`send_whatsapp_alert`, opt-in) = the operator's exact Zernio
  test code, productionised:
  `POST https://zernio.com/api/v1/inbox/conversations/{id}/messages` with
  `Authorization: Bearer <ZERNIO_API_KEY>` and
  `{"accountId": ..., "message": <text>}`.
- **Sandbox isolation**: the alert thread is addressed by
  `ZERNIO_ALERT_CONVERSATION_ID` + `ZERNIO_INBOX_ACCOUNT_ID` (your connected
  WhatsApp on sandbox **+1 202 908 7457**) -- deliberately separate env vars
  from `FROM_NUMBER` (the real toll-free voice line). Voice and alerts never
  share configuration.
- Failures never raise out of the call flow: the status becomes
  `failed: ...` (or `not_configured`) and is stored on the row together with
  the message.

### Config additions (`.env.example` documents all)

`DATABASE_URL`, `ALERTS_ENABLED`, `ALERT_DELIVERY`,
`ZERNIO_INBOX_ACCOUNT_ID`, `ZERNIO_ALERT_CONVERSATION_ID`.

## Tests (`test_step6.py` + `test_step7.py`, 19 offline tests)

- Step 6 TC1: row with transcript/risk/timestamps; TC2: link by patient_code
  (and by phone as fallback); TC3: survives a simulated restart (engine cache
  dropped, file reopened).
- Edge cases: unlinked dial keeps the number; low risk rows still persist;
  URL resolution defaults.
- API: patient upsert (create then update), 401 without the key.
- Full-flow: the mocked Step 5 `run_call` now writes a high-risk row.
- Step 7 TC1: HIGH -> correct URL/headers/payload, message id stored;
  TC2: low/medium -> no HTTP; TC3: content assertions; failure ->
  `failed: ...`; not-configured/disabled gates; full-flow send + skip.

## Verification

```
backend/.venv/Scripts/python.exe -m pytest backend/tests -q
141 passed, 1 deselected  (122 pre-existing + 19 new)
```

## Notes / limitations

- **No live call was made** -- dialing stays off until the operator says so.
  The manual verification sequence (3-4 calls total for the whole app) is in
  `backend/live-test-plan.md`.
- SQLite is the right store for this single-instance deployment; the SQLModel
  models port to Postgres later by changing `DATABASE_URL`.
- With the default `ALERT_DELIVERY=ready` nothing leaves the backend: the
  message sits on the row until a human sends it (dashboard Copy button). A
  retry queue can be added on top of `attach_alert` when a channel exists.
- WhatsApp rate limit applies per recipient (fine: one alert per call, one
  care-team recipient).

---

## Change log -- 29 Sep 2026 (alerts reworked: prepare first, deliver on request)

**Why.** Delivery rode on the Zernio *inbox sandbox* thread, which is a test
surface rather than a hospital channel, so nothing in the demo could depend on
a message actually leaving the backend. The alert *content* was the valuable
part, so the content is now guaranteed and the transport is optional.

**What changed**

| | before | now |
|---|---|---|
| HIGH-risk call | build text -> POST to the sandbox | build text -> store on the row (`alert_status = ready`) -> *optionally* POST |
| transport switch | none | `ALERT_DELIVERY` = `ready` (default) \| `whatsapp` |
| unconfigured / disabled | `not_configured`, message lost | `not_configured`, **message still stored** |
| call row | `alert_status`, `alert_detail` | + `alert_message` (full text) |
| dashboard | "sent / not configured" | call detail shows the message with a **Copy** button; Calls column chip = Ready to send / Sent / Not sent; cards add `alerts_ready` |

**Code**

- `services/alerts.py`: `prepare_alert(record, patient=None, settings)` returns
  an `AlertOutcome(status, detail, message)`; `delivery_mode()` normalises
  `ALERT_DELIVERY` (unknown value -> warn + `ready`, never send); the old
  `maybe_send_alert` remains as a status-only wrapper.
- `db/models.py`: `CallRecord.alert_message` (added by the existing
  `_sync_new_columns` ALTER-TABLE sync -- no data migration needed).
- `db/service.py`: `attach_alert(..., message=...)` stores it;
  `stats()` now also counts `alerts_ready` (and matches `sent (<id>)`, which
  the old exact-match check never actually counted).
- `services/call_flow.py`: `_persist_and_alert()` calls `prepare_alert` and logs
  `Alert for record N: status=... (detail) -- N-char message stored`.
- `api/records.py` exposes `alert_message`; `api/dashboard.py` exposes
  `alerts.delivery` + a channel label that matches the mode.
- Frontend: `CallDetail` renders the prepared message (Copy), `CallsPage` shows
  the alert chip, `types.ts` carries `alert_message` / `alerts_ready`.

**Tests** (`test_step7.py`, rewritten around the two modes): default mode
prepares and never calls the provider; `whatsapp` mode still posts and stores
the same text; provider rejection, unconfigured, disabled and unknown-mode
cases; full `run_call` flows for a HIGH call (`ready` + message on the row),
a HIGH call with the transport on (`sent (msg_live)`), and a low call
(`skipped`, no message).

---

## Change log -- 2 Oct 2026 (WhatsApp conversation wired up, live-verified)

**`get-info.py` (ops script, deliberately outside the app).** It opens the care
team's WhatsApp conversation with a Zernio *template* message -- WhatsApp only
allows a business-initiated thread through an approved template -- and prints
the conversationId to paste into `.env`. It is now env-driven
(`ZERNIO_INBOX_ACCOUNT_ID`, `ALERT_WHATSAPP_TO`, `ZERNIO_ALERT_TEMPLATE`),
fails loudly instead of printing `None`, and prints the exact `.env` lines plus
the suggested template text. **The app never opens a conversation itself**: a
window only lives 24 hours, so that is an operator decision, not a per-call one.

**Two messages, not one.** `format_alert_message` (full: every answer,
transcript, risk reasons) is what the row stores and the dashboard shows.
`format_alert_brief` -- **6 lines / ~240 chars** -- is what actually goes out,
because a 20-line WhatsApp message is unreadable on a phone:

```
🚨 VoiceCare — HIGH RISK
P-0003 · Nimal Perera
+94766697286 · surgical
Check-in 29 Sep 06:21 · score 16
Symptoms: bleeding [RED FLAG], breathlessness [RED FLAG], pain (severe) +1 more
Call them back now. Full transcript: dashboard → Calls.
```

**Three bugs found while wiring it up**

1. `ZERNIO_INBOX_ACCOUNT_ID` / `ZERNIO_ALERT_CONVERSATION_ID` had **never been
   read**. Pydantic maps `inbox_account_id` to `INBOX_ACCOUNT_ID`, so following
   our own `.env.example` silently produced "WhatsApp not configured". Fixed with
   `AliasChoices` (both the `ZERNIO_*` and the short names work).
2. Every send logged `id=unknown`: Zernio answers
   `{"success": true, "data": {"messageId": ...}}` and the code only read the
   top level. Now parsed from the envelope (live id:
   `wamid.HBgLOTQ3NjY2OTcyODYVAgARGBJBQzhDRjI1QjEyNzJGMEVGODAA`).
3. `format_alert_brief` first read findings as objects; `get_findings()` returns
   JSON **dicts**, so the real send path raised `AttributeError`. Caught by
   previewing the message before sending; now regression-tested.

**Also:** a 400/404/410 from a closed window now says *"re-open it with
`python get-info.py`"*, and startup logs
`Alerts: enabled (delivery=whatsapp, conversation=6aaadf93...)` plus a warning
when `whatsapp` is selected but not configured.

**Live verification (one real message, sent to the care-team number):**
`sent (wamid.HBgLOTQ3NjY2OTcyODYVAgARGBJBQzhDRjI1QjEyNzJGMEVGODAA)`, with the
full 679-char message stored on the row.

**Tests** (`test_step7.py`, `test_step9.py`, `conftest.py`): brief-vs-full
content, findings-as-dicts, no-findings case, nested envelope id, closed-window
hint, both dashboard channel labels, and the re-send endpoint. `conftest.py` now
**pins** `ALERT_DELIVERY`/`ZERNIO_*` so the suite can never follow -- or act on --
the developer's local `.env`.

---

## Change log -- 2 Oct 2026 (send by default + "Send alert now")

A live call scored HIGH and the row said **Ready to send** instead of **Sent**.
Two causes, one of them a config-cache trap:

1. **The backend had not been restarted.** `get_settings()` is `@lru_cache`d at
   startup, so a running process keeps the old `ALERT_DELIVERY=ready` from
   before the `.env` was updated. Any `.env`/code change here needs a restart.
2. **The default was `ready`.** It is now **`whatsapp`**: a HIGH-risk call
   alerts the care team automatically, and `ready` is the deliberate
   store-only fallback for when no conversation is configured.

**"Send alert now"** (`POST /records/calls/{id}/alert`, nurse|doctor|admin).
Alerts normally go out at the end of the call, but a row can still end up
`ready` / `failed` / `not_configured` -- the 24 h window closed, the backend was
mid-restart, delivery was off. The full message is already on the row, so this
re-runs *prepare + deliver* for that row (no second call to the patient, no new
risk assessment). Returns the new status; 404 for an unknown id, **409 for a
non-HIGH call** (alerts are HIGH-only, and silently re-alerting a low call would
be worse than a clear refusal). The dashboard shows the button on any HIGH-risk
row that has a stored message, next to Copy.

Startup now logs `Alerts: enabled (delivery=whatsapp, conversation=6aaadf93...)`
and warns when `whatsapp` is selected but not configured, so this state is
visible in the first three lines of the log instead of being discovered in the
dashboard.
