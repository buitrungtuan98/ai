# Shared image for the `web` and `worker` services (they differ only by command).
# Multi-arch base: python:3.11-slim resolves to linux/arm64 on the Oracle Ampere box.

# ── Build stage ─────────────────────────────────────────────────────────────
# build-essential is a fallback in case a pinned wheel (e.g. grpcio) lacks an
# aarch64 build for this Python and pip must compile from source.
FROM python:3.11-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ── Runtime stage ───────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg/ffprobe are runtime system binaries (see ADR: no ffmpeg wrapper lib).
# fonts-dejavu provides a default font for burned captions / thumbnails.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        sqlite3 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=build /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY . .

# Data volumes are created/mounted by docker-compose; ensure the mount points exist.
RUN mkdir -p /data/db /data/media/work

# Default command is the web server; the worker service overrides it in compose.
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
