import re
import logging
from collections import deque
from typing import List
logger = logging.getLogger(__name__)

# Words that mark the number following them as a phone number ("phone: 79261234567")
_PHONE_CONTEXT = r"(?:phone(?:_number)?|telefon|tel|mobile|msisdn|телефон|тел)"

# Patterns for sensitive credentials that must never be exposed in logs
SENSITIVE_PATTERNS = [
    # Telegram Bot Tokens: e.g. 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ_1234567
    (re.compile(r'(?<![\w:])\d{6,}:[A-Za-z0-9_-]{30,}'), '[REDACTED_BOT_TOKEN]'),
    # Values encrypted by the security vault ("enc:<token>") and bare Fernet tokens (gAAAA...)
    (re.compile(r'\benc:[A-Za-z0-9_\-+/=]{16,}'), '[REDACTED_ENCRYPTED]'),
    (re.compile(r'\bgAAAA[A-Za-z0-9_\-=]{40,}'), '[REDACTED_ENCRYPTED_TOKEN]'),
    # Telethon StringSession ("1" + urlsafe base64 of dc id, server address and auth key)
    (re.compile(r'\b1[A-Za-z0-9_-]{80,}={0,2}'), '[REDACTED_SESSION]'),
    # api_hash=<32 hex chars>
    (re.compile(r'(?i)(\bapi[_\s-]?hash\W{0,3})[0-9a-f]{32}\b'), r'\1[REDACTED_API_HASH]'),
    # Phone numbers introduced by a keyword: "phone: 79261234567", "telefon +998 90 123 45 67"
    (re.compile(rf'(?i)(\b{_PHONE_CONTEXT}\b[^\w+]{{0,3}})\+?\d[\d \-()]{{7,18}}\d'), r'\1[REDACTED_PHONE]'),
    # International numbers with a leading plus: "+79261234567", "+998 (90) 123-45-67", "+1 415 555 2671"
    (re.compile(r'(?<![\w+])\+\d{9,15}(?!\d)'), '[REDACTED_PHONE]'),
    (re.compile(r'(?<![\w+])\+\d{1,3}[ \-]?\(?\d{2,4}\)?[ \-]?\d{2,4}[ \-]?\d{2}[ \-]?\d{2,4}(?!\d)'), '[REDACTED_PHONE]'),
    # Uzbek numbers without the plus (998901234567)
    (re.compile(r'(?<!\d)998\d{9}(?!\d)'), '[REDACTED_PHONE]'),
]

_SECRET_SETTINGS = (
    "BOT_TOKEN", "ADMIN_BOT_TOKEN", "TELEGRAM_API_HASH", "ENCRYPTION_KEY", "GEMINI_API_KEY", "TELETHON_SESSION", "PROXY_URL"
)

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
        for secret_attr in _SECRET_SETTINGS:
            val = getattr(settings, secret_attr, None)
            if val and len(str(val)) > 6:
                sanitized = sanitized.replace(str(val), f"[REDACTED_{secret_attr}]")
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    return sanitized


class SecretRedactingFilter(logging.Filter):
    """Handler filter that redacts secrets and phone numbers from the rendered message, traceback and stack
    of every record before the handler writes it. Attach it to every handler (console, rotating file,
    in-memory viewer): filters on a logger do not apply to records propagated from child loggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = sanitize_log_message(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:
                logger.debug("Traceback formatting for redaction failed", exc_info=True)
        if record.exc_text:
            record.exc_text = sanitize_log_message(record.exc_text)
        if record.stack_info:
            record.stack_info = sanitize_log_message(record.stack_info)
        return True


def install_redaction_filter(handler: logging.Handler) -> logging.Handler:
    """Attaches a SecretRedactingFilter to `handler` once."""
    if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
        handler.addFilter(SecretRedactingFilter())
    return handler


class MemoryLogHandler(logging.Handler):
    """
    Thread-safe in-memory ring-buffer logging handler with automated secret redaction.
    Retains the most recent application log lines for real-time inspection in Telegram.
    """
    def __init__(self, maxlen: int = 150):
        super().__init__()
        self.buffer = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        install_redaction_filter(self)

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
