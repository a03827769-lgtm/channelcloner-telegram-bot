import re
from typing import Optional, Set
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator

# Pre-compiled at module level — admin_ids property uses this on every message
_ADMIN_IDS_SPLIT = re.compile(r'[,;\s]+')

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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
    PORT: int = Field(default=8080, description="Keep-alive HTTP server port")
    GEMINI_API_KEY: Optional[str] = Field(default=None, description="Google Gemini API key for AI Paraphraser")
    DROP_PENDING_UPDATES: bool = Field(default=False, description="Whether to drop pending updates on bot startup")
    ENABLE_PLAYWRIGHT: bool = Field(default=False, description="Enable Playwright Chromium rendering; default False for ultra-fast, zero-overhead PIL rendering")
    AUTO_RELOAD: bool = Field(default=False, description="Enable in-process file watcher for hot reloading")
    SUPPORT_USERNAME: str = Field(default="admin", description="Support contact username shown in Private mode")
    WEBAPP_URL: str = Field(default="http://localhost:8080", description="Base URL for Telegram Mini App")

    @field_validator("ENABLE_PLAYWRIGHT", "AUTO_RELOAD", "DROP_PENDING_UPDATES", mode="before")
    @classmethod
    def parse_bool_flags(cls, v):
        if isinstance(v, str):
            v_lower = v.strip().lower()
            if v_lower in ("0", "false", "no", "off", "disable", "disabled"):
                return False
            if v_lower in ("1", "true", "yes", "on", "enable", "enabled"):
                return True
        return bool(v) if v is not None else False

    @field_validator("BOT_TOKEN", "ADMIN_BOT_TOKEN", mode="before")
    @classmethod
    def strip_tokens(cls, v):
        if isinstance(v, str):
            return v.strip()
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

    @field_validator("TELEGRAM_API_HASH", "TELETHON_SESSION", "ENCRYPTION_KEY", mode="before")
    @classmethod
    def parse_empty_strings(cls, v):
        if isinstance(v, str):
            val = v.strip()
            return val if val else None
        return v

    @field_validator("PORT", mode="before")
    @classmethod
    def parse_port(cls, v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return 8080

    @property
    def admin_ids(self) -> Set[int]:
        raw = self.ADMIN_IDS_RAW
        ids: Set[int] = {self.PRIMARY_SUPER_ADMIN_ID} if self.PRIMARY_SUPER_ADMIN_ID else set()
        if raw:
            for part in _ADMIN_IDS_SPLIT.split(raw):
                part = part.strip()
                if part:
                    try:
                        ids.add(int(part))
                    except ValueError:
                        pass
        return ids

    def is_configured(self) -> bool:
        return bool(self.BOT_TOKEN and self.TELEGRAM_API_ID and self.TELEGRAM_API_HASH)

settings = Settings()
