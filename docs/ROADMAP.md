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

## Phase 13 — Cadence, episode memory, persona (humanization), duplicate `DONE`
- [DONE] `P13.1` Slot-timed publishing: rendering eager, publishing one-per-slot from the buffer in the campaign's timezone (SCHEDULED status; double-post guard). Fixes the buffer-dump flaw.
- [DONE] `P13.2` Episode memory: AI returns a synopsis per episode (stored on Task); continuity modes `no_repeat` (fresh premise vs all prior episodes) and `serial` (continue the story).
- [DONE] `P13.3` Persona layer: per-campaign persona, style examples (few-shot), signature open/close catchphrases + always-on anti-AI-tell rules — one human voice across narration/subtitles/titles/descriptions.
- [DONE] `P13.4` Duplicate campaign (`/campaigns/new?from_id=` prefill) + per-campaign timezone.
- [DONE] `P13.5` ADR-011; RUNBOOK "Making the content feel human" + cadence guide.
- Verified: 63 tests passing (7 new — one-per-slot publish + guard, continuous/review exempt, eager hydration, SCHEDULED parking, memory→prompt flow + synopsis store, persona prompt composition, no_repeat/serial prompts, persona/duplicate persistence).

## Phase 14+15 — Cinema Polish + Self-Improving Content Engine `DONE`
- [DONE] `P14.1` Motion on every clip: zoompan zoom-in/out + overscan pan, deterministic per-scene rotation, same encode pass; per-campaign on/off (default on)
- [DONE] `P14.2` Caption themes: classic / highlight (word-pop, accent colour from brand tint) / boxed / neon — ASS-only, zero extra cost
- [DONE] `P14.3` Loop 1: generator→critic→rewrite (hook ≤2s rule, spoken-ness, persona, freshness); critic failure never blocks; per-campaign toggle
- [DONE] `P14.4` Reject-with-reason in review → campaign avoid-list (fed into every future script)
- [DONE] `P15.1` Stats collector: YT Analytics (retention/views/likes; new yt-analytics.readonly scope) + FB insights → Task.stats_json; 48h min age, daily refresh, 30-day window
- [DONE] `P15.2` Playbook distiller: weekly per campaign (≥5 measured episodes, ≥3-video patterns), bounded ≤15 lessons + top-3 examples → Campaign.learning_json (form-proof column)
- [DONE] `P15.3` Performance page per campaign: episode stats table (🏆 best retention), visible playbook/avoid-notes, Reset learning
- [DONE] `P15.4` GEMINI_MODEL env setting (free model upgrades without code change)
- Verified: 72 tests passing (9 new — motion filters + wiring, caption themes/accent/pop, critic loop pass/rewrite/failure, distiller prompt, compose playbook/avoid, stats eligibility windows, distill guards + preservation of operator notes, reject-reason learning, performance page + reset).

