#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# backup_db.sh — daily SQLite backup producer (run by server cron, e.g. 0 3 * * *).
#
# Steps (see ADR-002):
#   1. Guard disk space (VACUUM INTO transiently ~2x the DB size).
#   2. Fold the WAL into the main DB.
#   3. Snapshot with VACUUM INTO (read transaction — does NOT take a long exclusive lock,
#      so it won't fight the single render worker).
#   4. Integrity-check the snapshot.
#   5. Dump an ALLOW-LISTED set of tables to plaintext SQL (never the binary .db).
#   6. Scan the dump for accidental plaintext secrets (defense in depth).
#   7. Commit + push to the private backups repo via a fine-grained PAT.
#   8. Clean up temp files.
#
# Secrets: FERNET_KEY is never in the DB, so Fernet ciphertext in the dump is acceptable.
# GITHUB_PAT / FERNET_KEY must never be committed.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# --- Config (overridable via env) --------------------------------------------
DB_PATH="${DB_PATH:-/data/db/factory.db}"
WORK_DIR="${WORK_DIR:-/data/media}"
SNAPSHOT="${WORK_DIR}/factory_snapshot.db"
DUMP="${WORK_DIR}/factory_dump.sql"
BACKUP_REPO="${BACKUP_REPO:?set BACKUP_REPO, e.g. owner/ai-video-factory-backups}"
BACKUP_BRANCH="${BACKUP_BRANCH:-main}"
GITHUB_PAT="${GITHUB_PAT:?set GITHUB_PAT (fine-grained, single repo, contents:write)}"
CLONE_DIR="${WORK_DIR}/backup_repo"

# The clone contains .git/config with the PAT embedded in the remote URL. Always wipe it on exit
# (success OR failure under `set -e`) so the PAT never persists on disk.
trap 'rm -rf "$CLONE_DIR"' EXIT

# Allow-list of tables to export. A NEW table is not exported until added here — safer than
# a deny-list (a new secret-bearing table can't leak silently).
TABLES=(users channels campaigns tasks buffer_pool)

log() { echo "[backup $(date -u +%FT%TZ)] $*"; }

# --- 1. Disk guard -----------------------------------------------------------
db_size=$(stat -c%s "$DB_PATH")
avail=$(df --output=avail -B1 "$WORK_DIR" | tail -1)
if (( avail < db_size * 2 + 50 * 1024 * 1024 )); then
  log "ERROR: not enough free space for snapshot (need ~2x DB size). Aborting."
  exit 1
fi

# --- 2. Fold WAL -------------------------------------------------------------
log "Checkpointing WAL..."
sqlite3 "$DB_PATH" 'PRAGMA busy_timeout=30000; PRAGMA wal_checkpoint(TRUNCATE);'

# --- 3. Snapshot (no long lock) ----------------------------------------------
log "Snapshotting via VACUUM INTO..."
rm -f "$SNAPSHOT"
sqlite3 "$DB_PATH" "PRAGMA busy_timeout=30000; VACUUM INTO '${SNAPSHOT}';"

# --- 4. Integrity check ------------------------------------------------------
log "Integrity check..."
result=$(sqlite3 "$SNAPSHOT" 'PRAGMA integrity_check;')
if [[ "$result" != "ok" ]]; then
  log "ERROR: integrity_check failed: $result"
  rm -f "$SNAPSHOT"
  exit 1
fi

# --- 5. Dump allow-listed tables to plaintext SQL ----------------------------
log "Dumping tables: ${TABLES[*]}"
sqlite3 "$SNAPSHOT" ".dump ${TABLES[*]}" > "$DUMP"

# --- 6. Plaintext-secret guard ----------------------------------------------
# Fernet tokens start with 'gAAAAA'. Bare high-entropy secret patterns that must never appear —
# covers classic (ghp_) AND fine-grained (github_pat_) PATs plus Google/Gemini (AIza) keys.
if grep -Eiq '(FERNET_KEY|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,})' "$DUMP"; then
  log "ERROR: dump appears to contain a plaintext secret. Aborting push."
  rm -f "$SNAPSHOT" "$DUMP"
  exit 1
fi

# --- 7. Commit + push --------------------------------------------------------
log "Pushing to ${BACKUP_REPO}@${BACKUP_BRANCH}..."
rm -rf "$CLONE_DIR"
remote="https://x-access-token:${GITHUB_PAT}@github.com/${BACKUP_REPO}.git"
# stderr is silenced on every git op touching $remote: a failure message would echo the PAT-bearing
# URL into the cron log.
git clone --depth 1 --branch "$BACKUP_BRANCH" "$remote" "$CLONE_DIR" 2>/dev/null \
  || git clone --depth 1 "$remote" "$CLONE_DIR" 2>/dev/null
cp "$DUMP" "$CLONE_DIR/factory_dump.sql"
(
  cd "$CLONE_DIR"
  git config user.email "backup-bot@ai-video-factory.local"
  git config user.name "AI Video Factory Backup"
  git add factory_dump.sql
  rows=$(sqlite3 "$SNAPSHOT" "SELECT (SELECT count(*) FROM users)||'u/'||(SELECT count(*) FROM campaigns)||'c/'||(SELECT count(*) FROM tasks)||'t';" 2>/dev/null || echo "n/a")
  if git diff --cached --quiet; then
    log "No changes since last backup."
  else
    git commit -m "Backup $(date -u +%FT%TZ) [${rows}]"
    git push origin "HEAD:${BACKUP_BRANCH}" 2>/dev/null  # silence: a push error would echo the PAT URL
    log "Pushed."
  fi
)

# --- 8. Cleanup --------------------------------------------------------------
# $CLONE_DIR is also removed by the EXIT trap (covers failure paths).
rm -rf "$CLONE_DIR"
rm -f "$SNAPSHOT" "$DUMP"
log "Done."
