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

## Known deferrals (credential-gated — verified by the operator, see RUNBOOK)
- Live Gemini script/metadata generation
- Live Pexels footage download
- YouTube OAuth refresh + real upload
- Facebook Page upload
- Telegram delivery
- Cloudflare Tunnel public exposure
- GitHub PAT backup push
