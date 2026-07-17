# Roadmap

At-a-glance status of the whole build. Flip tokens as work progresses (part of the Definition of
Done). Legend: `DONE` · `WIP` · `TODO` · `BLOCKED`.

## Phase 0 — Foundation & Standards `DONE`
- [DONE] `P0.1` CLAUDE.md agent contract
- [DONE] `P0.2` docs/ (CODING_STANDARDS, SYSTEM_MAP, ROADMAP, ARCHITECTURE, RUNBOOK)
- [DONE] `P0.3` README.md
- [DONE] `P0.4` docker-compose.yml + Dockerfile
- [DONE] `P0.5` requirements.txt + requirements-dev.txt (pinned, ARM-aware)
- [DONE] `P0.6` config/tunnel_config.yml + firebase creds example
- [DONE] `P0.7` scripts/backup_db.sh + scripts/check_docs.py
- [DONE] `P0.8` .github/workflows/backup.yml
- [DONE] `P0.9` .env.example + .gitignore

## Phase 1 — Data & Config layer `DONE`
- [DONE] `P1.1` core/config.py — Settings singleton, fail-fast
- [DONE] `P1.2` core/security.py — Fernet/MultiFernet util
- [DONE] `P1.3` database/types.py — EncryptedString + enums
- [DONE] `P1.4` database/models.py — Users/Channels/Campaigns/Tasks/BufferPool
- [DONE] `P1.5` database/db_session.py — WAL PRAGMA engine, get_db, init_db
- Verified: crypto round-trip, transparent encrypted columns (raw ciphertext confirmed), WAL active, schema create (`smoke test`).

## Phase 2 — Auth & Multi-tenancy `TODO`
- [TODO] `P2.1` auth/dependencies.py — get_current_user (solo/Firebase), CurrentUser
- [TODO] `P2.2` ownership guards (get_owned_campaign/channel) returning 404
- [TODO] `P2.3` Firebase verify wrapper

## Phase 3 — AI engine & safety `TODO`
- [TODO] `P3.1` core/ai_engine.py — generate_structured + schemas + retry/repair
- [TODO] `P3.2` core/safety_filter.py — profanity/brand-safety + variation/ToS gate

## Phase 4 — Rendering pipeline (CPU-only) `TODO`
- [TODO] `P4.1` core/ffmpeg_runner.py — nice/threads/progress runner
- [TODO] `P4.2` core/tts.py — edge-tts + word boundaries
- [TODO] `P4.3` core/media.py — ffprobe helpers
- [TODO] `P4.4` core/captions.py — ASS + PIL wrapping
- [TODO] `P4.5` core/thumbnail.py — PIL cover
- [TODO] `P4.6` core/cleanup.py — RenderWorkspace + orphan sweeper
- [TODO] `P4.7` core/video_factory.py — orchestration, audio ground-truth, concat copy, branding

## Phase 5 — Queue & Worker `TODO`
- [TODO] `P5.1` workers/task_queue.py — queue, render lock, worker_alive
- [TODO] `P5.2` run_worker.py — SimpleWorker, SIGTERM, job_timeout
- [TODO] `P5.3` workers/video_worker.py — pipeline job, buffer hydration, state machine, A/B rotation, error→Telegram

## Phase 6 — Publishing services `TODO`
- [TODO] `P6.1` services/youtube_service.py — OAuth2 refresh + resumable upload + pinned comment
- [TODO] `P6.2` services/facebook_service.py — Page upload
- [TODO] `P6.3` services/telegram_bot.py — alert helper

## Phase 7 — Web app & UI `TODO`
- [TODO] `P7.1` main.py — FastAPI, routers, Google OAuth web flow, HTMX poll, /health
- [TODO] `P7.2` templates/ — dark dashboard (Channels, Campaigns 3-tab, Asset Pool, Credentials, Task Logs)
- [TODO] `P7.3` static/ — prebuilt Tailwind CSS + assets

## Phase 8 — Automation & lifecycle wiring `TODO`
- [TODO] `P8.1` Hourly buffer hydration + campaign auto-advance tick
- [TODO] `P8.2` Posting-time-slot scheduling
- [TODO] `P8.3` Disk-pressure media sweep + log rotation

## Phase 9 — Verification, tests & hardening `TODO`
- [TODO] `P9.1` pytest suite (crypto, models/isolation, ai parsing mocked, safety, captions, ffmpeg progress, buffer/state fakeredis)
- [TODO] `P9.2` Boot FastAPI solo mode; /health; dashboard renders
- [TODO] `P9.3` Worker ↔ Redis; render lock behaviour
- [TODO] `P9.4` Install ffmpeg; synthetic end-to-end render + cleanup
- [TODO] `P9.5` Final docs sync + push

## Known deferrals (credential-gated — verified by the operator, see RUNBOOK)
- Live Gemini script/metadata generation
- Live Pexels footage download
- YouTube OAuth refresh + real upload
- Facebook Page upload
- Telegram delivery
- Cloudflare Tunnel public exposure
- GitHub PAT backup push
