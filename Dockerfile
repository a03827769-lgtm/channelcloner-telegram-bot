FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set working directory
WORKDIR /app

# Install system dependencies (including ffmpeg for video watermarking)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    ca-certificates \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY config/ ./config/
COPY database/ ./database/
COPY services/ ./services/
COPY bot/ ./bot/
COPY admin_bot/ ./admin_bot/
COPY run.py .
COPY setup_wizard.py .

# Create database and temp storage directories
RUN mkdir -p /app/database /app/temp_media

# Create non-root user for security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

# Expose HTTP port for keep-alive healthchecks (Koyeb, Render, Cloud PaaS)
EXPOSE 8080

# Switch to non-root user
USER appuser

# Run the application
CMD ["python", "run.py"]
