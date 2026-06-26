# AI Mailbox — Web

Vite + React + TypeScript SPA for the AI Mailbox triage app. Talks to the
FastAPI backend in `../api`.

## Setup

```bash
npm install
cp .env.example .env   # set VITE_API_BASE_URL if the API isn't on localhost:8000
npm run dev            # http://localhost:5173
```

The API must be running (see `../api`) and its `CORS_ORIGINS` must include this
app's origin (the default dev value already does).

## Scripts

- `npm run dev` — dev server with HMR
- `npm run build` — typecheck + production build to `dist/`
- `npm run lint` — oxlint
- `npm run preview` — serve the production build

## Auth

Sign in via dev demo-login (email only). The bearer token is kept in
`localStorage` and sent as `Authorization: Bearer` on every API call; on load it
is validated against `/auth/me`. Google OAuth sign-in is the next slice.
