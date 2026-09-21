# VoiceCare LK -- backend (Step 1: telephony connectivity, rebuilt)

The scratch probes that proved the original telephony leg now live in
`_archive/`. This is the real implementation following the root README's
target structure:

```
backend/
|-- main.py             # FastAPI app: GET /health, POST /calls
|-- stream_server.py    # WS /media-stream: live call audio from Zernio
|-- outbound_call.py    # Zernio API client (places the calls)
|-- media_auth.py       # per-call tokens guarding the WS endpoint
|-- config.py           # all settings from the single .env
|-- logging_setup.py    # shared logging config
|-- transcribe_call.py  # Step 2 scratch (rebuilt in Step 2)
|-- tests/              # offline pytest suite (no real calls)
`-- _archive/           # old probe scripts, reference only
```

## What a call does now

1. `POST /calls` (guarded by `X-Api-Key`) asks Zernio to dial the patient.
2. The request to Zernio carries the survey-informed greeting and a
   **one-time random token** appended to `forwardTo`:
   `wss://.../media-stream?token=<random>`.
3. Zernio dials, plays the greeting, then streams live audio to the WS.
4. `stream_server.py` validates the token, logs `event=start` with the
   media format (expected **PCMA / 8000 Hz / mono**), counts frames,
   receives `event=stop`, and disconnects cleanly.
5. On disconnect the buffered audio is still written to
   `recordings/call_<id>.wav` as 16-bit PCM (kept from the proven Step 2
   probe) so it can be played back / transcribed next.

## Setup (one-time)

    cd backend
    python -m venv .venv                      # already exists here
    .venv\Scripts\python -m pip install -r requirements.txt

`backend/.env` already exists with your real Zernio values; `.env.example`
is the committed template documenting every key:

| Variable | Meaning |
|---|---|
| `ZERNIO_API_KEY` | Bearer token for the Zernio API (secret) |
| `FROM_NUMBER` | Number Zernio dials from |
| `PUBLIC_WSS_URL` | `wss://<ngrok-domain>/media-stream` |
| `CALLS_API_KEY` | Long random string; `POST /calls` needs `X-Api-Key: <this>` |
| `HOSPITAL_NAME` | Spoken in the opening line (default VoiceCare LK) |
| `GREETING_TEXT` | Optional override of the default opening line |
| `ALLOWED_CALL_PREFIXES` | E.164 prefixes we may dial (default `+94`) |
| `MEDIA_STREAM_REQUIRE_TOKEN` | Reject WS connections without a valid token |
| `TEST_TO_NUMBER` | Default number when `POST /calls` omits `phone_number` |
| `BACKEND_HOST` / `BACKEND_PORT` | For `python main.py` (default `127.0.0.1:8000`) |
| `LOG_LEVEL` | Default INFO |
| `RECORDINGS_DIR` | Default `backend/recordings` |

## Manual end-to-end test (TC1-TC4 of Step 1)

1. Start ngrok:  `ngrok http 8000`
   (if the domain differs from `PUBLIC_WSS_URL`, update it in `.env`)
2. Start the backend:
   `.venv\Scripts\python -m uvicorn main:app --port 8000`
3. Place the call (PowerShell, from `backend/`):

```powershell
$env:CALLS_API_KEY = "<paste CALLS_API_KEY from .env>"
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/calls `
  -Headers @{ "X-Api-Key" = $env:CALLS_API_KEY } `
  -ContentType "application/json" `
  -Body '{"phone_number": "+94766697286"}'
```

   (omit `-Body` to use `TEST_TO_NUMBER`)

4. Answer the phone -> greeting plays -> console shows
   `event=start ... media_format={'encoding': 'PCMA', ...}`.
5. Hang up -> console shows `event=stop` and `Media stream closed`, and
   `recordings/call_<id>.wav` appears (Step 2 bridge).

## Offline tests (no calls, no cost)

    .venv\Scripts\python -m pytest tests/ -v

Covers: `/health`; `POST /calls` auth, number validation (E.164 + prefix),
and the exact payload sent to Zernio (stubbed); token issuance/revocation;
WS rejection without/with an unknown token; a full simulated call that
writes a valid 8 kHz mono 16-bit WAV and consumes the token.

## Security posture for this step

- `.env` is git-ignored (`.gitignore` at repo root); `.env.example` has
  placeholders only.
- `POST /calls` requires `X-Api-Key`, compared with `secrets.compare_digest`
  (timing-safe). An empty `CALLS_API_KEY` disables dialing entirely (503).
- `/media-stream` requires a fresh cryptographically random per-call token;
  missing/unknown/expired tokens are rejected before the socket is accepted.
  Tokens expire after 10 minutes and are revoked when the call ends.
- Outbound dialing is restricted to `ALLOWED_CALL_PREFIXES` (default `+94`)
  so a leaked key can't dial arbitrary international numbers.
- The server binds to `127.0.0.1` only; ngrok is the single public entry.
- Secrets are never logged: API keys are masked at startup, and the stream
  URL is logged with the token redacted (`token=***`).
- Provider-supplied call IDs are sanitized before being used in filenames.

## Notes

- The first backend restart invalidates outstanding stream tokens (they are
  in-memory by design at this step); just re-trigger the call.
- If you ever see `Rejected media-stream connection ... token` in the logs
  during a live call, set `MEDIA_STREAM_REQUIRE_TOKEN=false` temporarily to
  check whether the provider strips query params from `forwardTo`, and tell
  Kavin -- the per-call token is the main new security mechanism.
- Step 2 (batch transcription) is next: `transcribe_call.py` is still the
  old scratch version and will be rebuilt there.
