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

```powershell
Copy-Item deploy\.env.example .env
```

   The app reads this `.env` automatically. For local runs, make sure `DATABASE_URL`
   points at `localhost` (the example uses `db`, which is the Docker-internal host) and
   that its user/password match `POSTGRES_USER` / `POSTGRES_PASSWORD` in the same file.

2. Create the virtual environment and install the API:

```powershell
cd apps\api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
cd ..\..
```

   To use the local encoder classifier (`CLASSIFIER_BACKEND=local`), also install
   the optional ML dependencies (torch + transformers):

```powershell
cd apps\api; pip install -e ".[local-classifier]"; cd ..\..
```

3. Apply database migrations (needs Postgres running — see step 1 of the daily startup):

```powershell
$env:DATABASE_URL = "postgresql+psycopg://user:pass@localhost:5432/ai_mailbox"
cd apps\api; alembic upgrade head; cd ..\..
```

## Daily startup

1. Start Postgres and Redis (Docker Desktop must be running):

```powershell
docker compose -f deploy\docker-compose.yml up -d db redis
```

2. Open a terminal and activate the venv:

```powershell
.\apps\api\.venv\Scripts\Activate.ps1
```

3. Run the API:

```powershell
cd apps\api
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health check: http://localhost:8000/api/v1/health
Interactive API docs: http://localhost:8000/docs

> Alternative to step 3: from `apps/api/app` you can run `python -m app.main`.

### Triage demo data (optional, local only)
- The demo-data seed scripts live under `scripts/` (git-ignored) and are **not**
  required to run the app — the primary data path is Gmail ingest (below).
- If you have a seed script locally, run it to create a demo user and messages,
  then use the demo email `demo@example.com` with `/api/v1/auth/demo-login` to
  get the `user_id`.
- Call `/api/v1/mail/triage?user_id=<uuid>` to see the seeded threads and classifications.

### Gmail OAuth (dev)
1. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` in `.env`.
2. Create a demo user via `POST /api/v1/auth/demo-login` and copy the `user_id`.
3. Visit `/api/v1/auth/google/start?user_id=<uuid>` to get an `auth_url` and complete consent.
4. After redirect, the callback will store a Gmail provider account and tokens in `provider_account`.

### Gmail ingest (dev)
1. Ensure Gmail OAuth is connected and you have a `provider_account` for the user.
2. Trigger ingest: `POST /api/v1/mail/ingest/gmail?user_id=<uuid>&max_results=25`.
3. Fetch triage: `GET /api/v1/mail/triage?user_id=<uuid>`.
4. If some threads are missing classifications, run backfill:
   `POST /api/v1/mail/classify/backfill?user_id=<uuid>&limit=100`.

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

- Use `/api/v1/auth/demo-login` to create a dev user record (stores only email + display name).
- Pass `user_id` (UUID from the demo login response) to `/api/v1/mail/triage`, `/api/v1/mail/thread/{id}`, and `/api/v1/analytics/overview` to query data.
