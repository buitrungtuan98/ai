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

## Navigation refactor — one rail, lenses inside their owner `DONE`
A UX pass on the shell: the nav had 10 flat peers mixing places/states/tools, and mobile ran two
competing menus (a bottom bar duplicating the drawer, "Home" vs "Dashboard" for one route).
- **Phase 1 — nav shell** `DONE`: the global rail is now 6 destinations + a Setup cluster (was 10).
  The render log + Review queue are facets of **Episodes**; the Calendar is a view of **Campaigns** —
  demoted from the rail but still reached contextually. Two-level active state lights the parent on a
  child route (`/calendar`→Campaigns, `/assets`→Episodes) via `nav in [...]`. Mobile is now ONE nav
  source: the bottom bar mirrors the rail's top (Dashboard/Campaigns/Episodes/Auto) + a **More**
  button opening the same drawer; the duplicated/renamed items are gone. See ADR-049. Browser-verified
  desktop + 375px; 224 tests (1 updated in lockstep), ruff clean, docs green.
- **Phase 2 — content unification** `DONE`: Calendar is now a view-toggle of Campaigns (a 📅 Calendar
  button on the campaigns head ↔ a ▤ List button + "Campaigns › Calendar" breadcrumb on the calendar
  head, both scope-aware). Episodes is the explicit home for the render log + Review — its head links
  to both (scope-aware) and its subtitle says so.
- **Phase 3 — facet traceability** `DONE`: the demoted lenses always show their "up" path — the
  unscoped render log and Review pages carry an "Episodes › …" breadcrumb, and Calendar a "Campaigns
  › Calendar" one — so every facet is one click from its owner and the rail lights that owner.
  Browser-verified; 225 tests (1 new), ruff clean, docs green.
- Not built (YAGNI for this domain): a per-dashboard **Permissions** tab and a **widget-config
  drawer** from the generic proposal have no backing here — this is a single-operator tool with no
  per-object ACL, and episode/aesthetic config already lives in the campaign hub's Settings tab +
  the episode detail page. Building hollow ACL/drawer UI would violate the repo's KISS/YAGNI contract.

## Near-real-time analytics — early views in ~1h `DONE`
Operator asked why analytics takes ~2 days / ~5 videos. Traced it: retention (Analytics API) has a
~2-day lag by YouTube's design, AND we only polled once daily (stacking +24h), AND the 5-video
learning threshold is deliberate. Fix: serve the data that CAN be fast, honestly.
- **T1 — hourly early views** `DONE`: `collect_early_stats` uses YouTube's Data API (near-real-time
  views/likes/comments, 50 ids/call ≈ 1 quota unit) + Facebook views, for episodes younger than the
  Analytics lag, merged as `{views, likes, early}` on a separate `early_fetched_at` clock.
- **T2 — unstuck retention** `DONE`: `collect_stats` moved from the once-daily pass to its own hourly
  NX guard, so first retention lands ~48h sharp (the YouTube floor) instead of 48–72h. Distiller +
  heartbeat stay daily.
- **T3 — honest UI** `DONE`: the episode view shows live views + an explicit "⏳ Retention arrives
  ~2 days after publish (YouTube processing delay)" note, so a young video reads "live", not broken.
- **T4 — guardrails** `DONE`: early views never set `avg_pct_viewed`; the distiller threshold and the
  Overview scorecard now require it, so early data never trips learning/autopilot. See ADR-051.
- Zero Gemini cost, ~24 YT quota units/day, no new env var. 233 tests (3 new), ruff clean, docs green.

## "Queue stuck" — Task Logs was lying (+ real stuck-detection) `DONE`
A screenshot showed a "stuck" queue. Tracing it, the queue was mostly healthy (rendered episodes
waiting for their posting slots) — the Task Logs DISPLAY had two bugs that made it look frozen, plus
a genuine stuck-worker case wasn't surfaced.
- **F1 — ghost progress** `DONE`: `/api/tasks` showed live Redis % for any non-terminal status, so a
  re-queued task that crashed mid-render kept showing e.g. "Pending Queue · 89.2%". Now live % shows
  only for actively-working statuses; `clear_progress` is called on every re-queue path (retry route,
  `apply_reject` rerender, the reaper).
- **F2 — honest TIME** `DONE`: the TIME column was `finished_at − started_at`, but `finished_at` is
  overwritten with the PUBLISH time for slot-scheduled episodes — so a 2-min render that published
  14 h later at its slot read "886m". Now the render wall-time is stored (`render_json.render_seconds`)
  and shown instead.
- **F3 — real stuck-detection** `DONE`: a worker-down banner on the render log (nothing renders or
  publishes without the worker), and a render-concurrency-SAFE orphaned-lock clear
  (`clear_orphaned_render_lock` — two-tick confirmation so a live render is never mistaken for
  orphaned) so a crashed-worker lock frees fast instead of waiting out the ~46-min TTL.
- Verified in a browser (worker banner shows; a slot-waited task reads "2m 42s" not 886m; pending
  rows show 0% not a ghost). 230 tests (4 new), ruff clean, docs green.

## Component dedup — one control per job, shared macros, dead code out `DONE`
An audit of every macro/partial/CSS class for redundancy + confusing patterns.
- **R1 — one episode tab bar** `DONE`: `/episodes` had TWO overlapping controls (the view-switcher
  AND a stage chip-bar, both with Rendering/Review). Merged into a single `ui.stage_tabs` bar (All ·
  Queued · Rendering→log · Review→workbench · Scheduled · Published · Failed + counts), rendered
  identically on `/episodes`, `/assets`, `/tasks` via one `_episode_stage_counts` helper. Verified:
  exactly one bar per page, correct active tab + consistent counts.
- **R2/R3/R4** `DONE`: extracted shared `pager` (was hand-copied in the episodes table + Review +
  Autopilot) and `scope_note` (was hand-copied across 5 templates) macros; deleted the unused `card`
  macro. (The `/tasks` live-JS pager legitimately differs and is left alone.)
- **Part 2/3** `DONE`: on `/assets` the buffer sub-filters now nest cleanly UNDER the stage-tab bar
  (one hierarchy, not two rival vocabularies); scoped pages keep the breadcrumb and use the slim
  `scope_note` escape.
- **R5** `DONE`: removed dead CSS (`.seg*`, `.col-4/8/12`, `.section-title`, `.minibar`, `.win-row`).
- Verified NOT loops (left as-is): channels⇄autopilot and settings⇄credentials are purposeful peer
  cross-links (each direction a different job). 226 tests, ruff clean, docs green.

## Kill the navigation loop — one Episodes surface `DONE`
The operator reported "back keeps looping" + "hard to manage." A link-graph crawl + a real-browser
Back tracer proved: the browser Back button is fine (Chrome collapses the 307s), but `/episodes`,
`/assets` and `/tasks` cross-linked laterally with scattered "↗" buttons and no active-state — a
genuine in-app loop with no "you are here."
- **Unified surface + loop kill** `DONE`: a single segmented view-switcher (`ui.episode_views` /
  `.seg`) now sits atop `/episodes`, `/assets` and `/tasks`, active-stated, so the three read as one
  "Episodes" surface (verified: each shows the correct active tab). The old lateral "↗" links are
  gone; the episode detail links only UP to its campaign. Legacy dup routes
  (`/campaigns/{id}/performance`, `/{id}/edit`) now 301 (was 307) so they settle instead of
  lingering in Back. A regression test locks all of this. Browser + crawler verified; 226 tests
  (1 new), ruff clean, docs green. See ADR-050.
