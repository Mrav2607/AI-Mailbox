#!/usr/bin/env bash
# Download the fine-tuned email classifier and unpack it into
# ./models/email-classifier, so `CLASSIFIER_BACKEND=local` has something to
# serve. The model is git-ignored (~1GB, trained on private email data), so it
# ships as chunked assets on the `model-v1` GitHub Release instead.
#
# Usage:
#   ./fetch-model.sh
#   INSTALL_LOCAL_CLASSIFIER=true CLASSIFIER_BACKEND=local docker compose up --build
#
# Needs the GitHub CLI, authenticated and with read access to this repo:
#   gh auth login
set -euo pipefail

TAG="${MODEL_RELEASE_TAG:-model-v1}"
DEST="models/email-classifier"
SENTINEL="$DEST/model.safetensors"

cd "$(dirname "$0")"

if [ -f "$SENTINEL" ]; then
  echo "Model already present at $DEST — nothing to do."
  echo "(delete $DEST to re-download, or set MODEL_RELEASE_TAG for a different release)"
  exit 0
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "error: the GitHub CLI (gh) is required. Install it, then run 'gh auth login'." >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading '$TAG' model chunks..."
gh release download "$TAG" --pattern 'model.tgz.part*' --dir "$tmp"

echo "Reassembling and extracting..."
mkdir -p models
cat "$tmp"/model.tgz.part* | tar xz -C models

if [ ! -f "$SENTINEL" ]; then
  echo "error: extraction finished but $SENTINEL is missing — the archive layout may have changed." >&2
  exit 1
fi

echo "Done. Model is at $DEST."
echo "Run: INSTALL_LOCAL_CLASSIFIER=true CLASSIFIER_BACKEND=local docker compose up --build"
