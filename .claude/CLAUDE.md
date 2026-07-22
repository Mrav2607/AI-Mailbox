# CLAUDE.md — review calibration & project directives

Structure, commands, and style live in root `AGENTS.md` — don't restate them. This file exists to make automated reviews precise: what to flag, what is deliberate, and how work flows here. One correction to AGENTS.md: the frontend DOES have a test runner now (`cd apps/web && npx vitest run`, lib-only tests).

## Review standards

- Only report findings with a concrete failure scenario (inputs/state → wrong outcome). Cite `file:line`. Rank by severity. No style commentary — ruff and oxlint are enforced; if they pass, style is settled.
- Verify a suspected bug against actual call sites before reporting it. Most "bugs" here are documented invariants (below).
- Don't propose rewrites of working code, cursor pagination, new dependencies, or component-render test infra (none exists; web tests are lib-module tests by design).
- Tests are deterministic and offline: MagicMock/monkeypatch doubles, compiled-statement string assertions, no DB or network fixtures. Do NOT request integration/DB tests; the orchestrator runs live migration round-trips separately.

## Deliberate designs — do not flag these as defects

- **Sync FastAPI handlers** (incl. sync `httpx` calls): they run in the threadpool on purpose.
- **Offset pagination with `id DESC` tiebreak** on triage/search; `has_more` inferred from `items.length === limit`; `/mail/counts` is the only total source. Keyset cursors were evaluated and rejected (nullable `last_message_at`, O(10²–10³) rows).
- **Self-scoping account filters**: unknown/non-owned `provider_account_id` yields an empty result, never 404 — this is not an IDOR; rows are always user-scoped first. Same for ingest targeting silently ignoring unknown ids (benign disconnect race).
- **Enumeration resistance**: auth endpoints return uniform 200s; account-existence branching happens worker-side (email template choice). Uniform responses are intentional, not missing error handling.
- **NULL-only password promote**: verify-email writes a password only onto `password_hash IS NULL`; reset-password is the only overwrite path. Flag any change that weakens this — it is the account-takeover guard.
- **Token hygiene**: single-use tokens are SHA-256-at-rest, consumed via atomic `DELETE … RETURNING`; raw tokens travel only in URL fragments and are scrubbed client-side before any network call. The per-process random `_DUMMY_HASH` exists for timing parity and must never be able to authenticate.
- **Email iframe sandbox** has no `allow-scripts`/`allow-same-origin`; heights are viewport/pane-derived because content can never be measured. Don't suggest auto-sizing.
- **Per-account sync single-flight** via partial unique index; `deduplicated: true` responses are the losing-racer contract, not an error.
- **New-mail pill is unfiltered** (whole mailbox) even when an account filter is active — by design, so arriving mail on other accounts isn't hidden.
- **Paused accounts are skipped** by ingest/scheduler even when explicitly requested — reconnect is the only fix; queuing them burns quota.
- **Partial indexes live in migrations only** (0009 precedent), never mirrored to models — not drift.
- **`db.commit()` in route handlers**: sessions are request-scoped; this is the repo's transaction style.

## Things that SHOULD be flagged

- Any weakening of: production config validation (`RESEND_API_KEY`, explicit `EMAIL_FROM`, absolute `https://` `FRONTEND_BASE_URL`), OAuth state binding/PKCE, cross-user Gmail conflict 409s, or the `token_version` revocation path.
- Logging that could leak raw tokens, passwords, OAuth codes, or refresh tokens (the single `[dev-mail]` log line is the one sanctioned exception).
- New thread/message/sync-run writes missing `provider_account_id`, or queries filtering only by `user_id + provider` where account identity matters.
- Missing per-item failure isolation in any new fan-out loop (mirror `dispatch_scheduled_syncs`: rollback + log + continue).
- Migrations without preflight guards (RuntimeError + remediation SQL) or without a working downgrade.
- Anything committed under `docs/` or `local/` (gitignored; the remote is PUBLIC), or secrets/`.env` content in any tracked file.

## Workflow directives

- **Orchestrator-worker model**: implementation is done by path-scoped subagents in waves against a frozen contract written in the plan; each agent refuses work outside its file boundary. The orchestrator reviews diffs, routes fixes back to the owning agent, re-runs full suites itself (never trusts agent-reported results), and makes one commit per wave.
- **Verification gate before any commit**: `apps/api`: `ruff check . && pytest`; `apps/web`: `npx oxlint src && npx vitest run && npm run build`. New migrations additionally need a live upgrade → downgrade → upgrade round-trip.
- **Commits**: Conventional Commits, ≤50-char imperative subject, no AI attribution, footers, or session links.
- **PRs**: open as drafts; body calls out migrations and deploy-gate env changes explicitly. Never merge without the repo owner's explicit approval — merging `main` auto-deploys to the production VM via GitHub Actions and runs pending Alembic migrations there.
- **Review follow-ups**: reply on the finding's thread with the fix commit; trivial fixes may be applied directly, anything substantial goes through a subagent with the same verification gate.
