"""Fit a post-hoc confidence-calibration transform for a saved email
classifier and write ``calibration.json`` (plan:
``docs/plans/2026-08-07-model-v21-calibration-plan.md``, FROZEN v5).

Standalone by design, same rule as ``eval_classifier.py`` -- this does NOT
import ``apps/api``. It DOES import ``eval_classifier`` (its sibling in
``ml/``) to reuse label resolution, row loading, and the ECE/Brier math, so
the numbers this script prints are computed the exact same way the eval
script (and, ultimately, serving) will see them.

The whole run is dev-set-only: logits are computed ONCE, and a single
seed-42 stratified 5-fold split (pinned in code, not a CLI flag -- see
``SEED`` below) decides which of two candidate transforms (scalar
temperature vs. per-class vector scaling) is even eligible to ship.
``calibration.json`` is written ONLY after that candidate also clears a
one-shot holdout gate (plan §3.2) -- without ``--holdout`` the run is
REPORT-ONLY: it prints the full cross-fit comparison and selection verdict
and exits 0, but writes nothing. Nothing here trains or touches the
encoder's weights.

Report-only: cross-fit both forms on dev and print the selection verdict,
without writing anything (no holdout given):
    python ml/fit_calibration.py --model models/email-classifier \
        --data data/dev_v2.jsonl

Full run: same, then score the selected candidate against the one-shot
holdout and write calibration.json only if that gate passes:
    python ml/fit_calibration.py --model models/email-classifier \
        --data data/dev_v2.jsonl --holdout data/calibration_holdout.jsonl

Write to a non-default location (e.g. while testing against a checkpoint):
    python ml/fit_calibration.py --model models/email-classifier/checkpoints/checkpoint-500 \
        --data data/dev_v2.jsonl --holdout data/calibration_holdout.jsonl --out /tmp/calibration.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from eval_classifier import (
    compute_brier_score,
    compute_calibration,
    load_eval_set,
    load_labels,
)

SEED = 42  # the plan pins the stratified 5-fold split at seed 42 -- not operator-overridable, no CLI flag
N_FOLDS = 5
ACTION_REQUIRED = "action_required"  # C1 (plan §5) names this class specifically
NUM_LABELS = 6  # the v2.1 taxonomy is frozen at 6 labels (plan §2) -- not "however many this model has"
HOLDOUT_MIN_ACTION_REQUIRED = 5  # plan §4.1 support floor
HOLDOUT_MIN_ACTION_REQUIRED_CORRECT = 2  # plan §4.1 support floor (of the above, v2-correct)
BRIER_TOLERANCE = 0.005  # owner decision D12 (plan v6 amendment): C3's Brier slack -- see check_eligibility


def compute_logits(model, tokenizer, texts, max_length, batch_size, device):
    """Batched forward pass returning raw (pre-softmax) logits for every
    text, as one ``(len(texts), num_labels)`` CPU tensor. Computed once --
    every fold, and the full-dev refit, just slices this same tensor rather
    than re-running the encoder. ``texts`` must be non-empty (callers check
    this before loading the model)."""
    import torch

    chunks = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        enc = tokenizer(
            batch, truncation=True, max_length=max_length, padding=True, return_tensors="pt"
        ).to(device)
        with torch.no_grad():
            logits = model(**enc).logits
        chunks.append(logits.detach().cpu())
    return torch.cat(chunks, dim=0)


def logits_to_predictions(logits, labels):
    """Turn a ``(N, num_labels)`` logits tensor into (preds, confidences,
    class_probs) via softmax -- the same shape of output
    ``eval_classifier.predict_all`` returns, so the metric helpers imported
    from it work unchanged on calibrated OR uncalibrated logits."""
    import torch

    probs = torch.softmax(logits, dim=-1)
    conf, idx = torch.max(probs, dim=-1)
    preds = [labels[int(i)] for i in idx]
    confidences = [float(c) for c in conf]
    class_probs = probs.tolist()
    return preds, confidences, class_probs


def golden_section_minimize(f, lo, hi, tol=1e-5, max_iter=200):
    """Golden-section search for the minimizer of a unimodal 1-D function
    ``f`` over ``[lo, hi]``. No scipy dependency (not an allowed import
    here) -- this is the entire line-search algorithm the plan asks for."""
    invphi = (5 ** 0.5 - 1) / 2  # 1/golden ratio
    c = hi - invphi * (hi - lo)
    d = lo + invphi * (hi - lo)
    fc, fd = f(c), f(d)
    for _ in range(max_iter):
        if hi - lo < tol:
            break
        if fc < fd:
            hi, d, fd = d, c, fc
            c = hi - invphi * (hi - lo)
            fc = f(c)
        else:
            lo, c, fc = c, d, fd
            d = lo + invphi * (hi - lo)
            fd = f(d)
    return (lo + hi) / 2


def fit_scalar_temperature(logits, gold_idx, lo=0.01, hi=50.0, tol=1e-5):
    """Fit T > 0 minimizing mean NLL of ``softmax(logits / T)`` via
    golden-section search over ``[lo, hi]``. Dividing every logit by the
    same positive scalar never changes the argmax, so this transform can
    only ever soften/sharpen confidence -- never flip a prediction."""
    import torch

    def nll(t):
        with torch.no_grad():
            return float(torch.nn.functional.cross_entropy(logits / t, gold_idx))

    return golden_section_minimize(nll, lo, hi, tol=tol)


def fit_vector_scaling(logits, gold_idx, num_labels, max_iter=200):
    """Fit per-class w (length ``num_labels``, constrained > 0 by
    parameterizing w = exp(u)) and b minimizing NLL of
    ``softmax(logits * w + b)``, via L-BFGS (``torch.optim.LBFGS`` with
    strong-Wolfe line search). Returns ``(w, b)`` as detached CPU tensors."""
    import torch

    u = torch.zeros(num_labels, requires_grad=True)
    b = torch.zeros(num_labels, requires_grad=True)
    optimizer = torch.optim.LBFGS([u, b], lr=0.5, max_iter=max_iter, line_search_fn="strong_wolfe")

    def closure():
        optimizer.zero_grad()
        w = torch.exp(u)
        loss = torch.nn.functional.cross_entropy(logits * w + b, gold_idx)
        loss.backward()
        return loss

    optimizer.step(closure)
    with torch.no_grad():
        w_final = torch.exp(u).detach().clone()
        b_final = b.detach().clone()
    return w_final, b_final


def score_predictions(gold, preds, confidences, class_probs, labels):
    """Bundle every number the selection hierarchy (plan §3.1/§5) needs for
    one set of predictions: ECE and Brier (via the ``eval_classifier``
    functions, so the math is identical to the eval report), the overall
    conf(correct)-conf(wrong) gap, mean conf-when-correct, and a per-class
    conf-when-correct / accuracy table. Any mean over zero rows is
    ``None`` (undefined), never a divide-by-zero."""
    ece, _ = compute_calibration(gold, preds, confidences)
    brier = compute_brier_score(gold, class_probs, labels)

    correct_conf = [c for g, p, c in zip(gold, preds, confidences) if g == p]
    wrong_conf = [c for g, p, c in zip(gold, preds, confidences) if g != p]
    mean_correct = sum(correct_conf) / len(correct_conf) if correct_conf else None
    mean_wrong = sum(wrong_conf) / len(wrong_conf) if wrong_conf else None
    gap = (mean_correct - mean_wrong) if (mean_correct is not None and mean_wrong is not None) else None

    per_class_conf_correct = {}
    per_class_accuracy = {}
    for label in labels:
        rows = [(g, p, c) for g, p, c in zip(gold, preds, confidences) if g == label]
        right = [c for g, p, c in rows if p == g]
        per_class_conf_correct[label] = (sum(right) / len(right)) if right else None
        per_class_accuracy[label] = (len(right) / len(rows)) if rows else None

    return {
        "ece": ece,
        "brier": brier,
        "mean_conf_correct": mean_correct,
        "mean_conf_wrong": mean_wrong,
        "gap": gap,
        "per_class_conf_correct": per_class_conf_correct,
        "per_class_accuracy": per_class_accuracy,
    }


def check_eligibility(scores, uncal_scores):
    """C1-C3 (plan §3.1/§5, C2/C3 as relaxed by owner decision D12 -- plan
    v6 amendment), evaluated dataset-locally: ECE/Brier are always compared
    against the SAME rows' uncalibrated scores. Returns ``(eligible,
    detail)`` where ``detail`` carries every clause's raw values so the
    report can show exactly what passed or failed. A clause with no
    supporting rows (e.g. zero action_required-correct) evaluates to False,
    not an exception -- undefined counts as failing (v4).

    D12 relaxations (owner decision, recorded before any holdout scoring or
    look -- D8's evidence proved the old gap clause structurally
    unreachable by monotone calibration):
    - C2 drops the overall conf(correct)-conf(wrong) gap requirement
      entirely; it's mean conf-when-correct >= 0.70 alone now. The gap is
      still computed and reported (``C2_gap`` in ``detail``) for
      visibility -- it just no longer gates.
    - C3's Brier clause gets ``BRIER_TOLERANCE`` (0.005) of slack: calibrated
      Brier <= uncalibrated Brier + BRIER_TOLERANCE (the observed 0.0012 OOF
      failure that triggered this was noise-level). The ECE clause is
      UNCHANGED: calibrated ECE must still improve outright.
    C1 and C4 are unaffected by D12."""
    c1_value = scores["per_class_conf_correct"].get(ACTION_REQUIRED)
    c1 = c1_value is not None and c1_value >= 0.60

    c2_gap = scores["gap"]  # reported for visibility only -- D12 removed it from gating
    c2_mean = scores["mean_conf_correct"]
    c2 = c2_mean is not None and c2_mean >= 0.70

    c3_ece_ok = scores["ece"] <= uncal_scores["ece"]
    c3_brier_ok = scores["brier"] <= uncal_scores["brier"] + BRIER_TOLERANCE
    c3 = c3_ece_ok and c3_brier_ok

    detail = {
        "C1": c1, "C1_value": c1_value,
        "C2": c2, "C2_gap": c2_gap, "C2_mean": c2_mean,
        "C3": c3, "C3_ece_ok": c3_ece_ok, "C3_brier_ok": c3_brier_ok,
        "ece": scores["ece"], "uncal_ece": uncal_scores["ece"],
        "brier": scores["brier"], "uncal_brier": uncal_scores["brier"],
    }
    return (c1 and c2 and c3), detail


def count_flips(baseline_preds, calibrated_preds):
    """How many rows changed predicted label after calibration."""
    return sum(1 for b, c in zip(baseline_preds, calibrated_preds) if b != c)


def per_class_flip_report(gold, baseline_preds, calibrated_preds, labels):
    """Count flips grouped by the row's GOLD class (not predicted class) --
    that's what the per-class non-regression check in C4 needs."""
    report = {label: 0 for label in labels}
    for g, b, c in zip(gold, baseline_preds, calibrated_preds):
        if b != c:
            report[g] += 1
    return report


