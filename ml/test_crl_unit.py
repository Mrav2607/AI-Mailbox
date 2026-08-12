"""
Phase-0a unit pin for the CRL trainer change (plan §4, ``docs/plans/2026-08-11-
crl-gap-plan.md``). Standalone -- ml/ has no pytest suite. Run directly:

    python ml/test_crl_unit.py

Exits 0, printing one "[x] PASS" line per check, if every assertion in plan
§4 Phase 0a holds. Exits nonzero with a clear failure message on the first
check that doesn't. The CUDA leg (f) prints an explicit skip message and is
excluded from the pass/fail count when CUDA isn't available; it runs
unconditionally on the dev RTX 4060.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Test (e) loads distilbert-base-uncased from the local HF cache -- set this
# before anything under huggingface_hub gets imported (some of its offline
# checks are read once at import time, not per-call) so it never reaches out
# to the network even to check for updates.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import train_classifier as tc  # noqa: E402


class _FakeModel(nn.Module):
    """Stand-in for the real DistilBERT classifier used by tests (a)-(d) and
    (f): its forward() returns a PRE-SET logits tensor instead of doing a
    real forward pass, so the test can hand-compute the expected loss from
    a known logits tensor rather than trusting the model's own output. One
    real parameter keeps it behaving like a normal nn.Module (device
    placement, .train()/.eval()). Test (e) uses a real DistilBERT instead,
    since it's specifically checking the Trainer's collator behavior.
    """

    def __init__(self, logits_by_call: list[torch.Tensor]):
        super().__init__()
        self.dummy = nn.Parameter(torch.zeros(1))
        self._logits_by_call = logits_by_call
        self._call_idx = 0

    def forward(self, **kwargs):
        logits = self._logits_by_call[self._call_idx]
        self._call_idx += 1
        return SimpleNamespace(logits=logits)


def _build_trainer(logits_by_call, n_train, crl_lambda, class_weights=None, device=None):
    """Build a real ``WeightedTrainer`` around a ``_FakeModel`` so
    ``compute_loss`` runs the actual shipped code path without needing a
    real transformer forward pass or a real dataset. ``train_dataset`` only
    needs ``len()`` here -- WeightedTrainer.__init__ uses it to size the
    history buffers; nothing else touches it in these tests."""
    tmp_dir = tempfile.mkdtemp(prefix="crl_unit_")
    args = tc.TrainingArguments(
        output_dir=tmp_dir,
        report_to="none",
        logging_steps=10_000,  # keep compute_loss's self.log() quiet
        remove_unused_columns=False,
    )
    model = _FakeModel(logits_by_call)
    if device is not None:
        model = model.to(device)
    trainer = tc.WeightedTrainer(
        model=model,
        args=args,
        train_dataset=list(range(n_train)),
        class_weights=class_weights,
        crl_lambda=crl_lambda,
    )
    return trainer


def test_a_lambda_zero_matches_plain_ce() -> None:
    """(a) At λ=0, compute_loss must return EXACTLY the weighted CE and
    must never touch the history buffers -- this is the regression
    guarantee that every non-CRL caller is unaffected."""
    torch.manual_seed(0)
    logits = torch.randn(5, 6, requires_grad=True)
    labels = torch.tensor([0, 1, 2, 3, 4])
    class_weights = torch.tensor([1.0, 2.0, 0.5, 1.5, 1.0, 3.0])
    sample_idx = torch.tensor([0, 1, 2, 3, 4])  # present in the batch on purpose --
    # popping it defensively even at λ=0 is part of the contract (plan §3.4)

    trainer = _build_trainer([logits], n_train=10, crl_lambda=0.0, class_weights=class_weights)
    loss = trainer.compute_loss(trainer.model, {"labels": labels, "sample_idx": sample_idx})

    expected = torch.nn.functional.cross_entropy(logits, labels, weight=class_weights)
    assert torch.allclose(loss, expected), (
        f"[a] λ=0 compute_loss returned {loss.item()!r}, expected exactly the plain "
        f"weighted CE {expected.item()!r}"
    )
    assert trainer.hist_correct is None and trainer.hist_seen is None, (
        "[a] λ=0 must never allocate history buffers"
    )
    print("[a] PASS -- λ=0 compute_loss == plain weighted CE; history buffers untouched")


def test_b_circular_pair_loss_hand_computed() -> None:
    """(b) crl_pair_loss on a fixed 4-sample fixture, hand-verified,
    covering an equal-c pair, two unequal-c pairs, AND the circular
    final -> first wrap pair (2, 3) then (3, 0).

    Fixture: kappa = [0.9, 0.6, 0.7, 0.4], c = [1.0, 0.0, 0.5, 0.5].
    Pairs (k, k+1 mod 4), oriented so i has the larger c:
      (0,1): c 1.0 vs 0.0 (unequal) -> i=0 j=1
             L = max(0, -(0.9-0.6) + (1.0-0.0)) = max(0, 0.7) = 0.7
      (1,2): c 0.0 vs 0.5 (unequal) -> i=2 j=1
             L = max(0, -(0.7-0.6) + (0.5-0.0)) = max(0, 0.4) = 0.4
      (2,3): c 0.5 vs 0.5 (equal)   -> L = 0.0 (masked, not computed from kappa)
      (3,0): c 0.5 vs 1.0 (unequal) -> i=0 j=3
             L = max(0, -(0.9-0.4) + (1.0-0.5)) = max(0, 0.0) = 0.0
    sum = 0.7 + 0.4 + 0.0 + 0.0 = 1.1; L_CRL = 1.1 / 4 = 0.275
    """
    kappa = torch.tensor([0.9, 0.6, 0.7, 0.4])
    c = torch.tensor([1.0, 0.0, 0.5, 0.5])

    loss = tc.crl_pair_loss(kappa, c)
    expected = 0.275
    assert torch.allclose(loss, torch.tensor(expected), atol=1e-6), (
        f"[b] circular-pair loss {loss.item()!r} != hand-computed {expected!r}"
    )
    print("[b] PASS -- circular-pair loss matches the hand-computed fixture (0.275)")


def test_c_all_equal_c_is_exactly_zero() -> None:
    """(c) A batch where every adjacent pair (including the wrap) has equal
    c must yield exactly 0.0 -- not just "small", exactly zero, since the
    equal-c mask zeroes the pair regardless of the kappa values."""
    kappa = torch.tensor([0.9, 0.1, 0.5, 0.7])
    c = torch.tensor([0.5, 0.5, 0.5, 0.5])

    loss = tc.crl_pair_loss(kappa, c)
    assert loss.item() == 0.0, f"[c] all-equal-c batch gave {loss.item()!r}, expected exactly 0.0"
    print("[c] PASS -- all-equal-c batch yields exactly 0.0")


def test_d_history_updates_match_hand_counted() -> None:
    """(d) History updates are current-batch-inclusive (CRL-cb, plan §3.1):
    two synthetic batches over the same 2 samples, correctness hand-counted.

    Batch 1: logits favor class 0 for both rows; labels [0, 0] -> row 0
    correct, row 1 wrong. Expect hist_correct [1, 0], hist_seen [1, 1].
    Batch 2: same logits; labels [0, 1] -> both rows correct. Expect
    hist_correct [1+1, 0+1] = [2, 1], hist_seen [1+1, 1+1] = [2, 2].
    """
    logits1 = torch.tensor([[2.0, 0.0], [0.0, 2.0]], requires_grad=True)
    logits2 = torch.tensor([[2.0, 0.0], [0.0, 2.0]], requires_grad=True)
    sample_idx = torch.tensor([0, 1])

    trainer = _build_trainer([logits1, logits2], n_train=2, crl_lambda=1.0)
    assert trainer.hist_correct.tolist() == [0.0, 0.0], "[d] history buffers must start at zero"

    trainer.compute_loss(trainer.model, {"labels": torch.tensor([0, 0]), "sample_idx": sample_idx})
    assert trainer.hist_correct.tolist() == [1.0, 0.0], (
        f"[d] after batch 1: hist_correct = {trainer.hist_correct.tolist()!r}, expected [1.0, 0.0]"
    )
    assert trainer.hist_seen.tolist() == [1.0, 1.0], (
        f"[d] after batch 1: hist_seen = {trainer.hist_seen.tolist()!r}, expected [1.0, 1.0]"
    )

    trainer.compute_loss(trainer.model, {"labels": torch.tensor([0, 1]), "sample_idx": sample_idx})
    assert trainer.hist_correct.tolist() == [2.0, 1.0], (
        f"[d] after batch 2: hist_correct = {trainer.hist_correct.tolist()!r}, expected [2.0, 1.0]"
    )
    assert trainer.hist_seen.tolist() == [2.0, 2.0], (
        f"[d] after batch 2: hist_seen = {trainer.hist_seen.tolist()!r}, expected [2.0, 2.0]"
    )
    print("[d] PASS -- history buffers match hand-counted correctness after two batches")


def test_e_sample_idx_survives_the_collator() -> None:
    """(e) Integration tripwire (Codex P3-1): build a REAL Trainer around a
    real DistilBERT + DataCollatorWithPadding and assert the actual
    dataloader batch contains "sample_idx". Without
    remove_unused_columns=False, the Trainer wraps the collator with
    _get_collator_with_removed_columns and silently strips it -- CRL would
    then be a no-op that quietly reproduces the baseline. Uses
    distilbert-base-uncased from the local HF cache (no network)."""
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, DataCollatorWithPadding

    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-uncased")
    model = AutoModelForSequenceClassification.from_pretrained("distilbert-base-uncased", num_labels=6)

    texts = ["hello there", "please reply asap", "fyi only, no action", "buy now, limited offer"]
    labels = [0, 1, 2, 3]
    train_ds = tc.EmailDataset(texts, labels, tokenizer, max_length=32, include_index=True)

    tmp_dir = tempfile.mkdtemp(prefix="crl_unit_e_")
    args = tc.TrainingArguments(
        output_dir=tmp_dir,
        report_to="none",
        logging_steps=10_000,
        remove_unused_columns=False,
        per_device_train_batch_size=2,
    )
    trainer = tc.WeightedTrainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        data_collator=DataCollatorWithPadding(tokenizer),
        crl_lambda=1.0,
    )
    batch = next(iter(trainer.get_train_dataloader()))
    assert "sample_idx" in batch, (
        f"[e] sample_idx missing from the real dataloader batch (keys: {list(batch.keys())!r}) "
        "-- the Trainer's collator column-stripping silently ate it"
    )
    print("[e] PASS -- sample_idx survives the real Trainer's collator (remove_unused_columns=False holds)")


def test_f_cuda_leg() -> bool:
    """(f) CUDA leg: run compute_loss under torch.autocast("cuda") with
    CUDA logits/sample_idx and assert (1) the history buffers stay on CPU,
    (2) the loss is finite fp32, (3) after loss.backward() no history
    tensor has acquired a grad_fn or a .grad -- the history path is
    genuinely detached from autograd end to end.

    Returns True if the leg ran, False if it was skipped (no CUDA)."""
    if not torch.cuda.is_available():
        print("[f] SKIP -- CUDA not available on this machine")
        return False

    device = "cuda"
    torch.manual_seed(1)
    logits = torch.randn(4, 6, device=device, requires_grad=True)
    labels = torch.tensor([0, 1, 2, 3], device=device)
    sample_idx = torch.tensor([0, 1, 2, 3], device=device)

    trainer = _build_trainer([logits], n_train=4, crl_lambda=1.0, device=device)

    with torch.autocast("cuda"):
        loss = trainer.compute_loss(trainer.model, {"labels": labels, "sample_idx": sample_idx})

    assert loss.dtype == torch.float32, f"[f] loss dtype {loss.dtype!r}, expected fp32"
    assert torch.isfinite(loss).item(), f"[f] loss is not finite: {loss.item()!r}"
    assert trainer.hist_correct.device.type == "cpu", (
        f"[f] hist_correct landed on {trainer.hist_correct.device!r}, must stay on CPU"
    )
    assert trainer.hist_seen.device.type == "cpu", (
        f"[f] hist_seen landed on {trainer.hist_seen.device!r}, must stay on CPU"
    )

    loss.backward()
    assert trainer.hist_correct.grad_fn is None and trainer.hist_correct.grad is None, (
        "[f] hist_correct acquired autograd state -- the history path must be fully detached"
    )
    assert trainer.hist_seen.grad_fn is None and trainer.hist_seen.grad is None, (
        "[f] hist_seen acquired autograd state -- the history path must be fully detached"
    )
    print("[f] PASS -- CUDA leg: history stays on CPU, loss is finite fp32, no grad reaches history")
    return True


def main() -> None:
    checks = [
        test_a_lambda_zero_matches_plain_ce,
        test_b_circular_pair_loss_hand_computed,
        test_c_all_equal_c_is_exactly_zero,
        test_d_history_updates_match_hand_counted,
        test_e_sample_idx_survives_the_collator,
    ]
    for check in checks:
        try:
            check()
        except AssertionError as exc:
            sys.exit(f"FAIL: {check.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001  (surface anything unexpected with context)
            sys.exit(f"ERROR: {check.__name__} raised {type(exc).__name__}: {exc}")

    try:
        test_f_cuda_leg()
    except AssertionError as exc:
        sys.exit(f"FAIL: test_f_cuda_leg: {exc}")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"ERROR: test_f_cuda_leg raised {type(exc).__name__}: {exc}")

    print("\nAll CRL Phase-0a unit checks passed.")


if __name__ == "__main__":
    main()
