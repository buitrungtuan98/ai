#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — runs ON THE VPS to (re)deploy from the pre-built GHCR image (ADR-015).
# The CD workflow ships this file + docker-compose.yml, logs the box into GHCR, then runs this.
# Safe to run by hand for a manual deploy / rollback:
#     AVF_IMAGE_TAG=<git-sha> bash scripts/deploy.sh     # a specific build
#     bash scripts/deploy.sh                             # whatever is tagged :latest
# (For a manual run you must `docker login ghcr.io` once first — the CD path logs in for you.)
#
# Never touches .env (secrets live only on the box) or the named docker volumes
# (SQLite DB + media survive redeploys). The worker's 300s stop grace lets an
# in-flight render finish before its container is recreated.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."

export AVF_IMAGE_TAG="${AVF_IMAGE_TAG:-latest}"

if [[ ! -f .env ]]; then
  echo "[deploy] ERROR: .env not found. Create it once from .env.example before the first deploy."
  exit 1
fi

echo "[deploy] Pulling images (app tag: ${AVF_IMAGE_TAG})…"
docker compose pull

echo "[deploy] Recreating containers…"
docker compose up -d --remove-orphans

echo "[deploy] Waiting for web to become healthy…"
status="unknown"
for _ in $(seq 1 30); do
  cid="$(docker compose ps -q web || true)"
  if [[ -n "$cid" ]]; then
    status="$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)"
  fi
  echo "  web health: $status"
  [[ "$status" == "healthy" ]] && break
  sleep 5
done

if [[ "$status" != "healthy" ]]; then
  echo "[deploy] ERROR: web did not become healthy. Recent logs:"
  docker compose logs --tail=60 web || true
  echo "[deploy] Roll back with: AVF_IMAGE_TAG=<previous-good-sha> bash scripts/deploy.sh"
  exit 1
fi

echo "[deploy] Pruning dangling images to reclaim disk…"
docker image prune -f >/dev/null || true

docker compose ps
echo "[deploy] Done."