def check_holdout_support_floor(gold, uncal_preds):
    """Plan §4.1 support floor, checked BEFORE any holdout gate scoring: the
    holdout must carry >= ``HOLDOUT_MIN_ACTION_REQUIRED`` action_required
    rows, with >= ``HOLDOUT_MIN_ACTION_REQUIRED_CORRECT`` of them already
    correct under the UNCALIBRATED (baseline) model. Returns
    ``(ok, ar_count, ar_correct)``."""
    ar_rows = [(g, p) for g, p in zip(gold, uncal_preds) if g == ACTION_REQUIRED]
    ar_count = len(ar_rows)
    ar_correct = sum(1 for g, p in ar_rows if p == g)
    ok = ar_count >= HOLDOUT_MIN_ACTION_REQUIRED and ar_correct >= HOLDOUT_MIN_ACTION_REQUIRED_CORRECT
    return ok, ar_count, ar_correct


def check_c4_vector_holdout(gold, uncal_preds, cal_preds, uncal_scores, cal_scores, labels):
    """C4 for vector calibration on the holdout (plan §3.2, v5): flips vs.
    uncalibrated must be <= 1, and per-class accuracy must not regress
    (one-sided -- calibration has no license to trade class performance for
    confidence). An empty class bucket (no holdout rows for that gold
    class, so accuracy is undefined on either side) counts as FAILURE, not
    a vacuous pass -- the plan's "undefined counts as failure" rule applies
    per class here too. Returns
    ``(c4_pass, flips, per_class_flips, acc_ok_by_class)``."""
    flips = count_flips(uncal_preds, cal_preds)
    per_class_flips = per_class_flip_report(gold, uncal_preds, cal_preds, labels)
    acc_ok_by_class = {}
    for label in labels:
        u_acc = uncal_scores["per_class_accuracy"].get(label)
        c_acc = cal_scores["per_class_accuracy"].get(label)
        acc_ok_by_class[label] = (u_acc is not None and c_acc is not None and c_acc >= u_acc)
    no_regression = all(acc_ok_by_class.values())
    c4_pass = (flips <= 1) and no_regression
    return c4_pass, flips, per_class_flips, acc_ok_by_class