## Pre-flight hardening (before first production use) `DONE`
- [DONE] Pexels keywords forced to English (schema + prompt) — Vietnamese campaigns no longer fail footage search
- [DONE] Footage fallback chain: joined query → each keyword → generic backdrop (one weak keyword can't kill an episode)
- [DONE] Stuck-task reaper: tasks frozen in a working state for 2× job timeout (worker crash/OOM) → FAILED with Retry available
- Verified: 75 tests passing (3 new).

## Phase 16 — Auto background music (CC0) `DONE`
- [DONE] `P16.1` services/music_service.py — Freesound search filtered to CC0/public-domain (safe for monetized videos, no attribution), random pick among top matches per episode, local cache, graceful no-music fallback
- [DONE] `P16.2` Campaign music modes: None / Auto (mood, in English) / My file; per-episode music credit stored in metadata
- [DONE] `P16.3` FREESOUND_API_KEY setting (free key); form + config wiring
- Verified: 77 tests passing (2 new — CC0 filter enforced, cache hit skips download, failure→None; worker auto-mode passes the picked file to the renderer and stores the credit).

## Phase 17 — Auto-QC Gate (human review becomes the backup, not the process) `DONE`
- [DONE] `P17.1` Gemini vision helpers (`core/ai_engine.py`): `judge_footage` (does this clip fit the narration?) + `judge_video_frames` (is the finished video watchable — captions readable, visuals coherent?)
- [DONE] `P17.2` `core/qc.py` — footage vetter factory + final-QC runner; every check **fails open** (a vision-API outage never blocks an episode)
- [DONE] `P17.3` Footage vetting in the renderer: up to 3 leading candidates judged per scene, first accepted clip leads, rejected leaders dropped, downloads reused (never fetched twice)
- [DONE] `P17.4` Per-campaign colour grades (cinematic/warm/cool/vivid/noir) baked into the single scene encode, applied before captions so text is never graded
- [DONE] `P17.5` Loudness normalization to −14 LUFS (platform target) in the stitch — audio-only re-encode, video still stream-copied
- [DONE] `P17.6` Worker QC gate: machine reviews each finished master; fail → one automatic re-render; fail again → parked in Asset Pool as AWAITING_REVIEW with the issues listed (never published); verdict stored in episode metadata and shown in the Asset Pool
- [DONE] `P17.7` Form toggles (`auto_qc` default on, `color_grade`), ADR-013, RUNBOOK section
- Verified: 85 tests passing (8 new — vetter threshold + fail-open, final-QC pass/fail/fail-open + frame sampling, candidate reorder/reuse/bounded, grade filter placement + unknown-grade no-op, loudnorm arg builders both paths, worker gate publish-with-verdict / double-fail-parks / off-skips).

## Pre-deployment hardening pass (full-codebase review) `DONE`
Reviewed every module (parallel readers + per-finding adversarial verification), fixed the
confirmed defects (see ADR-014). Highlights: campaign completion off-by-one; reject→retry buffer
unique-constraint fail loop; idempotent publish; SCHEDULED-task recovery on buffer expiry; stale
render-lock crash recovery + PENDING_QUEUE reaping; scheduler per-campaign isolation; ffmpeg
stderr deadlock + zombie-on-callback-error; monotonic progress; encoder `-threads`; fail-safe
footage search/vetting/music; safety-filter no longer falls back to raw text; sweeper spares the
live render; multi-tenant SECRET_KEY fail-fast; OAuth `state` CSRF hole; Secure cookie; credential
verifier no longer leaks keys; `.dockerignore` keeps `.env` out of the image; backup PAT never
persisted/printed; YouTube refresh preserves scopes + rehydrates expiry.
- Verified: 91 tests passing (6 new — campaign-completion semantics, PENDING_QUEUE reaping,
  buffer-replace on re-render, publish idempotency, SCHEDULED-expiry recovery, Range suffix/416,
  monotonic progress, zero-duration clip skip), ruff clean, docs guard green.
- Deferred (documented in ADR-014/RUNBOOK, non-blocking): zoom-motion visual verification on the
  box; `WORK_ROOT` must avoid spaces/quotes; raise `JOB_TIMEOUT_SECONDS` for very long renders.

## Registry-based CD (build in Actions → GHCR → VPS pulls) `DONE`
Moved the Docker build off the render box (ADR-015). `deploy.yml` now builds the `linux/arm64`
image in GitHub Actions, pushes it to GHCR (`:latest` + `:<sha>`), and the deploy job ships
compose + deploy.sh, logs the box into GHCR with the run's ephemeral token, and pulls + restarts.
`docker-compose.yml` runs the GHCR image (tag pinned via `AVF_IMAGE_TAG`, `build: .` kept for local
builds); `deploy.sh` pulls instead of building. Box bootstrap is now just `.env` — no source
checkout, no deploy key, no stored registry secret. Instant rollback via `AVF_IMAGE_TAG=<sha>`.
- Note: private repo → ARM build runs under QEMU (slow first build, then Actions-cached).

## AI campaign designer (propose from a title, or from scratch) `DONE`
"Fill just the title (or nothing) and let AI design the rest." A **✨ Propose full campaign with
AI** button on the New Campaign form calls `POST /campaigns/propose`, which runs
`ai_engine.propose_campaign` (Gemini, temperature 1.1 + a random nonce → a distinct, standout
proposal each click) and returns a complete config — topic, persona, style examples, catchphrases,
continuity, voice (validated against a curated edge-tts list), caption theme, colour grade, motion,
music mood/mode, A/B, privacy, posting slot, CTA, episode count + a one-line rationale. The form
fills in client-side for review; nothing is saved until the operator clicks Create.
- Verified: 94 tests passing (3 new — route success, route needs-key, invalid-voice drop),
  ruff clean, docs guard green.

## Daily pacing — max renders/day cap + min-published watchdog `DONE`
For running several campaigns (and accounts) side by side on one shared Gemini quota:
- `max_per_day` (Distribution tab): caps how many episodes a campaign may START rendering per
  local day — hydration stops at the cap and resumes after midnight (campaign timezone). Slots
  still control publishing cadence; this rations the *generation* budget across campaigns.
- `min_per_day` (Distribution tab): watchdog, not a guarantee — the daily pass alerts via
  Telegram when an active campaign published fewer episodes in the last 24h than its minimum,
  so shortfalls (failures, quota) are never silent.
- Verified: 98 tests passing (2 new — cap beats buffer size + same-day re-hydrate creates none;
  watchdog alerts the behind campaign and stays silent for the on-track one).

## Observability & resilience — quota meter, heartbeat digest, model fallback `DONE`
The factory now tells the operator before it breaks:
- **Quota meter** (`core/usage.py`): every Gemini call attempt is counted in Redis, keyed to
  Google's Pacific quota day. The dashboard health strip shows "AI calls today: N / budget"
  (budget = optional `GEMINI_DAILY_BUDGET` env) and turns amber at 80%.
- **Daily heartbeat digest**: one Telegram line per operator per day — published / failed /
  awaiting-review in the last 24h, AI calls vs budget, disk %. Runs in the daily pass.
- **Model fallback chain**: `GEMINI_MODEL` accepts a comma-separated chain
  (e.g. `gemini-3.1-flash-lite,gemini-flash-latest`); a retired model (404, fail-fast) or a spent
  daily quota fails over to the next entry instead of halting generation. Vision calls use the
  chain's primary. New default: `gemini-flash-lite-latest,gemini-flash-latest`.
- Verified: 101 tests passing (4 new — chain fallback order + all-dead surfaces error, counter
  increments + fail-silent on Redis outage, heartbeat contents; quota fail-fast pinned to a
  single model), ruff clean.

## Known deferrals (credential-gated — verified by the operator, see RUNBOOK)
- Live Gemini script/metadata generation
- Live Pexels footage download
- YouTube OAuth refresh + real upload
- Facebook Page upload
- Telegram delivery
- Cloudflare Tunnel public exposure
- GitHub PAT backup push
