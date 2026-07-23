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
a single CPU-only box. ADR-025 deliberately deferred paginating Task Logs (a *live* surface, unlike
these two server-rendered pages); ADR-026 revisits that.

### ADR-026 — Paginating the live Task Logs feed (server-side page + search + scope)
**Decision:** Task Logs — the one client-rendered, continuously-polled table — now paginates on the
server too. `/api/tasks` gained `?page=` (25/page, newest first), `?q=` (SQL `ilike` over id / status
/ campaign topic / channel name), and `?campaign=` scope, and it returns `{tasks, page, pages, total}`;
the hard `LIMIT 50` that previously *hid* all older history is gone. `app.js` drives it: a debounced
search box and a Newer/Older pager that both re-issue the poll, a monotonic request-sequence guard so a
slow in-flight poll can never overwrite the result of a newer click, and it adopts the server's clamped
`page` from every response. The adaptive/visibility polling from ADR-025 is unchanged, and now has a
pleasant side effect — history pages contain only terminal tasks, so the poller automatically relaxes
to the slow interval while you browse them. **Why:** the earlier deferral was wrong once the real
constraint was named. The client filter and the `LIMIT 50` meant search and history both silently
stopped at the 50 newest rows — a long-running channel's older failures were simply unreachable and
unsearchable. A live table can't hold unbounded history in the DOM, so the only correct fix is to move
paging, search *and* scope into SQL where they span the whole history, and to keep the browser holding
just one page. Search moved server-side rather than staying a client convenience specifically so it
searches everything, not the 25 rows currently loaded; doing it in SQL also handles the Vietnamese
topic text the operator actually types. The request-sequence guard is the one piece a static paginated
page doesn't need — on a table that also refreshes itself every few seconds, without it a background
poll racing a pager click would snap the user back to the wrong page.

### ADR-027 — Script depth (research brief) + deterministic AI-cliché gate
**Decision:** two additions attack the "a bot wrote this" quality of scripts at the source. (1) An
optional per-campaign `script_depth` (`standard` default | `deep`): in deep mode, `generate_brief`
runs one research pass producing an `EpisodeBrief` (3-8 concrete facts — real names/dates/numbers —
plus a hook→build→payoff→cliffhanger arc), and the script-generation prompt is conditioned on it, so
the narration carries specific substance instead of generic filler. (2) A per-language AI-cliché
blacklist (`AI_CLICHES`) with a pure `find_cliches()` detector, wired three ways: injected into the
script prompt (generator avoids the phrases), injected into the critic prompt (critic flags them),
and a free deterministic post-draft gate that forces exactly one targeted rewrite naming the phrases
if any survive. Both are fail-open and default to today's behavior (`standard`, no brief; a clean
draft triggers no rewrite). **Why:** one-shot generation with no research tends to waffle, and models
reliably reach for the same tells ("delve into", "hãy cùng tìm hiểu"). A brief gives the generator
real material to work from — the single biggest lever on content quality — while deep mode stays opt-in
because it costs one extra Gemini call per episode (counted against the same daily budget meter). The
cliché gate follows the codebase's established "cheap deterministic gate before/after the expensive
step" pattern (voice_check, the length-fit rewrite): free, testable, and it catches the tells the
model ignores instructions about. Neither is detection evasion (ADR-006) — the goal is natural spoken
language, and operators still follow platform synthetic-content disclosure rules.

