# Coding Standards

These are the rules every change in this repo follows. They exist so the codebase stays small,
predictable, and safe to run unattended on a single free-tier box. Examples are **repo-concrete** —
they reference real modules, not abstractions.

## The five principles

### DRY — Don't Repeat Yourself
Each fact has exactly one home. Import it; never re-type it.

- Environment/config is read **only** in `core/config.py` (`settings` singleton). No `os.getenv`
  anywhere else.
- The Redis URL, queue name (`renders`), and global render lock key live **only** in
  `workers/task_queue.py`. Import them; don't re-string `"renders"`.
- Encryption happens in **one** place: the `EncryptedString` column type
  (`database/types.py`) backed by `core/security.py`. Never hand-roll `encrypt()`/`decrypt()` at a
  call site.
- Every ffmpeg invocation goes through `core/ffmpeg_runner.run_ffmpeg(...)` (one place applies
  `nice -n 19`, `-threads 4`, and `-progress` parsing).
- One Gemini call primitive (`core/ai_engine.generate_structured`) is reused for both the script
  and the metadata.

### KISS — Keep It Simple
Prefer the simplest thing that works on one box.

- SQLite (not Postgres), Redis + RQ (not Celery/Kafka), Cloudflare Tunnel (no self-managed
  nginx/TLS), raw `ffmpeg` subprocess (not a wrapper lib), a single `.env`.
- Synchronous SQLAlchemy — SQLite serializes writes anyway and the worker is synchronous; async
  buys nothing here.
- Word timings come free from edge-tts `WordBoundary` events — no forced aligner.
- Cleanup is one job directory removed with `rmtree` — no per-file registry.

### YAGNI — You Aren't Gonna Need It
Build only what a current `docs/ROADMAP.md` task requires.

- No new container, service, or dependency without a roadmap task that justifies it.
- No multi-worker scaling — the box is single-render **by design** (see ADR-004).
- No provider-abstraction framework; a plain function per external provider is enough until a
  second provider actually exists.

### SOLID — one responsibility per module
- `web` serves HTTP; `worker` renders; `task_queue` wires the queue; `backup_db.sh` dumps. A render
  function does **not** also enqueue or upload.
- `core/safety_filter.py` owns all ToS/brand-safety policy — that logic never leaks into render code.
- External providers (Pexels, edge-tts, Gemini) are reached through thin functions so they *could*
  be swapped (DIP) — without building an abstraction layer we don't need yet (YAGNI).
- Cross-cutting SQLite PRAGMA config lives in one `connect` event listener, not scattered per query.

### Boy Scout Rule
When you touch a file, leave it cleaner: fix the small smell you see (a missing type hint, a dead
import, a magic literal), and **update that file's row in `docs/SYSTEM_MAP.md`**. Small, in-scope
improvements only — don't start unrelated refactors.

## Conventions

- **Type hints** on every function signature. Target Python 3.11+.
- **Config** via `from core.config import settings`. Fail fast at startup on missing required vars.
- **Secrets**: never in source, logs, or the SQL dump. Stored credentials use `EncryptedString`.
  Never `print`/`log` a decrypted secret.
- **Errors**: every external call (Gemini, Pexels, edge-tts, YouTube, Facebook, Telegram, ffmpeg,
  Redis) is wrapped in `try/except`. Worker jobs catch, record `Task.error_message`, alert via
  Telegram, and move on — a single failure never takes down the queue.
- **Logging**: use the stdlib `logging` module with structured, greppable messages. No `print` in
  library code.
- **Naming**: `snake_case` for functions/vars, `PascalCase` for classes, `UPPER_SNAKE` for module
  constants. Filenames match their single responsibility.
- **Imports**: standard lib, third-party, then local — no wildcard imports.
- **Tenant isolation**: routes never accept `user_id` from the client; it comes only from
  `get_current_user`. Foreign-id access returns 404, not 403.

## Definition of Done

Repeated from `CLAUDE.md` because it is the enforcement point:

1. Code + type hints pass lint/format and `pytest`.
2. `docs/SYSTEM_MAP.md` row updated (+ `Last updated`).
3. `docs/ROADMAP.md` status flipped.
4. ADR appended to `docs/ARCHITECTURE.md` if a decision changed.
5. No new public port; no secret in code/log/git/dump; `.env.example` updated for new vars.
6. DRY/KISS/YAGNI/SOLID self-review.
