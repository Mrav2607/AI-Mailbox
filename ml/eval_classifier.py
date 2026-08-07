"""Score a saved email classifier against a hand-labeled jsonl eval set.

Standalone by design -- this does NOT import ``train_classifier`` or anything
under ``apps/api`` (that pulls in httpx/pydantic just to reach ``LABELS``).
Labels are instead resolved from the model directory the same way
``app/services/nlp/local_model.py`` resolves them at serving time:
``labels.json`` if it's there, else ``config.json``'s ``id2label`` sorted by
index. That keeps this script honest about what the *served* model actually
predicts, independent of whatever taxonomy the training code currently uses.

Score the shipped model against the held-out hand-labeled set:
    python ml/eval_classifier.py --data data/eval.jsonl

Score a checkpoint against a couple of eval files at once, on CPU:
    python ml/eval_classifier.py --model models/email-classifier/checkpoints/checkpoint-500 \
        --data data/eval.jsonl data/eval_extra.jsonl --device cpu

Dump every misclassified row (for error analysis) alongside the usual report:
    python ml/eval_classifier.py --data data/eval.jsonl --dump-errors data/eval_errors.jsonl

The summary block also reports calibration: expected calibration error (ECE,
10 equal-width confidence bins) and multiclass Brier score, so a v2 model
can be checked against the "confidence quality didn't regress" gate, not
just accuracy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def load_labels(model_dir: Path) -> list[str]:
    """Resolve the label set for ``model_dir`` exactly like serving does:
    prefer ``labels.json``, else fall back to ``config.json``'s ``id2label``
    ordered by index."""
    labels_path = model_dir / "labels.json"
    if labels_path.exists():
        return json.loads(labels_path.read_text(encoding="utf-8"))

    config_path = model_dir / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"no config.json (or labels.json) at {model_dir}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    id2label = config.get("id2label")
    if not id2label:
        raise ValueError(f"{config_path} has no id2label; can't resolve labels")
    return [id2label[str(i)] for i in range(len(id2label))]


def load_jsonl(path: Path) -> list[dict]:
    """Parse one jsonl file into raw ``{"text", "label", ...}`` dicts. Extra
    keys (e.g. "source") are kept but ignored downstream -- same as the
    training loader."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def load_eval_set(paths: list[str], labels: list[str]) -> tuple[list[str], list[str], list[dict], int]:
    """Merge one or more jsonl files, deduping on ``text.strip().lower()``
    (keeping the first occurrence), and drop rows whose label isn't in the
    served label set. Returns (texts, gold_labels, rows, skipped_count) --
    ``rows`` is the original dict for each kept row (extra keys like
    "source" included), parallel to ``texts``/``gold``, so ``--dump-errors``
    can round-trip the full input row."""
    label_set = set(labels)
    texts: list[str] = []
    gold: list[str] = []
    rows: list[dict] = []
    seen: set[str] = set()
    skipped = 0

    for p in paths:
        path = Path(p)
        if not path.is_file():
            sys.exit(f"error: --data file not found: {p} -- refusing to produce a partial report")
        file_rows = load_jsonl(path)
        kept = 0
        for row in file_rows:
            if not isinstance(row, dict):
                skipped += 1
                continue
            text = (row.get("text") or "").strip()
            label = row.get("label")
            if not text or label not in label_set:
                skipped += 1
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            texts.append(text)
            gold.append(label)
            rows.append(row)
            kept += 1
        print(f"  {p}: {len(file_rows)} rows -> {kept} kept after dedup/label-filter")

    return texts, gold, rows, skipped


def predict_all(model, tokenizer, texts, labels, max_length, batch_size, device):
    """Run batched inference and return (predicted_labels, confidences,
    class_probs), all parallel to ``texts``. ``class_probs`` is the full
    softmax vector per row (same label order as ``labels``) -- the Brier
    score needs the whole distribution, not just the argmax confidence."""
    import torch

    preds: list[str] = []
    confidences: list[float] = []
    class_probs: list[list[float]] = []

    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        enc = tokenizer(
            batch, truncation=True, max_length=max_length, padding=True, return_tensors="pt"
        ).to(device)
        with torch.no_grad():
            logits = model(**enc).logits
            probs = torch.softmax(logits, dim=-1)
            conf, idx = torch.max(probs, dim=-1)
        preds.extend(labels[int(i)] for i in idx)
        confidences.extend(float(c) for c in conf)
        class_probs.extend(probs.tolist())

    return preds, confidences, class_probs


