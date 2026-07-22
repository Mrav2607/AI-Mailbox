# Repository Guidelines

## Project Structure & Module Organization

`apps/api/` contains the FastAPI service. Keep endpoints in `app/routes/`, configuration and security in `app/core/`, models and schemas in `app/db/`, business logic in `app/services/`, and Celery jobs in `app/workers/`. Migrations live in `apps/api/alembic/versions/`; tests are in `apps/api/tests/`.

`apps/web/` is the React + TypeScript + Vite client. Page orchestration starts in `src/App.tsx`, reusable UI belongs in `src/components/`, and clients, hooks, types, and utilities in `src/lib/`. Static assets go in `public/`. Model training is under `ml/`; deployment files are under `deploy/`. Local `data/`, `models/`, and `scripts/` are git-ignored.

## Build, Test, and Development Commands

- `cp deploy/.env.example deploy/.env && docker compose up --build` starts the complete stack at `http://localhost:8080`.
- `cd apps/api && pip install -e ".[dev]"` installs the API and developer tools.
- `cd apps/api && uvicorn app.main:app --reload` runs the API locally.
- `cd apps/api && pytest` runs the API test suite; use `pytest tests/test_auth.py -q` for a focused run.
- `cd apps/api && ruff check . && mypy app` runs Python linting and type checks.
- `cd apps/web && npm ci && npm run dev` starts the frontend; `npm run build` type-checks and builds it, while `npm run lint` runs Oxlint.
- `cd apps/api && alembic upgrade head` applies database migrations.

## Coding Style & Naming Conventions

Use four-space indentation and `snake_case` for Python modules and functions; type new interfaces. In TypeScript, follow the existing two-space indentation, double quotes, and semicolons. Name React components and files in `PascalCase`; prefix hooks with `use`. Keep route handlers thin and reusable behavior in services.

## Testing Guidelines

Pytest is the automated test framework. Name files `test_<feature>.py` and tests `test_<behavior>`. Keep tests deterministic and offline; fake Redis, OAuth, and other external services. Add regression coverage for changed API behavior. Frontend has a test runner now (`cd apps/web && npx vitest run`, lib-only tests).

## Commit & Pull Request Guidelines

Follow the repository's Conventional Commit pattern: `feat(web): add filter`, `fix(ingest): handle MIME body`, or `test(auth): cover expiry`. Keep commits focused and imperative. Pull requests should explain the user-visible change, list verification commands, link relevant issues, call out migrations or configuration changes, and include screenshots for UI work. Never commit `.env` files, OAuth credentials, mailbox data, or model artifacts.
