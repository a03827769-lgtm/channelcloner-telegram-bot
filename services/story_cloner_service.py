import asyncio
import html
import logging
import os
import random
import re
import shutil
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple, Union, Set
import inspect

from telethon import TelegramClient, types, functions, utils as tl_utils, events
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    FloodPremiumWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    ChannelPrivateError,
    ChannelInvalidError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    PremiumAccountRequiredError,
    UserBannedInChannelError,
    UserAlreadyParticipantError
)
from PIL import Image, ImageFilter, ImageEnhance

from config.settings import settings
from config.limits import (
    STORY_VIDEO_DURATION_MIN, STORY_VIDEO_DURATION_MAX, STORY_MAX_PER_DAY_MIN, STORY_MAX_PER_DAY_MAX,
    STORY_DRIP_DELAY_MAX_MINUTES
)
from database.db_manager import db_manager, DatabaseManager
from database.models import StorySettings
from services.phone_utils import normalize_phone_number
from services.story_renderer import story_card_renderer, tashkent_date_label, TASHKENT_TZ
from services.story_video_generator import story_video_generator
from services.listing_analyzer import listing_analyzer, format_price_usd, normalize_listing_text
from services.story_queue_service import story_queue_service, NON_PREMIUM_DAILY_STORY_LIMIT
from services.render_queue import render_queue
from services.telethon_listener import session_uses_ipv6
from services.custom_emojis import INBOX, TROPHY, CHANNEL, MONEY, LOCATION, TIMER, PARTY, TAG, INFO, MOBILE, LINK, WARN

logger = logging.getLogger(__name__)


DEVICE_MODEL = "StoryCloner Pro"
SYSTEM_VERSION = "Telegram MTProto"
APP_VERSION = "StoryBot v3.0"
LANG_CODE = "uz"
SYSTEM_LANG_CODE = "uz-UZ"

# ---- Price parsing -------------------------------------------------------------------------------------------------
# extract_price() returns USD. Amounts in so'm are converted with this fixed rate so the USD filters ($700+),
# the quality score and the badges compare like with like.
UZS_PER_USD = 12700.0
# Only the start of a post is scanned (the price is always near the top; keeps the parser O(1) per post)
PRICE_TEXT_MAX_CHARS = 1500
USD_PRICE_MIN = 50.0
USD_PRICE_MAX = 5_000_000.0
UZS_PRICE_MIN = 100_000.0

# A number is either digit groups separated by ONE space / dot / comma ("4 500 000", "65.000", "1,500") or a plain
# integer / decimal ("750", "1.5", "12,5"). The token has fixed-width groups and no trailing context, and everything
# around a token is checked with anchored matches on short windows, so parsing is linear on any input.
_PRICE_NUMBER_RE = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:[ .,]\d{3})+(?!\d)|\d+(?:[.,]\d+)?)")
_PRICE_GROUPED_RE = re.compile(r"\d{1,3}(?:[ .,]\d{3})+")
_PRICE_MULTIPLIER_RE = re.compile(
    r" ?(?:(ming|минг|тыс(?:яч[а-я]*|\.)?|k(?![a-zа-я])|к(?![a-zа-я]))"
    r"|(mln|млн|million|миллион[а-я]*)|(mlrd|млрд|milliard|миллиард[а-я]*))\.?"
)
_PRICE_USD_AFTER_RE = re.compile(
    r" {0,3}(?:\$|usd\b|у\.? ?е\.?(?![а-я])|y\.? ?e\.?(?![a-z])|dollar[a-z]*|доллар[а-я]*|долл\b)"
)
_PRICE_USD_BEFORE_RE = re.compile(r"(?:\$|\busd|у\.е\.)\s{0,2}$")
_PRICE_UZS_AFTER_RE = re.compile(r" {0,3}(?:so'?m|sum|сўм|сум|сом\b|uzs\b)")
_PRICE_DISTRACTOR_RE = re.compile(
    r" {0,3}(?:kv\.? ?m|m2|м2|m²|м²|metr|метр|кв\.? ?м|кв\b|qavat|қават|этаж|etaj|xona|хона|комн|sotix|сотих|"
    r"сот(?:ок|ки)|kishi|odam|km\b|км\b|mavze|мавзе|kvartal|квартал|yil|год|%)"
)
_PRICE_RANGE_SEP_RE = re.compile(r" {0,3}(?:-|–|—|\.\.\.|…) {0,3}$")
_PRICE_DAN_RE = re.compile(r" {0,3}dan\b")
_PRICE_LABEL_BEFORE_RE = re.compile(
    r"(?:narxi|narx|нархи|нарх|цена|стоимость|qiymati|ijara|arenda|аренда|to'lov|тўлов|budjet|бюджет)"
    r"\s{0,3}[:\-–—=]?\s{0,3}$"
)

# ---- Demand / offer classification ---------------------------------------------------------------------------------
# Explicit offer verbs: a listing even when it mentions tenant conditions ("oila kerak", "Нужна порядочная семья")
_OFFER_VERBS_RE = re.compile(
    r"\b(?:beriladi|берилади|topshiriladi|топширилади|сда[её]тся|сдам|сдаю|сда[её]м|sotiladi|сотилади|"
    r"продается|продаётся|продам|продаю|ijaraga\s+(?:beraman|beramiz)|arendaga\s+(?:beraman|beramiz))\b"
)
# Strong demand phrases: somebody is looking for a flat
_STRONG_DEMAND_RE = re.compile(
    r"\b(?:kvartira|uy|joy|arenda|ijara|xona|хона|квартира|уй|жой|ижара|аренда)\s+(?:kerak|керак)\b"
    r"|\b(?:kerak|керак)\s+(?:kvartira|uy|joy|arenda|ijara|xona|хона|квартира)\b"
    r"|\b(?:menga|bizga|oilaga|klientga|klientimizga|менга|бизга|оилага|клиентга)\s+(?:kerak|керак)\b"
    r"|\b(?:qidiryapman|qidiryapmiz|qidirilmoqda|qidirayapmiz|qidirilyapti|qidiraman|қидиряпман|қидирилмоқда)\b"
    r"|\b(?:olmoqchiman|olmoqchimiz|olaman|олмоқчиман|оламан)\b"
    r"|\b(?:ищу|ищем|ищет|сниму|снимем|куплю)\b"
    r"|\bнуж(?:на|ен|но)\s+(?:квартир[а-я]*|дом|жиль[её]|\d[\w-]*комнат[а-я]*|комнат[а-я]*|студи[а-я]*)"
    r"|\bзапрос\s+на\s+(?:аренду|покупку|съ[её]м)"
)
# Offer markers (or a detected price) outweigh generic demand words ("Klient bor", "Нужна предоплата", "запрос на показ")
_OFFER_MARKERS_RE = re.compile(r"\b(?:аренда|в\s+аренду|ijaraga|arendaga|sotuvda|sotuvga|продажа|в\s+наем|сдача)\b")
_GENERIC_DEMAND_RE = re.compile(
    r"\b(?:нужна|нужно|нужен|запрос|клиент\s*бор|klient\s*bor|клиент\s*есть|клиентларга|mijoz\s+bor)\b"
)

# ---- Monitoring / publishing policy --------------------------------------------------------------------------------
WATCHDOG_INTERVAL_SECONDS = 45.0
# The watchdog only re-checks fresh posts (older ones were either handled live or predate the monitor)
WATCHDOG_MAX_MESSAGE_AGE = timedelta(hours=3)
SUPERVISOR_INTERVAL_SECONDS = 60.0
SUPERVISOR_MAX_BACKOFF_SECONDS = 1800.0
CLIENT_POOL_SOFT_LIMIT = 20
LOGIN_SESSION_TTL_SECONDS = 600.0
PREMIUM_CACHE_TTL_SECONDS = 3600.0
STORIES_LIMIT_RETRY_SECONDS = 24 * 3600
TRANSIENT_RETRY_SECONDS = 600
ALBUM_MAX_PHOTOS = 8
STORY_JOB_DIR_PREFIX = "story_job_"
STORY_JOB_DIR_MAX_AGE_SECONDS = 6 * 3600
FAILURE_NOTICE_INTERVAL_SECONDS = 6 * 3600


def _normalize_peer_id(value: Any) -> Optional[int]:
    return DatabaseManager.normalize_peer_id(value)


def _temp_media_dir() -> str:
    temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


class StoryPostResult(tuple):
    """(ok, story_id, message, story_url) - unpacks like the historical 4-tuple - plus the failure class:
    error_kind: None | 'permanent' | 'not_found' | 'vip' | 'limit' | 'flood' | 'transient'
    retry_after: seconds Telegram asked us to wait (FloodWait / story limits)"""

    def __new__(cls, ok: bool, story_id: Optional[int], message: str, story_url: Optional[str] = None,
                error_kind: Optional[str] = None, retry_after: Optional[float] = None):
        obj = super().__new__(cls, (ok, story_id, message, story_url))
        obj.error_kind = error_kind
        obj.retry_after = retry_after
        return obj


def _fail(message: str, kind: str = "transient", retry_after: Optional[float] = None) -> StoryPostResult:
    return StoryPostResult(False, None, message, None, kind, retry_after)


def classify_story_error(exc: BaseException) -> Tuple[str, Optional[float], str]:
    """Maps a Telegram error to (kind, retry_after_seconds, Uzbek user message)"""
    if isinstance(exc, (FloodWaitError, FloodPremiumWaitError)):
        seconds = float(getattr(exc, "seconds", 0) or 0)
        return "flood", seconds, f"Telegram cheklovi (FloodWait): {int(seconds)} soniyadan keyin qayta uriniladi."
    text = f"{getattr(exc, 'message', '') or ''} {exc}".upper()
    flood = re.search(r"STORY_SEND_FLOOD_(?:WEEKLY|MONTHLY)_(\d+)", text)
    if flood:
        seconds = float(flood.group(1))
        return "flood", seconds, f"Telegram istoriya limiti: {int(seconds // 3600)} soatdan keyin qayta uriniladi."
    if "STORIES_TOO_MUCH" in text:
        return ("limit", float(STORIES_LIMIT_RETRY_SECONDS),
                "Telegram istoriya limiti tugadi (STORIES_TOO_MUCH). 24 soatdan keyin qayta uriniladi.")
    if "BOOSTS_REQUIRED" in text:
        return "permanent", None, "Kanal istoriyasi uchun kanalga Boost kerak (BOOSTS_REQUIRED)."
    if isinstance(exc, ChatAdminRequiredError) or "CHAT_ADMIN_REQUIRED" in text:
        return "permanent", None, "Kanalga istoriya joylash huquqi yo'q: hisobingiz kanal admini bo'lishi va istoriya joylash ruxsatiga ega bo'lishi kerak."
    if isinstance(exc, PremiumAccountRequiredError) or "PREMIUM_ACCOUNT_REQUIRED" in text:
        return "permanent", None, "Telegram cheklovi: bu amal uchun Telegram Premium talab qilinadi."
    if isinstance(exc, (ChannelPrivateError, ChannelInvalidError, ChatWriteForbiddenError, UserBannedInChannelError)) \
            or any(k in text for k in ("CHANNEL_PRIVATE", "CHANNEL_INVALID", "CHAT_WRITE_FORBIDDEN", "USER_BANNED_IN_CHANNEL", "PEER_ID_INVALID")):
        return "permanent", None, f"Kanalga kirish imkoni yo'q ({type(exc).__name__})."
    return "transient", None, f"Xatolik yuz berdi: {exc}"


