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

**Asset Pool actions by status:**
- `Awaiting review` → **Approve & publish** · **Re-render** (discard + fresh render) · **Reject
  with reason** (deletes render; reason feeds the AI's avoid-list).
- `Ready` (auto mode, parked for its posting slot) → **Publish now** (skip the slot) ·
  **Discard & re-render** (for a bad render you don't want the slot to auto-publish).
- `Consumed` (published) / `Expired` / `Rejected` → no actions; use Task Logs → Retry if needed.

## The self-improvement loops
Every campaign improves automatically on two levels (see ADR-012):
1. **Critic pass (immediate):** an AI editor reviews every script before render — weak hook,
   essay-like sentences, persona slips or repeated premises trigger one rewrite. On by default;
   per-campaign toggle in the form. When you reject a video in review mode, **type the reason** —
   it becomes an explicit avoid-instruction for all future scripts of that campaign.
2. **Data loop (after ~2 weeks of publishing):** stats (retention %, views, likes) are pulled
   daily for videos 2–30 days old; weekly, each campaign with ≥5 measured episodes gets its
   **Playbook** re-distilled — bounded lessons learned from what this channel's real audience
   rewards, injected into every future script. Inspect/reset it on the campaign's
   **📊 Performance** page. YouTube channels connected before this feature need a one-click
   reconnect (adds the read-only `yt-analytics.readonly` scope).
Model upgrades: set `GEMINI_MODEL` in `.env` when Google ships a better free-tier model.

## Automatic background music (zero manual work)
Set a campaign's **Background music** to *Auto* and give a mood in English (e.g. "dark ambient
horror drone"). Each episode gets a **random CC0 (public-domain) track** matching the mood from
Freesound.org — safe for commercial/monetized videos, no attribution required — downloaded once
into `/data/media/music_cache/` and mixed under the narration. Setup: register a free API key at
freesound.org/apiv2 and set `FREESOUND_API_KEY` in `.env`. If the API is unreachable, the episode
renders without music (never fails). The chosen track (title/author/id) is recorded per episode in
the buffer metadata for transparency.

## Monetization: affiliate links (works from video #1)
Set a campaign's **Affiliate / product link** (Distribution tab, with a short label like
"📚 Sách hay về thời Trần:"). The link is auto-appended to **every description and pinned
comment**, always marked "(affiliate link)" — the disclosure is mandatory under platform and
advertising rules, so it cannot be turned off. Note: very new channels sometimes have link
comments held by YouTube's spam filter; the description link always sticks.

## Script preview (dry run — tune the persona cheaply)
On the campaign form (Core tab): **📝 Preview a script** generates one script from the values
currently in the form — unsaved edits included — and shows the scenes plus the estimated spoken
seconds. 1 AI call, nothing rendered or stored. Iterate persona → preview → adjust until the voice
on paper is right, then create the campaign.

## Content calendar
**Calendar** (sidebar) shows the next 7 days of posting slots per active campaign — weekday gate
and campaign timezone applied — plus each campaign's pre-rendered runway. A slot only publishes if
an episode is `ready`; a low runway with many upcoming slots means renders aren't keeping up
(check quota / Task Logs).

## Auto-QC (machine review — makes hands-off publishing safe)
Every campaign has **Auto-QC** ON by default (Distribution tab). Per episode it:
1. **Vets footage** — the whole episode's lead clips are judged against their narrations in ONE
   batched vision call (rejected scenes swap to their next candidate, checked in one follow-up
   call — ≤2 calls per episode).
2. **Judges the finished video** — 4 sampled frames are checked for readable captions and coherent
   visuals. Fail → the episode is automatically re-rendered once. Fail again → it is **parked in
   the Asset Pool for your review** (with the issues listed) instead of publishing, and you get a
   Telegram ping. The verdict (score/issues/attempts) is shown on each Asset Pool card.
It uses your existing Gemini key and **fails open**: if the vision API is down, episodes render and
publish exactly as if QC were off — quality gating never becomes an availability problem.
Recommended rollout: run new campaigns in *Review first* mode for the first ~2 weeks; once you
trust what Auto-QC lets through, switch to auto-publish and review only what QC rejects.
Related quality knobs: **Colour grade** (Aesthetics tab) gives a channel one consistent look;
audio loudness is always normalized to −14 LUFS (the YouTube/Reels target) — no knob needed.

## TTS failures (edge-tts 403 handshake)
The voice rides Microsoft's undocumented Edge read-aloud endpoint via the `edge-tts` library.
Microsoft rotates the endpoint's auth scheme every so often; when they do, **old library versions
start failing every render** with `WSServerHandshakeError: 403` in `tts.synthesize` (a URL without
a `Sec-MS-GEC=` parameter in the error is the tell for a stale version). Fix: bump `edge-tts` to
the latest release in `requirements.txt`, redeploy (the image rebuild picks it up), Retry the
failed episodes. Transient one-off 403s are retried automatically (3 attempts). If a CURRENT
edge-tts still 403s persistently from the box, Microsoft may be blocking that IP range — the
escape hatch is swapping `core/tts.py`'s backend for a paid/keyed TTS API (e.g. Azure Speech free
tier) behind the same interface.
**After any edge-tts upgrade, verify subtitles on one video**: library defaults can change
silently — e.g. 7.x switched the default from word to SENTENCE boundaries, which produced perfect
videos with zero captions until `boundary="WordBoundary"` was passed explicitly (a regression test
now pins this).

