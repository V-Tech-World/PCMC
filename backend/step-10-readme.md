# Staff email alerts (Step 10 backend)

HIGH-risk calls are emailed to the patient's ASSIGNED care team *in addition
to* the Step 7 WhatsApp alert:

- patient has an assigned care team -> those nurse(s) AND doctors (always)
- patient has nobody assigned       -> NO email; an explicit `no_assignment`
                                       note lands on the call row

There is no score threshold: who to wake up is a care-team decision made when
the patient is registered (Patients screen -> Care team checkboxes; the same
control edits the team). Admins are never emailed. Delivery is Gmail SMTP +
App Password (`SENDER_EMAIL` / `GOOGLE_APP_PASSWORD`), one message per
recipient (no CC/BCC), styled HTML + plain-text fallback. Every recipient's
outcome is stored on the call row (`alert_recipients_json`) and surfaces in
Calls + Dashboard.

## Setup

1. Gmail -> Security -> 2-Step Verification -> App passwords.
   Put the 16-char code in `backend/.env` as `GOOGLE_APP_PASSWORD`
   (never commit `.env`; `.env.example` documents every key).
2. `SENDER_EMAIL` = the same Gmail account; restart the backend.
   Startup logs confirm the channel: `Staff email alerts: ON (from=..., ...)`.
3. Create staff on the Staff screen with a valid email address
   (`POST /auth/staff` rejects a missing/malformed email with 422).

## Contracts

- New/high accounts without a usable email are refused (422).
- Non-super-admin accounts with no usable email are purged at startup
  (idempotent; logged as a warning). `admin` survives regardless.
- Care-team assignment is validated at save time: unknown usernames,
  non-nurse/doctor roles, and deactivated accounts are refused (422), so a
  typo can never silently mean "nobody gets the alert".
- The WhatsApp path is untouched: email disabled/unconfigured/failed never
  blocks it, and the alert text is always stored on the row.
- TCs live in `tests/test_step10.py` (assignment routing, the assignment API
  on create + edit, content, failure isolation, recipient recording, account
  rules). `tests/conftest.py` pins the suite hermetic:
  `EMAIL_ALERTS_ENABLED=false` + empty credentials.
