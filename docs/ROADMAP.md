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

## Catchy standalone titles + series hashtag + brand prefix `DONE`
Shorts are discovered individually, so titles must be hooks, not filing labels:
- **Title rules** (prompt-enforced): never the series/campaign name, never episode numbering
  ('Ep 5' / 'Tập 3' / 'Part 2'); the hook lands in the first 40 chars; 3 variants take genuinely
  different angles.
- **Series identity moved to the description**: a stable, code-computed ASCII hashtag
  (`series_hashtag()`, e.g. `#LichSuVNNhaTran`) is injected into the prompt verbatim so every
  episode carries the same tag — the series stays findable without polluting titles.
- **Optional catchy brand prefix** per campaign (`title_prefix`, e.g. `🔥 SỬ VIỆT |`) prepended at
  metadata-pick time with the 100-char YouTube cap held; proposed by the AI designer, editable in
  the form (Distribution tab).
- Verified: 108 tests passing (3 new — hashtag stability/diacritics/fallback, prompt bans, prefix
  prepend + cap + absence).

## Weekday publish gate (`posting_days`) `DONE`
Slots can now be limited to chosen weekdays (campaign timezone): checkboxes in the Distribution
tab; empty = every day (backwards compatible). Rendering stays eager; `expire_stale_buffers`
stretches the window to ≥7.5 days for day-gated campaigns so a healthy pre-render isn't destroyed
while waiting for its publish day. Proposed/filled by the AI campaign designer too.
- Verified: 112 tests passing (2 new — day gate + publish_due gating both days; stretched expiry
  keeps a 4-day-old item and still expires a 9-day-old one; form persists days and drops bogus
  values).

## Per-campaign video length range (`duration_min_s`/`duration_max_s`) `DONE`
Target spoken length per episode (10–180s), set in the Core tab (both bounds or none; reversed
bounds auto-ordered). Enforced in two layers: the scriptwriter gets an explicit seconds + word
budget (words-per-second heuristic per language, scaled by the campaign's rate_pct), and a
deterministic post-generation word-count check triggers exactly ONE corrective rewrite when the
draft misses the range by >20% (no extra Gemini calls when on-target). True duration is still
measured at TTS time (audio remains ground truth). Proposed by the AI designer.
- Verified: 113 tests passing (1 new — estimator sanity + rate scaling, prompt budget line,
  length-fix rewrite fires once and the fixed draft wins; form auto-orders reversed bounds).

## Kaizen batch — affiliate links, script preview, calendar, batched QC `DONE`
- **Affiliate monetization**: per-campaign `affiliate_url` + `affiliate_label` (http(s)-validated)
  auto-appended to every description AND pinned comment, always with an "(affiliate)" disclosure.
- **Script preview (dry run)**: `POST /campaigns/preview-script` + a form button — generate one
  script from the CURRENT (unsaved) form values, see scenes + estimated spoken seconds; 1 AI call,
  nothing rendered/stored. Makes persona tuning a 10-second loop.
- **Content calendar** (`/calendar`): 7-day grid of upcoming slots per active campaign (weekday
  gate + campaign timezone aware) with pre-rendered runway counts; continuous/review campaigns
  listed separately.
- **Batched footage vetting**: `produce()` restructured into prep→vet→render phases; the whole
  episode's lead candidates are judged in ONE vision call (rejects swap to candidate #2, verified
  in one follow-up) → **≤2 vetting calls/episode instead of ~1/scene**; a QC'd episode now costs
  ~4-5 Gemini calls (was ~8). All fail-open.
- Verified: 118 tests passing (6 new), ruff clean, docs guard green.

## Kaizen batch 2 — circuit breaker, closed A/B loop, grammar + voice QC `DONE`
- **Failure circuit breaker**: 3 consecutive failed episodes flip the campaign to `failed`
  (hydration/slot publishing skip it) + ONE Telegram alert with resume instructions — a systemic
  fault (dead key, revoked token, spent quota) no longer burns API calls and alert noise all
  night. ▶ Start resumes; a still-queued episode that succeeds anyway self-heals it (ADR-016).
- **Closed A/B loop**: the metadata variant (A/B/C) that actually went live is recorded on the
  Task at publish time (`ab_variant` column); the Performance page adds an "A/B Variant Results"
  card (episodes measured, avg retention, avg views per variant) + a per-episode variant column.
- **Grammar QC**: the existing critic pass gains a `grammar_score` dimension and its system
  prompt demands a rewrite on ANY spelling/grammar/diacritics error (subtitles are the narration
  verbatim — a typo is burned into every frame). Zero extra API calls.
- **Voice QC**, two layers: (1) deterministic `voice_check` after each scene's TTS (ffmpeg
  volumedetect + duration sanity; silent/truncated audio → one re-synthesis, then a loud
  failure) — zero API cost, fails closed; (2) the final-QC vision call now attaches the master's
  audio track (ADTS stream copy) so the SAME Gemini call also judges voice clarity, language and
  music balance — zero extra API calls, falls back to frames-only if extraction fails.
- Verified: 126 tests passing (8 new), ruff clean, docs guard green.

## Usability & reliability batch — voice picker, continuity hardening, music truth `DONE`
- **Per-language voice picker**: the free-text voice field became a dropdown that follows the
  Target language, fed by ONE curated catalog (`core/tts.py VOICE_CHOICES`, 2 vi / 10 en / 6 es
  voices with human labels); the AI designer's `PROPOSABLE_VOICES` derives from the same catalog
  (DRY), a hand-typed legacy voice stays selectable as "(custom)", and switching language resets
  the voice to that language's default.
- **Continuity hardening**: `synopsis` is now REQUIRED in the script schema (an omitted synopsis
  used to leave the episode invisible to later no-repeat/serial prompts — continuity silently
  degraded); the worker additionally falls back to the variant-A title so episode memory is never
  empty. Preview button + RUNBOOK now state that previews are memory-less one-offs, and a RUNBOOK
  section explains how to verify continuity on the Performance page.
- **Background music truth**: Auto music without `FREESOUND_API_KEY` now FAILS the episode with a
  clear error (config truth — it used to silently publish music-less videos); the campaign form
  shows a red warning when the server key is missing; the AI designer downgrades auto→none on a
  keyless box; a niche/non-English mood retries once with a generic query (generic music beats no
  music); the Credentials page gained a live Freesound **Test**.
- Verified: 133 tests passing (7 new), ruff clean, docs guard green.

