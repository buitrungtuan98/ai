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
