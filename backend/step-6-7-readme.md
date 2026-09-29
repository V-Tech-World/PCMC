# Step 6 + 7 -- Database (SQLModel + SQLite) + HIGH-risk alerts -- COMPLETE

Steps 6 and 7 of the VoiceCare LK backend, built together: every call is now
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

## Step 7 -- WhatsApp alerts (Zernio sandbox)

### `backend/app/services/alerts.py` (new)

- Delivery = the operator's exact Zernio test code, productionised:
  `POST https://zernio.com/api/v1/inbox/conversations/{id}/messages` with
  `Authorization: Bearer <ZERNIO_API_KEY>` and
  `{"accountId": ..., "message": <text>}`.
- **Sandbox isolation**: the alert thread is addressed by
  `ZERNIO_ALERT_CONVERSATION_ID` + `ZERNIO_INBOX_ACCOUNT_ID` (your connected
  WhatsApp on sandbox **+1 202 908 7457**) -- deliberately separate env vars
  from `FROM_NUMBER` (the real toll-free voice line). Voice and alerts never
  share configuration.
- **Gate** (`maybe_send_alert`): only `high` risk sends (TC2: low/medium ->
  `skipped`, no HTTP call). Unconfigured/disabled -> `not_configured`.
- **Message content** (TC3): patient code, name, phone, category, risk level
  + score, ended reason, key symptoms (label + severity + red-flag mark),
  every answer with transcript and interpretation, provider call id,
  timestamp.
- Failures never raise out of the call flow: status becomes `failed: ...`
  and is stored on the row.

### Config additions (`.env.example` documents all)

`DATABASE_URL`, `ALERTS_ENABLED`, `ZERNIO_INBOX_ACCOUNT_ID`,
`ZERNIO_ALERT_CONVERSATION_ID`.

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
