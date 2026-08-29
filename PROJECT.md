# Project: Telegram Channel Cloner 24/7 Deployment

## Architecture
- **Public Telegram Bot** (`@klonlabot`): Aiogram 3 async long-polling, handles user channel pair registrations, interactive filter configurations, watermarking, translations, and forwarding triggers.
- **Admin Management Bot** (`@klonlaadminbot`): Aiogram 3 async long-polling, handles super-admin controls, broadcast messages, channel stats, and interactive MTProto OTP login.
- **MTProto Userbot Listener**: Telethon async client listening to source channels, media groups, restricted/protected content, buffering media, and forwarding to destination channels via `cloner_engine`.
- **24/7 Keep-Alive HTTP Server**: `aiohttp.web` application running on dynamic `PORT` (default 8080) at `/` and `/health`, answering GET and HEAD probes with `{"status": "ok", "bot": "running", ...}` to prevent cloud container idle sleep.
- **Storage & State**: SQLite with WAL mode (`database/cloner.db`), memory deduplication cache (`cache_manager.py`), Fernet encrypted string session storage (`security_vault.py`).
- **Container & Cloud PaaS**: Multi-stage Debian Slim Docker image with FFmpeg, DejaVu fonts, curl healthchecks; Koyeb and Render manifest blueprints for 1-click zero-cost deployments.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Keep-Alive HTTP Server in `run.py` | Asynchronous `aiohttp.web` server responding at `/` and `/health` with `{"status": "ok", "bot": "running", ...}` on dynamic `$PORT` | M1 | ORIGINAL_REQUEST §R2 |
| 2 | Production Multi-Stage `Dockerfile` | Lean ~210MB Python 3.11 image with FFmpeg, DejaVu fonts, curl healthcheck, and non-root `appuser` | M2 | ORIGINAL_REQUEST §R3 |
| 3 | Container Orchestration `docker-compose.yml` | Full service configuration with persistent volumes, ulimits, logging limits, and healthcheck probe | M2 | ORIGINAL_REQUEST §R3 |
| 4 | Cloud Manifests `koyeb.yaml` & `render.yaml` | Zero-cost PaaS declarative configs for Frankfurt region, port 8080, healthcheck probe `/health`, single instance | M2 | ORIGINAL_REQUEST §R3 |
| 5 | PaaS Entrypoint `Procfile` | Direct `web: python run.py` definition for Heroku/Render/Dokku compatible hosts | M2 | ORIGINAL_REQUEST §R3 |
| 6 | Environment Template `.env.example` | Comprehensive environment variable template documenting all 9 configuration parameters with Uzbek explanations | M3 | ORIGINAL_REQUEST §R4 |
| 7 | Complete Deployment Documentation `DEPLOYMENT.md` | Step-by-step guides for Koyeb, Render, Docker, and UptimeRobot keep-alive setup | M3 | ORIGINAL_REQUEST §R4 |
| 8 | Robust `.gitignore` & `.dockerignore` | Multi-layer protection blocking `.env*`, `cloud_env_ready.txt`, `*.session*`, `*.db*`, `temp_media/`, `.agents/` | M4 | ORIGINAL_REQUEST §R1 |
| 9 | Private GitHub Repository Creation & Push | Create `a03827769-lgtm/channelcloner-telegram-bot` as a Private repo via `gh` and push `main` branch | M4 | ORIGINAL_REQUEST §R1 |
| 10 | E2E Healthcheck & Concurrency Verification | Automated empirical tests for HTTP 200 responses, schema validity, and non-blocking concurrency | M5 | ORIGINAL_REQUEST §Acceptance Criteria |
| 11 | Forensic Integrity Audit | Static analysis and secret scans verifying no hardcoded credentials or mock facades | M5 | Integrity Forensics |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | HTTP Keep-Alive Healthcheck Integration | Verify and refine `start_health_server()` in `run.py` to return required schema and handle graceful shutdown signals | Survey | PLANNED |
| M2 | Containerization & Cloud Deployment Manifests | Create multi-stage `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `koyeb.yaml`, `render.yaml`, `Procfile` | M1 | PLANNED |
| M3 | Environment Templates & Documentation | Create `.env.example` and `DEPLOYMENT.md` setup guides | M1 | PLANNED |
| M4 | Git Security, Secret Sanitization & Private GitHub Push | Create `.gitignore`, sanitize staging area, create private GitHub repository `channelcloner-telegram-bot`, push to `main` | M2, M3 | PLANNED |
| M5 | E2E Empirical Verification & Forensic Integrity Audit | Execute automated healthcheck tests, verify container build, run challenger stress tests, and conduct forensic audit | M4 | PLANNED |

## Interface Contracts
### HTTP Keep-Alive Healthcheck (`run.py`)
- **Routes**: `GET /`, `HEAD /`, `GET /health`, `HEAD /health`
- **Host / Port**: `0.0.0.0:${PORT:-8080}`
- **Success Status**: `HTTP/1.1 200 OK`
- **Content-Type**: `application/json; charset=utf-8`
- **Payload Schema**:
  ```json
  {
    "status": "ok",
    "bot": "running",
    "service": "telegram-channel-cloner",
    "telethon_connected": true
  }
  ```

## Code Layout
- `run.py`: Application entrypoint, initializes DB, launches bots, starts HTTP healthcheck server, and manages asyncio lifecycle.
- `config/settings.py`: Pydantic settings loading environment variables.
- `database/`: SQLite async database manager, migrations, and model schemas.
- `services/`: Telethon MTProto listener, watermark generator, video processor, cache manager, encryption security vault.
- `bot/`: Public Telegram Bot handlers, keyboards, states, middleware.
- `admin_bot/`: Admin Management Bot handlers, statistics, broadcast, MTProto OTP wizard.
- `Dockerfile`: Multi-stage Docker container build.
- `docker-compose.yml`: Local & VPS container stack orchestration.
- `koyeb.yaml` & `render.yaml`: PaaS cloud deployment configuration blueprints.
- `Procfile`: PaaS process definition.
- `.gitignore` & `.dockerignore`: Sensitive files and build artifact filters.
- `.env.example`: Environment variables template.
- `DEPLOYMENT.md`: 24/7 Zero-cost deployment manual.
