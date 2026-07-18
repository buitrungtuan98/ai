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

## Phase 2 — Auth & Multi-tenancy `DONE`
- [DONE] `P2.1` auth/dependencies.py — get_current_user (solo/Firebase), CurrentUser
- [DONE] `P2.2` ownership guards (get_owned_campaign/channel) returning 404
- [DONE] `P2.3` auth/firebase.py — lazy Firebase verify wrapper
- Verified: solo get-or-create idempotent; owner access ok; cross-tenant + missing id → 404.

## Phase 3 — AI engine & safety `DONE`
- [DONE] `P3.1` core/ai_engine.py — generate_structured + schemas + retry/repair
- [DONE] `P3.2` core/safety_filter.py — profanity/brand-safety + variation/ToS gate
- Verified (mocked Gemini): parse, code-fence strip, retry+repair, safety-block passthrough, exhausted→error; filter remove/mask; variation gate default-off + opt-in; footage license guard.

## Phase 4 — Rendering pipeline (CPU-only) `DONE`
- [DONE] `P4.1` core/ffmpeg_runner.py — nice/threads/progress runner
- [DONE] `P4.2` core/tts.py — edge-tts + word boundaries
- [DONE] `P4.3` core/media.py — ffprobe helpers
- [DONE] `P4.4` core/captions.py — ASS + PIL wrapping
- [DONE] `P4.5` core/thumbnail.py — PIL cover
- [DONE] `P4.6` core/cleanup.py — RenderWorkspace + orphan sweeper
- [DONE] `P4.7` core/video_factory.py — orchestration, audio ground-truth, concat copy, branding
- [DONE] `P4.8` core/pexels.py — footage search/download
- Verified (pure logic): select_clips cycling, scene/concat arg builders, branding filter order (mirror→tint→overlay→ass), ASS generation, wrap_text, A/B rotation. Real ffmpeg render deferred to P9.4.

## Phase 5 — Queue & Worker `DONE`
- [DONE] `P5.1` workers/task_queue.py — queue, render lock, Redis progress, worker_alive
- [DONE] `P5.2` run_worker.py — SimpleWorker, SIGTERM, job_timeout
- [DONE] `P5.3` workers/video_worker.py — pipeline job, buffer hydration, state machine, A/B rotation, error→Telegram
- Verified (fakeredis + sqlite, mocked render/publish): render lock mutual exclusion; buffer hydration + idempotency; state machine (active→completed→auto-activate next); full render_task (COMPLETED, buffer consumed, episode advanced, self-hydration); failure path (FAILED + stack captured).

## Phase 6 — Publishing services `DONE`
- [DONE] `P6.1` services/youtube_service.py — OAuth2 refresh + resumable upload + CTA comment
- [DONE] `P6.2` services/facebook_service.py — Page upload
- [DONE] `P6.3` services/telegram_bot.py — alert helper
- Verified (injected fakes): YouTube token refresh persists to channel; missing-refresh-token error; Facebook creds load/error; Telegram send True/False (never raises). Live uploads deferred to operator (RUNBOOK).

## Phase 7 — Web app & UI `DONE`
- [DONE] `P7.1` main.py — FastAPI, routers, Google OAuth web flow, AJAX task poll, /health
- [DONE] `P7.2` templates/ — dark dashboard (Channels, Campaigns 3-tab, Asset Pool, Credentials, Task Logs)
- [DONE] `P7.3` static/ — self-contained dark CSS + polling app.js
- Verified (TestClient, solo mode): all pages 200; add FB channel (creds encrypted at rest); create + start campaign (queues buffer); save credentials (encrypted); ownership 404; lifespan startup; zero deprecation warnings.
- Browser-side Firebase login: delivered in Phase 11.

## Phase 8 — Automation & lifecycle wiring `DONE`
- [DONE] `P8.1` workers/scheduler.py periodic_tick — hourly buffer hydration; campaign auto-advance already in render_task
- [DONE] `P8.2` Posting-time-slot gating (is_within_slot) drives when episodes are produced
- [DONE] `P8.3` Disk-pressure sweep + buffer expiry; docker-compose json-file log rotation
- [DONE] `P8.4` run_worker starts scheduler in a daemon thread (no extra container)
- Refactor: hydrate_campaign extracted; config moved into Settings (no os.getenv in scheduler); rq-scheduler dropped (YAGNI).
- Verified (fakeredis + sqlite): slot gating (near/far/midnight-wrap), slot-gated tick hydration, buffer expiry + file removal.