- Deliberately deferred (larger, lower-value): the strict single-physical-URL fold (merging the
  `/assets` + `/tasks` handlers into `/episodes?stage=` with mass 301s) — it reconciles two chip
  vocabularies and churns ~11 test call-sites for a purity gain the switcher already delivers to the
  operator. Revisit only if a strict canonical-URL requirement appears.

## Studio Mode — AI-drawn consistent-character visuals (2nd video source) `DONE`
A second way to build a video, alongside Pexels stock footage: AI-drawn scenes that keep a channel's
consistent character(s) and art style and follow the story. $0 / CPU-only — Gemini free-tier image
model draws keyframes, the existing Ken-Burns motion + crossfade stage animates them (no local
diffusion, no paid API). See ADR-052.
- **U1** `DONE`: data model + config + operator UI. `Channel.characters_json` cast (≤12 consistent
  characters) with an add/remove "🎭 Studio cast" manager on Channels; campaign `visual_source`
  (stock|studio) toggle + optional `visual_style` art-style override on the New Campaign form;
  `GEMINI_IMAGE_MODEL` server default + per-user `gemini_image_model`, managed **separately** from the
  text model via a new Credentials "Image model" card; additive migrations for all three columns.
- **U2** `DONE`: `ai_engine.generate_image` (Gemini image model, reference-image conditioning, budget
  metering, image-model fallback chain, quota/404 fail-fast, block not retried); new `core/studio.py`
  — character selection (seed-deterministic per episode), art-style resolution, sheet + scene prompt
  builders (dynamic pose / action lines / motion blur / no in-image text), `character_sheet` (draw
  once + cache) and `scene_visual` (sheet + previous frame as references).
- **U3** `DONE`: Studio render path in `core/video_factory.produce` — one drawn keyframe per scene,
  looped into a clip by `still_to_clip`, fed through the SAME motion/caption/stitch stages; character
  sheet cached in a stable per-channel dir for cross-episode consistency; fails clearly (no cast / no
  image key) rather than silently rendering stock. Worker wiring passes cast + image model + visual
  source; `_resolve_keys` needs no Pexels key in Studio mode.
- **U4** `DONE`: 12 new unit/worker tests (character pick, style/prompt building, `generate_image`
  fallback + block, `still_to_clip` args, produce() reference-chaining + no-cast guard, worker param
  pass-through) + an ffmpeg-gated integration test proving the real still→clip→scene path.
- Verified: 245 tests passing (12 new), 11 skipped (1 new ffmpeg-gated), ruff clean, docs guard green;
  the U1 operator UI checked in a real browser.

## Studio Mode — Pollinations FLUX as a $0 image provider `DONE`
A second image provider behind (or ahead of) Gemini, so Studio Mode keeps drawing for free when
Google's image model is down/blocked/quota-spent — or as the default. See ADR-053.
- The image field is now a **provider chain**: `pollinations:flux` draws with Pollinations (free,
  keyless, text-to-image); any entry may lead; any failure falls to the next except a content block.
  `ai_engine.generate_image` dispatches per entry (`_call_pollinations` + a deterministic per-prompt
  seed for reproducible, scene-varying frames); Pollinations isn't metered against the Gemini budget.
- Credentials: an optional **Pollinations token** (encrypted at rest, Test button) + **Image model**
  preset buttons (Gemini-first / Pollinations-first / Pollinations-only) to set the default in one click.
- Worker binds the token + output geometry into the image generator; `POLLINATIONS_TOKEN` optional
  server default; `.env.example` updated.
- Consistency note: Gemini stays recommended primary (reference-sheet conditioning = tightest
  character match); Pollinations holds the character by description + seed — looser, but a graceful
  degrade beats a failed render.
- Verified: 254 tests passing (9 new — provider dispatch, fallback, primary, block-not-rerouted,
  request builder, seed, encrypted token save, Test endpoint), 11 skipped, ruff clean, docs guard green.

## Studio Mode — billboard title + uploaded character reference (the "explainer" look) `DONE`
Match a reference channel's look: a consistent character (any style) + a big two-tone title on the
thumbnail AND burned into the video. See ADR-054.
- **W1** `DONE`: the cast manager is style-agnostic with diverse example presets (stickman / anime /
  3D mascot) — one click fills the form, nothing is locked to stickman.
- **W2** `DONE`: per-campaign **Billboard title** toggle — burns the hook title (top, UPPERCASE,
  white + brand accent, `split_two_tone`) into every scene as one ASS event (no extra encode, no AI
  call; libass renders Vietnamese diacritics). Works for stock and Studio.
- **W3** `DONE`: matching **poster thumbnail** — same two-tone title at the top (auto-fit font), so
  thumbnail and video match. One toggle drives both.
- **W4** `DONE`: upload your own **character reference image** (PNG/JPG/WebP ≤5 MB, re-encoded via
  PIL) — used directly as the identity anchor across every video (skips AI sheet generation), the
  tightest consistency; per-character add/replace/serve + cast-list preview. Works for any style
  (anime, mascot, a photo of yourself). Gemini leg only; the Pollinations fallback stays
  description-driven.
- Verified: 261 tests passing (7 new — two-tone split, headline ASS, thumbnail accent/fit, poster
  render, title_overlay flow, uploaded-ref render, character image CRUD) + 1 ffmpeg-gated headline
  burn test, 12 skipped, ruff clean, docs guard green; posters + both form UIs checked in a browser.

