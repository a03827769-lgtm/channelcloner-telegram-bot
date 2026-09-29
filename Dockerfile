# syntax=docker/dockerfile:1
# ==============================================================================
# Telegram Channel Cloner — production image (bot + Mini App API + Mini App SPA)
#
#   docker build -t channelcloner .                                   # default
#   docker build --build-arg INSTALL_PLAYWRIGHT=true -t channelcloner .  # + Chromium
#
# All persistent state lives in /app/data (SQLite DB, vault key, logs, backups):
# mount a volume / persistent disk there. Run exactly ONE container per bot token.
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Mini App (React + Vite) build
# ------------------------------------------------------------------------------
FROM node:20-alpine AS webapp

WORKDIR /webapp
COPY webapp/package.json webapp/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY webapp/ ./
RUN npm run build

# ------------------------------------------------------------------------------
# Stage 2: Python dependencies (compiled wheels stay out of the runtime image)
# ------------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------------------------
# Stage 3: Runtime
# ------------------------------------------------------------------------------
FROM python:3.11-slim AS runner

# Playwright Chromium is only needed with ENABLE_PLAYWRIGHT=true (default: Pillow renderer)
ARG INSTALL_PLAYWRIGHT=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080 \
    DB_PATH=/app/data/cloner.db \
    VAULT_KEY_PATH=/app/data/.vault_key \
    TEMP_DOWNLOAD_DIR=temp_media \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# FFmpeg (video watermark / stories), DejaVu fonts, curl (healthcheck), tini (PID 1), jemalloc
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    ca-certificates \
    curl \
    tzdata \
    tini \
    libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

RUN mkdir -p /ms-playwright && \
    if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
        /opt/venv/bin/playwright install --with-deps chromium && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# jemalloc reduces memory fragmentation of the long-running process (x86_64 and arm64)
RUN mkdir -p /usr/local/lib && \
    JEMALLOC_PATH=$(find /usr/lib -name "libjemalloc.so.2" 2>/dev/null | head -n 1) && \
    if [ -n "$JEMALLOC_PATH" ]; then ln -sf "$JEMALLOC_PATH" /usr/local/lib/libjemalloc.so.2; fi
ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so.2

# Non-root user (the app writes app.pid into /app and its state into data/, logs/, temp_media/)
RUN useradd -m -u 1000 -s /bin/bash appuser && \
    mkdir -p /app/data /app/logs /app/temp_media && \
    chown appuser:appuser /app /app/data /app/logs /app/temp_media /ms-playwright

# Application code only: no .env, database files, vault key, logs or tunnel credentials
# (see .dockerignore); database/ contributes its Python modules only.
COPY --chown=appuser:appuser config/ ./config/
COPY --chown=appuser:appuser database/*.py ./database/
COPY --chown=appuser:appuser services/ ./services/
COPY --chown=appuser:appuser bot/ ./bot/
COPY --chown=appuser:appuser admin_bot/ ./admin_bot/
COPY --chown=appuser:appuser assets/ ./assets/
COPY --chown=appuser:appuser run.py ./
COPY --from=webapp --chown=appuser:appuser /webapp/dist ./webapp/dist

EXPOSE 8080

# Liveness only (the process answers). Orchestrators that route traffic should use /ready.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/health" > /dev/null || exit 1

USER appuser

# tini as PID 1: forwards SIGTERM to Python for a graceful shutdown and reaps FFmpeg/Chromium children
ENTRYPOINT ["/usr/bin/tini", "-s", "--"]
CMD ["python", "run.py"]
