"""
Fine-tune a small encoder to classify emails into the 6-label taxonomy.

Reads one or more jsonl files (each line ``{"text", "label"}``) passed via
--data, stratified-splits them into train/val/test, fine-tunes a transformer
with a classification head using class weights (so the dominant `fyi` class
doesn't swamp the rare ones), selects the best epoch by macro-F1, prints a
per-class report, and saves the model + tokenizer + label map for in-process
serving (loaded at runtime by ``app/services/nlp/local_model.py``).

The --data inputs come from the data-prep scripts (local-only):
``pseudo_label_inbox.py`` -> ``data/real_train.jsonl`` (Gemini-labeled real
inbox), ``generate_synthetic.py`` -> ``data/synthetic.jsonl`` (grounded
synthetic for scarce classes), and ``import_manual_emails.py`` ->
``data/manual_seeds.jsonl``. The held-out eval (``data/eval.jsonl``, from
``labelsheet_to_eval.py`` / ``import_eval_emails.py``) is kept strictly disjoint
and scored via --eval-file.

Before any of that runs, a fail-closed preflight (``run_input_guards``) checks
--data against the guard set (--guard-files plus --eval-file, if given): every
guard path must exist and parse cleanly, no --data row may share normalized
text with a guard row, and no two kept --data rows may share text under
different labels. Any violation exits immediately, before the tokenizer or
model loads -- --guard-files is how a frozen test set (e.g. data/test_v2.jsonl)
gets a hard guarantee it never leaks into training.

Blend in-distribution inbox data with synthetic intent examples for the scarce
classes, and score against the real hand-labeled set:
    python ml/train_classifier.py \
        --data data/real_train.jsonl data/synthetic.jsonl \
        --eval-file data/eval.jsonl --guard-files data/test_v2.jsonl

Then try the stronger encoder once it works end-to-end:
    python ml/train_classifier.py --model-name answerdotai/ModernBERT-base --epochs 4 \
        --data data/real_train.jsonl data/synthetic.jsonl \
        --eval-file data/eval.jsonl --guard-files data/test_v2.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from app.services.nlp.classifier import LABELS  # noqa: E402  (canonical label order)

import torch  # noqa: E402
from sklearn.metrics import accuracy_score, classification_report, f1_score  # noqa: E402
from sklearn.model_selection import train_test_split  # noqa: E402
from sklearn.utils.class_weight import compute_class_weight  # noqa: E402
from transformers import (  # noqa: E402
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DataCollatorWithPadding,
    Trainer,
    TrainingArguments,
)

LABEL2ID = {label: i for i, label in enumerate(LABELS)}
ID2LABEL = {i: label for label, i in LABEL2ID.items()}


def load_jsonl(path: Path) -> tuple[list[str], list[int]]:
    texts, labels = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        label = obj.get("label")
        text = (obj.get("text") or "").strip()
        if not text or label not in LABEL2ID:
            continue
        texts.append(text)
        labels.append(LABEL2ID[label])
    return texts, labels


def load_many(paths: list[str], cap_per_label: int | None = None) -> tuple[list[str], list[int]]:
    """Load + concatenate several jsonl files, deduping on text across all of
    them. Lets us blend real (pseudo-labeled) inbox data with synthetic intent
    examples.

    Files are processed in the given ORDER and an optional ``cap_per_label``
    bounds how many rows each label may contribute. Because real inbox files
    are listed first, the cap keeps all real rows and lets synthetic only
    *top up* the scarce classes -- so synthetic can't swamp the real
    distribution the model is actually evaluated on. Reports per-file and
    per-label counts so the mix stays visible."""
    texts: list[str] = []
    labels: list[int] = []
    seen: set[str] = set()
    from collections import Counter

    per_label: Counter = Counter()
    for p in paths:
        path = Path(p)
        if not path.exists():
            sys.exit(f"error: --data file {p} not found")
        t, y = load_jsonl(path)
        kept = capped = 0
        for text_value, label_id in zip(t, y):
            key = text_value.strip().lower()
            if key in seen:
                continue
            label_name = LABELS[label_id]
            if cap_per_label is not None and per_label[label_name] >= cap_per_label:
                capped += 1
                continue
            seen.add(key)
            texts.append(text_value)
            labels.append(label_id)
            per_label[label_name] += 1
            kept += 1
        suffix = f" ({capped} dropped over cap)" if capped else ""
        print(f"  {p}: {len(t)} rows -> {kept} kept after dedup{suffix}")
    print("Label mix:", {label: per_label.get(label, 0) for label in LABELS})
    return texts, labels


def _read_and_validate(path: Path) -> tuple[list[tuple[str, int]], int]:
    """Read a jsonl file for the guards and validate every row: a row with an
    unknown label, empty text, or that doesn't parse as JSON is a hard
    failure, not a silent drop (the trainer expects data-prep scripts to hand
    it clean input). Returns the valid ``(text, label_id)`` rows plus how many
    rows failed."""
    rows: list[tuple[str, int]] = []
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(obj, dict):
            bad += 1
            continue
        label = obj.get("label")
        text = (obj.get("text") or "").strip()
        if not text or label not in LABEL2ID:
            bad += 1
            continue
        rows.append((text, LABEL2ID[label]))
    return rows, bad


def run_input_guards(data_paths: list[str], guard_paths: list[str]) -> None:
    """Fail-closed preflight over ``--data`` and the guard set, run before any
    tokenizer/model loading so a bad input layout fails in seconds instead of
    after training setup.

    ``guard_paths`` is ``--guard-files`` plus ``--eval-file`` (already merged
    by the caller): files whose rows must never appear in ``--data``. Paths
    are canonicalized with ``Path.resolve()`` first, so the same file listed
    twice (e.g. also passed to ``--guard-files`` by habit) collapses to one
    guard entry rather than being checked -- and reported -- twice.

    Exits the process (does not return) on:
      - a missing or unreadable ``--data`` or guard path
      - a ``--data`` or guard row with an unknown label, empty text, or
        malformed JSON
      - two kept ``--data`` rows with identical normalized text
        (``text.strip().lower()``) but different labels, checked *before*
        dedup/capping -- dedup would otherwise just silently pick whichever
        row came first
      - any normalized-text overlap between a ``--data`` file and a guard
        file, or between two guard files (a file is never compared against
        itself)

    On success, returns ``None`` and ``main()`` proceeds to ``load_many()``.
    """
    guard_resolved: dict[Path, str] = {}
    for p in guard_paths:
        guard_resolved.setdefault(Path(p).resolve(), p)

    data_entries = [(Path(p), p) for p in data_paths]
    guard_entries = [(path, label) for path, label in guard_resolved.items()]

    missing = []
    for path, label in data_entries + guard_entries:
        if not path.is_file():
            missing.append(label)
            continue
        try:
            path.read_text(encoding="utf-8")
        except OSError:
            missing.append(label)
    if missing:
        sys.exit(
            "error: missing or unreadable input file(s):\n"
            + "\n".join(f"  - {m}" for m in missing)
        )

    data_rows: dict[str, list[tuple[str, int]]] = {}
    guard_rows: dict[str, list[tuple[str, int]]] = {}
    bad_counts: dict[str, int] = {}
    for path, label in data_entries:
        rows, bad = _read_and_validate(path)
        data_rows[label] = rows
        if bad:
            bad_counts[label] = bad
    for path, label in guard_entries:
        rows, bad = _read_and_validate(path)
        guard_rows[label] = rows
        if bad:
            bad_counts[label] = bad
    if bad_counts:
        sys.exit(
            "error: row(s) with an unknown label, empty text, or malformed JSON:\n"
            + "\n".join(f"  - {name}: {count} bad row(s)" for name, count in bad_counts.items())
        )

    # Label conflicts: same normalized text, different labels, across --data
    # rows only -- checked before load_many's dedup/cap even runs.
    text_labels: dict[str, set[int]] = {}
    for rows in data_rows.values():
        for text, label_id in rows:
            text_labels.setdefault(text.strip().lower(), set()).add(label_id)
    conflicts = {key: ids for key, ids in text_labels.items() if len(ids) > 1}
    if conflicts:
        lines = [
            f"  - {key!r}: labels {sorted(LABELS[i] for i in ids)}"
            for key, ids in conflicts.items()
        ]
        sys.exit(
            "error: --data rows with identical text but conflicting labels:\n"
            + "\n".join(lines)
        )

    # Pairwise disjointness by normalized text: every --data file vs. every
    # guard file, and every guard file vs. every other guard file.
    def text_set(rows: list[tuple[str, int]]) -> set[str]:
        return {text.strip().lower() for text, _ in rows}

    overlaps = []
    for data_label, rows in data_rows.items():
        data_texts = text_set(rows)
        for guard_label, grows in guard_rows.items():
            overlap = data_texts & text_set(grows)
            if overlap:
                overlaps.append((data_label, guard_label, len(overlap)))
    guard_labels = list(guard_rows.keys())
    for i, a in enumerate(guard_labels):
        for b in guard_labels[i + 1:]:
            overlap = text_set(guard_rows[a]) & text_set(guard_rows[b])
            if overlap:
                overlaps.append((a, b, len(overlap)))
    if overlaps:
        lines = [f"  - {a} <-> {b}: {n} colliding row(s)" for a, b, n in overlaps]
        sys.exit(
            "error: --data/guard files are not disjoint (guard-file rows leaked into --data):\n"
            + "\n".join(lines)
        )


def _split(texts, labels, test_size, seed):
    """Stratified split, falling back to a plain random split if a class is too
    thin to stratify (rare intent classes can have just a handful of rows)."""
    try:
        return train_test_split(texts, labels, test_size=test_size, stratify=labels, random_state=seed)
    except ValueError:
        print(f"  (stratify failed at test_size={test_size}; using a random split for this stage)")
        return train_test_split(texts, labels, test_size=test_size, random_state=seed)


class EmailDataset(torch.utils.data.Dataset):
    def __init__(self, texts, labels, tokenizer, max_length, include_index: bool = False):
        self.enc = tokenizer(texts, truncation=True, max_length=max_length)
        self.labels = labels
        self.include_index = include_index

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        item = {k: v[idx] for k, v in self.enc.items()}
        item["labels"] = self.labels[idx]
        if self.include_index:
            # Only the CRL train dataset sets include_index=True; this is
            # what compute_loss uses to index the correctness-history
            # buffers (plan §3.4). compute_loss pops it defensively either
            # way -- an un-popped extra key would TypeError in the model
            # forward.
            item["sample_idx"] = torch.tensor(idx)
        return item


def crl_pair_loss(kappa: torch.Tensor, c: torch.Tensor) -> torch.Tensor:
    """Circular-adjacent-pair CRL ranking loss (the paper's pairing, the
    CRL-cb correctness-history variant frozen in plan §3.1).

    Sample k is paired with sample ``(k+1) % B`` for every k, including the
    wrap-around pair (B-1, 0). Each pair is oriented so ``i`` is the member
    with the larger correctness-history ratio ``c``; a pair with equal ``c``
    contributes exactly 0 regardless of the confidence values (plan §3.2 --
    the raw margin formula does NOT zero itself out on a tie, so ties are
    masked explicitly).

    Parameters
    ----------
    kappa : torch.Tensor
        Max-softmax confidence per sample, shape ``(B,)``, computed in fp32
        (plan §3.2 -- half-precision margin arithmetic under autocast isn't
        trustworthy). Must carry a grad_fn; it's the only differentiable
        input here.
    c : torch.Tensor
        Correctness-history ratio per sample, shape ``(B,)``, same device as
        ``kappa``. Comes from a no_grad history buffer, so it never
        contributes gradient.

    Returns
    -------
    torch.Tensor
        Scalar loss: sum of all B circular pair losses divided by B (plan
        §3.2, Codex P2-1 -- NEVER divide by the count of unequal pairs, or
        a batch with one unequal pair gets B times the intended pressure
        and lambda stops being comparable across batches). ``max(B, 1)``
        guards the empty-batch edge case.
    """
    batch_size = kappa.size(0)
    if batch_size == 0:
        return kappa.new_zeros(())

    kappa_next = torch.roll(kappa, shifts=-1, dims=0)
    c_next = torch.roll(c, shifts=-1, dims=0)

    i_has_larger_c = c >= c_next
    kappa_i = torch.where(i_has_larger_c, kappa, kappa_next)
    kappa_j = torch.where(i_has_larger_c, kappa_next, kappa)
    c_i = torch.where(i_has_larger_c, c, c_next)
    c_j = torch.where(i_has_larger_c, c_next, c)

    pair_loss = torch.clamp(-(kappa_i - kappa_j) + (c_i - c_j), min=0.0)
    equal_c = c == c_next
    pair_loss = torch.where(equal_c, torch.zeros_like(pair_loss), pair_loss)

    return pair_loss.sum() / max(batch_size, 1)


class WeightedTrainer(Trainer):
    """Trainer with class-weighted cross-entropy for imbalanced labels, plus
    an optional CRL-cb auxiliary loss (plan §3) that ranks confidence by
    per-sample correctness history to widen the correct-vs-wrong confidence
    gap. CRL is fully inert at crl_lambda=0.0 (the default) -- every branch
    below is skipped and this Trainer is bit-identical to the pre-CRL one.
    """

    def __init__(self, *args, class_weights=None, crl_lambda: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_weights = class_weights
        self.crl_lambda = crl_lambda
        # Buffers live on the Trainer, not the dataset -- a dataloader
        # worker would otherwise copy-on-fork its own private history
        # (workers are 0 today, but this placement doesn't depend on that
        # staying true). Sized off self.train_dataset because the row
        # count is only known post-dedup/cap/drop (plan §3.1).
        self.hist_correct: torch.Tensor | None = None
        self.hist_seen: torch.Tensor | None = None
        if self.crl_lambda > 0:
            n_train = len(self.train_dataset)
            self.hist_correct = torch.zeros(n_train, dtype=torch.float32)
            self.hist_seen = torch.zeros(n_train, dtype=torch.float32)

    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None, **kwargs):
        labels = inputs.pop("labels")
        sample_idx = inputs.pop("sample_idx", None)
        outputs = model(**inputs)
        logits = outputs.logits
        weight = self.class_weights.to(logits.device) if self.class_weights is not None else None
        loss = torch.nn.functional.cross_entropy(logits, labels, weight=weight)

        if self.crl_lambda > 0 and self.model.training and sample_idx is not None:
            # CRL-cb (plan §3.1): correctness history is updated with the
            # CURRENT batch before the loss uses it, so epoch 1 already has
            # signal (c_i in {0, 1}) instead of the paper's all-equal-c
            # first epoch. Everything in this block except kappa is under
            # no_grad -- no gradient may flow through the history path.
            with torch.no_grad():
                batch_correct = (logits.detach().argmax(dim=-1) == labels).float()
                idx_cpu = sample_idx.detach().cpu()
                self.hist_seen[idx_cpu] += 1
                self.hist_correct[idx_cpu] += batch_correct.cpu()
                c_batch = (self.hist_correct[idx_cpu] / self.hist_seen[idx_cpu]).to(logits.device)

            # fp32 under autocast (plan §3.2) -- kappa keeps its grad_fn so
            # the ranking term can backprop into the model.
            kappa = torch.softmax(logits.float(), dim=-1).max(dim=-1).values
            crl_loss = crl_pair_loss(kappa, c_batch)
            loss = loss + self.crl_lambda * crl_loss
            # Gate on the step counter ourselves: a direct self.log() fires
            # the console callback immediately, every call -- logging_steps
            # only paces the Trainer's own periodic logging, not this one.
            if self.state.global_step % max(self.args.logging_steps, 1) == 0:
                self.log({"crl_loss": float(crl_loss.detach())})

        # Gradient accumulation is 1 today, so one micro-batch == one
        # optimizer step; if that ever changes, CRL pairs are formed
        # per-micro-batch, not across the accumulated macro-batch.
        return (loss, outputs) if return_outputs else loss


def _mean_or_zero(values) -> float:
    """Mean of ``values``, or 0.0 if empty -- used for conf_gap so a side
    with no correct (or no wrong) predictions in an eval batch doesn't
    blow up the metric with a NaN."""
    return float(np.mean(values)) if len(values) else 0.0


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)

    # conf_gap makes the per-epoch gap trajectory visible without touching
    # checkpoint selection (plan §3.4 -- metric_for_best_model stays
    # macro_f1, D2).
    probs = np.exp(logits - logits.max(axis=-1, keepdims=True))
    probs = probs / probs.sum(axis=-1, keepdims=True)
    kappa = probs.max(axis=-1)
    correct = preds == labels
    conf_gap = _mean_or_zero(kappa[correct]) - _mean_or_zero(kappa[~correct])

    return {
        "accuracy": accuracy_score(labels, preds),
        "macro_f1": f1_score(labels, preds, average="macro"),
        "conf_gap": conf_gap,
    }


def report(name, trainer, dataset, labels):
    preds = np.argmax(trainer.predict(dataset).predictions, axis=-1)
    print(f"\n===== {name} =====")
    print(f"macro-F1: {f1_score(labels, preds, average='macro'):.4f} | "
          f"accuracy: {accuracy_score(labels, preds):.4f}")
    print(classification_report(
        labels, preds, target_names=list(LABELS), labels=list(range(len(LABELS))), zero_division=0
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", nargs="+", default=["data/real_train.jsonl"],
                        help="one or more jsonl files to blend (deduped on text)")
    parser.add_argument("--cap-per-label", type=int, default=None,
                        help="max rows per label across all --data files (list real "
                             "files first so synthetic only tops up scarce classes)")
    parser.add_argument("--eval-file", default=None, help="optional hand-labeled jsonl for an honest OOD score")
    parser.add_argument("--guard-files", nargs="*", default=[],
                        help="jsonl files whose rows must never appear in --data (e.g. a frozen "
                             "test set); combined with --eval-file into one guard set")
    parser.add_argument("--model-name", default="distilbert-base-uncased")
    parser.add_argument("--out", default="models/email-classifier")
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--crl-lambda", type=float, default=0.0,
                        help="weight on the CRL confidence-ranking auxiliary loss (plan §3); "
                             "0.0 (default) fully disables CRL and reproduces the pre-CRL trainer")
    args = parser.parse_args()

    guard_paths = list(args.guard_files) + ([args.eval_file] if args.eval_file else [])
    run_input_guards(args.data, guard_paths)

    print(f"Loading data from: {', '.join(args.data)}")
    texts, labels = load_many(args.data, args.cap_per_label)
    print(f"Loaded {len(texts)} total rows")

    # A stratified split needs >=2 rows per class. Drop classes too rare to
    # split (e.g. a lone spam example) from the internal train/val/test -- the
    # real hand-labeled eval still scores them; they just can't be split here.
    from collections import Counter

    counts = Counter(labels)
    rare = {i for i, c in counts.items() if c < 2}
    if rare:
        print(f"Dropping {sum(counts[i] for i in rare)} rows from classes too rare "
              f"to split: {[LABELS[i] for i in rare]} (still scored in the real eval)")
        kept = [(t, l) for t, l in zip(texts, labels) if l not in rare]
        texts = [t for t, _ in kept]
        labels = [l for _, l in kept]

    # 80/10/10 stratified split, with a non-stratified fallback in case a kept
    # class is still too thin for the second split.
    x_tmp, x_test, y_tmp, y_test = _split(texts, labels, 0.10, args.seed)
    x_train, x_val, y_train, y_val = _split(x_tmp, y_tmp, 0.1111, args.seed)
    print(f"Split -> train {len(x_train)} / val {len(x_val)} / test {len(x_test)}")

    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name, num_labels=len(LABELS), id2label=ID2LABEL, label2id=LABEL2ID
    )

    # include_index is train-only and CRL-only: it's what lets compute_loss
    # index the history buffers, and skipping it when crl_lambda == 0 keeps
    # non-CRL runs bit-identical to the pre-CRL dataset.
    train_ds = EmailDataset(x_train, y_train, tokenizer, args.max_length, include_index=args.crl_lambda > 0)
    val_ds = EmailDataset(x_val, y_val, tokenizer, args.max_length)
    test_ds = EmailDataset(x_test, y_test, tokenizer, args.max_length)

    # Some labels (e.g. spam) may have 0 training rows; compute_class_weight
    # only accepts classes that actually appear, so weight the present ones and
    # default absent ones to 1.0 (they have no examples to learn from anyway).
    present = np.unique(y_train)
    present_weights = compute_class_weight("balanced", classes=present, y=y_train)
    weight_by_id = {int(c): float(w) for c, w in zip(present, present_weights)}
    missing = [LABELS[i] for i in range(len(LABELS)) if i not in weight_by_id]
    if missing:
        print(f"WARNING: no training rows for {missing} -- model cannot learn these classes.")
    class_weights = torch.tensor(
        [weight_by_id.get(i, 1.0) for i in range(len(LABELS))], dtype=torch.float
    )
    print("Class weights:", {LABELS[i]: round(float(w), 2) for i, w in enumerate(class_weights)})

    use_cuda = torch.cuda.is_available()
    bf16 = use_cuda and torch.cuda.is_bf16_supported()
    print(f"CUDA: {use_cuda}" + (f" ({torch.cuda.get_device_name(0)})" if use_cuda else " -- training on CPU will be slow"))

    training_args = TrainingArguments(
        output_dir=str(Path(args.out) / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size * 2,
        learning_rate=args.lr,
        warmup_ratio=0.1,
        weight_decay=0.01,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="macro_f1",
        greater_is_better=True,
        save_total_limit=1,
        logging_steps=50,
        bf16=bf16,
        fp16=use_cuda and not bf16,
        report_to="none",
        seed=args.seed,
        # For a plain torch Dataset, Trainer wraps the collator with
        # _get_collator_with_removed_columns and silently strips any key
        # the model's forward signature doesn't accept -- that would drop
        # sample_idx and make CRL a no-op that quietly reproduces the
        # baseline (plan §3.4). Harmless for the non-CRL path too, so this
        # is unconditional rather than gated on crl_lambda -- a conditional
        # here would just invite the same drift back in later.
        remove_unused_columns=False,
    )

    trainer = WeightedTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        data_collator=DataCollatorWithPadding(tokenizer),
        compute_metrics=compute_metrics,
        class_weights=class_weights,
        crl_lambda=args.crl_lambda,
    )

    trainer.train()

    report("VALIDATION", trainer, val_ds, y_val)
    report("TEST (held-out, same distribution as training)", trainer, test_ds, y_test)

    if args.eval_file:
        eval_texts, eval_labels = load_jsonl(Path(args.eval_file))
        if eval_texts:
            eval_ds = EmailDataset(eval_texts, eval_labels, tokenizer, args.max_length)
            report(f"REAL INBOX EVAL ({args.eval_file})", trainer, eval_ds, eval_labels)
        else:
            print(f"\nNo usable rows in {args.eval_file}; skipping real-inbox eval.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))
    (out_dir / "labels.json").write_text(json.dumps(list(LABELS), indent=2), encoding="utf-8")
    print(f"\nSaved model + tokenizer to {out_dir}")


if __name__ == "__main__":
    main()