## Retrying failed episodes
Task Logs shows every failure with its full error. **Retry** re-runs the episode; if the rendered
file still exists (upload failed / was awaiting review) only the upload is retried — no re-render.

## Posting slots & cadence
Rendering runs ahead automatically (the buffer stays full); **posting slots control publishing** —
exactly one pre-rendered episode publishes per slot. "One video every night at 21:00" = slot
`21:00`, done. Slots are interpreted in the campaign's own timezone (IANA name, e.g.
`Asia/Ho_Chi_Minh`), falling back to `TIMEZONE` in `.env`. Keep slots ≥ 1 hour apart. No slots =
publish immediately after each render; review mode = publish on your approval.

**Publish days (weekday gate):** check specific days in the campaign form and the slots fire only
on those days (campaign timezone) — e.g. slots `21:00` + days Mon/Wed/Fri = three evenings a week.
All days unchecked = every day. Rendering still runs ahead daily, and day-gated pre-renders are
kept up to ~a week before expiring (instead of the default 72h) so an episode can wait for its
publish day.

## Making the content feel human (persona guide)
The per-campaign **Persona** section is the single biggest quality lever. What works:
1. **Persona**: a specific character — region, age, mood, speech habits ("người miền Tây, thân mật,
   hay dùng 'nha', 'dữ thần'"). Specific beats generic; write it in the target language.
2. **Style examples**: paste 2–3 short samples of the voice you want — your own writing/transcripts
   work best. The AI imitates rhythm and vocabulary (few-shot), not the content.
3. **Catchphrases**: a signature opening and sign-off, repeated every episode — this is what makes
   a channel feel like a person fans recognise.
4. **Continuity**: `Never repeat` for daily-story formats (the AI is shown all previous episode
   synopses); `Serial` for a continuing story.
5. The system always applies anti-AI-tell rules (spoken language, no "let's dive in" phrasing) to
   narration, titles and descriptions alike — subtitles inherit it since they are the narration.
Voice honesty: edge-tts is good neural TTS, but the biggest realism win is that TTS reading
*colloquial spoken text* sounds far more natural than TTS reading essay text — which is exactly
what the persona produces. For a truly human voice later, a paid voice API (e.g. ElevenLabs) can
replace `core/tts.py` behind the same interface.

**Tuning the voice when it sounds stilted (in order of impact):**
1. **Slow it down** — campaign form → *Audio speed adjustment* → **−5** (storytelling/horror often
   likes −8). Default full speed is the #1 cause of the "rushed robot" feel.
2. **Punctuation IS the pacing.** The TTS breathes at commas, lands at periods, and holds on an
   ellipsis (…). The script rules now force short, punctuation-paced sentences and spoken-form
   numbers/abbreviations — but your *style examples* teach it best: paste samples written the way
   you'd actually SPEAK them, pauses and all ("Khuya rồi đó… tắt đèn chưa?").
3. **Try the other voice for your language** (voice field): vi has `vi-VN-HoaiMyNeural` (female) /
   `vi-VN-NamMinhNeural` (male) — the same script can feel very different across voices.
4. **Keep music under it** (Auto music) — a bed masks residual synthetic timbre remarkably well.
5. **The ceiling:** edge-tts has no SSML/emotion tags. If, after 1–4, the voice still isn't good
   enough for your brand, the upgrade path is a paid TTS (ElevenLabs / Azure styles) swapped into
   `core/tts.py` behind the same interface — everything else in the pipeline stays untouched.
**Compliance:** a persona is a creative character — do not imitate a real, identifiable person, and
follow each platform's synthetic/altered-content disclosure rules (YouTube requires disclosure for
realistic synthetic content).

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

