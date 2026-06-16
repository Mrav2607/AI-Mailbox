# AI Mailbox

Monorepo scaffold containing the API service and a placeholder for the web app.

- API path: `apps/api`
- Web path: `apps/web` (placeholder)

## Getting Started

1. Start Postgres and Redis (Docker Desktop required)

```powershell
cd .\deploy
docker compose up -d db redis
```

2. Install API deps and run migrations

```powershell
cd apps\api
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e .
$env:DATABASE_URL = "postgresql+psycopg://user:pass@localhost:5432/ai_mailbox"
alembic upgrade head
```

3. Run API locally

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Alternative: from `apps/api/app` you can run `python -m app.main` (module form keeps imports working).

4. Seed demo data (optional)
```powershell
cd ..\..\scripts
python .\seed_demo_data.py
```

Health check: http://localhost:8000/api/v1/health

### Triage demo data
- Run the seed script above to create a demo user and messages.
- Use the returned/known demo user email `demo@example.com` with `/api/v1/auth/demo-login` to get the `user_id`.
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

### Rate-limited LLM classification (background)
- Queue classification to respect Gemini free-tier limits (5 RPM):
  `POST /api/v1/mail/classify/queue?user_id=<uuid>&limit=25&delay_seconds=12`
- Run a Celery worker in another terminal:
  `cd apps/api; celery -A app.workers.celery_app.celery_app worker -l info`

## Notes for local API usage

- Use `/api/v1/auth/demo-login` to create a dev user record (stores only email + display name).
- Pass `user_id` (UUID from the demo login response) to `/api/v1/mail/triage`, `/api/v1/mail/thread/{id}`, and `/api/v1/analytics/overview` to query data.