### ADR-028 — Sound craft: paced narration + sidechain-ducked music
**Decision:** two audio changes make an episode sound edited by a person. (1) `tts.synthesize_paced`
renders each *sentence* of a scene separately and stitches them with deterministic breath gaps
(`pause_after`: 0.35s default, longer after `?`/`!`/`…`), returning ONE merged word-timing list with
absolute offsets — so captions still align exactly with the assembled audio, and scene duration
(the render's ground truth) naturally includes the pauses. A single-sentence scene falls straight
through to the old `synthesize()` path. The stitch is one ffmpeg re-encode (`aevalsrc` silence +
`concat`), and per-sentence durations come from `probe_duration` so offsets are exact. (2) In
`build_concat_args`, the flat `volume`+`amix` music bed is replaced by `sidechaincompress`: the
narration is `asplit` into a mix copy and a sidechain key, the music (at its `music_volume` floor) is
compressed against that key, then the ducked music is mixed back — music dips under the voice and
swells in the gaps. Video is still stream-copied; loudnorm still normalizes the final mix.
**Why:** two dead giveaways of an auto-generated video are narration with no breathing room and a
music bed that sits at one flat level over speech. Both are fixed in the audio graph without extra
cost: pacing is just silence between existing TTS calls, and ducking is one compressor in the single
audio re-encode that already happens at concat. Keeping the timing merge exact was the hard part —
captions are built from word offsets, so per-sentence synthesis had to re-base every word onto the
stitched timeline. New CI-only integration tests push both filter graphs through real ffmpeg (like
the colour-grade guard) so an invalid `sidechaincompress`/`aevalsrc` option can never ship silently.

### ADR-029 — Edit rhythm: multi-shot scenes, word-aligned cuts, cross-episode footage dedupe
**Decision:** the encode stops letting one clip fill a whole scene. `plan_shots` slices each scene
into shots of ~`SHOT_TARGET_S` (capped at `SHOT_MAX_S`), landing each cut on a word boundary when
one falls in range (the edge-tts word timings we already have), and cycles the scene's clip pool so
consecutive shots use different footage. `build_scene_args` gained an optional `shot_durations` that
`trim`s each clip to its shot before the existing scale/crop/motion/grade/caption pass — so this is
still ONE encode per scene and the concat-copy stitch is untouched. Each shot's length is bounded by
its clip's native duration (never outruns its footage → no black gap), and a final coverage step
absorbs any sub-frame shortfall into the last shot, preserving the "video always covers the audio"
invariant the old overshoot-then-`-t`-trim gave. Motion effect is now seeded by `episode_number` so
episodes don't share an identical rhythm. Footage variety across episodes is handled by a new
`ChannelClipUsage` table: the worker loads a channel's recent clip ids, `prefer_unused` floats unused
clips to the front of each scene's pool, and the ids an episode actually used are recorded afterward.
`select_clips` is removed — `plan_shots` supersedes it. **Why:** the loudest "auto-generated" tell
after the script is the visual pacing — a single stock clip drifting under 10 seconds of narration,
and the *same* clips recurring across a channel's episodes. Cutting on word boundaries makes the edit
feel intentional; seeding motion per episode kills the identical-rhythm feel; the dedupe table stops
the recurring-footage tell. All of it is deterministic (seeded by episode/scene index, never random)
so the render tests stay reproducible, and none of it adds a second encode or a paid service. A
deliberate omission: dip-to-black transitions between scenes would force re-encoding at the concat
stage, breaking the stream-copy stitch that is the biggest CPU saver on the ARM box (hard constraint
1/4) — so transitions were left out rather than pay that cost.

### ADR-030 — Long-video support via a RenderProfile (short stays the default)
**Decision:** a per-campaign `video_format` (`short` default | `long`) selects a `RenderProfile`
(name, width, height, fps). `short` is exactly the historical 1080×1920 constants; `long` is 16:9
1920×1080. Everything geometric now reads the profile instead of module constants —
`motion_filter`, `build_scene_args` (scale/crop/fps/motion), `build_ass` (PlayRes + proportional
caption margin), `generate_thumbnail`, and Pexels search orientation (landscape for long). Because the
profile defaults to `short`, every existing call and test renders byte-identical vertical output — the
feature is purely additive. Long-form also: raises `VideoScript.scenes` to 40 and branches the script
prompt (12-30 scenes, part-numbered titles welcome — the opposite of the Shorts rule); emits YouTube
chapter markers into the description from scene start times (`chapter_lines`, ≥10s-spaced, ≥3 or none);
and takes wider duration bounds (60-900s vs 10-180s), clamped in the campaign form. Publishing is
unchanged — YouTube auto-classifies Short vs regular by aspect/duration, so no upload-API branch is
needed. **Why:** the whole pipeline was hard-coded to one geometry, so "support long video" could have
meant forking the renderer. A single `RenderProfile` threaded through the geometric functions adds the
format without a second code path, and defaulting to `short` guarantees the existing behavior and test
suite are untouched. Chapters and part-numbered titles are the cheap, high-signal "real long-form
creator" cues. The hard render cost of a multi-minute 1080p encode on one CPU-only box is real, but it
stays safe under the render-concurrency-1 lock (hard constraint 1); long campaigns are advised (in the
form) to keep a small daily cap and buffer rather than the pipeline enforcing a new limit. Deferred:
multi-call chaptered *generation* (outline + per-chapter) — single-call generation with a raised scene
cap is enough for a first cut, and the repair loop absorbs the occasional oversized draft.

### ADR-031 — Deterministic QC: free black/silence gates beside the vision judge
**Decision:** `run_deterministic_qc` adds two free, no-API checks on the finished master —
`media.max_black_span` (ffmpeg `blackdetect`) and `media.max_silence_span` (`silencedetect`) — and
fails the gate when a continuous black stretch exceeds 2.5s or a continuous silence exceeds 3.5s. The
worker runs it inside the Auto-QC gate alongside `run_final_qc`; the episode advances only if BOTH
pass, and their issues are merged into the stored QC report. It fails CLOSED on clearly-broken output
(like the render's own `voice_check`), but each detector fails OPEN individually so a probe glitch
never blocks a good render. **Why:** the vision QC is graded and deliberately fails *open* (a Gemini
outage must not halt the factory), which means a catastrophically broken master — all-black footage, a
muted audio track — could sail through when the API is down or simply scores it leniently. A black or
silent stretch is exactly the kind of failure that is cheap and unambiguous to detect deterministically
from ffmpeg, so it belongs in a free gate that runs regardless of the vision API's health. Two precise
detectors beat a handful of fuzzy ones: caption-overflow and "hook-present" were considered and left
out — the former needs a render-and-measure pass (not deterministic from the master), and the latter is
already enforced upstream by the script prompt + critic, so adding flaky versions here would only
produce false rejects (YAGNI).

### ADR-032 — Episode view: one home per episode (UI restructure, phase 1)
**Decision:** a new `/episodes/{task_id}` page gathers an episode's whole lifecycle into one place —
a Queued→Rendering→Review→Scheduled→Published timeline (current step derived from the task status),
the video preview, metadata + Auto-QC verdict, render/retry history, stage-aware actions, and (once
live) published stats. Every other surface links to it (Task Logs rows, Performance episode rows;
Asset Pool/Dashboard follow in later phases). The action buttons POST to the EXISTING shared routes
(`/assets/{id}/approve|reject|rerender|publish-now`, `/api/tasks/{id}/retry`) with a `return_to`
form field; a small `_episode_return` guard accepts only `/episodes/<digits>` and redirects there,
otherwise the routes behave exactly as before (default `/assets` redirects unchanged). **Why:** the
operator's real pain was *tracking* — one episode's story was smeared across Task Logs (render), Asset
Pool (review), Calendar (slot) and Performance (stats), and the human did the join. Giving an episode
a single URL is the highest-leverage fix, and it's almost entirely a read + link layer over data that
already exists (Task ⋈ BufferPoolItem by campaign+episode), so it ships without touching the render or
publish pipeline. Reusing the existing action routes via `return_to` (rather than duplicating them)
keeps one definition of each action and one set of tests; the allowlist guard means a crafted
`return_to` can only ever bounce within the app, never to an external host.

### ADR-033 — One filter grammar across list pages (UI restructure, phase 2)
**Decision:** a single `filter_bar` Jinja macro — status chips with true counts + a server-side
search box, all URL-driven — now renders identically on Campaigns, Channels and Asset Pool (Task Logs
already had server search; its stage chips arrive with the Phase-4 pipeline). Chip counts are computed
over the page's *scope* (channel/campaign drill-down) and are search-independent, so they always read
"how many exist here"; the search term and active status narrow only the visible rows and the paging
count. A `query_string()` template global builds the chip/search hrefs (dropping empty params,
URL-encoding), and Asset Pool keeps a separate `pool_total` (scope count, ignoring search) so an
empty search result shows "no match" rather than the "buffer pool is empty" state. Campaigns' old
client-side, only-shown-above-3 search box is gone. **Why:** the pages had four different filter
models (client hide-search on Campaigns, server chips on Assets, server search on Task Logs, nothing
on Channels), which is the concrete source of the "fragmented, hard to filter" complaint. One macro +
one URL convention means every list filters the same way, every filtered view is linkable and
back-button correct (same principle as the pagination ADRs), and the server sends only the rows it
means to show. Keeping chip counts scope-based rather than search-filtered avoids the confusing
"counts jump around as I type" behavior and keeps the empty-vs-no-match distinction honest.

### ADR-034 — Persistent channel scope switcher (UI restructure, phase 3)
**Decision:** a channel `<select>` in the sidebar (visible on desktop, reachable in the mobile drawer)
scopes the operator's whole workspace to one channel. It's populated by a `nav_channels(request)`
template global — a best-effort, self-contained helper that reuses the auth user-resolution and opens
its own short-lived session, returning [] on any failure so `base.html` always renders. The active
scope lives in the URL (`?channel=<id>`, the same drill-down param the list pages already accept), so
it is shareable and back-button correct; the switcher's onchange just reloads the current path with
(or without) that param, and the scope-aware nav links (Campaigns / Asset Pool / Task Logs) carry the
active `?channel=` so the scope follows you across them. Scoped pages already show a breadcrumb +
"show all" escape and now compute their chip counts within the scope. **Why:** a channel operator was
re-drilling from scratch on every navigation because scope didn't persist. Keeping the scope in the
URL (rather than a cookie/session) matches every other stateful thing in this app — no hidden state,
every scoped view linkable — and reusing the existing `?channel=` param means zero new query plumbing
on the pages. The global-function injection avoids threading `channels` through every route's context
or adding request middleware; it costs one cheap read per page render, acceptable on a single-box app,
and fails open so it can never take a page down.

### ADR-035 — Episodes pipeline list unifies Task Logs + Asset Pool (UI restructure, phase 4)
**Decision:** a new `/episodes` list shows every episode as one row grouped by lifecycle STAGE
(Queued / Rendering / Review / Scheduled / Published / Failed), each a friendly bucket over the 9 raw
task statuses (`_STAGE_STATUSES`). It reuses the Phase-2 filter grammar — stage tabs with counts +
search + scope + pagination, all URL-driven — and every row links to the Phase-1 `/episodes/{id}`
detail page. "Episodes" becomes a primary nav item (Content group); Task Logs and Asset Pool stay as
routes and are linked from the Episodes header ("live render log", "Asset Pool") as the specialized
live/review views, and the mobile tab bar swaps its Tasks slot for Episodes. The list is
server-rendered (not the live JS poller) with relative-time stamps that tick client-side. **Why:** the
fragmentation the operator felt was one episode having no single browsable home — you watched it in
Task Logs, reviewed it in Asset Pool, and mentally joined the two. Episodes is that home at the list
level, and it drops onto already-consistent foundations (the detail view from phase 1, the filter
grammar from phase 2, the scope switcher from phase 3), so it's mostly a query + template, not new
mechanism. It stays server-rendered because "browse/triage by stage" doesn't need per-second liveness
(that's what Task Logs is for) — keeping it static means it's just another instance of the one filter
grammar rather than a second live-polling surface to maintain.

### ADR-036 — Planner: an actionable calendar (UI restructure, phase 5)
**Decision:** the calendar gains week navigation (`?week=<offset>`, clamped to −8..+12) with
Prev/Today/Next controls and a "This week / in N weeks" label; `upcoming_slot_cells` takes the same
`week` offset. Each campaign row's name links to its scoped Episodes list, and a row whose ready
buffer is 0 shows an inline "⚠ buffer empty — check episodes" link. Runway indicators and the
per-campaign-timezone slot computation are unchanged. **Why:** the calendar was a read-only poster of
the next 7 days — you couldn't look ahead or act from it. Week navigation (URL-driven, like every
other stateful view) makes it a planning tool, and linking rows to the Episodes list turns "this slot
won't fill" into a one-click path to the episodes that need attention. A "render now" button was
deliberately NOT added: forcing an out-of-band render would mean a new queue-enqueue endpoint and
interact with the single-render lock / daily-cap logic — higher risk than this frontend-only phase
warrants — so the empty-buffer case links to where the operator already has the controls instead.

### ADR-037 — Global search palette (⌘K) (UI restructure, phase 6)
**Decision:** a command palette (⌘K / Ctrl-K, or "/" when not typing) searches the whole workspace
through one read-only `/api/search` endpoint that spans channels (name), campaigns (topic) and
episodes (synopsis / episode number), tenant-scoped, capped per type, min 2 chars. The palette lives
in `base.html` + `ui.js`: a debounced fetch with a monotonic request-sequence guard (so a slow
response can't overwrite a newer query — same pattern as the Task Logs poller), keyboard navigation
(↑/↓/↵/Esc), and results built with `textContent`/DOM nodes only (never innerHTML — the labels are
user/AI data, honoring the XSS boundary). A sidebar "🔎 Search ⌘K" button opens it for mouse/mobile.
**Why:** "which page do I search on?" was itself part of the fragmentation — each page searched only
its own type. One palette over one endpoint makes finding anything a single reflex and jumps straight
to the right home (a campaign → its Episodes, an episode → its detail view). Reusing the established
request-sequence guard and the textContent-only build means it inherits the app's correctness and
security conventions rather than inventing new ones; keeping the endpoint read-only and per-type-capped
keeps it cheap on the single box.

### ADR-038 — Scope switcher: honest filtering + sticky persistence (bugfix on ADR-034)
**Decision:** the channel scope switcher (ADR-034) had two gaps — some scope-aware surfaces carried
`?channel=` on their nav link but ignored it server-side, and the selection reset whenever you visited
a page that doesn't scope. Fixed: (1) `/tasks` + `/api/tasks` and `/calendar` now truly filter by
`?channel=` (via the channel's campaigns), so the live feed and calendar match what the switcher shows;
(2) the switcher's onchange now MERGES `channel` into the current query string (keeping an active
`status`/`q`, resetting `page`) instead of replacing it; (3) the choice is remembered in localStorage,
so on any page `ui.js` reflects it in the dropdown and rewrites the scope-aware nav links
(/episodes /campaigns /assets /tasks /calendar) to carry it — an explicit `?channel=` in the URL always
wins, and picking "🌐 All channels" clears the memory. The Dashboard and the setup pages
(Channels/Credentials) deliberately stay factory-wide: the health strip, AI-quota meter and scorecard
are machine/all-channel metrics (and `/api/summary` shares those helpers), so scoping them per-channel
would be wrong — persistence keeps the switcher from *resetting* there without pretending those pages
are channel-specific. **Why:** a switcher that shows a channel selected while the page ignores it is
worse than no switcher — it lies. Real server-side filtering makes the promise honest; keeping the
scope in the URL (authoritative) with localStorage only as a convenience layer preserves the
"linkable, no-hidden-state" property of ADR-034 while making the scope feel persistent as you click
around. Merging rather than replacing the query string means switching channel doesn't silently blow
away the filter you were using.