## Phase 9 — Verification, tests & hardening `DONE`
- [DONE] `P9.1` pytest suite — 37 tests across crypto/isolation, ai+safety, render units, worker, scheduler, web, services (35 pass, 2 ffmpeg-integration skip without the binary)
- [DONE] `P9.2` FastAPI solo boot; /health; all pages render (TestClient)
- [DONE] `P9.3` Worker ↔ fakeredis; render-lock mutual exclusion
- [DONE] `P9.4` ffmpeg integration test written (real synthetic render + concat-copy); auto-skips here — the sandbox egress policy blocks fetching an ffmpeg binary (apt + static-binary host both denied). Runs in the Docker image (apt ffmpeg) and in CI (.github/workflows/test.yml installs ffmpeg).
- [DONE] `P9.5` ruff clean; docs guard clean; CI workflow added; final docs sync + push

## Phase 10 — Continuous deployment `DONE`
- [DONE] `P10.1` .github/workflows/deploy.yml — push-to-main CD, raw SSH, host-key pinned, configurable port
- [DONE] `P10.2` scripts/deploy.sh — on-VPS build/up + health gate + prune (never touches .env/volumes)
- [DONE] `P10.3` docs: ADR-008, RUNBOOK CD section (required secrets + one-time bootstrap)
- Verified: all workflow YAML parses; embedded run scripts pass `bash -n`; deploy.sh syntax OK.
- Operator sets GitHub Secrets (SSH_HOST/PORT/USER/PRIVATE_KEY/KNOWN_HOSTS/DEPLOY_PATH) + box bootstrap (clone + .env + deploy key). Live deploy is operator-verified.

## Phase 11 — Multi-tenant login UI `DONE`
- [DONE] `P11.1` templates/login.html — dark standalone page: email/password sign-in + sign-up via the Firebase Auth REST API (no CDN/JS SDK)
- [DONE] `P11.2` "Continue with Google" — server-side OAuth → `accounts:signInWithIdp` (auth/firebase.sign_in_with_google_id_token)
- [DONE] `P11.3` POST /auth/session (verify ID token → signed session cookie, JIT-provision), POST /auth/logout, sidebar user chip
- [DONE] `P11.4` get_current_user accepts Bearer OR session cookie; unauthenticated browser navs 303→/login (API keeps plain 401); app.js redirects on 401
- [DONE] `P11.5` Config: FIREBASE_WEB_API_KEY, SECRET_KEY, SESSION_MAX_AGE_DAYS; RUNBOOK "Enable multi-tenant mode"; ADR-009
- Verified: 9 new tests (44 total pass) — solo /login redirect, page render, browser 303 vs API 401, session mint/JIT-provision/logout, invalid token 401, Bearer path, signInWithIdp unit, Google callback + state mismatch. Live boot in multi-tenant mode screenshot-verified (redirect to /login asserted in Chromium).

## Phase 12 — Config truth + Transparency + Preview/review `DONE`
- [DONE] `P12.1` Config truth: background-music mixing (looped, ducked, video still stream-copied), line-style subtitles, honored A/B toggle — no more silent no-ops
- [DONE] `P12.2` Hidden configs exposed: branding (watermark/tint/mirror), privacy, per-campaign buffer size, music volume; TIMEZONE-aware posting slots; edit campaign
- [DONE] `P12.3` Transparency: task started/finished/duration, retry_count, published_video_id/url (+ clickable links); real topic/channel names in task tables; Retry button (upload-only retry when the file survives); dashboard health strip (worker/redis/queue/buffer/disk) + attention banners + meaningful stat tiles; onboarding checklist empty-state; credential Test buttons
- [DONE] `P12.4` Preview/review: render/publish split (render_task + publish_task), AWAITING_REVIEW/awaiting_review/rejected states, authenticated ranged video/thumb streaming, Asset Pool player with Approve & publish / Reject
- [DONE] `P12.5` Fix: advance_campaign committed the episode increment only on status change; additive column upgrades in init_db; ADR-010; RUNBOOK sections
- Verified: 56 tests passing (12 new — music args, line captions, A/B toggle, review-mode flow, per-campaign buffer size, config persistence, edit, retry route, asset stream/approve/reject + range requests, credential tests, api transparency fields, timezone). Live screenshots below.

## Known deferrals (credential-gated — verified by the operator, see RUNBOOK)
- Live Gemini script/metadata generation
- Live Pexels footage download
- YouTube OAuth refresh + real upload
- Facebook Page upload
- Telegram delivery
- Cloudflare Tunnel public exposure
- GitHub PAT backup push