## Studio Mode — Pollinations honours the uploaded reference (kontext) `DONE`
Fix: base Pollinations `flux` is text-only and silently ignored an uploaded character image (so a
flux draw didn't match). Added the reference-capable models. See ADR-055.
- `generate_image` gains a `reference_url`; `pollinations:kontext` (FLUX.1 Kontext) + the other
  image-editing models (nanobanana/gptimage/seedream) pass it as `image=` so the free provider keeps
  the uploaded character; `flux` and Gemini ignore it (Gemini uses the local file).
- New PUBLIC route `GET /studio/ref/{token}` (no auth, 32-hex random-token filename, traversal-barred)
  so Pollinations can fetch the reference over the internet; the worker builds each character's public
  `ref_url` from `PUBLIC_BASE_URL` (falls back to the tunnel base; skipped on dev localhost).
- Credentials: a "Kontext → Gemini (uses your image)" preset + a clear flux-vs-kontext explanation;
  cast manager notes which image models honour an uploaded image. `PUBLIC_BASE_URL` added to config.
- Trade-off (opt-in, surfaced in UI): the reference image is briefly public at an unguessable URL —
  fine for a mascot, discouraged for a personal photo.
- Robustness fix: an image-editing model (kontext) invoked with NO reference URL (no upload, or
  PUBLIC_BASE_URL unset) used to 500 the whole render; it now degrades to text-only `flux` and logs
  why, so the episode still renders. (Prod traceback: kontext called without `image=`.)
- Verified: 267 tests passing (6 new — kontext `image=` gating, reference_url forwarding,
  `_cast_with_ref_urls`, public route, upload sets public token, kontext→flux degrade), 12 skipped,
  ruff clean, docs green.
- Upload UX fix: reference-image uploads used to fail SILENTLY (>5 MB phone photos rejected with no
  feedback → "no file chosen" + no thumbnail). Now the cap is 15 MB (we downscale to 1024px anyway),
  HEIC is decoded opportunistically if pillow-heif is present, the server logs the exact reason, and
  the UI shows an explicit "✓ image saved" / "⚠ couldn't use that image (PNG/JPG/WebP ≤15 MB)" banner
  plus an instant client-side size check. 268 tests (1 new), ruff clean, docs green.
- kontext-500 hardening: a kontext draw could still 500 when the reference URL isn't publicly
  reachable (Cloudflare Bot Fight Mode blocks the server-side fetch even though a browser loads it) or
  the prompt is over-long/garbled. Trimmed the Pollinations prompt to 700 chars, added an actionable
  error hint (verify the public URL + prefer Gemini-first), and reworked the Credentials presets to
  steer uploaded-reference users to Gemini-first (`gemini-2.5-flash-image,pollinations:kontext`) —
  Gemini reads the uploaded file locally, the reliable $0 path with no public URL and no kontext
  flakiness.
- SECURITY: the Pollinations token leaked into error messages/logs (the failing request URL carried
  `?token=…`). `_call_pollinations` now scrubs the token from any raised error. Rotate any token that
  appeared in logs.
- Safety net: when every image provider in the chain fails AND Pollinations was in the chain, a
  last-resort text-only `flux` draw keeps Studio rendering instead of hard-failing the episode (the
  uploaded reference is NOT applied — character is description-only; logged loudly). A Gemini-only
  chain is respected (no reroute). 271 tests (3 new), ruff clean, docs green.
- ROOT CAUSE of the kontext 500s (operator read the new API docs): backend auth is the
  `Authorization: Bearer` HEADER — a `?token=` query param is not documented and was ignored, so our
  authenticated requests actually hit the anonymous tier, which gated models (kontext) reject. Fixed
  `_call_pollinations` + `verify_pollinations` to send the Bearer header (secret never rides in a URL
  now); the new API's deterministic gates (401 auth / 402 pollen balance / 403 model access) raise
  actionable messages instead of a bare failure. 272 tests (1 new), ruff clean, docs green.
- Endpoint: switched to `gen.pollinations.ai/image/{prompt}` — the current documented API. The legacy
  `image.pollinations.ai/prompt` endpoint 500s for kontext even with valid Bearer auth; the operator
  PROVED the same token + reference URL succeed on gen.pollinations.ai (200, their character redrawn —
  which also disproved the Cloudflare-blocking theory). `nologo` dropped (legacy-only; watermarks are
  tier-based now). Verify Test uses the same endpoint. 272 tests, ruff clean, docs green.

## "Quote" content style + Vibe Engine (Batch Q) `DONE`
A third campaign style alongside stock stories and Studio explainers: the aesthetic "whisper-quote"
video — one short poem per video, shown line-by-line over an AI-drawn mood illustration, with a
one-word scribble cover. See ADR-056.
- **Q1 Vibe Engine** `DONE`: `core/vibe.py` re-rolls each episode (mood / one-off-character-or-scenery
  / setting / music-mood / voice-pace), seeded per (campaign, episode) — every video unique, art style
  constant.
- **Q2 Quote script** `DONE`: `build_quote_prompt` — a 5-8 line poem, one line per scene, each with an
  illustration brief in the rolled mood + a `cover_word`.
- **Q3 Character-less Studio** `DONE`: quote renders via the Studio path with NO cast; `scene_visual`
  accepts `character=None`; consistency = the fixed art style + previous-frame chaining.
- **Q4 Centered captions + signature** `DONE`: `style="quote"` (whole line centered, faded italic, not
  karaoke) + an optional custom `signature` mark lower-centre on every frame + thumbnail.
- **Q5 The look** `DONE`: `vintage` colour grade (muted + vignette + grain) + art-style presets on the
  form (incl. a lofi-retro-anime look).
- **Q6 Scribble cover** `DONE`: thumbnail = the cover word centered over the illustration.
- **Q7 Form** `DONE`: Content-style selector; picking Quote auto-tunes (Studio visuals + starter art
  style + reveals the signature field). AI Propose intentionally unchanged (the Vibe Engine is the
  per-episode designer).
- Verified: 280 tests passing (8 new — vibe determinism/ratio, quote prompt+routing, quote captions +
  signature, scribble cover, vintage grade, castless produce orchestration) + 1 ffmpeg-gated
  caption-burn test, 13 skipped, ruff clean, docs guard green; scribble cover + quote form checked in
  a browser.

## Per-campaign catchphrase on/off `DONE`
The signature opening + sign-off (persona catchphrases) each got a per-campaign on/off checkbox: the
TEXT stays saved, a `catchphrase_open_on` / `catchphrase_close_on` flag decides whether it's applied,
so an operator can pause a catchphrase without losing it (default on = unchanged behaviour). Wired
through `_build_campaign_config`, `_campaign_form`, the script preview, and the worker (only an
enabled catchphrase reaches `generate_script`). 281 tests (1 new), ruff clean, docs green.

## Worker self-recovery — wedged-render watchdog `DONE`
An episode stuck at "Rendering 10%" for two hours exposed that every safety net (RQ's 45-min job
timeout, the 90-min stuck-task reaper, the orphan-lock sweep) lives *inside* the worker process that
died — and that the container healthcheck only asked whether a worker was *registered*, which a
hung worker still is. Fixed at the mechanism level (ADR-057):
- `set_progress` change-stamps `task:progress-ts` (only a moved value refreshes it), so progress
  staleness is measurable: `stalled_render()` / `stall_limit_seconds()` / `worker_healthy()`.
- New `workers/watchdog.py` daemon thread (60s): a render idle past `JOB_TIMEOUT + 10 min` is failed
  with an actionable message, its progress + render lock released, the operator alerted, then the
  process exits so compose recreates the container. A thread can do this because a blocked render
  holds the main thread with the GIL released.
- The stall limit sits deliberately behind RQ's own timeout, so a slow-but-alive render is still
  failed cleanly by RQ rather than blunt-restarted (silent stages — image gen, concat, Auto-QC —
  legitimately report no progress for minutes).
- Operator restart flag (`worker:restart-requested`, TTL 5 min) honoured by the same thread — the
  groundwork for the Operations page's "Restart worker" button with **no Docker socket** (the
  internet-facing web container must never reach the Docker daemon).
- `run_worker.py` clears the lock, all progress entries and any stale restart flag at boot.
- Healthcheck: `worker_alive()` → `worker_healthy()` so a wedged worker shows as `(unhealthy)`.
- Verified: 293 tests (12 new), ruff clean, docs guard green.

## Operations page — the factory floor (queue + worker) `DONE`
"Can I restart the worker from the website instead of SSH?" — yes, and without ever giving the
internet-facing web container the Docker socket (host-root). New `/operations` page (ADR-058), System
group in the rail:
- **⏳ Render queue** — queued jobs in true RQ order joined to their episode: `🔼 Next` (RQ
  `at_front`, same Job so `rq_job_id` stays valid) and `✕ Cancel` (drops the job, Task→FAILED so the
  normal Retry puts it back). Uploads share the one queue, so `#` is the real queue position and
  queued uploads are counted, not hidden.
- **⚙ Worker** — the single worker's verdict (down/stalled/busy/idle, liveness from the same
  `worker_alive()` the health strip uses), the live render's progress **and how long since it last
  moved** (the number that was missing during the 2-hour incident), render-lock state, plus
  `🩹 Recover stuck renders` (the hourly sweep on demand — `scheduler.recover_now`, keeping the
  render-concurrency-1 guard, dropping only the two-tick wait) and `🔄 Restart worker` (Redis flag
  honoured by the watchdog).
- Tenancy runs through the Task row: another operator's job id is neither listed nor controllable.
- `_system_health` gained `worker_stalled`; the rail badges Operations when the worker is down OR
  wedged, and the degraded note is worded for a registered-but-wedged worker.