def _fmt(x, spec=".4f"):
    return format(x, spec) if isinstance(x, (int, float)) else "-"


def print_score_line(label, scores, uncal_scores, detail):
    print(f"  ECE:   {_fmt(scores['ece'])}  (uncalibrated {_fmt(uncal_scores['ece'])})  "
          f"C3-ECE {'PASS' if detail['C3_ece_ok'] else 'FAIL'}")
    print(f"  Brier: {_fmt(scores['brier'])}  (uncalibrated {_fmt(uncal_scores['brier'])})  "
          f"C3-Brier {'PASS' if detail['C3_brier_ok'] else 'FAIL'}")
    print(f"  overall gap: {_fmt(detail['C2_gap'])}   mean conf(correct): {_fmt(detail['C2_mean'])}   "
          f"C2 {'PASS' if detail['C2'] else 'FAIL'}")
    print(f"  {ACTION_REQUIRED} conf(correct): {_fmt(detail['C1_value'])}   "
          f"C1 {'PASS' if detail['C1'] else 'FAIL'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="models/email-classifier", help="model directory to calibrate")
    parser.add_argument("--data", nargs="+", required=True, help="dev jsonl file(s) with {text, label}")
    parser.add_argument("--holdout", nargs="+", default=None,
                        help="optional fresh calibration-holdout jsonl(s); scored once, gates the write (plan §3.2)")
    parser.add_argument("--out", default=None, help="output path (default: <model>/calibration.json)")
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--device", default=None, help="cuda or cpu (default: cuda if available, else cpu)")
    args = parser.parse_args()

    model_dir = Path(args.model)
    out_path = Path(args.out) if args.out else model_dir / "calibration.json"

    if not (model_dir / "config.json").exists():
        sys.exit(f"error: no config.json at {model_dir} -- is --model pointing at a saved model directory?")

    try:
        labels = load_labels(model_dir)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        sys.exit(f"error: couldn't resolve labels for {model_dir} ({exc})")

    if len(labels) != NUM_LABELS:
        sys.exit(
            f"error: {model_dir} has {len(labels)} labels {labels!r}; the v2.1 calibration "
            f"schema (plan §2) requires exactly {NUM_LABELS}"
        )
    if ACTION_REQUIRED not in labels:
        sys.exit(f"error: {ACTION_REQUIRED!r} not in model labels {labels!r}; C1 is undefined for this taxonomy")

    print(f"Loading dev data from: {', '.join(args.data)}")
    dev_texts, dev_gold, dev_rows, dev_skipped = load_eval_set(args.data, labels)
    if not dev_texts:
        sys.exit("error: no usable dev rows loaded (check --data paths and that labels match the model)")
    if dev_skipped:
        print(f"Skipped {dev_skipped} dev rows (empty text or label outside {labels})")

    from sklearn.model_selection import StratifiedKFold
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\nLoading model from {model_dir} on {device}")
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    model.eval()
    model.to(device)

    print(f"\nComputing logits for {len(dev_texts)} dev rows (once)...")
    dev_logits = compute_logits(model, tokenizer, dev_texts, args.max_length, args.batch_size, device)
    gold_idx = torch.tensor([labels.index(g) for g in dev_gold], dtype=torch.long)

    uncal_preds, uncal_confs, uncal_probs = logits_to_predictions(dev_logits, labels)
    uncal_scores = score_predictions(dev_gold, uncal_preds, uncal_confs, uncal_probs, labels)

    # --- 5-fold stratified cross-fit (plan §3.1) ---
    skf = StratifiedKFold(n_splits=N_FOLDS, shuffle=True, random_state=SEED)
    folds = list(skf.split(range(len(dev_gold)), dev_gold))

    fold_of = {}
    for fold_id, (_, test_idx) in enumerate(folds):
        for idx in test_idx:
            fold_of[int(idx)] = fold_id
    assignment = sorted(fold_of.items())
    fold_hash = hashlib.sha256(",".join(f"{i}:{f}" for i, f in assignment).encode()).hexdigest()
    print(f"\nfold-membership hash (sha256, seed={SEED}): {fold_hash}")

    n = len(dev_gold)
    oof = {
        "temperature": {"preds": [None] * n, "confs": [None] * n, "probs": [None] * n},
        "vector": {"preds": [None] * n, "confs": [None] * n, "probs": [None] * n},
    }
    per_fold_params = {"temperature": [], "vector": []}

    for fold_id, (train_idx, test_idx) in enumerate(folds):
        train_logits = dev_logits[train_idx]
        train_gold_idx = gold_idx[train_idx]
        test_logits = dev_logits[test_idx]

        T = fit_scalar_temperature(train_logits, train_gold_idx)
        w, b = fit_vector_scaling(train_logits, train_gold_idx, NUM_LABELS)
        per_fold_params["temperature"].append(T)
        per_fold_params["vector"].append((w, b))

        preds_t, confs_t, probs_t = logits_to_predictions(test_logits / T, labels)
        preds_v, confs_v, probs_v = logits_to_predictions(test_logits * w + b, labels)

        for local_i, global_i in enumerate(test_idx):
            oof["temperature"]["preds"][global_i] = preds_t[local_i]
            oof["temperature"]["confs"][global_i] = confs_t[local_i]
            oof["temperature"]["probs"][global_i] = probs_t[local_i]
            oof["vector"]["preds"][global_i] = preds_v[local_i]
            oof["vector"]["confs"][global_i] = confs_v[local_i]
            oof["vector"]["probs"][global_i] = probs_v[local_i]

    print("\nper-fold fitted params (auditability, not used for selection):")
    for fold_id in range(N_FOLDS):
        T = per_fold_params["temperature"][fold_id]
        w, b = per_fold_params["vector"][fold_id]
        print(f"  fold {fold_id}: T={T:.4f}  |  w={[round(x, 3) for x in w.tolist()]}  b={[round(x, 3) for x in b.tolist()]}")

    print("\n===== OOF cross-fit comparison (plan §3.1) =====")
    oof_scores, oof_detail, oof_flips, oof_eligible = {}, {}, {}, {}
    for form in ("temperature", "vector"):
        preds, confs, probs = oof[form]["preds"], oof[form]["confs"], oof[form]["probs"]
        scores = score_predictions(dev_gold, preds, confs, probs, labels)
        eligible, detail = check_eligibility(scores, uncal_scores)
        flips = count_flips(uncal_preds, preds)
        oof_scores[form], oof_detail[form], oof_flips[form], oof_eligible[form] = scores, detail, flips, eligible

        print(f"\n-- {form} --")
        print_score_line(form, scores, uncal_scores, detail)
        print(f"  prediction flips vs uncalibrated: {flips}")
        print(f"  eligible (C1 and C2 and C3): {eligible}")

    # --- deterministic selection hierarchy (plan §3.1, verbatim) ---
    scalar_eligible = oof_eligible["temperature"]
    vector_eligible = oof_eligible["vector"]
    vector_flips_ok = oof_flips["vector"] <= 1

    print("\n===== selection =====")
    if scalar_eligible:
        selected = "temperature"
        print("scalar T is eligible -> selected (scalar is chosen whenever eligible).")
    elif vector_eligible and vector_flips_ok:
        selected = "vector"
        print("scalar T ineligible; vector is eligible AND OOF flips <= 1 -> selected.")
    else:
        print("\nC5 STOP -- no transform passes C1-C3 jointly out-of-fold.")
        print(f"  scalar eligible: {scalar_eligible}  vector eligible: {vector_eligible}  vector OOF flips: {oof_flips['vector']}")
        print("Per plan §5 C5: the owner chooses which clause to relax; that decision must be")
        print("recorded before the candidate is locked and before any fresh-test look.")
        print("calibration.json NOT written.")
        sys.exit(1)

    # --- refit the selected form on the full dev set ---
    print(f"\n===== refit on full dev ({selected}) =====")
    if selected == "temperature":
        T_full = fit_scalar_temperature(dev_logits, gold_idx)
        params = {"T": T_full}
        print(f"  T = {T_full:.6f}")
    else:
        w_full, b_full = fit_vector_scaling(dev_logits, gold_idx, NUM_LABELS)
        params = {"w": [float(x) for x in w_full.tolist()], "b": [float(x) for x in b_full.tolist()]}
        print(f"  w = {[round(x, 4) for x in w_full.tolist()]}")
        print(f"  b = {[round(x, 4) for x in b_full.tolist()]}")

    # --- holdout gate (plan §3.2) -- REQUIRED to write the file. Without --holdout
    # this run is report-only: the CV comparison and selection verdict above are
    # the whole output, and nothing gets written (a candidate that's never cleared
    # a holdout look must not end up served).
    if not args.holdout:
        print("\nREPORT-ONLY: no holdout given, no file written.")
        print("Per plan §3.2, calibration.json is only written after a holdout gate pass --")
        print("rerun with --holdout <fresh calibration-holdout jsonl> to lock this candidate in.")
        return

    print(f"\n===== holdout gate: {', '.join(args.holdout)} =====")
    holdout_texts, holdout_gold, holdout_rows, holdout_skipped = load_eval_set(args.holdout, labels)
    if not holdout_texts:
        sys.exit("error: no usable holdout rows loaded")
    if holdout_skipped:
        print(f"Skipped {holdout_skipped} holdout rows (empty text or label outside {labels})")

    holdout_logits = compute_logits(model, tokenizer, holdout_texts, args.max_length, args.batch_size, device)
    uncal_h_preds, uncal_h_confs, uncal_h_probs = logits_to_predictions(holdout_logits, labels)
    uncal_h_scores = score_predictions(holdout_gold, uncal_h_preds, uncal_h_confs, uncal_h_probs, labels)

    # --- §4.1 support floors, checked BEFORE any gate scoring ---
    floor_ok, ar_count, ar_correct = check_holdout_support_floor(holdout_gold, uncal_h_preds)
    if not floor_ok:
        sys.exit(
            f"error: holdout support floor not met (plan §4.1): {ACTION_REQUIRED} rows="
            f"{ar_count} (need >= {HOLDOUT_MIN_ACTION_REQUIRED}), correct-by-uncalibrated="
            f"{ar_correct} (need >= {HOLDOUT_MIN_ACTION_REQUIRED_CORRECT}) -- no gate "
            f"evaluation, no file written"
        )
    print(f"holdout support floor OK: {ACTION_REQUIRED} rows={ar_count}, correct-by-uncalibrated={ar_correct}")

    if selected == "temperature":
        cal_h_logits = holdout_logits / params["T"]
    else:
        w_t = torch.tensor(params["w"])
        b_t = torch.tensor(params["b"])
        cal_h_logits = holdout_logits * w_t + b_t
    cal_h_preds, cal_h_confs, cal_h_probs = logits_to_predictions(cal_h_logits, labels)
    cal_h_scores = score_predictions(holdout_gold, cal_h_preds, cal_h_confs, cal_h_probs, labels)

    eligible_h, detail_h = check_eligibility(cal_h_scores, uncal_h_scores)
    print_score_line(selected, cal_h_scores, uncal_h_scores, detail_h)

    if selected == "temperature":
        flips_h = count_flips(uncal_h_preds, cal_h_preds)
        c4_pass = flips_h == 0
        print(f"  C4 (scalar, zero flips by construction): flips={flips_h}  {'PASS' if c4_pass else 'FAIL'}")
    else:
        c4_pass, flips_h, per_class_flips, acc_ok_by_class = check_c4_vector_holdout(
            holdout_gold, uncal_h_preds, cal_h_preds, uncal_h_scores, cal_h_scores, labels
        )
        print(f"  C4 (vector): flips={flips_h} (<=1 required)  per-class flips={per_class_flips}")
        print(f"    per-class accuracy non-regression (one-sided, undefined=fail): {acc_ok_by_class}  "
              f"{'PASS' if all(acc_ok_by_class.values()) else 'FAIL'}")
        print(f"  C4 overall: {'PASS' if c4_pass else 'FAIL'}")

    holdout_pass = eligible_h and c4_pass
    print(f"\nholdout gate: {'PASS' if holdout_pass else 'FAIL'} (eligible_h={eligible_h}, C4={c4_pass})")
    if not holdout_pass:
        print("\nHOLDOUT CONSUMED -- gate failed; calibration.json NOT written.")
        print("Any form or threshold change now requires a FRESH holdout (fresh labeled rows),")
        print("never a re-read of this one.")
        sys.exit(1)

    # --- write calibration.json (plan §2 schema) -- only reachable after a holdout pass ---
    if len(labels) != NUM_LABELS or (selected == "vector" and (len(params["w"]) != NUM_LABELS or len(params["b"]) != NUM_LABELS)):
        sys.exit(f"error: internal invariant broken -- payload isn't shaped for the frozen {NUM_LABELS}-label schema")
    payload = {"schema": 1, "kind": selected, "labels": labels, "params": params}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    file_hash = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"\nWrote {out_path}")
    print(f"sha256: {file_hash}")


if __name__ == "__main__":
    main()
