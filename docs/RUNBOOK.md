# Runbook

Operational procedures for the single Oracle Cloud ARM box. Assumes `docker compose` from the repo
root and a populated `.env`.

## First-time setup
1. `cp .env.example .env` and fill every value (see the file's comments).
2. Generate a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   → put it in `FERNET_KEY`. **Back this up offline** — losing it means every stored API key/OAuth
   token is unrecoverable.
3. For public/multi-tenant mode: set `MULTI_TENANT_MODE=true` and place Firebase service-account JSON
   at `config/firebase_credentials.json`.
4. Create a Cloudflare Tunnel, copy its token into `TUNNEL_TOKEN`, and map the public hostname to
   `http://web:8000` in the Cloudflare Zero-Trust dashboard.
5. `docker compose up -d`. Confirm health: `docker compose ps` (all `healthy`).

## Verify the stack locally (no public exposure)
- `docker compose up -d redis` then run tests: `pytest`.
- Web only: `docker compose up web` and, if you must reach it from the host for debugging, temporarily
  add `ports: ["127.0.0.1:8000:8000"]` (loopback only — never `0.0.0.0`, never commit it).

## Restore the database from a backup
The backup is plaintext SQL in the private backups repo (`factory_dump.sql`).
```bash
docker compose stop web worker
sqlite3 /data/db/factory.db < factory_dump.sql        # into a fresh/empty db path
# integrity check:
sqlite3 /data/db/factory.db 'PRAGMA integrity_check;'  # expect: ok
docker compose start web worker
```
The `FERNET_KEY` used when the data was encrypted must match, or encrypted columns won't decrypt.

## Rotate secrets
- **FERNET_KEY** (zero downtime): prepend a new key so `FERNET_KEY=new,old`. `MultiFernet` decrypts
  with either and encrypts with the first. Re-save each credential once to migrate it, then drop the
  old key.
- **TUNNEL_TOKEN**: rotate in the Cloudflare dashboard, update `.env`, `docker compose up -d cloudflared`.
- **GITHUB_PAT**: issue a new fine-grained PAT (single backup repo, `contents:write`, with expiry),
  update `.env`, done. The old one can be revoked immediately.

## Disk pressure (200 GB SSD)
- Check: `df -h /data` and `du -sh /data/media/*`.
- The worker removes each job workspace on completion/error; `sweep_orphans` clears anything > 60 min.
  To force it: `docker compose exec worker python -c "from core.cleanup import sweep_orphans; sweep_orphans()"`.
- If `no space left on device`: delete stale media under `/data/media` first (deletes still succeed
  when writes fail), then investigate what produced orphans.

## Recover a stuck render / render lock
- Symptom: no renders progress, `render:global-lock` present in Redis.
- A crashed worker leaves the lock, but it has a TTL and expires. To clear immediately:
  `docker compose exec redis redis-cli DEL render:global-lock`.
- Find stuck tasks: rows in `tasks` with status `RENDERING`/`PUBLISHING` and stale `updated_at`.
  Requeue or mark `FAILED` per the situation.

## Safe redeploy
- The worker has `stop_grace_period: 300s` — an in-flight render finishes (or aborts cleanly) before
  SIGKILL. Deploy with `docker compose up -d --build`; do not `docker kill` the worker mid-render.

## Backups
- Producer: server cron runs `scripts/backup_db.sh` daily (`0 3 * * *`). It checkpoints WAL,
  `VACUUM INTO` a snapshot, dumps to `factory_dump.sql`, and pushes to the private backups repo.
- Verifier: `.github/workflows/backup.yml` restores the committed dump on a hosted runner, runs
  `integrity_check`, and prunes history. A backup you can't restore isn't a backup — check the
  workflow is green.

## Emergency: take the app offline
`docker compose stop cloudflared` removes public access instantly while leaving data intact.