def print_confusion_matrix(gold: list[str], preds: list[str], labels: list[str]) -> None:
    """Print a gold-rows x predicted-columns confusion matrix with readable
    headers instead of sklearn's bare index-only array."""
    index = {label: i for i, label in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for g, p in zip(gold, preds):
        matrix[index[g]][index[p]] += 1

    col_width = max(6, max(len(label) for label in labels) + 1)
    header = " " * col_width + "".join(f"{label[:col_width]:>{col_width}}" for label in labels)
    print(header)
    for label, row in zip(labels, matrix):
        print(f"{label[:col_width]:<{col_width}}" + "".join(f"{count:>{col_width}}" for count in row))


def print_confidence_stats(gold: list[str], preds: list[str], confidences: list[float], labels: list[str]) -> None:
    """Print overall correct-vs-incorrect confidence means, plus a
    per-gold-class table (n, accuracy, mean confidence when correct/wrong)."""
    correct_conf = [c for g, p, c in zip(gold, preds, confidences) if g == p]
    wrong_conf = [c for g, p, c in zip(gold, preds, confidences) if g != p]

    def _mean(values: list[float]) -> str:
        return f"{sum(values) / len(values):.3f}" if values else "-"

    print(f"\nOverall mean confidence -- correct: {_mean(correct_conf)}  incorrect: {_mean(wrong_conf)}")

    print("\nPer-class confidence:")
    header = f"{'label':<20}{'n':>6}{'accuracy':>10}{'conf(correct)':>15}{'conf(wrong)':>13}"
    print(header)
    for label in labels:
        rows = [(g, p, c) for g, p, c in zip(gold, preds, confidences) if g == label]
        if not rows:
            print(f"{label:<20}{0:>6}{'-':>10}{'-':>15}{'-':>13}")
            continue
        n = len(rows)
        right = [c for g, p, c in rows if p == g]
        wrong = [c for g, p, c in rows if p != g]
        accuracy = f"{len(right) / n:.3f}"
        print(f"{label:<20}{n:>6}{accuracy:>10}{_mean(right):>15}{_mean(wrong):>13}")


def validate_dump_errors_path(dump_errors: str, data_paths: list[str]) -> Path:
    """Resolve ``--dump-errors`` up front and make it safe to write to: bail
    before any model loading if it resolves to the same file as one of the
    ``--data`` inputs (we open in "w" mode, which would silently truncate a
    frozen eval file), then create its parent directories so a bad path
    fails in seconds rather than after a full eval run."""
    dump_path = Path(dump_errors).resolve()
    for p in data_paths:
        if Path(p).resolve() == dump_path:
            sys.exit(f"error: --dump-errors {dump_errors} resolves to the same file as --data input {p} -- refusing to overwrite an input dataset")
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    return dump_path


def dump_misclassified(path: Path, rows: list[dict], gold: list[str], preds: list[str], confidences: list[float]) -> int:
    """Write every misclassified row to ``path`` as jsonl: the original row
    dict (text, label, and whatever extra keys like "source" it came with)
    plus "predicted", "confidence" (the max-softmax value), and "gold" -- a
    grep-friendly duplicate of the label. If a row already has one of those
    three keys, the model's value goes under "model_<key>" instead so we
    never clobber data that came from the input file. Returns how many rows
    were written."""
    written = 0
    with path.open("w", encoding="utf-8") as f:
        for row, g, p, c in zip(rows, gold, preds, confidences):
            if g == p:
                continue
            out = dict(row)
            out["model_predicted" if "predicted" in out else "predicted"] = p
            out["model_confidence" if "confidence" in out else "confidence"] = c
            out["model_gold" if "gold" in out else "gold"] = g
            f.write(json.dumps(out) + "\n")
            written += 1
    return written


def compute_calibration(gold: list[str], preds: list[str], confidences: list[float], n_bins: int = 10):
    """Bucket predictions into ``n_bins`` equal-width confidence bins over
    [0, 1] and return (ece, bin_rows). ``bin_rows`` holds one
    (lo, hi, n, mean_conf, accuracy) tuple per bin, in bin order (n/mean_conf
    /accuracy are ``None`` for empty bins). ECE = sum over bins of
    (n_bin / N) * |accuracy_bin - mean_conf_bin|."""
    n = len(confidences)
    buckets: list[list[tuple[bool, float]]] = [[] for _ in range(n_bins)]
    for g, p, c in zip(gold, preds, confidences):
        idx = min(int(c * n_bins), n_bins - 1)
        buckets[idx].append((g == p, c))

    ece = 0.0
    bin_rows = []
    for i, bucket in enumerate(buckets):
        lo, hi = i / n_bins, (i + 1) / n_bins
        if not bucket:
            bin_rows.append((lo, hi, 0, None, None))
            continue
        bucket_n = len(bucket)
        mean_conf = sum(c for _, c in bucket) / bucket_n
        accuracy = sum(1 for correct, _ in bucket if correct) / bucket_n
        ece += (bucket_n / n) * abs(accuracy - mean_conf)
        bin_rows.append((lo, hi, bucket_n, mean_conf, accuracy))

    return ece, bin_rows


def print_calibration_bins(bin_rows) -> None:
    """Print the per-bin table backing the ECE number, so it's auditable
    instead of a single opaque figure. The last bin is closed on both ends
    (``[0.9-1.0]``) since a confidence of exactly 1.0 falls in it."""
    header = f"{'bin':<12}{'n':>6}{'mean conf':>12}{'accuracy':>10}"
    print(header)
    last = len(bin_rows) - 1
    for i, (lo, hi, n, mean_conf, accuracy) in enumerate(bin_rows):
        bracket = "]" if i == last else ")"
        bin_label = f"[{lo:.1f}-{hi:.1f}{bracket}"
        if n == 0:
            print(f"{bin_label:<12}{0:>6}{'-':>12}{'-':>10}")
            continue
        print(f"{bin_label:<12}{n:>6}{mean_conf:>12.3f}{accuracy:>10.3f}")


def compute_brier_score(gold: list[str], class_probs: list[list[float]], labels: list[str]) -> float:
    """Multiclass Brier score: mean over samples of sum_c (p_c - y_c)^2,
    where p is the full softmax vector and y is the one-hot gold label.
    Lower is better; 0 is a perfect, fully-confident model."""
    index = {label: i for i, label in enumerate(labels)}
    total = 0.0
    for g, probs in zip(gold, class_probs):
        gold_idx = index[g]
        total += sum((p - (1.0 if i == gold_idx else 0.0)) ** 2 for i, p in enumerate(probs))
    return total / len(gold)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="models/email-classifier", help="model directory to evaluate")
    parser.add_argument("--data", nargs="+", required=True, help="one or more jsonl files with {text, label}")
    parser.add_argument("--max-length", type=int, default=256,
                        help="truncation length; must match what the model was trained/served with (default 256)")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--device", default=None, help="cuda or cpu (default: cuda if available, else cpu)")
    parser.add_argument("--dump-errors", default=None, help="write every misclassified row to this path as jsonl")
    args = parser.parse_args()

    dump_path = validate_dump_errors_path(args.dump_errors, args.data) if args.dump_errors else None

    model_dir = Path(args.model)
    if not (model_dir / "config.json").exists():
        sys.exit(f"error: no config.json at {model_dir} -- is --model pointing at a saved model directory?")

    try:
        labels = load_labels(model_dir)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        sys.exit(f"error: couldn't resolve labels for {model_dir} ({exc})")

    print(f"Loading eval data from: {', '.join(args.data)}")
    texts, gold, rows, skipped = load_eval_set(args.data, labels)
    if not texts:
        sys.exit("error: no usable eval rows loaded (check --data paths and that labels match the model)")
    if skipped:
        print(f"Skipped {skipped} rows (empty text or label outside {labels})")

    counts = Counter(gold)
    print(f"\nLoaded {len(texts)} eval rows")
    print("Row counts per gold label:")
    for label in labels:
        print(f"  {label}: {counts.get(label, 0)}")

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\nLoading model from {model_dir} on {device}")
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    model.eval()
    model.to(device)

    preds, confidences, class_probs = predict_all(model, tokenizer, texts, labels, args.max_length, args.batch_size, device)

    if dump_path is not None:
        written = dump_misclassified(dump_path, rows, gold, preds, confidences)
        print(f"\nDumped {written} misclassified rows to {dump_path}")

    from sklearn.metrics import accuracy_score, classification_report, f1_score

    print(f"\n===== {model_dir} vs {', '.join(args.data)} =====")
    print(classification_report(gold, preds, labels=labels, digits=3, zero_division=0))
    print(f"accuracy: {accuracy_score(gold, preds):.3f} | macro-F1: {f1_score(gold, preds, labels=labels, average='macro'):.3f}")

    ece, bin_rows = compute_calibration(gold, preds, confidences)
    brier = compute_brier_score(gold, class_probs, labels)
    print(f"\nECE (10 bins): {ece:.4f}")
    print_calibration_bins(bin_rows)
    print(f"Brier score: {brier:.4f}")

    print("\nConfusion matrix (rows = gold, columns = predicted):")
    print_confusion_matrix(gold, preds, labels)

    print_confidence_stats(gold, preds, confidences, labels)


if __name__ == "__main__":
    main()
