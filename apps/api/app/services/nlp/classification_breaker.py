"""Per-ingest-run circuit breaker for inline classification calls (plan:
phase 3 of the LLM-failure work) -- shared by ``gmail_ingest.py``,
``outlook_ingest.py``, and ``reconcile.py``'s ingest-driven passes, so "3
consecutive no-verdict BYOK outcomes" isn't spelled a third time.

``llm_client.WORKER_RETRIES`` already bounds ONE wire call to roughly 60s of
cumulative waits. Nothing bounded the RUN: an ingest page loop calls
``classify_with_usage()`` once per unclassified message with no stop --
unlike ``backfill.py``'s ``_CONSECUTIVE_NO_VERDICT_LIMIT`` (Phase 2) and
``extraction_run.py``'s ``_CONSECUTIVE_FAILURE_LIMIT`` (this phase's own
extraction-sweep breaker), neither ingest path had an equivalent. A run of
~30 all-429 messages burns retry waits alone past the ingest task's own
soft time limit; the task then hits its hard limit, Celery's
``autoretry_for`` re-runs it, and since a failed message never gets a
``Classification`` row it gets reprocessed and re-called on the retry --
the exact retry storm this phase exists to prevent, rebuilt out of this
phase's own parts (Codex review).

Same threshold and reasoning as the two breakers above: not 1 (a single odd
message must never trip a whole run), reset on any produced verdict, so
only a genuine losing streak trips it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_CONSECUTIVE_NO_VERDICT_LIMIT = 3


@dataclass
class ClassificationBreaker:
    """One instance per ingest run, shared across every phase of that run
    that can issue an inline classification call -- the START reconciliation
    pass, the provider's own page/message loop, and the END reconciliation
    pass. Threaded through explicitly (never rebuilt per phase) so a losing
    streak discovered in one phase stops every LATER phase's classification
    calls too, instead of each phase discovering (and retrying through) the
    same failing provider independently (Codex review).

    Tripping this NEVER stops ingest itself -- messages and threads keep
    being persisted, and provider cursors/cursors-equivalent state keep
    advancing exactly as they would without it. Only classification calls
    stop. Counting a skipped message/attempt into ``left_unclassified`` so
    the user is told (not silently shorted) is the CALLER's job, not this
    class's -- it only tracks the streak and answers "should you even try".
    """

    tripped: bool = False
    _consecutive: int = field(default=0, repr=False)

    @property
    def should_call(self) -> bool:
        """Whether the caller should still issue a classification call.
        Flips to `False` the moment the streak trips and stays `False` for
        the rest of this breaker's life -- there is no reset within a
        single run, by design: a provider that failed 3 times in a row is
        very likely to keep failing the same way for the rest of it."""
        return not self.tripped

    def record(self, verdict_produced: bool) -> None:
        """Record one REAL classification attempt's outcome -- call this
        only for an attempt that actually reached ``call_chat_completion``.
        A call ``should_call`` said to skip was never attempted, so it must
        never be recorded here; doing so anyway would be scoring the
        breaker's own skip as another failure, and after tripping this is a
        no-op regardless (the streak no longer matters once `tripped` is
        `True` for the rest of the run)."""
        if self.tripped:
            return
        if verdict_produced:
            self._consecutive = 0
            return
        self._consecutive += 1
        if self._consecutive >= _CONSECUTIVE_NO_VERDICT_LIMIT:
            self.tripped = True
