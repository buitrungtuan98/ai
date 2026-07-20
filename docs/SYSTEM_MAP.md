# System Map

Living dependency map — **one row per module/file**. This is the source of truth for "what exists,
what it does, and what depends on what". Updating this table is part of the Definition of Done: add
a row when you add a file, bump `Last updated` when you change one.

Status tokens: `DONE` · `WIP` · `TODO` · `BLOCKED`.

## Application code

| Path | Layer | Responsibility | Inputs | Outputs | Depends on | Status | Last updated |
|------|-------|----------------|--------|---------|------------|--------|--------------|
| `core/config.py` | config | The only place env/`.env` is read; `settings` singleton, fail-fast validation | `.env` | `Settings` | pydantic-settings | DONE | 2026-07-19 |
| `core/security.py` | security | Fernet/MultiFernet encrypt/decrypt util (key rotation, None-safe) | `FERNET_KEY` | ciphertext/plaintext | cryptography, config | DONE | 2026-07-17 |
| `database/types.py` | data | `EncryptedString` TypeDecorator + enums (incl. SCHEDULED, AWAITING_REVIEW, awaiting_review/rejected) | column values | encrypted columns | security | DONE | 2026-07-18 |
| `database/models.py` | data | ORM: Users→Channels→Campaigns→{Tasks,BufferPool}; task timing/retry/published-url/synopsis columns | — | ORM classes | SQLAlchemy, types | DONE | 2026-07-18 |
| `database/db_session.py` | data | Engine + WAL PRAGMA listener, `SessionLocal`, `get_db`, `init_db` (+ additive column upgrades) | `DATABASE_URL` | sessions | SQLAlchemy, config | DONE | 2026-07-18 |
| `auth/dependencies.py` | auth | `get_current_user` (Bearer/session/solo), ownership guards incl. buffer items (404), `get_or_create_user` | request | `User` | firebase, models, config | DONE | 2026-07-18 |
| `auth/firebase.py` | auth | Lazy Firebase Admin init, `verify_id_token`, `sign_in_with_google_id_token` (REST signInWithIdp) | tokens | claims / firebase sign-in | firebase-admin, requests, config | DONE | 2026-07-18 |
| `core/ai_engine.py` | ai | Gemini wrapper; persona+playbook system-prompt composer; hook rule; episode memory; generator→critic loop; playbook distiller; vision judges (footage match, final video QC); AI campaign designer (`propose_campaign`, curated voice validation); schemas | topic, cfg, learning, frames | script + metadata + critiques + playbooks + verdicts + campaign proposals | google-generativeai, pydantic, Pillow, config | DONE | 2026-07-20 |
| `core/safety_filter.py` | ai | Profanity/brand-safety term filter; Pexels license check; variation/ToS policy gate | script text, flags | filtered text, gate result | stdlib | DONE | 2026-07-17 |
| `core/ffmpeg_runner.py` | render | DRY subprocess runner: `nice -n 19`, `-threads 4`, `-progress` → progress % | ffmpeg args | files, progress callbacks | ffmpeg (system) | DONE | 2026-07-19 |
| `core/tts.py` | render | edge-tts per scene; returns mp3 path + word-boundary timings | narration, voice, rate | mp3 + timings | edge-tts | DONE | 2026-07-17 |
| `core/media.py` | render | ffprobe helpers (duration/codec); audio duration = ground truth | media path | duration/codec info | ffprobe (system) | DONE | 2026-07-17 |
| `core/pexels.py` | render | Pexels stock-footage search + download (portrait renditions) | query, api key | clip metadata, files | requests | DONE | 2026-07-19 |
| `core/captions.py` | render | Word/line ASS subtitles with caption themes (classic/highlight/boxed/neon, accent colour, pop animation); PIL wrapping | timings, theme | `.ass` file | Pillow | DONE | 2026-07-19 |
| `core/thumbnail.py` | render | PIL cover from mid-video frame + wrapped title + optional logo | frame, title | `.jpg` | Pillow, ffmpeg_runner | DONE | 2026-07-19 |
| `core/cleanup.py` | render | `RenderWorkspace` context manager + `sweep_orphans` (nothing >60 min) | job id | removed temp dirs | stdlib | DONE | 2026-07-19 |
| `core/video_factory.py` | render | Orchestration: TTS→footage (EN keywords + fallback chain + vision vetting)→motion→colour grade→captions→render→stitch(copy, music, −14 LUFS loudnorm)→thumbnail→cleanup | script, episode, cfg | master.mp4 + thumb + metadata | all core/*, config | DONE | 2026-07-19 |
| `core/qc.py` | ai | Auto-QC gate: footage vetter factory + final-video QC runner (frame sampling → vision verdict); every check fails open | clip/master path, Gemini key | accept/reject, QCResult | ai_engine, ffmpeg_runner, media | DONE | 2026-07-19 |
| `workers/task_queue.py` | queue | Single `renders` queue, render lock, Redis progress, `enqueue_render`/`enqueue_publish`, `worker_alive()` | `REDIS_URL` | `Queue`, helpers | redis, rq, config | DONE | 2026-07-19 |
| `workers/video_worker.py` | worker | render_task (buffer + continuous/slot-scheduled/review) + publish_task; Auto-QC gate (vet footage, judge master, re-render once, else park for review); persona+memory pass-through, synopsis store; hydration, state machine, error→Telegram | Task/Buffer id | published video, DB updates | video_factory, qc, services, models | DONE | 2026-07-19 |
| `run_worker.py` | worker | Entrypoint: one `SimpleWorker`, warm SIGTERM shutdown, job_timeout; starts scheduler thread | — | running worker | task_queue, scheduler, rq | DONE | 2026-07-19 |
| `workers/scheduler.py` | worker | Daemon tick: stuck-task reaper, eager hydration, slot-timed publishing, buffer expiry, disk sweep, daily learning pass | active campaigns | jobs, cleanup, playbooks | video_worker, analytics_service, ai_engine | DONE | 2026-07-19 |
| `services/youtube_service.py` | publish | OAuth2 token refresh (persist to channel) + resumable upload + CTA comment | video, metadata, channel | uploaded video id | google-api-python-client, google-auth | DONE | 2026-07-19 |
| `services/facebook_service.py` | publish | Page video upload via Page ID + permanent token (decrypted on the fly) | video, metadata, channel | uploaded video id | requests | DONE | 2026-07-17 |
| `services/telegram_bot.py` | publish | DRY alert helper (queued/finished/failed) to a user's chat | message, token, chat id | Telegram message | requests | DONE | 2026-07-17 |
| `services/verification.py` | publish | Live credential checks (Gemini/Pexels/Telegram) for the "Test" buttons | keys | (ok, detail) | requests | DONE | 2026-07-19 |
| `services/analytics_service.py` | learn | Collects per-video stats (YT Analytics retention/views, FB insights) into Task.stats_json | channels, video ids | stats_json | googleapiclient, requests | DONE | 2026-07-18 |
| `services/music_service.py` | render | Auto background music: random CC0 track by mood via Freesound API, local cache, per-episode credit | mood, api key | mp3 path + credit | requests | DONE | 2026-07-19 |
| `main.py` | web | FastAPI app: login/session, dashboard (health/counts), campaigns CRUD+edit+duplicate+performance page (form incl. colour grade + Auto-QC), AI campaign designer endpoint (`/campaigns/propose`), asset review (reason→learning), streaming, task API+retry, credential tests, OAuth | HTTP | HTML/JSON | fastapi, auth, models, task_queue, ai_engine | DONE | 2026-07-20 |
| `templates/` | web | Jinja2 dashboard (6 pages) + standalone login.html; sidebar session chip; Asset Pool shows Auto-QC verdicts; New Campaign has an "AI Propose" button that fills the form | context | HTML | jinja2 | DONE | 2026-07-20 |
| `static/` | web | Dark-theme CSS (incl. auth styles) + polling app.js (401→/login) — no runtime CDN | — | CSS/JS | — | DONE | 2026-07-18 |

## Ops / infra

| Path | Layer | Responsibility | Inputs | Outputs | Depends on | Status | Last updated |
|------|-------|----------------|--------|---------|------------|--------|--------------|
| `docker-compose.yml` | infra | 4 services: redis, web (expose-only), worker (1 cpu-capped), cloudflared; web/worker run the GHCR image `ghcr.io/<owner>/<repo>:${AVF_IMAGE_TAG:-latest}` | `.env` | running stack | docker | DONE | 2026-07-20 |
| `Dockerfile` | infra | Shared image for web+worker; installs ffmpeg (apt), Python deps | requirements.txt | image | docker | DONE | 2026-07-17 |
| `requirements.txt` | infra | Pinned runtime Python dependencies (ARM-wheel aware) | — | deps | pip | DONE | 2026-07-17 |
| `requirements-dev.txt` | infra | Test/lint deps (pytest, fakeredis, ruff); not in runtime image | — | deps | pip | DONE | 2026-07-17 |
| `Dockerfile`/`.env.example`/`.gitignore` | infra | Shared image build; env template; ignore rules | — | — | docker | DONE | 2026-07-17 |
| `config/tunnel_config.yml` | infra | cloudflared ingress mapping to `web:8000` (documentation of the tunnel) | `TUNNEL_TOKEN` | tunnel routes | cloudflared | DONE | 2026-07-17 |
| `scripts/backup_db.sh` | ops | WAL checkpoint → `VACUUM INTO` → `.dump` plaintext SQL → push to backup repo | `factory.db`, `GITHUB_PAT` | `factory_dump.sql` | sqlite3, git | DONE | 2026-07-19 |
| `scripts/check_docs.py` | ops | Fail if a source file has no `SYSTEM_MAP.md` row (docs-drift guard) | tree, this file | pass/fail | stdlib | DONE | 2026-07-17 |
| `.github/workflows/backup.yml` | ops | Verify committed dump restores + integrity check + retention prune | backup repo | CI result | GitHub Actions | DONE | 2026-07-17 |
| `.github/workflows/test.yml` | ops | CI: install ffmpeg + deps, ruff lint, run pytest (no secrets) | push/PR | CI result | GitHub Actions | DONE | 2026-07-17 |
| `.github/workflows/deploy.yml` | ops | CD (ADR-015): build linux/arm64 image → push to GHCR → SSH ship compose+deploy.sh, GHCR login (ephemeral token), pull + restart | merge to main | pushed image + deployed stack | GitHub Actions, buildx/QEMU, GHCR, ssh | DONE | 2026-07-20 |
| `scripts/deploy.sh` | ops | On-VPS deploy: docker compose pull (pinned GHCR tag) + up -d, health-gate, image prune (keeps .env/volumes) | shipped compose, `.env`, AVF_IMAGE_TAG | running stack | docker compose | DONE | 2026-07-20 |
| `tests/` | test | pytest suite (crypto, isolation, ai/safety, render units, worker, scheduler, web, services, ffmpeg integration) | — | test results | pytest, fakeredis | DONE | 2026-07-17 |
| `pytest.ini` | test | pytest config (testpaths=tests) | — | — | pytest | DONE | 2026-07-17 |

## Docs

| Path | Responsibility | Status | Last updated |
|------|----------------|--------|--------------|
| `CLAUDE.md` | Agent contract, hard constraints, Definition of Done | DONE | 2026-07-17 |
| `docs/CODING_STANDARDS.md` | DRY/KISS/YAGNI/SOLID/Boy-Scout with repo-concrete examples | DONE | 2026-07-17 |
| `docs/SYSTEM_MAP.md` | This living module map | DONE | 2026-07-17 |
| `docs/ROADMAP.md` | Phased task list with status tokens | DONE | 2026-07-17 |
| `docs/ARCHITECTURE.md` | Topology, trust boundaries, ADR log | DONE | 2026-07-17 |
| `docs/RUNBOOK.md` | Ops procedures (restore, rotate, recover, redeploy) | DONE | 2026-07-17 |
| `README.md` | Human onramp + quick start | DONE | 2026-07-17 |
