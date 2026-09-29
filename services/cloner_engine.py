import asyncio
import time
import logging
import os
import re
import html
import io
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional, Union, Tuple, Dict, Any, Iterable
from aiogram import Bot
from aiogram.types import (
    FSInputFile,
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    InputPollOption,
    InlineKeyboardMarkup,
    Message as AiogramMessage
)
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramEntityTooLarge,
    TelegramNetworkError
)
from telethon.tl import types as tl_types
from telethon.tl.types import Message as TelethonMessage
from database.models import ChannelPair
from database.db_manager import db_manager, DatabaseManager
from services.text_processor import TextProcessor, truncate_utf16
from services.media_handler import media_handler
from services.translator_service import translator_service
from services.watermark_service import watermark_service
from services.video_watermark_service import video_watermark_service
from services.ai_paraphraser import ai_paraphraser
from services.dynamic_affiliate_engine import dynamic_affiliate_engine
from services.drip_feed_queue import drip_feed_service
from services.disaster_recovery import disaster_recovery_service
from services.affiliate_replacer import affiliate_replacer
from services.rate_limiter import rate_limiter
from services.cache_manager import cache_manager
from services.emoji_converter import emoji_converter
from services.fast_telethon import fast_telethon
from services.button_remapper import button_remapper
from services.ai_ad_detector import ai_ad_detector
from config.settings import PROJECT_ROOT
from services.custom_emojis import (
    ROCKET, SUCCESS, ERROR, DOCUMENT, LINK, CLEAN, TRANSLATE, IMAGE, STAR_SPARKLE
)

logger = logging.getLogger(__name__)

# Bot API uploads are limited to 50 MB (multipart overhead included); larger files need the Telethon userbot
BOT_API_UPLOAD_LIMIT_BYTES = 49 * 1024 * 1024
BOT_API_CAPTION_LIMIT = 1024
TELETHON_CAPTION_LIMIT = 2048
MESSAGE_TEXT_LIMIT = 4096
POLL_QUESTION_LIMIT = 300
POLL_OPTION_LIMIT = 100
POLL_MAX_OPTIONS = 12
POLL_EXPLANATION_LIMIT = 200
# Price extraction runs in a worker thread on the visible text only, capped in size and time
EXTRACT_PRICE_INPUT_CHARS = 1500
EXTRACT_PRICE_TIMEOUT = 5.0
# Posts the engine just published (loop prevention in the listener before the clone record exists)
RECENT_SENT_TTL_SECONDS = 120.0
RECENT_SENT_MAX_ITEMS = 2000
ASSETS_DIR = os.path.join(str(PROJECT_ROOT), "assets")
DEFAULT_LOGO_PATH = os.path.join(ASSETS_DIR, "logo.png")
# Media types sent from a downloaded file / media types a drip-feed queue item can carry
FILE_MEDIA_TYPES = ("photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation")
QUEUEABLE_MEDIA_TYPES = FILE_MEDIA_TYPES
PAID_TIERS = ("pro", "vip")

_TG_EMOJI_RE = re.compile(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', re.DOTALL | re.IGNORECASE)
_TG_EMOJI_TAG_RE = re.compile(r'</?tg-emoji\b[^>]*>', re.IGNORECASE)
_BARE_AMPERSAND_RE = re.compile(r'&(?!(?:[a-zA-Z0-9]+|#[0-9]+|#x[0-9a-fA-F]+);)')
_PRE_LANGUAGE_RE = re.compile(r'[\w+#.-]{1,32}')

_SIMPLE_ENTITY_TAGS = {
    tl_types.MessageEntityBold: ("<b>", "</b>"),
    tl_types.MessageEntityItalic: ("<i>", "</i>"),
    tl_types.MessageEntityUnderline: ("<u>", "</u>"),
    tl_types.MessageEntityStrike: ("<s>", "</s>"),
    tl_types.MessageEntitySpoiler: ("<tg-spoiler>", "</tg-spoiler>"),
    tl_types.MessageEntityCode: ("<code>", "</code>"),
}


def _entity_tags(entity: Any) -> Optional[Tuple[str, str]]:
    """Telegram-HTML open/close tags of a Telethon entity; None for entities Telegram detects on its own
    (plain URLs, @mentions, hashtags, ...)."""
    tags = _SIMPLE_ENTITY_TAGS.get(type(entity))
    if tags:
        return tags
    if isinstance(entity, tl_types.MessageEntityPre):
        language = (getattr(entity, "language", "") or "").strip()
        if language and _PRE_LANGUAGE_RE.fullmatch(language):
            return f'<pre><code class="language-{html.escape(language, quote=True)}">', "</code></pre>"
        return "<pre>", "</pre>"
    if isinstance(entity, tl_types.MessageEntityTextUrl):
        url = getattr(entity, "url", "") or ""
        return (f'<a href="{html.escape(url, quote=True)}">', "</a>") if url else None
    if isinstance(entity, tl_types.MessageEntityMentionName):
        return f'<a href="tg://user?id={int(entity.user_id)}">', "</a>"
    if isinstance(entity, tl_types.MessageEntityCustomEmoji):
        return f'<tg-emoji emoji-id="{int(entity.document_id)}">', "</tg-emoji>"
    if isinstance(entity, tl_types.MessageEntityBlockquote):
        return ("<blockquote expandable>" if getattr(entity, "collapsed", False) else "<blockquote>"), "</blockquote>"
    return None


def entities_to_html(text: str, entities: Optional[Iterable[Any]]) -> str:
    """Converts a Telegram message (text + entities, offsets in UTF-16 code units) to Telegram-HTML.

    Unlike telethon.extensions.html.unparse this keeps spoilers, emits <pre>/<code> without injected
    indentation or a literal "{}", escapes every text part (&, <, >) and properly nests overlapping entities."""
    if not text:
        return ""
    if not entities:
        return html.escape(text, quote=False)
    utf16 = text.encode("utf-16-le")
    total_units = len(utf16) // 2
    spans = []
    for idx, entity in enumerate(entities):
        tags = _entity_tags(entity)
        if not tags:
            continue
        start = max(0, int(getattr(entity, "offset", 0) or 0))
        end = min(total_units, start + max(0, int(getattr(entity, "length", 0) or 0)))
        if end > start:
            spans.append((start, end, idx, tags))
    if not spans:
        return html.escape(text, quote=False)

    def segment(a: int, b: int) -> str:
        return utf16[a * 2:b * 2].decode("utf-16-le", errors="replace")

    points = sorted({0, total_units, *(s[0] for s in spans), *(s[1] for s in spans)})
    starts = sorted(spans, key=lambda s: (s[0], -s[1], s[2]))
    open_stack: List[Tuple[int, int, int, Tuple[str, str]]] = []
    out: List[str] = []
    next_start = 0
    for i, pos in enumerate(points):
        if any(span[1] == pos for span in open_stack):
            reopen = []
            while any(span[1] == pos for span in open_stack):
                span = open_stack.pop()
                out.append(span[3][1])
                if span[1] != pos:
                    reopen.append(span)
            for span in reversed(reopen):
                out.append(span[3][0])
                open_stack.append(span)
        while next_start < len(starts) and starts[next_start][0] == pos:
            span = starts[next_start]
            next_start += 1
            out.append(span[3][0])
            open_stack.append(span)
        if i + 1 < len(points):
            out.append(html.escape(segment(pos, points[i + 1]), quote=False))
    while open_stack:
        out.append(open_stack.pop()[3][1])
    return "".join(out)


def extract_message_html(message: TelethonMessage) -> str:
    """Extracts rich-formatted, correctly escaped Telegram-HTML from a Telethon message (bold, italic, spoilers,
    code blocks, links, custom emojis). A message without entities is returned HTML-escaped."""
    if not message:
        return ""
    raw = getattr(message, "message", None)
    if not isinstance(raw, str):
        fallback = getattr(message, "text", None)
        raw = fallback if isinstance(fallback, str) else ""
    if not raw:
        return ""
    entities = getattr(message, "entities", None)
    if not isinstance(entities, (list, tuple)):
        entities = None
    try:
        return entities_to_html(raw, entities)
    except Exception:
        logger.debug("Entity to HTML conversion failed; using escaped plain text", exc_info=True)
        return html.escape(raw, quote=False)


def is_private_chat_target(target_chat_id: Any) -> bool:
    """
    Returns True if target_chat_id corresponds to a private Telegram user.
    Telegram channels and supergroups ALWAYS have negative IDs (< 0, e.g. -100...).
    Positive IDs (> 0) exclusively belong to private Telegram users.
    """
    if target_chat_id is None:
        return False
    # Check for Telethon / Aiogram User entity instances
    type_name = type(target_chat_id).__name__
    if "User" in type_name and not ("Chat" in type_name or "Channel" in type_name):
        return True
    if isinstance(target_chat_id, int):
        return target_chat_id > 0
    if isinstance(target_chat_id, str):
        s = target_chat_id.strip()
        if s.startswith("-"):
            return False
        if s.isdigit():
            return True
    return False


class RecentSentTargets:
    """(raw positive chat id, message id) of posts the engine published during the last RECENT_SENT_TTL_SECONDS.
    Filled right after every successful send and before the clone is recorded, so the listener can recognise the
    engine's own posts in chats that are both a destination and a source (loop prevention)."""

    def __init__(self, ttl: float = RECENT_SENT_TTL_SECONDS, max_items: int = RECENT_SENT_MAX_ITEMS):
        self._ttl = ttl
        self._max_items = max_items
        self._items: "OrderedDict[Tuple[int, int], float]" = OrderedDict()

    def _prune(self):
        now = time.monotonic()
        while self._items:
            key, added = next(iter(self._items.items()))
            if len(self._items) > self._max_items or now - added > self._ttl:
                self._items.popitem(last=False)
            else:
                break

    def add(self, chat_id: Any, message_id: Any):
        raw = DatabaseManager.normalize_peer_id(chat_id)
        if raw is None or not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            return
        key = (raw, message_id)
        self._items[key] = time.monotonic()
        self._items.move_to_end(key)
        self._prune()

    def __contains__(self, key: Any) -> bool:
        self._prune()
        try:
            chat_id, message_id = key
            return (int(chat_id), int(message_id)) in self._items
        except (TypeError, ValueError):
            return False

    def __len__(self) -> int:
        self._prune()
        return len(self._items)


@dataclass
class _SendContext:
    """Destination parameters shared by every send of one post."""
    pair: ChannelPair
    chat_id: Union[int, str]
    topic_id: Optional[int]
    silent: bool
    caption_above: bool

    def base_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"chat_id": self.chat_id}
        if self.topic_id:
            kwargs["message_thread_id"] = self.topic_id
        if self.silent:
            kwargs["disable_notification"] = True
        return kwargs


@dataclass
class _MediaItem:
    """One downloaded album item."""
    index: int
    message: Any
    path: str
    media_type: str
    size: int