class StoryClonerService:
    def __init__(self):
        self._user_clients: Dict[int, TelegramClient] = {}
        self._login_sessions: Dict[int, Dict[str, Any]] = {}
        self._otp_cooldowns: Dict[int, float] = {}
        self._bot_instance = None
        self._user_poll_tasks: Dict[int, asyncio.Task] = {}
        self._active_channel_handlers: Dict[int, Any] = {}
        self._processing_locks: Dict[int, asyncio.Lock] = {}
        self._client_locks: Dict[int, asyncio.Lock] = {}
        self._monitored_ids: Dict[int, Set[int]] = {}
        self._monitor_started_at: Dict[int, datetime] = {}
        self._restart_backoff: Dict[int, Tuple[float, float]] = {}
        self._premium_cache: Dict[int, Tuple[Optional[bool], float]] = {}
        self._publish_backoff_until: Dict[int, float] = {}
        self._failure_notices: Dict[Tuple[int, str], float] = {}
        self._background_tasks: Set[asyncio.Task] = set()
        self._supervisor_task: Optional[asyncio.Task] = None
        self._last_job_dir_sweep = 0.0

    def set_bot(self, bot):
        self._bot_instance = bot

    def _spawn(self, coro) -> asyncio.Task:
        """create_task with a strong reference (tasks without one can be garbage-collected mid-run)"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ==========================================
    # --- REAL ESTATE PRICE & QUALITY FILTER ---
    # ==========================================

    @staticmethod
    def _parse_num_string(raw: str) -> Optional[float]:
        """Parses one price number: '4 500 000', '65.000', '1,500' (thousands groups) or '1.5' / '12,5' (decimals).
        When the capture holds several numbers (e.g. '65 m2 75 000'), the last complete number is used."""
        if not raw:
            return None
        s = re.sub(r"\s+", " ", str(raw)).strip(" .,")
        candidates = _PRICE_NUMBER_RE.findall(s)
        if not candidates:
            return None
        token = candidates[-1]
        if _PRICE_GROUPED_RE.fullmatch(token):
            return float(re.sub(r"[ .,]", "", token))
        try:
            return float(token.replace(",", "."))
        except ValueError:
            return None

    @staticmethod
    def _normalize_price_text(text: str) -> str:
        return normalize_listing_text(text[:PRICE_TEXT_MAX_CHARS]).lower()

    @classmethod
    def extract_price(cls, text: str) -> Optional[float]:
        """
        Extracts the listing price in USD from real estate posts of Tashkent / Uzbekistan channels.
        Handles $, USD, y.e., у.е., dollar/доллар; so'm/sum/сўм/сум/UZS (converted with UZS_PER_USD);
        multipliers ming/минг/тыс/k (x1 000) and mln/млн (x1 000 000); dot/comma/space thousand separators.
        Examples:
        - 750$ -> 750.0, $800 -> 800.0, 1.500$ -> 1500.0, 65,000 $ -> 65000.0, Цена: 1200 у.е. -> 1200.0
        - Narxi: 4 500 000 so'm -> 354.3 (UZS converted), Narxi 650 ming so'm -> 51.2
        - Narxi: 2 500 000 (no currency, >= 100 000) -> treated as so'm -> 196.9
        - Цена 65 тыс $ -> 65000.0, 1.5 mln $ -> 1500000.0, Ijara 750-800$ -> 750.0 (lower bound)
        Only the first PRICE_TEXT_MAX_CHARS characters are read; the parser is linear in the input length.
        """
        if not text:
            return None
        t = cls._normalize_price_text(str(text))
        tokens = []
        for m in _PRICE_NUMBER_RE.finditer(t):
            value = cls._parse_num_string(m.group(0))
            if value is None:
                continue
            start, end, multiplier = m.start(), m.end(), 1.0
            mm = _PRICE_MULTIPLIER_RE.match(t, end)
            if mm:
                multiplier = 1e3 if mm.group(1) else (1e6 if mm.group(2) else 1e9)
                end = mm.end()
            cur_after, cur_end = None, end
            usd = _PRICE_USD_AFTER_RE.match(t, end)
            if usd:
                cur_after, cur_end = "usd", usd.end()
            else:
                uzs = _PRICE_UZS_AFTER_RE.match(t, end)
                if uzs:
                    cur_after, cur_end = "uzs", uzs.end()
            cur_before = "usd" if _PRICE_USD_BEFORE_RE.search(t, max(0, start - 6), start) else None
            tokens.append({
                "start": start, "end": end, "value": value * multiplier, "raw": value, "mult": multiplier,
                "cur_after": cur_after, "cur_before": cur_before,
                "distractor": bool(_PRICE_DISTRACTOR_RE.match(t, end)),
                "distractor_after_cur": bool(cur_after and _PRICE_DISTRACTOR_RE.match(t, cur_end)),
            })
        if not tokens:
            return None

        def usd_ok(v: Optional[float]) -> bool:
            return v is not None and USD_PRICE_MIN <= v <= USD_PRICE_MAX

        def uzs_to_usd(v: float) -> Optional[float]:
            if v < UZS_PRICE_MIN:
                return None
            usd_value = round(v / UZS_PER_USD, 1)
            return usd_value if usd_ok(usd_value) else None

        # 0. Ranges "750-800$", "700 - 800 у.е.", "65-70 ming $": the lower bound (multiplier written once at the end)
        for a, b in zip(tokens, tokens[1:]):
            if (a["cur_after"] is None and not a["distractor"] and b["cur_after"] == "usd"
                    and _PRICE_RANGE_SEP_RE.search(t, a["end"], b["start"])):
                value = a["value"] if a["mult"] != 1.0 else a["raw"] * b["mult"]
                if usd_ok(value):
                    return value
        # 1. Number followed by a USD marker
        for tok in tokens:
            if tok["cur_after"] == "usd" and not tok["distractor_after_cur"] and usd_ok(tok["value"]):
                return tok["value"]
        # 2. USD marker before the number ("$800", "USD 1200")
        for tok in tokens:
            if tok["cur_before"] == "usd" and tok["cur_after"] is None and not tok["distractor"] and usd_ok(tok["value"]):
                return tok["value"]
        # 3. Price label without currency ("Narxi: 850"): up to 100 000 it is USD, larger amounts are so'm
        for tok in tokens:
            if tok["cur_after"] or tok["cur_before"] or tok["distractor"]:
                continue
            if not _PRICE_LABEL_BEFORE_RE.search(t, max(0, tok["start"] - 24), tok["start"]):
                continue
            value = uzs_to_usd(tok["value"]) if tok["value"] >= UZS_PRICE_MIN else (tok["value"] if usd_ok(tok["value"]) else None)
            if value is not None:
                return value
        # 4. Starting price "700 dan"
        for tok in tokens:
            if tok["cur_after"] is None and not tok["distractor"] and _PRICE_DAN_RE.match(t, tok["end"]) and usd_ok(tok["value"]):
                return tok["value"]
        # 5. Explicit so'm amounts, converted to USD
        for tok in tokens:
            if tok["cur_after"] == "uzs":
                value = uzs_to_usd(tok["value"])
                if value is not None:
                    return value
        return None

    @staticmethod
    def is_demand_post(text: str, detected_price: Optional[float] = None) -> bool:
        """
        Detects whether a post is a client inquiry / search request rather than an available listing.
        Explicit offer verbs win; strong demand phrases ("kvartira kerak", "ищу", "qidiryapman") come next; offer markers
        (аренда, ijaraga, sotuvda...) or a detected price outweigh generic demand words ("Klient bor", "Нужна предоплата").
        """
        if not text:
            return False
        t = normalize_listing_text(str(text)[:PRICE_TEXT_MAX_CHARS]).lower()
        if _OFFER_VERBS_RE.search(t):
            return False
        if _STRONG_DEMAND_RE.search(t):
            return True
        if detected_price is None:
            detected_price = StoryClonerService.extract_price(t)
        if detected_price is not None or _OFFER_MARKERS_RE.search(t):
            return False
        return bool(_GENERIC_DEMAND_RE.search(t))

    @staticmethod
    def has_media(message: Any) -> bool:
        """Checks whether the message contains photos or video"""
        if not message:
            return False
        if getattr(message, 'photo', None):
            return True
        if getattr(message, 'video', None):
            return True
        if getattr(message, 'media', None):
            media_type = type(message.media).__name__.lower()
            if "photo" in media_type or "document" in media_type or "video" in media_type:
                return True
        return False

    def matches_filter(
        self,
        message: Any,
        settings: StorySettings
    ) -> Tuple[bool, str, Optional[float]]:
        """
        Evaluates whether an incoming post matches the real estate story criteria (CPU only; callers on the event
        loop run it through asyncio.to_thread).
        Returns: (is_match, reason, detected_price)
        """
        text = getattr(message, 'text', '') or getattr(message, 'message', '') or ""
        if not isinstance(text, str):
            text = str(text)

        price = self.extract_price(text)

        # Check demand / inquiry filter
        if settings.filter_demands and self.is_demand_post(text, detected_price=price):
            return False, "Mijoz qidiruvi / talab xabari (listing emas)", None

        # Check media requirement
        if settings.require_photos and not self.has_media(message):
            return False, "Postda rasm yoki video yo'q", None

        if price is not None:
            if price < settings.min_price:
                return False, f"Narx ({format_price_usd(price)}) minimal chegara ({format_price_usd(settings.min_price, '$0')}) dan kam", price
            if settings.max_price > 0 and price > settings.max_price:
                return False, f"Narx ({format_price_usd(price)}) maksimal chegara ({format_price_usd(settings.max_price)}) dan yuqori", price
        elif settings.require_price:
            return False, "Postda narx aniqlanmadi (yoki $ emas)", None

        return True, "Mos keldi", price

    # ==========================================
    # --- STORY BACKGROUND GENERATOR ---
    # ==========================================

    @staticmethod
    def get_or_create_background(
        style: str = "telegram_green",
        source_photo_path: Optional[str] = None,
        out_dir: Optional[str] = None
    ) -> str:
        """
        Retrieves or generates the background image for the Telegram Story (1080x1920, 9:16).
        Styles:
        - telegram_green: Authentic Telegram doodle wallpaper
        - listing_blur: Cinematic Gaussian-blurred apartment photo with dark vignette (written to out_dir)
        - luxury_dark: Sleek dark aesthetic
        - emerald: Deep emerald gradient
        """
        base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "story_backgrounds")
        os.makedirs(base_dir, exist_ok=True)

        if style == "listing_blur" and source_photo_path and os.path.exists(source_photo_path):
            try:
                target_dir = out_dir or _temp_media_dir()
                out_path = os.path.join(target_dir, f"blur_bg_{uuid.uuid4().hex}.jpg")
                # Blurred at half size and upscaled: same look, a fraction of the memory
                work_w, work_h = 540, 960
                with Image.open(source_photo_path) as src:
                    src.draft("RGB", (work_w, work_h))
                    im = src.convert('RGB')
                src_w, src_h = im.size
                scale = max(work_w / src_w, work_h / src_h)
                new_w, new_h = max(work_w, int(round(src_w * scale))), max(work_h, int(round(src_h * scale)))
                im = im.resize((new_w, new_h), Image.Resampling.BILINEAR)
                left = (new_w - work_w) // 2
                top = (new_h - work_h) // 2
                im = im.crop((left, top, left + work_w, top + work_h))

                im = im.filter(ImageFilter.GaussianBlur(radius=18))
                im = ImageEnhance.Brightness(im).enhance(0.75)
                im = im.resize((1080, 1920), Image.Resampling.BICUBIC)
                im.save(out_path, format="JPEG", quality=92)
                return out_path
            except Exception as e:
                logger.warning(f"Failed to generate blurred listing background: {e}. Falling back to default.")

        preset_map = {
            "telegram_green": os.path.join(base_dir, "telegram_green.jpg"),
            "luxury_dark": os.path.join(base_dir, "luxury_dark.jpg"),
            "emerald": os.path.join(base_dir, "emerald.jpg")
        }

        path = preset_map.get(style, preset_map["telegram_green"])
        if os.path.exists(path):
            return path

        # Fallback: create default green if missing
        tg_green = Image.new('RGB', (1080, 1920), color=(135, 184, 120))
        fallback_path = os.path.join(base_dir, "telegram_green.jpg")
        tg_green.save(fallback_path, format="JPEG", quality=90)
        return fallback_path

    # ==========================================
    # --- CHANNEL / TARGET RESOLUTION ---
    # ==========================================

    @staticmethod
    def _entity_label(entity: Any) -> str:
        username = getattr(entity, "username", None)
        if isinstance(username, str) and username:
            return f"@{username}"
        raw_id = _normalize_peer_id(getattr(entity, "id", None))
        return f"-100{raw_id}" if raw_id else str(entity)

    async def _resolve_invite(self, client: TelegramClient, link: str) -> Any:
        """Resolves (and joins when needed) a private invite link: '+HASH' or 'joinchat/HASH'"""
        hash_val = re.sub(r"^(?:\+|joinchat/)", "", link, flags=re.IGNORECASE).strip("/ ")
        if not hash_val:
            return None
        chat_info = None
        try:
            chat_info = await client(functions.messages.CheckChatInviteRequest(hash_val))
            chat = getattr(chat_info, 'chat', None)
            if chat:
                return chat
        except Exception as e:
            logger.warning(f"Invite link check failed ({link}): {e}")
        try:
            imp_res = await client(functions.messages.ImportChatInviteRequest(hash_val))
            chats = getattr(imp_res, 'chats', None)
            if chats:
                return chats[0]
        except UserAlreadyParticipantError:
            target_title = getattr(chat_info, 'title', None)
            if not target_title and hasattr(chat_info, 'chat'):
                target_title = getattr(chat_info.chat, 'title', None)
            try:
                async for dialog in client.iter_dialogs(limit=500):
                    if (dialog.is_channel or dialog.is_group) and target_title and getattr(dialog, 'title', None) == target_title:
                        return dialog.entity
            except Exception as dlg_err:
                logger.warning(f"Dialog lookup for invite link {link} failed: {dlg_err}")
        except Exception as imp_err:
            logger.warning(f"Invite import failed for {link}: {imp_err}")
        return None

    async def _resolve_channel_entity(self, client: TelegramClient, ref: Any) -> Any:
        """
        Resolves a channel reference WITH THIS CLIENT: entity / InputPeer objects are returned as-is,
        '@name', 'name', 't.me/name' by username, '-1001234' / '1234' / ints as channel ids (never as phone numbers),
        falling back to the account's dialogs when the id is not in the session cache, and invite links.
        """
        if ref is None or ref == "":
            return None
        if not isinstance(ref, (str, int)) or isinstance(ref, bool):
            return ref
        if isinstance(ref, int):
            raw_id = _normalize_peer_id(ref)
        else:
            s = ref.strip()
            s = re.sub(r'^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/', '', s, flags=re.IGNORECASE)
            s = s.split('?')[0].strip('/')
            if s.startswith('+') or s.lower().startswith('joinchat/'):
                return await self._resolve_invite(client, s)
            if s.lower().startswith('s/'):
                s = s[2:]
            s = s.split('/')[0]
            if re.fullmatch(r'-?\d+', s):
                raw_id = _normalize_peer_id(s)
            else:
                username = s.lstrip('@')
                if not username:
                    return None
                try:
                    return await client.get_entity(f"@{username}")
                except Exception as e:
                    logger.warning(f"Could not resolve channel @{username}: {e}")
                    return None
        if not raw_id:
            return None
        try:
            return await client.get_entity(types.PeerChannel(raw_id))
        except Exception:
            logger.debug(f"Channel {raw_id} not in session cache; searching dialogs", exc_info=True)
        try:
            async for dialog in client.iter_dialogs(limit=500):
                if _normalize_peer_id(getattr(dialog.entity, 'id', None)) == raw_id:
                    return dialog.entity
        except Exception as e:
            logger.warning(f"Dialog lookup for channel -100{raw_id} failed: {e}")
        logger.warning(f"Channel -100{raw_id} could not be resolved (the account must be a member of it)")
        return None

    async def _resolve_target_peer(
        self,
        client: TelegramClient,
        peer: Any,
        kwargs: Dict[str, Any],
        user_settings: Optional[StorySettings]
    ) -> Tuple[Any, Optional[str]]:
        """Story destination: explicit peer, the configured channel, or the user's own profile.
        A configured but unusable channel is an error: stories never silently land on the personal profile."""
        if peer:
            return peer, None
        t_type = kwargs.get("target_type")
        t_channel = kwargs.get("target_channel")
        if not t_type and user_settings is not None:
            t_type = user_settings.target_type
            t_channel = user_settings.target_channel
        if t_type != "channel":
            return types.InputPeerSelf(), None
        if not t_channel:
            return None, "Istoriya kanalga joylanishi kerak, lekin kanal sozlanmagan. 'Manba Kanallar' bo'limida Istoriya kanalini kiriting."
        entity = await self._resolve_channel_entity(client, t_channel)
        if entity is None:
            return None, f"Istoriya kanali ({t_channel}) topilmadi yoki hisobingiz unga a'zo emas."
        try:
            return await client.get_input_entity(entity), None
        except Exception as e:
            return None, f"Istoriya kanali ({t_channel}) ga ulanib bo'lmadi: {e}"

    async def _check_can_send_story(self, client: TelegramClient, target_peer: Any) -> Optional[StoryPostResult]:
        """stories.canSendStory before any download / render; returns a failure result or None when allowed"""
        try:
            res = await client(functions.stories.CanSendStoryRequest(peer=target_peer))
        except Exception as e:
            kind, retry_after, msg = classify_story_error(e)
            logger.warning(f"CanSendStory refused ({kind}): {e}")
            return _fail(msg, kind, retry_after)
        remains = getattr(res, "count_remains", None)
        if res is False or (isinstance(remains, int) and not isinstance(remains, bool) and remains <= 0):
            return _fail(
                "Telegram istoriya limiti tugagan (STORIES_TOO_MUCH). 24 soatdan keyin qayta uriniladi.",
                "limit", float(STORIES_LIMIT_RETRY_SECONDS)
            )
        return None

    @staticmethod
    def video_story_media(uploaded_file: Any, duration: float, card_coords: Dict[str, Any]) -> Any:
        """Story video document: streamable, WITH sound (the music track must play), sized as rendered."""
        return types.InputMediaUploadedDocument(
            file=uploaded_file,
            mime_type="video/mp4",
            attributes=[
                types.DocumentAttributeVideo(
                    duration=float(duration),
                    w=int(card_coords.get("video_w", 1080) or 1080),
                    h=int(card_coords.get("video_h", 1920) or 1920),
                    supports_streaming=True,
                    nosound=False
                )
            ],
            nosound_video=False
        )

    @staticmethod
    def post_link_area(card_coords: Dict[str, Any], input_channel: Any, msg_id: int) -> Any:
        """Tappable area over the rendered post card that opens the original channel post (None without a
        channel). Coordinates are percentages of the story frame, as returned by the renderer."""
        if not input_channel:
            return None
        coords = types.MediaAreaCoordinates(
            x=float(card_coords.get("x", 50.0)),
            y=float(card_coords.get("y", 50.0)),
            w=float(card_coords.get("w", 81.5)),
            h=float(card_coords.get("h", 68.0)),
            rotation=0.0,
            radius=2.5
        )
        return types.InputMediaAreaChannelPost(coordinates=coords, channel=input_channel, msg_id=msg_id)

    @staticmethod
    def _is_image_media(m_obj: Any) -> bool:
        if not m_obj:
            return False
        if getattr(m_obj, 'photo', None):
            return True
        doc = getattr(m_obj, 'document', None)
        if doc:
            mime = getattr(doc, 'mime_type', '') or ''
            if isinstance(mime, str) and mime.startswith('image/'):
                return True
            for attr in getattr(doc, 'attributes', None) or []:
                fn = getattr(attr, 'file_name', None)
                if isinstance(fn, str) and fn.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic')):
                    return True
        return False

    @staticmethod
    def _new_job_dir() -> str:
        """Per-publication work directory (protected 'story_' prefix; the temp cleaner never enters directories)"""
        job_dir = os.path.join(_temp_media_dir(), f"{STORY_JOB_DIR_PREFIX}{uuid.uuid4().hex}")
        os.makedirs(job_dir, exist_ok=True)
        return job_dir

    @staticmethod
    def cleanup_stale_job_dirs(max_age: float = STORY_JOB_DIR_MAX_AGE_SECONDS) -> int:
        """Removes work directories left behind by a crash"""
        removed = 0
        temp_dir = _temp_media_dir()
        now = time.time()
        try:
            names = os.listdir(temp_dir)
        except OSError:
            return 0
        for name in names:
            if not name.startswith(STORY_JOB_DIR_PREFIX):
                continue
            path = os.path.join(temp_dir, name)
            try:
                if os.path.isdir(path) and now - os.path.getmtime(path) > max_age:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed

    # ==========================================
    # --- TELEGRAM STORY POSTING (MTPROTO) ---
    # ==========================================

    async def post_story_from_channel(
        self,
        client: Optional[Union[TelegramClient, int]] = None,
        channel_identifier: Optional[Union[str, int, Any]] = None,
        msg_id: Optional[int] = None,
        peer: Optional[Any] = None,
        bg_style: str = "telegram_green",
        source_photo_path: Optional[str] = None,
        badges: Optional[List[str]] = None,
        pinned: Optional[bool] = None,
        **kwargs
    ) -> StoryPostResult:
        """
        Publishes a video story (static composite as fallback) reposting the specified channel message.
        Renders the native repost card (photos collage, header, text, price, avatar, Tashkent date/time) and attaches
        InputMediaAreaChannelPost for 1-tap jumping to the post.
        Returns StoryPostResult - unpacks as (ok, story_id, message, story_url); failures carry error_kind/retry_after.
        badges=None computes smart badges only when the user enabled them; pass [] to render none.
        """
        # Resolve client if user_id is passed
        target_user_id = kwargs.get("user_id")
        if client is None and target_user_id is not None:
            client = target_user_id

        if isinstance(client, int):
            target_user_id = client
            client = await self.get_client_for_user(target_user_id)
            if not client:
                return _fail("Telegram hisobi ulanmagan!", "transient")
        elif target_user_id is None and client is not None:
            for uid, c in self._user_clients.items():
                if c is client:
                    target_user_id = uid
                    break

        if target_user_id and not await db_manager.is_vip(target_user_id):
            return _fail("VIP obunasi talab etiladi yoki muddati tugagan!", "vip")

        if channel_identifier is None and "source_channel" in kwargs:
            channel_identifier = kwargs["source_channel"]

        if client and not client.is_connected():
            try:
                await client.connect()
            except Exception as conn_err:
                logger.warning(f"Client auto-reconnect note: {conn_err}")

        if not client or not client.is_connected():
            return _fail("Telegram hisobi ulanmagan!", "transient")

        user_settings: Optional[StorySettings] = None
        if target_user_id:
            try:
                user_settings = await db_manager.get_story_settings(target_user_id)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        job_dir = self._new_job_dir()
        try:
            # 1. Resolve the source channel with THIS client (entities from other clients carry foreign access hashes)
            full_entity = await self._resolve_channel_entity(client, channel_identifier)
            input_channel = None
            if full_entity is not None:
                try:
                    input_channel = tl_utils.get_input_channel(full_entity)
                except Exception:
                    try:
                        input_channel = tl_utils.get_input_channel(await client.get_input_entity(full_entity))
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
            if not input_channel:
                logger.warning(f"Could not resolve InputChannel for {channel_identifier}. Story will be published without media area post link.")

            # 2. Target peer (own profile or the configured channel; never a silent fallback)
            target_peer, target_err = await self._resolve_target_peer(client, peer, kwargs, user_settings)
            if target_err:
                return _fail(target_err, "permanent")

            # 3. Ask Telegram whether a story can be posted there now, before downloading / rendering anything
            refusal = await self._check_can_send_story(client, target_peer)
            if refusal is not None:
                return refusal

            if full_entity is None and not source_photo_path:
                return _fail(f"Manba kanalni aniqlab bo'lmadi ({channel_identifier})", "transient")

            # 4. Channel metadata (title & avatar)
            channel_title = getattr(full_entity, 'title', '') or "ARENDA UY"
            if not isinstance(channel_title, str):
                channel_title = "ARENDA UY"
            avatar_path = None
            try:
                if full_entity is not None:
                    avatar_path = await client.download_profile_photo(full_entity, file=job_dir + os.sep)
            except Exception as av_err:
                logger.debug(f"Avatar download note: {av_err}")
            if not isinstance(avatar_path, str) or not os.path.exists(avatar_path):
                avatar_path = None

            # 5. Fetch the message and all sibling photos if album
            msg = None
            try:
                if full_entity is not None:
                    msg = await client.get_messages(full_entity, ids=msg_id)
            except Exception as msg_err:
                logger.warning(f"Fetching message #{msg_id} from {channel_identifier} failed: {msg_err}")

            if msg is None and not source_photo_path:
                return _fail(f"Xabar topilmadi yoki o'qib bo'lmadi (#{msg_id})", "not_found")

            photo_paths: List[str] = []
            caption_text = ""
            msg_date = getattr(msg, 'date', None) if msg else None

            async def download(m_obj: Any) -> None:
                if len(photo_paths) >= ALBUM_MAX_PHOTOS or not self._is_image_media(m_obj):
                    return
                try:
                    p_path = await client.download_media(m_obj, file=job_dir + os.sep)
                    if isinstance(p_path, str) and os.path.exists(p_path):
                        photo_paths.append(p_path)
                except Exception as dl_err:
                    logger.warning(f"Error downloading story media #{getattr(m_obj, 'id', '?')}: {dl_err}")

            if msg and getattr(msg, 'grouped_id', None):
                album_msgs = []
                target_id = msg.id
                # 1. Fetch surrounding messages by ID (within +-12 of msg.id)
                try:
                    surrounding_ids = list(range(max(1, target_id - 12), target_id + 13))
                    surrounding = await client.get_messages(full_entity, ids=surrounding_ids)
                    if surrounding:
                        album_msgs = [m for m in surrounding if m and getattr(m, 'grouped_id', None) == msg.grouped_id]
                except Exception as sur_err:
                    logger.debug(f"Surrounding album fetch note: {sur_err}")

                # 2. Fallback: offset_id search if surrounding IDs didn't find multiple items
                if len(album_msgs) <= 1:
                    try:
                        offset_msgs = await client.get_messages(full_entity, limit=30, offset_id=target_id + 10)
                        for om in (offset_msgs or []):
                            if om and getattr(om, 'grouped_id', None) == msg.grouped_id and not any(m.id == om.id for m in album_msgs):
                                album_msgs.append(om)
                    except Exception as off_err:
                        logger.debug(f"Offset album fetch note: {off_err}")

                # 3. Guarantee msg itself is included
                if not any(m.id == msg.id for m in album_msgs):
                    album_msgs.append(msg)

                album_msgs.sort(key=lambda x: x.id)
                for m in album_msgs:
                    m_text = getattr(m, 'message', None)
                    if isinstance(m_text, str) and m_text.strip():
                        clean_m = m_text.strip()
                        if not caption_text or (len(clean_m) > len(caption_text) and len(caption_text) < 40):
                            caption_text = clean_m
                    await download(m)
            elif msg:
                m_text = getattr(msg, 'message', None)
                caption_text = m_text if isinstance(m_text, str) else ""
                await download(msg)

            # Fallback caption from kwargs if Telegram message has no caption text
            if not caption_text:
                caption_text = kwargs.get("caption") or kwargs.get("caption_snippet") or ""

            if source_photo_path and os.path.exists(source_photo_path) and not photo_paths:
                photo_paths.append(source_photo_path)

            # 6. Price & Date & Forward Info
            detected_price = await asyncio.to_thread(self.extract_price, caption_text) if caption_text else None
            if not detected_price and kwargs.get("price"):
                try:
                    detected_price = float(kwargs["price"])
                except (TypeError, ValueError):
                    logger.debug("Ignored exception", exc_info=True)

            d_str = tashkent_date_label(msg_date if isinstance(msg_date, datetime) else None)

            forward_title = await self._forward_title(client, msg)

            valid_photos = [p for p in photo_paths if p and os.path.exists(p)]
            if not valid_photos:
                req_photos = bool(user_settings.require_photos) if user_settings is not None else True
                if req_photos:
                    logger.warning(f"Aborting story post for msg #{msg_id}: no valid photos found and require_photos=True.")
                    return _fail("Xabarda fotosuratlar topilmadi yoki yuklab olinmadi (require_photos faol)", "permanent")

            if badges is None:
                badges_enabled = bool(user_settings.enable_smart_badges) if user_settings is not None else True
                badges = []
                if badges_enabled and caption_text:
                    try:
                        meta = await asyncio.to_thread(listing_analyzer.analyze, caption_text, len(valid_photos), detected_price)
                        badges = meta.smart_badges
                    except Exception:
                        badges = []

            story_duration = float(getattr(user_settings, "video_duration", 25) or 25.0) if user_settings is not None else 25.0
            story_duration = max(float(STORY_VIDEO_DURATION_MIN), min(float(STORY_VIDEO_DURATION_MAX), story_duration))

            # 7. Render: video story, or a static composite (both serialized through the render queue)
            media = None
            card_coords: Dict[str, Any] = {}
            effective_uid = target_user_id or kwargs.get("user_id")
            if valid_photos:
                try:
                    logger.info(f"Generating {story_duration}s video story for msg #{msg_id} with {len(valid_photos)} photos (badges: {badges})...")
                    video_path, card_coords = await render_queue.run_render_job(
                        story_video_generator.create_video_story_async,
                        photo_paths=valid_photos,
                        channel_title=channel_title,
                        caption=caption_text,
                        price=detected_price,
                        date_str=d_str,
                        avatar_path=avatar_path,
                        forward_title=forward_title,
                        badges=badges,
                        duration=story_duration,
                        user_id=effective_uid,
                        output_path=os.path.join(job_dir, "story_video.mp4")
                    )
                    card_coords = dict(card_coords or {})
                    uploaded_file = await client.upload_file(video_path)
                    media = self.video_story_media(uploaded_file, story_duration, card_coords)
                    logger.info(f"Video story media prepared successfully for msg #{msg_id} (duration: {story_duration}s)")
                except Exception as vid_err:
                    logger.error(f"Video story generation failed for msg #{msg_id} ({vid_err}), falling back to static composite photo.", exc_info=True)
                    media = None

            if not media:
                blur_source = source_photo_path or (valid_photos[0] if valid_photos else None)
                bg_base_path = await asyncio.to_thread(self.get_or_create_background, bg_style, blur_source, job_dir)
                if not os.path.exists(bg_base_path):
                    return _fail("Fon rasmi topilmadi!", "transient")
                composite_path, card_coords = await render_queue.run_render_job(
                    story_card_renderer.render_story_composite_with_coords_async,
                    bg_base_path=bg_base_path,
                    channel_title=channel_title,
                    photo_paths=valid_photos,
                    caption=caption_text,
                    price=detected_price,
                    date_str=d_str,
                    avatar_path=avatar_path,
                    forward_title=forward_title,
                    badges=badges,
                    output_path=os.path.join(job_dir, "story_card.jpg")
                )
                card_coords = dict(card_coords or {})
                uploaded_file = await client.upload_file(composite_path)
                media = types.InputMediaUploadedPhoto(file=uploaded_file)

            # 8. Interactive channel post media area (card position returned by the render call itself)
            media_area = self.post_link_area(card_coords, input_channel, msg_id)

            # Final VIP check before sending
            if target_user_id and not await db_manager.is_vip(target_user_id):
                return _fail("VIP obunasi talab etiladi yoki muddati tugagan!", "vip")

            # Pinned status (defaults to True to keep story in profile posts & highlights)
            is_pinned = pinned
            if is_pinned is None:
                is_pinned = bool(user_settings.pin_to_profile) if user_settings is not None else True

            # 9. stories.sendStory (public, 24h)
            request = functions.stories.SendStoryRequest(
                peer=target_peer,
                media=media,
                privacy_rules=[types.InputPrivacyValueAllowAll()],
                pinned=is_pinned,
                period=86400,
                media_areas=[media_area] if media_area else None,
                random_id=random.randint(10000000, 99999999)
            )

            try:
                result = await client(request)
            except Exception as send_err:
                err_text = f"{getattr(send_err, 'message', '')} {send_err}".upper()
                if media_area and any(k in err_text for k in ("MEDIA_AREA", "INPUT_CHANNEL", "CHANNEL_INVALID", "CHANNEL_PRIVATE")):
                    logger.warning(f"SendStory with media_area failed ({send_err}), retrying without the post link area...")
                    request.media_areas = None
                    request.random_id = random.randint(10000000, 99999999)
                    result = await client(request)
                else:
                    raise

            # 10. Extract story ID from updates
            story_id = None
            for upd in getattr(result, 'updates', None) or []:
                if isinstance(upd, types.UpdateStory):
                    story_id = getattr(upd.story, 'id', None)
                    break
                if isinstance(upd, types.UpdateStoryID):
                    story_id = upd.id
                    break

            if story_id and is_pinned:
                try:
                    await client(functions.stories.TogglePinnedRequest(peer=target_peer, id=[story_id], pinned=True))
                except Exception as pin_err:
                    logger.debug(f"TogglePinned note for story #{story_id}: {pin_err}")

            story_url = None
            if story_id:
                try:
                    exported = await client(functions.stories.ExportStoryLinkRequest(peer=target_peer, id=story_id))
                    story_url = getattr(exported, 'link', None)
                    if not isinstance(story_url, str):
                        story_url = None
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            logger.info(f"Telegram Story published for msg #{msg_id} (story ID: {story_id}, user {target_user_id})")
            return StoryPostResult(True, story_id, "Istoriya muvaffaqiyatli joylandi!", story_url)

        except Exception as e:
            kind, retry_after, user_msg = classify_story_error(e)
            if kind == "transient":
                logger.error(f"Error publishing Telegram Story for msg #{msg_id} (user {target_user_id}): {e}", exc_info=True)
            else:
                logger.warning(f"Telegram refused story for msg #{msg_id} (user {target_user_id}, {kind}): {e}")
            return _fail(user_msg, kind, retry_after)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    async def _forward_title(self, client: TelegramClient, msg: Any) -> Optional[str]:
        """Name of the original channel / user of a forwarded post"""
        fwd = getattr(msg, 'fwd_from', None) if msg else None
        if not fwd:
            return None
        from_name = getattr(fwd, 'from_name', None)
        if isinstance(from_name, str) and from_name:
            return from_name
        from_id = getattr(fwd, 'from_id', None)
        if isinstance(from_id, (types.PeerChannel, types.PeerUser, types.PeerChat)):
            try:
                ent = await client.get_entity(from_id)
                title = getattr(ent, 'title', None)
                if not title:
                    title = " ".join(filter(None, [getattr(ent, 'first_name', None), getattr(ent, 'last_name', None)]))
                return title or None
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        return None

    # ==========================================
    # --- USER TELETHON CLIENT & OTP AUTH ---
    # ==========================================

    @staticmethod
    async def _disconnect_quietly(client: Any) -> None:
        if client is None:
            return
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    def _has_active_monitor(self, user_id: int) -> bool:
        task = self._user_poll_tasks.get(user_id)
        return bool(task is not None and not task.done()) or user_id in self._active_channel_handlers

    async def _install_user_client(self, user_id: int, client: TelegramClient) -> None:
        """Puts a client into the pool; a replaced client (and the monitor bound to it) is shut down"""
        old = self._user_clients.get(user_id)
        if old is not None and old is not client:
            self.stop_monitor_for_user(user_id, client=old)
            await self._disconnect_quietly(old)
        self._user_clients.pop(user_id, None)
        self._user_clients[user_id] = client  # most recently used at the end
        await self._evict_idle_clients(protect=user_id)

    async def _evict_idle_clients(self, protect: Optional[int] = None) -> None:
        """LRU eviction beyond CLIENT_POOL_SOFT_LIMIT among clients WITHOUT an active monitor only"""
        while len(self._user_clients) > CLIENT_POOL_SOFT_LIMIT:
            victim = next(
                (uid for uid in self._user_clients if uid != protect and not self._has_active_monitor(uid)),
                None
            )
            if victim is None:
                break  # every pooled client serves a live monitor: exceed the soft limit rather than break one
            old = self._user_clients.pop(victim, None)
            logger.info(f"Evicting idle Telethon client of user {victim} from the story client pool")
            await self._disconnect_quietly(old)

    async def get_client_for_user(self, user_id: int) -> Optional[TelegramClient]:
        """
        Retrieves or initializes an active, authenticated TelegramClient for the user
        (per-user lock: concurrent callers never create two clients for one session).
        Uses the per-user session stored in user_sessions table.
        """
        lock = self._client_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            c = self._user_clients.get(user_id)
            if c is not None:
                # Kept in the pool while reconnecting, so monitors stay bound to it
                if not c.is_connected():
                    try:
                        await c.connect()
                    except Exception as conn_err:
                        logger.warning(f"Reconnecting Telethon client of user {user_id} failed: {conn_err}")
                        return None
                try:
                    if await c.is_user_authorized():
                        self._user_clients.pop(user_id, None)
                        self._user_clients[user_id] = c
                        return c
                except Exception as auth_err:
                    logger.warning(f"Authorization check for user {user_id} failed: {auth_err}")
                    return None
                logger.warning(f"Telegram session of user {user_id} is no longer authorized; dropping the client")
                self.stop_monitor_for_user(user_id, client=c)
                self._user_clients.pop(user_id, None)
                await self._disconnect_quietly(c)

            session_str = await db_manager.get_user_session(user_id)
            if not session_str:
                return None
            client = None
            try:
                user_string_session = StringSession(session_str)
                client = TelegramClient(
                    user_string_session,
                    settings.TELEGRAM_API_ID,
                    settings.TELEGRAM_API_HASH,
                    device_model=DEVICE_MODEL,
                    system_version=SYSTEM_VERSION,
                    app_version=APP_VERSION,
                    lang_code=LANG_CODE,
                    system_lang_code=SYSTEM_LANG_CODE,
                    use_ipv6=session_uses_ipv6(user_string_session)
                )
                await client.connect()
                if await client.is_user_authorized():
                    await self._install_user_client(user_id, client)
                    return client
                logger.warning(f"Stored Telegram session of user {user_id} is not authorized")
            except Exception as e:
                logger.error(f"Error connecting Telethon client for user {user_id}: {e}")
            await self._disconnect_quietly(client)
            return None

    get_user_client = get_client_for_user

    # --- Helpers for the bot UI ---

    def schedule_monitor_start(self, user_id: int) -> asyncio.Task:
        """(Re)starts the user's monitor in the background, e.g. after the story sources changed."""
        return self._spawn(self.start_monitor_for_user(user_id))

    async def cancel_login(self, user_id: int) -> None:
        """Aborts an unfinished OTP login and releases its temporary client."""
        await self._discard_login_session(user_id)

    async def resolve_channel_for_user(self, user_id: int, ref: Any) -> Tuple[Any, Optional[str]]:
        """Resolves a channel reference with the user's own Telegram account.

        Returns (entity, None), or (None, code) with code "not_connected" (no usable session), "not_found"
        (unknown, private without access, expired invite) or "not_channel" (a user or a basic group)."""
        client = await self.get_client_for_user(user_id)
        if client is None or not client.is_connected():
            return None, "not_connected"
        entity = await self._resolve_channel_entity(client, ref)
        if entity is None:
            return None, "not_found"
        if not isinstance(entity, types.Channel):
            return None, "not_channel"
        return entity, None

    async def check_story_target(self, user_id: int, ref: Any) -> Tuple[Any, Optional[str]]:
        """Resolves the channel stories should be posted to and asks Telegram (stories.canSendStory) whether
        the user's account may post there. Returns (entity, None) or (None, Uzbek reason)."""
        entity, code = await self.resolve_channel_for_user(user_id, ref)
        if entity is None:
            return None, {
                "not_connected": "Telegram hisobingiz ulanmagan. Avval 'Telegram Hisob' bo'limida hisobni ulang.",
                "not_found": "Kanal topilmadi yoki hisobingiz unga a'zo emas.",
                "not_channel": "Istoriyani faqat kanalga joylash mumkin.",
            }.get(code, "Kanalni aniqlab bo'lmadi.")
        client = await self.get_client_for_user(user_id)
        if client is None:
            return None, "Telegram hisobingiz ulanmagan. Avval 'Telegram Hisob' bo'limida hisobni ulang."
        try:
            peer = await client.get_input_entity(entity)
        except Exception as e:
            return None, f"Kanalga ulanib bo'lmadi: {e}"
        refusal = await self._check_can_send_story(client, peer)
        if refusal is not None:
            return None, refusal[2]
        return entity, None

    @staticmethod
    def channel_label(entity: Any) -> str:
        """'@username' of a channel, or its '-100<id>' form for channels without a public username."""
        return StoryClonerService._entity_label(entity)

    async def is_premium_account(self, user_id: int) -> Optional[bool]:
        """Telegram Premium status of the user's account (cached for an hour); None when unknown"""
        cached = self._premium_cache.get(user_id)
        if cached is not None and time.monotonic() - cached[1] < PREMIUM_CACHE_TTL_SECONDS:
            return cached[0]
        client = await self.get_client_for_user(user_id)
        if not client:
            return None
        try:
            me = await client.get_me()
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
            return None
        if me is None:
            return None
        premium = bool(getattr(me, "premium", False))
        self._premium_cache[user_id] = (premium, time.monotonic())
        return premium

    async def get_effective_daily_limit(self, user_id: int, settings: StorySettings) -> int:
        """User's daily story limit, capped at NON_PREMIUM_DAILY_STORY_LIMIT for accounts without Premium"""
        try:
            limit = int(settings.max_stories_per_day)
        except (TypeError, ValueError):
            limit = 5
        limit = max(STORY_MAX_PER_DAY_MIN, min(STORY_MAX_PER_DAY_MAX, limit))
        if await self.is_premium_account(user_id) is False:
            limit = min(limit, NON_PREMIUM_DAILY_STORY_LIMIT)
        return limit

    async def get_publish_backoff_until(self, user_id: int) -> Optional[float]:
        """Unix time until which Telegram asked this account not to post stories (FloodWait / story limits)"""
        until = self._publish_backoff_until.get(user_id)
        if until is None:
            return None
        if until <= time.time():
            self._publish_backoff_until.pop(user_id, None)
            return None
        return until

    def _set_publish_backoff(self, user_id: int, seconds: float) -> float:
        until = time.time() + max(30.0, float(seconds))
        self._publish_backoff_until[user_id] = max(until, self._publish_backoff_until.get(user_id, 0.0))
        return self._publish_backoff_until[user_id]

    def _expire_login_sessions(self) -> None:
        """Drops login attempts older than LOGIN_SESSION_TTL_SECONDS and disconnects their clients"""
        now = time.time()
        for uid, sess in list(self._login_sessions.items()):
            if now - float(sess.get("created_at", now)) > LOGIN_SESSION_TTL_SECONDS:
                self._login_sessions.pop(uid, None)
                old_c = sess.get("client")
                if old_c is not None:
                    self._spawn(self._disconnect_quietly(old_c))

    async def _discard_login_session(self, user_id: int) -> None:
        sess = self._login_sessions.pop(user_id, None)
        if sess:
            await self._disconnect_quietly(sess.get("client"))

    async def request_otp_code(self, user_id: int, phone: str) -> Tuple[bool, str]:
        """Initiates MTProto phone code verification for user"""
        if not settings.is_configured():
            return False, "TELEGRAM_API_ID va TELEGRAM_API_HASH sozlanmagan!"

        now_ts = time.monotonic()
        last_req = self._otp_cooldowns.get(user_id, 0.0)
        if last_req and now_ts - last_req < 60.0:
            remaining = int(60.0 - (now_ts - last_req))
            return False, f"Juda tez! Iltimos {remaining} soniya kuting va qaytadan urinib ko'ring."

        is_valid, phone_e164, _ = normalize_phone_number(phone)
        if not is_valid:
            return False, "Telefon raqami noto'g'ri formatda! Xalqaro formatda kiriting (masalan: +998901234567)."

        self._expire_login_sessions()
        await self._discard_login_session(user_id)

        login_session = StringSession("")
        client = TelegramClient(
            login_session,
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH,
            device_model=DEVICE_MODEL,
            system_version=SYSTEM_VERSION,
            app_version=APP_VERSION,
            lang_code=LANG_CODE,
            system_lang_code=SYSTEM_LANG_CODE,
            use_ipv6=session_uses_ipv6(login_session)
        )
        try:
            await client.connect()
            sent_code = await client.send_code_request(phone_e164)
        except FloodWaitError as e:
            await self._disconnect_quietly(client)
            return False, f"Telegram cheklovi: Iltimos {e.seconds} soniya kuting."
        except Exception as e:
            await self._disconnect_quietly(client)
            logger.error(f"Error requesting OTP for user {user_id}: {e}")
            return False, f"Xatolik: {e}"

        self._login_sessions[user_id] = {
            "client": client,
            "phone": phone_e164,
            "phone_code_hash": sent_code.phone_code_hash,
            "created_at": time.time()
        }
        self._otp_cooldowns[user_id] = time.monotonic()
        return True, "Tasdiqlash kodi Telegram akkauntingizga yuborildi."

    def _login_session_for(self, user_id: int) -> Optional[Dict[str, Any]]:
        self._expire_login_sessions()
        return self._login_sessions.get(user_id)

    async def _finish_login(self, user_id: int, client: TelegramClient, phone: str) -> Any:
        me = await client.get_me()
        session_str = client.session.save()
        await db_manager.save_user_session(
            user_id=user_id,
            session_str=session_str,
            phone=phone,
            first_name=getattr(me, 'first_name', '') or '',
            last_name=getattr(me, 'last_name', '') or '',
            username=getattr(me, 'username', '') or ''
        )
        self._login_sessions.pop(user_id, None)
        self._premium_cache.pop(user_id, None)
        await self._install_user_client(user_id, client)
        # Auto-start monitoring if configured
        self._spawn(self.start_monitor_for_user(user_id))
        return me

    async def submit_otp_code(self, user_id: int, code: str) -> Tuple[bool, str, str]:
        """Submits phone code. Returns (ok, message, status: 'success'|'needs_2fa'|'error')"""
        sess = self._login_session_for(user_id)
        if not sess:
            return False, "Sessiya topilmadi yoki muddati tugagan! Telefon raqamingizni qaytadan kiriting.", "error"

        client: TelegramClient = sess["client"]
        phone = sess["phone"]
        phone_code_hash = sess["phone_code_hash"]

        code_clean = "".join(c for c in code if c.isdigit())

        try:
            await client.sign_in(phone, code_clean, phone_code_hash=phone_code_hash)
            me = await self._finish_login(user_id, client, phone)
            name = getattr(me, 'first_name', '') or 'Foydalanuvchi'
            return True, f"Hisob muvaffaqiyatli ulandi: {name}!", "success"

        except SessionPasswordNeededError:
            return False, "Ikki bosqichli autentifikatsiya (2FA Cloud Password) yoqilgan.", "needs_2fa"
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as pe:
            return False, f"Tasdiqlash kodi xato yoki muddati o'tgan: {pe}", "error"
        except Exception as e:
            return False, f"Xatolik: {e}", "error"

    async def submit_2fa_password(self, user_id: int, password: str) -> Tuple[bool, str]:
        """Submits 2FA password"""
        sess = self._login_session_for(user_id)
        if not sess:
            return False, "Sessiya topilmadi yoki muddati tugagan! Qaytadan telefon raqam kiriting."

        client: TelegramClient = sess["client"]
        phone = sess.get("phone", "")

        try:
            await client.sign_in(password=password.strip())
            me = await self._finish_login(user_id, client, phone)
            name = getattr(me, 'first_name', '') or ''
            return True, f"2FA tasdiqlandi! Hisob ulandi: {name}"

        except PasswordHashInvalidError:
            return False, "2FA parol noto'g'ri kiritildi!"
        except Exception as e:
            return False, f"Xatolik: {e}"

    async def disconnect_user(self, user_id: int) -> bool:
        """Stops monitoring, logs the account out on Telegram's side (also when the client was not pooled) and
        deletes the stored session"""
        self.stop_monitor_for_user(user_id)
        client = self._user_clients.get(user_id)
        if client is None:
            try:
                client = await self.get_client_for_user(user_id)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
                client = None
        if client is not None:
            try:
                if not client.is_connected():
                    await client.connect()
                if await client.is_user_authorized():
                    await client.log_out()
            except Exception as e:
                logger.warning(f"Error during logout for user {user_id}: {e}")
            await self._disconnect_quietly(client)
        self._user_clients.pop(user_id, None)
        await self._discard_login_session(user_id)
        self._otp_cooldowns.pop(user_id, None)
        self._processing_locks.pop(user_id, None)
        self._premium_cache.pop(user_id, None)
        self._publish_backoff_until.pop(user_id, None)
        self._restart_backoff.pop(user_id, None)
        await db_manager.delete_user_session(user_id)
        return True

    # ==========================================
    # --- AUTO-MONITORING & REAL-TIME ENGINE ---
    # ==========================================

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._processing_locks:
            self._processing_locks[user_id] = asyncio.Lock()
        return self._processing_locks[user_id]

    @staticmethod
    def _source_key(chat_username: str, chat_raw_id: Optional[int], settings: StorySettings) -> str:
        """Canonical source channel stored with stories / queue rows: @username, else -100<id> (never a title)"""
        if chat_username:
            return f"@{chat_username.lstrip('@')}"
        if chat_raw_id:
            return f"-100{chat_raw_id}"
        return settings.source_channel or "unknown"

    async def _already_handled(self, user_id: int, source_key: str, chat_raw_id: Optional[int], msg_id: int, grouped_id: Any) -> bool:
        if await db_manager.is_story_posted(
            user_id=user_id,
            source_channel=source_key,
            source_msg_id=msg_id,
            source_id=chat_raw_id,
            grouped_id=grouped_id
        ):
            return True
        try:
            return await story_queue_service.is_queued(user_id, msg_id, source_key, chat_raw_id)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
            return False

    async def _estimate_photo_count(self, client: TelegramClient, chat: Any, message: Any) -> int:
        """Photos of the post: albums are counted from their sibling messages (the quality score depends on it)"""
        grouped_id = getattr(message, 'grouped_id', None)
        if not grouped_id:
            return 1 if (self._is_image_media(message) or self.has_media(message)) else 0
        try:
            ids = list(range(max(1, message.id - 9), message.id + 10))
            siblings = await client.get_messages(chat, ids=ids)
            count = sum(
                1 for m in (siblings or [])
                if m and getattr(m, 'grouped_id', None) == grouped_id and self._is_image_media(m)
            )
            return max(count, 1)
        except Exception:
            return 2

    async def _notify(self, user_id: int, text: str) -> None:
        if not self._bot_instance:
            return
        try:
            await self._bot_instance.send_message(chat_id=user_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as ne:
            logger.warning(f"Failed to notify user {user_id}: {ne}")

    async def _enqueue_listing(
        self,
        user_id: int,
        settings: StorySettings,
        chat: Any,
        chat_raw_id: Optional[int],
        chat_username: str,
        source_key: str,
        message: Any,
        caption_text: str,
        price: Optional[float],
        meta: Any,
        badges: List[str],
        not_before: Optional[datetime] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
        notify: bool = True
    ) -> Optional[str]:
        payload = {
            "source_id": chat_raw_id,
            "channel_id": chat_raw_id,
            "channel_username": chat_username,
            "channel_title": getattr(chat, 'title', '') or settings.source_channel,
            "badges": list(badges),
            "caption_snippet": caption_text[:150],
            "fingerprint": meta.fingerprint or "",
            "grouped_id": getattr(message, 'grouped_id', None),
        }
        if extra_payload:
            payload.update(extra_payload)
        try:
            _item_id, sched_str = await story_queue_service.enqueue_listing(
                user_id=user_id,
                source_channel=source_key,
                source_msg_id=message.id,
                payload=payload,
                price=price,
                district=meta.district or "",
                rooms=meta.rooms,
                area=meta.area,
                score=meta.quality_score,
                not_before=not_before
            )
        except PermissionError:
            logger.info(f"Listing #{message.id} not queued for user {user_id}: VIP subscription inactive")
            return None
        if meta.fingerprint:
            await db_manager.record_listing_hash(user_id, meta.fingerprint, source_key, message.id)

        if notify:
            try:
                sched_local = datetime.strptime(sched_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(TASHKENT_TZ)
                sched_label = sched_local.strftime("%d.%m.%Y %H:%M")
            except ValueError:
                sched_label = sched_str
            text = f"""
{INBOX} <b>Yangi sara variant Navbatga olindi!</b>

├ {TROPHY} <b>Sifat bali:</b> <b>{meta.quality_score}/100</b>
├ {CHANNEL} <b>Manba kanal:</b> {html.escape(source_key)}
├ {MONEY} <b>Narxi:</b> <b>{format_price_usd(price)}</b>
├ {LOCATION} <b>Hudud:</b> {html.escape(meta.district or "Aniqlanmadi")}
└ {TIMER} <b>Rejalashtirilgan vaqt:</b> {sched_label} (Toshkent vaqti)

<i>Auditoriyangizni spam qilmaslik va maksimal ko'rishlar uchun post navbatga rejalashtirildi.</i>
"""
            await self._notify(user_id, text)
        return sched_str

    async def _handle_live_failure(
        self,
        user_id: int,
        settings: StorySettings,
        result: Any,
        res_msg: str,
        chat: Any,
        chat_raw_id: Optional[int],
        chat_username: str,
        source_key: str,
        message: Any,
        caption_text: str,
        price: Optional[float],
        meta: Any,
        badges: List[str]
    ) -> None:
        """Failed live publication: permanent errors are recorded (never retried), limits / FloodWait set an
        account back-off and hand the listing to the queue, other errors are retried by the queue later"""
        kind = getattr(result, "error_kind", None) or "transient"
        retry_after = getattr(result, "retry_after", None)
        if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
            retry_after = None
        logger.warning(f"Story publish failed for user {user_id}, channel {source_key}, msg #{message.id} ({kind}): {res_msg}")

        if kind in ("limit", "flood"):
            until = self._set_publish_backoff(user_id, retry_after or STORIES_LIMIT_RETRY_SECONDS)
            await self._enqueue_listing(
                user_id, settings, chat, chat_raw_id, chat_username, source_key, message, caption_text, price, meta, badges,
                not_before=datetime.fromtimestamp(until, tz=timezone.utc), extra_payload={"deferrals": 1}, notify=False
            )
            await self._notify_failure_once(user_id, kind, res_msg)
            return
        if kind == "transient":
            await self._enqueue_listing(
                user_id, settings, chat, chat_raw_id, chat_username, source_key, message, caption_text, price, meta, badges,
                not_before=datetime.now(timezone.utc) + timedelta(seconds=TRANSIENT_RETRY_SECONDS),
                extra_payload={"retries": 1}, notify=False
            )
            return

        await db_manager.record_posted_story(
            user_id=user_id,
            source_channel=source_key,
            source_id=chat_raw_id,
            source_msg_id=message.id,
            story_id=None,
            price=price,
            caption_snippet=caption_text[:150],
            target_type=settings.target_type,
            status="failed",
            grouped_id=getattr(message, 'grouped_id', None)
        )
        if kind != "not_found":
            await self._notify_failure_once(user_id, kind, res_msg)

    async def _notify_failure_once(self, user_id: int, kind: str, res_msg: str) -> None:
        """Tells the user why stories are not being published, at most once per FAILURE_NOTICE_INTERVAL per error"""
        key = (user_id, f"{kind}:{res_msg[:60]}")
        now = time.time()
        if now - self._failure_notices.get(key, 0.0) < FAILURE_NOTICE_INTERVAL_SECONDS:
            return
        self._failure_notices[key] = now
        await self._notify(
            user_id,
            f"{WARN} <b>Istoriya joylanmadi</b>\n\n{html.escape(str(res_msg))}"
        )

    async def _process_channel_message(
        self,
        client: TelegramClient,
        user_id: int,
        settings: StorySettings,
        message: Any,
        chat: Any
    ):
        """Processes a single incoming message from any monitored source channel.
        `chat` must be an entity resolved by THIS user's client."""
        if not message or getattr(message, 'action', None) is not None:
            return

        # 0. Strict VIP restriction
        if not await db_manager.is_vip(user_id):
            logger.info(f"Dropping incoming channel message for user {user_id}: VIP subscription not active")
            self.stop_monitor_for_user(user_id)
            return

        chat_raw_id = _normalize_peer_id(getattr(chat, 'id', None))
        chat_username = getattr(chat, 'username', '') or ''
        if not isinstance(chat_username, str):
            chat_username = ''
        chat_username = chat_username.lstrip('@')
        source_key = self._source_key(chat_username, chat_raw_id, settings)
        grouped_id = getattr(message, 'grouped_id', None)

        # 1. Message-level deduplication (posted, failed for good, or already waiting in the queue)
        if await self._already_handled(user_id, source_key, chat_raw_id, message.id, grouped_id):
            return

        # 2. Filter (demand inquiry, photos, price) off the event loop
        is_match, reason, price = await asyncio.to_thread(self.matches_filter, message, settings)
        if not is_match:
            logger.debug(f"Message #{message.id} skipped for user {user_id}: {reason}")
            return

        caption_text = getattr(message, 'message', '') or getattr(message, 'text', '') or ""
        if not isinstance(caption_text, str):
            caption_text = ""
        photo_count = await self._estimate_photo_count(client, chat, message)

        # 3. Listing metadata, quality score, smart badges and fingerprint
        meta = await asyncio.to_thread(listing_analyzer.analyze, caption_text, photo_count, price)

        # 4. Cross-channel listing deduplication (14 days; posts without identifying content have no fingerprint)
        if meta.fingerprint and await db_manager.is_listing_duplicate(user_id, meta.fingerprint, days=14):
            logger.info(f"Duplicate real estate listing ignored for user {user_id} (fingerprint {meta.fingerprint})")
            return

        lock = self._get_user_lock(user_id)
        async with lock:
            # Double-check deduplication inside lock
            if await self._already_handled(user_id, source_key, chat_raw_id, message.id, grouped_id):
                return
            if meta.fingerprint and await db_manager.is_listing_duplicate(user_id, meta.fingerprint, days=14):
                return

            badges_to_use = list(meta.smart_badges) if settings.enable_smart_badges else []

            # 5. Drip-feed: cooldown, daily limit, Telegram back-off and prime hours all route through the queue
            now_uzb = story_queue_service.get_uzb_now()
            in_prime = story_queue_service.is_in_prime_hours(settings.prime_hours_start, settings.prime_hours_end, now_uzb)
            last_posted_utc = await db_manager.get_last_posted_story_time(user_id)
            try:
                delay_minutes = int(settings.drip_delay_minutes or 0)
            except (TypeError, ValueError):
                delay_minutes = 45
            cooldown = timedelta(minutes=max(0, min(STORY_DRIP_DELAY_MAX_MINUTES, delay_minutes)))
            is_in_cooldown = bool(last_posted_utc and (datetime.now(timezone.utc) - last_posted_utc) < cooldown)
            daily_limit = await self.get_effective_daily_limit(user_id, settings)
            is_daily_limit = await story_queue_service.count_posted_today(user_id) >= daily_limit
            backoff_until = await self.get_publish_backoff_until(user_id)
            outside_prime = bool(settings.prime_hours_enabled and not in_prime)

            if is_in_cooldown or is_daily_limit or backoff_until or outside_prime:
                not_before = datetime.fromtimestamp(backoff_until, tz=timezone.utc) if backoff_until else None
                await self._enqueue_listing(
                    user_id, settings, chat, chat_raw_id, chat_username, source_key, message, caption_text, price, meta,
                    badges_to_use, not_before=not_before
                )
                return

            # If album, wait briefly for all sibling media messages to arrive
            if grouped_id:
                await asyncio.sleep(1.5)

            result = await self.post_story_from_channel(
                client=client,
                channel_identifier=chat,
                msg_id=message.id,
                bg_style=settings.background_style,
                badges=badges_to_use,
                user_id=user_id,
                pinned=settings.pin_to_profile,
                target_type=settings.target_type,
                target_channel=settings.target_channel
            )
            ok, story_id, res_msg = result[0], result[1], result[2]
            story_url = result[3] if len(result) > 3 else None

            if not ok:
                await self._handle_live_failure(
                    user_id, settings, result, str(res_msg), chat, chat_raw_id, chat_username, source_key, message,
                    caption_text, price, meta, badges_to_use
                )
                return

            await db_manager.record_posted_story(
                user_id=user_id,
                source_channel=source_key,
                source_id=chat_raw_id,
                source_msg_id=message.id,
                story_id=story_id,
                price=price,
                caption_snippet=caption_text[:150],
                target_type=settings.target_type,
                status="success",
                grouped_id=grouped_id
            )
            if meta.fingerprint:
                await db_manager.record_listing_hash(user_id, meta.fingerprint, source_key, message.id)

            story_link_html = f'\n{LINK} <a href="{html.escape(story_url, quote=True)}">Istoriyani ochish</a>' if isinstance(story_url, str) and story_url else ''
            badges_str = " ".join(f"<code>[{html.escape(str(b))}]</code>" for b in badges_to_use)
            badges_line = f"\n├ {TAG} <b>Badjlar:</b> {badges_str}" if badges_str else ""
            text = f"""
{PARTY} <b>Yangi sara variant Istoriyaga joylandi!</b>

├ {TROPHY} <b>Sifat bali:</b> <b>{meta.quality_score}/100</b>
├ {CHANNEL} <b>Manba kanal:</b> {html.escape(source_key)}
├ {MONEY} <b>Narxi:</b> <b>{format_price_usd(price)}</b>{badges_line}
├ {INFO} <b>Xabar ID:</b> <code>{message.id}</code>
└ {MOBILE} <b>Format:</b> Nativ Repost Story (bosganda post ochiladi){story_link_html}
"""
            await self._notify(user_id, text)

    async def _user_channel_poll_loop(self, user_id: int, client: TelegramClient, entities: List[Any]):
        """Background watchdog checking fresh posts of all monitored channels (catches posts missed while offline).
        Ends with a log line; the supervisor restarts it while monitoring stays enabled."""
        logger.info(f"Watchdog poll loop started for user {user_id} with {len(entities)} channels")
        stop_reason = "cancelled"
        while True:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
                st = await db_manager.get_story_settings(user_id)
                if not st.is_active:
                    stop_reason = "monitoring disabled"
                    break

                if not client.is_connected():
                    try:
                        await client.connect()
                    except Exception as conn_err:
                        logger.warning(f"Watchdog reconnect for user {user_id} failed: {conn_err}")
                if not client.is_connected():
                    stop_reason = "Telegram client disconnected"
                    break
                if not await client.is_user_authorized():
                    stop_reason = "Telegram session no longer authorized"
                    break

                # VIP check during watchdog loop
                if not await db_manager.is_vip(user_id):
                    stop_reason = "VIP subscription inactive"
                    break

                now_utc = datetime.now(timezone.utc)
                started_at = self._monitor_started_at.get(user_id) or now_utc
                cutoff = max(started_at - timedelta(minutes=1), now_utc - WATCHDOG_MAX_MESSAGE_AGE)

                for entity in entities:
                    label = self._entity_label(entity)
                    try:
                        messages = await client.get_messages(entity, limit=5)
                        for msg in reversed(list(messages or [])):
                            msg_date = getattr(msg, 'date', None)
                            if isinstance(msg_date, datetime):
                                if msg_date.tzinfo is None:
                                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                                if msg_date < cutoff:
                                    continue
                            await self._process_channel_message(client, user_id, st, msg, entity)
                    except FloodWaitError as fwe:
                        logger.warning(f"Watchdog FloodWait for user {user_id}, channel {label}: sleeping {fwe.seconds}s")
                        await asyncio.sleep(min(float(fwe.seconds), 300.0))
                    except ChannelPrivateError as cpe:
                        logger.warning(f"Watchdog: channel {label} is private / inaccessible for user {user_id}: {cpe}")
                    except Exception as ent_err:
                        logger.warning(f"Watchdog check failed for user {user_id}, channel {label}: {ent_err}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Watchdog iteration failed for user {user_id}: {e}")
                await asyncio.sleep(15.0)
        if stop_reason != "cancelled":
            logger.warning(f"Watchdog poll loop for user {user_id} stopped: {stop_reason}")

    async def start_monitor_for_user(self, user_id: int) -> bool:
        """Starts real-time MTProto monitoring and periodic watchdog across all configured user channels"""
        # Strict VIP restriction
        if not await db_manager.is_vip(user_id):
            logger.info(f"User {user_id} is not an active VIP subscriber. Monitoring skipped.")
            self.stop_monitor_for_user(user_id)
            return False

        st = await db_manager.get_story_settings(user_id)
        if not st.is_active:
            return False

        try:
            client = await self.get_client_for_user(user_id)
            if not client or not client.is_connected():
                logger.warning(f"User {user_id} Telethon client not connected; story monitoring deferred.")
                return False

            # Gather all channels (primary source + multi-channel entries)
            channels_to_resolve = []
            if st.source_channel:
                channels_to_resolve.append(st.source_channel)
            extra_channels = await db_manager.get_story_source_channels(user_id)
            for ec in extra_channels:
                if ec.is_active and ec.channel_username not in channels_to_resolve:
                    channels_to_resolve.append(ec.channel_username)

            if not channels_to_resolve:
                return False

            resolved_entities = []
            for ch in channels_to_resolve:
                try:
                    entity = await self._resolve_channel_entity(client, ch)
                    if entity is None:
                        logger.warning(f"Could not resolve story source channel {ch} for user {user_id}")
                        continue
                    if isinstance(ch, str) and not re.match(r'^(?:https?://)?(?:t\.me/)?(?:\+|joinchat/)', ch.strip(), re.IGNORECASE):
                        try:
                            await client(functions.channels.JoinChannelRequest(channel=entity))
                        except UserAlreadyParticipantError:
                            pass
                        except Exception as join_e:
                            logger.debug(f"Join channel {ch} skipped or not needed: {join_e}")
                    resolved_entities.append(entity)
                except Exception as ent_e:
                    logger.warning(f"Could not resolve channel {ch} for user {user_id}: {ent_e}")

            if not resolved_entities:
                return False

            # Stop existing poll task and unbind previous handler from client
            self.stop_monitor_for_user(user_id, client=client)

            # Register real-time event handler across all resolved channels
            async def on_new_channel_message(event):
                try:
                    if not await db_manager.is_vip(user_id):
                        logger.info(f"User {user_id} is no longer VIP. Deactivating story monitor.")
                        self.stop_monitor_for_user(user_id, client=client)
                        return
                    current_st = await db_manager.get_story_settings(user_id)
                    if current_st.is_active:
                        chat = await event.get_chat()
                        await self._process_channel_message(client, user_id, current_st, event.message, chat)
                except Exception as he:
                    logger.warning(f"Error handling channel event for user {user_id} (chat {getattr(event, 'chat_id', '?')}): {he}")

            h_res = client.add_event_handler(on_new_channel_message, events.NewMessage(chats=resolved_entities))
            if inspect.iscoroutine(h_res):
                await h_res
            self._active_channel_handlers[user_id] = on_new_channel_message
            self._monitored_ids[user_id] = {
                rid for rid in (_normalize_peer_id(getattr(e, 'id', None)) for e in resolved_entities) if rid
            }
            self._monitor_started_at[user_id] = datetime.now(timezone.utc)

            # Start watchdog poll task covering all entities
            task = asyncio.create_task(self._user_channel_poll_loop(user_id, client, resolved_entities))
            self._user_poll_tasks[user_id] = task

            logger.info(f"Real-time Multi-Channel Monitoring ACTIVE for user {user_id} ({len(resolved_entities)} channels)")
            return True

        except Exception as e:
            logger.warning(f"Could not start monitor for user {user_id} ({st.source_channel}): {e}")
            return False

    def stop_monitor_for_user(self, user_id: int, client: Optional[Any] = None):
        """Stops background watchdog and MTProto event handler for user (the client itself stays connected)"""
        task = self._user_poll_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        handler = self._active_channel_handlers.pop(user_id, None)
        self._monitored_ids.pop(user_id, None)
        cli = client or self._user_clients.get(user_id)
        if cli and handler:
            try:
                cli.remove_event_handler(handler)
            except Exception as e:
                logger.debug(f"Could not remove event handler for user {user_id}: {e}")

    def _monitor_is_healthy(self, user_id: int) -> bool:
        task = self._user_poll_tasks.get(user_id)
        client = self._user_clients.get(user_id)
        if task is None or task.done() or client is None:
            return False
        try:
            return bool(client.is_connected())
        except Exception:
            return False

    def _ensure_supervisor(self) -> None:
        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = asyncio.create_task(self._monitor_supervisor_loop())

    async def _monitor_supervisor_loop(self):
        """Restarts dead story monitors (finished watchdog or disconnected client) with exponential back-off,
        expires abandoned logins and removes work directories left by crashes"""
        while True:
            try:
                await asyncio.sleep(SUPERVISOR_INTERVAL_SECONDS)
                self._expire_login_sessions()
                if time.time() - self._last_job_dir_sweep > 3600:
                    self._last_job_dir_sweep = time.time()
                    await asyncio.to_thread(self.cleanup_stale_job_dirs)

                active = await db_manager.get_all_active_story_settings()
                active_ids = set()
                for st in active:
                    uid = st.user_id
                    active_ids.add(uid)
                    if self._monitor_is_healthy(uid):
                        self._restart_backoff.pop(uid, None)
                        continue
                    next_try, delay = self._restart_backoff.get(uid, (0.0, SUPERVISOR_INTERVAL_SECONDS))
                    if time.monotonic() < next_try:
                        continue
                    if not await db_manager.is_vip(uid):
                        continue
                    logger.warning(f"Story monitor for user {uid} is not running; restarting it")
                    if await self.start_monitor_for_user(uid):
                        self._restart_backoff.pop(uid, None)
                    else:
                        delay = min(delay * 2, SUPERVISOR_MAX_BACKOFF_SECONDS)
                        self._restart_backoff[uid] = (time.monotonic() + delay, delay)
                        logger.warning(f"Restarting the story monitor of user {uid} failed; next attempt in {int(delay)}s")
                for uid in list(self._restart_backoff):
                    if uid not in active_ids:
                        self._restart_backoff.pop(uid, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Story monitor supervisor error: {e}", exc_info=True)

    async def start_all_active_monitors(self):
        """Loads and starts monitoring for all active users on bot startup, then keeps them alive (supervisor)"""
        try:
            await asyncio.to_thread(self.cleanup_stale_job_dirs)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
        self._ensure_supervisor()
        try:
            active_settings = await db_manager.get_all_active_story_settings()
            logger.info(f"Initializing {len(active_settings)} active story monitors...")
            for st in active_settings:
                if not st.is_active:
                    continue
                # Strict VIP check on startup
                if await db_manager.is_vip(st.user_id):
                    self._spawn(self.start_monitor_for_user(st.user_id))
                else:
                    logger.info(f"Skipping monitor for user {st.user_id}: not an active VIP subscriber")
                    try:
                        await db_manager.update_story_settings(st.user_id, is_active=False)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
        except Exception as e:
            logger.error(f"Error starting active story monitors: {e}")

    # ==========================================
    # --- INSTANT TEST STORY & LATEST POST ---
    # ==========================================

    async def test_publish_latest_post(
        self,
        user_id: int
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Fetches recent listings from the configured source channel, finds one matching the user's filters
        and immediately posts it as a Telegram Story (recorded like any other story, so it is never reposted).
        """
        if not await db_manager.is_vip(user_id):
            return False, "Ko'chmas Mulk Auto-Story Cloner faqat VIP Cheksiz tarif egalari uchun! Iltimos, VIP tarifga o'ting.", None

        client = await self.get_client_for_user(user_id)
        if not client or not client.is_connected():
            return False, "Telegram hisobingiz ulanmagan! Avval 'Telegram Akkauntni Ulash' bo'limidan hisobingizni ulang.", None

        st = await db_manager.get_story_settings(user_id)
        if not st.source_channel:
            return False, "Manba kanal sozlanmagan! Avval 'Manba Kanal' bo'limida kanalingizni kiriting.", None

        entity = await self._resolve_channel_entity(client, st.source_channel)
        if entity is None:
            return False, f"Kanalga ulanib bo'lmadi ({st.source_channel}).", None

        try:
            messages = await client.get_messages(entity, limit=25)
        except Exception as e:
            return False, f"Xabarlarni o'qib bo'lmadi: {e}", None

        chat_raw_id = _normalize_peer_id(getattr(entity, 'id', None))
        chat_username = getattr(entity, 'username', '') or ''
        source_key = self._source_key(chat_username if isinstance(chat_username, str) else '', chat_raw_id, st)

        matching_msg = None
        detected_price = None
        for msg in messages or []:
            if not msg or getattr(msg, 'action', None) is not None:
                continue
            is_match, _reason, price = await asyncio.to_thread(self.matches_filter, msg, st)
            if is_match:
                matching_msg = msg
                detected_price = price
                break

        if not matching_msg:
            return False, (
                f"Kanaldagi oxirgi 25 ta post ichida narxi {format_price_usd(st.min_price, '$0')}+ bo'lgan rasmli e'lon topilmadi. "
                f"Kanalga mos e'lon joylang yoki test qilish uchun vaqtincha minimal narxni pasaytirib ko'ring."
            ), None

        grouped_id = getattr(matching_msg, 'grouped_id', None)
        if await self._already_handled(user_id, source_key, chat_raw_id, matching_msg.id, grouped_id):
            return False, "Bu e'lon allaqachon Istoriyaga joylangan yoki navbatda turibdi.", None

        ok, story_id, res_msg, story_url = await self.post_story_from_channel(
            client=client,
            channel_identifier=entity,
            msg_id=matching_msg.id,
            bg_style=st.background_style,
            user_id=user_id,
            pinned=st.pin_to_profile
        )

        if not ok:
            return False, res_msg, None
        caption_snip = (getattr(matching_msg, 'message', '') or '')[:150]
        await db_manager.record_posted_story(
            user_id=user_id,
            source_channel=source_key,
            source_id=chat_raw_id,
            source_msg_id=matching_msg.id,
            story_id=story_id,
            price=detected_price,
            caption_snippet=caption_snip,
            target_type=st.target_type,
            status="success",
            grouped_id=grouped_id
        )
        detail = {
            "msg_id": matching_msg.id,
            "story_id": story_id,
            "price": detected_price,
            "story_url": story_url,
            "caption": caption_snip,
            "source_channel": source_key
        }
        return True, res_msg, detail

    # ==========================================
    # --- CENTRAL LISTENER DISPATCHER ---
    # ==========================================

    async def _source_matches(self, st: StorySettings, chat_raw_id: Optional[int], username: str) -> bool:
        """True when the channel (raw id / lowercase username) is one of the user's story sources"""
        def matches(channel_ref: Any, channel_id: Any = None) -> bool:
            ref_raw = _normalize_peer_id(channel_id) or _normalize_peer_id(channel_ref)
            if chat_raw_id and ref_raw and ref_raw == chat_raw_id:
                return True
            if username and isinstance(channel_ref, str):
                name = re.sub(r'^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/', '', channel_ref.strip(), flags=re.IGNORECASE)
                return name.lstrip('@').split('/')[0].lower() == username
            return False

        if matches(st.source_channel, st.source_id):
            return True
        try:
            extra = await db_manager.get_story_source_channels(st.user_id)
        except Exception:
            return False
        return any(ec.is_active and matches(ec.channel_username, ec.channel_id) for ec in extra)

    async def handle_incoming_channel_message(self, message: Any, chat: Any):
        """
        Invoked by the central Telethon listener whenever a new message appears in any channel.
        Users whose own monitor covers the channel are skipped (their client already delivers the post); for the
        others the channel and message are re-fetched with the USER's client (listener entities carry the listener
        account's access hashes, which are invalid for another account).
        """
        if not message or not chat:
            return

        chat_raw_id = _normalize_peer_id(getattr(chat, 'id', None))
        username = getattr(chat, 'username', '') or ''
        username = username.lower().lstrip('@') if isinstance(username, str) else ''

        active_settings = await db_manager.get_all_active_story_settings()
        if not active_settings:
            return

        for st in active_settings:
            try:
                if not st.is_active:
                    continue
                if not await self._source_matches(st, chat_raw_id, username):
                    continue
                # Strict VIP restriction
                if not await db_manager.is_vip(st.user_id):
                    continue
                if chat_raw_id and chat_raw_id in self._monitored_ids.get(st.user_id, set()) and self._has_active_monitor(st.user_id):
                    continue

                client = await self.get_client_for_user(st.user_id)
                if not client or not client.is_connected():
                    continue
                ref = f"@{username}" if username else (int(f"-100{chat_raw_id}") if chat_raw_id else None)
                entity = await self._resolve_channel_entity(client, ref)
                if entity is None:
                    logger.warning(f"Story source {ref} is not accessible for user {st.user_id}")
                    continue
                own_message = await client.get_messages(entity, ids=message.id)
                if not own_message:
                    continue
                await self._process_channel_message(client, st.user_id, st, own_message, entity)
            except Exception as e:
                logger.warning(f"Story dispatch failed for user {st.user_id} (chat {chat_raw_id or username}): {e}")

    async def delete_story_by_id(self, user_id: int, story_id: int, peer_entity: Any = None) -> bool:
        """Deletes an active story via userbot MTProto DeleteStoriesRequest (profile story unless a peer is given)"""
        client = await self.get_client_for_user(user_id)
        if not client or not client.is_connected():
            return False
        try:
            await client(functions.stories.DeleteStoriesRequest(peer=peer_entity or types.InputPeerSelf(), id=[story_id]))
            logger.info(f"Successfully deleted active story #{story_id} for user {user_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete story #{story_id} of user {user_id} via MTProto: {e}")
            return False

    async def handle_source_message_deleted(self, source_channel: str, source_msg_id: int, source_id: Any = None):
        """Finds posted stories of the deleted / sold source message (matched by username or channel id) and deletes
        them from Telegram; a row is marked 'deleted' only after Telegram confirmed the deletion"""
        clean_chan = (source_channel or "").strip().lstrip("@").lower()
        raw_id = _normalize_peer_id(source_id) if source_id is not None else None
        if raw_id is None:
            raw_id = _normalize_peer_id(clean_chan)
        raw_str = str(raw_id) if raw_id else None
        try:
            async with db_manager.get_connection() as db:
                cursor = await db.execute("""
                    SELECT id, user_id, story_id, target_type, status
                    FROM posted_stories
                    WHERE source_msg_id = ?
                      AND status = 'success'
                      AND (
                            LOWER(TRIM(REPLACE(source_channel, '@', ''))) = ?
                            OR (? IS NOT NULL AND source_id = ?)
                            OR (? IS NOT NULL AND LTRIM(REPLACE(source_channel, '-100', ''), '-') = ?)
                      )
                """, (source_msg_id, clean_chan, raw_id, raw_id, raw_str, raw_str))
                rows = await cursor.fetchall()
        except Exception as e:
            logger.error(f"Error in handle_source_message_deleted: {e}", exc_info=True)
            return

        for r in rows:
            p_id, u_id, st_id, target_type = r[0], r[1], r[2], r[3]
            if not st_id:
                continue
            try:
                if target_type == "channel":
                    st = await db_manager.get_story_settings(u_id)
                    peer_entity = None
                    client = await self.get_client_for_user(u_id)
                    if st and st.target_channel and client:
                        entity = await self._resolve_channel_entity(client, st.target_channel)
                        if entity is not None:
                            peer_entity = await client.get_input_entity(entity)
                    if peer_entity is None:
                        logger.warning(f"Channel story #{st_id} of user {u_id} not deleted: story channel not resolvable")
                        continue
                    deleted = await self.delete_story_by_id(user_id=u_id, story_id=st_id, peer_entity=peer_entity)
                else:
                    deleted = await self.delete_story_by_id(user_id=u_id, story_id=st_id)
                if not deleted:
                    continue
                async with db_manager.write_transaction() as db:
                    await db.execute("UPDATE posted_stories SET status = 'deleted' WHERE id = ?", (p_id,))
                    await db.commit()
                logger.info(f"Marked posted_story #{p_id} as deleted for source msg #{source_msg_id}")
            except Exception as row_err:
                logger.warning(f"Story deletion for posted_story #{p_id} (user {u_id}) failed: {row_err}")

    async def stop_all(self):
        """Gracefully terminates all active user watchdogs, queue worker, and Telethon client sessions"""
        logger.info("Stopping all Story Cloner monitors and workers...")
        story_queue_service.stop_worker()
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
        self._supervisor_task = None
        for user_id in list(self._user_poll_tasks.keys()):
            self.stop_monitor_for_user(user_id)
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        for user_id, client in list(self._user_clients.items()):
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception as e:
                logger.debug(f"Client disconnect note for user {user_id}: {e}")
        for user_id in list(self._login_sessions.keys()):
            await self._discard_login_session(user_id)
        self._user_clients.clear()
        self._active_channel_handlers.clear()
        self._monitored_ids.clear()
        logger.info("Story Cloner service stopped successfully.")


story_cloner_service = StoryClonerService()
