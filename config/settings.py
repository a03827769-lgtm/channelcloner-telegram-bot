import os
import re
import logging
from pathlib import Path
from typing import Optional, Set, Tuple
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, PrivateAttr, field_validator

logger = logging.getLogger(__name__)

# Project root (the directory that contains run.py). The .env file is resolved from here so the
# configuration is found no matter which working directory the process was started from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_ADMIN_IDS_SPLIT = re.compile(r'[,;\s]+')
# Values copied verbatim from .env.example ("YOUR_BOT_TOKEN_HERE") must never be used as real secrets
_PLACEHOLDER_RE = re.compile(r'^YOUR_[A-Z0-9_]+_HERE$', re.IGNORECASE)


def _is_placeholder(value) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.match(value.strip()))

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    BOT_TOKEN: str = Field(default="", description="Telegram Bot Token from @BotFather")
    ADMIN_BOT_TOKEN: str = Field(default="", description="Dedicated Admin Bot Token from @BotFather")
    TELEGRAM_API_ID: Optional[int] = Field(default=None, description="Telegram API ID from my.telegram.org")
    TELEGRAM_API_HASH: Optional[str] = Field(default=None, description="Telegram API Hash from my.telegram.org")
    TELETHON_SESSION: Optional[str] = Field(default=None, description="StringSession or session name for Telethon")

    PRIMARY_SUPER_ADMIN_ID: int = Field(default=0, description="Root Super Admin Telegram ID from environment")
    ADMIN_IDS_RAW: str = Field(default="", alias="ADMIN_IDS", description="Comma-separated admin user IDs")
    DB_PATH: str = Field(default="database/cloner.db", description="Path to SQLite database")
    TEMP_DOWNLOAD_DIR: str = Field(default="temp_media", description="Directory for temporary media files")
    ENCRYPTION_KEY: Optional[str] = Field(default=None, description="Master AES encryption key independent from bot tokens")
    VAULT_KEY_PATH: str = Field(default="database/.vault_key", description="Machine-local vault key file used when ENCRYPTION_KEY is not set")
    PROXY_URL: Optional[str] = Field(default=None, description="Optional HTTP/SOCKS proxy for Bot API requests, e.g. socks5://user:pass@host:1080")
    PORT: int = Field(default=8080, description="Keep-alive HTTP server port")
    GEMINI_API_KEY: Optional[str] = Field(default=None, description="Google Gemini API key for AI Paraphraser")
    DROP_PENDING_UPDATES: bool = Field(default=False, description="Whether to drop pending updates on bot startup")
    ENABLE_PLAYWRIGHT: bool = Field(default=False, description="Enable Playwright Chromium rendering; default False for ultra-fast, zero-overhead PIL rendering")
    AUTO_RELOAD: bool = Field(default=False, description="Enable in-process file watcher for hot reloading")
    SUPPORT_USERNAME: str = Field(default="admin", description="Support contact username shown in Private mode")
    WEBAPP_URL: str = Field(default="http://localhost:8080", description="Base URL for Telegram Mini App")

    # Parsed admin ID set, cached per (raw value, primary id) so it is not re-parsed on every update.
    _admin_ids_cache: Optional[Tuple[Tuple[str, int], Set[int]]] = PrivateAttr(default=None)

    @field_validator("ENABLE_PLAYWRIGHT", "AUTO_RELOAD", "DROP_PENDING_UPDATES", mode="before")
    @classmethod
    def parse_bool_flags(cls, v):
        if isinstance(v, str):
            v_lower = v.strip().lower()
            if v_lower in ("0", "false", "no", "off", "disable", "disabled", ""):
                return False
            if v_lower in ("1", "true", "yes", "on", "enable", "enabled"):
                return True
        return bool(v) if v is not None else False

    @field_validator("BOT_TOKEN", "ADMIN_BOT_TOKEN", "ADMIN_IDS_RAW", mode="before")
    @classmethod
    def strip_tokens(cls, v):
        if isinstance(v, str):
            return "" if _is_placeholder(v) else v.strip()
        return v or ""

    @field_validator("TELEGRAM_API_ID", mode="before")
    @classmethod
    def parse_api_id(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            return int(str(v).strip())
        except (ValueError, TypeError):
            return None

    @field_validator("PRIMARY_SUPER_ADMIN_ID", mode="before")
    @classmethod
    def parse_primary_admin(cls, v):
        # An empty or malformed value must not crash the whole application at import time.
        if v is None or (isinstance(v, str) and not v.strip()):
            return 0
        try:
            return int(str(v).strip())
        except (ValueError, TypeError):
            logger.warning(f"Invalid PRIMARY_SUPER_ADMIN_ID {v!r} ignored")
            return 0

    @field_validator("TELEGRAM_API_HASH", "TELETHON_SESSION", "ENCRYPTION_KEY", "PROXY_URL", "GEMINI_API_KEY", mode="before")
    @classmethod
    def parse_empty_strings(cls, v):
        if isinstance(v, str):
            val = v.strip()
            return val if val and not _is_placeholder(val) else None
        return v

    @field_validator("VAULT_KEY_PATH", mode="before")
    @classmethod
    def parse_vault_key_path(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return "database/.vault_key"
        return str(v).strip()

    @field_validator("PORT", mode="before")
    @classmethod
    def parse_port(cls, v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return 8080

    @property
    def admin_ids(self) -> Set[int]:
        """Super admin IDs from the environment (ADMIN_IDS plus PRIMARY_SUPER_ADMIN_ID)."""
        cache_key = (self.ADMIN_IDS_RAW or "", self.PRIMARY_SUPER_ADMIN_ID or 0)
        cached = self._admin_ids_cache
        if cached is not None and cached[0] == cache_key:
            return set(cached[1])

        ids: Set[int] = {self.PRIMARY_SUPER_ADMIN_ID} if self.PRIMARY_SUPER_ADMIN_ID else set()
        for part in _ADMIN_IDS_SPLIT.split(cache_key[0]):
            part = part.strip()
            if not part:
                continue
            try:
                admin_id = int(part)
            except ValueError:
                logger.warning(f"Invalid admin ID format skipped: {part!r}")
                continue
            if admin_id > 0:
                ids.add(admin_id)
            else:
                logger.warning(f"Non-positive admin ID skipped: {part!r}")
        self._admin_ids_cache = (cache_key, ids)
        return set(ids)

    def is_configured(self) -> bool:
        return bool(self.BOT_TOKEN and self.TELEGRAM_API_ID and self.TELEGRAM_API_HASH)


def _env_file() -> Optional[str]:
    """The .env file to read: CLONER_ENV_FILE when it is set (an empty value disables the file, which the
    test suite uses to stay independent of the production configuration), otherwise <project root>/.env."""
    override = os.environ.get("CLONER_ENV_FILE")
    if override is not None:
        return override.strip() or None
    return str(PROJECT_ROOT / ".env")


settings = Settings(_env_file=_env_file())