## Continuous deployment (CD) — registry model (ADR-015)
Merging to `main` (or *Run workflow*) triggers `.github/workflows/deploy.yml`:
1. **build** — builds the `linux/arm64` image in GitHub Actions and pushes it to the GitHub
   Container Registry (GHCR) as `ghcr.io/<owner>/<repo>:latest` and `:<git-sha>`.
2. **deploy** — SSHes into the VPS, ships `docker-compose.yml` + `scripts/deploy.sh`, logs the box
   into GHCR with the run's **ephemeral token** (no registry secret stored on the box), then
   `docker compose pull` + `up -d` the pinned `:<git-sha>` → health-gate → image prune → GHCR logout.

The image is built in the cloud, so the render box **never spends CPU/RAM building** — it just pulls.
Your `.env` and the docker volumes (DB + media) are never touched. The repo is **private**, so the
first build is slow (ARM emulated via QEMU on x86 runners); subsequent builds reuse the Actions
cache.

**One-time VPS bootstrap (now just `.env` — no source checkout, no deploy key):**
1. Install Docker and add the SSH user to the `docker` group (`sudo usermod -aG docker <user>`;
   re-login).
2. `mkdir -p ~/ai` and create `~/ai/.env` with your secrets (copy the field list from
   `.env.example`). That is the only file you place by hand — the workflow ships everything else.
3. Nothing else: the workflow creates `~/ai/scripts/`, uploads `docker-compose.yml` and
   `deploy.sh`, and authenticates the box to GHCR itself on each run.

**One-time GitHub setup:** ensure Actions may write packages — Settings → Actions → General →
*Workflow permissions* = **Read and write** (default on most repos). No PAT is needed; the built-in
`GITHUB_TOKEN` pushes and pulls the repo's own GHCR image.

**Required GitHub repository Secrets** (Settings → Secrets and variables → Actions):
| Secret | Value |
|---|---|
| `SSH_HOST` | VPS public host/IP |
| `SSH_PORT` | Your **non-default** SSH port. May be a **Variable** (recommended — edit anytime, not sensitive) or a Secret; defaults to 22 if unset. |
| `SSH_USER` | Login user (defaults to `ubuntu` if unset) |
| `SSH_PRIVATE_KEY` | Private key whose public half is in the box's `~/.ssh/authorized_keys` |
| `SSH_KNOWN_HOSTS` | Output of `ssh-keyscan -p <PORT> <HOST>` — pins the host key (recommended). If omitted, the workflow falls back to trust-on-first-use. |
| `DEPLOY_PATH` | Directory on the box holding compose + `.env` (defaults to `ai`, i.e. `~/ai`) |

(There is **no** GHCR/registry secret — the workflow's `GITHUB_TOKEN` covers both push and the box's
pull.)

**Deploy:** merge the feature branch into `main` (or *Run workflow*). Watch the Actions tab; the run
fails loudly if the build fails to push or the web container doesn't become healthy.

**Changing the SSH port:** resolves as *manual-run input → repo Variable `SSH_PORT` → Secret
`SSH_PORT` → 22*. Edit the `SSH_PORT` **Variable** (no commit) or pass a one-off `ssh_port` on
*Run workflow*. The host key doesn't change with the port, so `SSH_KNOWN_HOSTS` stays valid
(regenerate only if the box was rebuilt).

**Rollback (instant — no rebuild):** on the box,
`cd ~/ai && AVF_IMAGE_TAG=<previous-good-sha> bash scripts/deploy.sh`. Find previous SHAs in the
repo's GHCR package (Packages tab) or the commit history. (A manual run needs a one-time
`docker login ghcr.io` with a PAT that has `read:packages`; the CD path logs in automatically.)

## Gemini API quota & cost (free tier vs billing)
Generation runs on the Gemini API, and the **free tier is the real throughput ceiling** — not the
box. How to pick a model and stay within quota:
- **Look up YOUR account's real limits** — AI Studio → the **Rate limits** page (ai.dev/rate-limit)
  lists RPM / TPM / **RPD per model** for your account. Limits differ per model and per account and
  change over time — never assume a number.
