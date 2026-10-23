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
#
# Self-recovering (R35, ADR-100): BOTH web and worker must turn healthy. If either does not, the
# stack is put back on the last tag that passed this gate (remembered in .deploy/last-good-tag) and
# the script still exits 1, so CI shows the failed deploy while the box keeps running the old build.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."

export AVF_IMAGE_TAG="${AVF_IMAGE_TAG:-latest}"
POLL_SECONDS="${DEPLOY_POLL_SECONDS:-5}"
# web is healthy in ~20s; the worker's healthcheck runs every 30s and only passes once it has
# registered with Redis, so it gets the longer wait.
WEB_TRIES="${DEPLOY_WEB_TRIES:-30}"
WORKER_TRIES="${DEPLOY_WORKER_TRIES:-36}"
STATE_FILE=".deploy/last-good-tag"

if [[ ! -f .env ]]; then
  echo "[deploy] ERROR: .env not found. Create it once from .env.example before the first deploy."
  exit 1
fi

# The tag to fall back to: the last one that passed the health gate, else whatever web runs now.
previous_tag() {
  if [[ -s "$STATE_FILE" ]]; then
    cat "$STATE_FILE"
    return
  fi
  local cid image
  cid="$(docker compose ps -q web 2>/dev/null || true)"
  [[ -n "$cid" ]] || return 0
  image="$(docker inspect --format '{{.Config.Image}}' "$cid" 2>/dev/null || true)"
  [[ "$image" == *:* ]] && echo "${image##*:}"
  return 0
}

# is_healthy <service> — one instant look, no waiting.
is_healthy() {
  local cid
  cid="$(docker compose ps -q "$1" 2>/dev/null || true)"
  [[ -n "$cid" ]] && [[ "$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null)" == "healthy" ]]
}

# wait_healthy <service> <tries> — 0 once the service's healthcheck reports healthy.
wait_healthy() {
  local svc="$1" tries="$2" cid status="unknown"
  for _ in $(seq 1 "$tries"); do
    cid="$(docker compose ps -q "$svc" || true)"
    if [[ -n "$cid" ]]; then
      status="$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)"
    fi
    echo "  $svc health: $status"
    [[ "$status" == "healthy" ]] && return 0
    sleep "$POLL_SECONDS"
  done
  echo "[deploy] $svc did not become healthy. Recent logs:"
  docker compose logs --tail=60 "$svc" || true
  return 1
}

stack_healthy() {
  echo "[deploy] Waiting for web and worker to become healthy…"
  wait_healthy web "$WEB_TRIES" && wait_healthy worker "$WORKER_TRIES"
}

PREVIOUS_TAG="$(previous_tag)"
echo "[deploy] Previous good tag: ${PREVIOUS_TAG:-none}"
# Rolling back only helps when the old build WAS healthy. If the stack is already down (bad .env,
# Redis, disk…), going back would just block every fix from landing — so roll forward instead.
STACK_WAS_HEALTHY=0
if is_healthy web && is_healthy worker; then STACK_WAS_HEALTHY=1; fi

echo "[deploy] Pulling images (app tag: ${AVF_IMAGE_TAG})…"
docker compose pull

echo "[deploy] Recreating containers…"
docker compose up -d --remove-orphans

if ! stack_healthy; then
  # `latest` now names the build that just failed, so it is no rollback target either.
  if [[ -z "$PREVIOUS_TAG" || "$PREVIOUS_TAG" == "$AVF_IMAGE_TAG" || "$PREVIOUS_TAG" == "latest" ]]; then
    echo "[deploy] ERROR: no earlier good tag to roll back to — fix forward and redeploy."
    exit 1
  fi
  if [[ "$STACK_WAS_HEALTHY" != 1 ]]; then
    echo "[deploy] ERROR: the stack was already unhealthy before this deploy, so ${AVF_IMAGE_TAG} stays"
    echo "         up (a rollback would not help). Check .env, Redis and disk: docker compose logs worker"
    exit 1
  fi
  echo "[deploy] ERROR: ${AVF_IMAGE_TAG} is unhealthy — rolling back to ${PREVIOUS_TAG}…"
  export AVF_IMAGE_TAG="$PREVIOUS_TAG"
  docker compose up -d --remove-orphans
  if stack_healthy; then
    echo "[deploy] Rolled back: the stack is running ${PREVIOUS_TAG} again. This deploy FAILED."
  else
    echo "[deploy] CRITICAL: the rollback to ${PREVIOUS_TAG} is unhealthy too — needs a human."
  fi
  exit 1
fi

mkdir -p "$(dirname "$STATE_FILE")"
echo "$AVF_IMAGE_TAG" > "$STATE_FILE"

echo "[deploy] Pruning dangling images to reclaim disk…"
# Every deploy pulls a NEW tag (the git sha), so the previous app image is not "dangling" and
# a plain `prune -f` never freed it — each deploy left about one image behind (R34). Remove unused
# images older than 3 days: recent ones stay for a fast local rollback, GHCR keeps them all (a
# rollback to a pruned tag just pulls it again — the CD run is still logged in to GHCR).
docker image prune -af --filter "until=72h" >/dev/null || true

docker compose ps
echo "[deploy] Done."
