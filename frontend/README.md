# VoiceCare -- Staff Dashboard (frontend)

Vite + React 19 + TypeScript (strict) + Tailwind CSS 4 + react-router 7.

```
npm install        # once
npm run dev        # dev server on http://localhost:5173
npm run build      # tsc --noEmit + production build to dist/
```

Talks to the backend on `http://localhost:8000` (override with
`VITE_API_URL=http://host:port`). Log in with the staff account seeded from
`backend/.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`).

See `backend/step-9-readme.md` for the full screen map and the live
test checklist.