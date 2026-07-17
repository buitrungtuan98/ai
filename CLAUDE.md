# CLAUDE.md — Agent Contract for AI Video Factory

> **Read this first, every session.** Before writing or changing any code, read
> [`docs/CODING_STANDARDS.md`](docs/CODING_STANDARDS.md) and
> [`docs/SYSTEM_MAP.md`](docs/SYSTEM_MAP.md). They are short by design and tell you where
> everything lives and how to write it.

AI Video Factory is a zero-cost, multi-tenant-ready SaaS that auto-generates vertical
short-form videos and publishes them to YouTube/Facebook. It runs on **one** Oracle Cloud
Always-Free ARM box (4 cores / 24 GB / 200 GB, **CPU-only**), behind a Cloudflare Tunnel,
with daily Git backups.

## Hard constraints (never violate)

1. **Render concurrency = 1.** Exactly one video renders at a time, machine-wide. Never scale the
   worker, never fork render jobs, never remove the Redis global render lock. CPU-only ARM will
   lock up otherwise.
2. **The web service is never published to a host port.** No `ports:` mapping to `0.0.0.0`; ingress
   arrives only through Cloudflare Tunnel → `web:8000`. No open 80/443/8000 on the box.
3. **Secrets live only in `.env`** (and Firebase creds file). `FERNET_KEY`, `TUNNEL_TOKEN`,
   `GITHUB_PAT`, OAuth secrets, API keys are **never** committed, logged, or written into the SQL
   dump in plaintext. All stored third-party credentials are Fernet-encrypted at rest.
4. **CPU-only.** No GPU code paths. ffmpeg runs `-threads 4` at `nice -n 19`.
5. **ffmpeg is a system binary**, invoked via `subprocess` through `core/ffmpeg_runner.py`. Do not
   add an ffmpeg wrapper library.

## Coding principles (see `docs/CODING_STANDARDS.md` for repo-concrete examples)

- **DRY** — one definition of each thing (settings, queue config, crypto, ffmpeg invocation).
- **KISS** — the simplest thing that works on one box.
- **YAGNI** — no feature/dependency/container without a `docs/ROADMAP.md` task justifying it.
- **SOLID** — one responsibility per module; a render function never also enqueues or uploads.
- **Boy Scout Rule** — leave every file you touch cleaner than you found it, and update its
  `docs/SYSTEM_MAP.md` row.

## Definition of Done (a change is not "done" until all hold)

- [ ] Code has type hints and passes lint/format and `pytest`.
- [ ] `docs/SYSTEM_MAP.md` updated — row added/edited and its `Last updated` bumped for any file
      added, renamed, deleted, or materially changed.
- [ ] `docs/ROADMAP.md` status flipped for the affected task (`TODO`→`WIP`→`DONE`).
- [ ] An ADR appended to `docs/ARCHITECTURE.md` if an architectural decision or trade-off changed.
- [ ] No new public host port; no secret added to code, logs, git, or the SQL dump; `.env.example`
      updated if a new environment variable was introduced.
- [ ] DRY / KISS / YAGNI / SOLID self-review done.

**Documentation is part of the change, not an afterthought — keep the docs at latest on every
commit.** If you add a source file without a `SYSTEM_MAP.md` row, the change is incomplete;
`scripts/check_docs.py` will flag it.

## Where things live (quick index — full detail in `docs/SYSTEM_MAP.md`)

| Concern | Module |
|---|---|
| Config / env | `core/config.py` (only place env is read) |
| Encryption | `core/security.py` + `database/types.py` (`EncryptedString`) |
| DB models / session | `database/models.py`, `database/db_session.py` |
| Auth / tenancy | `auth/dependencies.py` |
| AI script/metadata | `core/ai_engine.py` |
| Safety / ToS gate | `core/safety_filter.py` |
| Rendering | `core/video_factory.py` + `core/{ffmpeg_runner,tts,media,captions,thumbnail,cleanup}.py` |
| Queue / worker | `workers/task_queue.py`, `workers/video_worker.py`, `run_worker.py` |
| Publishing | `services/{youtube_service,facebook_service,telegram_bot}.py` |
| Web / UI | `main.py`, `templates/`, `static/` |

## Compliance note (settled decision — see ADR-006)

The per-video visual variation is an **optional channel-branding / pacing** feature (watermark,
subtle tint, TTS-rate pacing). It is **not** built or tuned to evade platform duplicate-detection
or anti-spam systems; the bulk-variation gate defaults **off**. The text filter is a
**profanity / brand-safety** filter. Operators must comply with each platform's Terms of Service;
mass-posting near-identical content can violate platform policy regardless of byte differences.
