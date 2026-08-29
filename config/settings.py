import os
from typing import List, Optional, Set
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

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
    
    ADMIN_IDS_RAW: str = Field(default="", alias="ADMIN_IDS", description="Comma-separated admin user IDs")
    DB_PATH: str = Field(default="database/cloner.db", description="Path to SQLite database")
    TEMP_DOWNLOAD_DIR: str = Field(default="temp_media", description="Directory for temporary media files")

    @property
    def admin_ids(self) -> Set[int]:
        if not self.ADMIN_IDS_RAW:
            return set()
        try:
            return {int(x.strip()) for x in self.ADMIN_IDS_RAW.split(",") if x.strip()}
        except ValueError:
            return set()

    def is_configured(self) -> bool:
        return bool(self.BOT_TOKEN and self.TELEGRAM_API_ID and self.TELEGRAM_API_HASH)

settings = Settings()
