# Runbook

Operational procedures for the single Oracle Cloud ARM box. Assumes `docker compose` from the repo
root and a populated `.env`.

## First-time setup
1. `cp .env.example .env` and fill every value (see the file's comments).
2. Generate a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   → put it in `FERNET_KEY`. **Back this up offline** — losing it means every stored API key/OAuth
   token is unrecoverable.
3. For public/multi-tenant mode, see **Enable multi-tenant mode** below.
4. Create a Cloudflare Tunnel, copy its token into `TUNNEL_TOKEN`, and map the public hostname to
   `http://web:8000` in the Cloudflare Zero-Trust dashboard.
5. `docker compose up -d`. Confirm health: `docker compose ps` (all `healthy`).

## Enable multi-tenant mode (public registration via Firebase)
1. Firebase console → create a project → **Authentication → Sign-in method**: enable
   **Email/Password** and (optionally) **Google**.
2. Project settings → Service accounts → generate a private key → save as
   `config/firebase_credentials.json` on the box (gitignored).
3. Project settings → General → copy the **Web API Key** into `FIREBASE_WEB_API_KEY`.
4. Set in `.env`: `MULTI_TENANT_MODE=true`, `FIREBASE_CREDENTIALS_PATH`, `FIREBASE_WEB_API_KEY`,
   and a strong `SECRET_KEY` (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
5. For **Continue with Google**: in the Google Cloud console, add a second authorized redirect URI
   `<OAUTH_REDIRECT_BASE>/auth/google/callback` to the same OAuth client used for YouTube connect.
6. `docker compose up -d` — unauthenticated visitors now land on `/login`; accounts are
   JIT-provisioned on first sign-in, each isolated to their own channels/campaigns.

Notes: sessions last `SESSION_MAX_AGE_DAYS` (default 7); disabling a user in Firebase takes effect
at their next login, worst case when the session expires (ADR-009). In **solo mode** there is no
login page — anyone reaching the URL is the admin, so keep the hostname private or put a
Cloudflare Access policy (email OTP) in front of it.

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

## Review mode (preview before publish)
Set a campaign's **Publishing mode** to *Review first*. Each rendered episode then waits in the
**Asset Pool** with an in-browser player; nothing is uploaded until you click **Approve & publish**
(Reject deletes the render — the episode can be re-rendered from Task Logs via Retry). You get a
Telegram ping when an episode is ready for review. Review items do not auto-expire; published items
have their local files cleaned up immediately.

## Retrying failed episodes
Task Logs shows every failure with its full error. **Retry** re-runs the episode; if the rendered
file still exists (upload failed / was awaiting review) only the upload is retried — no re-render.

## Posting-slot timezone
Slots (e.g. `09:00, 18:30`) are interpreted in `TIMEZONE` from `.env` (IANA name, e.g.
`Asia/Ho_Chi_Minh`; default UTC). Set it before relying on scheduled posting times.

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

## Continuous deployment (CD)
Merging to `main` triggers `.github/workflows/deploy.yml`, which SSHes into the VPS and runs
`scripts/deploy.sh` (git reset to `origin/main` → `docker compose up -d --build` → health check →
image prune). Your `.env` and the docker volumes (DB + media) are never touched.

**One-time VPS bootstrap:**
1. `git clone <repo-url> ~/ai` on the box (the deploy path). Give the box **read access** to pull:
   add a read-only **deploy key** (`ssh-keygen`, add the public key to the repo's Deploy Keys) and
   set the clone's remote to SSH, or configure a PAT credential helper.
2. `cd ~/ai && cp .env.example .env` and fill it in (see first-time setup above).
3. Ensure the deploy SSH user can run Docker (in the `docker` group).

**Required GitHub repository Secrets** (Settings → Secrets and variables → Actions):
| Secret | Value |
|---|---|
| `SSH_HOST` | VPS public host/IP |
| `SSH_PORT` | Your **non-default** SSH port. May be a **Variable** (recommended — edit anytime, not sensitive) or a Secret; defaults to 22 if unset. |
| `SSH_USER` | Login user (defaults to `ubuntu` if unset) |
| `SSH_PRIVATE_KEY` | Private key whose public half is in the box's `~/.ssh/authorized_keys` |
| `SSH_KNOWN_HOSTS` | Output of `ssh-keyscan -p <PORT> <HOST>` — pins the host key (recommended). If omitted, the workflow falls back to trust-on-first-use. |
| `DEPLOY_PATH` | Repo path on the box (defaults to `ai`, i.e. `~/ai`) |

**Deploy:** merge the feature branch into `main` (or run the workflow manually from the Actions
tab via *Run workflow*). Watch progress in the Actions tab; the run fails loudly if the web
container doesn't become healthy.

**Changing the SSH port** (it varies on your VPS): the port resolves as
*manual-run input → repo Variable `SSH_PORT` → Secret `SSH_PORT` → 22*. So you can:
- edit the `SSH_PORT` **Variable** in Settings (no commit needed), or
- pass a one-off `ssh_port` when you click *Run workflow*.
The host key does **not** change when you change the port, so `SSH_KNOWN_HOSTS` usually stays valid
(regenerate with `ssh-keyscan -p <newport> <host>` only if the box was rebuilt).

**Rollback:** on the box, `cd ~/ai && git reset --hard <previous-good-sha> && bash scripts/deploy.sh`.

## Emergency: take the app offline
`docker compose stop cloudflared` removes public access instantly while leaving data intact.
