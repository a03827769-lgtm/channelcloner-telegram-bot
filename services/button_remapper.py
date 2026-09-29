import re
import logging
from typing import Optional, List, Dict, Any
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

try:
    from telethon.tl.types import ReplyInlineMarkup
except Exception:
    ReplyInlineMarkup = None

logger = logging.getLogger(__name__)

# Links into Telegram: t.me / telegram.me / telegram.dog (also username.t.me) and every tg:// deep link
_TELEGRAM_LINK_RE = re.compile(
    r'^(?:https?://)?(?:www\.)?(?:[a-z0-9_-]{1,64}\.)?(?:t\.me|telegram\.me|telegram\.dog)(?:[/?#]|$)|^tg:',
    re.IGNORECASE
)
# Inline URL buttons only accept these schemes; anything else makes Telegram reject the whole post
_ALLOWED_BUTTON_URL_RE = re.compile(r'^(?:https?://|tg://)\S+$', re.IGNORECASE)


class SmartButtonRemapper:
    """
    Intelligent Inline Button & CTA Remapper.
    Extracts, filters, and maps interactive inline keyboard buttons from source messages,
    replacing competitor links with destination channel / affiliate links.
    """

    @staticmethod
    def extract_telethon_buttons(message: Any) -> Optional[List[List[Dict[str, str]]]]:
        """
        Extracts raw button rows from a Telethon message instance.
        Returns [[{'text': ..., 'url': ...}], ...]
        Compatible with Telethon <= 1.44 (btn.url) and Telethon 1.45+ (btn.type.url).
        """
        if not hasattr(message, "reply_markup") or not message.reply_markup:
            return None

        markup = message.reply_markup
        if ReplyInlineMarkup and not isinstance(markup, ReplyInlineMarkup) and not hasattr(markup, "rows"):
            return None
        elif not hasattr(markup, "rows"):
            return None

        button_grid: List[List[Dict[str, str]]] = []
        try:
            for row in getattr(markup, "rows", []):
                btn_row: List[Dict[str, str]] = []
                for btn in getattr(row, "buttons", []):
                    text = getattr(btn, "text", "")
                    url = getattr(btn, "url", None)
                    if not url:
                        btn_type = getattr(btn, "type", None)
                        if btn_type and hasattr(btn_type, "url"):
                            url = getattr(btn_type, "url", None)
                    if url and text:
                        btn_row.append({"text": str(text), "url": str(url)})
                if btn_row:
                    button_grid.append(btn_row)
            return button_grid if button_grid else None
        except Exception as e:
            logger.debug(f"Button extraction notice: {e}")
            return None

    @staticmethod
    def is_telegram_link(url: str) -> bool:
        return bool(_TELEGRAM_LINK_RE.match((url or "").strip()))

    @classmethod
    def build_remapped_markup(
        cls,
        source_buttons: Optional[List[List[Dict[str, str]]]],
        target_channel_link: Optional[str] = None,
        custom_cta_buttons: Optional[List[Dict[str, str]]] = None,
        block_competitor_links: bool = True
    ) -> Optional[InlineKeyboardMarkup]:
        """
        Builds a production Aiogram InlineKeyboardMarkup with remapped URLs.
        Buttons pointing into Telegram (another channel, bot or invite) are remapped to the destination channel,
        or dropped when the destination has no public link; buttons with unsupported URL schemes are dropped.
        """
        inline_keyboard: List[List[InlineKeyboardButton]] = []

        # 1. Process source buttons if present
        if source_buttons:
            for row in source_buttons:
                aiogram_row: List[InlineKeyboardButton] = []
                for btn in row:
                    text = btn.get("text", "").strip()
                    url = btn.get("url", "").strip()
                    if not text or not url:
                        continue

                    # Check if link points to another Telegram channel / group / bot
                    if block_competitor_links and cls.is_telegram_link(url):
                        if not target_channel_link:
                            logger.debug(f"Dropping competitor button '{text}' ({url}): destination has no public link")
                            continue
                        # Remap competitor channel link to our destination channel
                        url = target_channel_link

                    if not _ALLOWED_BUTTON_URL_RE.match(url):
                        logger.debug(f"Dropping button '{text}' with unsupported URL scheme")
                        continue

                    aiogram_row.append(InlineKeyboardButton(text=text, url=url))
                if aiogram_row:
                    inline_keyboard.append(aiogram_row)

        # 2. Append custom CTA buttons if configured
        if custom_cta_buttons:
            cta_row: List[InlineKeyboardButton] = []
            for cta in custom_cta_buttons:
                c_text = cta.get("text", "").strip()
                c_url = cta.get("url", "").strip()
                if c_text and c_url:
                    cta_row.append(InlineKeyboardButton(text=c_text, url=c_url))
            if cta_row:
                inline_keyboard.append(cta_row)

        if not inline_keyboard:
            return None

        return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

button_remapper = SmartButtonRemapper()
