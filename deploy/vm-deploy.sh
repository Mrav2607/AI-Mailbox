#!/usr/bin/env bash
# Rebuild and restart the production stack from whatever is currently checked
# out. The Deploy workflow runs this after fetching origin/main; it's also safe
# to run by hand on the VM (it's the RUNBOOK's `dc up -d --build`, wrapped).
#
# deploy/.env, models/, and data/ are gitignored, so nothing here touches your
# secrets or the trained model.
set -euo pipefail

cd "$(dirname "$0")/.." # repo root, wherever this is invoked from

docker compose --env-file deploy/.env \
  -f docker-compose.yml -f deploy/compose.prod.yml up -d --build

# Old images pile up on every rebuild and the VM's disk is small. This only
# removes dangling (untagged) images -- never volumes or running containers.
docker image prune -f

echo "Deployed $(git rev-parse --short HEAD)"
