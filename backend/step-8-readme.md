# Step 8 -- Follow-up call scheduler (the cronjob) -- COMPLETE

Manual calls from the frontend are this demo's primary path (Step 9). The
cronjob is nevertheless fully built -- it just sleeps behind one switch:

## The master switch: `SCHEDULE_CALLS_ENABLED` (backend/.env, default false)

| Value | Behaviour |
|---|---|
| `false` (what this demo ships with) | The backend NEVER dials on its own. `GET /schedule` still computes every patient's real next-call time (the dashboard column is live data) and `POST /schedule/run-now` returns the dial plan while placing **zero** calls. |
| `true` | An in-process APScheduler job wakes every `SCHEDULE_INTERVAL_MINUTES` (30), finds patients whose check-in is due, and dials them through exactly the same cost rails as a manual call. |
| `SCHEDULE_DRY_RUN=true` | Logs the plan, dials nothing (works with either value above). |

## How "due" is computed

`discharge_date + SCHEDULE_CHECKIN_DAYS (3,7,14,30)` at `SCHEDULE_HOUR:00`
local time = one check-in slot. Per slot: **scheduled** (future) -> **due**
(slot day arrived) -> **overdue** (later days); a call already made on/after
the slot day marks it **completed**. Only slots inside the
`SCHEDULE_GRACE_DAYS` (2) catch-up window are ever dialed -- an older slot is
never fired late (a surprise call weeks later would alarm patients), and
`SCHEDULE_MAX_DIALS_PER_TICK` (2) caps each tick (the rest is deferred).

## Files

- `app/services/scheduler.py` -- `compute_schedule()` (pure maths: powers the
  dashboard column AND the tick, so they can never disagree), `plan_due()`,
  `run_tick()`, `_dial()` (shared cost rails), `start()/stop()/status()`
  (APScheduler interval job + one immediate tick on startup).
- `app/api/schedule.py` -- `GET /schedule` (full timeline), `GET /schedule/due`,
  `GET /schedule/status` (human note: "Automatic calls are OFF..."),
  `POST /schedule/run-now` (admin role or API key).
- `/health` reports `automatic_calls_enabled` + `scheduler_running`.
- Dependencies pinned: `apscheduler>=3.11,<4` (plus `pyjwt` for Step 9).

## Why a forgotten switch cannot run up a bill

1. switch off -> forced dry-run (plan only) -- proven by test;
2. hourly/daily/concurrency rails re-checked before every provider dial;
3. `SCHEDULE_MAX_DIALS_PER_TICK` per tick;
4. grace window excludes stale slots;
5. `SCHEDULE_DRY_RUN` for safe experimentation;
6. `run-now` requires the admin role (403 for nurse/doctor).

## Tests -- `tests/test_step8.py` (16, all offline; provider dial mocked)

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

## Verify live (zero cost)

1. Backend running -> `GET /schedule/status` (with `X-Api-Key`) shows
   `"enabled": false` and the OFF note.
2. Dashboard -> Schedule tab: banner "Automatic calls: OFF", real next
   check-in times per patient; as admin, "Run one tick now" -> the confirm
   dialog says no call will be placed, result shows `dialed=0`.
3. Only if/when you WANT live auto-dialing: set `SCHEDULE_CALLS_ENABLED=true`
   and restart the backend -- that is the single switch that arms it.