## Gemini model picker in the UI `DONE`
- **Credentials → Gemini model chain**: the model is chosen in the dashboard instead of by editing
  `.env`. "🔍 Load available models" lists every model the saved key can call (one un-metered REST
  call), annotated with a curated free-tier RPM/TPM/RPD table (`GEMINI_MODEL_CATALOG`, advisory —
  links to Google's authoritative rate-limits page); one click appends a model to the
  comma-separated fallback chain.
- Stored per user (`users.gemini_model`, additive column); blank = server default (`GEMINI_MODEL`
  in `.env` is now only the default). The chain flows everywhere generation happens: script +
  critic, batched footage vetting, final QC, script preview, AI campaign designer, and the weekly
  playbook distiller.
- Verified: 137 tests passing (4 new), ruff clean, docs guard green.

## Dashboard UX/UI refactor `DONE`
- **Design system** in `static/app.css`: dark-first token layer (colour/type/spacing/radii/elevation)
  with ONE semantic status-colour set (`--st-*`) reused by pills, banners, table highlights and chart
  bars; a 12-column grid + auto-fill card grid on a shared spacing rhythm.
- **Component layer**: `templates/macros.html` (pill/page_head/card/stat/progress/bar/banner/empty/field)
  replaces the copy-pasted markup across all pages; `static/ui.js` adds a shared `busyButton` helper
  (generalises the async-button idiom) and the mobile drawer-nav toggle with aria state.
- **Responsive**: intent-grouped nav (Monitor/Content/Setup); the sidebar becomes an off-canvas drawer
  under a top bar ≤720px (it previously vanished with no replacement); tables stack into labelled cards
  on mobile; ≥44px tap targets; both 375px (Operator) and 1280px (Strategist) are first-class.
- **Per-persona pages + all system states**: health strip with a deliberate degraded (red) state +
  AI-quota meter; guided campaign form (AI Propose/Preview lead-in, progressive-disclosure advanced
  sections, sticky save bar); video-first Asset Pool review cards; Performance A/B retention comparison
  bars + episode mini-bars; calendar runway indicators; skeleton loading states; teaching empty states.
- **Data-viz**: hand-rolled CSS/inline-SVG bars only — no chart library, no CDN, no external assets.
- Contracts preserved: route paths, form field names, JS/test element ids + `data-*`, flash whitelist,
  and the `textContent`-only XSS boundary. ADR-019 records the design system.
- Verified: 137 tests passing, ruff clean, docs guard green; every page screenshotted at 375px & 1280px
  (seeded + fresh-install empty state) via the pre-installed Playwright Chromium.

## Dashboard ease-of-use follow-ups `DONE`
- **Live status everywhere**: read-only `GET /api/summary` (reuses the dashboard helpers) drives a
  cross-page attention badge (failures on Task Logs, awaiting-review on Asset Pool, combined on the
  mobile hamburger) and auto-refreshes the dashboard health strip / tiles / banners every 6s — the
  Operator no longer has to reload or even be on the dashboard to notice work.
- **Accessible confirm + toasts**: native `confirm()` on every destructive action replaced by an
  in-page dialog (`data-confirm`); transient aria-live toasts for client feedback.
- **Campaign form is `novalidate` + JS-validated**: fixes a latent trap where a required field on a
  hidden tab silently blocked submit; validation now jumps to the offending field's tab, explains the
  problem, and puts the submit button in a busy state on a valid save. Asset timestamps show relative
  time ("4m ago") with the full UTC time on hover.
- Verified: 138 tests passing (1 new — `/api/summary`), ruff clean, docs guard green; interactions
  (badge, confirm modal, toast, form validation, tab-jump) screenshotted at 375px & 1280px.

## Dashboard UX batch 2 (approved backlog) `DONE`
- **Reviewer flow (Asset Pool)**: status filter chips (with counts), reject-reason quick-pick chips
  from the channel's learned avoid-notes, keyboard review (J/K move · A approve · R reject).
- **Strategist**: Performance retention sparkline (inline SVG); client-side search on Campaigns &
  Task Logs; "Create & Start" one-click on the campaign form (`start_now` reuses the start route).
- **Mobile**: bottom tab bar (Home/Tasks/Assets/Campaigns) with attention badges for one-thumb access;
  a pulsing "Live" freshness indicator on the dashboard.
- **Polish/correctness**: dynamic degraded copy that names the actual culprit; `aria-current` on the
  active nav link; a "reconnecting…" toast when polling drops and a "reconnected" one on recovery.
- **Light theme**: opt-in `[data-theme="light"]` with no-FOUC head script + sidebar/top-bar toggle.
- **Safer deletes**: channel delete requires typing the channel name to confirm.
- Verified: 139 tests passing (1 new — start_now), ruff clean, docs guard green; every new interaction
  (tab bar, light theme, filter/reason chips, keyboard review, search, sparkline, typed-confirm,
  live indicator) screenshotted at 375px & 1280px. ADR-021 records the theme + client-filtering stance.

## Channel → Campaign → Asset linkage (master-detail navigation) `DONE`
- The four flat lists are now a navigable hierarchy: every entity name links to its home, related
  collections show as counts that open a **scoped list** (`/campaigns?channel=`, `/assets?campaign=` /
  `?channel=`, `/tasks?campaign=`), and each scoped view renders a **breadcrumb + "show all"** with the
  URL as the source of truth (server-side scoping; Task Logs filters the live feed client-side).
- **Channels** cards show a campaign rollup ("3 campaigns · 2 active") + Campaigns/Assets drill-downs;
  **Campaign** cards link the channel and add Assets(N)/Tasks; **Asset** cards link campaign + channel;
  **Performance** is promoted to the campaign **hub** (Overview · Assets · Tasks · Edit tab row).
- Additive read-only backend only: optional `?channel=`/`?campaign=` params + rollup `group by` counts;
  no route paths added, no business logic touched. ADR-022 records the pattern.
- Verified: 139 tests passing, ruff clean, docs guard green; the full drill-down flow (channel → its
  campaigns → a campaign's assets/tasks/performance, with breadcrumbs) screenshotted at 1280px.

## Dashboard as a trust instrument (deep UX) `DONE`
- **Triage inbox** ("Needs your attention"): the concrete failed / awaiting-review items with inline
  Retry + Review, or a calm **"All clear"** state — the 30-second glance now yields a verdict, not a
  count. **Activity feed** turns the pipeline into a narrative with relative times, and a client-side
  **"N new since your last visit"** marker answers *what changed?*
- **Review-in-context**: the channel's playbook + avoid-notes sit beside the player in the Asset Pool.
- **Visible learning loop**: reject-with-reason states it becomes a permanent avoid-note; Performance
  shows those notes as the feedback that steers every new script.
- **Live campaign identity card**: a plain-language summary of the channel you're about to create.
- Fixed a latent CSS trap with a global `[hidden]{display:none!important}` so JS-toggled cards/badges
  hide reliably. Backend: two read-only triage queries reusing existing helpers. ADR-023 records it.
- Verified: 139 tests passing, ruff clean, docs guard green; triage/all-clear, activity feed,
  review-criteria, identity card and loop-note screenshotted (seeded + empty states).
- **Factory scorecard + next-publish** (trajectory layer): adds "is the factory winning?" beside
  "what needs me / what happened" — 7-day publish throughput sparkbars, buffer runway (≈ days at
  current cadence), week-over-week retention trend, and the soonest upcoming posting slot across
  active campaigns (each in its own tz). Read-only helpers (`_scorecard`, `_next_publish`) reusing
  scheduler primitives; screenshotted.

## Post-merge fixes + adaptive-first responsive `DONE`
- **Cache-busting (root-cause fix)**: `static_url()` appends a per-file content hash
  (`/static/app.css?v=<sha1>`) so a deploy always invalidates the browser cache — the reported
  "no responsive / unstyled" symptom was a stale cached `app.css` (and a stale `ui.js` would have
  silently dropped the confirm-dialog guards).
- **3 smaller audit fixes**: campaign identity card bound to the wrong form in multi-tenant mode
  (stable `#campaign-form` id) · login page ignored the saved theme (added the no-FOUC head script) ·
  skip-link revealed on mobile tap (now `:focus-visible`, keyboard-only).
- **Adaptive-first responsive**: fluid `clamp()` type + page gutter; intrinsic `auto-fit` grids
  (stats, scorecard); a main column that grows then centres (caps at 1400px — kills the dead space
  on wide monitors); **container-query** table stacking (a table stacks when its wrapper is narrow,
  not just the viewport); three shell tiers — full sidebar >1024 · icon rail 721–1024 · drawer +
  bottom tab bar ≤720. ADR-024 records it.
- Verified: 139 tests, ruff clean, docs guard green; width sweep {375, 768, 1024, 1440, 1920}
  screenshotted, main-column centring measured (1400 cap, balanced gutters at 1920), container-query
  stacking asserted, cache-bust hashes confirmed on all three static files.

## Quick wins + pagination `DONE`
- **Asset Pool pagination + server-side filter**: `?status=`/`?page=`, 24 cards/page; filter chips
  are now `<a>` links (URL is the source of truth) with true whole-scope per-status counts from a
  `GROUP BY status` query, replacing the client-side hide-only buttons; Newer/Older pager.
- **Performance episode pagination**: 20 rows/page (newest first); the A/B variant summary, retention
  sparkline and best-episode 🏆 still read the full episode list so no metric is distorted by paging.
- **Immutable static caching**: `CachedStaticFiles` adds `Cache-Control: public, max-age=31536000,
  immutable` for `?v=`-hashed requests only (plain `/static/app.css` stays uncached) — the
  serving-side complement to the ADR-024 content hash.
- **Visibility-aware polling**: both `/api/tasks` and `/api/summary` pollers pause when the tab is
  backgrounded and refresh on return; the task poller also adapts its interval (fast while a job is in
  flight, relaxed when everything is terminal). ADR-025 records the batch.
- Initially deferred Task Logs history pagination — later delivered, see the next batch.
- Verified: 139 tests, ruff clean, docs guard green; on a 48-asset / 36-episode seed the Asset Pool
  paged 24+24 with honest chip counts (16/19/13), Performance paged 20+13 with aggregates intact and
  the 🏆 winner row surviving onto page 2, `Cache-Control: …immutable` present on `?v=` and absent on
  the plain asset, all screenshotted at 375px & 1280px.

## Task Logs history pagination `DONE`
- Turns the live Task Logs feed from a truncated 50-row window into fully reachable, searchable history.
- `/api/tasks` gained `?page=` (25/page, newest first), `?q=` (SQL `ilike` over id / status / campaign
  topic / channel name) and `?campaign=` scope; returns `{tasks, page, pages, total}`; the old hard
  `LIMIT 50` (which hid all older history) is removed.
- `app.js`: debounced server-side search box + Newer/Older pager, a request-sequence guard so a slow
  in-flight poll can't overwrite a newer pager/search action, adopts the server's clamped page, and
  keeps the ADR-025 adaptive/visibility polling (history pages are all-terminal → auto-relax to slow).
- Search + scope moved server-side on purpose: they now span the *whole* history (incl. Vietnamese
  topic text), not just the rows currently in the browser. ADR-026 records the decision + the reversal
  of the ADR-025 deferral.
- Verified: 140 tests (1 new — `/api/tasks` pagination/search/scope), ruff clean, docs guard green; on
  a 38-task seed page 1 showed 25 rows (newest #38/Ep129 first) + "Page 1 of 2 · 38 tasks", page 2
  showed 13, `?page=99` clamped to 2, `q=Trần` matched 36 across both pages, `q=failed` found the lone
  FAILED task (which lives on page 2) from page 1, and the pager collapsed on single-page results;
  screenshotted at 375px & 1280px.

# Pipeline v2 — realism & long-format (backend)

Multi-batch upgrade to the video pipeline: better scripts, more human editing/sound, long-video
support, stronger QC. Everything stays automation-first and zero-cost (free tiers only). Order: A → C
→ B → D → E.

## Batch A — Script depth & humanization `DONE`
- **Research brief (deep mode)**: optional per-campaign `script_depth` (`standard` default | `deep`).
  Deep mode runs `generate_brief` → `EpisodeBrief` (3-8 concrete facts + hook→build→payoff→cliffhanger
  arc) and conditions the script prompt on it, so narration carries real substance not filler. One
  extra Gemini call per episode (against the same daily budget meter); fail-open.
- **AI-cliché gate**: per-language blacklist (`AI_CLICHES`) + pure `find_cliches()`; injected into the
  script prompt and the critic prompt, and a free deterministic post-draft check forces one targeted
  rewrite if any tell survives (e.g. "delve into", "hãy cùng tìm hiểu"). Clean drafts add no call.
- Wired end-to-end: worker passes `script_depth`; campaign form has a standard/deep selector; the AI
  designer proposes a depth. ADR-027 records it.
- Verified: 144 tests (4 new — cliché detection, prompt injection, deep-mode brief call, one-rewrite
  gate), ruff clean, docs guard green.

## Batch C — Sound craft `DONE`
- **Paced narration**: `tts.synthesize_paced` renders each sentence separately and stitches them with
  deterministic breath gaps (`pause_after` — longer after `?`/`!`/`…`), returning one merged word-
  timing list with absolute offsets so captions still align. Single-sentence scenes fall through to
  the old `synthesize()`. One ffmpeg re-encode (`aevalsrc` silence + `concat`).
- **Music ducking**: `build_concat_args` replaces the flat `volume`+`amix` bed with `sidechaincompress`
  — narration `asplit` into a mix copy + sidechain key, music compressed against the voice, ducked
  music mixed back. Video still stream-copied; loudnorm still normalizes the final mix.
- ADR-028 records it. Two new CI-only integration tests push both audio graphs through real ffmpeg so
  an invalid compressor/silence option can't ship silently.
- Verified: 147 tests (3 new units — sentence split/pause, paced-concat arg shape, timing-merge; +2
  ffmpeg-gated graph validators that skip without ffmpeg), ruff clean, docs guard green.

## Batch B — Edit rhythm (the "human editor" feel) `DONE`
- **Multi-shot scenes**: `plan_shots` slices each scene into ~3s shots (cap 4.5s) with cuts landing
  on word boundaries, cycling the clip pool so consecutive shots differ. No clip sits a whole scene.
- **Shot trim**: `build_scene_args` gained `shot_durations` — each clip is `trim`med to its shot in
  the SAME single encode pass (concat-copy stitch untouched). Shot length is bounded by clip native
  length (no black gap); a coverage step absorbs any sub-frame shortfall into the last shot.
- **Cross-episode footage dedupe**: new `ChannelClipUsage` table; worker loads recent clip ids,
  `prefer_unused` floats fresh footage first, and used ids are recorded after render. Fail-open.
- **Per-episode motion seed**: motion effect indexed by `episode_number` so episodes don't share an
  identical rhythm. `select_clips` removed (superseded by `plan_shots`). ADR-029 records it.
- Deliberately omitted dip-to-black transitions — they'd force a re-encode at concat and break the
  stream-copy stitch (the biggest CPU saver on the ARM box).
- Verified: 150 tests (4 new — plan_shots coverage/cap/snap, shot-trim args, prefer_unused reorder,
  worker dedupe round-trip; select_clips test replaced; +trim path added to the ffmpeg integration
  render), ruff clean, docs guard green.

## Batch D — Long-video support `DONE`
- **RenderProfile**: per-campaign `video_format` (`short` default | `long`) selects geometry — short =
  vertical 1080×1920 (unchanged), long = 16:9 1920×1080. `motion_filter`, `build_scene_args`,
  `build_ass`, `generate_thumbnail` and Pexels orientation all read the profile; it defaults to
  `short` so every existing call/test renders byte-identical vertical output (purely additive).
- **Long-form script**: `VideoScript.scenes` cap raised to 40; prompt branches (12-30 scenes,
  part-numbered titles welcome — inverse of the Shorts rule). `CampaignProposal` proposes a format.
- **Chapters**: `chapter_lines` emits YouTube description timestamps from scene starts (≥10s-spaced,
  ≥3 or none). Publishing unchanged — YouTube auto-classifies by aspect/duration.
- **Bounds + UI**: duration clamps 60-900s for long (vs 10-180s short); campaign form gets a
  short/long selector (+ propose-fill) with a CPU-cost hint. ADR-030 records it.
- Deferred within D: multi-call chaptered *generation* (outline + per-chapter) — single-call with the
  raised scene cap suffices for a first cut; the repair loop absorbs oversized drafts.
- Verified: 155 tests (5 new — profiles/geometry, caption dims, chapter_lines, long prompt branch,
  web format+duration bounds; +1 ffmpeg-gated 16:9 scene render), ruff clean, docs guard green.

## Batch E — QC upgrades `DONE`
- **Deterministic QC**: `run_deterministic_qc` adds free, no-API checks on the master —
  `media.max_black_span` (ffmpeg blackdetect) and `media.max_silence_span` (silencedetect) — failing
  the gate on a >2.5s black stretch or >3.5s silence.
- Runs inside the Auto-QC gate beside the vision judge; the episode advances only if BOTH pass (issues
  merged into the stored QC report). Fails CLOSED on catastrophic breakage, per-detector fail-OPEN so
  a probe glitch never blocks a good render — and it still guards when the vision API fails open.
- Considered and left out (YAGNI): caption-overflow (needs a render-and-measure pass, not deterministic)
  and hook-present (already enforced by the script prompt + critic). ADR-031 records it.
- Verified: 157 tests (2 new units — flags black/silence, per-detector fail-open; +1 ffmpeg-gated real
  black+silent master), ruff clean, docs guard green.

## Pipeline v2 — status
All five batches (A script depth · C sound · B edit rhythm · D long-format · E QC) are DONE, each
committed with tests green + docs. Still automation-first and zero-cost (free tiers only); the
render-concurrency-1 lock and CPU-only constraints are untouched. Follow-ups noted inline: per-user
Gemini budget setting (awaiting go), and multi-call chaptered generation for very long videos.

# UI/UX restructure — "one episode, one home"

Fixes the fragmentation where one episode's story is scattered across Task Logs / Asset Pool /
Calendar / Performance. North star: one episode = one home · one filter language · one persistent
scope · act where you see. Six phases, each additive (no route renamed/removed, tests stay green).

## Phase 1 — Episode view `DONE`
- New `/episodes/{task_id}` page: lifecycle timeline (Queued→Rendering→Review→Scheduled→Published,
  current step from task status), video preview, metadata + Auto-QC verdict, render/retry history,
  stage-aware actions, and published stats (retention/views/likes) once live.
- Actions reuse the existing shared routes (approve/reject/rerender/publish-now/retry) via a
  `return_to` form field guarded to `/episodes/<digits>` only; default `/assets` redirects unchanged.
- Linked from Task Logs rows and Performance episode rows (Asset Pool/Dashboard in later phases).
  ADR-032 records it.
- Verified: 160 tests (3 new — episode view renders lifecycle+actions, 404 guard, return_to redirect
  incl. hostile-path rejection), ruff clean, docs guard green; review/failed/published states
  screenshotted at 1280px + mobile.

## Phase 2 — One filter grammar `DONE`
- Shared `filter_bar` macro (status chips with true counts + server-side search) now renders
  identically on Campaigns, Channels and Asset Pool; all URL-driven via a `query_string` global.
- Chip counts are scope-based (search-independent — "how many exist here"); search + status narrow
  the visible rows + paging count. Asset Pool keeps `pool_total` so an empty search shows "no match",
  not "empty pool". Campaigns' old client-side search removed. ADR-033.
- Task Logs already had server search; its stage chips land with the Phase-4 pipeline (stage tabs).
- Verified: 163 tests (3 new — campaigns filter+search+chip counts, channels search, assets search
  incl. no-match), ruff clean, docs guard green; campaigns filter bar screenshotted.

## Phase 3 — Persistent scope switcher `DONE`
- Sidebar channel `<select>` (desktop + mobile drawer) scopes the workspace to one channel, populated
  by a best-effort `nav_channels(request)` global (reuses auth resolution, fails open).
- Scope lives in the URL (`?channel=<id>` — the existing drill-down param), so it's shareable +
  back-button correct; the scope-aware nav links (Campaigns / Asset Pool / Task Logs) carry it, and
  scoped pages compute chip counts within the scope. ADR-034.
- Verified: 164 tests (1 new — switcher appears with channels + active scope carried onto nav links),
  ruff clean, docs guard green; scoped Campaigns view screenshotted.

## Phase 4 — Episodes pipeline list `DONE`
- New `/episodes` list: every episode as one row grouped by lifecycle stage (Queued / Rendering /
  Review / Scheduled / Published / Failed) — stage tabs with counts + search + scope + pagination,
  reusing the Phase-2 filter grammar; each row → the Phase-1 detail view. Unifies what was split
  between Task Logs (render) and Asset Pool (review).
- "Episodes" is now a primary nav item; Task Logs & Asset Pool kept as routes + linked from the
  Episodes header as the specialized live/review views; mobile tab bar swaps Tasks → Episodes.
  Server-rendered (browse/triage doesn't need the live poller). ADR-035.
- Verified: 165 tests (1 new — stage tabs+counts, stage filter, synopsis search, row→detail links),
  ruff clean, docs guard green; Episodes list screenshotted.

## Phase 5 — Planner (actionable calendar) `DONE`
- Week navigation (`?week=` offset, clamped −8..+12) with Prev/Today/Next + a week label;
  `upcoming_slot_cells` takes the same offset.
- Campaign rows link to their scoped Episodes list; a zero-runway row shows an inline "⚠ buffer
  empty → check episodes" link. Runway + per-campaign-timezone slots unchanged. ADR-036.
- "Render now" deliberately omitted (would need a new queue-enqueue endpoint touching the single-
  render lock / daily cap — beyond this frontend phase); empty-buffer links to where controls exist.
- Verified: 165 tests (calendar test extended — campaign→episodes link, week labels, Today reset,
  clamped out-of-range week), ruff clean, docs guard green; Planner screenshotted.

## Phase 6 — Global search ⌘K `DONE`
- Command palette (⌘K / Ctrl-K, or "/") over one read-only `/api/search` endpoint spanning channels
  / campaigns / episodes (tenant-scoped, per-type capped, min 2 chars, Vietnamese text included).
- `ui.js`: debounced fetch with a request-sequence guard, keyboard nav (↑/↓/↵/Esc), results built
  with textContent/DOM nodes only (XSS-safe); sidebar "🔎 Search ⌘K" button opens it for mouse/mobile.
  Jumps straight to the right home (campaign → its Episodes, episode → its detail). ADR-037.
- Verified: 166 tests (1 new — search spans types, tenant-scoped, min-length, palette present in
  shell), ruff clean, docs guard green; palette screenshotted with live results.

## UI/UX restructure — status
All six phases DONE (Episode view · filter grammar · scope switcher · Episodes pipeline · Planner ·
⌘K search). One episode now has one home; one filter language; one persistent scope; act where you
see. All additive — no route renamed/removed, Task Logs & Asset Pool kept as specialized views.

## Scope-switcher fixes (keep-the-state) `DONE`
- `/tasks` + `/api/tasks` and `/calendar` now truly filter by `?channel=` (via the channel's
  campaigns) — the live feed and calendar match what the switcher shows, instead of a selected
  channel that the page ignored.
- Switcher onchange MERGES `channel` into the current query (keeps an active status/search, resets
  page) rather than replacing it.
- localStorage stickiness: the choice survives visits to the factory-wide Dashboard/setup pages —
  `ui.js` reflects it in the dropdown and rewrites the scope-aware nav links; explicit `?channel=`
  wins; "All channels" clears it. Dashboard stays factory-wide by design (health/quota are
  machine-wide, shared with /api/summary). ADR-038.
- Verified: 168 tests (2 new — /api/tasks channel scope + tasks scope note; calendar channel filter +
  week-nav keeps scope), ruff clean, docs guard green; persistence + filter-keeping verified live in
  a browser (pick channel → scopes; visit Dashboard → remembered; switch/clear → status kept).

## Polish sweep (review findings) `DONE`
- Episodes pager: label now shows the FILTERED total ("N matches"), not the whole-scope count; pager
  URLs built via `query_string` so an unfiltered page no longer emits a malformed `?&page=`.
- Episode timeline: a COMPLETED episode now shows every step done (green) instead of the Published
  step glowing as "current".
- Retry banner: retrying a failed episode whose file still exists (re-publish, no re-render) now shows
  "Publish queued", not "Re-render queued".
- Removed the dead `id="campaign-grid"` left over when the client-side campaign search was replaced.
- Verified: 171 tests (3 new — pager filtered-count + clean URLs, timeline completed-vs-in-progress,
  retry publish-vs-render flash), ruff clean, docs guard green.

## Role clarity + scope-preserving actions `DONE`
- **Campaign actions keep the channel scope**: create/update/start/delete redirect to
  `/campaigns?channel=N` (list forms carry a hidden `scope_channel`; create/edit use the campaign's
  own channel) — an action taken while filtered no longer dumps you back to "all campaigns".
- **Unified entry points**: dashboard triage items, the Task-Logs AWAITING_REVIEW cell, the Review
  cards, and the campaign card all link to the episode's single home (`/episodes/{id}`); the
  Performance hub's Assets+Tasks tabs collapse into one **Episodes** tab.
- **Asset Pool → "Review"**: nav label + page heading renamed (route stays `/assets`) to name its
  job — the video-review workbench — distinct from Episodes (stage tracking) and Task Logs (live).
  ADR-039. No page/route removed.
- Verified: 173 tests (2 new — campaign-action scope preservation incl. no-scope default; review/track
  entry points → episode + Performance Episodes tab), ruff clean, docs guard green; nav rename, campaign
  card, and live scope-preserving Start redirect verified in a browser.

## Campaign hub — one page, three tabs `DONE`
- **Tabbed hub at the clean URL `/campaigns/{id}`** replaces the three separate destinations
  (Performance / global Episodes-filtered / Edit). Three server-rendered tabs share a header
  (breadcrumb + title + status + Start/Duplicate/Delete): **Overview** (`/campaigns/{id}` — playbook,
  A/B retention bars, retention sparkline, episodes/measured/best-🏆 scorecard), **Episodes**
  (`/campaigns/{id}/episodes` — this campaign's stage-tabbed episode list) and **Settings**
  (`/campaigns/{id}/settings` — the edit form).
- **DRY extraction**: the episode stage-list logic became `_episode_list_ctx` (main.py) + the shared
  `templates/_episodes_table.html`, reused by both the global `/episodes` view and the hub Episodes
  tab; the hub header/tab bar is the shared `templates/_campaign_hub.html`.
- **Legacy URLs kept as 307 redirects** — `/campaigns/{id}/performance` → the hub Overview, GET
  `/campaigns/{id}/edit` → Settings — so bookmarks, cross-page links and tests still land right; POST
  `/edit` stays the form target and now returns to the hub Overview after saving. ADR-040.
- **Batch 2 — one way in**: every cross-page campaign link now points at the hub — breadcrumbs on
  Episodes/Review/Task Logs, the Episode view's action (was "Performance ↗", now "Campaign ↗"), and
  the dashboard feed all resolve to `/campaigns/{id}`. The campaign card is decluttered from six
  buttons (Start/Edit/Duplicate/Performance/Episodes/Delete) to three — **Open →** (the hub) + Start +
  Delete — with the actionable "N awaiting review" count surfaced as a card hint; Edit/Duplicate/
  Episodes now live as hub tabs/actions.
- Verified: 173 tests (test_review_and_track_entry_points updated for the in-hub Episodes tab URL),
  ruff clean, docs guard green; all three tabs, the pending-campaign Start action, and the decluttered
  card list verified in a browser at 1280px and 375px.

## Campaign UX bugfixes + Settings page `DONE`
- **Bugfixes** (all reproduced in a browser): the New Campaign form follows the scoped channel
  (`?channel=`) and preselects in new mode (was edit-only); **Duplicate** preselects the source
  campaign's channel; the mobile save bar no longer hides under the bottom tab bar; **"Create &
  Start" actually starts** (the busy-state handler disabled the clicked button before the browser
  serialized it, dropping `start_now` — now carried through as a hidden field); Channels'
  "Add a Facebook Page" is a collapsed `<details>` (no button-plus-open-form); starting from a
  campaign hub stays on the hub.
- **Polish**: hub Episodes badge shows only the awaiting-review count (amber); ⌘K campaign results
  open the hub; the "Review" rename reaches the last labels (Episodes' "Review ↗", the Review
  breadcrumb, the channel card's "Review →"); Cancel on Settings returns to the hub; creating a
  campaign lands on its new hub; hub tabs scroll sideways on mobile instead of wrapping.
- **Settings page** (`/settings`, under Setup): per-user preferences in a new additive
  `users.settings_json` column — new-campaign defaults (language / video format / publish mode /
  total episodes / posting slots) that seed the New Campaign form, and the AI daily budget (moved
  from env-only to per-user, env fallback kept) shown on the dashboard quota meter + Telegram
  heartbeat. Preferences vs secrets: keys stay on Credentials. ADR-041.
- Verified: 174 tests (1 new — Settings save → new-campaign prefill + dashboard budget + clear),
  ruff clean, docs guard green; Settings save/prefill/quota-chip and all bugfixes verified in a
  browser at 1280px and 375px.

## AI Propose designs long-form too `DONE`
- AI Propose was shorts-only — and choosing **Long** then proposing silently reset to Short (the
  response's `video_format` overwrote the form). Fixed end to end: the form sends `video_format`
  with the request; the route forwards it (whitelisted); `propose_campaign` designs FOR the format
  (short vs long prompt guidance) and **forces** the operator's choice onto the result; the proposal
  schema's duration ceiling widens `le=180 → le=900` and durations are clamped to the format range
  (60–900 long / 10–180 short) + auto-ordered — matching the create-time clamp. The form's
  video-length inputs are now format-aware (min/max/placeholder), with matching validation. One AI
  call, unchanged. ADR-042.
- Verified: 176 tests (2 new — route forwards video_format incl. bogus→short; propose forces long +
  clamps durations + prompt designed for long), ruff clean, docs guard green; the format-aware inputs
  and the `video_format=long` propose request verified in a browser.

## Operational visibility — show "now / next", not just "how it went" `DONE`
Reorder the operator-facing surfaces to answer ① what needs me → ② what's happening now →
③ what happens next → ④ how it's going. Shared read-only helpers `_next_slot` / `_campaign_ops`
and macros `sched_facts` / `now_next` feed all four surfaces. ADR-043.
- **Batch E — campaign cards are status boards**: each card shows format+language+schedule chips and
  a live "▶ Rendering Ep N · %" / "⏭ Next post <when> · N ready" / "⚠ Buffer empty — slot will be
  missed" / "Not started" line; the list is sorted active→pending→failed→completed (was creation
  order) and action buttons are pinned to the card bottom so they align. Verified: 176 tests, ruff
  clean; cards checked in a browser.
- **Batch F — hub Overview is status-first**: leads with a "Now & next" strip (Rendering now ·
  Queued · Ready buffer · Next post + the schedule facts + a "Change schedule →" link), a
  plain-language explainer for pending/failed campaigns, and folds the two often-empty cards
  (playbook, A/B) plus the retention trend into ONE "Learning & results" card with a single empty
  state — so opening a young campaign no longer shows two empty cards above the fold. Verified: 176
  tests, ruff clean; active + pending hubs checked in a browser.
- **Batch G — dashboard hierarchy + per-campaign view**: new "Running now" card — one row per
  active campaign showing what it's doing (▶ Rendering Ep N · % / ⏭ Next post / ⚠ Buffer empty)
  with a "N to review" chip + Open link — the operational per-campaign view the home page lacked.
  De-duplicated the numeric bands (the health strip is now infra-only; the buffer count lives once
  in the scorecard runway). Reading order: health → needs-attention → Running now → stat tiles →
  scorecard → activity. `/api/summary` JSON keys unchanged. Verified: 176 tests, ruff clean;
  dashboard checked in a browser.
- **Batch H — calendar is a week planner**: each cell shows what will HAPPEN, not just the time —
  `_calendar_row_cells` assigns ready buffer episodes (lowest-numbered first, the scheduler's real
  rule) to upcoming slots, so a cell reads ● 21:00 Ep 8 (will publish) / ○ 18:00 (empty buffer —
  will be missed, amber) / dimmed past / — gate. Today's column is highlighted; rows gained channel
  + format and link to the hub; a legend explains the marks. Honest caveat: episode projections
  assume the buffer doesn't change. Verified: 176 tests (calendar link assertion → hub), ruff clean;
  planner checked in a browser.
- Verified overall: 176 tests, ruff clean, docs guard green.

## Channel autopilot — manage each channel on the data, zero-cost `DONE`
Opt-in, per-channel. Deterministic rules decide WHEN, AI decides WHAT, the operator picks HOW MUCH
autonomy (Off / Copilot / Autopilot). Judged against each channel's own retention baseline. Runs in
the existing scheduler daemon on a per-channel cadence (default 3h, configurable). ADR-044.
- **Phase I — classification engine** `DONE`: `core/autopilot.py` labels each campaign
  winner / healthy / underperforming / too-early vs its channel baseline (`channel_baseline`,
  `classify_campaigns`) — pure, read-only, no AI calls. Surfaced as a verdict chip on the campaign
  cards and the hub Overview scorecard. Verified: 178 tests (2 new — classification vs baseline,
  and the no-baseline guard), ruff clean, docs guard green; chip checked in a browser.
- **Phase II — the hands** `DONE`: enabled channels manage their own daily work. The scheduler's
  `autopilot_pass` (per-channel cadence via a Redis NX guard, default 3h) does — from the render
  pipeline's already-stored QC verdict, 0 extra AI calls — AI **review** (auto-reject weak/failed
  renders with a reason that feeds the learning loop + re-render; auto-approve & publish strong ones
  in Full-auto, or recommend them for one-click confirm in Copilot; escalate the middle band),
  quota-aware **retry** of genuine render failures (skips operator rejects + quota exhaustion), and
  **catch-up publish** of missed slots (bounded, never bursts). Per-channel control on the Channels
  page (Off/Copilot/Full-auto + cadence + QC thresholds); "🤖 AI recommends" hint on Review cards;
  Telegram summary each cycle. Shared `apply_approve`/`apply_reject` keep the manual + auto paths DRY.
  Verified: 184 tests (6 new — decision thresholds, full-auto approve/reject/escalate, copilot
  recommend-don't-publish, retry skip-rules, catch-up, per-channel cadence guard), ruff clean, docs
  guard green; the Channels autopilot control checked in a browser.
- **Phase III — the brain (Copilot proposals inbox)** `DONE`: `autopilot_propose_channel` files
  deterministic, reversible, evidence-backed strategy suggestions into a new `AutopilotAction` table
  — **extend** a winner near its cap (+25% episodes), plan a **successor** for a healthy one (a
  pending clone of its winning config to review), **wind down** a laggard with ≥5 straight below-avg
  episodes (stops new work; nothing deleted). Idempotent (no duplicate live proposal; won't re-file a
  dismissed one for 30 days). New `/autopilot` inbox page shows each proposal with the numbers behind
  it + Approve/Dismiss; approve applies via `apply_autopilot_action`; the pending count surfaces in
  the dashboard triage. Verified: 188 tests (5 new — proposals by class, idempotency + apply extend,
  wind-down + successor apply, HTTP approve/dismiss + ownership + no-crash on legacy rows), ruff
  clean, docs guard green; inbox checked in a browser.
- **Phase IV — full-auto + weekly strategist + guardrails** `DONE`: in Full-auto mode the pass now
  auto-applies its structural proposals (`autopilot_autoapply_channel`) — extend/wind-down
  immediately, successor with guardrails (respects the `max_active` cap, ≤1 per pass, and the new
  campaign starts **review-first "training wheels"** so its first videos wait for review even in
  full-auto). Creative changes stay operator-confirmed: a once-weekly strategist
  (`autopilot_strategist_channel`) makes ONE Gemini call (`ai_engine.suggest_channel_tune`) —
  guarded by weekly cadence, a Gemini key, AND a daily-budget reserve (skips above 80% so rendering
  is never starved) — and files a suggest-only "tune" (caption/music/rate). Never deletes; one click
  back to Off freezes everything. Verified: 191 tests (3 new — full-auto apply + training wheels,
  max-active cap, strategist files/guards/applies), ruff clean, docs guard green; all pages 200 after
  the changes.
- Verified overall: 191 tests, ruff clean, docs guard green. Zero-cost holds — review reuses the
  render pipeline's QC verdict (0 AI calls), proposals/apply are deterministic, and the strategist is
  ~1 budget-guarded call/week per channel.

## Channel profile — a per-channel persona that localizes every video to its country `DONE`
Each channel gets an explicit persona (`Channel.profile_json`: audience/language/timezone/voice/
style/vision) that flows into every AI touchpoint, so "Channel 1 = Vietnam, Channel 2 = USA" is
something the whole system acts on. Organic platforms have no country switch — the algorithm infers
the audience from language + topic + posting time + who watches — so the profile makes every signal
agree. ADR-045.
- **K1 — the profile** `DONE`: per-channel profile editor + summary chips on the Channels page;
  the New Campaign form seeds language/voice/timezone from the selected channel's profile
  (profile > Settings > default) and re-localizes client-side on channel switch; AI Propose forwards
  the channel so the design (persona/topic/voice/posting time) is localized to its audience; the
  autopilot strategist's scorecard carries the profile. All inputs validated (language whitelist,
  voice vs the TTS catalog, timezone via ZoneInfo). Verified: 193 tests (3 new — profile
  save/validate/prefill, propose forwards the profile), ruff clean, docs guard green; editor +
  channel-switch re-localization checked in a browser.
- **K2 — country signal hardening** `DONE`: YouTube uploads now declare
  `defaultAudioLanguage`/`defaultLanguage` (BCP-47, from the campaign language) — the strongest
  classifier signal for which audience a video targets; the render carries the language into the
  buffer metadata for it. The profile box gained a one-time manual localization checklist (YouTube
  Studio country, Facebook Page region). Verified: 194 tests (1 new — upload declares the language,
  drops unknown values), ruff clean, docs guard green; live upload is operator-verified (RUNBOOK).
- **K3 — audience-geography verification** `DONE`: the daily stats pass fetches views-by-country
  per video (`fetch_youtube_geography` → top country + share, merged into `stats_json`);
  `audience_summary` aggregates it into a channel/campaign verdict (dominant country + avg share +
  whether it matches the profile language's expected countries). The campaign-hub Overview shows an
  "🎯 Audience" line (matches ✅ / off-target ⚠), and the autopilot files an acknowledge-only
  "audience_drift" advisory when a channel's real audience is off-target across ≥3 measured episodes.
  Verified: 196 tests (2 new — audience_summary match/mismatch/none, drift advisory filed +
  idempotent), ruff clean, docs guard green; hub line + acknowledge-only inbox advisory checked in a
  browser (VN channel reaching US → off-target). Live geography fetch is operator-verified (RUNBOOK).

## UX/logic sweep — bugs, channels declutter, cleanup `DONE`
A full-site review (13 pages × desktop/mobile + code checks) turned up a handful of real issues.
- **Batch L — bugs** `DONE`: (A1) "awaiting review" now has ONE source of truth — the buffer review
  queue — so the dashboard tile / sidebar badge / `/api/summary` can no longer disagree with the
  Review page + triage inbox (they were task-status vs buffer-status). (A2) the "🤖 AI recommends"
  hint on a Review card shows only while that channel's autopilot is on (a stale hint no longer
  lingers after autopilot is switched off). (A3) an autopilot successor action now links to the
  campaign it created (Open →) in the decision log. (B3) the hub "Next post" cell reads "after
  review" for review-first campaigns instead of a bare "—". Verified: 198 tests (3 new — buffer-based
  count, stale-hint gate; api-summary test updated to the buffer source), ruff clean, docs green.
- **Batch M — channels declutter** `DONE`: the Channels page was a wall of auto-opened forms (2,512px
  tall for 3 channels). Profile + Autopilot disclosures are now closed by default with self-sufficient
  summaries (`🌍 profile: <vision>` / `not set`; `🤖 ✈️ Full auto · every 3h · QC ≥7/≤4`), and saving a
  profile or autopilot config shows a success banner. Page dropped to ~1,165px (−54%). Verified in a
  browser; 198 tests, ruff clean, docs green.
- **Batch N — cleanup** `DONE`: (A4) removed the dead `upcoming_slot_cells` helper (only a test still
  referenced it after the calendar moved onto `_calendar_row_cells` in batch H); the test now asserts
  the real row-cell shape (today allowed, other days gated). (C1) `/api/summary` now returns
  `autopilot_proposed`, feeding a new Autopilot sidebar badge so open AI proposals are visible from
  any page — the count comes from one shared `_autopilot_proposed_count` helper (DRY with the
  dashboard route). (C2) the campaign Overview route fetched the parent `Channel` three times
  (`_hub_context` + twice inline for the audience line) — now one fetch, reused. (C3) the Credentials
  page links across to Settings (keys ↔ defaults/model are adjacent concerns). Verified in a browser
  (badge shows on the sidebar, Settings cross-link renders, overview pages HTTP 200); 198 tests, ruff
  clean, docs green.

## Timezone picker + channel-page fixes + quality lift `DONE`
Follow-up round: a real timezone dropdown, channel-page bugs, and automatic quality improvements
across footage/encode/learning.
- **Batch O — timezone dropdown + channel-page bugs** `DONE`: free-text IANA entry was error-prone
  (a typo was silently dropped on the profile, or silently misread as UTC by the scheduler on a
  campaign). New `core/timezones.py` is the single source of a friendly, region-grouped `<select>`
  (Việt Nam + SEA first) with **live DST-correct UTC offsets**, used by both the channel profile and
  the campaign Distribution tab; a stored legacy zone stays selectable; campaign save now validates
  the zone like the profile already did. Channel-page fixes: (B1) disclosure summaries no longer
  scatter/interleave — label + value are one flex unit that wraps cleanly; (B2) the profile voice
  picker filters to the selected language (no more en voice on a vi channel); (B3) the voice chip
  shows the friendly name ("Hoài My") not the raw id; (B4) autopilot review thresholds are made
  consistent at save (approve strictly above reject) so stored = shown = used; (B6) the profile
  summary falls back to audience/language when there's no vision line; (B7) opening one card's
  disclosure no longer stretches its row-mate into an empty box. Verified in a browser at 1280/375px,
  dark + light; 205 tests (7 new), ruff clean, docs green.
- **Batch P — footage & vision quality** `DONE`: (P1) `_best_file` was a real bug — it always chose
  a portrait rendition (long-form 16:9 got a cropped strip) and always the largest file (4K downloads
  wasting bandwidth + ARM decode). Now it matches the requested orientation and picks the SMALLEST
  rendition clearing the 1080 short-side floor. (P2) clips whose best rendition is below that floor
  sort last (they'd upscale to soft footage). (P3) in-episode dedupe — a growing seen-set steers
  later scenes off the clips earlier scenes consumed, so overlapping-keyword scenes no longer share a
  lead clip. (P4) smart thumbnail — with the duration known, the cover is the sharpest/most-colourful
  of 5 sampled frames (edge + colour score) instead of one blind mid-video grab. All zero-cost,
  CPU-safe, fail-open. 210 tests (5 new), ruff clean, docs green.
- **Batch Q — encode & finish polish** `DONE`: (Q1) scene scaling uses `lanczos` and CRF drops 23→21
  — a sharper source survives the platforms' re-encode better (~+20% file, same speed class). (Q2)
  the final master gets `+faststart` (moov atom up front) so the Review player + platforms start
  streaming instantly. (Q3) long-form fades video+audio over the last scene's final 1.5s for a real
  ending, riding the existing encode (no new pass); shorts stay abrupt so the last-frame→first-frame
  loop drives rewatches. See ADR-046. 211 tests (1 new), ruff clean, docs green; the real ffmpeg path
  is exercised by the Docker/CI integration test (skipped in this sandbox — no ffmpeg).
- **Batch R — retention-curve learning** `DONE`: the loop learned from one number per video
  (`avg_pct_viewed`); now it also uses YouTube's free second-by-second retention curve. (R1) the Task
  stores a `render_json` scene map (start/end + caption-hook label) at render time — it outlives the
  buffer item. (R2) `analytics_service.fetch_youtube_retention` pulls the curve for measured episodes
  (bounded, best-effort) into `stats_json`. (R3) the pure `core/retention.py` attributes the steepest
  drop-offs to the scene playing there ("Biggest drop-off at 0:08 (scene 2 — 'Background context')"),
  shown on the Episode view (curve + drop markers) and fed into the EXISTING daily playbook-distiller
  call — the scriptwriter now learns WHERE it loses people, at **zero new AI calls**. See ADR-047.
  Browser-verified (curve renders, drops attributed to scenes); 217 tests (6 new), ruff clean, docs
  green.

## Autopilot glass box + smarter decisions `DONE`
The operator couldn't see what autopilot did (only strategy proposals were recorded; approve/reject/
retry/catch-up left no trace), couldn't tell "ran, nothing to do" from "never ran", and the
successor/tune logic was cruder than it needed to be.
- **Batch S (S1–S3) — glass box** `DONE`: (S1) every autonomous decision — approve, reject, escalate,
  retry, catch-up — is now logged as a done `AutopilotAction` with its **reason + evidence**
  (`_log_action`); escalations are logged once (on the ap_hint transition), not every cadence tick;
  the operational log auto-prunes after 90 days (`prune_autopilot_log`). (S2) each pass stamps a
  per-channel **heartbeat** (`last_run` time + one-line summary) into the channel config, so the UI
  shows "🕒 last ran 2h ago" and — crucially — a red "⚠ never ran — check the worker container" when
  a channel is on but has never ticked. (S3) `/autopilot` is now mission control: a per-channel run
  status strip, the proposals inbox, and a **paginated activity feed** of every decision with
  reasoning + evidence chips. Browser-verified; 222 tests (5 new), ruff clean, docs green.
- **Batch S (S4, S6) — smarter strategist** `DONE`: the weekly tune now targets the WEAKEST measured
  campaign (the one that needs help) instead of an arbitrary `campaigns[0]` (S6), and its scorecard
  now carries that campaign's retention drop-off notes (S4), so the AI reasons about which scene
  types to fix — zero extra API calls (reuses stored data). See ADR-048.
- **Batch S (S5) — AI-designed successor** `DONE`: an approved/auto-applied successor is no longer a
  blind "«parent» II" clone. A budget-guarded AI pass designs a fresh angle (topic/persona/
  catchphrases/caption) that carries the proven formula — the base is the parent's config (voice/
  format/schedule/QC retained), the parent's playbook is fed to the designer, and it is fully
  fail-open: no key / over budget / AI error → today's plain clone. See ADR-048.

## Known deferrals (credential-gated — verified by the operator, see RUNBOOK)
- Live Gemini script/metadata generation
- Live Pexels footage download
- YouTube OAuth refresh + real upload
- Facebook Page upload
- Telegram delivery
- Cloudflare Tunnel public exposure
- GitHub PAT backup push
