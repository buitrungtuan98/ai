# System Map

Living dependency map — **one row per module/file**. This is the source of truth for "what exists,
what it does, and what depends on what". Updating this table is part of the Definition of Done: add
a row when you add a file, bump `Last updated` when you change one.

Status tokens: `DONE` · `WIP` · `TODO` · `BLOCKED`.

## Application code

| Path | Layer | Responsibility | Inputs | Outputs | Depends on | Status | Last updated |
|------|-------|----------------|--------|---------|------------|--------|--------------|
| `core/config.py` | config | The only place env/`.env` is read; `settings` singleton, fail-fast validation | `.env` | `Settings` | pydantic-settings | DONE | 2026-07-17 |
| `core/security.py` | security | Fernet/MultiFernet encrypt/decrypt util (key rotation, None-safe) | `FERNET_KEY` | ciphertext/plaintext | cryptography, config | DONE | 2026-07-17 |
| `database/types.py` | data | `EncryptedString` TypeDecorator + Python enums (Platform, statuses) | column values | encrypted columns | security | DONE | 2026-07-17 |
| `database/models.py` | data | ORM: Users→Channels→Campaigns→{Tasks,BufferPool}, encrypted creds | — | ORM classes | SQLAlchemy, types | DONE | 2026-07-17 |
| `database/db_session.py` | data | Engine + WAL/busy_timeout/foreign_keys PRAGMA listener, `SessionLocal`, `get_db`, `init_db` | `DATABASE_URL` | sessions | SQLAlchemy, config | DONE | 2026-07-17 |
| `auth/dependencies.py` | auth | `get_current_user` (solo/Firebase), `CurrentUser`, ownership guards (404) | request headers | `User` | firebase, models, config | DONE | 2026-07-17 |
| `auth/firebase.py` | auth | Lazy Firebase Admin init + `verify_id_token` wrapper (only module touching firebase_admin) | ID token | decoded claims | firebase-admin, config | DONE | 2026-07-17 |
| `core/ai_engine.py` | ai | `generate_structured` Gemini wrapper; `VideoScript`/`MetadataSet` schemas; retry/repair | topic, campaign cfg | script + 3 A/B metadata | google-generativeai, pydantic | DONE | 2026-07-17 |
| `core/safety_filter.py` | ai | Profanity/brand-safety term filter; Pexels license check; variation/ToS policy gate | script text, flags | filtered text, gate result | stdlib | DONE | 2026-07-17 |
| `core/ffmpeg_runner.py` | render | DRY subprocess runner: `nice -n 19`, `-threads 4`, `-progress` → progress % | ffmpeg args | files, progress callbacks | ffmpeg (system) | DONE | 2026-07-17 |
| `core/tts.py` | render | edge-tts per scene; returns mp3 path + word-boundary timings | narration, voice, rate | mp3 + timings | edge-tts | DONE | 2026-07-17 |
| `core/media.py` | render | ffprobe helpers (duration/codec); audio duration = ground truth | media path | duration/codec info | ffprobe (system) | DONE | 2026-07-17 |
| `core/pexels.py` | render | Pexels stock-footage search + download (portrait renditions) | query, api key | clip metadata, files | requests | DONE | 2026-07-17 |
| `core/captions.py` | render | Word-by-word ASS subtitles; PIL 1080px text wrapping (shared font loader) | timings, text | `.ass` file | Pillow | DONE | 2026-07-17 |
| `core/thumbnail.py` | render | PIL cover from mid-video frame + wrapped title + optional logo | frame, title | `.jpg` | Pillow, ffmpeg_runner | DONE | 2026-07-17 |
| `core/cleanup.py` | render | `RenderWorkspace` context manager + `sweep_orphans` (nothing >60 min) | job id | removed temp dirs | stdlib | DONE | 2026-07-17 |
| `core/video_factory.py` | render | Orchestration: TTS→footage→captions→render→stitch(copy)→thumbnail→branding→cleanup | script, episode | master.mp4 + thumb + metadata | all core/* , config | DONE | 2026-07-17 |
| `workers/task_queue.py` | queue | Single `renders` queue, global render lock, Redis progress, `worker_alive()` | `REDIS_URL` | `Queue`, lock/progress helpers | redis, rq, config | DONE | 2026-07-17 |
| `workers/video_worker.py` | worker | The job: pipeline, buffer hydration, campaign state machine, A/B rotation, error→Telegram | Task id | published video, DB updates | video_factory, services, models | DONE | 2026-07-17 |
| `run_worker.py` | worker | Entrypoint: one `SimpleWorker`, warm SIGTERM shutdown, job_timeout; starts scheduler thread | — | running worker | task_queue, scheduler, rq | DONE | 2026-07-17 |
| `workers/scheduler.py` | worker | In-process daemon tick: slot-gated buffer hydration, buffer expiry, disk sweep | active campaigns | enqueued jobs, cleanup | video_worker, cleanup, config | DONE | 2026-07-17 |
| `services/youtube_service.py` | publish | OAuth2 token refresh (persist to channel) + resumable upload + CTA comment | video, metadata, channel | uploaded video id | google-api-python-client, google-auth | DONE | 2026-07-17 |
| `services/facebook_service.py` | publish | Page video upload via Page ID + permanent token (decrypted on the fly) | video, metadata, channel | uploaded video id | requests | DONE | 2026-07-17 |
| `services/telegram_bot.py` | publish | DRY alert helper (queued/finished/failed) to a user's chat | message, token, chat id | Telegram message | requests | DONE | 2026-07-17 |
| `main.py` | web | FastAPI app, routers (channels/campaigns/credentials/tasks), Google OAuth web flow, AJAX task poll, `/health` | HTTP | HTML/JSON | fastapi, auth, models, task_queue | DONE | 2026-07-17 |
| `templates/` | web | Jinja2 dashboard (sidebar; dashboard/channels/campaigns/assets/credentials/tasks) | context | HTML | jinja2 | DONE | 2026-07-17 |
| `static/` | web | Dark-theme CSS (Tailwind-flavoured, self-contained) + polling app.js (no runtime CDN) | — | CSS/JS | — | DONE | 2026-07-17 |

## Ops / infra

| Path | Layer | Responsibility | Inputs | Outputs | Depends on | Status | Last updated |
|------|-------|----------------|--------|---------|------------|--------|--------------|
| `docker-compose.yml` | infra | 4 services: redis, web (expose-only), worker (1 cpu-capped), cloudflared | `.env` | running stack | docker | DONE | 2026-07-17 |
| `Dockerfile` | infra | Shared image for web+worker; installs ffmpeg (apt), Python deps | requirements.txt | image | docker | DONE | 2026-07-17 |
| `requirements.txt` | infra | Pinned runtime Python dependencies (ARM-wheel aware) | — | deps | pip | DONE | 2026-07-17 |
| `requirements-dev.txt` | infra | Test/lint deps (pytest, fakeredis, ruff); not in runtime image | — | deps | pip | DONE | 2026-07-17 |
| `Dockerfile`/`.env.example`/`.gitignore` | infra | Shared image build; env template; ignore rules | — | — | docker | DONE | 2026-07-17 |
| `config/tunnel_config.yml` | infra | cloudflared ingress mapping to `web:8000` (documentation of the tunnel) | `TUNNEL_TOKEN` | tunnel routes | cloudflared | DONE | 2026-07-17 |
| `scripts/backup_db.sh` | ops | WAL checkpoint → `VACUUM INTO` → `.dump` plaintext SQL → push to backup repo | `factory.db`, `GITHUB_PAT` | `factory_dump.sql` | sqlite3, git | DONE | 2026-07-17 |
| `scripts/check_docs.py` | ops | Fail if a source file has no `SYSTEM_MAP.md` row (docs-drift guard) | tree, this file | pass/fail | stdlib | DONE | 2026-07-17 |
| `.github/workflows/backup.yml` | ops | Verify committed dump restores + integrity check + retention prune | backup repo | CI result | GitHub Actions | DONE | 2026-07-17 |
| `.github/workflows/test.yml` | ops | CI: install ffmpeg + deps, ruff lint, run pytest (no secrets) | push/PR | CI result | GitHub Actions | DONE | 2026-07-17 |
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
