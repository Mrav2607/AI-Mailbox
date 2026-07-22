# CLAUDE.md — project directives

Structure, commands, and style live in root `AGENTS.md`; code-review calibration (what to flag, what is deliberate) lives in root `REVIEW.md`. Don't restate either — follow both.

## Workflow directives

- **Orchestrator-worker model**: implementation is done by path-scoped subagents in waves against a frozen contract written in the plan; each agent refuses work outside its file boundary. The orchestrator reviews diffs, routes fixes back to the owning agent, re-runs full suites itself (never trusts agent-reported results), and makes one commit per wave.
- **Verification gate before any commit**: `apps/api`: `ruff check . && pytest`; `apps/web`: `npx oxlint src && npx vitest run && npm run build`. New migrations additionally need a live upgrade → downgrade → upgrade round-trip.
- **Commits**: Conventional Commits, ≤50-char imperative subject, no AI attribution, footers, or session links.
- **PRs**: open as drafts; body calls out migrations and deploy-gate env changes explicitly. Never merge without the repo owner's explicit approval — merging `main` auto-deploys to the production VM via GitHub Actions and runs pending Alembic migrations there. A direct push to `main` also deploys.
- **Review follow-ups**: reply on the finding's thread with the fix commit; trivial fixes may be applied directly, anything substantial goes through a subagent with the same verification gate. Findings are judged against `REVIEW.md` before any fix is dispatched.