- A plain-language explainer of the three recovery layers (RQ timeout → watchdog → housekeeping), so
  the operator knows whether to wait or intervene.
- Verified: 312 tests (19 new), ruff clean, docs guard green; queue + worker tabs checked in a real
  browser at 1280px and 375px against a seeded wedged render.

## Publish queue + per-episode reschedule `DONE`
The Operations page's third tab, and the "shift one video without moving the whole campaign" control
the operator asked for (ADR-059):
- 🚀 **Publish queue** — every rendered episode with WHEN it goes out and why: an operator override,
  a projected slot (ready episodes assigned to the campaign's next free slots lowest-first — the
  scheduler's own rule, via a shared `_upcoming_slots` that `_next_slot` now reads too, so the
  dashboard chip and this projection can never disagree), or "after you approve it". Rows whose video
  file left the disk are flagged, because Publish now would fail on them.
- **⚡ Now** reuses the existing publish-now route (the shared action now bounces back to Operations
  via an allow-listed `return_to`, never an open redirect) and **✏ Reschedule** writes a new nullable
  `BufferPoolItem.publish_at`.
- The scheduler checks the override FIRST and lets it outrank the posting-day / slot-window /
  one-per-slot gates — an operator who names a time means it — while a FUTURE override is excluded
  from the slot pick, from missed-slot catch-up, and from the calendar projection, so the logic that
  was overridden can never race ahead and undo the reschedule. `auto_publish` still wins: a
  review-first campaign publishes on approval only.
- Times are read and shown in the CAMPAIGN's timezone (the clock its slots already use) and stored
  as naive UTC — interpreting a `datetime-local` value as UTC would have shifted every reschedule by
  the operator's offset.
- Deferred → DONE in R4 (ADR-067): overridden episodes are drawn on the week grid as ✏ chips.
- Verified: 325 tests (13 new), ruff clean, docs guard green; all four row states (slot / your time /
  needs review / file missing) plus the open reschedule panel checked in a real browser at 1280px
  and 375px.

## Alert bell — one cross-channel inbox `DONE`
The operator's `[Channel] ➔ [Campaign] ➔ problem ➔ [action]` bell, colour-coded red/amber/green
(ADR-060):
- `GET /api/alerts` derives the whole feed from live state — no `Notification` table, no read/unread.
  The badge is the count of red+amber rows present right now, so it clears itself when the problems
  are fixed and can never disagree with the list it opens.
- Four fail-soft sources: infrastructure (Redis/worker down or wedged, disk pressure, AI quota ≥80%),
  work needing a human (per-episode failures showing the LAST stack-trace line — the actual error —
  plus breaker-paused campaigns and counted review/autopilot backlogs), an imminent missed slot (a
  slot within 6h with an empty buffer: the only predictive row, because a passed slot cannot be
  recovered), and recent publishes as green evidence the factory works.
- One broken source can never empty the bell — an empty bell reads as "everything is fine".
- The app bar now exists at EVERY width (it was phone-only), hosting brand + bell + theme + the
  signed-in address; the sidebar sticks below it and drops its duplicate brand/email outside the
  phone drawer. The panel is viewport-anchored on the phone, where a bell-anchored 420px dropdown
  hung off the left edge.
- Rows are built with createElement/textContent — channel, campaign and error text are user/AI data.
- Deviation from the approved plan: localStorage read-watermarking was dropped. With a live count
  there is nothing to mark as read, and an ops panel should keep showing what is still broken.
- Verified: 339 tests (14 new), ruff clean, docs guard green; 12 pages checked in a real browser at
  1280px and 375px with zero page errors and no horizontal overflow.

## Header: breadcrumb in the app bar `DONE`
Completes the operator's header layout — breadcrumb left, bell/theme/profile right (ADR-061):
- Every breadcrumb moved from the content flow into a `{% block crumbs %}` rendered by the app bar,
  so "where am I / go back one level" is in one fixed place on every page instead of being a per-page
  accident tucked under the `<h1>` with a negative margin.
- `_campaign_crumbs.html` holds the campaign-hub trail, included by the three hub pages: a template
  block cannot be filled from inside an include, so the shared hub partial could no longer own it.
- On the phone the brand yields to the trail (via `:has()`, so a crumb-less page like the Dashboard
  still shows a title), and the trail scrolls sideways instead of truncating each crumb to a letter —
  every parent stays readable and tappable.
- `.crumbs` lost its content-flow margins; `.topbar-crumbs` truncates on desktop, scrolls on phone.
- Verified: 339 tests, ruff clean, docs guard green; 15 pages × 2 breakpoints checked in a real
  browser — the trail is in the app bar exactly where expected, never left behind in the content, and
  a long Vietnamese campaign name never pushes the bell off screen.

## Macro analytics — factory vitals `DONE`
The top-down view the per-campaign pages cannot give (ADR-062), as one dashboard card:
- **Total views** across every measured episode, reported WITH how many episodes are measured out of
  how many are published — YouTube Analytics lags ~2 days, so a bare total would read as the whole
  catalogue when it may cover a fraction of it.
- **Renders today**: failure rate over renders that FINISHED today (in-flight work has no outcome
  yet), plus the machine minutes they consumed from `render_json.render_seconds`.
- **CPU load + memory** from a new stdlib-only `core/host.py` — no psutil dependency for two numbers
  the kernel publishes. CPU is load-average-per-core (the right question on a box running one
  `nice -19` render: is work queueing?), memory is Total−Available so reclaimable page cache does not
  show a healthy 24 GB box at 95%. Both fail soft to "—" off Linux.
- Every cell has an explaining empty state; the card reuses the existing scorecard cells and the
  progress-bar macro rather than inventing a widget.
- GPU is deliberately absent — this deployment is CPU-only by hard constraint.
- Verified: 350 tests (11 new), ruff clean, docs guard green; checked in a real browser at 1280px and
  375px in both the empty and populated states.

## Micro analytics — channel growth vs publishing `DONE`
"Does publishing this much actually grow the channel?" — answered per channel (ADR-063):
- New `ChannelSnapshot` table: one row per channel per LOCAL day (subscribers / views / videos) from
  the platform's own totals. Per-episode stats cannot answer this — a channel's totals are not the sum
  of the episodes we made — and the APIs expose only the CURRENT total, so if we don't sample daily the
  past is permanently unavailable.
- `collect_channel_snapshots` rides the hourly stats pass and self-throttles: the day row is checked
  before fetching, so extra ticks spend no API quota; one revoked token never blocks other channels.
- `channel_growth` serves the correlation view — per-day sub/view deltas beside episodes published
  that day — drawn on each Channels card as CSS bars (episodes) under an inline-SVG polyline (subs
  gained). Hand-rolled from numeric server values: no chart library, XSS-safe by construction.
- Two "unknown vs zero" distinctions kept deliberately: a hidden subscriber count is None, never 0
  (otherwise a hidden channel reads as "0 subs, no growth" forever), and the first sample yields None
  deltas with a "the curve appears tomorrow" note rather than a flat line at zero implying publishing
  did nothing.
- Facebook contributes followers only; it has no lifetime page-view total comparable to YouTube's, so
  views stays None instead of substituting a similar-sounding metric.
- Verified: 362 tests (12 new), ruff clean, docs guard green; the chart checked in a real browser at
  1280px and 375px against 14 seeded days of uneven publishing.

