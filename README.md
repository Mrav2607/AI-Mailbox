# CortexMail

AI-assisted email triage: ingests Gmail, classifies each message into a 6-label
taxonomy (`needs_reply`, `action_required`, `fyi`, `promotional`,
`security_alert`, `spam`) with a locally fine-tuned encoder model, and serves
triage buckets over a FastAPI service.

- API path: `apps/api`
- Web path: `apps/web` (React + Vite console and landing page)
- Model training: `ml/` (see [Email classification](#email-classification))

## Quick start (Docker)

The fastest way to run the whole app — API, Celery worker, Postgres (pgvector),
Redis, and the web UI — is Docker Compose. From the repo root:

```bash
cp deploy/local.env.example deploy/.env    # once; ready to boot as-is
docker compose up --build
```

Then open **http://localhost:8080**, click **Sign in**, and use the **demo
login** box (email only, no Google account needed). A new demo user starts with
a handful of sample threads across every bucket, so the console has something in
it before you connect real mail. Everything is served from one origin — nginx
serves the SPA and proxies `/api` to the API, so there's no CORS to configure.

> Use `deploy/local.env.example`, not `deploy/.env.example`. The latter is the
> production template: it sets `APP_ENV=production`, which makes the API refuse
> to start until you supply real secrets, and disables the demo login.

What comes up:

| Service   | Purpose                              | URL / port |
|-----------|--------------------------------------|------------|
| `web`     | React SPA + `/api` reverse proxy     | http://localhost:8080 |
| `api`     | FastAPI service                      | http://localhost:8000 |
| `worker`  | Celery worker (Gmail ingest, backfill) | — |
| `migrate` | one-shot `alembic upgrade head`, then exits | — |
| `db`      | Postgres 15 + pgvector               | localhost:5432 |
| `redis`   | Celery broker + OAuth state          | localhost:6379 |

The `migrate` service runs first and the app waits on health checks, so a single
`docker compose up` comes up clean with no ordering races.

**Classifier:** fetch the fine-tuned local model first — it's a one-time
download, costs nothing per message, works offline, and no mail content
leaves the machine. It's git-ignored (~256MB, trained on private data), so it
ships as chunked assets on the `model-v2` GitHub Release. Fetch it (needs the
[GitHub CLI](https://cli.github.com), `gh auth login`), then build with the
torch deps:

```bash
./fetch-model.sh    # downloads + unpacks into ./models/email-classifier
INSTALL_LOCAL_CLASSIFIER=true CLASSIFIER_BACKEND=auto docker compose up --build
```

Without a model installed, the shipped default (`CLASSIFIER_BACKEND=auto`,
no torch, no keys) degrades to the keyword `heuristic` backend — no deps
required, which is enough to test the app end to end but gives worse results.

Prefer an LLM over downloading 256MB? Set `GEMINI_API_KEY` and
`CLASSIFIER_BACKEND=llm` instead — that's an ongoing per-message cost, so the
local model is the better default for most self-hosters.

**Gmail (optional):** to ingest real mail, fill `GOOGLE_CLIENT_ID` /
`GOOGLE_CLIENT_SECRET` in `deploy/.env` and register
`http://localhost:8080/auth/google/callback` in your Google Cloud OAuth client.

> The sections below cover running the API **directly on the host** (venv +
> `uvicorn --reload`) for API development. For just trying the app, the Docker
> quick start above is all you need.

> **Local-only paths:** `models/`, `data/`, and `scripts/` are git-ignored. The
> trained model (~256MB to download), the training/eval datasets (real email
> content), and the
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

   To use the local encoder classifier (`CLASSIFIER_BACKEND=auto`), also install
   the optional ML dependencies (torch + transformers):

```bash
cd apps/api; pip install -e ".[local-classifier]"; cd ../..
```

3. Apply database migrations (needs Postgres running — see step 1 of the daily startup):

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/cortexmail"
cd apps/api; alembic upgrade head; cd ../..
```

## Daily startup

1. Start Postgres and Redis (Docker Desktop must be running):

```bash
docker compose up -d db redis
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

### Triage demo data (local only)
Signing in through the demo login on a dev deployment seeds a new user with
sample threads automatically, so there's nothing to run by hand.

To seed an account that already exists (or to top up after clearing the
database):

```bash
# Docker stack (the quick start above)
docker compose exec api python -m app.scripts.seed_demo you@example.com

# Host API (the "Daily startup" flow, where only db and redis run in Docker).
# Needs the venv active and DATABASE_URL set, same as running the API.
cd apps/api && python -m app.scripts.seed_demo you@example.com
```

It's idempotent — running it twice won't duplicate the sample inbox. The
threads hang off a placeholder connection with no refresh token, so both the
scheduled sync and a manual one skip it and never reach Gmail.

The demo login only works when `APP_ENV` is one of `dev`, `development`,
`local`, `test`, `testing`, or `ci`. Every other value — including `staging` —
counts as production, where the route 404s and the sign-in screen hides the
button.

### Gmail OAuth (dev)
1. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` in `.env`.
2. `GET /api/v1/auth/google/start` to get an `auth_url`, then complete consent.
3. The callback creates/links the Gmail account, stores tokens in
   `provider_account`, and returns an `access_token` to use for the calls below.
   The provider OAuth tokens are encrypted at rest (see `TOKEN_ENCRYPTION_KEY`).

### Gmail ingest (dev)
All calls below require the `Authorization: Bearer <token>` header.
1. Ensure Gmail OAuth is connected for the signed-in user.
2. Trigger ingest: `POST /api/v1/mail/ingest?max_results=25`.
3. Fetch triage: `GET /api/v1/mail/triage`.
4. If some threads are missing classifications, run backfill:
   `POST /api/v1/mail/classify/backfill?limit=100`.

### Triage buckets
- `bucket=needs_reply` filters to items classified as needs_reply.
- `bucket=unclassified` filters to items with no classification yet.
- `bucket=all` returns all threads regardless of label.

## Email classification

Each ingested message is classified into one of six labels: `needs_reply`,
`action_required`, `fyi`, `promotional`, `security_alert`, `spam`. The
deployment-wide backend is chosen by `CLASSIFIER_BACKEND` in `.env` (both
deploy templates and the code default ship `auto`). The canonical values:

- `auto` — an opted-in user's own saved key is tried **first**, before the
  local encoder is even attempted. Everyone else gets the local encoder
  (loaded from `CLASSIFIER_MODEL_PATH`, default `models/email-classifier`;
  needs the `local-classifier` extra) if it can serve; if torch or the model
  files are missing, or the encoder can't produce a verdict, it falls through
  to an LLM — the operator's `GEMINI_API_KEY` if one is set (keyword-heuristic
  fallback on failure), otherwise the heuristic directly. If the model
  directory contains a `calibration.json`, the encoder applies that confidence
  calibration at load time (absent file = raw confidences; a dir whose
  `config.json` marks `"calibration_required": true` refuses to serve
  uncalibrated).
- `llm` — the same routing as `auto` minus the local-encoder step: an
  opted-in user's own key first, else the operator's `GEMINI_API_KEY` with a
  keyword-heuristic fallback.
- `heuristic` — keyword rules only, no extra dependencies, no LLM calls at
  all. This is the one value that **doesn't** honor a saved key: it returns
  before an opted-in user's routing is even read.

`local` and `gemini` are deprecated aliases for `auto` and `llm` (still
accepted, mapped at startup with a deprecation warning, scheduled for
removal — move off them when you touch this file). Per-run overrides (the
console's backfill picker, or `backend=` on the classify endpoints) accept a
different, stricter `local`: only an *explicit* per-run `backend="local"` is
guaranteed to never reach an LLM even when the encoder can't serve (it ends
at the heuristic instead) — the global default, in every other routing
state, does reach an LLM. Per-run also has `local_then_llm` ("local encoder,
LLM on failure"), the per-run analogue of `auto`'s non-opted-in path; it
isn't a valid value for `CLASSIFIER_BACKEND` itself.

The trained model is **not** committed (`models/` is git-ignored) — fetch it
from the `model-v2` release with `./fetch-model.sh`, or train your own with the
pipeline in `ml/` (`python ml/train_classifier.py ...`, which enforces
train/eval disjointness via `--guard-files`) and score it with
`python ml/eval_classifier.py --model <dir> --data <eval.jsonl>` (per-class
report, confusion matrix, confidence and calibration metrics).
`ml/fit_calibration.py` fits an optional post-hoc confidence calibration and
writes the `calibration.json` the serving path understands — but only after
its holdout gate passes: without `--holdout` the run is report-only and no
file is written.

## Agenda (action extraction)

An optional second stage on top of classification: messages labeled
`needs_reply` or `action_required` get one extra LLM call that pulls out the
concrete obligation — what to do, what kind of task it is (reply, payment,
signature, form, RSVP, deadline, other), the due date, and any amount. Results
land in the **Agenda** view (press `0` in the console): one deadline-ordered
board — Overdue / Today / This week / Later / No deadline — across every
connected Gmail and Outlook account. Mark items done with `e` or dismiss with
`x`; resolving a thread as done resolves its items too.

It is **off by default**. To enable, set both in `.env`:

```dotenv
ACTION_EXTRACTION_ENABLED=true
GEMINI_API_KEY=...            # required — there is no non-LLM fallback
```

With the flag off (or no key) nothing runs and nothing is billed. When
enabled, extraction happens automatically after each ingest that brings in new
mail, plus a background recovery pass every 15 minutes. To extract from mail
that arrived before you enabled it, run a backfill from the command palette
("extract actions"), or:

```http
POST /api/v1/mail/actions/backfill?limit=100&since_days=30
```

Cost stays bounded: only already-classified action mail is considered, each
message is extracted at most once per classification verdict (retried at most
3 times on failure), and sweeps are capped per run.

### Who pays: per-user API keys (BYOK)

Each user can store their own LLM credential, so extraction is billed to them
and not to the server's key. Open **AI settings** from the command palette,
the accounts menu, or the Agenda's empty state, then pick a provider, paste a
key, and name a model. The key is encrypted at rest and never sent back to the
browser — only its last 4 characters are shown. **Test** makes one real call
so a bad key fails here instead of silently later.

Supported providers, all through the OpenAI-compatible chat API:

| Provider | Example model |
|---|---|
| OpenAI | `gpt-4o-mini` |
| Gemini | `gemini-2.5-flash` |
| OpenRouter | `openai/gpt-4o-mini` |
| Groq | `llama-3.3-70b-versatile` |
| Mistral | `mistral-small-latest` |

Anthropic is not in this list on purpose. Its OpenAI-compatible endpoint
accepts the request but ignores the JSON response format this feature relies
on, so replies aren't guaranteed to be machine-readable. Nothing bad gets
stored — the parser rejects anything malformed — but the calls would be paid
for and thrown away, which reads like flaky extraction. Native support is a
possible follow-up.

#### Using your key for sorting too

By default a saved key only powers the Agenda. There's a second checkbox,
**"Also use my key to sort incoming mail"**, that extends it to
classification — the step that files each message into needs-reply, action
required, promotional and so on.

It's a separate choice on purpose. Sorting runs on **every email you
receive**, while the Agenda only looks at the small slice already marked as
needing action, so turning this on uses far more of your quota. It's off
until you tick it.

Two things to know:

- **Preset providers only.** Custom endpoints aren't offered here yet — the
  safety check that runs before every request to a custom URL costs a DNS
  lookup, which is fine once per action but not once per email.
- **Your key goes first, not the built-in model.** On any deployment set to
  `CLASSIFIER_BACKEND=auto` or `llm`, checking this box means your key is
  tried *before* the local encoder is ever attempted — not "only used when
  the encoder can't". Every email you receive is sent to your provider. The
  checkbox is only inert (and hidden) on a `heuristic` deployment, where no
  LLM is used at all.

If your key ever stops being usable for sorting, the settings panel says so
— and, unless you've also checked **"If your LLM fails, classify with the
built-in model instead"**, that mail is left **unclassified**, not
heuristically labelled. It never quietly falls back to the server's key.

Two settings control the rest:

```dotenv
# When true (default), users WITHOUT their own key fall back to the server's
# GEMINI_API_KEY. Set false so only users with their own key get extraction.
ACTION_EXTRACTION_SERVER_FALLBACK=true

# Let users point at a custom endpoint. The server sends their key to whatever
# URL they save, so this is off by default. When on, only https addresses on
# the public internet are accepted.
LLM_CUSTOM_ENDPOINTS_ENABLED=false

# On TOP of the flag above: also allow plain http and private/LAN addresses
# (for example a local Ollama). This lets any signed-in user make the server
# reach addresses inside your network — only turn it on if you trust every
# user of this deployment.
LLM_PRIVATE_ENDPOINTS_ENABLED=false
```

If a user's custom endpoint stops being allowed (you turned a flag off, or its
address now resolves somewhere private), their extraction stops and the
settings panel says so. It deliberately does **not** fall back to the server's
key — they chose where their mail gets sent, so quietly redirecting it and
billing you instead would be the wrong default.

## Notes for local API usage

- Use `/api/v1/auth/demo-login` to create a dev user and get an `access_token`.
- Send `Authorization: Bearer <access_token>` on data endpoints
  (`/api/v1/mail/*`, `/api/v1/analytics/overview`, `/api/v1/auth/connections`);
  the user is derived from the token, so no `user_id` is passed. In the
  interactive docs (`/docs`), click **Authorize** and paste the token.

## License

MIT — see [LICENSE](LICENSE).
