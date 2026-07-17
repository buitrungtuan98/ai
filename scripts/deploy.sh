#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — runs ON THE VPS to (re)deploy the current checkout.
# The CD workflow SSHes in, updates git to the target branch, then runs this.
# Safe to run by hand for a manual deploy: `bash scripts/deploy.sh`.
#
# Never touches .env (secrets live only on the box) or the named docker volumes
# (SQLite DB + media survive redeploys). The worker's 300s stop grace lets an
# in-flight render finish before its container is recreated.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "[deploy] ERROR: .env not found. Create it once from .env.example before the first deploy."
  exit 1
fi

echo "[deploy] Pulling base images (redis, cloudflared)…"
docker compose pull redis cloudflared || true   # web/worker are built from source, not pulled

echo "[deploy] Building + recreating containers…"
docker compose up -d --build --remove-orphans

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
  exit 1
fi

echo "[deploy] Pruning dangling images to reclaim disk…"
docker image prune -f >/dev/null || true

docker compose ps
echo "[deploy] Done."
