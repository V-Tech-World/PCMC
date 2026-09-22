# Step 6 + 7 -- Database (SQLModel + SQLite) + WhatsApp Alerts -- COMPLETE

Steps 6 and 7 of the VoiceCare LK backend, built together: every call is now
persisted in SQLite (patients table + one row per call) and every **HIGH-risk
call pushes a WhatsApp alert to the care team** through the Zernio sandbox
inbox. 19 new offline tests; full suite: **141 passed, 1 deselected**.

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
- Alerts are sent once, right after the call, with the stored status on the
  row; a retry queue can be added on top of `attach_alert` if needed.
- WhatsApp rate limit applies per recipient (fine: one alert per call, one
  care-team recipient).
