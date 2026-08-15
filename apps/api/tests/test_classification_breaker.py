"""Unit tests for ClassificationBreaker (plan: phase 3 of the LLM-failure
work, Codex review blocker) -- the shared per-ingest-run circuit breaker.
Integration coverage (proving it actually stops an ingest run's classify
calls, and that ingest itself keeps going regardless) lives in
test_gmail_history_sync.py / test_outlook_ingest.py / test_reply_reconcile.py.
"""

from app.services.nlp.classification_breaker import ClassificationBreaker


def test_should_call_is_true_before_anything_is_recorded():
    breaker = ClassificationBreaker()
    assert breaker.should_call is True
    assert breaker.tripped is False


def test_trips_after_three_consecutive_no_verdict_records():
    breaker = ClassificationBreaker()
    breaker.record(verdict_produced=False)
    assert breaker.should_call is True
    breaker.record(verdict_produced=False)
    assert breaker.should_call is True
    breaker.record(verdict_produced=False)
    assert breaker.should_call is False
    assert breaker.tripped is True


def test_a_produced_verdict_resets_the_streak():
    """Two no-verdicts, then a success, then two more no-verdicts: the
    longest CONSECUTIVE run is 2, under the threshold -- never trips."""
    breaker = ClassificationBreaker()
    breaker.record(verdict_produced=False)
    breaker.record(verdict_produced=False)
    breaker.record(verdict_produced=True)
    breaker.record(verdict_produced=False)
    breaker.record(verdict_produced=False)
    assert breaker.should_call is True
    assert breaker.tripped is False


def test_an_isolated_no_verdict_never_trips_it():
    breaker = ClassificationBreaker()
    breaker.record(verdict_produced=False)
    assert breaker.should_call is True


def test_stays_tripped_regardless_of_further_records():
    """Once tripped, `record` is a documented no-op -- a later successful
    verdict must never un-trip the breaker within the same run (there's no
    reset within a run, by design)."""
    breaker = ClassificationBreaker()
    for _ in range(3):
        breaker.record(verdict_produced=False)
    assert breaker.tripped is True

    breaker.record(verdict_produced=True)
    assert breaker.tripped is True
    assert breaker.should_call is False
