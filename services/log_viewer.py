import re
import logging
from collections import deque
from typing import List
logger = logging.getLogger(__name__)

# Patterns for sensitive credentials that must never be exposed in logs
SENSITIVE_PATTERNS = [
    # Telegram Bot Tokens: e.g. 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ_1234567
    (re.compile(r'\b\d{8,12}:[A-Za-z0-9_-]{30,50}\b'), '[REDACTED_BOT_TOKEN]'),
    # Telethon StringSession (1BQAN... or long base64/ascii session keys)
    (re.compile(r'\b1[A-Za-z0-9_-]{80,}\b'), '[REDACTED_SESSION]'),
    # Fernet encrypted token strings (gAAAA...)
    (re.compile(r'\bgAAAA[A-Za-z0-9_-]{40,}\b'), '[REDACTED_ENCRYPTED_TOKEN]'),
    # International & Uzbek Phone Numbers (+998901234567)
    (re.compile(r'\+?\b(998\d{9})\b'), '[REDACTED_PHONE]'),
    (re.compile(r'\b\+\d{1,3}[-.\s]?\(?\d{2,4}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'), '[REDACTED_PHONE]'),
]

def sanitize_log_message(msg: str) -> str:
    """Masks bot tokens, phone numbers, encryption keys, and session strings from log text"""
    if not msg:
        return msg
    sanitized = msg
    for pattern, replacement in SENSITIVE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)

    # Also dynamically mask configured secrets if available
    try:
        from config.settings import settings
        for secret_attr in ("BOT_TOKEN", "ADMIN_BOT_TOKEN", "TELEGRAM_API_HASH", "ENCRYPTION_KEY", "GEMINI_API_KEY", "TELETHON_SESSION"):
            val = getattr(settings, secret_attr, None)
            if val and len(str(val)) > 6:
                sanitized = sanitized.replace(str(val), f"[REDACTED_{secret_attr}]")
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    return sanitized

class MemoryLogHandler(logging.Handler):
    """
    Thread-safe in-memory ring-buffer logging handler with automated secret redaction.
    Retains the most recent application log lines for real-time inspection in Telegram.
    """
    def __init__(self, maxlen: int = 150):
        super().__init__()
        self.buffer = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            safe_msg = sanitize_log_message(msg)
            self.buffer.append(safe_msg)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    def get_recent_logs(self, count: int = 35) -> List[str]:
        return list(self.buffer)[-count:]

# Global singleton attached to root logger
log_viewer = MemoryLogHandler()
logging.getLogger().addHandler(log_viewer)
