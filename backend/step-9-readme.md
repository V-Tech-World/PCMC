# Step 9 -- Staff dashboard (Vite + React + TypeScript + Tailwind) -- COMPLETE

One shared dashboard, three roles (nurse | doctor | admin), no patient
logins. Login screen follows `designs/73ef74e7-...png`; the app shell follows
the sidebar/cards sketch `designs/245c1bfb-...png`; icons/logos come from
`designs/icons/` (copied into `frontend/public/`). Brand gradient
`#4C8C5C -> #2F6142`, ink `#1F3D28` (read from the SVG logos).

## Backend (tested: `tests/test_step9.py` -- 14 tests)

- `app/core/security.py` -- HS256 JWTs (PyJWT) + PBKDF2-HMAC-SHA256
  passwords (200k rounds). `require_auth` accepts **either** the machine
  `X-Api-Key` **or** a staff bearer token: the browser never needs the
  backend API key, and existing scripts keep working. `require_roles(...)`
  states each endpoint's rule in one line.
- `app/api/auth.py` -- `POST /auth/login` (generic 401 for bad id OR
  password; 503 when no secret is configured), `POST /auth/reset-password`
  (public self-service: employee ID + new/confirm password -- generic 404 for
  unknown/deactivated IDs, 422 mismatch or shorter than 6 chars; **no admin
  approval and no third-party mail/SMS call**), `GET /auth/me` (session
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
| `LoginPage` | green gradient card, logo, role tabs (placeholder only -- the server decides the role), hospital, Employee ID, password, "Login to Dashboard"; "Forgot password?" swaps to a self-service reset (employee ID + new password + confirm, no admin/third party); the card keeps light, fully readable colours even in dark mode (`.login-card`) |
| `Layout` | dark sidebar (logo, nav dividers, **Logout** bar pinned bottom), light top bar with profile circle right, drawer under `lg` (TC4) |
| `DashboardPage` | the sketch's **6 green cards** (StatCard) from `/dashboard/summary` + recent-calls table + open alerts + due list with a Call shortcut |
| `CallsPage` | **TC2** risk colour badges (red/amber/green) + filter chips; rows expand to transcript answers, findings, risk reasons, the **prepared alert message (Copy)** + review/note/close actions (role-gated) |
| `PatientsPage` | admin register form **and per-row Edit** (same endpoint, pre-filled; the code stays the record key), discharge type incl. `respiratory` / `diabetic`, language + active toggle, plus the **Call now** button behind a ConfirmDialog that names the number and warns about billing (**TC3** cost guard) |
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

- Backend: `pytest tests/test_step9.py` -> 14 pass (login x3 roles, matrix,
  401/503, role gating, call-now with mocked dial, review/close rules,
  summary/activity payload, admin seed, self-service password reset x2).
  Full suite: **175 passed**.
- Frontend: `npm run build` (`tsc --noEmit` strict + vite) passes.
- Live test cases (login x3 roles, colours, **one** real Call-Now, mobile
  width) are in `backend/live-test-plan.md` section 7 -- zero extra calls
  beyond the existing plan (Call B is placed from the dashboard instead of
  curl).

## Demo simplifications (deliberate)

- The login hospital dropdown is informational (single-hospital demo) and
  the role tabs only prefill the Employee-ID placeholder.
- "Forgot password" is deliberately self-service (employee ID + new password
  only -- no admin approval, no mail/SMS provider): it matches the demo brief
  and costs nothing, but a production system would verify identity out-of-band
  (email/OTP) first. Unknown IDs get one generic 404 so the form can't be used
  to probe which employee IDs exist.
- Token in localStorage is fine for the demo; production would use an
  httpOnly cookie.

---

## Change log -- 29 Sep 2026 (patient edit, alert message, dark-mode audit)

**1. Edit a patient record.** Each row now has an **Edit** button (admin only,
`can("manage_patients")`). It opens the existing form pre-filled and switches
it to edit mode:

- the form posts to the same `POST /records/patients` -- the backend matches on
  `patient_code`, so it updates the row in place (`status: "updated"`) and can
  never create a duplicate;
- the **code is read-only while editing** (it is the record key, and old call
  rows point at it) with an inline note;
- the form gained the two fields the API always accepted but the UI never
  showed: **preferred language** (en/ta/si) and **active** (archive instead of
  delete);
- Cancel closes and resets; the form scrolls itself into view on edit.
- Backend: category validation now reads `dialogue.CATEGORIES` instead of a
  hard-coded tuple, so a new discharge type can't be rejected by the API.
- Tests: `test_admin_can_edit_an_existing_patient`,
  `test_patient_category_must_be_a_known_discharge_type`.

**2. The prepared alert message is visible.** A HIGH-risk call stores its alert
text (`alert_status = "ready"`); the expanded call row now shows it in an amber
panel with a **Copy** button (clipboard API, with select-the-text as fallback),
and the Calls table's Alert column is a chip: *Ready to send* / *Sent* /
*Not sent* / quiet for `skipped`. See `backend/step-6-7-readme.md`.

**3. Dark-mode audit (the "row turns light" bug).** The login card already had
`.login-card { color-scheme: light; color: #262626 }`; the same class of bug
existed elsewhere, where a light Tailwind surface had no `dark:` counterpart:

| where | what was wrong | fix |
|---|---|---|
| `CallsPage` expanded row | `bg-brand-50` with no dark variant -- the open row went white (the reported bug) | `bg-brand-50 dark:bg-brand-900/30` |
| `CallDetail` risk-reasons / symptoms panels | `bg-red-50` / `bg-amber-50` + dark text, no dark variants | `dark:bg-red-950/40` / `dark:bg-amber-950/40` + lighter text |
| `DashboardPage` open-alerts list | `bg-red-50` + `text-red-800`, no dark variants | dark variants added |
| `SchedulePage` status pills | all four `STATUS_STYLES` + the fallback pill were light-only | dark variants added |
| `StaffPage` role pill, `PatientsPage` active/inactive pills | light-only | dark variants added |
| 4 table rows | malformed `dark:hover:bg-white/5/50` (not a real class, so no hover feedback) | `dark:hover:bg-white/5` |

Rule of thumb now applied everywhere: **any light surface class gets a `dark:`
sibling in the same class string** (solid brand/red buttons with `text-white`
and decorative `bg-white/10` glows are the only intentional exceptions, as is
the deliberately light login card).