## UX consolidation R1 — one truth for counts, names and stages `DONE`
A five-persona UX audit (first-time owner, phone operator, power operator, strategist, plus a
structural code audit) found one root problem behind most complaints: **the same fact is stated in
many places with different numbers and different words.** R1 fixes the foundation (ADR-064):
- **One attention count**: `_attention_count` (failed + awaiting review + open proposals) computed
  once server-side, served on `/api/summary` and `/api/alerts`, rendered by the hamburger, rail badge,
  bell and triage pill. Previously four badges showed 4 / 5 / 3 / 2 for one situation.
- **One stage vocabulary** — Queued · Writing · Rendering · Review · Scheduled · Published · Failed ·
  Cancelled — applied to `app.js`'s labels too, retiring the synonyms ("Pending Queue", "Completed",
  "Audio Synced") that made one episode read differently per page.
- **`TaskStatus.CANCELLED`**: an operator's cancel is no longer a FAILED. It is neutral-grey, out of
  the failure KPI and the alert feed, skipped by autopilot's auto-retry (which previously queued the
  cancelled episode straight back), a finished outcome for hydration, and still retryable.
- **Approve releases the episode at once**: `apply_approve` sets the buffer row `ready` and the Task
  `SCHEDULED`, so it leaves the review queue immediately (it used to read as approved AND awaiting
  review simultaneously, invite a double submit, and count as a queued *render*).
- **Honest counters**: "Episode 0 / 30" → "0 of 30 published".
- **Dead weight removed**: `ui.js` writes to `#hv-buffer` / `#banner-failed` / `#banner-review` (no
  such elements), tasks.html's second content-flow breadcrumb (ADR-061 says once), and the "re-render
  from Task Logs → Retry" advice that pointed at a button that is never offered for a rejected item.
- Verified: 376 tests (13 new), ruff clean, docs guard green.

## UX consolidation R2 — one episode list `DONE`
Three of four simulated operators lost their place the same way: the stage-chip row looked like one
control, but two chips silently changed page, layout and vocabulary. Two tables over one object was
the duplication; the chips were how you noticed (ADR-065):
- The `/tasks` **page** is retired → 301 into `/episodes?status=rendering`. `/api/tasks` stays (every
  pagination/search/scope test still applies) and gains `live=1` for working stages only.
- Every stage chip filters `/episodes` in place. Rows in a working stage carry `data-live-task`, and a
  much smaller `app.js` moves their pill + progress in place — the live render log is a filter now.
  Gone with the page: a second search grammar, a second pager, dead Time/Result columns, and progress
  bars showing 0% on published episodes.
- **The Review chip was wrong, not just duplicated**: it read "Review (0)" while two videos waited,
  because it counted `Task.status` while the review queue IS the buffer and a Retry had moved one task
  on. The stage is now buffer-derived (`_review_episode_keys`), so it equals the attention badge by
  construction, and review membership overrides the task status — an episode can no longer be both
  "Queued" and "Review" (testers hit that on three surfaces).
- The `/assets` review workbench stays (watching video is a different job) but is now *offered* by a
  link on the Review filter instead of being where a chip dumps you.
- One ordering (`updated_at` desc) for both episode surfaces, so actionable work is never buried under
  hundreds of published rows — the campaign hub tab did exactly that.
- Dense 2-line mobile rows: 38 episodes went from ~8.9 phone screens to ~3.5.
- Fixed a latent CSS bug found while verifying: `.banner` was `display:flex`, so every inline child of
  a sentence became its own narrow column — unreadable at 375px, on every banner in the app.
- Verified: 382 tests (5 new), ruff clean, docs guard green; chips, live row movement, the redirect and
  both breakpoints checked in a real browser.

## UX consolidation R3 — an honest, short dashboard `DONE`
Eight widgets competed to answer "is anything broken?", so the answer took four phone screens and came
back as four disagreeing numbers. The dashboard is now four blocks (ADR-066): health strip → triage →
running now → one Factory card → activity. **5.0 phone screens → 2.9.**
- **Deleted the six stat tiles** — every number was already in the triage card, the health strip or the
  card below.
- **Merged "Factory scorecard" + "Factory vitals" into one Factory card.** They shared a layout and
  split one question down the wrong seam (total views sat in the machine-health card; the failure rate
  sat away from the failure count). Windows are now named: "Retention · last 7 days", "across N
  measured episodes — analytics lag ~2 days".
- **CPU + RAM moved into the health strip**, beside Disk and Queue — they answer the same question.
- **Runway leads with the worst case**: "2 at zero — those slots will be missed" instead of "≈1.0 day",
  which averaged away the very emergency shown in the panel directly below it.
