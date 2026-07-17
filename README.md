# AI Video Factory

Zero-cost, automated vertical-video (Shorts/Reels) generation and publishing, built to run on a
single Oracle Cloud Always-Free ARM instance (4 cores / 24 GB / 200 GB, **CPU-only**), behind a
Cloudflare Tunnel, with daily Git backups. Multi-tenant-ready, but ships in **solo mode** by default
for dogfooding.

## What it does
- Generates a structured video script + 3 A/B metadata variants with Gemini.
- Synthesizes narration with edge-tts (the audio's length is the ground truth for video duration).
- Assembles Pexels stock footage into a 1080×1920 clip with burned word-by-word captions.
- Optionally applies channel branding (watermark/tint) and generates a thumbnail.
- Publishes to a mapped YouTube channel or Facebook page, then alerts via Telegram.
- Renders **one video at a time** (CPU-safe), keeping a pre-rendered buffer ahead of schedule.

## Architecture
Four containers on one box: `redis` (RQ broker), `web` (FastAPI, never publicly exposed), `worker`
(single sequential renderer), `cloudflared` (the only path in). See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quick start
```bash
cp .env.example .env          # then fill in every value (see comments in the file)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # -> FERNET_KEY
docker compose up -d          # redis + web + worker + cloudflared
docker compose ps             # wait for all healthy
```
Solo mode (`MULTI_TENANT_MODE=false`, the default) skips Firebase and uses a single built-in admin
user — no login needed for local dogfooding. Flip to `true` to enable Firebase SSO and public
registration.

## Documentation (read before contributing)
- [`CLAUDE.md`](CLAUDE.md) — hard constraints + Definition of Done (read first).
- [`docs/CODING_STANDARDS.md`](docs/CODING_STANDARDS.md) — DRY/KISS/YAGNI/SOLID/Boy-Scout.
- [`docs/SYSTEM_MAP.md`](docs/SYSTEM_MAP.md) — every module, what it does, what depends on it.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — phased build status.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — topology + ADRs.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — ops procedures.

## Testing
`pytest` runs the full suite without any external credentials (external providers are mocked).

## Compliance
The visual-variation feature is optional **channel branding**, not a platform-detection-evasion tool
(see ADR-006). Operators are responsible for complying with YouTube/Facebook Terms of Service.

## License
MIT — see [`LICENSE`](LICENSE).
