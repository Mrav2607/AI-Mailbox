"""Persistence helpers for classification results and the action-extraction
claim/attempt state machine.

`upsert_classification` is a single upsert keyed on ``message_id`` so every
write path (inline ingest, backfill, and the Celery workers) stays race-safe
and idempotent: if two classifiers reach the same message concurrently, the
second updates the row instead of crashing on the unique constraint.

`claim_action_item` / `record_extraction` are the same idea applied to a
longer-lived attempt: a claim reserves ``message_id``'s row for one worker's
extraction call, and the matching record either settles it or -- fenced on
``claim_token`` -- no-ops if the claim has since expired and been stolen.
Neither commits; the caller (``extraction_run``) owns the transaction
boundary so the claim can release its lock before the (possibly slow) LLM
call runs, mirroring ``backfill.py``'s snapshot-then-release pattern.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import ActionItem, AppUser, Classification, UserLlmCredential
from app.services.nlp.extractor import ExtractedAction, NoAction

# A claim's lease: past this age, a fresh worker may steal a still-"pending"
# row (the one that claimed it is presumed dead -- crashed, killed, or lost
# its Celery lease).
PENDING_LEASE = timedelta(minutes=30)
MAX_ATTEMPTS = 3

# Categories where every retry with the SAME credential fails identically --
# a bad key (401), a forbidden model (403), or a retired/renamed model
# (404). `blocked_by_policy` is deliberately excluded: it reflects mutable
# deployment/DNS policy, not the credential itself, and policy recovery
# must not require a credential mutation to revive rows (plan:
# 2026-08-19-extraction-cost-hardening D2).
CREDENTIAL_CLASS_FAILURE_CATEGORIES = frozenset({"http_401", "http_403", "http_404"})

# Marks a label set by a human in the console rather than a model -- lets
# every write path (this module, the route, backfill) agree on the string
# without importing it from a route. `upsert_classification`'s write-time
# guard checks against this to decide whether a model path is allowed to
# clobber it.
OPERATOR_MODEL_VERSION = "user-override"

# Bounded snapshot cap for classification_feedback.input_text: the first
# FEEDBACK_TEXT_MAX chars of build_classification_text(...)'s output. Sized
# independently of either classifier's own cap (classifier.py's LLM path
# truncates at 3,000 chars as of D9; the local encoder's window is smaller
# still) -- 6,000 stays generous enough to cover what either one actually
# saw without storing unbounded email bodies forever.
FEEDBACK_TEXT_MAX = 6000


def upsert_classification(
    db: Session,
    *,
    message_id: UUID,
    label: str | None,
    confidence: float | None,
    rationale: str | None,
    model_version: str | None,
    overwrite_user_override: bool = False,
) -> str:
    """Insert a classification for ``message_id``, or overwrite the existing one.

    Does not commit -- the caller controls the transaction boundary so it can
    batch writes.

    ``overwrite_user_override`` gates whether this write is allowed to clobber
    an operator's manual label. Every model-driven path (backfill, both
    ingests, the Celery classify task) leaves it ``False``, which adds a
    ``WHERE classification.model_version IS DISTINCT FROM 'user-override'`` to
    the conflict update -- so a user override that lands mid-run survives even
    when the model path finishes after it. Only the reclassify route itself
    passes ``True``.

    Returns ``"written"`` if the row was inserted or updated, or
    ``"protected"`` if the conflict hit the override guard and nothing
    changed (distinguished via the statement's rowcount: 1 vs 0).
    """
    values = {
        "message_id": message_id,
        "label": label,
        "confidence": confidence,
        "rationale": rationale,
        "model_version": model_version,
    }
    stmt = insert(Classification).values(**values).on_conflict_do_update(
        constraint="uq_classification_message",
        set_={
            "label": label,
            "confidence": confidence,
            "rationale": rationale,
            "model_version": model_version,
        },
        where=(
            None
            if overwrite_user_override
            else Classification.model_version.is_distinct_from(OPERATOR_MODEL_VERSION)
        ),
    )
    # preserve_rowcount matters: without it, psycopg3 reports -1 (truthy!) for
    # INSERT..ON CONFLICT statements, and every guarded write would read as
    # "written" even when the override guard blocked it. Set on the statement
    # (not as an execute() kwarg) so callers and test fakes keep the plain
    # single-argument execute signature.
    result = db.execute(stmt.execution_options(preserve_rowcount=True))
    return "written" if result.rowcount else "protected"


def claimable_predicate(*, force: bool = False, model_version: str | None = None):
    """WHERE-clause fragment: is an EXISTING ``action_item`` row a valid claim
    target right now?

    Settled rows (``extracted`` / ``no_action``) are terminal; only
    ``ineligible`` (a reclassify-back must always be able to re-extract),
    ``failed`` under the attempt cap, and an expired ``pending`` lease under
    the cap remain retryable.

    ``force`` (plan: 2026-08-19-extraction-cost-hardening D5) widens this,
    but no longer to "any non-pending row" -- re-running a settled
    extraction for free is only worth paying for when the model that
    produced it has since changed:

    - ``failed`` rows are claimable regardless of the attempt cap (retrying
      failures is force's whole job).
    - ``extracted``/``no_action`` rows are claimable ONLY when the row's
      stored ``model_version`` differs from the CURRENT ``model_version``
      (``f"{provider}:{model}"``, threaded from the caller's resolved
      credential), via SQL ``IS DISTINCT FROM`` -- plain ``!=`` is
      NULL-poisoned and would silently exclude every historical row with no
      stored model_version, the opposite of what a one-time re-extraction
      needs. Same model -> not claimable even under force; the paid result
      stands.
    - a live ``pending`` claim is still NEVER stolen, forced or not -- it's
      simply absent from every disjunct below.
    """
    lease_cutoff = datetime.now(timezone.utc) - PENDING_LEASE
    base = or_(
        ActionItem.outcome == "ineligible",
        and_(ActionItem.outcome == "failed", ActionItem.attempts < MAX_ATTEMPTS),
        and_(
            ActionItem.outcome == "pending",
            ActionItem.last_attempted_at < lease_cutoff,
            ActionItem.attempts < MAX_ATTEMPTS,
        ),
    )
    if not force:
        return base
    return or_(
        base,
        ActionItem.outcome == "failed",
        and_(
            ActionItem.outcome.in_(("extracted", "no_action")),
            ActionItem.model_version.is_distinct_from(model_version),
        ),
    )


def reset_failed_extraction_attempts(db: Session, user_id: UUID) -> None:
    """D2 recovery: zero ``attempts`` on the caller's ``outcome='failed'``
    ``action_item`` rows, under the SAME guard-locked transaction as the
    credential mutation that's about to commit.

    A credential-class failure (D2's cap-jump in `record_extraction` below) leaves
    a row terminally capped at ``MAX_ATTEMPTS`` under the credential that
    just failed it -- the whole point is to stop burning calls on a
    credential known to be dead. But once the user actually FIXES that
    credential (a material PUT, an activate, or a first-create landing
    active), those rows must get a fresh claimable life, or D2's storm fix
    would trade "storms forever" for "silently stuck forever" instead.

    Callers gate this on what actually changed: a material rewrite or an
    activate that switches which credential is active, yes; a flag-only PUT,
    an inactive spare create, a single spare delete, or the kill switch, no
    -- none of those change anything about the credential a failed row was
    capped under, so resetting there would just re-spend calls against the
    exact same dead credential.
    """
    db.execute(
        update(ActionItem)
        .where(ActionItem.user_id == user_id, ActionItem.outcome == "failed")
        .values(attempts=0)
    )


def lock_guard_user(db: Session, user_id: UUID) -> None:
    """The single per-user serialization anchor (PR #64's D6 guard lock,
    ``llm_settings._lock_guard_user``): a ``SELECT ... FOR UPDATE`` on
    ``app_user`` that every STRUCTURAL credential mutation takes before
    reading or writing this user's credential rows.

    Defined here rather than in the routes module so this extraction-record
    path can take the SAME lock without services reaching up into routes --
    ``llm_settings.py`` delegates to this implementation instead of keeping
    its own copy. Used by ``record_extraction``'s D2 credential-identity
    check: taking this lock before re-reading the active credential is what
    stops a PUT/activate from committing between that read and the fenced
    cap-jump write it guards (the TOCTOU Codex's pass 3 review caught).
    """
    db.execute(select(AppUser.id).where(AppUser.id == user_id).with_for_update())


def _credential_class_cap_jump_attempts(
    db: Session,
    *,
    user_id: UUID | None,
    failure_category: str | None,
    credential_id: UUID | None,
    credential_revision: int | None,
) -> int | None:
    """The attempts value a credential-class failure should jump the row
    to, or ``None`` to leave ``attempts`` untouched (plan D2).

    ``None`` short-circuits whenever there's nothing to jump: a
    non-credential-class category (retryable or `blocked_by_policy`), or no
    ``user_id`` to check identity against (callers that don't care about D2
    at all -- most of this module's own test suite -- simply never pass
    ``failure_category``, so this stays a no-op for them, unchanged from
    before D2 existed).

    Identity check, ATOMIC with the caller's write: takes ``lock_guard_user``
    THEN re-reads the user's currently active credential, all in the SAME
    transaction ``record_extraction``'s fenced UPDATE commits -- so a
    PUT/activate can no longer land between the read and the write. Match
    (attempt's credential id+revision == the active row's) -> ``MAX_ATTEMPTS``,
    terminalizing the row under the credential that actually produced this
    failure. Mismatch -- a rotation raced this call -- -> ``0``, so the claim
    this attempt already spent doesn't strand the row capped under a
    credential nobody's using anymore; the row gets a fresh claimable life
    under whichever credential is active now (fenced on the still-owned
    claim token, same as any other `record_extraction` write).

    A ``None`` ``credential_id`` (the operator/fallback key, never a
    per-user row a PUT/activate can rotate) has nothing to compare identity
    against, so the cap always applies straight -- no rotation is possible
    without a row to rotate.
    """
    if failure_category not in CREDENTIAL_CLASS_FAILURE_CATEGORIES:
        return None
    if user_id is None:
        return None
    lock_guard_user(db, user_id)
    if credential_id is None:
        return MAX_ATTEMPTS
    current = db.execute(
        select(UserLlmCredential.id, UserLlmCredential.revision).where(
            UserLlmCredential.user_id == user_id, UserLlmCredential.is_active
        )
    ).first()
    if (
        current is not None
        and current.id == credential_id
        and current.revision == credential_revision
    ):
        return MAX_ATTEMPTS
    return 0


def _done_stamp_columns(thread_done: bool, now: datetime) -> dict:
    """Conditional status stamp shared by claim and record: a done thread
    resolves an ``open`` row to ``done`` at write time, but must never
    rewrite a row an operator already resolved (e.g. ``dismissed``) -- so the
    CASE reads the row's OWN pre-write status, never a constant.
    """
    if not thread_done:
        return {}
    return {
        "status": case((ActionItem.status == "open", "done"), else_=ActionItem.status),
        "status_at": case(
            (ActionItem.status == "open", now), else_=ActionItem.status_at
        ),
    }


def claim_action_item(
    db: Session,
    *,
    message_id: UUID,
    thread_id: UUID,
    user_id: UUID,
    thread_done: bool,
    force: bool = False,
    model_version: str | None = None,
) -> UUID | None:
    """Atomically claim ``message_id``'s ``action_item`` row for one
    extraction attempt.

    Returns a fresh ``claim_token`` on success, ``None`` if nothing is
    claimable right now (a live pending claim someone else owns). Does not
    commit. When ``thread_done`` and the row's existing ``status`` is
    ``"open"``, additionally stamps ``status="done"`` -- a worker crash
    between claim and record must never leave an open row on a done thread,
    where it would surface as an obligation after an auto-reopen. A forced
    reclaim of a dismissed row must preserve ``dismissed`` byte-for-byte.

    ``model_version`` (plan D5) is threaded straight to this claim's own
    ``claimable_predicate(force=force, model_version=model_version)`` --
    without it, the claim's WHERE clause would compare against ``NULL``
    (``IS DISTINCT FROM NULL``, true for any non-null stored value), a much
    WEAKER gate than the one the caller's candidate SELECT already applied
    to pick this row, and every settled row with SOME stored model_version
    would claim regardless of whether it actually differs from the current
    one. The candidate SELECT and this claim must agree on what "current"
    means, or a message the SELECT rejected as same-model could still claim
    here.
    """
    now = datetime.now(timezone.utc)
    token = uuid4()

    insert_values = {
        "message_id": message_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "outcome": "pending",
        "claim_token": token,
        "attempts": 1,
        "last_attempted_at": now,
    }
    if thread_done:
        # Fresh insert -- there is no prior status to preserve, so the
        # unconditional stamp (not the CASE used below) is correct here.
        insert_values["status"] = "done"
        insert_values["status_at"] = now

    insert_stmt = (
        insert(ActionItem)
        .values(**insert_values)
        .on_conflict_do_nothing(constraint="uq_action_item_message")
    )
    # D4 (plan: 2026-08-19-extraction-cost-hardening, D1's actual root
    # cause): preserve_rowcount matters here exactly like it does in
    # upsert_classification -- without it, psycopg3 reports -1 (truthy!)
    # for an INSERT ... ON CONFLICT DO NOTHING statement even when the
    # conflict path fired and ZERO rows were actually inserted. Two
    # concurrent first-ever claims on the SAME brand-new message (the
    # owner's two mail connections firing concurrent sweeps) would both
    # read a truthy rowcount here and BOTH believe they'd won the claim --
    # the loser gets handed a claim_token that doesn't correspond to any
    # row in the DB, spends a real wire call on it, and then fences out at
    # record time (0 rows updated) as bucket "skipped" carrying a real
    # failure_category. That's the exact prod storm signature: the
    # existing consecutive-FAILURE breaker never sees it (it's a "skipped"
    # bucket, not "failed"), so nothing bounds the loser's connection to a
    # handful of calls the way D3 now does for a genuine failure streak.
    # Repro: test_extraction_concurrency_integration.py's
    # test_two_concurrent_claims_on_a_brand_new_row_self_limits fails
    # without this line and passes with it.
    if db.execute(insert_stmt.execution_options(preserve_rowcount=True)).rowcount:
        return token

    # Someone already has a row for this message -- try to (re)claim it.
    update_values = {
        "outcome": "pending",
        "claim_token": token,
        "attempts": ActionItem.attempts + 1,
        "last_attempted_at": now,
        **_done_stamp_columns(thread_done, now),
    }
    claim_stmt = (
        update(ActionItem)
        .where(
            ActionItem.message_id == message_id,
            claimable_predicate(force=force, model_version=model_version),
        )
        .values(**update_values)
    )
    if db.execute(claim_stmt).rowcount:
        return token

    # Not claimable -- but if it's a pending claim that expired AT the
    # attempt cap, it's not retryable either, and nothing else ever settles
    # it without this: terminalize it to failed. Disjoint from the claim
    # predicate above (that one requires attempts < cap), so this never
    # steals a claim the update just above could have taken.
    terminalize_stmt = (
        update(ActionItem)
        .where(
            ActionItem.message_id == message_id,
            ActionItem.outcome == "pending",
            ActionItem.last_attempted_at < now - PENDING_LEASE,
            ActionItem.attempts >= MAX_ATTEMPTS,
        )
        .values(outcome="failed")
    )
    db.execute(terminalize_stmt)
    return None


def record_extraction(
    db: Session,
    *,
    message_id: UUID,
    claim_token: UUID,
    result: ExtractedAction | NoAction | None,
    thread_done: bool,
    label_still_actionable: bool,
    already_replied: bool = False,
    model_version: str | None = None,
    user_id: UUID | None = None,
    failure_category: str | None = None,
    credential_id: UUID | None = None,
    credential_revision: int | None = None,
) -> bool:
    """Fenced write of an extraction attempt's outcome.

    ``UPDATE ... WHERE message_id = :m AND claim_token = :t AND
    outcome = 'pending'`` -- a stale worker's late write (its lease expired
    and someone else already reclaimed the row) affects zero rows and the
    caller treats that as a logged no-op, never overwriting a newer attempt.
    Does not commit.

    Precedence: a label that left ``ACTION_LABELS`` while the call was in
    flight always wins (records ``ineligible``, claimable, so a
    reclassify-back can re-extract) over the call's own result. Otherwise the
    result decides: ``ExtractedAction`` -> ``extracted``, ``NoAction`` ->
    ``no_action`` (extraction fields cleared -- a forced re-extraction that
    finds nothing must not leave stale fields from a prior attempt), ``None``
    -> ``failed`` (fields untouched, transient failure -- except for a
    credential-class category, see below).

    ``model_version`` (plan D6) is the attempt's ``f"{provider}:{model}"``,
    written on BOTH settled branches: ``extracted`` keeps preferring
    ``result.model_version`` (the extractor always sets it to this same
    value, so this is not a behavior change -- see
    ``test_record_extraction_extracted_model_version_matches_call_context``),
    and ``no_action`` -- which carries no model version of its own -- now
    stamps the parameter instead of hard-writing ``None``. This is what lets
    D5's ``claimable_predicate(force=True)`` tell "this settled row came from
    the CURRENT model" from "the model has since changed" via
    ``IS DISTINCT FROM``.

    ``failure_category``/``user_id``/``credential_id``/``credential_revision``
    (plan D2) only matter on the ``result is None`` (failed) branch, and only
    when ``failure_category`` is one of ``CREDENTIAL_CLASS_FAILURE_CATEGORIES``
    -- every retry with the SAME credential would fail identically, so this
    jumps ``attempts`` straight to ``MAX_ATTEMPTS`` (terminal) instead of
    leaving it for the caller to burn two more calls on the same dead
    credential. The identity check is atomic with this write (see
    ``_credential_class_cap_jump_attempts``): a mismatch -- caught mid-flight
    by a credential rotation -- writes ``attempts=0`` instead, so the claim
    this attempt already spent doesn't strand the row capped under a
    credential nobody's using anymore. Callers that don't pass
    ``failure_category`` (most of this module's own test suite) get the
    exact same ``failed``-with-fields-untouched behavior as before D2.

    Independent of which branch above fires: this record also resolves the
    row's existing ``"open"`` status to ``"done"`` when either ``thread_done``
    -- an open row created on a done thread must not resurface after an
    auto-reopen -- OR ``already_replied`` AND the result is a
    ``kind="reply"`` extraction (docs/plans/2026-08-13-reply-plan.md §3.5:
    the caller has already checked this message's ``created_at`` against
    ``mail_thread.replied_at``, so an extraction that only now discovers
    "needs a reply" on a message the user already answered from CortexMail
    records itself pre-resolved, never a second obligation for an answered
    message). Status is never reset otherwise; re-extraction must not
    resurrect an item an operator resolved.
    """
    now = datetime.now(timezone.utc)
    values: dict = {"claim_token": None}

    if not label_still_actionable:
        values["outcome"] = "ineligible"
    elif isinstance(result, ExtractedAction):
        values.update(
            outcome="extracted",
            kind=result.kind,
            title=result.title,
            due_at=result.due_at,
            due_precision=result.due_precision,
            due_raw=result.due_raw,
            amount=result.amount,
            currency=result.currency,
            source_confidence=result.confidence,
            model_version=result.model_version,
        )
    elif isinstance(result, NoAction):
        values.update(
            outcome="no_action",
            kind=None,
            title=None,
            due_at=None,
            due_precision=None,
            due_raw=None,
            amount=None,
            currency=None,
            source_confidence=None,
            model_version=model_version,
        )
    else:
        values["outcome"] = "failed"
        cap_jump = _credential_class_cap_jump_attempts(
            db,
            user_id=user_id,
            failure_category=failure_category,
            credential_id=credential_id,
            credential_revision=credential_revision,
        )
        if cap_jump is not None:
            values["attempts"] = cap_jump

    resolves_already_answered = (
        already_replied and isinstance(result, ExtractedAction) and result.kind == "reply"
    )
    values.update(_done_stamp_columns(thread_done or resolves_already_answered, now))

    stmt = (
        update(ActionItem)
        .where(
            ActionItem.message_id == message_id,
            ActionItem.claim_token == claim_token,
            ActionItem.outcome == "pending",
        )
        .values(**values)
    )
    return bool(db.execute(stmt).rowcount)
