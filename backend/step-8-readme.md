# Step 8 -- Follow-up call scheduler (the cronjob) -- COMPLETE

Manual calls from the frontend are this demo's primary path (Step 9). The
cronjob is nevertheless fully built -- it just sleeps behind one switch:

## The master switch: `SCHEDULE_CALLS_ENABLED` (backend/.env, default false)

| Value | Behaviour |
|---|---|
| `false` (what this demo ships with) | The backend NEVER dials on its own. `GET /schedule` still computes every patient's real next-call time (the dashboard column is live data) and `POST /schedule/run-now` returns the dial plan while placing **zero** calls. |
| `true` | An in-process APScheduler job wakes every `SCHEDULE_INTERVAL_MINUTES` (30), finds patients whose check-in is due, and dials them through exactly the same cost rails as a manual call. |
| `SCHEDULE_DRY_RUN=true` | Logs the plan, dials nothing (works with either value above). |

The switch can also be flipped **from the dashboard** (Schedule tab, admin
only): `POST /schedule/enabled` turns the cron on/off, the choice is persisted
in the `appsetting` table and re-applied on restart, and `.env` stays the
default for a fresh database. Arming never dials by itself (`tick_now=False`):
due patients are called on the next tick or an explicit "Run one tick now".

## How "due" is computed

`discharge_date + SCHEDULE_CHECKIN_DAYS (3,7,14,30)` at `SCHEDULE_HOUR:00`
local time = one check-in slot. Per slot: **scheduled** (future) -> **due**
(slot day arrived) -> **overdue** (later days); a call already made on/after
the slot day marks it **completed**. Only slots inside the
`SCHEDULE_GRACE_DAYS` (2) catch-up window are ever dialed -- an older slot is
never fired late (a surprise call weeks later would alarm patients), and
`SCHEDULE_MAX_DIALS_PER_TICK` (2) caps each tick (the rest is deferred).

## The 24h per-patient cooldown (`SCHEDULE_MIN_HOURS_BETWEEN_CALLS`, default 24)

Every dial -- automatic AND manual (`POST /calls` Call Now) -- stamps the
patient's last-dial time in the `appsetting` table. The automatic tick then
skips that patient until the window has passed: a call today at 2 PM means the
tick will not re-dial them until after 2 PM the next day, no matter how often
the cron wakes. The patient stays visible in the plan/due board with
`cooldown_hours` ("next auto-dial in Nh") and a `"cooldown"` tick outcome, so
the dashboard can never disagree with the tick. Manual calls are **never**
blocked -- this knob only paces the automatic path.

## Files

- `app/services/scheduler.py` -- `compute_schedule()` (pure maths: powers the
  dashboard column AND the tick, so they can never disagree), `plan_due()`
  (annotates each due item with `cooldown_hours`), `run_tick()` +
  `record_dial_stamp()`/`cooldown_hours_left()` (24h per-patient cooldown),
  `_dial()` (shared cost rails), `start()/stop()/status()`
  (APScheduler interval job + one immediate tick on startup).
- `app/api/schedule.py` -- `GET /schedule` (full timeline, annotated with
  `cooldown_hours` per patient), `GET /schedule/due` (same annotation),
  `GET /schedule/status` (human note: "Automatic calls are OFF..."),
  `POST /schedule/run-now` (admin role or API key),
  `POST /schedule/enabled` (admin: runtime on/off, persisted in `appsetting`).
- `/health` reports `automatic_calls_enabled` + `scheduler_running`.
- Dependencies pinned: `apscheduler>=3.11,<4` (plus `pyjwt` for Step 9).

## Why a forgotten switch cannot run up a bill

1. switch off -> forced dry-run (plan only) -- proven by test;
2. hourly/daily/concurrency rails re-checked before every provider dial;
3. `SCHEDULE_MAX_DIALS_PER_TICK` per tick;
4. grace window excludes stale slots;
5. `SCHEDULE_DRY_RUN` for safe experimentation;
6. `run-now` and the on/off switch require the admin role (403 for nurse/doctor);
7. arming the switch from the UI never dials by itself -- the first automatic
   dial happens on the next tick (or an explicit run-now), through the same rails;
8. the 24h per-patient cooldown stamps every dial: a patient auto/manual-dialed
   today at 2 PM is skipped by the tick until 2 PM tomorrow -- the same patient
   can never be re-called 30 minutes later.

## Tests -- `tests/test_step8.py` (20, all offline; provider dial mocked)

- **TC1**: enabled tick dials the due patient (asserts dialed number,
  `patient_code` and `scheduled=true` config), cron starts/stops with
  `next_tick_at` populated.
- **TC2**: a not-yet-due patient is never planned/dialed even with the switch
  ON; a slot older than the grace window is never dialed.
- **TC3**: `next_call_at` computed from the discharge date (offsets
  3/7/14/30 at 09:00), due -> overdue progression, completed slots skipped
  and the next offset promoted.
- Safety: master-switch-off reports the plan with `dialed == 0`; dry-run;
  per-tick budget cap; rate-limit rail blocks the scheduled dial; schedule
  API board/due/status; run-now 403 nurse / 200 admin "no call placed";
  bad `at` timestamp -> 422; `/health` state.
- Runtime switch: `POST /schedule/enabled` 401 anonymous / 403 nurse / 200
  admin, arming places **no** call while a patient is due, cron starts+stops
  with the switch, choice persisted, and a saved value wins over `.env` after
  a restart (fresh DB -> `.env` default).
- Cooldown: same-afternoon re-tick after a 2 PM dial skips with a "cooldown"
  outcome (planned, provider untouched); dialling resumes at 2 PM the next
  day; a manual Call Now dial is never gated but starts the same window.

## Verify live (zero cost)

1. Backend running -> `GET /schedule/status` (with `X-Api-Key`) shows
   `"enabled": false` and the OFF note.
2. Dashboard -> Schedule tab: banner "Automatic calls: OFF", real next
   check-in times per patient; as admin, "Run one tick now" -> the confirm
   dialog says no call will be placed, result shows `dialed=0`.
3. Admin on the Schedule tab: flip the "Auto-dial" switch ON -> the danger
   confirm explains what will happen -> banner flips to ON + toast says it is
   saved; flip it straight back OFF (immediate, no confirm). Toggling never
   places a call by itself.
4. Only if/when you WANT live auto-dialing: leave the dashboard switch ON (it
   survives restarts) or set `SCHEDULE_CALLS_ENABLED=true` and restart --
   a saved dashboard choice wins over `.env`; a fresh database uses `.env`.