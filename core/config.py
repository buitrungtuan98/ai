"""Application configuration — the single place environment/`.env` is read.

DRY: every module imports the `settings` singleton from here; there is no `os.getenv` anywhere
else in the codebase. Required secrets have no defaults, so the app fails fast at startup rather
than breaking later (e.g. a missing FERNET_KEY would silently break decryption of every stored
credential).
"""
from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Core mode ---
    MULTI_TENANT_MODE: bool = False

    # --- Encryption (required) ---
    # Comma-separated Fernet keys (first is used to encrypt; all are tried to decrypt → rotation).
    FERNET_KEY: str

    # --- Datastore ---
    DATABASE_URL: str = "sqlite:////data/db/factory.db"
    REDIS_URL: str = "redis://redis:6379/0"

    # --- Filesystem ---
    MEDIA_ROOT: str = "/data/media"
    WORK_ROOT: str = "/data/media/work"

    # --- Google OAuth2 (YouTube) ---
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    OAUTH_REDIRECT_BASE: str = "http://127.0.0.1:8000"

    # --- Firebase (multi-tenant only) ---
    FIREBASE_CREDENTIALS_PATH: str | None = None
    # Public web API key (Firebase console → Project settings). Used by the /login page for the
    # Firebase Auth REST API — it is an identifier, not a secret, but lives in .env like all config.
    FIREBASE_WEB_API_KEY: str | None = None

    # --- Optional global fallback provider keys (per-user keys take priority) ---
    GEMINI_API_KEY: str | None = None
    PEXELS_API_KEY: str | None = None
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None

    # --- Cloudflare / backups ---
    TUNNEL_TOKEN: str | None = None
    GITHUB_PAT: str | None = None
    BACKUP_REPO: str | None = None
    BACKUP_BRANCH: str = "main"

    # --- Rendering tuning ---
    FFMPEG_THREADS: int = 4
    FFMPEG_NICE: int = 19
    FFMPEG_PRESET: str = "veryfast"
    DEFAULT_BUFFER_SIZE: int = 3
    JOB_TIMEOUT_SECONDS: int = 2700
    ORPHAN_MAX_AGE_MINUTES: int = 60

    # --- Scheduler / automation tick ---
    TIMEZONE: str = "UTC"                   # IANA name (e.g. Asia/Ho_Chi_Minh); posting slots use it
    SCHEDULER_INTERVAL_SECONDS: int = 3600  # hourly buffer-hydration + housekeeping
    SLOT_TOLERANCE_MINUTES: int = 30        # how close to a posting slot counts as "now"
    BUFFER_MAX_AGE_HOURS: int = 72          # expire pre-rendered buffer items older than this
    DISK_PRESSURE_PCT: int = 90             # sweep aggressively above this disk usage

    # --- App ---
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = Field(default="dev-insecure-session-key-change-me")
    SESSION_MAX_AGE_DAYS: int = 7  # signed browser session lifetime (multi-tenant login)

    @model_validator(mode="after")
    def _require_firebase_in_multi_tenant(self) -> "Settings":
        """Fail fast at boot (not at first login) if public mode lacks Firebase credentials."""
        if self.MULTI_TENANT_MODE and not self.FIREBASE_CREDENTIALS_PATH:
            raise ValueError(
                "MULTI_TENANT_MODE=true requires FIREBASE_CREDENTIALS_PATH to be set."
            )
        return self


settings = Settings()  # module-level singleton — import this, do not instantiate elsewhere.
