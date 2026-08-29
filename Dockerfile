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

# Install runtime packages (FFmpeg for video watermark, DejaVu fonts, curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    ca-certificates \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv

# Create non-root application user
RUN useradd -m -u 1000 -s /bin/bash appuser && \
    mkdir -p /app/database /app/temp_media && \
    chown -R appuser:appuser /app

# Copy application source code
COPY --chown=appuser:appuser config/ ./config/
COPY --chown=appuser:appuser database/ ./database/
COPY --chown=appuser:appuser services/ ./services/
COPY --chown=appuser:appuser bot/ ./bot/
COPY --chown=appuser:appuser admin_bot/ ./admin_bot/
COPY --chown=appuser:appuser run.py setup_wizard.py ./

# Expose HTTP healthcheck port
EXPOSE 8080

# Native Docker Healthcheck probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:${PORT:-8080}/health || exit 1

# Switch to non-root user
USER appuser

# Application entrypoint
CMD ["python", "run.py"]