- **Pick the model with the largest RPD.** Observed on a live account (2026-07): the flagship
  flash models each had **20 req/day** (and some older ones **0**), while **`gemini-3.1-flash-lite`
  had 500 req/day** — 25× more. Flash-lite quality is fine for short spoken scripts + structured
  JSON, it barely "thinks" (fewer tokens, no truncation risk), and 500/day supports **~60 fully
  Auto-QC'd episodes/day free**. Set it in `.env`: `GEMINI_MODEL=gemini-3.1-flash-lite`.
- **Calls per episode.** Roughly: 1 (script) + 1 (self-critique, if on) + 1-2 (BATCHED footage
  vetting for the whole episode) + 1 (final Auto-QC) — a fully QC'd episode is **~4-5 calls**;
  with QC and critique off it's **~1 call**. A duration-range miss or a parse repair can add one.
- **Quota-efficient failures:** a per-DAY quota 429 now **fails fast** (no retry burn — retrying an
  exhausted daily cap just wastes more of it), while per-minute 429s retry after a longer backoff.
  A `429 … PerDay … FreeTier` in the worker log = daily cap hit; wait for the reset
  (~midnight US-Pacific) or switch to a bigger-RPD model.
- **For serious volume, enable billing** on the Google Cloud project. Flash models cost fractions
  of a cent per call — dozens of QC'd videos/day is cents/month — and limits jump into the
  thousands. The $0 goal holds for the infrastructure (Oracle box, Cloudflare, GHCR).
- **Watch the meter, not the failures:** the dashboard health strip shows **AI calls today**
  (counted per Google's Pacific quota day). Set `GEMINI_DAILY_BUDGET` in `.env` to your model's
  RPD and the chip turns amber at 80% — and the daily Telegram heartbeat includes the same number.
- **Model fallback chain:** `GEMINI_MODEL` accepts a comma-separated list, first = primary
  (e.g. `GEMINI_MODEL=gemini-3.1-flash-lite,gemini-flash-latest`). If the primary is retired
  (404) or its daily quota is spent, generation automatically continues on the next model.

## Running multiple campaigns / accounts on one quota (daily pacing)
Campaigns already run in parallel (each channel/account gets its own campaigns, slots, timezone).
Two Distribution-tab fields pace them against the shared Gemini quota:
- **Max new renders per day** — how many episodes the campaign may *start rendering* per local
  day. Hydration stops at the cap and resumes after midnight. Sizing rule of thumb:
  `sum over campaigns of (max_per_day × calls-per-episode) ≤ your model's RPD`
  (calls/episode ≈ 8 with Auto-QC + critique on, ≈ 1 with both off). Example: 500 RPD free on
  flash-lite → e.g. 5 campaigns × 10 renders/day with QC ≈ 400 calls — fits.
- **Min published per day** — a watchdog: if the campaign publishes fewer than this in 24h, you
  get a Telegram alert (it cannot force publishes — it makes shortfalls loud instead of silent).
Publishing cadence itself is still the posting slots; these fields govern generation pace and
monitoring.

## Operational notes from the hardening review (ADR-014)
- **`WORK_ROOT` / `MEDIA_ROOT` paths:** keep them free of spaces and quotes (the defaults
  `/data/media/...` are fine). Scene render passes embed the subtitle path into an ffmpeg filter
  graph, which is not shell-escaped for exotic characters.
- **Very long renders + Auto-QC:** a QC failure re-renders once, so a single episode can render
  twice inside one job. If your renders routinely approach `JOB_TIMEOUT_SECONDS` (default 45 min),
  raise it so the re-render isn't cut off (it also widens the stuck-task reaper window, which is
  fine).
- **Motion effects:** the subtle zoom-in/zoom-out are ffmpeg-`zoompan`-based; on some builds they
  can look static. Eyeball one rendered video; the pan effect always works, and captions/grade are
  unaffected. It's cosmetic only.
- **Multi-tenant mode requires a real `SECRET_KEY`:** the app now refuses to boot in
  `MULTI_TENANT_MODE=true` with an empty or default `SECRET_KEY` (sessions are signed with it).
  Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Emergency: take the app offline
`docker compose stop cloudflared` removes public access instantly while leaving data intact.
