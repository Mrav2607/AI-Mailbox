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

Blend in-distribution inbox data with synthetic intent examples for the scarce
classes, and score against the real hand-labeled set:
    python ml/train_classifier.py \
        --data data/real_train.jsonl data/synthetic.jsonl \
        --eval-file data/eval.jsonl

Then try the stronger encoder once it works end-to-end:
    python ml/train_classifier.py --model-name answerdotai/ModernBERT-base --epochs 4 \
        --data data/real_train.jsonl data/synthetic.jsonl --eval-file data/eval.jsonl
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
            print(f"  WARNING: {p} not found, skipping")
            continue
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


def _split(texts, labels, test_size, seed):
    """Stratified split, falling back to a plain random split if a class is too
    thin to stratify (rare intent classes can have just a handful of rows)."""
    try:
        return train_test_split(texts, labels, test_size=test_size, stratify=labels, random_state=seed)
    except ValueError:
        print(f"  (stratify failed at test_size={test_size}; using a random split for this stage)")
        return train_test_split(texts, labels, test_size=test_size, random_state=seed)


class EmailDataset(torch.utils.data.Dataset):
    def __init__(self, texts, labels, tokenizer, max_length):
        self.enc = tokenizer(texts, truncation=True, max_length=max_length)
        self.labels = labels

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        item = {k: v[idx] for k, v in self.enc.items()}
        item["labels"] = self.labels[idx]
        return item


class WeightedTrainer(Trainer):
    """Trainer with class-weighted cross-entropy for imbalanced labels."""

    def __init__(self, *args, class_weights=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_weights = class_weights

    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None, **kwargs):
        labels = inputs.pop("labels")
        outputs = model(**inputs)
        weight = self.class_weights.to(outputs.logits.device) if self.class_weights is not None else None
        loss = torch.nn.functional.cross_entropy(outputs.logits, labels, weight=weight)
        return (loss, outputs) if return_outputs else loss


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    return {
        "accuracy": accuracy_score(labels, preds),
        "macro_f1": f1_score(labels, preds, average="macro"),
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
    parser.add_argument("--model-name", default="distilbert-base-uncased")
    parser.add_argument("--out", default="models/email-classifier")
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

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

    train_ds = EmailDataset(x_train, y_train, tokenizer, args.max_length)
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
    )

    trainer = WeightedTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        data_collator=DataCollatorWithPadding(tokenizer),
        compute_metrics=compute_metrics,
        class_weights=class_weights,
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
