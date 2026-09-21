import asyncio
import logging
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple, Union
import inspect

from telethon import TelegramClient, types, functions, utils as tl_utils, events
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
    UserAlreadyParticipantError
)
from PIL import Image, ImageFilter, ImageEnhance

from config.settings import settings
from database.db_manager import db_manager
from database.models import StorySettings, PostedStory
from services.phone_utils import normalize_phone_number
from services.security_vault import security_vault
from services.story_renderer import story_card_renderer
from services.story_video_generator import story_video_generator
from services.listing_analyzer import listing_analyzer
from services.story_queue_service import story_queue_service
from services.telethon_listener import is_ipv6_supported
from services.custom_emojis import INBOX, TROPHY, CHANNEL, MONEY, LOCATION, TIMER, PARTY, TAG, INFO, MOBILE, LINK

logger = logging.getLogger(__name__)


DEVICE_MODEL = "StoryCloner Pro"
SYSTEM_VERSION = "Telegram MTProto"
APP_VERSION = "StoryBot v3.0"
LANG_CODE = "uz"
SYSTEM_LANG_CODE = "uz-UZ"


class StoryClonerService:
    def __init__(self):
        self._user_clients: Dict[int, TelegramClient] = {}
        self._login_sessions: Dict[int, Dict[str, Any]] = {}
        self._otp_cooldowns: Dict[int, float] = {}
        self._bot_instance = None
        self._user_poll_tasks: Dict[int, asyncio.Task] = {}
        self._active_channel_handlers: Dict[int, Any] = {}
        self._processing_locks: Dict[int, asyncio.Lock] = {}

    def set_bot(self, bot):
        self._bot_instance = bot

    # ==========================================
    # --- REAL ESTATE PRICE & QUALITY FILTER ---
    # ==========================================

    @staticmethod
    def _parse_num_string(raw: str) -> Optional[float]:
        """Cleans and parses a numeric string with support for spaces, dots, and commas as thousands separators"""
        if not raw:
            return None
        s = raw.strip().replace(' ', '')
        # Dot or comma followed by 3 digits -> thousands separator (e.g. 1.500, 65.000, 1,500)
        s = re.sub(r'[.,](\d{3})(?!\d)', r'\1', s)
        s = s.replace(',', '.')
        s = re.sub(r'\.+', '.', s).strip('.')
        try:
            return float(s)
        except ValueError:
            return None

    @classmethod
    def extract_price(cls, text: str) -> Optional[float]:
        """
        Extracts price from real estate listings in Tashkent / Uzbekistan channels.
        Handles USD ($), y.e., у.е., dollar, доллар, UZS conversion, and dot/comma separated thousands.
        Examples:
        - 750$ -> 750.0
        - $800 -> 800.0
        - 1.500$ -> 1500.0
        - 1,500$ -> 1500.0
        - 65.000$ -> 65000.0
        - 65,000 $ -> 65000.0
        - Narxi: 700 $ -> 700.0
        - Цена: 1200 у.е. -> 1200.0
        - 1 000 USD / oy -> 1000.0
        - 700$ dan boshlanadi -> 700.0
        - Arenda: 77 kv.m, narxi 800$ -> 800.0
        """
        if not text:
            return None

        clean_text = unicodedata.normalize('NFKD', text)

        # Distractor units (square meters, floors, rooms, people)
        distractor_unit = r'(?:kv\.?m|m2|м2|метр|кв\.?м|qavat|қават|этаж|etaj|xona|хона|sotix|соток|kishi|odam)'

        # USD currency keywords
        curr_usd = r'(?:\$|usd|у\.?е\.?|y\.?e\.?|ye\b|уе\b|dollar[a-z]*|доллар[а-я]*)'

        # UZS currency keywords
        curr_uzs = r'(?:so[\'ʼ`]?m|som|сум|сўм|uzs)'

        # 0. Price range patterns: e.g. "750-800$", "750 - 800 у.е.", "700$ dan 1000$ gacha"
        # Always pick the starting/minimum price
        pat_range_hyphen = re.compile(
            r'([0-9\s.,]+)\s*(?:[-–—]|\.\.\.)\s*([0-9\s.,]+)\s*' + curr_usd + r'(?!\s*' + distractor_unit + r')',
            re.IGNORECASE
        )
        for match in pat_range_hyphen.finditer(clean_text):
            val = cls._parse_num_string(match.group(1))
            if val is not None and 50 <= val <= 5000000:
                return val

        pat_range_dan_gacha = re.compile(
            r'([0-9\s.,]+)\s*(?:' + curr_usd + r')?\s*dan\s*([0-9\s.,]+)\s*(?:' + curr_usd + r')?\s*gacha\b',
            re.IGNORECASE
        )
        for match in pat_range_dan_gacha.finditer(clean_text):
            val = cls._parse_num_string(match.group(1))
            if val is not None and 50 <= val <= 5000000:
                return val

        # 1. Number explicitly followed by USD currency (excluding distractor units)
        pat_num_usd = re.compile(r'([0-9\s.,]+)\s*' + curr_usd + r'(?!\s*' + distractor_unit + r')', re.IGNORECASE)
        for match in pat_num_usd.finditer(clean_text):
            val = cls._parse_num_string(match.group(1))
            if val is not None and 50 <= val <= 5000000:
                return val

        # 2. USD currency explicitly preceding number
        pat_usd_num = re.compile(curr_usd + r'\s*([0-9\s.,]+)(?!\s*' + distractor_unit + r')', re.IGNORECASE)
        for match in pat_usd_num.finditer(clean_text):
            val = cls._parse_num_string(match.group(1))
            if val is not None and 50 <= val <= 5000000:
                return val

        # 3. Explicit price label: "narxi: 850", "цена - 1200", "ijara 800" (ignoring UZS & area distractors)
        pat_label = re.compile(
            r'(?:narxi|narx|цена|стоимость|qiymati|ijara|arenda|аренда|to[\'ʼ`]?lov)\s*[:\-–—]?\s*([0-9\s.,]+)',
            re.IGNORECASE
        )
        for match in pat_label.finditer(clean_text):
            tail = clean_text[match.end():match.end() + 25].lower()
            if re.match(r'^\s*(?:' + curr_uzs + r'|' + distractor_unit + r')', tail):
                continue
            val = cls._parse_num_string(match.group(1))
            if val is not None and 50 <= val <= 5000000:
                return val

        # 4. "dan" pattern: "700$ dan", "700 dan"
        pat_dan = re.compile(r'([0-9\s.,]+)\s*(?:' + curr_usd + r')?\s*dan\b(?!\s*' + distractor_unit + r')', re.IGNORECASE)
        for match in pat_dan.finditer(clean_text):
            val = cls._parse_num_string(match.group(1))
            if val is not None and 50 <= val <= 5000000:
                return val

        # 5. Uzbek So'm (UZS) converted to USD (e.g. 12 000 000 so'm / 12800)
        pat_uzs = re.compile(r'([0-9\s.,]+)\s*' + curr_uzs, re.IGNORECASE)
        for match in pat_uzs.finditer(clean_text):
            val = cls._parse_num_string(match.group(1))
            if val is not None and val >= 100000:
                usd_val = round(val / 12800.0, 1)
                if 50 <= usd_val <= 5000000:
                    return usd_val

        return None

    @staticmethod
    def is_demand_post(text: str) -> bool:
        """
        Detects whether a post is a client inquiry / search request rather than an available listing.
        Smartly avoids false positives when landlords mention conditions (e.g., 'oila kerak', 'kvartirant kerak').
        """
        if not text:
            return False

        t_lower = text.lower()

        # 1. Strong listing indicators (landlords/agents offering properties)
        offer_patterns = [
            r'\b(?:arendaga|ijaraga)?\s*(?:beriladi|берилади|topshiriladi|топширилади)\b',
            r'\b(?:сдается|сдаётся|сдам|сдаю|сдаем)\b',
            r'\b(?:sotiladi|сотилади|sotuvda|продается|продаётся|продам)\b'
        ]
        if any(re.search(pat, t_lower) for pat in offer_patterns):
            return False

        # 2. Demand indicators (clients/tenants seeking properties)
        demand_patterns = [
            r'\b(?:kvartira|uy|joy|arenda|ijara|xona|хона|квартира)\s+(?:kerak|керак)\b',
            r'\b(?:kerak|керак)\s+(?:kvartira|uy|joy|arenda|ijara|xona)\b',
            r'\b(?:menga|bizga|oilaga|klientga|klientimizga)\s+(?:kerak|керак)\b',
            r'\b(?:qidiryapman|qidirilmoqda|qidirayapmiz|qidirilyapti)\b',
            r'\b(?:olmoqchiman|olmoqchimiz|olaman)\b',
            r'\b(?:ищу|ищем|сниму|снимем|нужна|нужно|ищет)\b',
            r'\b(?:клиент\s*бор|klient\s*bor|клиент\s*есть|клиентларга)\b',
            r'\b(?:запрос\s*на\s*аренду|запрос)\b',
            r'\barenda\s*kerak\b',
            r'\bkvartira\s*kerak\b'
        ]
        return any(re.search(pat, t_lower) for pat in demand_patterns)

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
        Evaluates whether an incoming post matches the real estate story criteria.
        Returns: (is_match, reason, detected_price)
        """
        text = getattr(message, 'text', '') or getattr(message, 'message', '') or ""

        # Check demand / inquiry filter
        if settings.filter_demands and self.is_demand_post(text):
            return False, "Mijoz qidiruvi / talab xabari (listing emas)", None

        # Check media requirement
        if settings.require_photos and not self.has_media(message):
            return False, "Postda rasm yoki video yo'q", None

        # Price extraction
        price = self.extract_price(text)
        if price is not None:
            if price < settings.min_price:
                return False, f"Narx (${price:g}) minimal chegara (${settings.min_price:g}) dan kam", price
            if settings.max_price > 0 and price > settings.max_price:
                return False, f"Narx (${price:g}) maksimal chegara (${settings.max_price:g}) dan yuqori", price
        else:
            if settings.require_price:
                return False, "Postda narx aniqlanmadi (yoki $ emas)", None

        return True, "Mos keldi", price

    # ==========================================
    # --- STORY BACKGROUND GENERATOR ---
    # ==========================================

    @staticmethod
    def get_or_create_background(
        style: str = "telegram_green",
        source_photo_path: Optional[str] = None
    ) -> str:
        """
        Retrieves or generates the background image for the Telegram Story (1080x1920, 9:16).
        Styles:
        - telegram_green: Authentic Telegram doodle wallpaper (matches user's reference screenshot!)
        - listing_blur: Cinematic Gaussian-blurred apartment photo with dark vignette
        - luxury_dark: Sleek dark aesthetic
        - emerald: Deep emerald gradient
        """
        base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "story_backgrounds")
        os.makedirs(base_dir, exist_ok=True)

        if style == "listing_blur" and source_photo_path and os.path.exists(source_photo_path):
            try:
                temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
                os.makedirs(temp_dir, exist_ok=True)
                out_path = os.path.join(temp_dir, f"blur_bg_{int(time.time() * 1000)}.jpg")

                im = Image.open(source_photo_path).convert('RGB')
                target_w, target_h = 1080, 1920
                src_w, src_h = im.size
                scale = max(target_w / src_w, target_h / src_h)
                new_w, new_h = int(src_w * scale), int(src_h * scale)
                im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
                left = (new_w - target_w) // 2
                top = (new_h - target_h) // 2
                im = im.crop((left, top, left + target_w, top + target_h))

                im = im.filter(ImageFilter.GaussianBlur(radius=35))
                enhancer = ImageEnhance.Brightness(im)
                im = enhancer.enhance(0.75)
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
    ) -> Tuple[bool, Optional[int], str, Optional[str]]:
        """
        Publishes a 25-second luxury video story reposting the specified channel message with 1:1 pixel accuracy.
        Renders the full native repost card (photos collage, header, text, price highlight, avatar,
        and date/time) and attaches InputMediaAreaChannelPost for interactive 1-tap jumping to the post.
        """
        # Resolve client if user_id is passed
        target_user_id = kwargs.get("user_id")
        if client is None and target_user_id is not None:
            client = target_user_id

        if isinstance(client, int):
            target_user_id = client
            client = await self.get_client_for_user(target_user_id)
            if not client:
                return False, None, "Telegram hisobi ulanmagan!", None
        elif target_user_id is None and client is not None:
            for uid, c in self._user_clients.items():
                if c is client:
                    target_user_id = uid
                    break

        if target_user_id and not await db_manager.is_vip(target_user_id):
            return False, None, "VIP obunasi talab etiladi yoki muddati tugagan!", None

        if channel_identifier is None and "source_channel" in kwargs:
            channel_identifier = kwargs["source_channel"]

        if msg_id is None and "msg_id" in kwargs:
            msg_id = kwargs["msg_id"]

        if client and not client.is_connected():
            try:
                await client.connect()
            except Exception as conn_err:
                logger.warning(f"Client auto-reconnect note: {conn_err}")

        if not client or not client.is_connected():
            return False, None, "Telegram hisobi ulanmagan!", None

        temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
        os.makedirs(temp_dir, exist_ok=True)
        temp_files_to_clean = []

        try:
            # 1. Resolve channel input entity & full entity cleanly
            if isinstance(channel_identifier, str):
                ci_str = channel_identifier.strip()
                if ci_str.startswith("-100") or ci_str.lstrip("-").isdigit():
                    try:
                        channel_identifier = int(ci_str)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                elif not ci_str.startswith("@") and not ci_str.startswith("https://") and not ci_str.startswith("t.me/"):
                    channel_identifier = f"@{ci_str}"

            input_channel = None
            full_entity = None
            try:
                input_channel = tl_utils.get_input_channel(channel_identifier)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

            try:
                if isinstance(channel_identifier, (str, int)):
                    full_entity = await client.get_entity(channel_identifier)
                else:
                    full_entity = channel_identifier

                if not input_channel and full_entity:
                    channel_input_entity = await client.get_input_entity(full_entity)
                    input_channel = tl_utils.get_input_channel(channel_input_entity)
            except Exception as ent_err:
                logger.debug(f"Entity resolve note: {ent_err}")

            if not input_channel:
                logger.warning(f"Could not resolve InputChannel for {channel_identifier}. Story will be published without media area post link.")

            # 2. Target peer (default: self / user's own profile story or target channel)
            target_peer = peer
            if not target_peer:
                t_type = kwargs.get("target_type")
                t_channel = kwargs.get("target_channel")
                if not t_type and target_user_id:
                    try:
                        u_st = await db_manager.get_story_settings(target_user_id)
                        t_type = u_st.target_type
                        t_channel = u_st.target_channel
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                if t_type == "channel" and t_channel:
                    try:
                        tc = t_channel
                        if isinstance(tc, str) and (tc.startswith("-100") or tc.lstrip("-").isdigit()):
                            tc = int(tc)
                        elif isinstance(tc, str) and not tc.startswith("@") and not tc.startswith("http"):
                            tc = f"@{tc}"
                        target_peer = await client.get_input_entity(tc)
                    except Exception as tp_err:
                        logger.warning(f"Could not resolve target channel peer {t_channel}, defaulting to InputPeerSelf: {tp_err}")
                        target_peer = types.InputPeerSelf()
                else:
                    target_peer = types.InputPeerSelf()

            # 3. Channel metadata (title & avatar)
            channel_title = getattr(full_entity, 'title', '') or "ARENDA UY"
            avatar_path = None
            try:
                if full_entity:
                    avatar_path = await client.download_profile_photo(full_entity, file=temp_dir)
                    if avatar_path:
                        temp_files_to_clean.append(avatar_path)
            except Exception as av_err:
                logger.debug(f"Avatar download note: {av_err}")

            # 4. Fetch the message and all sibling photos if album
            msg = None
            try:
                if full_entity:
                    msg = await client.get_messages(full_entity, ids=msg_id)
            except Exception as msg_err:
                logger.debug(f"Fetch message note: {msg_err}")

            if msg is None and not source_photo_path:
                return False, None, f"Xabar topilmadi yoki o'qib bo'lmadi (#{msg_id})", None

            def _is_image_media(m_obj: Any) -> bool:
                if not m_obj:
                    return False
                if getattr(m_obj, 'photo', None):
                    return True
                doc = getattr(m_obj, 'document', None)
                if doc:
                    mime = getattr(doc, 'mime_type', '') or ''
                    if mime.startswith('image/'):
                        return True
                    for attr in getattr(doc, 'attributes', []):
                        if hasattr(attr, 'file_name') and attr.file_name:
                            fn = attr.file_name.lower()
                            if fn.endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic')):
                                return True
                return False

            photo_paths = []
            caption_text = ""
            msg_date = getattr(msg, 'date', None) if msg else None

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

                # 3. Fallback: channel recent messages
                if not album_msgs:
                    try:
                        recent = await client.get_messages(full_entity, limit=30)
                        album_msgs = [m for m in (recent or []) if m and getattr(m, 'grouped_id', None) == msg.grouped_id]
                    except Exception as rec_err:
                        logger.debug(f"Recent album fetch note: {rec_err}")

                # 4. Guarantee msg itself is included
                if not any(m.id == msg.id for m in album_msgs):
                    album_msgs.append(msg)

                album_msgs.sort(key=lambda x: x.id)
                for m in album_msgs:
                    if getattr(m, 'message', None):
                        clean_m = m.message.strip()
                        if not caption_text or (len(clean_m) > len(caption_text) and len(caption_text) < 40):
                            caption_text = clean_m
                    if _is_image_media(m):
                        try:
                            p_path = await client.download_media(m, file=temp_dir)
                            if p_path and os.path.exists(p_path):
                                photo_paths.append(p_path)
                                temp_files_to_clean.append(p_path)
                        except Exception as dl_err:
                            logger.warning(f"Error downloading album media #{m.id}: {dl_err}")
            elif msg:
                caption_text = msg.message or ""
                if _is_image_media(msg):
                    try:
                        p_path = await client.download_media(msg, file=temp_dir)
                        if p_path and os.path.exists(p_path):
                            photo_paths.append(p_path)
                            temp_files_to_clean.append(p_path)
                    except Exception as dl_err:
                        logger.warning(f"Error downloading single media: {dl_err}")

            # Fallback caption from kwargs if Telegram message has no caption text
            if not caption_text:
                caption_text = kwargs.get("caption") or kwargs.get("caption_snippet") or ""

            if source_photo_path and os.path.exists(source_photo_path) and not photo_paths:
                photo_paths.append(source_photo_path)

            # 5. Price & Date & Forward Info
            detected_price = self.extract_price(caption_text) if caption_text else None
            if not detected_price and "price" in kwargs and kwargs["price"]:
                try:
                    detected_price = float(kwargs["price"])
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            months_ru = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
            if msg_date:
                d_str = f"{msg_date.day} {months_ru[msg_date.month - 1]}, {msg_date.strftime('%H:%M')}"
            else:
                now_dt = datetime.now()
                d_str = f"{now_dt.day} {months_ru[now_dt.month - 1]}, {now_dt.strftime('%H:%M')}"

            forward_title = None
            if msg and getattr(msg, 'fwd_from', None):
                fwd_name = getattr(msg.fwd_from, 'from_name', None)
                if fwd_name:
                    forward_title = fwd_name
                elif getattr(msg.fwd_from, 'channel_id', None):
                    try:
                        fwd_ent = await client.get_entity(msg.fwd_from.channel_id)
                        forward_title = getattr(fwd_ent, 'title', None)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)

            # 6. Generate 25-Second Luxury Video Story or Static Composite Photo
            valid_photos = [p for p in photo_paths if p and os.path.exists(p)]
            card_coords = None
            media = None
            effective_uid = target_user_id or kwargs.get("user_id")

            # Early check: if user requires photos and none were found or downloaded, abort cleanly
            if not valid_photos:
                req_photos = True
                if effective_uid:
                    try:
                        u_st = await db_manager.get_story_settings(effective_uid)
                        req_photos = bool(u_st.require_photos)
                    except Exception:
                        req_photos = True
                if req_photos:
                    logger.warning(f"Aborting story post for msg #{msg_id}: no valid photos found and require_photos=True.")
                    return False, None, "Xabarda fotosuratlar topilmadi yoki yuklab olinmadi (require_photos faol)", None

            if badges is None and caption_text:
                try:
                    badges = listing_analyzer.analyze(caption_text, len(valid_photos), detected_price).smart_badges
                except Exception:
                    badges = None

            # Determine configured video duration (voiceover narration disabled for stories)
            story_duration = 25.0
            enable_ai_voice = False
            if effective_uid:
                try:
                    u_st = await db_manager.get_story_settings(effective_uid)
                    story_duration = float(getattr(u_st, "video_duration", 25) or 25.0)
                except Exception:
                    story_duration = 25.0
            story_duration = max(15.0, min(40.0, story_duration))

            if valid_photos:
                try:
                    logger.info(f"Generating {story_duration}s luxury video story for msg #{msg_id} with {len(valid_photos)} photos (luxury_music: True, badges: {badges})...")
                    from services.render_queue import render_queue
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
                        enable_ai_voice=False
                    )
                    temp_files_to_clean.append(video_path)

                    # Upload video document to Telegram MTProto
                    uploaded_file = await client.upload_file(video_path)
                    media = types.InputMediaUploadedDocument(
                        file=uploaded_file,
                        mime_type="video/mp4",
                        attributes=[
                            types.DocumentAttributeVideo(
                                duration=int(story_duration),
                                w=1080,
                                h=1920,
                                supports_streaming=True,
                                nosound=False
                            )
                        ],
                        nosound_video=False
                    )
                    logger.info(f"Video story media prepared successfully for msg #{msg_id} (duration: {story_duration}s)")
                except Exception as vid_err:
                    logger.error(f"Video story generation failed for msg #{msg_id} ({vid_err}), falling back to static composite photo.", exc_info=True)

            if not media:
                bg_base_path = await asyncio.to_thread(self.get_or_create_background, style=bg_style, source_photo_path=source_photo_path)
                if not os.path.exists(bg_base_path):
                    return False, None, "Fon rasmi topilmadi!", None

                composite_path = await story_card_renderer.render_story_composite_async(
                    bg_base_path=bg_base_path,
                    channel_title=channel_title,
                    photo_paths=valid_photos,
                    caption=caption_text,
                    price=detected_price,
                    date_str=d_str,
                    avatar_path=avatar_path,
                    forward_title=forward_title,
                    badges=badges
                )
                temp_files_to_clean.append(composite_path)
                card_coords = story_card_renderer.get_last_card_coordinates()

                uploaded_file = await client.upload_file(composite_path)
                media = types.InputMediaUploadedPhoto(file=uploaded_file)

            # 7. Interactive channel post media area (centered widget matching exact card dimensions)
            if not card_coords:
                card_coords = story_card_renderer.get_last_card_coordinates()

            coords = types.MediaAreaCoordinates(
                x=card_coords.get("x", 53.2),
                y=card_coords.get("y", 50.0),
                w=card_coords.get("w", 79.6),
                h=card_coords.get("h", 49.6),
                rotation=0.0,
                radius=2.5
            )
            media_area = types.InputMediaAreaChannelPost(
                coordinates=coords,
                channel=input_channel,
                msg_id=msg_id
            )

            # 10. Privacy: Public story
            privacy_rules = [types.InputPrivacyValueAllowAll()]

            # Final VIP check before sending
            if target_user_id and not await db_manager.is_vip(target_user_id):
                return False, None, "VIP obunasi talab etiladi yoki muddati tugagan!", None

            # Determine pinned status (defaults to True to keep story in profile posts & highlights)
            is_pinned = pinned
            if is_pinned is None:
                if target_user_id:
                    try:
                        u_st = await db_manager.get_story_settings(target_user_id)
                        is_pinned = bool(u_st.pin_to_profile)
                    except Exception:
                        is_pinned = True
                else:
                    is_pinned = True

            # 11. Execute MTProto stories.sendStory request with pinned status and 24h active period
            has_media_area = bool(media_area and input_channel)
            request = functions.stories.SendStoryRequest(
                peer=target_peer,
                media=media,
                privacy_rules=privacy_rules,
                pinned=is_pinned,
                period=86400,
                media_areas=[media_area] if has_media_area else None,
                random_id=random.randint(10000000, 99999999)
            )

            try:
                result = await client(request)
            except Exception as send_err:
                err_str = str(send_err)
                if has_media_area and any(k in err_str for k in ("MEDIA_AREA", "CHANNEL", "INPUT_CHANNEL", "CHAT_ADMIN")):
                    logger.warning(f"SendStory with media_area failed ({err_str}), retrying clean story without media_areas...")
                    request.media_areas = None
                    result = await client(request)
                else:
                    raise

            # 12. Extract story ID from updates
            story_id = None
            if hasattr(result, 'updates'):
                for upd in result.updates:
                    if isinstance(upd, types.UpdateStory):
                        story_id = getattr(upd.story, 'id', None)
                        break
                    elif isinstance(upd, types.UpdateStoryID):
                        story_id = upd.id
                        break

            # 13. Ensure story is permanently pinned to profile / channel highlights (never moves to archive)
            if story_id and is_pinned:
                try:
                    await client(functions.stories.TogglePinnedRequest(
                        peer=target_peer,
                        id=[story_id],
                        pinned=True
                    ))
                    logger.info(f"Story #{story_id} successfully pinned to profile / channel highlights")
                except Exception as pin_err:
                    logger.debug(f"TogglePinned note for story #{story_id}: {pin_err}")

            # 14. Try exporting story link
            story_url = None
            if story_id:
                try:
                    exported = await client(functions.stories.ExportStoryLinkRequest(
                        peer=target_peer,
                        id=story_id
                    ))
                    story_url = getattr(exported, 'link', None)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            logger.info(f"Telegram Story composite published successfully for msg #{msg_id} (story ID: {story_id})")
            return True, story_id, "Istoriya muvaffaqiyatli joylandi!", story_url

        except FloodWaitError as fwe:
            msg = f"Telegram cheklovi (FloodWait): Iltimos {fwe.seconds} soniya kuting."
            logger.warning(msg)
            return False, None, msg, None
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Error publishing Telegram Story for msg #{msg_id}: {err_msg}", exc_info=True)
            if "PREMIUM_ACCOUNT_REQUIRED" in err_msg or "premium" in err_msg.lower():
                return False, None, "Telegram cheklovi: Istoriya joylash uchun Telegram hisobingizda Premium yoki kunlik limit yetarli bo'lishi lozim.", None
            return False, None, f"Xatolik yuz berdi: {err_msg}", None
        finally:
            for tf in temp_files_to_clean:
                if tf and os.path.exists(tf):
                    try:
                        os.remove(tf)
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)

    # ==========================================
    # --- USER TELETHON CLIENT & OTP AUTH ---
    # ==========================================

    async def get_client_for_user(self, user_id: int) -> Optional[TelegramClient]:
        """
        Retrieves or initializes an active, authenticated TelegramClient for the user.
        Uses the per-user session stored in user_sessions table.
        """
        if user_id in self._user_clients:
            c = self._user_clients.pop(user_id, None)
            if c:
                if not c.is_connected():
                    try:
                        await c.connect()
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                if c.is_connected():
                    try:
                        if await c.is_user_authorized():
                            self._user_clients[user_id] = c
                            return c
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                try:
                    await c.disconnect()
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

        session_str = await db_manager.get_user_session(user_id)
        if session_str:
            try:
                client = TelegramClient(
                    StringSession(session_str),
                    settings.TELEGRAM_API_ID,
                    settings.TELEGRAM_API_HASH,
                    device_model=DEVICE_MODEL,
                    system_version=SYSTEM_VERSION,
                    app_version=APP_VERSION,
                    lang_code=LANG_CODE,
                    system_lang_code=SYSTEM_LANG_CODE,
                    use_ipv6=is_ipv6_supported()
                )
                await client.connect()
                if await client.is_user_authorized():
                    # Evict oldest client if pool exceeds 20 active MTProto connections
                    if len(self._user_clients) >= 20:
                        old_uid, old_c = next(iter(self._user_clients.items()))
                        self._user_clients.pop(old_uid, None)
                        try:
                            if old_c.is_connected():
                                await old_c.disconnect()
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                    self._user_clients[user_id] = client
                    return client
            except Exception as e:
                logger.error(f"Error connecting Telethon client for user {user_id}: {e}")

        return None

    get_user_client = get_client_for_user

    async def request_otp_code(self, user_id: int, phone: str) -> Tuple[bool, str]:
        """Initiates MTProto phone code verification for user"""
        if not settings.is_configured():
            return False, "TELEGRAM_API_ID va TELEGRAM_API_HASH sozlanmagan!"

        now_ts = time.monotonic()
        last_req = self._otp_cooldowns.get(user_id, 0.0)
        if now_ts - last_req < 60.0:
            remaining = int(60.0 - (now_ts - last_req))
            return False, f"Juda tez! Iltimos {remaining} soniya kuting va qaytadan urinib ko'ring."

        is_valid, phone_e164, _ = normalize_phone_number(phone)
        if not is_valid:
            return False, "Telefon raqami noto'g'ri formatda! Xalqaro formatda kiriting (masalan: +998901234567)."

        try:
            if user_id in self._login_sessions:
                old_c = self._login_sessions[user_id].get("client")
                if old_c and old_c.is_connected():
                    try:
                        await old_c.disconnect()
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)

            client = TelegramClient(
                StringSession(""),
                settings.TELEGRAM_API_ID,
                settings.TELEGRAM_API_HASH,
                device_model=DEVICE_MODEL,
                system_version=SYSTEM_VERSION,
                app_version=APP_VERSION,
                lang_code=LANG_CODE,
                system_lang_code=SYSTEM_LANG_CODE,
                use_ipv6=is_ipv6_supported()
            )
            await client.connect()
            sent_code = await client.send_code_request(phone_e164)

            self._login_sessions[user_id] = {
                "client": client,
                "phone": phone_e164,
                "phone_code_hash": sent_code.phone_code_hash,
                "created_at": time.time()
            }
            self._otp_cooldowns[user_id] = time.monotonic()
            return True, "Tasdiqlash kodi Telegram akkauntingizga yuborildi."

        except FloodWaitError as e:
            return False, f"Telegram cheklovi: Iltimos {e.seconds} soniya kuting."
        except Exception as e:
            logger.error(f"Error requesting OTP for user {user_id}: {e}")
            return False, f"Xatolik: {e}"

    async def submit_otp_code(self, user_id: int, code: str) -> Tuple[bool, str, str]:
        """Submits phone code. Returns (ok, message, status: 'success'|'needs_2fa'|'error')"""
        if user_id not in self._login_sessions:
            return False, "Sessiya topilmadi! Telefon raqamingizni qaytadan kiriting.", "error"

        sess = self._login_sessions[user_id]
        client: TelegramClient = sess["client"]
        phone = sess["phone"]
        phone_code_hash = sess["phone_code_hash"]

        code_clean = "".join(c for c in code if c.isdigit())

        try:
            await client.sign_in(phone, code_clean, phone_code_hash=phone_code_hash)
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

            self._user_clients[user_id] = client
            self._login_sessions.pop(user_id, None)

            # Auto-start monitoring if configured
            asyncio.create_task(self.start_monitor_for_user(user_id))

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
        if user_id not in self._login_sessions:
            return False, "Sessiya topilmadi! Qaytadan telefon raqam kiriting."

        sess = self._login_sessions[user_id]
        client: TelegramClient = sess["client"]
        phone = sess.get("phone", "")

        try:
            await client.sign_in(password=password.strip())
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

            self._user_clients[user_id] = client
            self._login_sessions.pop(user_id, None)

            # Auto-start monitoring if configured
            asyncio.create_task(self.start_monitor_for_user(user_id))

            name = getattr(me, 'first_name', '') or ''
            return True, f"2FA tasdiqlandi! Hisob ulandi: {name}"

        except PasswordHashInvalidError:
            return False, "2FA parol noto'g'ri kiritildi!"
        except Exception as e:
            return False, f"Xatolik: {e}"

    async def disconnect_user(self, user_id: int) -> bool:
        """Disconnects user client, cancels monitoring, invalidates MTProto session, and deletes session"""
        self.stop_monitor_for_user(user_id)
        c = self._user_clients.pop(user_id, None)
        if c and c.is_connected():
            try:
                if await c.is_user_authorized():
                    await c.log_out()
            except Exception as e:
                logger.warning(f"Error during logout for user {user_id}: {e}")
            try:
                if c.is_connected():
                    await c.disconnect()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
        self._login_sessions.pop(user_id, None)
        self._otp_cooldowns.pop(user_id, None)
        self._processing_locks.pop(user_id, None)
        await db_manager.delete_user_session(user_id)
        return True

    # ==========================================
    # --- AUTO-MONITORING & REAL-TIME ENGINE ---
    # ==========================================

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._processing_locks:
            self._processing_locks[user_id] = asyncio.Lock()
        return self._processing_locks[user_id]

    async def _process_channel_message(
        self,
        client: TelegramClient,
        user_id: int,
        settings: StorySettings,
        message: Any,
        chat: Any
    ):
        """Processes a single incoming message from any monitored source channel"""
        if not message or getattr(message, 'action', None) is not None:
            return

        # 0. Strict VIP restriction
        if not await db_manager.is_vip(user_id):
            logger.info(f"Dropping incoming channel message for user {user_id}: VIP subscription not active")
            self.stop_monitor_for_user(user_id)
            return

        chat_id = getattr(chat, 'id', None)
        grouped_id = getattr(message, 'grouped_id', None)
        chat_username = getattr(chat, 'username', '') or ''
        source_label = f"@{chat_username.lstrip('@')}" if chat_username else (getattr(chat, 'title', '') or settings.source_channel)
        photo_path: Optional[str] = None

        # 1. Check message-level deduplication
        if await db_manager.is_story_posted(
            user_id=user_id,
            source_channel=source_label,
            source_msg_id=message.id,
            source_id=chat_id,
            grouped_id=grouped_id
        ):
            return

        # 2. Check filter (demand inquiry, photos, price)
        is_match, reason, price = self.matches_filter(message, settings)
        if not is_match:
            logger.debug(f"Message #{message.id} skipped for user {user_id}: {reason}")
            return

        caption_text = getattr(message, 'message', '') or getattr(message, 'text', '') or ""
        photo_count = 1 if getattr(message, 'photo', None) else 0

        # 3. Analyze listing metadata, quality score, smart badges, and deduplication fingerprint
        meta = listing_analyzer.analyze(caption_text, photo_count=photo_count, existing_price=price)

        # 4. Check cross-channel listing fingerprint deduplication (14-day window)
        if await db_manager.is_listing_duplicate(user_id, meta.fingerprint, days=14):
            logger.info(f"Duplicate real estate listing ignored for user {user_id} (fingerprint {meta.fingerprint})")
            return

        lock = self._get_user_lock(user_id)
        async with lock:
            # Double-check deduplication inside lock
            if await db_manager.is_story_posted(
                user_id=user_id,
                source_channel=source_label,
                source_msg_id=message.id,
                source_id=chat_id,
                grouped_id=grouped_id
            ):
                return
            if await db_manager.is_listing_duplicate(user_id, meta.fingerprint, days=14):
                return

            # Download photo for listing blur background if configured
            photo_path = None
            if settings.background_style == "listing_blur" and getattr(message, 'photo', None):
                try:
                    temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
                    os.makedirs(temp_dir, exist_ok=True)
                    photo_path = await client.download_media(message, file=temp_dir)
                except Exception:
                    photo_path = None

            # 5. Check Drip-Feed Queue & Prime Hours
            now_uzb = story_queue_service.get_uzb_now()
            in_prime = story_queue_service.is_in_prime_hours(settings.prime_hours_start, settings.prime_hours_end, now_uzb)
            last_posted_utc = await db_manager.get_last_posted_story_time(user_id)
            cooldown = timedelta(minutes=settings.drip_delay_minutes)
            now_utc = datetime.now(timezone.utc)
            is_in_cooldown = bool(last_posted_utc and (now_utc - last_posted_utc) < cooldown)
            today_count = await db_manager.get_today_posted_story_count(user_id)
            is_daily_limit = bool(today_count >= settings.max_stories_per_day)

            if settings.prime_hours_enabled and (not in_prime or is_in_cooldown or is_daily_limit):
                payload = {
                    "source_id": chat_id,
                    "channel_id": chat_id,
                    "channel_username": chat_username,
                    "channel_title": getattr(chat, 'title', '') or settings.source_channel,
                    "source_photo_path": photo_path,
                    "badges": meta.smart_badges if settings.enable_smart_badges else None,
                    "caption_snippet": caption_text[:150],
                    "fingerprint": meta.fingerprint
                }
                canonical_source = f"@{chat_username.lstrip('@')}" if chat_username else (str(chat_id) if chat_id else source_label)
                item_id, sched_str = await story_queue_service.enqueue_listing(
                    user_id=user_id,
                    source_channel=canonical_source,
                    source_msg_id=message.id,
                    payload=payload,
                    price=price,
                    district=meta.district or "",
                    rooms=meta.rooms,
                    area=meta.area,
                    score=meta.quality_score
                )
                await db_manager.record_listing_hash(user_id, meta.fingerprint, source_label, message.id)

                if self._bot_instance:
                    try:
                        price_fmt = f"${price:g}" if price else "Aniqlangan"
                        text = f"""
{INBOX} <b>Yangi sara variant Navbatga olindi!</b>

├ {TROPHY} <b>Sifat bali:</b> <b>{meta.quality_score}/100</b>
├ {CHANNEL} <b>Manba kanal:</b> {source_label}
├ {MONEY} <b>Narxi:</b> <b>{price_fmt}</b>
├ {LOCATION} <b>Hudud:</b> {meta.district or "Aniqlanmadi"}
└ {TIMER} <b>Rejalashtirilgan vaqt:</b> {sched_str} (UTC)

<i>Auditoriyangizni spam qilmaslik va maksimal ko'rishlar uchun post Prime Time vaqtiga rejalashtirildi.</i>
"""

                        await self._bot_instance.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True
                        )
                    except Exception as ne:
                        logger.warning(f"Queue notify error: {ne}")
                return

            # If album, wait briefly for all sibling media messages to arrive
            if grouped_id:
                await asyncio.sleep(1.5)

            # Publish story with smart badges
            badges_to_use = meta.smart_badges if settings.enable_smart_badges else None
            ok, story_id, res_msg, story_url = await self.post_story_from_channel(
                client=client,
                channel_identifier=chat,
                msg_id=message.id,
                bg_style=settings.background_style,
                source_photo_path=photo_path,
                badges=badges_to_use,
                user_id=user_id,
                pinned=settings.pin_to_profile
            )

            if photo_path and os.path.exists(photo_path):
                try:
                    os.remove(photo_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            if ok:
                caption_snip = (getattr(message, 'message', '') or '')[:150]
                await db_manager.record_posted_story(
                    user_id=user_id,
                    source_channel=source_label,
                    source_id=chat_id,
                    source_msg_id=message.id,
                    story_id=story_id,
                    price=price,
                    caption_snippet=caption_snip,
                    target_type=settings.target_type,
                    status="success",
                    grouped_id=grouped_id
                )
                await db_manager.record_listing_hash(user_id, meta.fingerprint, source_label, message.id)

                # Send bot notification to user
                if self._bot_instance:
                    try:
                        price_str = f"${price:g}" if price else "Aniqlangan"
                        story_link_html = f'\n{LINK} <a href="{story_url}">Istoriyani ochish</a>' if story_url else ''
                        badges_str = " ".join(f"<code>[{b}]</code>" for b in meta.smart_badges) if meta.smart_badges else ""
                        badges_line = f"\n├ {TAG} <b>Badjlar:</b> {badges_str}" if badges_str else ""
                        text = f"""
{PARTY} <b>Yangi sara variant Istoriyaga joylandi!</b>

├ {TROPHY} <b>Sifat bali:</b> <b>{meta.quality_score}/100</b>
├ {CHANNEL} <b>Manba kanal:</b> {source_label}
├ {MONEY} <b>Narxi:</b> <b>{price_str}</b>{badges_line}
├ {INFO} <b>Xabar ID:</b> <code>{message.id}</code>
└ {MOBILE} <b>Format:</b> Nativ Repost Story (bosganda post ochiladi){story_link_html}
"""

                        await self._bot_instance.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True
                        )
                    except Exception as notify_err:
                        logger.warning(f"Failed to notify user {user_id}: {notify_err}")

    async def _user_channel_poll_loop(self, user_id: int, client: TelegramClient, entities: List[Any]):
        """Background watchdog loop checking all monitored source channels periodically"""
        logger.info(f"Watchdog poll loop started for user {user_id} with {len(entities)} channels")
        while True:
            try:
                await asyncio.sleep(45.0)
                st = await db_manager.get_story_settings(user_id)
                if not st.is_active:
                    break

                if not client.is_connected() or not await client.is_user_authorized():
                    break

                # VIP check during watchdog loop
                if not await db_manager.is_vip(user_id):
                    logger.info(f"Watchdog loop stopped for user {user_id}: VIP subscription inactive")
                    break

                for entity in entities:
                    try:
                        messages = await client.get_messages(entity, limit=5)
                        for msg in reversed(messages):
                            await self._process_channel_message(client, user_id, st, msg, entity)
                    except Exception as ent_err:
                        logger.debug(f"Poll check note for entity {entity}: {ent_err}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Poll loop iteration note for user {user_id}: {e}")
                await asyncio.sleep(15.0)

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
                logger.info(f"User {user_id} Telethon client not connected; monitoring deferred.")
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
                    clean_ch = str(ch).strip()
                    clean_ch = re.sub(r'^https?:\/\/(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)\/', '', clean_ch, flags=re.IGNORECASE)
                    entity = None
                    if clean_ch.startswith("+") or clean_ch.startswith("joinchat/"):
                        hash_val = clean_ch.lstrip("+").replace("joinchat/", "").strip()
                        chat_info = None
                        try:
                            check_res = await client(functions.messages.CheckChatInviteRequest(hash_val))
                            if hasattr(check_res, 'chat') and check_res.chat:
                                entity = check_res.chat
                            chat_info = check_res
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                        if not entity:
                            try:
                                imp_res = await client(functions.messages.ImportChatInviteRequest(hash_val))
                                if hasattr(imp_res, 'chats') and imp_res.chats:
                                    entity = imp_res.chats[0]
                            except UserAlreadyParticipantError:
                                target_title = getattr(chat_info, 'title', None)
                                if not target_title and hasattr(chat_info, 'chat'):
                                    target_title = getattr(chat_info.chat, 'title', None)
                                try:
                                    async for dialog in client.iter_dialogs(limit=500):
                                        if (dialog.is_channel or dialog.is_group):
                                            if target_title and getattr(dialog, 'title', None) == target_title:
                                                entity = dialog.entity
                                                break
                                except Exception:
                                    logger.debug("Ignored exception", exc_info=True)
                            except Exception as imp_err:
                                logger.debug(f"Invite import failed for {clean_ch}: {imp_err}")
                    else:
                        entity = await client.get_entity(ch)
                        try:
                            await client(functions.channels.JoinChannelRequest(channel=entity))
                        except (UserAlreadyParticipantError, Exception):
                            pass
                    if entity:
                        resolved_entities.append(entity)
                except Exception as ent_e:
                    logger.warning(f"Could not resolve channel {ch} for user {user_id}: {ent_e}")

            if not resolved_entities:
                return False

            # Stop existing poll task and unbind previous handler from client
            self.stop_monitor_for_user(user_id, client=client)
            self._user_clients[user_id] = client

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
                    logger.error(f"Error handling channel event for user {user_id}: {he}")

            h_res = client.add_event_handler(on_new_channel_message, events.NewMessage(chats=resolved_entities))
            if inspect.iscoroutine(h_res):
                await h_res
            self._active_channel_handlers[user_id] = on_new_channel_message

            # Start watchdog poll task covering all entities
            task = asyncio.create_task(self._user_channel_poll_loop(user_id, client, resolved_entities))
            self._user_poll_tasks[user_id] = task

            logger.info(f"Real-time Multi-Channel Monitoring ACTIVE for user {user_id} ({len(resolved_entities)} channels)")
            return True

        except Exception as e:
            logger.warning(f"Could not start monitor for user {user_id} ({st.source_channel}): {e}")
            return False

    def stop_monitor_for_user(self, user_id: int, client: Optional[Any] = None):
        """Stops background watchdog and MTProto event handler for user"""
        task = self._user_poll_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        handler = self._active_channel_handlers.pop(user_id, None)
        cli = client or self._user_clients.get(user_id)
        if cli and handler:
            try:
                cli.remove_event_handler(handler)
            except Exception as e:
                logger.debug(f"Could not remove event handler for user {user_id}: {e}")

    async def start_all_active_monitors(self):
        """Loads and starts monitoring for all active users on bot startup"""
        try:
            active_settings = await db_manager.get_all_active_story_settings()
            logger.info(f"Initializing {len(active_settings)} active story monitors...")
            for st in active_settings:
                if st.is_active and st.source_channel:
                    # Strict VIP check on startup
                    if await db_manager.is_vip(st.user_id):
                        asyncio.create_task(self.start_monitor_for_user(st.user_id))
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
        Fetches recent listings from the configured source channel,
        strictly finds one matching the real estate filter ($700+),
        and immediately posts it as a Telegram Story for the user to verify live on mobile!
        """
        if not await db_manager.is_vip(user_id):
            return False, "Ko'chmas Mulk Auto-Story Cloner faqat VIP Cheksiz tarif egalari uchun! Iltimos, VIP tarifga o'ting.", None

        client = await self.get_client_for_user(user_id)
        if not client or not client.is_connected():
            return False, "Telegram hisobingiz ulanmagan! Avval 'Telegram Akkauntni Ulash' bo'limidan hisobingizni ulang.", None

        st = await db_manager.get_story_settings(user_id)
        if not st.source_channel:
            return False, "Manba kanal sozlanmagan! Avval 'Manba Kanal' bo'limida kanalingizni kiriting.", None

        try:
            entity = await client.get_entity(st.source_channel)
        except Exception as e:
            return False, f"Kanalga ulanib bo'lmadi ({st.source_channel}): {e}", None

        # Fetch recent posts from source channel
        try:
            messages = await client.get_messages(entity, limit=25)
        except Exception as e:
            return False, f"Xabarlarni o'qib bo'lmadi: {e}", None

        matching_msg = None
        detected_price = None

        for msg in messages:
            if not msg or getattr(msg, 'action', None) is not None:
                continue
            is_match, reason, price = self.matches_filter(msg, st)
            if is_match:
                matching_msg = msg
                detected_price = price
                break

        if not matching_msg:
            return False, (
                f"Kanaldagi oxirgi 25 ta post ichida narxi ${st.min_price:g}+ bo'lgan rasmli e'lon topilmadi. "
                f"Kanalga mos e'lon joylang yoki test qilish uchun vaqtincha minimal narxni pasaytirib ko'ring."
            ), None

        # Download media to use as listing blur background if requested
        photo_path = None
        if st.background_style == "listing_blur" and matching_msg.photo:
            try:
                temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
                os.makedirs(temp_dir, exist_ok=True)
                photo_path = await client.download_media(matching_msg, file=temp_dir)
            except Exception:
                photo_path = None

        # Post to story
        ok, story_id, res_msg, story_url = await self.post_story_from_channel(
            client=client,
            channel_identifier=entity,
            msg_id=matching_msg.id,
            bg_style=st.background_style,
            source_photo_path=photo_path,
            user_id=user_id,
            pinned=st.pin_to_profile
        )

        if photo_path and os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        if ok:
            caption_snip = (getattr(matching_msg, 'message', '') or '')[:150]
            grouped_id = getattr(matching_msg, 'grouped_id', None)
            await db_manager.record_posted_story(
                user_id=user_id,
                source_channel=st.source_channel,
                source_id=getattr(entity, 'id', None),
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
                "caption": caption_snip
            }
            return True, res_msg, detail
        else:
            return False, res_msg, None

    # ==========================================
    # --- CENTRAL LISTENER DISPATCHER ---
    # ==========================================

    async def handle_incoming_channel_message(self, message: Any, chat: Any):
        """
        Invoked by the central Telethon listener whenever a new message appears in any channel.
        Checks all active story settings and automatically publishes stories!
        """
        if not message or not chat:
            return

        chat_id = getattr(chat, 'id', None)
        username = getattr(chat, 'username', '') or ''
        username = username.lower().lstrip('@')

        active_settings = await db_manager.get_all_active_story_settings()
        if not active_settings:
            return

        for st in active_settings:
            if not st.is_active:
                continue

            # Strict VIP restriction
            if not await db_manager.is_vip(st.user_id):
                continue

            # Match source channel
            clean_src = st.source_channel.lower().lstrip('@')
            is_match = False
            if st.source_id and st.source_id == chat_id:
                is_match = True
            elif username and clean_src == username:
                is_match = True
            elif str(chat_id).replace('-100', '').lstrip('-') == clean_src:
                is_match = True

            if not is_match:
                continue

            client = await self.get_client_for_user(st.user_id)
            if not client or not client.is_connected():
                continue

            await self._process_channel_message(client, st.user_id, st, message, chat)

    async def delete_story_by_id(self, user_id: int, story_id: int, peer_entity: Any = None) -> bool:
        """Deletes an active story via userbot MTProto DeleteStoriesRequest"""
        client = await self.get_client_for_user(user_id)
        if not client or not client.is_connected():
            return False
        try:
            from telethon.tl.functions.stories import DeleteStoriesRequest
            if peer_entity:
                await client(DeleteStoriesRequest(peer=peer_entity, id=[story_id]))
            else:
                await client(DeleteStoriesRequest(id=[story_id]))
            logger.info(f"Successfully deleted active story #{story_id} for user {user_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete story #{story_id} via MTProto: {e}")
            return False

    async def handle_source_message_deleted(self, source_channel: str, source_msg_id: int):
        """Finds posted stories corresponding to source message and deletes active story from Telegram"""
        try:
            from database.db_manager import db_manager
            clean_chan = (source_channel or "").lstrip("@").lower().strip()
            async with db_manager.get_connection() as db:
                cursor = await db.execute("""
                    SELECT id, user_id, story_id, target_type, status
                    FROM posted_stories
                    WHERE (LOWER(TRIM(REPLACE(source_channel, '@', ''))) = ?)
                      AND source_msg_id = ?
                      AND status = 'success'
                """, (clean_chan, source_msg_id))
                rows = await cursor.fetchall()

            for r in rows:
                p_id, u_id, st_id, target_type = r[0], r[1], r[2], r[3]
                if st_id:
                    peer_entity = None
                    if target_type == "channel":
                        st = await self.get_settings(u_id)
                        if st and st.target_channel:
                            try:
                                client = await self.get_client_for_user(u_id)
                                if client and client.is_connected():
                                    peer_entity = await client.get_input_entity(st.target_channel)
                            except Exception as pe_err:
                                logger.warning(f"Could not resolve peer entity for channel story deletion: {pe_err}")
                    if peer_entity:
                        await self.delete_story_by_id(user_id=u_id, story_id=st_id, peer_entity=peer_entity)
                    else:
                        await self.delete_story_by_id(user_id=u_id, story_id=st_id)
                    async with db_manager.write_transaction() as db:
                        await db.execute("UPDATE posted_stories SET status = 'deleted' WHERE id = ?", (p_id,))
                        await db.commit()
                    logger.info(f"Marked posted_story #{p_id} as deleted for source msg #{source_msg_id}")
        except Exception as e:
            logger.error(f"Error in handle_source_message_deleted: {e}", exc_info=True)

    async def stop_all(self):
        """Gracefully terminates all active user watchdogs, queue worker, and Telethon client sessions"""
        logger.info("Stopping all Story Cloner monitors and workers...")
        story_queue_service.stop_worker()
        for user_id in list(self._user_poll_tasks.keys()):
            self.stop_monitor_for_user(user_id)
        for user_id, client in list(self._user_clients.items()):
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception as e:
                logger.debug(f"Client disconnect note for user {user_id}: {e}")
        self._user_clients.clear()
        self._active_channel_handlers.clear()
        logger.info("Story Cloner service stopped successfully.")


story_cloner_service = StoryClonerService()
