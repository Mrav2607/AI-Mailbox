"""In-process serving for the fine-tuned email classifier.

Loads the encoder saved by ``ml/train_classifier.py`` (model + tokenizer +
labels.json under ``settings.classifier_model_path``) once per process and
serves predictions on the same ``classify`` contract used elsewhere:
``(label, confidence, rationale, model_version)``.

Everything heavy (torch, transformers) is imported lazily inside the load path
so importing this module is cheap and so a deployment WITHOUT those packages (or
without a trained model on disk) degrades gracefully: ``try_predict`` returns
``None`` and the caller falls back to the LLM / heuristic classifier. The
unavailable state is cached, so we only attempt the load -- and log the reason --
once per process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock, Semaphore

from app.core.config import settings
from app.core.logging import logger

_lock = Lock()
_state: tuple | None = None  # (tokenizer, model, labels, device, version)
_unavailable = False  # set once a load attempt has definitively failed

# Caps concurrent forward passes. Each API threadpool thread that reaches
# inference drives its own torch call, and torch fans out intra-op threads per
# call -- unbounded, that oversubscribes the CPU and slows everyone down. Four
# in flight is plenty for a single-box deployment; the rest queue briefly here
# instead of thrashing.
_infer_slots = Semaphore(4)
# How long to wait for a slot before giving up. Waiting forever would pin
# request threads behind a stuck forward pass; returning None instead lets the
# caller fall back to the LLM/heuristic path like every other failure here.
_SLOT_TIMEOUT_S = 5.0


def reset() -> None:
    """Drop the cached model / failure state. Used by tests."""
    global _state, _unavailable
    with _lock:
        _state = None
        _unavailable = False


def _resolve_model_dir() -> Path:
    """Resolve the model dir. Absolute paths are used as-is; a relative path is
    tried against the CWD and then each ancestor of this file, so it works
    whether the process starts at the repo root, apps/api, or /app in a
    container -- without depending on a fixed directory depth."""
    raw = Path(settings.classifier_model_path).expanduser()
    if raw.is_absolute():
        return raw
    candidates = [raw, *(parent / raw for parent in Path(__file__).resolve().parents)]
    for candidate in candidates:
        if (candidate / "config.json").exists():
            return candidate
    return raw  # fall through; caller raises a clear FileNotFoundError


def _load() -> tuple:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    # Keep torch's intra-op pool to half the cores so concurrent inference
    # calls (gated by _infer_slots) don't oversubscribe the CPU between them.
    torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))

    model_dir = _resolve_model_dir()
    if not (model_dir / "config.json").exists():
        raise FileNotFoundError(f"no local classifier model at {model_dir}")

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)

    labels_path = model_dir / "labels.json"
    if labels_path.exists():
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
    else:  # fall back to the id2label baked into the HF config
        id2label = model.config.id2label
        labels = [id2label[i] for i in range(len(id2label))]

    logger.info("Loaded local classifier from %s on %s", model_dir, device)
    return tokenizer, model, labels, device, model_dir.name


def warmup() -> None:
    """Load the model eagerly (e.g. from a startup hook) so the first real
    request doesn't pay for the load. Cheap no-op when already loaded or when
    a prior attempt marked us unavailable; failures degrade exactly like the
    lazy path -- flag it and let callers fall back."""
    global _state, _unavailable

    if _state is not None or _unavailable:
        return
    with _lock:
        if _state is not None or _unavailable:
            return
        try:
            _state = _load()
        except Exception as exc:  # missing deps, missing model, corrupt files
            _unavailable = True
            logger.warning("Local classifier unavailable; falling back (%s)", exc)


def try_predict(text: str) -> tuple[str, float, str, str] | None:
    """Classify ``text`` with the local encoder.

    Returns ``(label, confidence, rationale, model_version)`` or ``None`` when
    the local model can't serve (missing torch/transformers or no model on
    disk), so the caller can fall back.
    """
    global _state, _unavailable

    if _unavailable:
        return None

    # Fast path: once the model's loaded we can read it without the lock.
    # Grab a local ref first so a concurrent reset() can't yank it mid-use.
    state = _state
    if state is None:
        with _lock:
            if _unavailable:
                return None
            if _state is None:
                try:
                    _state = _load()
                except Exception as exc:  # missing deps, missing model, corrupt files
                    _unavailable = True
                    logger.warning("Local classifier unavailable; falling back (%s)", exc)
                    return None
            state = _state
    tokenizer, model, labels, device, version = state

    try:
        import torch

        # Bound how many forward passes run at once -- see _infer_slots above.
        if not _infer_slots.acquire(timeout=_SLOT_TIMEOUT_S):
            logger.warning("Local classifier busy (no slot in %.0fs); falling back", _SLOT_TIMEOUT_S)
            return None
        try:
            enc = tokenizer(text or "", truncation=True, max_length=256, return_tensors="pt").to(device)
            with torch.no_grad():
                logits = model(**enc).logits[0]
                probs = torch.softmax(logits, dim=-1)
                conf, idx = torch.max(probs, dim=-1)
        finally:
            _infer_slots.release()
        confidence = round(float(conf), 4)
        label = labels[int(idx)]
        return (label, confidence, f"local encoder (p={confidence:.2f})", f"local:{version}")
    except Exception as exc:
        logger.warning("Local classify failed at inference; falling back (%s)", exc)
        return None