- **All-clear can no longer sit beside a red banner** (a green "everything is fine" under "factory is
  degraded" teaches the operator to distrust every signal).
- **Activity collapses runs**: "6 episodes published (Ep 519–524)" instead of six identical lines.
- **The scope switcher is visibly disabled here.** Selecting a channel changed the URL and nothing
  else while every number still showed the whole factory. Per-channel lives on Channels and on scoped
  Campaigns/Episodes; half-scoping this page would recreate the inconsistency being removed.
- Verified: 387 tests (4 new), ruff clean, docs guard green; measured in a real browser at 375px/1280px.

## UX consolidation R4 — one scheduling surface `DONE`
Two pages answered "what publishes when" and gave different answers (ADR-067):
- `/calendar` is now **Publishing**, with a `Week grid | List & actions` toggle. The list view IS the
  former Operations publish tab (`?tab=publish` 301s there), so Operations is purely the machine.
- **Deleted the codebase's only duplicated business rule.** "Ready episodes fill upcoming slots,
  lowest number first" existed twice — in `_upcoming_slots` (dashboard chip, publish list) and again as
  a private day-walk with `pool.pop(0)` inside the calendar. It had already drifted: after a reschedule
  the calendar said "2 ready" while the hub said 4.
- **Rescheduled episodes appear again.** They used to vanish from the one page whose job is showing
  when things publish, and the grid drew "will be missed" on days that actually had a publish. They now
  render as ✏ chips at their own time and count toward Ready ("incl. 2 at your own time").
- **⚡ Now asks first.** Publishing publicly and immediately — the most irreversible action in the
  product — was a bare POST beside Reschedule, while the same action on /assets always confirmed.
- Verified: 391 tests (4 new), ruff clean, docs guard green; grid ✏ chips, the toggle, the 301 and the
  agreeing ready counts all checked in a real browser.

## UX consolidation R5 — surviving the first hour `DONE`
The product was navigable only by someone who already knew it worked (ADR-068):
- **The dashboard no longer greets a fresh install with "All clear — nothing needs you right now."**
  The three setup steps lead the page until a channel, keys and a campaign exist — and they now live in
  exactly one place (the activity card's duplicate copy is gone). Found and fixed a real bug doing it:
  `setup.keys` in Jinja resolves to the dict's own `.keys` method, so step 2 rendered as ✓ done on an
  account with no keys at all.
- **No button leads somewhere impossible.** "+ YouTube (OAuth)" with no configured Google client used
  to hand the operator Google's "Error 400: invalid_request"; it now explains what the server is
  missing. A Facebook Page is verified against the Graph API *before* it is stored — a made-up token
  used to save as "● Active" and reveal the lie weeks later, when a publish failed. Only a definite
  rejection blocks the save, so a network hiccup never locks an operator out of their own Page.
- **Missing keys are named before they cost a render.** A campaign could be started with no keys at
  all: three episodes queued, every one doomed, dashboard green. Now a red alert, with Pexels demanded
  only from campaigns that actually use stock footage. Every Credentials row links to the page that
  issues that free key and says what breaks without it; the two model-chain cards fold away (and the
  Test button no longer overwrites the explanation next to it).
- **A failure says what to do about it.** A stack trace with one Retry button is the wrong advice for a
  spent quota, a rejected key or a full disk. Seven recognised causes now carry a fix and a link, worded
  identically on the episode page, in triage and in the bell — with the raw text folded underneath, and
  no guess when nothing matches.
- **The first campaign starts in Review mode** (an explicit Settings choice still wins), and every
  irreversible action confirms with a verb — "Delete campaign", "Publish now" — naming the campaign or
  episode. One generic "Confirm" for both delete-a-campaign and publish-now trains the reflex to click.
- Flashes are one-shot (a reload used to re-show a success for an action nobody took), a browser 404 is
  a styled page that keeps the navigation instead of `{"detail":"Not found"}`, the AI buttons report
  errors inline with a link to Credentials instead of in a lost `alert()`, ⌘K jumps to pages as well as
  content and folds Vietnamese diacritics both ways ("lich dang" → Publishing, "ep 3" → Ep 3), and
  every phone control is ≥44px.
- Verified: 422 tests (31 new), ruff clean, docs guard green; checked in a real browser at 375px and
  1280px — no page scrolls sideways, the setup card leads, the confirm modal shows its verb and stays
  on screen, the palette lists 11 destinations, and `?flash=` disappears without a reload.

## R7 — Slow-vendor healing: resume, don't restart `DONE`
The reported failure: 8 scene images at 120s each; the vendor answered 5, slowed past 120s on the
6th, and the whole render failed — losing the 5 images, the TTS and the script (ADR-069):
- **The retry waits longer, not the same 120s again.** Image fetches ladder their timeout (base ×2
  per attempt, per-user knob on Settings, throttle-friendly pauses) inside a per-episode budget that
  keeps the render safely under its own 45-minute job cap — a naive timeout raise would have traded
  a clean failure for a SIGKILL mid-encode.
- **A failed render is a checkpoint now.** The workspace survives failure, stills are named by their
  prompt hash (a new script can never reuse a stale drawing), and the script itself is persisted on
  the task — so Retry redraws only the missing scenes and never pays for a second script. Reject and
  Discard & re-render still reroll for real: they drop the checkpoint on purpose.
- **The autopilot continues interrupted renders.** Its retry now shares the failure classification
  with the bell and the episode page (`core/failure.py`) — it resumes vendor timeouts and worker
  kills, and never burns its cap on a missing key, a spent quota or a safety block. The orphan
  sweeper leaves recent checkpoints alone (24h) so an hours-later autopilot pass still finds them.
- Verified: 439 tests (17 new), ruff clean, docs guard green.

## R8 — Post-R7 audit: five almost-right mechanisms `DONE`
Audited the R7 result against what the UI actually promises; every finding passed the suite already
(gaps in coverage, not regressions) — ADR-070:
- **"Restart worker" no longer leaves a phantom render.** The watchdog did full bookkeeping on the
  stall path but the operator-restart path just exited, so the episode read "Rendering 47%" — with
  nothing working on it — until the reaper noticed up to 2 hours later. A redeploy whose 300s grace
  expired mid-encode did the same. Boot recovery now fails abandoned renders the moment the worker
  comes back, which is also what makes the button's confirm text true.
- **The autopilot stopped fighting the breaker and the lifecycle.** Its retry was joined on the
  channel, not the campaign: it re-queued the very episodes the consecutive-failure breaker had
  stopped a campaign for, and could re-render — and on auto-publish actually upload — leftover
  failures of a campaign the operator had already completed.
- **A wedged worker is no longer reported as an unreachable provider.** The watchdog's own message
  contains "timeout" as well as "stalled", and the network class matched first: same retry verdict,
  wrong explanation, and the explanation is what the operator acts on.
- **Auto-QC's re-render actually re-draws.** Pollinations seeds come from the prompt, so the one
  re-render rebuilt the rejected video pixel-for-pixel and re-judged it — a whole episode of image
  calls plus a vision call for the identical verdict. Attempt 2 now salts the seed; attempt 1 stays
  deterministic so a resume can still reuse its checkpointed stills.
- Uploads get an hour instead of 30 minutes (a long-form master on a slow uplink can exceed it, and
  being killed mid-upload is the failure this box handles worst), and ⌘K preselects its first row on
  open — ⌘K then Enter did nothing while ⌘K, one letter, Enter worked.
- Verified: 457 tests (18 new), ruff clean, docs guard green; ⌘K-then-Enter and the restart confirm
  checked in a real browser at 375px and 1280px.

## R9 — The quote aesthetic, and a voice that whispers as far as free TTS can `DONE`
Audited Quote mode against the reference style (retro 80s/90s anime, sepia + film grain, lofi piano,
a confiding read). The skeleton was there; three of the six settings that make it recognisable were
unreachable, off by default, or unbuilt — ADR-071:
- **The `vintage` grade existed, rendered correctly, and no campaign could select it.** Film grain,
  sepia warmth and the vignette were written back in ADR-056, but the create-time whitelist and the
  form's dropdown were hand-copied lists that never learned about it. Both now read one catalog with
  an assert, so a grade can no longer exist in the filter table yet be invisible in the UI.
- **The quote art preset is now explicitly 1980s–90s anime** — cel shading, muted sepia and
  burnt-orange, dusk light, city-pop mood, analog grain.
- **A soft, confiding read** (`voice_delivery`): rate −12%, a per-voice pitch drop, and one
  duration-preserving filter pass (highpass → lowpass → compressor → tiny room) applied once to the
  assembled narration. Named honestly in the UI: there is no free whisper, and the paid Azure
  "whispering" style has no Vietnamese or Spanish voice at all.
- **Soft voices curated per language** — 🌙 in the picker, auto-picked by Quote, and the only pool the
  AI designer may propose from for a quote campaign, so foreign-language quote channels sound right too.
- **Picking Quote tunes all six settings and says so.** Every branch respects a choice the operator
  already made, and a toast lists what changed — most of those fields sit on another tab inside a
  collapsed section.
- **The save bar stacks on the phone instead of squeezing.** Spotted while verifying the above at
  375px: one non-wrapping row could not fit two submit buttons, Cancel and the hint, so flex shrank
  them all and the page's most important control read "Create &". The primary action now gets its own
  full-width row, the hint moves below, and desktop keeps its single row.
- Verified: 483 tests (26 new), ruff clean, docs guard green; the auto-tune, the 🌙 marks, the notice
  and the save bar checked in a real browser at 375px and 1280px — including that a deliberate grade
  survives the auto-tune and that no button label clips.

## R10 — Facebook: verify the right thing, and stay fixable `DONE`
Reproduced in a browser first: a Page ID and token that were pure invention saved as "● Active" under
a green "✓ Page connected and verified" banner. Seven fixes (ADR-072):
- **The check asked the one question that cannot fail.** Reading a Page's public name proves nothing —
  the short-lived USER token the Graph Explorer offers by default reads it perfectly, which is the
  commonest mistake in this integration. It now asks `/me` (with a Page token, that IS the Page; only
  a Page carries `category`), and refuses a token belonging to a different Page.
- **The banner only says "verified" when it was.** "Could not tell" now says so.
- **Paste anything**: a Page URL, `@handle`, username or raw id — and a verified save stores the
  canonical NUMERIC id, which does not break the day the Page is renamed.
- **Facebook's own words reach the operator**, token-scrubbed, instead of "400 Client Error: Bad
  Request for url: …" — and an OAuth failure is classified as a credential fault pointing at
  /channels, so the autopilot stops spending its retry cap re-uploading with a dead token.
- **`expired` is finally written** — by the publish path and the analytics pass — so the pill and the
  filter chip stop being decoration. Paired with a token-replacement panel on the channel, because
  the only previous way back was Remove + re-add, which deletes the channel's campaigns and rendered
  videos with it. Only a verified token clears the flag.
- **One `GRAPH_VERSION`** (v20 → v23), and a test that fails if any file hardcodes a Graph URL again.
- **The form explains how to get a permanent Page token** — the step that actually defeats people —
  in one partial shared by Add-a-Page and Replace-token.
- Verified: 520 tests (37 new), ruff clean, docs guard green; the add flow, the honest banner, the
  token panel and the help checked in a real browser at 375px.

## R11 — Facebook publishing catches up with YouTube `DONE`
Diffed the two publish paths line by line. Facebook was implemented once, early, and every improvement
since had landed on the YouTube side only (ADR-073):
- **A vertical short is a Reel now.** Posting 9:16 to `/videos` made an ordinary Page video that never
  entered Reels distribution — the whole reason this product renders vertical. The three-phase Reels
  upload also makes the transfer **resumable**, which YouTube had been all along.
- **A private campaign no longer publishes publicly.** YouTube read the privacy setting; Facebook
  ignored it entirely. That is not a missing feature — it is the product doing the opposite of what
  the operator asked, on the one axis where it cannot be taken back.
- **The CTA is posted as a comment**, as YouTube always did. It carries the affiliate link, so
  monetization simply did not exist on Facebook channels.
- **"View ↗" leads somewhere.** `facebook.com/{video_id}` is not a video URL; it is `/reel/{id}` or
  `/watch/?v={id}` now. YouTube got the same fix — it was building `/shorts/{id}` for 15-minute videos.
- **No duplicate posts.** An upload that succeeds server-side but times out client-side looks exactly
  like a failure, and the retry posted a second copy. The Reels API hands us the video id before the
  bytes move, so it is persisted first and a retry asks whether that upload already landed.
- One batched insights call instead of fifty, https-only avatars, and an expired channel parks its
  rendered episode in the buffer rather than failing it and burning a retry.
- Verified: 540 tests (57 new across R10+R11), ruff clean, docs guard green.

## R12 — "The screen flashed and nothing happened" `DONE`
An operator reported that adding a Facebook Page did nothing. The code was right — it verified, the
token was refused, the save was blocked, a red banner said so. The experience was still nothing
(ADR-074). Their submission showed the cause: the Page ID had been pasted into BOTH boxes.
- **The mistake is named now**, locally, before any Graph call: "You pasted the Page ID into the token
  box." Facebook's own answer, "Cannot parse access token", is accurate and never says which box.
- **A refusal returns to the form**: the redirect anchors `#fb-form`, the disclosure re-opens, and the
  Page id / name / avatar come back so nothing is retyped. The token is never echoed into the URL.
- **The error is repeated at the form.** The Add-a-Page panel is the last element of a page several
  screens long; a banner at the top is invisible to whoever just submitted from the bottom.
- Verified: 546 tests (6 new), ruff clean, docs guard green; the operator's exact submission replayed
  in a desktop browser — the page lands on the form, open, prefilled, error above the fields.

## R13 — no password manager may touch a credential box `DONE`

The operator's follow-up: they had pasted the right token. So the substitution happened between
their keyboard and the POST, and `/channels` was built to invite exactly that.

- **The page looked like a login form.** A text input (`page_id`) directly above a `type="password"`
  input is the shape Chrome saves as username+password — and every connected Page added one more
  token box, none of them saying "do not manage this". A saved entry then refills a field nobody is
  looking at, with a value nobody chose.
- **One `ui.secret()` macro** now renders every secret box — the six suppression attributes it takes
  to actually stop Chrome, 1Password, LastPass, Bitwarden and Dashlane are written once. Six
  hand-written copies is five chances to forget one; that is the same defect that hid the `vintage`
  grade and hardcoded four Graph versions.
- **The token box checks itself at the field**, on input, before submit: a Page ID, an all-digit
  value or anything under 40 characters is named and the submit is blocked. A server refusal arrives
  a round-trip later on a reloaded page with the box blank again — which reads as "nothing happened".
- Verified: 551 tests (5 new), ruff clean, docs guard green; replayed in Chromium at 390px — the
  reported submission is refused at the form, a real token clears it.

## R14 — audit: the autopilot must not be able to spend the box, and measurement must not lose what it measured `DONE`

Every item below was reproduced before it was fixed, not inferred from reading.

**Autopilot**
- **The auto-reject loop had no cap.** Rejecting re-renders, and a re-render can score badly again —
  eight cycles produced eight re-renders in a simulation, campaign still `active`. Nothing bounded it:
  `apply_reject` never routes through `_fail_task`, so the consecutive-failure circuit breaker is never
  consulted. On one render slot, one bad episode could starve every other campaign. Capped, then
  escalated to the operator — after that many tries the machine has no better idea.
- **One failure silenced the whole channel for a day.** The cadence key was claimed before the work and
  a single `except` wrapped every step, so a Gemini 503 in the optional weekly strategist skipped
  review, retry, catch-up and auto-apply — and kept skipping them until the interval expired. Steps are
  isolated now, cheapest and most load-bearing first; a partial pass shortens the key to 10 minutes.
- **One counter, three budgets.** `retry_count` is what the operator sees, so every path bumps it —
  including the autopilot's own cap. Two hand-pressed Retries disabled the self-healing R7 added.
  Split into `auto_retry_count` / `auto_reject_count`.
- **A dead-token channel kept being fed.** Autopilot iterated every channel regardless of status:
  approve → publish → fail → roll back, while the buffer kept rendering and then expiring. The tick's
  eager hydrator did the same, so guarding only the autopilot would have fixed half of it — both stop
  now, and the tick counts the stalled channels rather than skipping them silently.
- `_record_heartbeat` re-reads the row before its read-modify-write of the operator's config JSON.

**Analytics**
- **`collect_stats` deleted what it could not re-fetch.** It rebuilt `stats_json` from scratch, so a
  rate-limited geography call — or simply an episode past the 50-video curve cap, no error at all —
  wiped the retention curve, the scene drop attribution and the top-viewer country. Those are the
  inputs to the playbook distiller and the audience verdict. Merged now.
- Curves are requested only for episodes that lack one (one sequential HTTP round trip each, in the
  scheduler thread), truncation is logged instead of silent, and the due list is ordered
  never-measured-then-oldest so a cap drains rather than starving a busy channel's tail.
- **A channel that may not read analytics now says so.** A pre-`yt-analytics.readonly` connection 403s
  hourly into a log file; the operator just saw retention that never arrived.
- Facebook reports no “% viewed”, so every autopilot verdict is unavailable there — named, and said in
  the UI, instead of grading every campaign “healthy” forever.
- The growth caption reports the calendar span AND the sample count; they part company after an outage.
- The stats pass is handed UTC explicitly — the tick's `now` is LOCAL time and was being compared
  against UTC timestamps (latent: production passes `None`).

**R13 trim.** `autocorrect`/`autocapitalize`/`spellcheck` removed from `ui.secret()` — every browser
already disables them on `type="password"`; they looked like diligence and did nothing. The token
field's "too short" branch went with them: `minlength` already says it, in the browser's own words.

- Verified: 564 tests (18 new), ruff clean, docs guard green; the reject loop, the cadence lockout and
  the stats data-loss each reproduced first, then pinned; new UI states checked in Chromium at 390px.

## R15 — the refusal must name the mistake, not our own request `DONE`

Reported: *"add facebook thấy chớp cái rồi không vô"* — banner
`⚠ Not connected. (#100) Tried accessing nonexisting field (category)`.

- **The verdict was right and the explanation was ours.** That was a User token — the mistake ADR-072
  exists to catch — but `check_facebook_page` asked `/me?fields=…,category,…` because only a Page has
  `category`. True of Graph's data model, false of its API: Graph refuses the WHOLE request for a node
  without that field, so the "that is a personal User token" branch was **unreachable** and the
  operator read a complaint about a field they never typed.
- **The test hid it.** It faked `200` with no `category` — my assumption rather than Graph's behaviour
  — and passed for months over dead code. Rewritten to the real response shape.
- **Identification now uses `metadata=1`**, Graph's own introspection, with only universally-valid
  fields — the request cannot be the thing that fails. A `#100` that still arrives is translated into
  what it means, reading the node type out of Graph's text (`on node type (User)`). A direct probe is
  the fallback, and an unidentifiable token is "saved without checking", never "verified".
- **`Why:` replaces `Facebook said:`** — the reason is sometimes Graph's words and sometimes ours, and
  attributing ours to Facebook is a small lie on the one surface that must not tell them.
- **A refusal opens the token guide it points at**, instead of telling the operator to follow steps
  that are still collapsed.
- Verified: 572 tests (8 new), ruff clean, docs guard green; the operator's exact submission replayed
  in Chromium at 390px against a Graph stub returning their verbatim error — the banner now names the
  mistake, leaks none of Graph's complaint about our request, keeps the Page ID, and opens the guide.

## R16 — billboard: a 3-second hook flash, not a half-screen tenant `DONE`

Reported with a frame: the billboard title covered ~half the screen, doubled into two offset
copies, for all 54 seconds.

- **Doubled**: the poster thumbnail extracts a frame from the finished video — every frame already
  carried the burned title — then drew the same title again in PIL; and the frame scorer prefers
  edge-rich frames, i.e. the ones fullest of outlined text. Frames are now sampled AFTER the flash
  window, so the double-draw is impossible by construction.
- **Half the screen, all episode**: every scene's ASS starts at its own t=0 and all of them got the
  headline; plus no row cap at 5.2% height per row against 13-word AI hooks. Now: scene 0 only,
  3s + fade-out, teaser-cut at a word boundary (~56 chars), fitted into ≤3 rows from 4% height.
- **`title_overlay` = `off` | `thumb` | `flash`** (legacy "on"/bool → flash, normalized in one
  place — "off" is a truthy string). `thumb` = poster thumbnail, clean video. `flash` (recommended)
  = thumbnail + the hook over the opening ~3s — the window Shorts/Reels ranking actually measures,
  readable even muted; the footage gets the frame back for the part retention is scored on.
- Verified: 578 tests (7 new), ruff clean, docs guard green; before/after frames rendered at scale
  with the operator's exact 13-word hook (41% of frame + doubled → 13% for 3s → clean), poster
  thumbnail produced by the real PIL path.

## R17 — the money machine: autopilot with a brain, anti-flop, anti-slop `DONE`

Approved in full ("Duyệt làm hết tất cả"): automate M1-M4 under the autopilot, add anti-flop and
anti-AI-slop layers, and give the autopilot a Gemini decision layer — data-driven, with a reason on
every decision — behind hard rails. Shipped as six commits, each test-green:

- **C1 — script quality gate** (ADR-079): deterministic, pre-render, 0 AI — self-repetition
  (3-gram), duplicate titles, cliché filler (operator-extendable in Settings), rambling hooks.
  Block → one regenerate with the issues as avoid-notes → honest non-transient failure.
- **B1+B2 — early-flop detection + autopsy** (ADR-079): `views_24h` stamped once at 24h, flop =
  <30% of the campaign's own median (≥5 measured, silence below), autopsy note self-feeds the next
  scripts; the retention curve later upgrades it when scene 1 is to blame.
- **A1 — measure the money** (ADR-080): watched minutes collected on both platforms, YPP windows on
  the daily snapshot, an honest per-channel scoreboard on Channels, milestones announced once per
  level (phone only at 100%).
- **D1-D3 — the strategy council** (ADR-081): code computes → Gemini interprets (1 call/channel/day,
  closed action menu, reasons in the channel's language) → rails validate (bounds, live campaign,
  and the anti-hallucination rule: numbers ≥10 must exist in the pack). Proposals ride the existing
  inbox/auto-apply. Review/retry/catch-up stay 100% deterministic.
- **A2 — best-of compilations** (ADR-082): masters retained at publish (capped library), stream-copy
  concat of top-retention episodes with chapters, sentinel numbering, review-always, kind-aware
  retries. The long-form format that actually pays, built from work already done.
- **A3 — golden-hour slot changes**: the council proposes from the measured hour table; applied
  reversibly (one slot swapped), one change per campaign per week, full-auto allowed within that.
- **B3 — the flop breaker**: 3 straight first-day flops → a wind-down proposal days before
  retention could say the same. Proposes; never auto-stops.
- **C2 — AI script judge**: same /10 scale and reject threshold as vision QC, shares the single
  regenerate budget with the gate, fail-open, skipped above the 80% AI-budget reserve.
- **D4 — manager report**: the council's verdict delivered as a daily manager's note (log always,
  phone only when something was filed) — no extra AI call.
- **A4 — series playlists**: every YouTube upload joins its campaign's playlist (created once,
  cached, fail-open) — session time + watch-hours toward the threshold.
- Self-audit hardening after the batch: a compilation is labeled “Best-of” everywhere (not
  “Ep 9001”), rejecting one never writes a script avoid-note (an editing complaint must not steer
  the scriptwriter), a thin-library compile failure is non-transient (it heals by publishing more,
  not by retrying the concat), and a slot-change TARGET must be measured — ±1h of an hour with real
  first-day numbers that beat the hour being abandoned, so “move to 03:00” can never pass on format
  alone.
- Verified: 621 tests (35 new across the batch), ruff clean, docs guard green; every loop capped
  (one regenerate, one slot change/week, one council run/day, milestone once per level); every
  autopilot behaviour simulated in tests including AI-garbage verdicts and judge outages.

## R18 — a healthy token must stay believed: no more false expiries `DONE`

Reported: a valid permanent Page token repeatedly marked expired; re-pasting the SAME token fixed
it every time — the signature of a false conviction (ADR-083).

- **Root cause 1:** any Graph error typed "OAuthException" classified as a dead token — and
  Facebook stamps that type on rate limits (4/17/32/613) and temporary errors (1/2/368) too. A
  small Page's insights quota trips code 32 under the hourly stats pass on a perfectly healthy
  token. Auth is now decided by `error.code` alone.
- **Root cause 2:** both retirement sites (publish, hourly-retrying snapshot) condemned on a single
  error with no second opinion. They now re-verify the token first and retire only on a DEFINITE
  rejection — "verified fine" and "could not tell" both leave the channel alone.
- The Check-token button says "could not verify right now — the token was NOT rejected" when
  Facebook is rate-limiting, instead of a rejection that sends the operator token-hunting.
- Verified: 625 tests (4 new incl. the operator's exact loop encoded as a test), ruff clean, docs
  guard green.

## Known deferrals (credential-gated — verified by the operator, see RUNBOOK)
- Live Gemini script/metadata generation
- Live Pexels footage download
- YouTube OAuth refresh + real upload
- Live channel-totals sampling (YouTube channels.list / Facebook Page followers → ChannelSnapshot)
- Facebook Page upload
- Telegram delivery
- Cloudflare Tunnel public exposure
- GitHub PAT backup push
