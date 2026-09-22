# Step 9 -- Staff dashboard (Vite + React + TypeScript + Tailwind) -- COMPLETE

One shared dashboard, three roles (nurse | doctor | admin), no patient
logins. Login screen follows `designs/73ef74e7-...png`; the app shell follows
the sidebar/cards sketch `designs/245c1bfb-...png`; icons/logos come from
`designs/icons/` (copied into `frontend/public/`). Brand gradient
`#4C8C5C -> #2F6142`, ink `#1F3D28` (read from the SVG logos).

## Backend (tested: `tests/test_step9.py` -- 12 tests)

- `app/core/security.py` -- HS256 JWTs (PyJWT) + PBKDF2-HMAC-SHA256
  passwords (200k rounds). `require_auth` accepts **either** the machine
  `X-Api-Key` **or** a staff bearer token: the browser never needs the
  backend API key, and existing scripts keep working. `require_roles(...)`
  states each endpoint's rule in one line.
- `app/api/auth.py` -- `POST /auth/login` (generic 401 for bad id OR
  password; 503 when no secret is configured), `GET /auth/me` (session
  restore), `GET /auth/roles` (permission matrix the UI hides/shows with),
  `GET/POST /auth/staff` (admin only; 409 duplicate, 422 bad role).
- `app/api/dashboard.py` -- `GET /dashboard/summary` returns the whole
  landing screen in one round trip (6-card stats, recent calls, open alerts,
  due patients, scheduler + cost-rail state), plus `/dashboard/activity`.
- `app/api/records.py` -- `PATCH /records/calls/{id}` for the workflow:
  nurse/doctor/admin may review + annotate; **close_case = doctor|admin**
  (403 otherwise, attributed via `closed_by`). Patient CRUD is admin-only.
- `app/api/calls.py` -- Call Now accepts a staff token; **bug fixed this
  step**: number resolution now happens *after* the patient lookup, so a
  `patient_code`-only dial (exactly what the button sends) works even when
  `TEST_TO_NUMBER` is empty.
- Seeding: the first admin is created on startup from
  `ADMIN_USERNAME`/`ADMIN_PASSWORD` when the staff table is empty.
  `JWT_SECRET` falls back to `CALLS_API_KEY`, so a fresh checkout can log in.

Role matrix (`GET /auth/roles`): view/call/review -> all three roles;
`close_case` -> doctor+admin; `manage_patients`/`manage_staff`/
`manage_scheduler` -> admin.

## Frontend (`frontend/` -- Vite 6 + React 19 + TypeScript strict + Tailwind 4 + react-router 7)

| Screen | Design / requirement |
|---|---|
| `LoginPage` | green gradient card, logo, role tabs (placeholder only -- the server decides the role), hospital, Employee ID, password, "Login to Dashboard", forgot-password hint |
| `Layout` | dark sidebar (logo, nav dividers, **Logout** bar pinned bottom), light top bar with profile circle right, drawer under `lg` (TC4) |
| `DashboardPage` | the sketch's **6 green cards** (StatCard) from `/dashboard/summary` + recent-calls table + open alerts + due list with a Call shortcut |
| `CallsPage` | **TC2** risk colour badges (red/amber/green) + filter chips; rows expand to transcript answers, findings, risk reasons + review/note/close actions (role-gated) |
| `PatientsPage` | admin register form + **Call now** button behind a ConfirmDialog that names the number and warns about billing (**TC3** cost guard) |
| `SchedulePage` | **Step 8**: master-switch banner (ON/OFF + note), full timeline with next check-in per patient, admin "Run one tick now" (confirm dialog says plainly whether it can dial) |
| `StaffPage` | admin creates the nurse/doctor logins TC1 exercises |

Guards: `RequireAuth`/`RequirePerm` routes + `can(permission)` buttons from
the role matrix; every money-spending action requires an explicit confirm.

## Run it

```
# terminal 1 -- backend
cd backend && .venv/Scripts/python -m uvicorn app.main:app --port 8000

# terminal 2 -- dashboard
cd frontend && npm install && npm run dev     # http://localhost:5173
```

Login: `admin` / the `ADMIN_PASSWORD` from `backend/.env` (seeded on
backend start). CORS already allows `http://localhost:5173`; the API base
defaults to `http://localhost:8000` and can be overridden with
`VITE_API_URL`.

## Verification

- Backend: `pytest tests/test_step9.py` -> 12 pass (login x3 roles, matrix,
  401/503, role gating, call-now with mocked dial, review/close rules,
  summary/activity payload, admin seed). Full suite: **169 passed**.
- Frontend: `npm run build` (`tsc --noEmit` strict + vite) passes.
- Live test cases (login x3 roles, colours, **one** real Call-Now, mobile
  width) are in `backend/live-test-plan.md` section 7 -- zero extra calls
  beyond the existing plan (Call B is placed from the dashboard instead of
  curl).

## Demo simplifications (deliberate)

- The login hospital dropdown is informational (single-hospital demo) and
  the role tabs only prefill the Employee-ID placeholder.
- "Forgot password" shows an admin-contact hint (no mail server in a demo).
- Token in localStorage is fine for the demo; production would use an
  httpOnly cookie.