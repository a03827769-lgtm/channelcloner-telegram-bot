# ==============================================================================
# Stage 1: Builder Stage (Compiles C-extensions and builds Python wheels)
# ==============================================================================
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies for compiling Python C-extensions (uvloop, cryptography, pillow)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ==============================================================================
# Stage 2: Runtime Stage (Lean, Secure, Production-Ready)
# ==============================================================================
FROM python:3.11-slim AS runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080 \
    DB_PATH=database/cloner.db \
    TEMP_DOWNLOAD_DIR=temp_media

WORKDIR /app

# Install runtime packages (FFmpeg for video watermark, DejaVu fonts, curl for healthcheck, tini, jemalloc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    ca-certificates \
    curl \
    tzdata \
    tini \
    libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv

# Install Playwright Chromium headless engine and system dependencies
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN /opt/venv/bin/playwright install --with-deps chromium && \
    mkdir -p /ms-playwright && \
    chmod -R 777 /ms-playwright

# Configure jemalloc for optimal memory fragmentation management (supports x86_64 and arm64)
RUN mkdir -p /usr/local/lib && \
    JEMALLOC_PATH=$(find /usr/lib -name "libjemalloc.so.2" 2>/dev/null | head -n 1) && \
    if [ -n "$JEMALLOC_PATH" ]; then ln -sf "$JEMALLOC_PATH" /usr/local/lib/libjemalloc.so.2; fi
ENV LD_PRELOAD=/usr/local/lib/libjemalloc.so.2

# Create non-root application user
RUN useradd -m -u 1000 -s /bin/bash appuser && \
    mkdir -p /app/data /app/database /app/temp_media /app/assets && \
    chown -R appuser:appuser /app /ms-playwright

# Copy application source code
COPY --chown=appuser:appuser config/ ./config/
COPY --chown=appuser:appuser database/ ./database/
COPY --chown=appuser:appuser services/ ./services/
COPY --chown=appuser:appuser bot/ ./bot/
COPY --chown=appuser:appuser admin_bot/ ./admin_bot/
COPY --chown=appuser:appuser deploy/ ./deploy/
COPY --chown=appuser:appuser assets/ ./assets/
COPY --chown=appuser:appuser scripts/ ./scripts/
COPY --chown=appuser:appuser run.py setup_wizard.py ./

# Expose HTTP healthcheck port
EXPOSE 8080

# Native Docker Healthcheck probe
HEALTHCHECK --interval=15s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -f http://127.0.0.1:${PORT:-8080}/health || exit 1

# Switch to non-root user
USER appuser

# Application entrypoint with tini process supervisor
ENTRYPOINT ["/usr/bin/tini", "-s", "--"]
CMD ["python", "run.py"]
