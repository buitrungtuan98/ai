# Architecture

## Topology (one box, four containers)

```
Internet ──(outbound only: QUIC/UDP 7844)── Cloudflare edge
                                                 │  encrypted tunnel
                                        cloudflared container
                                                 │  http://web:8000  (private compose network)
   ┌───────────────┐   enqueue    ┌───────────────┐   pull (1 at a time)   ┌──────────────┐
   │ web (FastAPI)  │ ───────────▶ │ redis (RQ)     │ ◀──────────────────── │ worker        │
   │ uvicorn :8000  │              │ appendonly     │                        │ SimpleWorker  │
   │ expose-only    │              │ noeviction     │                        │ ffmpeg (nice) │
   └──────┬────────┘              └───────────────┘                        └──────┬───────┘
          │  SQLite (WAL)   ◀── shared volume db_data ──▶   SQLite (WAL)            │
          │  live progress  ◀── Redis (hot % only) ──▶     progress writes         │
          │  media_tmp      ◀── shared volume media ──▶    render scratch          │
          └──────────────────────────────────────────── os.remove / rmtree ◀──────┘
```

## Trust boundaries
- The box makes **only outbound** connections (to Cloudflare's edge). No inbound ports are opened on
  the Oracle VCN security list or the local firewall. The public hostname resolves to Cloudflare,
  which proxies over the tunnel to the internal `web:8000`.
- `web` is reachable only on the private Docker network (`expose:`, never `ports:` on `0.0.0.0`).
- Secrets (`FERNET_KEY`, `TUNNEL_TOKEN`, `GITHUB_PAT`, OAuth secrets) live in `.env` (chmod 600),
  never in git. Stored third-party credentials are Fernet-encrypted at rest in SQLite.

## Data flow (one episode)
1. Scheduler/worker picks the next Pending/Active campaign episode → creates a `Task`.
2. `core/ai_engine` asks Gemini for a structured script + 3 A/B metadata variants.
3. `core/safety_filter` cleans the narration (profanity/brand safety) and checks the variation gate.
4. Per scene: `core/tts` (edge-tts) → mp3 + word timings; `core/media` measures duration
   (**audio = ground truth**); Pexels clips are selected to cover it; `core/ffmpeg_runner`
   re-encodes to 1080×1920 with burned captions + optional branding.
5. Scenes are stitched with the concat demuxer `-c copy` (no re-encode) → `master.mp4`;
   `core/thumbnail` renders a cover.
6. Output is parked in the `BufferPool` (pre-render ahead of schedule) or published immediately.
7. `services/*` publish to the campaign's mapped channel; `services/telegram_bot` alerts.
8. `core/cleanup` removes the whole job workspace — nothing lingers > 60 min.

## Concurrency model
Render concurrency is **exactly 1**, guaranteed three independent ways:
1. One worker container, never scaled.
2. One in-process `SimpleWorker` (no fork) consuming one `renders` queue → strictly sequential.
3. A Redis global lock (`SET NX EX`) inside the render task → belt-and-suspenders if a second
   worker ever appears.
The worker is capped at `cpus: 3.0` and ffmpeg runs at `nice -n 19`, leaving a core for web/redis/OS.

---

# ADR log (append-only)

Architecture Decision Records. Newest at the bottom. Each records the decision and *why*, so the
rationale survives.

### ADR-001 — SQLite over a database server
**Decision:** Use SQLite (WAL mode) as the only datastore.
**Why:** Single box, zero cost, modest write volume (one render at a time). WAL + `busy_timeout`
handles the rare web-vs-worker write collision. A DB server would add a container, memory, and ops
burden for no benefit (KISS/YAGNI). High-frequency progress goes to Redis, not SQLite, to keep the
single writer near-idle.

### ADR-002 — Plaintext SQL dump for backups, not the binary `.db`
**Decision:** Back up `sqlite3 .dump` plaintext SQL (`factory_dump.sql`), not the `.db` file.
**Why:** Binary blobs bloat and can corrupt git history and defeat delta compression. Plaintext SQL
delta-compresses beautifully across daily commits. `VACUUM INTO` a snapshot first (read transaction,
no long exclusive lock) so backups don't fight the worker.

### ADR-003 — Cloudflare Tunnel (token mode), no inbound ports
**Decision:** Expose the app only through a `cloudflared` tunnel using `TUNNEL_TOKEN`.
**Why:** Zero-cost edge, no public ingress to attack, no self-managed TLS. Token mode keeps ingress
config in the Cloudflare dashboard (KISS). No 80/443/8000 ever opened on the box.

### ADR-004 — Render concurrency hard-capped at 1
**Decision:** Exactly one render machine-wide; never scale the worker.
**Why:** CPU-only ARM. Two concurrent x264 encodes at 1080×1920 saturate all cores and can trigger
kernel lockups / OOM. Sequential rendering with a buffer pool (render ahead of schedule) gives
throughput without the risk.

### ADR-005 — `EncryptedString` column type over manual encrypt/decrypt
**Decision:** Transparent at-rest encryption via a SQLAlchemy `TypeDecorator`.
**Why:** Encryption lives at exactly one binding point (DRY). Call sites read/write plain strings, so
nobody can forget to decrypt or accidentally persist/log a plaintext secret (SRP). Adding a secret
column is just `mapped_column(EncryptedString)`.

### ADR-006 — Content variation is branding, not detection-evasion
**Decision:** The per-video visual variation is an optional channel-branding/pacing feature
(watermark, subtle tint, TTS-rate pacing); the text filter is profanity/brand-safety. Neither is
built or tuned to defeat platform duplicate-detection or anti-spam systems. The bulk-variation gate
defaults **off**, and `core/safety_filter` surfaces the near-duplicate-posting ToS risk.
**Why:** Building tooling whose purpose is evading platform integrity systems is out of scope and
against policy. The legitimate branding/testing use is fully served by this framing. Operators are
responsible for platform-ToS compliance.

### ADR-007 — Synchronous SQLAlchemy and RQ `SimpleWorker`
**Decision:** Sync DB access; `SimpleWorker` (no fork) for the worker.
**Why:** SQLite serializes writes regardless of async; the render worker is inherently synchronous
(subprocess ffmpeg). Async adds complexity with no throughput gain (KISS). `SimpleWorker` avoids
fork overhead and makes the single-render guarantee trivial.

### ADR-008 — Push-to-main CD via raw SSH, credentials in GitHub Secrets
**Decision:** `.github/workflows/deploy.yml` deploys on merge to `main` by SSHing into the VPS (raw
`ssh`, host-key pinned via `SSH_KNOWN_HOSTS`, configurable non-default `SSH_PORT`) and running
`scripts/deploy.sh` there (`git reset --hard origin/main` → `docker compose up -d --build`).
**Why:** No third-party marketplace action (smaller supply-chain surface, KISS). All credentials
live in GitHub Secrets, never in the repo. The box keeps its own `.env` and named volumes, so the
deploy transmits **no secrets** and never risks the DB/media — it only updates code and rebuilds.
The worker's 300s stop grace means an in-flight render finishes before its container is recreated.
Pull happens on the box (needs a read-only deploy key), keeping GitHub's egress one-directional.

### ADR-009 — Browser login via Firebase REST + signed session cookie (no CDN, no JS SDK)
**Decision:** The multi-tenant `/login` page authenticates with the **Firebase Auth REST API**
(email/password sign-in + sign-up) and a **server-side Google OAuth** flow that exchanges the Google
id_token via `accounts:signInWithIdp` — no Firebase JS SDK, no external CDN script. After any
successful auth, the browser POSTs the Firebase ID token to `/auth/session`, which verifies it with
firebase-admin and mints a **signed Starlette session cookie** (`SECRET_KEY`, SameSite=Lax,
`SESSION_MAX_AGE_DAYS`). `get_current_user` accepts either a `Bearer` ID token (API clients) or the
session cookie (browsers); unauthenticated browser navigations 303-redirect to `/login`.
**Why:** Keeps the "no runtime CDN" property (KISS, CSP-friendly, self-contained) and reuses the
existing SessionMiddleware and Google OAuth helper (DRY). Trade-off: a signed session is not
server-revocable before expiry (disabling a Firebase user takes effect on next login, worst case
`SESSION_MAX_AGE_DAYS`); acceptable at this scale and documented in the RUNBOOK.

### ADR-010 — Render/publish split with an optional review gate
**Decision:** Rendering and publishing are separate steps. `render_task` produces the episode into
the buffer pool; in auto mode it publishes in the same job, in **review mode**
(`auto_publish=false`) it parks the item as `awaiting_review` and the operator previews the actual
MP4 in the browser (authenticated ranged streaming) before **Approve** (queues `publish_task`) or
**Reject** (deletes files; the task fails and is re-renderable via Retry). Publish outcomes
(`published_video_id`, `published_url`) and timings (`started_at`/`finished_at`) are recorded on
the task; the campaign episode counter advances only on actual publish.
**Why:** Trust in an autonomous system comes from a human checkpoint being *available* (not
mandatory) and from transparency. The split also gives cheap upload-only retries (no re-render for
a failed upload) and manual publish control. The publish job runs on the same single queue (KISS:
uploads are short and sequential-safe on one box). The 72h buffer expiry deliberately skips
`awaiting_review` items — only `ready` items age out.

### ADR-011 — Slot-timed publishing, episode memory, and the persona layer
**Decision:** Three coupled changes. (1) **Cadence:** rendering is eager (buffers stay full);
posting slots control *publishing* — exactly one pre-rendered episode per slot, in the campaign's
own timezone, with a recent-publish guard against double-posting; tasks parked for a slot show as
`SCHEDULED`. No slots = publish right after render; review mode = publish on approval. (2)
**Episode memory:** every generation returns a one-line `synopsis`, stored on the task; later
episodes receive prior synopses with continuity mode `no_repeat` (fresh premise every time) or
`serial` (genuinely continue the story). (3) **Persona layer:** per-campaign persona, style
examples (few-shot), and signature open/close catchphrases are composed into the system prompt for
every generation — one voice across narration (hence subtitles), titles, and descriptions — plus
always-on anti-AI-tell writing rules (spoken register, no formulaic AI phrasing).
**Why:** The earlier design slot-gated *rendering* and published at render-completion, which could
dump a full buffer in one slot window — slot-timed publish from the buffer is what the buffer pool
was for. Memory and persona are what separate "content factory" output from a recognisable
creator: consistency + non-repetition + local voice. Compliance: a persona is a creative character,
not an impersonation of a real person; operators must follow platform synthetic-content disclosure
rules (see RUNBOOK).

### ADR-012 — Cinema Polish + the two-loop self-improvement engine
**Decision:** (1) **Cinema Polish**: every clip gets subtle motion (zoom-in/pan/zoom-out rotating
deterministically per scene, baked into the single encode pass) and captions get themes
(classic/highlight/boxed/neon) with per-word pop animation and the campaign accent colour;
defaults ON for every campaign. (2) **Loop 1 — critic pass** (works from video #1): a second
Gemini call reviews each script as a harsh editor (hook ≤2s, spoken-ness, persona fidelity,
freshness); a 'rewrite' verdict triggers exactly one revision with the concrete issues injected;
critic failures never block a render. Operators rejecting a review-mode video give a one-line
reason that becomes an avoid-instruction for future scripts. (3) **Loop 2 — data loop**: a daily
pass (Redis NX guard) pulls per-video stats — retention % above all — from the free YouTube
Analytics API (new `yt-analytics.readonly` scope; pre-existing channels need a reconnect) and FB
insights into `Task.stats_json`; weekly, per campaign with ≥5 measured episodes, a distiller call
rewrites the channel's bounded **Playbook** (≤15 lessons + top-3 examples, patterns must span ≥3
videos) stored in `Campaign.learning_json` — a separate column so form edits can never wipe it.
The playbook, best examples and avoid-notes are composed into every future generation, and are
fully visible/resettable on the Performance page.
**Why:** "Better every video" requires a closed loop: measure → learn → inject. The critic raises
the floor immediately; the data loop optimises for what this channel's real audience rewards.
Bounded, guarded and transparent by design — learning refines tactics, never overrides the persona
or safety rules, and never becomes a black box. All of it stays $0 (free-tier Gemini calls, free
Analytics API, motion/captions ride the existing encode pass).

### ADR-013 — Auto-QC gate: the machine reviews its own output; human review is the backup
**Decision:** A per-campaign **Auto-QC** gate (default ON) makes review-free operation safe.
(1) **Footage vetting** — before a scene renders, up to 3 leading Pexels candidates are judged by
Gemini vision (one extracted frame vs the scene's narration); the first accepted clip leads,
rejected leaders are dropped, downloads are reused. (2) **Colour grade** — an optional
per-campaign look (cinematic/warm/cool/vivid/noir) baked into the existing single encode pass,
applied before captions so text is never graded. (3) **Loudness** — the final stitch normalizes
audio to −14 LUFS (`loudnorm=I=-14:TP=-1.5:LRA=11`), the short-form platform target, so every
episode publishes at the same perceived volume; audio-only re-encode, video stays stream-copied.
(4) **Final verdict** — 4 frames sampled across the finished master are judged for readable
captions and coherent visuals; a failing verdict triggers exactly **one** automatic re-render, and
a second failure parks the episode in the Asset Pool as AWAITING_REVIEW with the issues listed —
it is never published. The verdict is stored in the episode metadata (visible in the Asset Pool).
**Why:** the operator wants "perfect with no human touch — manual is just in case". That demands
the pipeline judge itself with the same signal a human reviewer uses (looking at frames), while
keeping two safety properties: **fail-open** (a vision-API outage degrades to the pre-QC pipeline,
never blocks an episode — availability of the nightly upload beats a stricter gate) and
**fail-closed on quality** (a video the machine judged bad twice waits for a human instead of
publishing). Costs stay $0: a handful of extra free-tier Gemini vision calls per episode; grade
and loudnorm ride existing encode passes on the CPU-only box.

### ADR-014 — Pre-deployment hardening pass
**Decision:** A full-codebase review (four parallel readers, then per-finding adversarial
verification against the live code) surfaced and fixed a set of correctness, robustness, and
secrets-hygiene defects before first production use. The fixes, grouped:
- **Campaign lifecycle:** completion is `current_episode >= total_episodes` (was `>`, so a
  campaign never completed and the next pending campaign never auto-activated); a re-render
  (Retry-after-reject, or an expired slot item) now REPLACES the prior buffer row for that episode
  instead of colliding on the `(campaign, episode)` unique constraint (which had dead-ended Retry
  in a re-render→fail loop); `publish_task` is idempotent (a double-enqueued slot/Approve can't
  upload twice); an expired pre-rendered buffer fails its stranded SCHEDULED task so Retry recovers
  it; post-publish hydration is isolated so a hydration hiccup can't flip a just-published episode
  to FAILED.
- **Crash recovery:** a render lock present at worker startup is cleared (single-worker topology →
  it's a crash artifact) so a hard crash mid-render can't dead-letter the whole queue; the stuck
  reaper also reaps long-stranded `PENDING_QUEUE` tasks; the scheduler tick isolates each campaign
  so one tenant's fault can't starve the others.
- **Render engine:** ffmpeg stderr is drained to a temp file (a >64 KB stderr burst could deadlock
  the reader and hang the single-render box until timeout); a callback error now kills+reaps the
  child instead of leaving a zombie; global progress is monotonic (was a per-scene sawtooth); the
  encoder honors `-threads` (it was only reaching the input decoder); footage search, footage
  vetting, and background-music download all fail safe; the brand-safety filter no longer falls
  back to the raw text when it strips a scene to empty; the orphan sweeper never deletes the
  workspace of the render in flight, even under disk pressure.
- **Auth / secrets:** multi-tenant boot now fails fast on a missing/insecure `SECRET_KEY`; OAuth
  callbacks reject a missing/mismatched `state` (closing the `None != None` hole) and consume the
  pending-user id; the session cookie is `Secure`; credential-test errors never echo the URL that
  embeds the API key/bot token; `.dockerignore` keeps `.env` and the Firebase key out of image
  layers; the backup script wipes the PAT-bearing clone on every exit and never prints the PAT.
- **Publishing:** YouTube token refresh preserves the originally-granted scopes (a fixed subset had
  downscoped the refreshed token and silently killed the analytics/self-improvement loop) and
  rehydrates the stored expiry so proactive refresh actually runs.
**Deferred (documented, not code-changed):** the two zoom motion effects may render subtly on some
ffmpeg builds (verify on the box; the pan effect is unaffected); `WORK_ROOT` must stay free of
spaces/quotes (the `ass=` filtergraph path isn't shell-escaped — default paths are safe); a very
long render that fails Auto-QC could exceed the single job timeout on its re-render (raise
`JOB_TIMEOUT_SECONDS` if renders routinely run long). None block deployment.
**Why:** the operator asked for a hands-off factory; these were the seams where "hands-off" could
strand a campaign, hang the box, publish twice, or leak a secret. Every fix keeps the existing
architecture — the changes harden the edges, they don't reshape the system.

### ADR-015 — Registry-based CD (build in Actions, pull on the VPS)
**Decision:** CD no longer builds on the box. `deploy.yml` builds the `linux/arm64` image in GitHub
Actions and pushes it to GHCR (`ghcr.io/<owner>/<repo>`, tagged `latest` + the git SHA); the deploy
job then SSHes in, ships `docker-compose.yml` + `scripts/deploy.sh`, logs the box into GHCR with the
workflow's ephemeral `GITHUB_TOKEN` (piped to `docker login --password-stdin`, never stored), and
runs `docker compose pull` + `up -d` on the pinned SHA tag. `docker-compose.yml` references
`ghcr.io/<owner>/<repo>:${AVF_IMAGE_TAG:-latest}` (with `build: .` kept for local builds). The box
now holds only its `.env` (and the two shipped files) — no source checkout, no deploy key, no
long-lived registry secret.
**Why:** the render box is a single CPU-only ARM instance with a hard "never lock it up" rule and
CPU-bound renders. Building the image there (compiling grpcio et al.) competed with rendering and
required a full git checkout + read credential on the box — the exact bootstrap that made the first
deploy fail. Moving the build into Actions keeps the box's CPU for rendering, shrinks its footprint
to one hand-placed file (`.env`), and gives immutable, per-SHA images with instant rollback
(`AVF_IMAGE_TAG=<sha>`). **Trade-off:** the repo is private, so ARM builds run under QEMU emulation
on x86 runners (slow first build, then Actions-cached) rather than on free native-ARM runners
(public-repo only). Accepted: deploys are infrequent and the cache makes steady-state builds
reasonable; keeping build load off the render box is worth more than build latency. Secrets posture
is unchanged — `.env` still lives only on the box, and GHCR auth uses the run's short-lived token.

### ADR-016 — Failure circuit breaker, closed A/B loop, and layered voice/grammar QC
**Decision:** (1) After **3 consecutive** failed episodes (newest-first streak of terminal task
outcomes; anything non-FAILED resets it), the worker's failure path flips the campaign to the
existing `failed` status and sends ONE Telegram alert. `failed` was already skipped by hydration
and slot publishing and already had a ▶ Start (resume) button — the breaker gives that status its
purpose instead of adding a new `paused` enum value. Guarding the trip on `status == active`
makes the alert fire exactly once; if an episode already in the queue later succeeds anyway,
`advance_campaign` re-activates the campaign (self-heal). Reaper-failed tasks count toward the
streak only on the NEXT `_fail_task` evaluation — the breaker is an anti-noise valve, not an SLA.
(2) The A/B loop closes with one nullable `tasks.ab_variant` column copied from the buffer's
metadata at publish time; the Performance page aggregates retention/views per variant in Python
(3 variants × small N — no SQL aggregation needed).
(3) QC gains grammar and voice coverage at ~zero marginal API cost, layered by what each check
costs: grammar rides on the EXISTING critic call (a `grammar_score` dimension + rewrite trigger —
subtitles are the narration verbatim, so a typo is burned into every frame); voice sanity is a
DETERMINISTIC ffmpeg `volumedetect` + duration check after each scene's TTS (silent/truncated
output → one re-synthesis, then a loud failure — it fails CLOSED, unlike vision QC, because it is
free and exact); and perceptual voice quality (clarity, language, music balance) rides on the
EXISTING final-QC vision call by attaching the master's audio track (ADTS stream copy, no
re-encode), falling back to frames-only judging if extraction fails.
**Why:** a systemic fault (dead API key, revoked channel token, spent daily quota) used to retry
every queued episode and alert on each — burning quota and waking the operator with noise that
all had one cause. The breaker turns N alerts into one actionable one. The A/B rotation had been
generating variants blindly since Phase 5 — recording which variant actually shipped is the
missing half that makes the rotation an experiment. The QC layering keeps the quota math from
ADR-013/the batching work intact: an episode still costs ~4-5 Gemini calls with strictly more
coverage.

### ADR-017 — One voice catalog, required episode memory, and music config-truth
**Decision:** (1) `core/tts.py` gains `VOICE_CHOICES`, the single per-language voice catalog
(id + human label); the campaign form renders it as a dropdown that follows the Target language,
and the AI designer's `PROPOSABLE_VOICES` is derived from it at import time. (2) `VideoScript.
synopsis` is now required (`min_length=1`) instead of defaulting to empty, and the worker falls
back to the variant-A title when storing episode memory. (3) `music_mode=auto` without a
`FREESOUND_API_KEY` raises at render time (mirroring the existing missing-music-file behavior),
the AI designer downgrades auto→none on a keyless box, and a mood with zero CC0 matches retries
once with a generic query.
**Why:** the voice field was free text — a typo produced a campaign whose every render failed in
TTS, and the designer kept its own separate voice list that could drift from what the form knew.
One catalog makes an unusable voice unrepresentable in both entry paths. The optional synopsis
was the continuity feature's silent failure mode: any episode whose model response omitted it
became invisible to all later no-repeat/serial prompts, which is indistinguishable from
"continuity doesn't work" for the operator. Making it required moves the failure into the
existing repair-turn loop (one extra call only when the model misbehaves). Music followed the
config-truth rule already applied to missing music files: a deterministic misconfiguration
(missing key) fails loudly and visibly, while transient provider failures still degrade to a
music-less render — an enhancement outage must never fail a video, but a permanent misconfig
must never be silent.

### ADR-018 — Gemini model selection moves into the UI; .env is only the default
**Decision:** the Gemini model fallback chain becomes a per-user setting chosen on the
Credentials page (`users.gemini_model`, plaintext — a model id is not a secret), resolved
everywhere as `user.gemini_model or settings.GEMINI_MODEL`. The picker fetches the LIVE model
list from the REST `models` endpoint with the user's key (one cheap, un-metered call) and
overlays `GEMINI_MODEL_CATALOG` — a curated, dated, ADVISORY table of free-tier RPM/TPM/RPD
numbers with a link to Google's authoritative rate-limits page. Rate limits are deliberately NOT
scraped or fetched: Google exposes no API for per-account quota numbers, they differ per tier,
and a stale hardcoded number presented as live truth is worse than a labeled estimate.
**Why:** the model choice is an operational decision that changes with Google's quota policy
(observed: flagship flash at 20 RPD free while flash-lite had 500), and editing `.env` + restart
for it violated the "manage in the dashboard" principle every other credential already follows.
Per-user (not per-campaign) matches the quota's blast radius — the daily cap is per API key, and
keys are per user. The chain keeps the ADR-quoted fallback semantics (404/daily-quota fail over)
unchanged; it now just originates from the DB instead of the environment.

### ADR-019 — Front-end design system: tokens, 12-col grid, one status-colour vocabulary, Jinja macros
**Decision:** the UI is rebuilt on an explicit design system instead of ad-hoc per-page markup.
`static/app.css` starts with a token layer — colour (4 dark surfaces, text, a brand accent, and a
SEMANTIC STATUS SET `--st-{success,working,scheduled,review,failed,pending}` each with solid/bg/border
variants), a modular type scale, a 4px spacing scale, radii, and elevation — and every component reads
those tokens, so status colours are defined ONCE and reused identically by pills, banners, table
highlights and chart bars. Repeated components live as Jinja macros in `templates/macros.html`
(pill/page_head/card/stat/progress/bar/banner/empty/field); shared behaviour lives in `static/ui.js`
(a `busyButton(btn, label, run)` that generalises the copy-pasted async-button idiom, plus the mobile
drawer toggle with aria state). Layout is a 12-column grid + `.grid.cols-N` auto-fill, collapsing to a
single column at mobile. The sidebar becomes an off-canvas drawer under a top bar below 720px (it used
to simply vanish). Data-viz is hand-rolled CSS/inline-SVG bars only — NO chart library, NO CDN, NO
external fonts/icons — honouring the self-contained/CSP constraint; every metric answers a persona
question (health strip + AI-quota meter, campaign progress, A/B retention comparison, episode
mini-bars, calendar runway). All backend contracts are preserved: route paths, form field names,
element ids/`data-*` the JS and tests key on (`voice-select`+`data-current`, `task-rows`, `data-test`,
tab panels), the flash whitelist, and the `textContent`-only / `esc()` XSS boundary.
**Why:** the dashboard had grown feature-by-feature into 11 templates with zero shared components,
hard-coded px/hex everywhere, and a mobile experience that hid the entire navigation — unusable for the
primary Operator persona, who checks in from a phone. One token vocabulary + a component layer kills
copy-paste drift (a status colour or spacing change is now one edit, not eleven), makes the three usage
modes (Operator/Reviewer/Strategist) first-class at both 375px and 1280px, and keeps the whole thing
inside the box's no-egress, single-stylesheet, hand-written-JS constraints.

### ADR-020 — Live status via one read-only /api/summary poll; client-owned form validation
**Decision:** ease-of-use features that need fresh server state read from a single read-only
`GET /api/summary` endpoint that returns `{health, counts, channels, active_campaigns}` by reusing
the exact helpers the dashboard renders from (`_system_health`, `_task_counts`) — so a polled value
can never diverge from a full reload. `ui.js` polls it every 6s on every page and drives (a) a
cross-page attention badge on the Task Logs / Asset Pool nav items + mobile hamburger, and (b) the
dashboard's live health strip / tiles / banners. Destructive actions use an accessible in-page
confirm dialog (`data-confirm` on the form; graceful fallback to native `confirm()`), and transient
feedback uses aria-live toasts. The campaign form carries `novalidate` and validates entirely in JS,
because a native `required` field on a hidden tab panel blocks submit with an un-focusable bubble
(a silent failure); JS validation instead jumps to the offending field's tab and shows the reason.
**Why:** the primary Operator persona checks in from a phone and must never miss a failure or a
video awaiting review — a badge that follows them across pages beats a static dashboard they have to
reload. Deriving counts client-side from the 50-row `/api/tasks` feed would drift from the real
totals, so a purpose-built snapshot that shares the dashboard's own code is the honest choice. The
endpoint is read-only and tenant-scoped (`CurrentUser`), adds no business logic, and touches no other
package — the only backend change in the UI work, and squarely a "template needs live data" one.

### ADR-021 — Optional light theme via `[data-theme]`; client-owned filtering; thin flow shortcuts
**Decision:** the design system stays dark-first but gains an OPTIONAL light theme as a single
`:root[data-theme="light"]` token override block (status hues darken to keep WCAG-AA on white); a
tiny inline `<head>` script applies the saved `localStorage` preference before first paint to avoid a
flash, and a toggle in the sidebar/top bar flips it. List filtering (Asset Pool status chips,
Campaigns + Task Logs search) and keyboard review (J/K/A/R) are done entirely client-side over the
already-rendered rows — no new endpoints, no server round-trips. The one flow shortcut that needs the
server, "Create & Start", is a single optional `start_now` form field on the existing `POST /campaigns`
that reuses the standalone start route's exact logic (`status=active` + `hydrate_buffers`). The most
destructive action (channel delete, which cascades campaigns + renders) upgrades its confirm to a
type-the-name gate.
**Why:** a light option is cheap once everything is tokenised and some operators prefer it on a
bright desktop, but dark stays the default the product was designed around. Filtering/search/keyboard
belong on the client — the data is already on the page, and a round-trip would only add latency and
backend surface for what is pure presentation. "Create & Start" removes a two-step create-then-start
dance without duplicating logic; and requiring the channel name to be typed makes the one irreversible
cascading delete deliberate rather than a stray click.

### ADR-022 — Master–detail "object hub" navigation for Channel → Campaign → Assets/Tasks
**Decision:** the four flat lists (Channels, Campaigns, Asset Pool, Task Logs) are wired into the real
hierarchy without adding route paths. Every entity name is a link to its home; related collections
appear as counts that link to a SCOPED list view (`/campaigns?channel=`, `/assets?campaign=` or
`?channel=`, `/tasks?campaign=`); each scoped view narrows its query SERVER-SIDE from the URL param
and renders a breadcrumb + a "show all" clear, so the URL is the single source of truth (back-button,
bookmarking, sharing all work). Rollup counts (campaigns-per-channel, buffer-per-campaign) are small
read-only `group by` queries added to the relevant page contexts. The existing
`/campaigns/{id}/performance` route is promoted to the campaign HUB with an Overview · Assets · Tasks ·
Edit tab row — a real detail page reusing a route that already existed. Task Logs is JS-rendered, so
its scope filters `/api/tasks` client-side over the campaign id the page embeds; every other scope is
server-side.
**Why:** a management console's whole job is to let you travel the relationships between objects, not
eyeball parallel lists. The master–detail + scoped-list + breadcrumb pattern is the industry norm
(GitHub, Stripe, YouTube Studio) and maps 1:1 to the `User → Channel → Campaign → {Task,
BufferPoolItem}` model. Doing the scoping server-side from the URL (rather than client-side over a
fully-loaded list) keeps it correct at any size and makes views shareable; reusing Performance as the
hub delivers a true detail page without new top-level routes or a heavier backend.

### ADR-023 — Dashboard as a "trust instrument": triage, narrative, and change over raw counts
**Decision:** the dashboard is reframed from a status board into a sense-making surface for an
unattended, self-learning system. It leads with a **triage inbox** ("Needs your attention") — the
concrete failed/awaiting-review items, most-recent first, with inline Retry (reusing the task-retry
endpoint) and Review links — or a calm **"All clear"** state when the queue is empty. Below it, an
**activity feed** renders the pipeline as a narrative ("Published · Failed · Awaiting review", with
relative times), and a client-side **"N new since your last visit"** marker (last-visit stamp in
localStorage) answers the 2–3×/day checker's real question — *what changed?* The Asset Pool shows the
channel's learned **playbook + avoid-notes beside the player** (review-in-context) so judgment happens
against known criteria; rejecting with a reason states plainly that it becomes a permanent avoid-note,
and Performance surfaces the closed **learning loop** ("your rejections shaped these notes; they steer
every new script"). The campaign form carries a live **identity card** — a plain-language summary of
the channel being created. A global `[hidden] { display:none !important }` rule guarantees the hidden
attribute always beats component `display` (so JS-toggled cards/badges hide reliably).
**Why:** the product's real UX job is trust, not controls — the operator glances for ~30 seconds and
must know instantly whether to act. Counts are low-information; a prioritized action queue, a legible
history, and a "what's new" diff are what make an autonomous factory feel steerable. Showing the
learning loop closing is what keeps a human bothering to give feedback that improves the AI. The
backend additions are read-only (two focused queries for the triage lists) and reuse existing helpers.

### ADR-024 — Adaptive-first responsive design + content-hashed static assets
**Decision:** the UI moves from breakpoint-only to adaptive-first. Layout adapts *continuously* — a
fluid type scale and page gutter via `clamp()`, intrinsic `auto-fit`/`minmax` grids (stats,
scorecard, cards), and a main column that grows then centres (`max-width` + `margin-inline:auto`,
capped at 1400px so wide monitors no longer strand content on the left). Media queries are reserved
for genuine MODE changes only: table layout uses a **container query** (`.table-wrap` is an
`inline-size` container; `.stack-table` collapses to cards when *its wrapper* is narrow — so a table
stacks even in a narrow column on a wide screen, which viewport queries cannot express), and the
navigation shell has exactly three tiers (full sidebar >1024px · compact icon rail 721–1024px ·
off-canvas drawer + bottom tab bar ≤720px). Browsers without container-query support fall back to the
existing horizontal-scroll wrapper. Separately, static assets are served through a `static_url()`
template global that appends a per-file content hash (`/static/app.css?v=<sha1>`), so a deploy always
busts the browser cache.
**Why:** "correct at 375 and 1280, hope in between" is fragile — intrinsic sizing is correct at every
width by construction and uses the room on large monitors instead of wasting it. Container queries put
adaptation where it belongs (the component's own space), which is the only correct model once the same
table can appear both full-width and in a narrow scoped column. The cache-busting fix closes a real
production trap: the refactor's new HTML was being styled by a stale cached stylesheet (and, worse, a
stale `ui.js` would drop the confirm-dialog guards) — a content hash makes every build self-invalidating
with zero operator action. This also fixed four smaller bugs found in the audit: the campaign-form
identity card bound to the wrong form in multi-tenant mode (now a stable id), the login page ignored the
saved theme (added the no-FOUC head script), and the skip-link revealed on mobile taps (now
`:focus-visible`, keyboard-only).

### ADR-025 — Server-side pagination + filtering, immutable static caching, visibility-aware polling
**Decision:** the two unbounded list surfaces now paginate on the server. The Asset Pool takes
`?status=`/`?page=` and renders 24 cards per page behind filter chips whose counts are true
per-status tallies over the *whole* scope (a `GROUP BY status` query, not a count of the current
page); the chips are plain `<a>` links (URL is the single source of truth — shareable, back-button
correct, no JS state), replacing the old client-side buttons that only hid already-loaded DOM. The
Performance episode table paginates the same way (20 rows/page, newest first) while its aggregates —
A/B variant summary, retention sparkline, best-episode 🏆 — keep reading the *full* episode list, so
pagination never distorts a metric. Page clamping (`min(max(page,1),pages)`) makes out-of-range URLs
safe. Separately, `CachedStaticFiles` sends `Cache-Control: public, max-age=31536000, immutable` but
**only** for requests carrying the `?v=` content hash — a plain `/static/app.css` request stays
uncached — so hashed URLs are cached forever and non-versioned fetches never go stale. Finally both
JS pollers became visibility-aware: they clear their timer on `visibilitychange` when the tab is
hidden and refresh immediately on return, and the task poller additionally adapts its interval (fast
while a job is in flight, relaxed once every task is terminal).
**Why:** the list pages loaded every asset/episode a campaign ever produced into one DOM — fine at a
handful, a real cost once a long-running channel accumulates hundreds. Bounding the query at the
database is the only fix that scales; doing the counts as a scoped `GROUP BY` keeps the chips honest
regardless of which page you're on. Keeping the filter in the URL (rather than JS) makes every view
linkable and lets the server send only the rows it means to show. The immutable-cache header is the
serving-side complement to ADR-024's content hash: the hash makes each build a new URL, and
`immutable` tells the browser it never needs to revalidate that URL — together they give
zero-revalidation caching that is still instantly correct on deploy. Visibility-aware polling stops a
backgrounded tab from hitting `/api/tasks` and `/api/summary` forever; the adaptive interval spends
requests where they matter (an active render) and backs off when nothing is happening — meaningful on
a single CPU-only box.
