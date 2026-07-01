# AI Mailbox — Web

Vite + React + TypeScript SPA for the AI Mailbox triage app — a high-density,
keyboard-driven "Command Center" for QA-ing the local email classifier. Talks to
the FastAPI backend in `../api`.

## Setup

```bash
npm install
cp .env.example .env   # set VITE_API_BASE_URL if the API isn't on localhost:8000
npm run dev            # http://localhost:5173
```

The API must be running (see `../api`) and its `CORS_ORIGINS` must include this
app's origin (the default dev value already does).

If `VITE_API_BASE_URL` is left unset, the app runs against in-memory **mock data**
that matches the real API shapes — useful for UI work without a live backend.

## Console

A three-pane console: bucket sidebar (the 6 labels + `all`/`unclassified` with
live counts), a thread list with per-row label chip + confidence bar, and a
detail pane with the model's prediction and inline reclassify controls.
Keyboard-first: `1`–`8` switch buckets, `j`/`k` move selection, `gg`/`G` jump to
ends, `c` cycles the confidence sort, `i`/`b`/`q` run ingest/backfill/queue,
`⌘K` opens the command palette, and `?` shows the shortcut cheatsheet.

## Scripts

- `npm run dev` — dev server with HMR
- `npm run build` — typecheck + production build to `dist/`
- `npm run lint` — oxlint
- `npm run preview` — serve the production build

## Auth

Sign in via dev demo-login (email only). The bearer token is kept in
`localStorage` and sent as `Authorization: Bearer` on every API call; on load it
is validated against `/auth/me`, and a `401` from any call signs you back out.
Google OAuth sign-in is the next slice.