class ClonerEngine:
    def __init__(self, bot: Optional[Bot] = None):
        self.bot = bot
        self._telethon_admin_required_targets: Dict[str, float] = {}
        # (pair_id, source_msg_id) keys currently being cloned. Real-time events, offline catch-up and
        # history cloning can race on the same message; the claim makes each message single-flight.
        self._inflight_messages: set = set()
        self.recent_sent_targets = RecentSentTargets()
        self._telethon_bot_account: Optional[Tuple[float, bool]] = None

    def _is_telethon_admin_required(self, target_key: str) -> bool:
        if target_key in self._telethon_admin_required_targets:
            if time.time() - self._telethon_admin_required_targets[target_key] < 300.0:
                return True
            self._telethon_admin_required_targets.pop(target_key, None)
        return False

    def clear_telethon_admin_required_targets(self):
        """Clears temporary admin permission blocklist during channel refresh"""
        self._telethon_admin_required_targets.clear()

    def set_bot(self, bot: Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_tg_emoji(text: Optional[str]) -> str:
        """Bot API (and unboosted channels) cannot use custom emoji: keep only the fallback emoji character."""
        if not text:
            return text or ""
        cleaned = _TG_EMOJI_RE.sub(r'\1', text)
        return _TG_EMOJI_TAG_RE.sub('', cleaned)

    @staticmethod
    async def _privileges(pair: ChannelPair) -> Tuple[Any, bool]:
        """(subscription, owner is an admin) of the pair's owner. The admin check uses the in-memory admin
        cache, so the per-post pipeline adds no database query for it."""
        sub = await db_manager.get_user_subscription(pair.user_id)
        return sub, bool(db_manager.is_admin_sync(pair.user_id))

    @staticmethod
    def _is_paid(sub: Any, is_admin: bool) -> bool:
        """Pro / VIP features (watermarks, affiliate rules, CTA buttons, AI rewrite): active paid plan or admin."""
        if is_admin:
            return True
        return bool(sub is not None and getattr(sub, "is_active", False) is True and getattr(sub, "tier", None) in PAID_TIERS)

    @staticmethod
    def _is_vip(sub: Any, is_admin: bool) -> bool:
        """Telegram Premium animated emojis are a VIP feature (or admin)."""
        if is_admin:
            return True
        return bool(sub is not None and getattr(sub, "is_active", False) is True and getattr(sub, "tier", None) == "vip")

    def _target_chat_id(self, pair: ChannelPair) -> Union[int, str]:
        return pair.target_id or self._normalize_chat_id(pair.target_channel or "")

    @staticmethod
    def _silent_now(pair: ChannelPair) -> bool:
        """Night "silent" mode: every message of the post is delivered without notification."""
        return bool(getattr(pair, "night_mode", "off") == "silent" and drip_feed_service.is_night_time())

    @staticmethod
    def _should_queue(pair: ChannelPair) -> bool:
        return bool((pair.drip_delay_minutes or 0) > 0 or (pair.night_mode == "buffer" and drip_feed_service.is_night_time()))

    def _context(self, pair: ChannelPair, silent: bool) -> _SendContext:
        return _SendContext(
            pair=pair,
            chat_id=self._target_chat_id(pair),
            topic_id=getattr(pair, "target_topic_id", None),
            silent=silent,
            caption_above=bool(getattr(pair, "show_caption_above", False))
        )

    @staticmethod
    async def _pace(ctx: _SendContext, cost: int = 1):
        await rate_limiter.wait_for_slot(ctx.pair.target_channel, peer_id=ctx.pair.target_id, cost=cost)

    @staticmethod
    def _file_size(path: Optional[str]) -> int:
        try:
            return os.path.getsize(path) if path else 0
        except OSError:
            return 0

    @staticmethod
    def _upload_timeout(size_bytes: int) -> int:
        """Request timeout for uploads: large files must not hit the default 60 s timeout (a timed-out upload may
        still be delivered, and retrying it would duplicate the post)."""
        size_mb = max(0, size_bytes) / (1024 * 1024)
        return int(min(600, max(60, 60 + size_mb * 6)))

    @staticmethod
    def _sent_message_id(sent: Any) -> Optional[int]:
        if sent is None:
            return None
        for attr in ("message_id", "id"):
            value = getattr(sent, attr, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    def _remember_sent(self, sent: Any, fallback_chat_id: Any):
        """Registers a delivered message in recent_sent_targets (before it is recorded in the database)."""
        msg_id = self._sent_message_id(sent)
        if not msg_id:
            return
        chat_id = None
        chat_obj_id = getattr(getattr(sent, "chat", None), "id", None)
        if isinstance(chat_obj_id, int) and not isinstance(chat_obj_id, bool):
            chat_id = chat_obj_id
        if chat_id is None:
            telethon_chat_id = getattr(sent, "chat_id", None)
            if isinstance(telethon_chat_id, int) and not isinstance(telethon_chat_id, bool):
                chat_id = telethon_chat_id
        self.recent_sent_targets.add(chat_id if chat_id is not None else fallback_chat_id, msg_id)

    @staticmethod
    def _bot_file_id(sent: Any) -> Optional[str]:
        """Bot API file_id of delivered media, for the disaster-recovery archive. Messages sent by the Telethon
        userbot carry no Bot API file_id (their photo is not even a list)."""
        if not isinstance(sent, AiogramMessage):
            return None
        if sent.photo:
            return sent.photo[-1].file_id
        for attr in ("video", "document", "audio", "voice", "animation", "sticker", "video_note"):
            media = getattr(sent, attr, None)
            if media is not None:
                return media.file_id
        return None

    @staticmethod
    def _markup_to_payload(markup: Optional[InlineKeyboardMarkup]) -> Optional[Dict[str, Any]]:
        if markup is None:
            return None
        try:
            return markup.model_dump(mode="json", exclude_none=True)
        except Exception:
            logger.debug("Could not serialise reply markup", exc_info=True)
            return None

    @staticmethod
    def _markup_from_payload(data: Any) -> Optional[InlineKeyboardMarkup]:
        if not isinstance(data, dict):
            return None
        try:
            return InlineKeyboardMarkup.model_validate(data)
        except Exception:
            logger.debug("Could not restore queued reply markup", exc_info=True)
            return None

    @staticmethod
    def _source_document(message: Any) -> Optional[Any]:
        doc = getattr(getattr(message, "media", None), "document", None) if message is not None else None
        return doc if isinstance(doc, tl_types.Document) else None

    @classmethod
    def _source_document_meta(cls, message: Any) -> Tuple[Optional[List[Any]], Optional[str]]:
        """Attributes (duration, dimensions, voice/round flags, file name) and mime type of the source document."""
        doc = cls._source_document(message)
        if doc is None:
            return None, None
        attrs = [a for a in (doc.attributes or []) if not isinstance(a, tl_types.DocumentAttributeSticker)]
        return (attrs or None), (getattr(doc, "mime_type", None) or None)

    @classmethod
    def _source_attribute(cls, message: Any, kind: Any) -> Optional[Any]:
        doc = cls._source_document(message)
        if doc is None:
            return None
        for attr in doc.attributes or []:
            if isinstance(attr, kind):
                return attr
        return None

    @classmethod
    def _source_duration(cls, message: Any) -> Optional[float]:
        for kind in (tl_types.DocumentAttributeVideo, tl_types.DocumentAttributeAudio):
            attr = cls._source_attribute(message, kind)
            duration = getattr(attr, "duration", None) if attr is not None else None
            if isinstance(duration, (int, float)) and duration > 0:
                return float(duration)
        return None

    @classmethod
    async def _source_thumb(cls, message: Any, media_type: str) -> Optional[bytes]:
        """Largest thumbnail of the source video/document, so Telethon uploads keep a proper preview."""
        if media_type not in ("video", "animation", "document", "audio") or message is None:
            return None
        doc = cls._source_document(message)
        if doc is None or not getattr(doc, "thumbs", None):
            return None
        try:
            data = await asyncio.wait_for(message.download_media(file=bytes, thumb=-1), timeout=20.0)
            return data if isinstance(data, (bytes, bytearray)) and data else None
        except Exception:
            logger.debug("Source thumbnail unavailable", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Watermark helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _watermark_fallback_text(pair: ChannelPair) -> Optional[str]:
        """Destination name used when no watermark text is set: the target title or its @username. Never the
        signature (HTML with custom emoji tags) and never a raw -100... id."""
        title = TextProcessor.html_to_plain(pair.target_title or "").strip()
        if title and not re.fullmatch(r'-?\d+', title) and not title.lower().startswith(("http://", "https://", "t.me/", "+")):
            return title
        normalized = TextProcessor.normalize_channel_input(pair.target_channel or "")
        if normalized.startswith("@"):
            return normalized
        return None

    def _image_watermark_text(self, pair: ChannelPair) -> Optional[str]:
        text = TextProcessor.html_to_plain(pair.image_watermark_text or "").strip()
        return text or self._watermark_fallback_text(pair)

    def _video_watermark_text(self, pair: ChannelPair) -> Optional[str]:
        text = (
            TextProcessor.html_to_plain(pair.video_watermark_text or "").strip()
            or TextProcessor.html_to_plain(pair.image_watermark_text or "").strip()
        )
        return text or self._watermark_fallback_text(pair)

    @staticmethod
    def _resolve_logo_path(user_value: Optional[str]) -> Optional[str]:
        """Logo watermarks only ever read image files inside the bundled assets/ directory (default
        assets/logo.png). The user-editable watermark text is never used as an arbitrary file path."""
        assets_root = os.path.realpath(ASSETS_DIR)
        candidates = []
        value = (user_value or "").strip()
        if value and "://" not in value:
            rel = value.replace("\\", "/")
            if rel.lower().startswith("assets/"):
                rel = rel[len("assets/"):]
            candidates.append(os.path.join(assets_root, rel))
        candidates.append(DEFAULT_LOGO_PATH)
        for candidate in candidates:
            real = os.path.realpath(candidate)
            try:
                inside = os.path.commonpath([real, assets_root]) == assets_root
            except ValueError:
                inside = False
            if inside and os.path.isfile(real) and real.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                return real
        return None

    @staticmethod
    def _provenance_payload(pair: ChannelPair) -> Optional[str]:
        try:
            from services.steganography_service import steganography_service
            return steganography_service.create_provenance_payload(pair.target_id or pair.target_channel, pair.user_id)
        except Exception:
            logger.debug("Invisible watermark unavailable", exc_info=True)
            return None

    async def _invisible_watermark_file(self, path: str, pair: ChannelPair) -> str:
        """Embeds the invisible provenance watermark (enable_invisible_watermark) into a photo file in place."""
        if not getattr(pair, "enable_invisible_watermark", False) or not path.lower().endswith((".jpg", ".jpeg", ".png")):
            return path
        payload = self._provenance_payload(pair)
        if not payload:
            return path
        try:
            from services.steganography_service import steganography_service
            await asyncio.to_thread(steganography_service.embed_watermark, path, path, payload)
        except Exception:
            logger.debug("Invisible watermark embedding failed", exc_info=True)
        return path

    async def _watermark_photo_bytes(self, data: bytes, pair: ChannelPair, paid: bool) -> bytes:
        if paid and pair.image_watermark_type == "text":
            wm_text = self._image_watermark_text(pair)
            if wm_text:
                wm_bytes = await asyncio.to_thread(
                    watermark_service.apply_text_watermark_bytes,
                    data,
                    wm_text,
                    pair.image_watermark_pos or "bottom_right"
                )
                if wm_bytes:
                    data = wm_bytes
        if getattr(pair, "enable_invisible_watermark", False):
            payload = self._provenance_payload(pair)
            if payload:
                try:
                    from services.steganography_service import steganography_service
                    embedded = await asyncio.to_thread(steganography_service.embed_watermark_bytes, data, payload)
                    if embedded:
                        data = embedded
                except Exception:
                    logger.debug("Invisible watermark embedding failed", exc_info=True)
        return data

    async def _watermark_photo_file(self, path: str, pair: ChannelPair, paid: bool, files: List[str]) -> str:
        """Visible image watermark (Pro/VIP) and invisible watermark for a downloaded photo file."""
        if paid and pair.image_watermark_type != "none":
            out = None
            if pair.image_watermark_type == "logo":
                logo_path = self._resolve_logo_path(pair.image_watermark_text)
                if logo_path:
                    out = await asyncio.to_thread(
                        watermark_service.apply_logo_watermark,
                        path,
                        logo_path,
                        pair.image_watermark_pos or "bottom_right"
                    )
            else:
                wm_text = self._image_watermark_text(pair)
                if wm_text:
                    out = await asyncio.to_thread(
                        watermark_service.apply_text_watermark,
                        path,
                        wm_text,
                        pair.image_watermark_pos or "bottom_right"
                    )
            if out and out != path and os.path.exists(out):
                self._track(files, out)
                path = out
        return await self._invisible_watermark_file(path, pair)

    async def _watermark_video_file(self, path: str, pair: ChannelPair, paid: bool, files: List[str], source_message: Any) -> str:
        """Video watermark (Pro/VIP only). Returns the watermarked file or the original path."""
        if pair.video_watermark_type == "none" or not paid:
            return path
        duration = self._source_duration(source_message)
        out = None
        if pair.video_watermark_type == "logo":
            logo_path = self._resolve_logo_path(pair.video_watermark_text)
            if logo_path:
                out = await video_watermark_service.apply_video_logo_watermark(
                    input_video_path=path,
                    logo_image_path=logo_path,
                    pos=pair.video_watermark_pos or "bottom_right",
                    duration=duration
                )
        else:
            wm_text = self._video_watermark_text(pair)
            if wm_text:
                out = await video_watermark_service.apply_video_text_watermark(
                    input_video_path=path,
                    watermark_text=wm_text,
                    pos=pair.video_watermark_pos or "bottom_right",
                    duration=duration
                )
        if out and os.path.exists(out):
            self._track(files, out)
            return out
        return path

    @staticmethod
    def _track(files: List[str], path: Optional[str]):
        """Adds a temp file to the job's cleanup list and protects it from the background cleaner meanwhile."""
        if path and path not in files:
            files.append(path)
            media_handler.hold_path(path)

    # ------------------------------------------------------------------
    # Test post
    # ------------------------------------------------------------------

    async def send_test_post(self, pair: ChannelPair) -> Tuple[bool, str]:
        """Sends a verification test message to the target channel with animated emojis"""
        if not self.bot:
            return False, "Bot ishga tushmagan!"

        target_chat_id = self._target_chat_id(pair)
        if is_private_chat_target(target_chat_id):
            return False, "Xatolik: Shaxsiy profilga test xabari yuborib bo'lmaydi! Maqsad faqat kanal yoki superguruh bo'lishi shart."

        clean_status = f"{SUCCESS} Yoqilgan" if pair.clean_links else f"{ERROR} O'chirilgan"
        trans_status = f"{SUCCESS} {html.escape((pair.target_lang or 'uz').upper())}" if pair.auto_translate else f"{ERROR} O'chirilgan"
        wm_status = html.escape(pair.image_watermark_text) if pair.image_watermark_text else (f"{SUCCESS} Yoqilgan" if pair.image_watermark_type != "none" else f"{ERROR} O'chirilgan")
        emoji_status = f"{SUCCESS} Yoqilgan (VIP)" if pair.auto_premium_emojis else f"{ERROR} O'chirilgan"

        src_title = html.escape(pair.source_title or pair.source_channel or "")
        tgt_title = html.escape(pair.target_title or pair.target_channel or "")
        src_channel = html.escape(str(pair.source_channel or ""))
        tgt_channel = html.escape(str(pair.target_channel or ""))

        sample_text = f"""
{ROCKET} <b>Telegram Kloner — Test Xabari!</b>

Kanalingiz botga muvaffaqiyatli ulandi va sozlamalar tekshirildi.

{DOCUMENT} <b>Juftlik ma'lumotlari:</b>
├ {LINK} <b>Manba kanal:</b> {src_title} (<code>{src_channel}</code>)
├ {LINK} <b>Maqsadli kanal:</b> {tgt_title} (<code>{tgt_channel}</code>)
├ {CLEAN} <b>Reklama tozalash:</b> {clean_status}
├ {TRANSLATE} <b>Avto-Tarjima:</b> {trans_status}
├ {IMAGE} <b>Suv belgisi (Watermark):</b> {wm_status}
└ {STAR_SPARKLE} <b>Telegram Premium Emojilar:</b> {emoji_status}

<i>Endi manba kanaldagi yangi xabarlar to'g'ridan-to'g'ri shu yerga nusxalanadi!</i>
"""
        # The owner's own signature is attached exactly like on real posts (remove_signature only strips the
        # SOURCE channel's signature and has nothing to do with it)
        if pair.custom_signature:
            sample_text = TextProcessor.attach_signature(sample_text, pair.custom_signature)

        topic_id = getattr(pair, "target_topic_id", None)
        sent = None
        if pair.auto_premium_emojis:
            try:
                sub, is_admin = await self._privileges(pair)
                if self._is_vip(sub, is_admin):
                    sample_text = emoji_converter.convert_to_premium_emojis(sample_text)
            except Exception:
                logger.debug("Subscription lookup for the test post failed", exc_info=True)
            # Try Telethon first for full VIP custom emoji support
            sent = await self._telethon_send_post(
                target_chat_id=target_chat_id,
                media_type="text",
                caption=sample_text,
                topic_id=topic_id
            )

        if not sent:
            clean_sample_text = self._strip_tg_emoji(sample_text)
            send_kw: Dict[str, Any] = {
                "chat_id": target_chat_id,
                "text": clean_sample_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            if topic_id:
                send_kw["message_thread_id"] = topic_id
            try:
                sent = await self._send_with_retry(self.bot.send_message, **send_kw)
            except TelegramBadRequest as e:
                return False, f"Xatolik: Bot kanalda administrator emas yoki ruxsat yetarli emas! ({html.escape(str(e))})"
            except Exception as e:
                return False, f"Xatolik: {html.escape(str(e))}"

        if hasattr(sent, 'id') and isinstance(sent.id, int):
            msg_id = sent.id
        elif hasattr(sent, 'message_id') and isinstance(sent.message_id, int):
            msg_id = sent.message_id
        else:
            msg_id = getattr(sent, 'message_id', None) or getattr(sent, 'id', 'OK')
        return True, f"Test xabari {tgt_title} kanaliga muvaffaqiyatli yuborildi! (ID: {msg_id})"

    # ------------------------------------------------------------------
    # Text pipeline
    # ------------------------------------------------------------------

    async def process_post_text(self, raw_text: str, pair: ChannelPair, has_media: bool = False) -> Optional[str]:
        """Builds the text to publish.

        Returns None when the post must be skipped (blacklisted words, or an advertisement without media).
        For a media post an advertisement only removes the caption: "" is returned and the media is still cloned.
        Pro/VIP features (affiliate rules, AI rewrite) and VIP premium emojis are applied only for entitled owners."""
        if raw_text is None:
            raw_text = ""

        if pair.blacklist_list and TextProcessor.contains_blacklisted_words(raw_text, pair.blacklist_list):
            return None

        dropped: Optional[str] = "" if has_media else None
        current_text = raw_text
        privileges: Optional[Tuple[Any, bool]] = None

        async def _get_privileges() -> Tuple[Any, bool]:
            nonlocal privileges
            if privileges is None:
                privileges = await self._privileges(pair)
            return privileges

        # Affiliate rules (Pro/VIP): rewritten links are replaced by placeholders so no later step can alter them
        protected_aff_urls: Dict[str, str] = {}
        if pair.affiliate_rules and current_text.strip() and self._is_paid(*(await _get_privileges())):
            current_text, protected_aff_urls = affiliate_replacer.replace_and_protect(current_text, pair.affiliate_rules)

        # Link & commercial-ad cleaning: controlled by the pair's "Link Tozalash" toggle only
        if pair.clean_links and current_text.strip():
            is_ad, cleaned = await asyncio.to_thread(TextProcessor.clean_for_clone, current_text)
            if is_ad:
                logger.info(f"Commercial advertisement {'caption dropped' if has_media else 'blocked'} for pair #{pair.id}: {TextProcessor.html_to_plain(current_text)[:60]}...")
                return dropped
            current_text = cleaned

        # AI Semantic Ad & Casino Shield
        ad_action = getattr(pair, "ad_action", "clean") or "clean"
        if ad_action != "off" and current_text.strip():
            should_pub, current_text = await ai_ad_detector.process_ad_action(
                current_text,
                action=ad_action,
                swap_signature=pair.custom_signature
            )
            if not should_pub or not current_text:
                logger.info(f"AI Ad Shield {'dropped the caption of' if has_media else 'dropped'} a commercial/casino post for pair #{pair.id}")
                return dropped

        # The source's own signature is part of the source text: strip it before rewriting/translating
        if pair.remove_signature and current_text:
            current_text = TextProcessor.strip_source_signature(current_text)

        if pair.replace_dict and current_text.strip():
            current_text = TextProcessor.apply_word_replacements(current_text, pair.replace_dict)

        if pair.auto_translate and current_text.strip():
            try:
                async with cache_manager.translate_semaphore:
                    current_text = await translator_service.translate_text(
                        current_text,
                        target_lang=pair.target_lang or "uz",
                        source_lang=pair.source_lang or "auto"
                    )
            except Exception as e:
                logger.error(f"Auto-translation failed: {e}")

        # AI Paraphraser & Tone Shifter (Pro/VIP exclusive with active status)
        active_tone = pair.tone_of_voice if getattr(pair, "tone_of_voice", "standard") not in ["standard", "off", None] else pair.ai_paraphrase_mode
        if active_tone and active_tone != "off" and current_text.strip() and self._is_paid(*(await _get_privileges())):
            try:
                current_text = await ai_paraphraser.paraphrase_async(current_text, mode=active_tone)
            except Exception as e_para:
                logger.warning(f"AI paraphrasing notice for pair #{pair.id}: {e_para}")

        if pair.custom_signature and current_text:
            current_text = TextProcessor.attach_signature(current_text, pair.custom_signature)

        # VIP: Convert standard Unicode emojis into animated Telegram Premium custom emojis
        if pair.auto_premium_emojis and current_text.strip() and self._is_vip(*(await _get_privileges())):
            current_text = emoji_converter.convert_to_premium_emojis(current_text)

        # Protected affiliate links are restored last
        if protected_aff_urls:
            current_text = affiliate_replacer.restore_placeholders(current_text, protected_aff_urls)

        return current_text

    @staticmethod
    def _smart_fit_caption_with_badge(old_cap: str, badge: str, max_len: int = 1024) -> str:
        """Appends a badge to an existing caption. The old caption is shortened on a tag-safe boundary so the
        visible length (UTF-16, as Telegram counts it) of the result stays within max_len."""
        old_cap = old_cap or ""
        badge_len = TextProcessor.get_visible_text_length(badge)
        if TextProcessor.get_visible_text_length(old_cap) + badge_len <= max_len:
            return TextProcessor.ensure_closed_tags(old_cap) + badge
        allowed_old = max_len - badge_len - 3
        if allowed_old > 0:
            fitted, _overflow = TextProcessor.fit_caption_limit(old_cap, max_limit=allowed_old)
            return fitted.rstrip() + "..." + badge
        return truncate_utf16(TextProcessor.html_to_plain(badge), max_len)

    @staticmethod
    def _is_self_loop(pair: ChannelPair) -> bool:
        s_norm = db_manager._normalize_channel_name(pair.source_channel)
        t_norm = db_manager._normalize_channel_name(pair.target_channel)
        s_id_str = str(pair.source_id).replace("-100", "").lstrip("-") if pair.source_id else ""
        t_id_str = str(pair.target_id).replace("-100", "").lstrip("-") if pair.target_id else ""
        if s_id_str and t_id_str and s_id_str == t_id_str:
            return True
        if s_norm and t_norm and s_norm == t_norm:
            return True
        if (s_id_str and s_id_str == t_norm) or (t_id_str and t_id_str == s_norm):
            return True
        return False

    def _claim_messages(self, pair_id: int, message_ids: List[int]) -> Optional[List[tuple]]:
        keys = [(pair_id, mid) for mid in message_ids]
        if any(k in self._inflight_messages for k in keys):
            return None
        self._inflight_messages.update(keys)
        return keys

    # ------------------------------------------------------------------
    # Clone records
    # ------------------------------------------------------------------

    async def _record_skip(self, pair: ChannelPair, message_ids: List[int], media_type: str, status: str):
        """Marks source posts that were intentionally not re-posted (visual duplicates / price-drop updates,
        blacklisted or advertisement posts) as processed, so catch-up and history cloning skip them."""
        for mid in message_ids:
            try:
                await db_manager.record_cloned_message(
                    pair_id=pair.id,
                    source_msg_id=mid,
                    target_msg_id=None,
                    media_type=media_type,
                    source_channel=pair.source_channel,
                    target_channel=pair.target_channel,
                    status=status
                )
            except Exception:
                logger.debug(f"Could not record {status} skip", exc_info=True)

    async def _record_delivery(
        self,
        pair: ChannelPair,
        source_msg_id: int,
        target_msg_id: Optional[int],
        media_type: str,
        caption: Optional[str],
        price: Optional[float] = None,
        media_group_id: Optional[str] = None
    ):
        """Records a published clone. target_msg_id is always the primary (content) message and caption the
        text actually shown on it (without custom emoji tags), which is what edit/sold sync edits later."""
        await db_manager.record_cloned_message(
            pair_id=pair.id,
            source_msg_id=source_msg_id,
            target_msg_id=target_msg_id,
            media_group_id=media_group_id,
            media_type=media_type,
            source_channel=pair.source_channel,
            target_channel=pair.target_channel,
            price=float(price or 0.0),
            last_caption=self._strip_tg_emoji(caption) if caption else None
        )

    async def _archive_backup(
        self,
        pair: ChannelPair,
        message_id: int,
        text: str,
        media_type: str,
        file_id: Optional[str],
        media_group_id: Optional[str] = None
    ):
        """Disaster-recovery archive; failures never turn a delivered post into a failed clone."""
        if not pair.backup_enabled:
            return
        try:
            kwargs: Dict[str, Any] = {
                "pair_id": pair.id,
                "source_id": pair.source_id,
                "message_id": message_id,
                "text": text or "",
                "media_type": media_type,
                "media_file_id": file_id
            }
            if media_group_id:
                kwargs["media_group_id"] = media_group_id
            await disaster_recovery_service.archive_message(**kwargs)
        except Exception as e:
            logger.warning(f"Backup archive of message {message_id} for pair #{pair.id} failed: {e}")

    # ------------------------------------------------------------------
    # Listing duplicates & price drops
    # ------------------------------------------------------------------

    async def _extract_price(self, html_text: Optional[str]) -> Optional[float]:
        """Listing price from the visible text, parsed in a worker thread (never on the event loop)."""
        plain = TextProcessor.html_to_plain(html_text or "").strip()
        if not plain:
            return None
        try:
            from services.story_cloner_service import story_cloner_service
            return await asyncio.wait_for(
                asyncio.to_thread(story_cloner_service.extract_price, plain[:EXTRACT_PRICE_INPUT_CHARS]),
                timeout=EXTRACT_PRICE_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(f"Price extraction exceeded {EXTRACT_PRICE_TIMEOUT:.0f}s; continuing without a price")
            return None
        except Exception:
            logger.debug("Price extraction failed", exc_info=True)
            return None

    async def _check_listing(
        self,
        pair: ChannelPair,
        hashes: List[str],
        price: Optional[float],
        first_msg_id: int,
        source_ids: List[int],
        media_type: str,
        require_price: bool
    ) -> Optional[Tuple[bool, Dict[str, Any]]]:
        """pHash duplicate check. Check and save run under the owner's lock, so two posts with the same photos
        processed at the same time cannot both pass. Returns (is_price_drop, match_info) when the post is a
        duplicate listing (already recorded as a skip), otherwise None."""
        hashes = [h for h in hashes if h]
        if not hashes:
            return None
        from services.image_hasher import image_hasher
        async with image_hasher.listing_lock(pair.user_id):
            is_dup, is_price_drop, match_info = await image_hasher.check_listing_duplicate(
                hashes,
                current_price=price,
                pair_id=pair.id,
                source_channel=pair.source_channel,
                source_msg_id=first_msg_id,
                user_id=pair.user_id
            )
            if is_dup and match_info and (price or not require_price):
                await self._record_skip(pair, source_ids, media_type, "duplicate")
                await image_hasher.update_listing_price(match_info, price)
                return is_price_drop, match_info
            if not is_dup:
                await image_hasher.save_listing_hashes(
                    hashes=hashes,
                    source_channel=pair.source_channel,
                    source_msg_id=first_msg_id,
                    pair_id=pair.id,
                    price=price
                )
        return None

    @staticmethod
    def _record_edit_chat_id(rec: Dict[str, Any]) -> Union[int, str, None]:
        """Chat of a clone record for Bot API edits: the pair's numeric target id, else a public @username or -100
        id stored as target (never an invite link, which the Bot API cannot address)."""
        raw = DatabaseManager.normalize_peer_id(rec.get("pair_target_id"))
        if raw:
            return int(f"-100{raw}")
        for ref in (rec.get("target_channel"), rec.get("pair_target_channel")):
            normalized = TextProcessor.normalize_channel_input(str(ref or ""))
            if normalized.startswith("@"):
                return normalized
            if re.fullmatch(r'-100\d+', normalized):
                return int(normalized)
        return None

    async def _matched_listing_records(self, pair: ChannelPair, match_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Published clones of the matched listing that belong to the same owner and destination as `pair`."""
        matched_msg_id = match_info.get("matched_msg_id")
        if not matched_msg_id:
            return []
        matched_pair = None
        if match_info.get("matched_pair_id"):
            try:
                matched_pair = await db_manager.get_pair_by_id(match_info["matched_pair_id"])
            except Exception:
                matched_pair = None
        channel = str(match_info.get("matched_channel") or "")
        peer_id = (matched_pair.source_id if matched_pair and matched_pair.source_id else None) or DatabaseManager.normalize_peer_id(channel)
        name = TextProcessor.normalize_channel_input((matched_pair.source_channel if matched_pair else channel) or "")
        username = name[1:] if name.startswith("@") else None
        rows = await db_manager.get_cloned_messages_for_source(matched_msg_id, peer_id=peer_id, username=username)
        target_key = DatabaseManager.pair_endpoint_key(pair.target_channel, pair.target_id)
        matched = []
        for rec in rows:
            if rec.get("pair_user_id") != pair.user_id or not rec.get("pair_is_active"):
                continue
            if not rec.get("target_msg_id") or rec.get("status") == "sold":
                continue
            if DatabaseManager.pair_endpoint_key(rec.get("pair_target_channel"), rec.get("pair_target_id")) != target_key:
                continue
            matched.append(rec)
        return matched

    async def _apply_price_drop(self, pair: ChannelPair, match_info: Dict[str, Any], new_price: float, markup: Optional[InlineKeyboardMarkup]):
        """Adds a price-drop badge to the already published listing (same owner and destination only)."""
        prev_price = float(match_info.get("previous_price") or 0.0)
        logger.info(f"🔥 PRICE DROP DETECTED for pair #{pair.id}: ${prev_price:,.0f} -> ${new_price:,.0f}")
        badge = f"\n\n🔥 <b>NARX ARZONLASHDI:</b> <s>${prev_price:,.0f}</s> ➡️ <b>${new_price:,.0f}</b>"
        try:
            records = await self._matched_listing_records(pair, match_info)
        except Exception:
            logger.debug("Price-drop record lookup failed", exc_info=True)
            return
        for rec in records:
            chat_id = self._record_edit_chat_id(rec)
            if chat_id is None:
                continue
            new_cap = self._smart_fit_caption_with_badge(self._strip_tg_emoji(rec.get("last_caption") or ""), badge)
            edit_kw: Dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": rec["target_msg_id"],
                "caption": new_cap,
                "parse_mode": "HTML"
            }
            # Editing without reply_markup would remove the post's buttons; re-pass the current ones when known
            if markup is not None:
                edit_kw["reply_markup"] = markup
            if getattr(pair, "show_caption_above", False):
                edit_kw["show_caption_above_media"] = True
            try:
                await self.bot.edit_message_caption(**edit_kw)
                await db_manager.update_cloned_message_price(rec["id"], new_price)
                await db_manager.update_cloned_message_caption(rec["id"], new_cap)
            except Exception as pe:
                logger.debug(f"Could not update price drop caption: {pe}")

    # ------------------------------------------------------------------
    # Single messages
    # ------------------------------------------------------------------

    async def clone_single_message(self, message: TelethonMessage, pair: ChannelPair) -> bool:
        keys = self._claim_messages(pair.id, [message.id])
        if keys is None:
            logger.debug(f"Message {message.id} for pair #{pair.id} is already being cloned. Skipping concurrent run.")
            return False
        try:
            return await self._clone_single_message(message, pair)
        finally:
            self._inflight_messages.difference_update(keys)

    def _build_markup(
        self,
        message: Any,
        pair: ChannelPair,
        processed_text: Optional[str],
        paid: bool
    ) -> Tuple[Optional[InlineKeyboardMarkup], Optional[List[Any]]]:
        """CTA buttons (Pro/VIP) + remapped source buttons, and the same URL buttons for Telethon."""
        cta_markup = None
        telethon_buttons = None
        if pair.auto_cta_buttons and processed_text and paid:
            # Affiliate rules were already applied to the text by process_post_text; they must not be applied twice
            _text, cta_links = dynamic_affiliate_engine.extract_and_convert_links(processed_text, "")
            # Custom emoji button icons are not allowed in channel posts
            cta_markup = dynamic_affiliate_engine.build_cta_keyboard(cta_links, with_icons=False)
            telethon_buttons = dynamic_affiliate_engine.build_telethon_buttons(cta_links)

        source_buttons = button_remapper.extract_telethon_buttons(message)
        if source_buttons:
            target_name = TextProcessor.normalize_channel_input(pair.target_channel or "")
            target_link = f"https://t.me/{target_name[1:]}" if target_name.startswith("@") else None
            remapped_kb = button_remapper.build_remapped_markup(
                source_buttons=source_buttons,
                target_channel_link=target_link,
                block_competitor_links=True
            )
            if remapped_kb:
                if cta_markup and hasattr(cta_markup, "inline_keyboard"):
                    cta_markup = InlineKeyboardMarkup(inline_keyboard=remapped_kb.inline_keyboard + cta_markup.inline_keyboard)
                else:
                    cta_markup = remapped_kb

        # Telethon can also send the buttons (bot accounts only, see _telethon_send_post)
        if cta_markup and not telethon_buttons:
            try:
                from telethon import Button
                tb_grid = []
                for row in getattr(cta_markup, "inline_keyboard", []):
                    row_btns = [Button.url(text=b.text, url=b.url) for b in row if getattr(b, "url", None)]
                    if row_btns:
                        tb_grid.append(row_btns)
                if tb_grid:
                    telethon_buttons = tb_grid
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        return cta_markup, telethon_buttons

    async def _clone_single_message(self, message: TelethonMessage, pair: ChannelPair) -> bool:
        if not self.bot:
            logger.error("Bot instance not set in ClonerEngine.")
            return False

        if not pair.is_active:
            return False

        # Safeguard against self-cloning loops
        if self._is_self_loop(pair):
            logger.error(f"Infinite loop detected: pair #{pair.id} has identical source and target ({pair.source_channel}). Skipping.")
            return False

        # Verify active subscription / 14-day trial for channel owner
        sub, is_admin = await self._privileges(pair)
        if not is_admin and not sub.is_active:
            logger.warning(f"Subscription or 14-day trial expired for user {pair.user_id}. Skipping post {message.id} for pair #{pair.id}")
            return False

        if await db_manager.is_message_cloned(pair.id, message.id):
            logger.debug(f"Message {message.id} already cloned for pair {pair.id}. Skipping.")
            return False

        target_chat_id = self._target_chat_id(pair)
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Pair #{pair.id} has private user target {target_chat_id}! Aborting clone.")
            return False

        files_to_cleanup: List[str] = []
        try:
            media_type = media_handler.get_media_type(message)
            has_media = media_type in FILE_MEDIA_TYPES
            raw_text = extract_message_html(message)
            processed_text = await self.process_post_text(raw_text, pair, has_media=has_media)

            if processed_text is None:
                logger.info(f"Message {message.id} filtered (blacklist / advertisement) for pair {pair.id}.")
                await self._record_skip(pair, [message.id], media_type, "filtered")
                return False

            paid = self._is_paid(sub, is_admin)
            premium_emojis = bool(pair.auto_premium_emojis and self._is_vip(sub, is_admin))
            cta_markup, telethon_buttons = self._build_markup(message, pair, processed_text, paid)
            ctx = self._context(pair, self._silent_now(pair))

            # Drip Feed / Night Buffer queueing
            if self._should_queue(pair):
                if media_type == "text" or media_type in QUEUEABLE_MEDIA_TYPES:
                    return await self._enqueue_single(message, pair, media_type, processed_text, cta_markup, paid, files_to_cleanup)
                logger.info(f"Message {message.id} ({media_type}) cannot be queued; publishing it immediately.")

            if media_type == "text":
                if not processed_text or not processed_text.strip():
                    logger.debug("Empty text message after cleaning. Skipping.")
                    await self._record_skip(pair, [message.id], media_type, "filtered")
                    return False
                return await self._clone_text_post(message, ctx, processed_text, cta_markup, telethon_buttons, premium_emojis)

            if has_media:
                return await self._clone_media_post(
                    message, ctx, media_type, processed_text or "", cta_markup, telethon_buttons, paid, premium_emojis, files_to_cleanup
                )

            sent = None
            if media_type == "poll":
                await self._pace(ctx)
                sent = await self._send_poll(message, ctx)
            elif media_type == "contact" and message.media:
                await self._pace(ctx)
                sent = await self._send_contact(message, ctx)
            elif media_type == "location" and message.media:
                await self._pace(ctx)
                sent = await self._send_location(message, ctx)
            elif processed_text:
                # Unknown media with text: publish the text
                return await self._clone_text_post(message, ctx, processed_text, cta_markup, telethon_buttons, premium_emojis)

            if not sent:
                logger.error(f"Failed to clone message {message.id} ({media_type}) to {target_chat_id} - delivery failed")
                return False
            self._remember_sent(sent, ctx.chat_id)
            target_id = self._sent_message_id(sent)
            await self._record_delivery(pair, message.id, target_id, media_type, None)
            await self._archive_backup(pair, message.id, processed_text or "", media_type, None)
            logger.info(f"Successfully cloned message {message.id} -> {target_chat_id} (msg {target_id})")
            return True

        except Exception as e:
            logger.error(f"Error cloning message {message.id} to {target_chat_id}: {e}", exc_info=True)
            return False
        finally:
            if files_to_cleanup:
                await media_handler.cleanup_files(files_to_cleanup)

    async def _send_text_chunk(self, ctx: _SendContext, chunk: str, markup: Optional[InlineKeyboardMarkup],
                               telethon_buttons: Optional[List[Any]], premium_emojis: bool, bot: Optional[Bot] = None) -> Tuple[Any, str]:
        """Sends one text message; returns (sent_message, html_actually_shown)."""
        sent = None
        if premium_emojis:
            sent = await self._telethon_send_post(
                target_chat_id=ctx.chat_id,
                media_type="text",
                file_path=None,
                caption=chunk,
                buttons=telethon_buttons,
                topic_id=ctx.topic_id,
                silent=ctx.silent
            )
            if sent:
                return sent, chunk
        clean_chunk = self._strip_tg_emoji(chunk)
        if not clean_chunk.strip():
            return None, ""
        sent = await self._send_with_retry(
            (bot or self.bot).send_message,
            **ctx.base_kwargs(),
            text=clean_chunk,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=False
        )
        return sent, clean_chunk

    async def _clone_text_post(self, message: Any, ctx: _SendContext, processed_text: str,
                               cta_markup: Optional[InlineKeyboardMarkup], telethon_buttons: Optional[List[Any]],
                               premium_emojis: bool) -> bool:
        pair = ctx.pair
        text_chunks = [c for c in TextProcessor.fit_text_limit(processed_text, max_limit=MESSAGE_TEXT_LIMIT) if c.strip()]
        primary = None
        primary_text = None
        for chunk_i, chunk_text in enumerate(text_chunks):
            last = chunk_i == len(text_chunks) - 1
            await self._pace(ctx)
            sent, shown = await self._send_text_chunk(
                ctx, chunk_text, cta_markup if last else None, telethon_buttons if last else None, premium_emojis
            )
            if not sent:
                if primary is None:
                    logger.error(f"Failed to clone text message {message.id} to {ctx.chat_id}")
                    return False
                logger.warning(f"Text message {message.id}: chunk {chunk_i + 1}/{len(text_chunks)} could not be delivered")
                break
            self._remember_sent(sent, ctx.chat_id)
            if primary is None:
                primary, primary_text = sent, shown
        if primary is None:
            return False
        target_id = self._sent_message_id(primary)
        # The first chunk is the primary (content) message; its text is what later edits replace
        await self._record_delivery(pair, message.id, target_id, "text", primary_text)
        await self._archive_backup(pair, message.id, processed_text, "text", None)
        logger.info(f"Successfully cloned message {message.id} -> {ctx.chat_id} (msg {target_id})")
        return True

    async def _send_overflow(self, ctx: _SendContext, overflow_html: Optional[str], via_telethon: bool, bot: Optional[Bot] = None):
        """Sends the part of a caption that did not fit, once, as text message(s)."""
        if not overflow_html or TextProcessor.get_visible_text_length(overflow_html) <= 0:
            return
        try:
            for o_chunk in TextProcessor.fit_text_limit(overflow_html, max_limit=MESSAGE_TEXT_LIMIT):
                if not o_chunk.strip():
                    continue
                await self._pace(ctx)
                sent, _shown = await self._send_text_chunk(ctx, o_chunk, None, None, via_telethon, bot=bot)
                if sent:
                    self._remember_sent(sent, ctx.chat_id)
        except Exception as e:
            logger.warning(f"Could not send overflow text: {e}")

    @staticmethod
    def _audio_kwargs(message: Any) -> Dict[str, Any]:
        attr = ClonerEngine._source_attribute(message, tl_types.DocumentAttributeAudio)
        kwargs: Dict[str, Any] = {}
        if attr is not None:
            if getattr(attr, "duration", None):
                kwargs["duration"] = int(attr.duration)
            if getattr(attr, "title", None):
                kwargs["title"] = str(attr.title)
            if getattr(attr, "performer", None):
                kwargs["performer"] = str(attr.performer)
        return kwargs

    @staticmethod
    def _video_kwargs(message: Any) -> Dict[str, Any]:
        attr = ClonerEngine._source_attribute(message, tl_types.DocumentAttributeVideo)
        kwargs: Dict[str, Any] = {}
        if attr is not None:
            if getattr(attr, "duration", None):
                kwargs["duration"] = int(attr.duration)
            if getattr(attr, "w", None) and getattr(attr, "h", None):
                kwargs["width"] = int(attr.w)
                kwargs["height"] = int(attr.h)
        return kwargs

    async def _bot_send_file(self, bot: Bot, ctx: _SendContext, media_type: str, input_file: Any, caption: Optional[str],
                             markup: Optional[InlineKeyboardMarkup], source_message: Any, size: int) -> Any:
        """Bot API send of one media file with the matching method and the source metadata."""
        base = ctx.base_kwargs()
        timeout = self._upload_timeout(size)
        caption = caption or None
        if media_type == "photo":
            kw = dict(base, photo=input_file, caption=caption, parse_mode="HTML", reply_markup=markup, request_timeout=timeout)
            if ctx.caption_above and caption:
                kw["show_caption_above_media"] = True
            return await self._send_with_retry(bot.send_photo, **kw)
        if media_type == "video":
            kw = dict(base, video=input_file, caption=caption, parse_mode="HTML", reply_markup=markup,
                      supports_streaming=True, request_timeout=timeout, **self._video_kwargs(source_message))
            if ctx.caption_above and caption:
                kw["show_caption_above_media"] = True
            return await self._send_with_retry(bot.send_video, **kw)
        if media_type == "animation":
            kw = dict(base, animation=input_file, caption=caption, parse_mode="HTML", reply_markup=markup,
                      request_timeout=timeout, **self._video_kwargs(source_message))
            if ctx.caption_above and caption:
                kw["show_caption_above_media"] = True
            return await self._send_with_retry(bot.send_animation, **kw)
        if media_type == "voice":
            voice_attr = self._source_attribute(source_message, tl_types.DocumentAttributeAudio)
            duration = int(voice_attr.duration) if voice_attr is not None and getattr(voice_attr, "duration", None) else None
            return await self._send_with_retry(bot.send_voice, **base, voice=input_file, caption=caption, parse_mode="HTML",
                                               duration=duration, reply_markup=markup, request_timeout=timeout)
        if media_type == "video_note":
            video_attr = self._source_attribute(source_message, tl_types.DocumentAttributeVideo)
            note_kw: Dict[str, Any] = {}
            if video_attr is not None:
                if getattr(video_attr, "duration", None):
                    note_kw["duration"] = int(video_attr.duration)
                if getattr(video_attr, "w", None):
                    note_kw["length"] = int(video_attr.w)
            return await self._send_with_retry(bot.send_video_note, **base, video_note=input_file, reply_markup=markup,
                                               request_timeout=timeout, **note_kw)
        if media_type == "audio":
            return await self._send_with_retry(bot.send_audio, **base, audio=input_file, caption=caption, parse_mode="HTML",
                                               reply_markup=markup, request_timeout=timeout, **self._audio_kwargs(source_message))
        if media_type == "sticker":
            try:
                return await self._send_with_retry(bot.send_sticker, **base, sticker=input_file, request_timeout=timeout)
            except Exception as e_stk:
                logger.warning(f"send_sticker failed ({e_stk}), trying send_document fallback")
                return await self._send_with_retry(bot.send_document, **base, document=input_file, request_timeout=timeout)
        return await self._send_with_retry(bot.send_document, **base, document=input_file, caption=caption, parse_mode="HTML",
                                           reply_markup=markup, request_timeout=timeout)

    async def _clone_media_post(self, message: Any, ctx: _SendContext, media_type: str, caption_html: str,
                                cta_markup: Optional[InlineKeyboardMarkup], telethon_buttons: Optional[List[Any]],
                                paid: bool, premium_emojis: bool, files: List[str]) -> bool:
        pair = ctx.pair
        original_name = media_handler.get_original_filename(message)

        # 1. Download (photos up to 20 MB stay in RAM unless a logo watermark needs a file)
        photo_bytes: Optional[bytes] = None
        temp_file: Optional[str] = None
        if media_type == "photo" and not (paid and pair.image_watermark_type == "logo"):
            async with cache_manager.media_semaphore:
                photo_bytes = await media_handler.download_telethon_media_bytes(message, max_size_bytes=20 * 1024 * 1024)
        if photo_bytes is None:
            async with cache_manager.media_semaphore:
                temp_file = await media_handler.download_telethon_media(message)
            if not temp_file or not os.path.exists(temp_file):
                logger.error(f"Failed to download media for message {message.id}")
                return False
            self._track(files, temp_file)

        # 2. Listing duplicates / price arbitrage (photos), checked before any watermark work
        price = None
        if media_type == "photo":
            price = await self._extract_price(caption_html)
            try:
                from services.image_hasher import image_hasher
                photo_hash = await image_hasher.get_phash_async(photo_bytes if photo_bytes is not None else temp_file)
                duplicate = await self._check_listing(pair, [photo_hash] if photo_hash else [], price, message.id, [message.id], "photo", True)
            except Exception as he:
                logger.warning(f"Photo pHash deduplication error: {he}")
                duplicate = None
            if duplicate is not None:
                is_price_drop, match_info = duplicate
                if is_price_drop and price:
                    await self._apply_price_drop(pair, match_info, price, cta_markup)
                else:
                    logger.info(f"pHash: Skipping duplicate photo message #{message.id} for pair #{pair.id}")
                return True

        # 3. Watermarks (Pro/VIP visible watermark, invisible provenance watermark)
        if media_type == "photo":
            if photo_bytes is not None:
                photo_bytes = await self._watermark_photo_bytes(photo_bytes, pair, paid)
            else:
                temp_file = await self._watermark_photo_file(temp_file, pair, paid, files)
        elif media_type == "video":
            temp_file = await self._watermark_video_file(temp_file, pair, paid, files, message)

        size = len(photo_bytes) if photo_bytes is not None else self._file_size(temp_file)
        too_big_for_bot = size > BOT_API_UPLOAD_LIMIT_BYTES
        long_caption = TextProcessor.get_visible_text_length(caption_html) > BOT_API_CAPTION_LIMIT

        from services.telethon_listener import telethon_listener
        telethon_ok = telethon_listener.is_connected() and media_type != "sticker"
        sent = None
        shown_caption = None
        overflow = None
        via_telethon = False

        await self._pace(ctx)
        if telethon_ok and (too_big_for_bot or (media_type != "video_note" and (premium_emojis or long_caption))):
            caption_tl, overflow_tl = TextProcessor.fit_caption_limit(caption_html, max_limit=TELETHON_CAPTION_LIMIT)
            sent = await self._telethon_send_post(
                target_chat_id=ctx.chat_id,
                media_type=media_type,
                file_path=photo_bytes if photo_bytes is not None else temp_file,
                caption=(caption_tl or None) if media_type != "video_note" else None,
                supports_streaming=(media_type == "video"),
                buttons=telethon_buttons,
                topic_id=ctx.topic_id,
                silent=ctx.silent,
                source_message=message,
                file_name=original_name,
                buttons_required=not too_big_for_bot
            )
            if sent:
                via_telethon = True
                shown_caption = caption_tl if media_type != "video_note" else None
                overflow = overflow_tl if media_type != "video_note" else caption_html

        if not sent:
            if too_big_for_bot:
                logger.error(
                    f"Media of message {message.id} ({size / (1024 * 1024):.1f} MB) exceeds the Bot API upload limit and "
                    f"the Telethon userbot could not send it; left unrecorded for a later retry."
                )
                return False
            bot_clean_text = self._strip_tg_emoji(caption_html)
            if media_type in ("video_note", "sticker"):
                caption, overflow = None, (bot_clean_text or None)
            else:
                caption, overflow = TextProcessor.fit_caption_limit(bot_clean_text, max_limit=BOT_API_CAPTION_LIMIT)
            if photo_bytes is not None:
                input_file = BufferedInputFile(photo_bytes, filename="photo.jpg")
            else:
                input_file = FSInputFile(temp_file, filename=original_name) if original_name else FSInputFile(temp_file)
            sent = await self._bot_send_file(self.bot, ctx, media_type, input_file, caption, cta_markup, message, size)
            shown_caption = caption

        if not sent:
            logger.error(f"Failed to clone message {message.id} ({media_type}) to {ctx.chat_id} - media delivery failed")
            return False
        self._remember_sent(sent, ctx.chat_id)

        # Caption text that did not fit (or the caption of a video note / sticker) follows as text, once
        await self._send_overflow(ctx, overflow, via_telethon)

        target_id = self._sent_message_id(sent)
        await self._record_delivery(pair, message.id, target_id, media_type, shown_caption, price)
        await self._archive_backup(pair, message.id, caption_html, media_type, self._bot_file_id(sent))
        logger.info(f"Successfully cloned message {message.id} -> {ctx.chat_id} (msg {target_id})")
        return True

    @staticmethod
    def _poll_text(value: Any) -> str:
        if hasattr(value, "text"):
            return str(value.text or "")
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value or "")

    async def _send_poll(self, message: Any, ctx: _SendContext) -> Any:
        poll_media = message.media
        if not poll_media or not hasattr(poll_media, 'poll'):
            logger.warning(f"Could not extract poll from message {message.id}")
            return None
        poll = poll_media.poll
        question = truncate_utf16(self._poll_text(getattr(poll, 'question', '')).strip(), POLL_QUESTION_LIMIT)
        source_answers = list(poll.answers or [])[:POLL_MAX_OPTIONS]
        answers = [truncate_utf16(self._poll_text(getattr(a, 'text', '')).strip(), POLL_OPTION_LIMIT) for a in source_answers]
        if not question or len([a for a in answers if a]) < 2 or not all(answers):
            logger.warning(f"Poll has fewer than 2 valid options ({len(answers)}). Skipping poll for message {message.id}")
            return None

        poll_kw: Dict[str, Any] = dict(
            ctx.base_kwargs(),
            question=question,
            question_parse_mode=None,
            options=[InputPollOption(text=a, text_parse_mode=None) for a in answers],
            # Channels only accept anonymous polls
            is_anonymous=True,
            allows_multiple_answers=bool(getattr(poll, 'multiple_choice', False))
        )
        if getattr(poll, 'quiz', False):
            results = getattr(getattr(poll_media, 'results', None), 'results', None) or []
            correct_options = {bytes(r.option) for r in results if getattr(r, 'correct', False) and getattr(r, 'option', None) is not None}
            correct_ids = [i for i, a in enumerate(source_answers) if getattr(a, 'option', None) is not None and bytes(a.option) in correct_options]
            if correct_ids:
                poll_kw["type"] = "quiz"
                poll_kw["correct_option_ids"] = sorted(correct_ids)
                solution = getattr(getattr(poll_media, 'results', None), 'solution', None)
                if solution:
                    poll_kw["explanation"] = truncate_utf16(str(solution), POLL_EXPLANATION_LIMIT)
                    poll_kw["explanation_parse_mode"] = None
            else:
                logger.info(f"Quiz {message.id}: the correct answer is not visible to the userbot; posting it as a regular poll")
        return await self._send_with_retry(self.bot.send_poll, **poll_kw)

    async def _send_contact(self, message: Any, ctx: _SendContext) -> Any:
        c = message.media
        contact_kw = dict(
            ctx.base_kwargs(),
            phone_number=getattr(c, 'phone_number', '') or '',
            first_name=getattr(c, 'first_name', '') or '',
            last_name=getattr(c, 'last_name', '') or None
        )
        return await self._send_with_retry(self.bot.send_contact, **contact_kw)

    async def _send_location(self, message: Any, ctx: _SendContext) -> Any:
        geo = getattr(message.media, 'geo', None)
        if not geo:
            logger.warning(f"No valid geo coordinates found for message {message.id}")
            return None
        if hasattr(message.media, 'title') and hasattr(message.media, 'address'):
            # Venue
            return await self._send_with_retry(
                self.bot.send_venue,
                **ctx.base_kwargs(),
                latitude=geo.lat,
                longitude=geo.long,
                title=getattr(message.media, 'title', ''),
                address=getattr(message.media, 'address', '')
            )
        return await self._send_with_retry(
            self.bot.send_location,
            **ctx.base_kwargs(),
            latitude=geo.lat,
            longitude=geo.long
        )

    # ------------------------------------------------------------------
    # Drip feed
    # ------------------------------------------------------------------

    @staticmethod
    def _to_queued_file(path: str) -> str:
        """Renames a temp file to queued_<name>: the cleaner keeps it while its drip-feed row is pending."""
        base = os.path.basename(path)
        if base.startswith("queued_"):
            return path
        queued_path = os.path.join(os.path.dirname(path), f"queued_{base}")
        try:
            os.replace(path, queued_path)
            media_handler.release_path(path)
            return queued_path
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
            return path

    async def _enqueue_payload(self, pair: ChannelPair, payload: Dict[str, Any], source_ids: List[int], media_type: str,
                               price: Optional[float], media_group_id: Optional[str] = None) -> bool:
        """Records the source messages as "queued" (no target yet) and inserts the queue row. dispatch_queued_payload
        completes the records after delivery; the worker releases them if delivery finally fails."""
        for mid in source_ids:
            await db_manager.record_cloned_message(
                pair_id=pair.id,
                source_msg_id=mid,
                target_msg_id=None,
                media_group_id=media_group_id,
                media_type=media_type,
                source_channel=pair.source_channel,
                target_channel=pair.target_channel,
                status="queued",
                price=float(price or 0.0)
            )
        try:
            await drip_feed_service.enqueue_post(pair, payload)
        except Exception:
            await db_manager.forget_cloned_messages(pair.id, source_ids)
            raise
        return True

    async def _enqueue_single(self, message: Any, pair: ChannelPair, media_type: str, processed_text: Optional[str],
                              cta_markup: Optional[InlineKeyboardMarkup], paid: bool, files: List[str]) -> bool:
        payload: Dict[str, Any] = {
            "version": 2,
            "pair_id": pair.id,
            "source_msg_ids": [message.id],
            "text": processed_text or "",
            "media_type": media_type,
            "reply_markup": self._markup_to_payload(cta_markup),
            "progress": {}
        }
        price = None
        if media_type == "text":
            if not (processed_text or "").strip():
                await self._record_skip(pair, [message.id], media_type, "filtered")
                return False
        else:
            async with cache_manager.media_semaphore:
                temp_media_file = await media_handler.download_telethon_media(message)
            if not temp_media_file or not os.path.exists(temp_media_file):
                # Nothing is recorded: catch-up / history cloning can retry the message later
                logger.warning(f"Media download failed for message {message.id} (drip mode); not queued, left for a retry")
                return False
            self._track(files, temp_media_file)
            if media_type == "photo":
                price = await self._extract_price(processed_text)
                try:
                    from services.image_hasher import image_hasher
                    photo_hash = await image_hasher.get_phash_async(temp_media_file)
                    duplicate = await self._check_listing(pair, [photo_hash] if photo_hash else [], price, message.id, [message.id], "photo", True)
                except Exception as he:
                    logger.warning(f"Photo pHash deduplication error: {he}")
                    duplicate = None
                if duplicate is not None:
                    is_price_drop, match_info = duplicate
                    if is_price_drop and price:
                        await self._apply_price_drop(pair, match_info, price, cta_markup)
                    return True
                temp_media_file = await self._watermark_photo_file(temp_media_file, pair, paid, files)
            elif media_type == "video":
                temp_media_file = await self._watermark_video_file(temp_media_file, pair, paid, files, message)
            queued_path = self._to_queued_file(temp_media_file)
            # The queued file must survive this job; the pre-watermark original is removed with the job's files
            if temp_media_file in files:
                files.remove(temp_media_file)
            media_handler.release_path(temp_media_file)
            payload.update({
                "media_path": queued_path,
                "media_file_id": queued_path,
                "file_name": media_handler.get_original_filename(message),
                "watermarked": True,
                "price": price
            })
        await self._enqueue_payload(pair, payload, [message.id], media_type, price)
        logger.info(f"Message {message.id} ({media_type}) enqueued to drip feed for pair {pair.id}")
        return True

    @staticmethod
    def _looks_like_local_path(value: Any) -> bool:
        if not isinstance(value, str) or not value:
            return False
        return "/" in value or "\\" in value or os.path.basename(value).startswith("queued_") or bool(re.search(r'\.\w{2,5}$', value))

    async def dispatch_queued_payload(self, bot: Bot, pair: ChannelPair, payload: Dict[str, Any], disable_notification: bool = False):
        """Dispatches an enqueued message payload for drip feed or night buffer delivery.

        Resumable: every finished step is written to payload["progress"] (the worker persists the payload before a
        retry), so a retry never re-sends what was already delivered. After delivery the queued clone records are
        completed with the published message id. Raises FileNotFoundError when the queued media is gone and there
        is no text to publish instead."""
        target_chat_id = pair.target_id or self._normalize_chat_id(pair.target_channel)
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Refusing to dispatch queued payload to private user {target_chat_id}")
            return
        ctx = self._context(pair, bool(disable_notification))
        progress = payload.setdefault("progress", {}) if isinstance(payload, dict) else {}
        text = payload.get("text", "") or ""
        media_type = payload.get("media_type", "text")
        markup = self._markup_from_payload(payload.get("reply_markup"))
        source_ids = [int(m) for m in (payload.get("source_msg_ids") or []) if str(m).lstrip("-").isdigit()]

        if media_type == "media_group" and payload.get("media_files"):
            await self._dispatch_queued_album(bot, ctx, payload, progress)
        elif media_type in QUEUEABLE_MEDIA_TYPES and (payload.get("media_path") or payload.get("media_file_id")):
            await self._dispatch_queued_single(bot, ctx, payload, progress, markup)
        elif text:
            await self._dispatch_queued_text(bot, ctx, text, progress, markup)

        # Complete the clone records of the delivered source messages
        if source_ids and payload.get("pair_id") in (None, pair.id):
            album_targets = progress.get("album_targets") or {}
            for mid in source_ids:
                target_id = album_targets.get(str(mid)) if album_targets else progress.get("delivered_target_id")
                if not target_id:
                    continue
                caption = progress.get("caption_shown") if (not album_targets or str(mid) == str(progress.get("caption_holder"))) else None
                try:
                    await db_manager.mark_cloned_message_delivered(pair.id, mid, target_id, target_channel=pair.target_channel,
                                                                   last_caption=caption)
                except Exception:
                    logger.debug("Could not complete the queued clone record", exc_info=True)
            if album_targets:
                undelivered = [mid for mid in source_ids if not album_targets.get(str(mid))]
                if undelivered:
                    await db_manager.forget_cloned_messages(pair.id, undelivered)

        # On successful dispatch, remove temporary media files
        drip_feed_service._cleanup_payload_files(payload)

    async def _dispatch_queued_text(self, bot: Bot, ctx: _SendContext, text: str, progress: Dict[str, Any],
                                    markup: Optional[InlineKeyboardMarkup]):
        text_chunks = [c for c in TextProcessor.fit_text_limit(text, max_limit=MESSAGE_TEXT_LIMIT) if self._strip_tg_emoji(c).strip()]
        done = int(progress.get("chunks_done") or 0)
        for idx, chunk in enumerate(text_chunks):
            if idx < done:
                continue
            last = idx == len(text_chunks) - 1
            await self._pace(ctx)
            sent, shown = await self._send_text_chunk(ctx, chunk, markup if last else None, None, False, bot=bot)
            if not sent:
                raise RuntimeError(f"Queued text chunk {idx + 1}/{len(text_chunks)} was not delivered")
            self._remember_sent(sent, ctx.chat_id)
            if not progress.get("delivered_target_id"):
                progress["delivered_target_id"] = self._sent_message_id(sent)
                progress["caption_shown"] = shown
            progress["chunks_done"] = idx + 1

    async def _dispatch_queued_single(self, bot: Bot, ctx: _SendContext, payload: Dict[str, Any], progress: Dict[str, Any],
                                      markup: Optional[InlineKeyboardMarkup]):
        media_type = payload.get("media_type")
        media_ref = payload.get("media_path") or payload.get("media_file_id")
        text = payload.get("text", "") or ""
        clean_text = self._strip_tg_emoji(text)
        if not progress.get("media_done"):
            local = self._looks_like_local_path(media_ref)
            if local and not os.path.isfile(media_ref):
                if not clean_text.strip():
                    raise FileNotFoundError(f"Queued media file is missing: {media_ref}")
                logger.warning(f"Queued media file {media_ref} is missing; publishing the text only")
                await self._dispatch_queued_text(bot, ctx, text, progress, markup)
                progress["media_done"] = True
                progress["overflow_done"] = True
                return
            if local:
                file_name = payload.get("file_name") or None
                input_file = FSInputFile(media_ref, filename=file_name) if file_name else FSInputFile(media_ref)
                size = self._file_size(media_ref)
            else:
                # Legacy payloads may carry a Telegram file_id instead of a local path
                input_file = media_ref
                size = 0
            if media_type in ("video_note", "sticker"):
                caption, overflow = None, (clean_text or None)
            else:
                caption, overflow = TextProcessor.fit_caption_limit(clean_text, max_limit=BOT_API_CAPTION_LIMIT)
            await self._pace(ctx)
            sent = None
            if local and size > BOT_API_UPLOAD_LIMIT_BYTES:
                sent = await self._telethon_send_post(
                    target_chat_id=ctx.chat_id, media_type=media_type, file_path=media_ref, caption=caption,
                    supports_streaming=(media_type == "video"), topic_id=ctx.topic_id, silent=ctx.silent,
                    file_name=payload.get("file_name"), buttons_required=False
                )
            if not sent:
                sent = await self._bot_send_file(bot, ctx, media_type, input_file, caption, markup, None, size)
            if not sent:
                raise RuntimeError("Queued media was not delivered")
            self._remember_sent(sent, ctx.chat_id)
            progress["media_done"] = True
            progress["delivered_target_id"] = self._sent_message_id(sent)
            progress["caption_shown"] = caption
            progress["overflow"] = overflow
        if not progress.get("overflow_done"):
            overflow = progress.get("overflow")
            if overflow:
                await self._send_overflow(ctx, overflow, False, bot=bot)
            progress["overflow_done"] = True

    async def _dispatch_queued_album(self, bot: Bot, ctx: _SendContext, payload: Dict[str, Any], progress: Dict[str, Any]):
        album_targets: Dict[str, Any] = progress.setdefault("album_targets", {})
        text = payload.get("text", "") or ""
        items: List[_MediaItem] = []
        for idx, mf in enumerate(payload.get("media_files", []) or []):
            if not isinstance(mf, dict):
                continue
            src_id = mf.get("source_msg_id")
            # Legacy payloads have no source ids: the position identifies the item
            item_key = src_id if src_id is not None else f"idx:{idx}"
            if album_targets.get(str(item_key)):
                continue  # delivered by an earlier attempt
            m_path = mf.get("path")
            if not m_path or not os.path.isfile(m_path):
                logger.warning(f"Queued album file {m_path} is missing; the item is skipped")
                continue
            items.append(_MediaItem(idx, _QueuedSource(item_key, mf.get("file_name")), m_path, mf.get("type") or "photo", self._file_size(m_path)))
        if not items and not album_targets:
            if not self._strip_tg_emoji(text).strip():
                raise FileNotFoundError("All queued album files are missing")
            await self._dispatch_queued_text(bot, ctx, text, progress, None)
            return
        caption_html = "" if progress.get("caption_holder") else text
        if items:
            sent_map, caption_shown, overflow, holder = await self._send_album_items(bot, ctx, items, caption_html, False)
            for src_key, target_id in sent_map.items():
                album_targets[str(src_key)] = target_id
                if not progress.get("delivered_target_id"):
                    progress["delivered_target_id"] = target_id
            if holder is not None and not progress.get("caption_holder"):
                progress["caption_holder"] = holder
                progress["caption_shown"] = caption_shown
                progress["overflow"] = overflow
            if len(sent_map) < len(items):
                raise RuntimeError(f"{len(items) - len(sent_map)} queued album item(s) were not delivered")
        if not progress.get("overflow_done"):
            if progress.get("overflow"):
                await self._send_overflow(ctx, progress["overflow"], False, bot=bot)
            progress["overflow_done"] = True

    # ------------------------------------------------------------------
    # Albums
    # ------------------------------------------------------------------

    async def clone_media_group(self, messages: List[TelethonMessage], pair: ChannelPair) -> bool:
        if not messages:
            return False
        keys = self._claim_messages(pair.id, [m.id for m in messages])
        if keys is None:
            logger.debug(f"Media group for pair #{pair.id} is already being cloned. Skipping concurrent run.")
            return False
        try:
            return await self._clone_media_group(messages, pair)
        finally:
            self._inflight_messages.difference_update(keys)

    def _build_input_media(self, item: _MediaItem, caption: Optional[str], caption_above: bool) -> Any:
        file_name = getattr(item.message, "file_name", None) if isinstance(item.message, _QueuedSource) else media_handler.get_original_filename(item.message)
        input_file = FSInputFile(item.path, filename=file_name) if file_name else FSInputFile(item.path)
        kw: Dict[str, Any] = {"media": input_file, "caption": caption, "parse_mode": "HTML"}
        if item.media_type == "photo":
            if caption_above and caption:
                kw["show_caption_above_media"] = True
            return InputMediaPhoto(**kw)
        if item.media_type == "video":
            if caption_above and caption:
                kw["show_caption_above_media"] = True
            kw.update(self._video_kwargs(item.message))
            kw["supports_streaming"] = True
            return InputMediaVideo(**kw)
        if item.media_type == "audio":
            kw.update(self._audio_kwargs(item.message))
            return InputMediaAudio(**kw)
        return InputMediaDocument(**kw)

    @staticmethod
    def _album_chunks(items: List[Any]) -> List[List[Any]]:
        """Splits a partition into Telegram's 10-item album chunks of balanced size."""
        total = len(items)
        if total <= 10:
            return [items]
        num_chunks = (total + 9) // 10
        base_size, rem = divmod(total, num_chunks)
        chunks, cursor = [], 0
        for c_i in range(num_chunks):
            size = base_size + (1 if c_i < rem else 0)
            chunks.append(items[cursor:cursor + size])
            cursor += size
        return chunks

    async def _send_single_album_item(self, bot: Bot, ctx: _SendContext, item: _MediaItem, input_media: Any) -> Any:
        media_type = item.media_type if item.media_type in ("photo", "video", "audio") else "document"
        return await self._bot_send_file(bot, ctx, media_type, input_media.media, input_media.caption, None,
                                         None if isinstance(item.message, _QueuedSource) else item.message, item.size)

    async def _send_album_items(self, bot: Bot, ctx: _SendContext, items: List[_MediaItem], caption_html: str,
                                premium_emojis: bool) -> Tuple[Dict[Any, Any], Optional[str], Optional[str], Any]:
        """Sends album items grouped into compatible partitions (photos+videos / audio / documents).

        Returns (sent_map {source key: target msg id}, caption_shown, overflow_to_send, caption_holder_key). The
        caption is attached exactly once (first item sent); items that failed stay out of sent_map so they are not
        recorded and can be retried."""
        partitions = [
            [it for it in items if it.media_type in ("photo", "video")],
            [it for it in items if it.media_type == "audio"],
            [it for it in items if it.media_type == "document"],
            [it for it in items if it.media_type not in ("photo", "video", "audio", "document")],
        ]
        sent_map: Dict[Any, Any] = {}
        caption_shown: Optional[str] = None
        overflow: Optional[str] = None
        caption_holder: Any = None
        from services.telethon_listener import telethon_listener
        telethon_ok = telethon_listener.is_connected()

        for part in [p for p in partitions if p]:
            wants_caption = caption_holder is None and bool(caption_html and caption_html.strip())
            part_caption = caption_html if wants_caption else ""
            try:
                any_too_big = any(it.size > BOT_API_UPLOAD_LIMIT_BYTES for it in part)
                long_caption = TextProcessor.get_visible_text_length(part_caption) > BOT_API_CAPTION_LIMIT
                if telethon_ok and (any_too_big or premium_emojis or long_caption):
                    caption_tl, overflow_tl = TextProcessor.fit_caption_limit(part_caption, max_limit=TELETHON_CAPTION_LIMIT)
                    await self._pace(ctx, cost=len(part))
                    sent_list = await self._telethon_send_media_group(
                        target_chat_id=ctx.chat_id,
                        file_paths=[it.path for it in part],
                        caption=caption_tl or None,
                        topic_id=ctx.topic_id,
                        silent=ctx.silent,
                        source_messages=[None if isinstance(it.message, _QueuedSource) else it.message for it in part],
                        media_types=[it.media_type for it in part]
                    )
                    if sent_list:
                        for j, s_msg in enumerate(sent_list):
                            if j < len(part):
                                self._remember_sent(s_msg, ctx.chat_id)
                                sent_map[part[j].message.id] = self._sent_message_id(s_msg)
                        if wants_caption and caption_tl:
                            caption_holder = part[0].message.id
                            caption_shown = caption_tl
                            overflow = overflow_tl
                        continue
                if any_too_big:
                    big = [it for it in part if it.size > BOT_API_UPLOAD_LIMIT_BYTES]
                    logger.error(
                        f"{len(big)} album item(s) exceed the Bot API upload limit and the Telethon userbot is unavailable; "
                        f"they stay unrecorded for a later retry."
                    )
                    part = [it for it in part if it.size <= BOT_API_UPLOAD_LIMIT_BYTES]
                    if not part:
                        continue

                # Bot API: custom emoji tags are stripped, caption fits 1024 on the first item only
                caption_bot, overflow_bot = TextProcessor.fit_caption_limit(self._strip_tg_emoji(part_caption), max_limit=BOT_API_CAPTION_LIMIT)
                media_items = [
                    self._build_input_media(it, caption_bot if (idx == 0 and caption_bot) else None, ctx.caption_above)
                    for idx, it in enumerate(part)
                ]
                for chunk in self._album_chunks(list(zip(part, media_items))):
                    chunk_items = [c[0] for c in chunk]
                    chunk_media = [c[1] for c in chunk]
                    await self._pace(ctx, cost=len(chunk))
                    try:
                        if len(chunk) == 1:
                            result = await self._send_single_album_item(bot, ctx, chunk_items[0], chunk_media[0])
                            results = [result] if result else []
                        else:
                            timeout = self._upload_timeout(sum(it.size for it in chunk_items))
                            results = await self._send_with_retry(
                                bot.send_media_group, **ctx.base_kwargs(), media=chunk_media, request_timeout=timeout
                            ) or []
                    except TelegramBadRequest as tbr:
                        logger.warning(f"send_media_group failed ({tbr}), falling back to individual sends.")
                        results = []
                        for it, input_media in zip(chunk_items, chunk_media):
                            try:
                                res = await self._send_single_album_item(bot, ctx, it, input_media)
                            except Exception as item_err:
                                logger.warning(f"Album item {it.message.id} could not be sent individually: {item_err}")
                                res = None
                            if res:
                                self._remember_sent(res, ctx.chat_id)
                                sent_map[it.message.id] = self._sent_message_id(res)
                        results = None
                    except Exception as chunk_err:
                        logger.warning(f"Album chunk of {len(chunk)} item(s) could not be sent: {chunk_err}")
                        results = []
                    if results:
                        for j, r_msg in enumerate(results if isinstance(results, (list, tuple)) else [results]):
                            if j < len(chunk_items):
                                self._remember_sent(r_msg, ctx.chat_id)
                                sent_map[chunk_items[j].message.id] = self._sent_message_id(r_msg)
                    if wants_caption and caption_bot and caption_holder is None and part[0].message.id in sent_map:
                        caption_holder = part[0].message.id
                        caption_shown = caption_bot
                        overflow = overflow_bot
            except Exception as part_err:
                logger.error(f"Album partition could not be sent: {part_err}", exc_info=True)
        return sent_map, caption_shown, overflow, caption_holder

    async def _download_album_item(self, msg: Any, idx: int) -> Optional[_MediaItem]:
        try:
            async with cache_manager.media_semaphore:
                t_path = await media_handler.download_telethon_media(msg)
            if not t_path or not os.path.exists(t_path):
                return None
            return _MediaItem(idx, msg, t_path, media_handler.get_media_type(msg), self._file_size(t_path))
        except Exception as prep_err:
            logger.error(f"Error downloading media item {getattr(msg, 'id', '?')}: {prep_err}")
            return None

    async def _clone_media_group(self, messages: List[TelethonMessage], pair: ChannelPair) -> bool:
        if not self.bot or not messages:
            return False

        if not pair.is_active:
            return False

        # Safeguard against self-cloning loops
        if self._is_self_loop(pair):
            logger.error(f"Infinite loop detected: pair #{pair.id} has identical source and target ({pair.source_channel}). Skipping.")
            return False

        # Verify active subscription / 14-day trial for channel owner
        sub, is_admin = await self._privileges(pair)
        if not is_admin and not sub.is_active:
            logger.warning(f"Subscription or 14-day trial expired for user {pair.user_id}. Skipping media group for pair #{pair.id}")
            return False

        uncloned = [m for m in messages if not await db_manager.is_message_cloned(pair.id, m.id)]
        if not uncloned:
            return False

        target_chat_id = self._target_chat_id(pair)
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Media group target {target_chat_id} is a private user! Aborting clone.")
            return False

        ctx = self._context(pair, self._silent_now(pair))
        paid = self._is_paid(sub, is_admin)
        premium_emojis = bool(pair.auto_premium_emojis and self._is_vip(sub, is_admin))
        group_id_str = str(uncloned[0].grouped_id or "")

        raw_caption = ""
        for msg in uncloned:
            msg_html = extract_message_html(msg)
            if msg_html:
                raw_caption = msg_html
                break

        downloaded_files: List[str] = []
        try:
            processed_caption = await self.process_post_text(raw_caption, pair, has_media=True)
            if processed_caption is None:
                logger.info(f"Media group blocked by blacklist for pair {pair.id}.")
                await self._record_skip(pair, [m.id for m in uncloned], "media_group", "filtered")
                return False

            # 1. Download all items in parallel
            results = await asyncio.gather(*[self._download_album_item(m, i) for i, m in enumerate(uncloned)])
            valid_items = sorted([r for r in results if isinstance(r, _MediaItem)], key=lambda it: it.index)
            for it in valid_items:
                self._track(downloaded_files, it.path)
            failed_ids = [m.id for m in uncloned if m.id not in {it.message.id for it in valid_items}]
            if failed_ids:
                logger.warning(f"Album items {failed_ids} of pair #{pair.id} could not be downloaded; they stay unrecorded for a retry")
            if not valid_items:
                return False

            # 2. Listing duplicates / price arbitrage on the original photos
            detected_mg_price = await self._extract_price(processed_caption)
            photo_paths = [it.path for it in valid_items if it.media_type == "photo"]
            if photo_paths:
                try:
                    from services.image_hasher import image_hasher
                    mg_hashes = await image_hasher.get_multiple_phashes_async(photo_paths)
                    duplicate = await self._check_listing(
                        pair, mg_hashes, detected_mg_price, valid_items[0].message.id,
                        [it.message.id for it in valid_items], "media_group", False
                    )
                except Exception as he:
                    logger.warning(f"Media group pHash deduplication error: {he}")
                    duplicate = None
                if duplicate is not None:
                    is_price_drop, match_info = duplicate
                    if is_price_drop and detected_mg_price:
                        await self._apply_price_drop(pair, match_info, detected_mg_price, None)
                    else:
                        logger.info(f"pHash: Skipping duplicate media group for pair #{pair.id} (matched {match_info.get('matched_channel')} #{match_info.get('matched_msg_id')})")
                    return True

            # 3. Watermarks
            for it in valid_items:
                if it.media_type == "photo":
                    it.path = await self._watermark_photo_file(it.path, pair, paid, downloaded_files)
                elif it.media_type == "video":
                    it.path = await self._watermark_video_file(it.path, pair, paid, downloaded_files, it.message)
                it.size = self._file_size(it.path)

            # 4. Drip Feed / Night Buffer queueing for media groups
            if self._should_queue(pair):
                media_files = []
                for it in valid_items:
                    queued_path = self._to_queued_file(it.path)
                    if it.path in downloaded_files:
                        downloaded_files.remove(it.path)
                    media_handler.release_path(it.path)
                    media_files.append({
                        "path": queued_path,
                        "type": it.media_type,
                        "file_name": media_handler.get_original_filename(it.message),
                        "source_msg_id": it.message.id
                    })
                payload = {
                    "version": 2,
                    "pair_id": pair.id,
                    "source_msg_ids": [it.message.id for it in valid_items],
                    "media_group_id": group_id_str,
                    "text": processed_caption or "",
                    "media_type": "media_group",
                    "media_files": media_files,
                    "watermarked": True,
                    "price": detected_mg_price,
                    "progress": {}
                }
                await self._enqueue_payload(pair, payload, [it.message.id for it in valid_items], "media_group",
                                            detected_mg_price, media_group_id=group_id_str)
                logger.info(f"Media group ({len(media_files)} items) enqueued to drip feed for pair #{pair.id}")
                return True

            # 5. Send, then record exactly the delivered items (even when a later partition failed)
            sent_map, caption_shown, overflow, caption_holder = await self._send_album_items(
                self.bot, ctx, valid_items, processed_caption or "", premium_emojis
            )
            if sent_map:
                await self._send_overflow(ctx, overflow, premium_emojis)

            item_by_id = {it.message.id: it for it in valid_items}
            for msg in uncloned:
                target_msg_id = sent_map.get(msg.id)
                if not target_msg_id:
                    continue
                await self._record_delivery(
                    pair, msg.id, target_msg_id, "media_group",
                    caption_shown if msg.id == caption_holder else None,
                    detected_mg_price, media_group_id=group_id_str
                )
                it = item_by_id.get(msg.id)
                await self._archive_backup(
                    pair, msg.id, raw_caption if msg.id == caption_holder else "",
                    it.media_type if it and it.media_type in ("photo", "video", "document", "audio") else "media_group",
                    None, media_group_id=group_id_str
                )

            if not sent_map:
                logger.error(f"Media group for pair #{pair.id} could not be delivered to {target_chat_id}")
                return False
            not_sent = [m.id for m in uncloned if m.id not in sent_map]
            if not_sent:
                logger.warning(f"Media group items {not_sent} of pair #{pair.id} were not delivered and stay unrecorded for a retry")
            logger.info(f"Successfully cloned media group ({len(sent_map)} items) to {target_chat_id}")
            return True

        except Exception as e:
            logger.error(f"Error cloning media group to {target_chat_id}: {e}", exc_info=True)
            return False
        finally:
            await media_handler.cleanup_files(downloaded_files)

    @staticmethod
    def _normalize_chat_id(target_chat: str) -> Union[int, str]:
        target_chat = target_chat.strip()
        if target_chat.startswith("-100") or (target_chat.startswith("-") and target_chat[1:].isdigit()):
            return int(target_chat)
        elif target_chat.isdigit():
            if len(target_chat) >= 10:
                return int(f"-100{target_chat}")
            return int(target_chat)
        return target_chat

    # ------------------------------------------------------------------
    # Telethon (MTProto userbot) sends
    # ------------------------------------------------------------------

    async def _telethon_can_send_buttons(self, client: Any) -> bool:
        """Only bot accounts may attach inline keyboards over MTProto; the listener normally is a user account."""
        cached = self._telethon_bot_account
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        is_bot = False
        try:
            me = await client.get_me()
            is_bot = getattr(me, "bot", False) is True
        except Exception:
            is_bot = False
        self._telethon_bot_account = (time.monotonic(), is_bot)
        return is_bot

    def _mark_telethon_failure(self, target_key: str, error: Exception, what: str):
        err_str = str(error).lower()
        if "admin" in err_str or "chatadmin" in err_str or "privilege" in err_str:
            self._telethon_admin_required_targets[target_key] = time.time()
            logger.info(f"Telethon userbot lacks admin rights in {target_key}. Switched to Bot API directly.")
        else:
            logger.warning(f"Telethon {what} failed: {error}, falling back to Bot API")

    async def _telethon_send_fallback(self, target_chat_id: Union[int, str], file_path: str, caption: Optional[str],
                                      media_type: Optional[str] = None, topic_id: Optional[int] = None,
                                      buttons: Optional[List[Any]] = None, silent: bool = False,
                                      source_message: Any = None, file_name: Optional[str] = None):
        """Large-file path: sends a local file through the userbot, keeping topic, buttons (if possible), streaming
        and the source media attributes."""
        if not media_type:
            lower = str(file_path).lower()
            media_type = "video" if lower.endswith((".mp4", ".mov", ".mkv", ".webm")) else ("photo" if lower.endswith((".jpg", ".jpeg", ".png")) else "document")
        return await self._telethon_send_post(
            target_chat_id=target_chat_id,
            media_type=media_type,
            file_path=file_path,
            caption=caption,
            supports_streaming=(media_type == "video"),
            buttons=buttons,
            topic_id=topic_id,
            silent=silent,
            source_message=source_message,
            file_name=file_name,
            buttons_required=False
        )

    async def _telethon_send_post(
        self,
        target_chat_id: Union[int, str],
        media_type: str,
        file_path: Optional[Union[str, bytes]] = None,
        caption: Optional[str] = None,
        supports_streaming: bool = False,
        buttons: Optional[List[Any]] = None,
        topic_id: Optional[int] = None,
        silent: bool = False,
        source_message: Any = None,
        file_name: Optional[str] = None,
        buttons_required: bool = True
    ) -> Any:
        """Sends through the Telethon userbot (custom emoji, 2048-char captions, files over 50 MB).

        Media keep the source document's attributes (duration, dimensions, voice / round flags, file name), mime type
        and thumbnail. User accounts cannot attach inline buttons: with buttons_required the method returns None so
        the caller uses the Bot API and the buttons survive; otherwise the post is sent without them (logged)."""
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Blocked Telethon send to private user {target_chat_id}")
            return None
        from services.telethon_listener import telethon_listener
        from telethon.tl.types import User as TelethonUser
        client = telethon_listener.client
        if not client or not client.is_connected():
            return None
        target_key = str(target_chat_id).strip()
        if self._is_telethon_admin_required(target_key):
            return None
        try:
            send_buttons = None
            if buttons:
                if await self._telethon_can_send_buttons(client):
                    send_buttons = buttons
                elif buttons_required:
                    logger.info("The userbot account cannot attach inline buttons; this post goes through the Bot API to keep them.")
                    return None
                else:
                    logger.warning("Inline buttons dropped: only the userbot can deliver this post and user accounts cannot attach buttons.")

            target_entity = await telethon_listener.resolve_entity(target_key, join_invite=False)
            if not target_entity:
                return None
            if isinstance(target_entity, TelethonUser):
                logger.error(f"SECURITY ALERT: Resolved entity for {target_key} is a private User, not a Channel! Aborting.")
                return None

            common: Dict[str, Any] = {"parse_mode": "html", "buttons": send_buttons, "reply_to": topic_id}
            if silent:
                common["silent"] = True

            if media_type == "text":
                return await client.send_message(
                    target_entity,
                    message=caption or "",
                    link_preview=False,
                    **common
                )
            if not file_path:
                return None

            send_kwargs: Dict[str, Any] = dict(
                common,
                caption=caption or "",
                supports_streaming=supports_streaming,
                voice_note=(media_type == "voice"),
                video_note=(media_type == "video_note"),
                force_document=(media_type == "document")
            )
            attributes, mime_type = self._source_document_meta(source_message)
            if media_type != "photo":
                if attributes:
                    send_kwargs["attributes"] = attributes
                if mime_type:
                    send_kwargs["mime_type"] = mime_type
                thumb = await self._source_thumb(source_message, media_type)
                if thumb:
                    send_kwargs["thumb"] = thumb

            if isinstance(file_path, bytes):
                file_to_send: Any = io.BytesIO(file_path)
                file_to_send.name = file_name or "photo.jpg"
            else:
                file_to_send = file_path
                # Fast MTProto Parallel Upload for large media (> 10MB)
                if os.path.isfile(file_path) and os.path.getsize(file_path) > 10 * 1024 * 1024:
                    try:
                        uploaded = await fast_telethon.upload_file_parallel(
                            client=client,
                            file_input=file_path,
                            file_name=file_name or os.path.basename(file_path)
                        )
                        if uploaded:
                            file_to_send = uploaded
                    except Exception as up_err:
                        logger.debug(f"FastTelethon upload fallback: {up_err}")

            return await client.send_file(target_entity, file=file_to_send, **send_kwargs)
        except Exception as e:
            self._mark_telethon_failure(target_key, e, "send post")
        return None

    async def _telethon_album_file(self, client: Any, path: str, source_message: Any, media_type: Optional[str]) -> Any:
        """Album item for Telethon: photos go as paths; other media are uploaded with the source attributes (the
        album sender of Telethon would otherwise give videos 1x1 / 0:00 metadata)."""
        if source_message is None or media_type == "photo":
            return path
        attributes, mime_type = self._source_document_meta(source_message)
        if not attributes:
            return path
        file_name = media_handler.get_original_filename(source_message) or os.path.basename(path)
        uploaded = None
        if self._file_size(path) > 10 * 1024 * 1024:
            uploaded = await fast_telethon.upload_file_parallel(client=client, file_input=path, file_name=file_name)
        if uploaded is None:
            uploaded = await client.upload_file(path, file_name=file_name)
        thumb_file = None
        thumb = await self._source_thumb(source_message, media_type or "document")
        if thumb:
            thumb_file = await client.upload_file(thumb, file_name="thumb.jpg")
        return tl_types.InputMediaUploadedDocument(
            file=uploaded,
            mime_type=mime_type or "application/octet-stream",
            attributes=attributes,
            thumb=thumb_file
        )

    async def _telethon_send_media_group(
        self,
        target_chat_id: Union[int, str],
        file_paths: List[str],
        caption: Optional[str] = None,
        overflow_text: Optional[str] = None,
        topic_id: Optional[int] = None,
        silent: bool = False,
        source_messages: Optional[List[Any]] = None,
        media_types: Optional[List[str]] = None
    ) -> Optional[List[Any]]:
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Blocked Telethon media group send to private user {target_chat_id}")
            return None
        from services.telethon_listener import telethon_listener
        from telethon.tl.types import User as TelethonUser
        client = telethon_listener.client
        if not client or not client.is_connected() or not file_paths:
            return None
        target_key = str(target_chat_id).strip()
        if self._is_telethon_admin_required(target_key):
            return None
        try:
            target_entity = await telethon_listener.resolve_entity(target_key, join_invite=False)
            if not target_entity:
                return None
            if isinstance(target_entity, TelethonUser):
                logger.error(f"SECURITY ALERT: Resolved entity for {target_key} is a private User, not a Channel! Aborting.")
                return None
            files: List[Any] = []
            for idx, path in enumerate(file_paths):
                src = source_messages[idx] if source_messages and idx < len(source_messages) else None
                m_type = media_types[idx] if media_types and idx < len(media_types) else None
                files.append(await self._telethon_album_file(client, path, src, m_type))

            extra: Dict[str, Any] = {"silent": True} if silent else {}
            sent_all = []
            for chunk_idx in range(0, len(files), 10):
                f_chunk = files[chunk_idx:chunk_idx + 10]
                c_cap = (caption or "") if chunk_idx == 0 else ""
                sent = await client.send_file(
                    target_entity,
                    file=f_chunk,
                    caption=c_cap,
                    parse_mode="html",
                    reply_to=topic_id,
                    **extra
                )
                sent_all.extend(sent if isinstance(sent, (list, tuple)) else [sent])

            if overflow_text and TextProcessor.get_visible_text_length(overflow_text) > 0:
                try:
                    for o_chunk in TextProcessor.fit_text_limit(overflow_text, max_limit=MESSAGE_TEXT_LIMIT):
                        await client.send_message(
                            target_entity,
                            message=o_chunk,
                            parse_mode="html",
                            link_preview=False,
                            reply_to=topic_id,
                            **extra
                        )
                except Exception as oe:
                    logger.warning(f"Telethon overflow send failed: {oe}")
            return sent_all if sent_all else None
        except Exception as e:
            self._mark_telethon_failure(target_key, e, "media group send")
        return None

    # ------------------------------------------------------------------
    # Bot API send with retries
    # ------------------------------------------------------------------

    @staticmethod
    async def _call_absorbing_flood(send_func, args: tuple, kwargs: Dict[str, Any], max_waits: int = 2):
        """Fallback-tier call that waits out flood control instead of letting it escape."""
        for attempt in range(max_waits + 1):
            try:
                return await send_func(*args, **kwargs)
            except TelegramRetryAfter as e:
                if attempt == max_waits or e.retry_after > 300:
                    raise
                logger.warning(f"Telegram FloodWait in fallback send: waiting {e.retry_after}s")
                await asyncio.sleep(e.retry_after + 1)
        return None

    @staticmethod
    def _is_connection_error(error: TelegramNetworkError) -> bool:
        """True when the request never reached Telegram (safe to repeat)."""
        text = str(error).lower()
        return any(marker in text for marker in ("cannot connect", "connectorerror", "connector", "dns", "name resolution"))

    async def _send_with_retry(self, send_func, *args, max_retries: int = 3, **kwargs):
        network_retry_used = False
        for attempt in range(1, max_retries + 1):
            try:
                return await send_func(*args, **kwargs)
            except TelegramRetryAfter as e:
                wait_time = e.retry_after
                if wait_time > 300:
                    logger.error(f"Telegram FloodWait too long ({wait_time}s > 300s). Aborting attempt to prevent worker freeze.")
                    raise
                logger.warning(f"Telegram FloodWait: waiting {wait_time}s (attempt {attempt}/{max_retries})")
                await asyncio.sleep(wait_time + 1)
                if attempt == max_retries:
                    try:
                        return await send_func(*args, **kwargs)
                    except Exception as last_flood_err:
                        logger.error(f"Final retry after FloodWait failed: {last_flood_err}")
                        raise
            except TelegramEntityTooLarge as e:
                logger.error(f"File too large for the Bot API ({e}); not retried")
                raise
            except TelegramForbiddenError as e:
                logger.error(f"Telegram refused the send (bot not allowed in the chat): {e}")
                raise
            except TelegramBadRequest as e:
                err_str = str(e).lower()
                if kwargs.get("reply_markup") is not None and ("button" in err_str or "reply markup" in err_str or "reply_markup" in err_str):
                    # A rejected button (e.g. BUTTON_URL_INVALID) must not cost the whole post
                    logger.warning(f"Inline buttons rejected ({e}); sending the post once without buttons.")
                    retry_kwargs = dict(kwargs)
                    retry_kwargs["reply_markup"] = None
                    return await self._call_absorbing_flood(send_func, args, retry_kwargs)
                if ("parse" in err_str or "tag" in err_str or "unsupported" in err_str or "emoji" in err_str or "character" in err_str or "entit" in err_str):
                    return await self._send_with_format_fallback(send_func, args, kwargs, e)
                logger.error(f"Telegram BadRequest: {e}")
                raise
            except TelegramNetworkError as e:
                # A timed-out request may already have been delivered: repeating it would duplicate the post.
                # Only errors that happened before the request reached Telegram are repeated, once.
                if network_retry_used or attempt == max_retries or not self._is_connection_error(e):
                    logger.error(f"Telegram network error (not retried): {e}")
                    raise
                network_retry_used = True
                logger.warning(f"Telegram connection error, retrying once: {e}")
                await asyncio.sleep(2 * attempt)
            except TelegramAPIError as e:
                logger.error(f"Telegram API Error (attempt {attempt}): {e}")
                if attempt == max_retries:
                    raise
                await asyncio.sleep(2 * attempt)
            except Exception as e:
                logger.error(f"Unexpected error sending message: {e}")
                if attempt == max_retries:
                    raise
                await asyncio.sleep(1)
        return None

    async def _send_with_format_fallback(self, send_func, args: tuple, kwargs: Dict[str, Any], original_error: Exception):
        """Retries a send rejected for its formatting: 1) custom emoji removed (keeps <b>, <i>, <a>, ...),
        1.5) naked '&' escaped, 2) plain text. Each tier starts from the previous one, so a button icon removed in
        tier 1 never comes back."""
        # Tier 1: Try stripping ONLY <tg-emoji> tags to keep <b>, <i>, <a>, <code> intact
        logger.warning(f"Telegram entity/tag error ({original_error}), retrying with custom emojis converted to unicode...")

        def _map_texts(source: Dict[str, Any], func) -> Dict[str, Any]:
            result = dict(source)
            for key in ("text", "caption"):
                if isinstance(result.get(key), str):
                    result[key] = func(result[key])
            if isinstance(result.get("media"), (list, tuple)):
                new_media = []
                for m_item in result["media"]:
                    if hasattr(m_item, "caption") and isinstance(m_item.caption, str) and hasattr(m_item, "model_copy"):
                        new_media.append(m_item.model_copy(update={"caption": func(m_item.caption)}))
                    else:
                        new_media.append(m_item)
                result["media"] = new_media
            return result

        t1_kwargs = _map_texts(kwargs, self._strip_tg_emoji)
        rm = t1_kwargs.get("reply_markup")
        if rm is not None and hasattr(rm, "inline_keyboard") and hasattr(rm, "model_copy"):
            new_rows = []
            for row in rm.inline_keyboard:
                new_rows.append([
                    btn.model_copy(update={"icon_custom_emoji_id": None}) if hasattr(btn, "model_copy") else btn
                    for btn in row
                ])
            t1_kwargs["reply_markup"] = rm.model_copy(update={"inline_keyboard": new_rows})
        try:
            return await self._call_absorbing_flood(send_func, args, t1_kwargs)
        except TelegramBadRequest as t1_err:
            # Tier 1.5: Fix naked ampersands before destroying all formatting
            t15_kwargs = _map_texts(t1_kwargs, lambda s: _BARE_AMPERSAND_RE.sub('&amp;', s))
            try:
                return await self._call_absorbing_flood(send_func, args, t15_kwargs)
            except TelegramBadRequest:
                logger.debug("Ampersand fallback failed, falling back to plain text", exc_info=True)
            # Tier 2: General HTML syntax error, fallback to stripping all HTML tags
            logger.warning(f"HTML entity retry failed ({t1_err}), retrying without any HTML formatting...")
            clean_kwargs = _map_texts(t1_kwargs, lambda s: html.unescape(re.sub(r'<[^>]+>', '', s)))
            clean_kwargs["parse_mode"] = None
            if isinstance(clean_kwargs.get("media"), (list, tuple)):
                clean_kwargs["media"] = [
                    m_item.model_copy(update={"parse_mode": None}) if hasattr(m_item, "parse_mode") and hasattr(m_item, "model_copy") else m_item
                    for m_item in clean_kwargs["media"]
                ]
            try:
                return await self._call_absorbing_flood(send_func, args, clean_kwargs)
            except Exception as retry_err:
                logger.error(f"Fallback plain text send also failed: {retry_err}")
                raise


class _QueuedSource:
    """Stand-in for the source message of a queued album item (only the id and the file name are known)."""
    __slots__ = ("id", "file_name")

    def __init__(self, msg_id: Any, file_name: Optional[str]):
        self.id = msg_id
        self.file_name = file_name


cloner_engine = ClonerEngine()
