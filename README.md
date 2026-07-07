# AI Mailbox

AI-assisted email triage: ingests Gmail, classifies each message into a 6-label
taxonomy (`needs_reply`, `action_required`, `fyi`, `promotional`,
`security_alert`, `spam`) with a locally fine-tuned encoder model, and serves
triage buckets over a FastAPI service.

- API path: `apps/api`
- Web path: `apps/web` (placeholder)
- Model training: `ml/` (see [Email classification](#email-classification))

> **Local-only paths:** `models/`, `data/`, and `scripts/` are git-ignored. The
> trained model (~1 GB), the training/eval datasets (real email content), and the
> one-off data-prep scripts live on your machine, not in the repo. None of them
> are required to *run* the API — the model is loaded at serve time if present,
> and the classifier falls back gracefully when it isn't.

## One-time setup

1. Create your `.env` at the repo root by copying the example, then fill in real values
   (Postgres credentials, and `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` for Gmail OAuth):

```bash
cp deploy/.env.example .env
```

   The app reads this `.env` automatically. For local runs, make sure `DATABASE_URL`
   points at `localhost` (the example uses `db`, which is the Docker-internal host) and
   that its user/password match `POSTGRES_USER` / `POSTGRES_PASSWORD` in the same file.

2. Create the virtual environment and install the API:

```bash
cd apps/api
python -m venv .venv
source .venv/bin/activate
pip install -e .
cd ../..
```

   To use the local encoder classifier (`CLASSIFIER_BACKEND=local`), also install
   the optional ML dependencies (torch + transformers):

```bash
cd apps/api; pip install -e ".[local-classifier]"; cd ../..
```

3. Apply database migrations (needs Postgres running — see step 1 of the daily startup):

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/ai_mailbox"
cd apps/api; alembic upgrade head; cd ../..
```

## Daily startup

1. Start Postgres and Redis (Docker Desktop must be running):

```bash
docker compose -f deploy/docker-compose.yml up -d db redis
```

2. Open a terminal and activate the venv:

```bash
source apps/api/.venv/bin/activate
```

3. Run the API:

```bash
cd apps/api
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Liveness check: http://localhost:8000/api/v1/health (process is up)
Readiness check: http://localhost:8000/api/v1/ready (pings Postgres + Redis;
returns 503 if either is unreachable)
Interactive API docs: http://localhost:8000/docs

> Alternative to step 3: from `apps/api/app` you can run `python -m app.main`.

### Authentication
All data endpoints require a session token. Sign in to get one, then send it as
an `Authorization: Bearer <token>` header on every request. Unauthenticated or
expired requests get `401`.

- **Dev login:** `POST /api/v1/auth/demo-login` with `{"email": "you@example.com"}`
  returns `{ access_token, token_type, user }`. It verifies no password, so it is
  dev-only.
- **Real login (Gmail):** `GET /api/v1/auth/google/start` returns an `auth_url`;
  complete consent and the callback returns an `access_token` for that Google
  account (the user is created on first sign-in).

Tokens are HS256 JWTs signed with `API_SECRET` and expire after
`ACCESS_TOKEN_EXPIRES_MINUTES` (default 7 days).

A browser frontend must be served from an origin listed in `CORS_ORIGINS`
(comma-separated; defaults cover `http://localhost:3000` and
`http://localhost:5173`). Other origins are blocked by the browser.

### Triage demo data (optional, local only)
- The demo-data seed scripts live under `scripts/` (git-ignored) and are **not**
  required to run the app — the primary data path is Gmail ingest (below).
- If you have a seed script locally, run it to create a demo user and messages,
  then `POST /api/v1/auth/demo-login` with that user's email to get a token.
- Call `GET /api/v1/mail/triage` (with the bearer token) to see the seeded
  threads and classifications.

### Gmail OAuth (dev)
1. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` in `.env`.
2. `GET /api/v1/auth/google/start` to get an `auth_url`, then complete consent.
3. The callback creates/links the Gmail account, stores tokens in
   `provider_account`, and returns an `access_token` to use for the calls below.
   The provider OAuth tokens are encrypted at rest (see `TOKEN_ENCRYPTION_KEY`).

### Gmail ingest (dev)
All calls below require the `Authorization: Bearer <token>` header.
1. Ensure Gmail OAuth is connected for the signed-in user.
2. Trigger ingest: `POST /api/v1/mail/ingest/gmail?max_results=25`.
3. Fetch triage: `GET /api/v1/mail/triage`.
4. If some threads are missing classifications, run backfill:
   `POST /api/v1/mail/classify/backfill?limit=100`.

### Triage buckets
- `bucket=needs_reply` filters to items classified as needs_reply.
- `bucket=unclassified` filters to items with no classification yet.
- `bucket=all` returns all threads regardless of label.

## Email classification

Each ingested message is classified into one of six labels: `needs_reply`,
`action_required`, `fyi`, `promotional`, `security_alert`, `spam`. The backend is
chosen by `CLASSIFIER_BACKEND` in `.env`:

- `local` (default) — a fine-tuned encoder loaded from `CLASSIFIER_MODEL_PATH`
  (default `models/email-classifier`). Needs the `local-classifier` extra. If
  torch or the model files are missing, it falls back automatically to the
  gemini/heuristic path, so the API still runs without a trained model.
- `gemini` — Google Gemini (needs `GEMINI_API_KEY`), with a keyword-heuristic
  fallback.
- `heuristic` — keyword rules only, no extra dependencies.

The trained model is **not** committed (`models/` is git-ignored). To produce
one, train it with the pipeline in `ml/` (`python ml/train_classifier.py ...`)
and point `CLASSIFIER_MODEL_PATH` at the output directory. Until then, run with
`CLASSIFIER_BACKEND=heuristic` (zero deps) or `gemini` (with an API key).

## Notes for local API usage

- Use `/api/v1/auth/demo-login` to create a dev user and get an `access_token`.
- Send `Authorization: Bearer <access_token>` on data endpoints
  (`/api/v1/mail/*`, `/api/v1/analytics/overview`, `/api/v1/auth/connections`);
  the user is derived from the token, so no `user_id` is passed. In the
  interactive docs (`/docs`), click **Authorize** and paste the token.
