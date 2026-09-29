"""
Telegram Mini App REST API.

This process is the single backend of the Mini App and works directly on the bot's own SQLite
database, so every change made in the Mini App is immediately visible to the cloning engine.

Security model
--------------
Every `/api/*` route (except the public audio previews used by `<audio src>`) requires the
`X-Telegram-Init-Data` header. The value is verified with HMAC-SHA256 against the bot token as
described in https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
and rejected when older than INIT_DATA_MAX_AGE. The authenticated Telegram user id is the only
identity used; client supplied ids are never trusted. Every pair / order access is ownership
checked, and admin-only data (logs, server telemetry, supplier balance) requires an admin account.

The Mini App enforces the same business rules as the bot UI: a pair may only publish into a
channel that both the bot and the requesting user administer, pairs may not form cloning loops,
plan limits and paid features are checked server-side, and every user has a per-minute request
budget (HTTP 429 with Retry-After). The SPA is served same-origin by this process, so no CORS
headers are emitted.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl

from aiohttp import web

from config.limits import (
    BACKFILL_MAX_MESSAGES,
    BACKFILL_MAX_MESSAGES_PRIVILEGED,
    BLACKLIST_MAX_CHARS,
    DRIP_DELAY_MAX_MINUTES,
    REPLACE_WORDS_MAX_CHARS,
    SIGNATURE_MAX_CHARS,
    STORY_DRIP_DELAY_MAX_MINUTES,
    STORY_MAX_PER_DAY_MAX,
    STORY_MAX_PER_DAY_MIN,
    STORY_VIDEO_DURATION_MAX,
    STORY_VIDEO_DURATION_MIN,
    WATERMARK_TEXT_MAX_CHARS,
)
from config.plans import PLANS, TIER_MAX_CHANNELS, TRIAL_DAYS
from config.settings import PROJECT_ROOT, settings
from database.db_manager import db_manager
from database.models import ChannelPair, StorySettings

logger = logging.getLogger("MiniAppAPI")

INIT_DATA_MAX_AGE = 24 * 3600
ASSETS_DIR = os.path.join(str(PROJECT_ROOT), "assets")
AUDIO_DIR = os.path.join(ASSETS_DIR, "audio")
APP_LOG_FILE = os.path.join(str(PROJECT_ROOT), "data", "app.log")
AUDIO_FILENAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}\.mp3$")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
LANG_RE = re.compile(r"^(auto|[a-z]{2,3}(-[A-Za-z]{2,4})?)$")
INTEGER_RE = re.compile(r"^\s*[+-]?\d{1,18}\s*$")
LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_TOPIC_ID = 2_147_483_647
MAX_ROW_ID = 2 ** 62
MAX_STORE_QUANTITY = 10_000_000
DEFAULT_SUPPLIER_MARGIN = 25.0

WATERMARK_TYPES = {"none", "text", "logo"}
WATERMARK_POSITIONS = {
    "top_left", "top_center", "top_right", "center_left", "center",
    "center_right", "bottom_left", "bottom_center", "bottom_right",
}
NIGHT_MODES = {"off", "silent", "buffer"}
PARAPHRASE_MODES = {"off", "short", "hype", "formal", "luxury", "urgency", "conversational"}
TONES = {"standard", "luxury", "urgency", "conversational", "off"}
AD_ACTIONS = {"clean", "drop", "swap", "off"}
CLONE_MODES = {"clean", "forward"}
STORY_TARGET_TYPES = {"self", "channel"}
# Same choices as the bot's story design keyboard (bot/keyboards/story_keyboards.py)
STORY_BACKGROUNDS = {"telegram_green", "listing_blur", "luxury_dark", "emerald"}

# Per-user request budgets (sliding 60 s window). Every authenticated request counts against the
# general budget; the routes below additionally have their own, stricter budget because they call
# Telegram, spend the central MTProto account or create payment invoices.
RATE_LIMIT_WINDOW_SECONDS = 60.0
GENERAL_RATE_LIMIT = 60
ROUTE_RATE_LIMITS: Dict[Tuple[str, str], int] = {
    ("POST", "/api/pairs"): 10,
    ("POST", "/api/pairs/{id}/test-post"): 5,
    ("POST", "/api/pairs/{id}/backfill"): 3,
    ("POST", "/api/billing/checkout"): 5,
    ("POST", "/api/store/buy"): 5,
}

_background_jobs: set = set()
# Running Mini App history backfills per requesting user (at most one at a time)
_user_backfills: Dict[int, asyncio.Task] = {}

# Typed request/app storage keys (plain strings as a fallback for older aiohttp versions)
_RequestKey = getattr(web, "RequestKey", None)
_AppKey = getattr(web, "AppKey", None)
TG_USER_KEY = _RequestKey("tg_user", dict) if _RequestKey else "tg_user"
USER_ID_KEY = _RequestKey("user_id", int) if _RequestKey else "user_id"


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, message: str, code: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.headers = headers
        self.extra = extra


def json_response(data: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> web.Response:
    merged = {"Cache-Control": "no-store"}
    if headers:
        merged.update(headers)
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json",
        headers=merged,
    )


class SlidingWindowRateLimiter:
    """In-memory per-key sliding-window limiter (a single process serves the whole Mini App)."""

    def __init__(self, max_keys: int = 20000):
        self._hits: Dict[Any, Deque[float]] = {}
        self._max_keys = max_keys

    def hit(self, key: Any, limit: int, window: float, now: Optional[float] = None) -> float:
        """Counts one request for `key`. Returns 0 when the request is allowed, otherwise the number of
        seconds until a slot frees up (a rejected request is not counted)."""
        now = time.monotonic() if now is None else now
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self._max_keys:
                self._prune(now, window)
            hits = self._hits[key] = deque()
        cutoff = now - window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return max(hits[0] + window - now, 0.001)
        hits.append(now)
        return 0.0

    def _prune(self, now: float, window: float) -> None:
        cutoff = now - window
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[key]
        if len(self._hits) >= self._max_keys:
            # Every key is busy: start over rather than grow without bound
            self._hits.clear()

    def reset(self) -> None:
        self._hits.clear()


RATE_LIMITER_KEY = _AppKey("api_rate_limiter", SlidingWindowRateLimiter) if _AppKey else "api_rate_limiter"


def validate_init_data(init_data: str, bot_token: str, max_age: int = INIT_DATA_MAX_AGE,
                       now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Verifies Telegram WebApp initData and returns the embedded user dict, or None if invalid."""
    if not init_data or not bot_token:
        return None
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received_hash = fields.pop("hash", "")
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        return None
    current = time.time() if now is None else now
    if auth_date <= 0 or current - auth_date > max_age:
        return None
    try:
        user = json.loads(fields.get("user", ""))
    except ValueError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def _user_id(request: web.Request) -> int:
    return request[USER_ID_KEY]


async def _is_admin(request: web.Request) -> bool:
    return await db_manager.is_admin(_user_id(request))


async def _require_admin(request: web.Request):
    if not await _is_admin(request):
        raise ApiError(403, "Ushbu bo'lim faqat administratorlar uchun", "ADMIN_REQUIRED")


def _require_bot():
    from services.cloner_engine import cloner_engine
    bot = cloner_engine.bot
    if bot is None:
        raise ApiError(503, "Bot hali ishga tushmagan. Birozdan so'ng qayta urinib ko'ring.", "BOT_UNAVAILABLE")
    return bot


async def _read_json(request: web.Request) -> Dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "Noto'g'ri JSON so'rov", "INVALID_JSON")
    if not isinstance(body, dict):
        raise ApiError(400, "JSON obyekt kutilgan", "INVALID_JSON")
    return body


def _int_param(value: Any, name: str, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    """Accepts JSON integers, integral floats (15.0) and decimal strings (query parameters)."""
    number: Optional[int] = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and INTEGER_RE.fullmatch(value):
        number = int(value)
    if number is None:
        raise ApiError(400, f"'{name}' butun son bo'lishi kerak", "INVALID_FIELD")
    if minimum is not None and number < minimum:
        raise ApiError(400, f"'{name}' kamida {minimum} bo'lishi kerak", "INVALID_FIELD")
    if maximum is not None and number > maximum:
        raise ApiError(400, f"'{name}' ko'pi bilan {maximum} bo'lishi kerak", "INVALID_FIELD")
    return number


def _float_param(value: Any, name: str, minimum: float = 0.0, maximum: float = 1e12) -> float:
    number: Optional[float] = None
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            number = None
    if number is None or not math.isfinite(number):
        raise ApiError(400, f"'{name}' son bo'lishi kerak", "INVALID_FIELD")
    if not (minimum <= number <= maximum):
        raise ApiError(400, f"'{name}' {minimum:g}..{maximum:g} oralig'ida bo'lishi kerak", "INVALID_FIELD")
    return number


def _text_param(value: Any, name: str, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"'{name}' matn bo'lishi kerak", "INVALID_FIELD")
    if len(value) > max_len:
        raise ApiError(400, f"'{name}' {max_len} belgidan oshmasligi kerak", "INVALID_FIELD")
    if "\x00" in value:
        raise ApiError(400, f"'{name}' ruxsat etilmagan belgini o'z ichiga oladi", "INVALID_FIELD")
    return value


def _choice_param(value: Any, name: str, allowed: set) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ApiError(400, f"'{name}' qiymati noto'g'ri", "INVALID_FIELD")
    return value


def _bool_param(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ApiError(400, f"'{name}' true/false bo'lishi kerak", "INVALID_FIELD")


def _lang_param(value: Any, name: str, allow_auto: bool) -> str:
    if not isinstance(value, str) or not LANG_RE.fullmatch(value) or (value == "auto" and not allow_auto):
        raise ApiError(400, f"'{name}' qiymati noto'g'ri", "INVALID_FIELD")
    return value


def _topic_param(value: Any, name: str) -> Optional[int]:
    if value in (None, "", 0):
        return None
    return _int_param(value, name, 1, MAX_TOPIC_ID)


def _logo_path_param(value: Any, name: str) -> str:
    """Logo watermarks reference an image file on the server; only images inside assets/ are allowed.
    The path is stored relative to the project root (the working directory of the bot)."""
    raw = _text_param(value, name, 255).strip()
    resolved = ""
    inside = False
    if raw:
        try:
            candidate = raw if os.path.isabs(raw) else os.path.join(str(PROJECT_ROOT), raw)
            resolved = os.path.realpath(candidate)
            assets_root = os.path.realpath(ASSETS_DIR)
            inside = os.path.commonpath([assets_root, resolved]) == assets_root
        except (ValueError, OSError):
            inside = False
    if (not inside or not os.path.isfile(resolved)
            or os.path.splitext(resolved)[1].lower() not in LOGO_EXTENSIONS):
        raise ApiError(400, "Logo fayli faqat assets/ papkasidagi mavjud rasm (PNG, JPG, WEBP) bo'lishi mumkin",
                       "INVALID_FIELD")
    return os.path.relpath(resolved, os.path.realpath(str(PROJECT_ROOT))).replace(os.sep, "/")


def _watermark_text_param(value: Any, name: str) -> str:
    return _text_param(value, name, WATERMARK_TEXT_MAX_CHARS).strip()


def normalize_channel_ref(raw: Any) -> Tuple[str, Optional[int]]:
    """Normalizes user input (@name, t.me link, invite link, numeric id) to (channel_ref, numeric_id)."""
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, "Kanal manzili kiritilmagan", "INVALID_CHANNEL")
    ref = raw.strip()
    ref = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me|telegram\.dog)/", "", ref, flags=re.IGNORECASE)
    if ref.startswith("+") or ref.lower().startswith("joinchat/"):
        invite = ref[1:] if ref.startswith("+") else ref[len("joinchat/"):]
        if not re.fullmatch(r"[A-Za-z0-9_\-]{8,64}", invite):
            raise ApiError(400, "Taklif havolasi noto'g'ri", "INVALID_CHANNEL")
        return f"https://t.me/+{invite}", None
    ref = ref.split("/")[0].split("?")[0].lstrip("@")
    if re.fullmatch(r"-?\d{5,20}", ref):
        digits = ref.lstrip("-")
        if digits.startswith("100") and ref.startswith("-"):
            numeric = int(ref)
        else:
            numeric = int(f"-100{digits}")
        return str(numeric), numeric
    if USERNAME_RE.fullmatch(ref):
        return f"@{ref}", None
    raise ApiError(400, "Kanal manzili noto'g'ri. Misol: @kanal yoki https://t.me/kanal", "INVALID_CHANNEL")


async def _get_owned_pair(request: web.Request) -> ChannelPair:
    pair_id = _int_param(request.match_info.get("id"), "id", 1, MAX_ROW_ID)
    pair = await db_manager.get_pair_by_id(pair_id)
    if not pair:
        raise ApiError(404, "Kanal juftligi topilmadi", "NOT_FOUND")
    if pair.user_id != _user_id(request) and not await _is_admin(request):
        # Do not reveal that the pair exists
        raise ApiError(404, "Kanal juftligi topilmadi", "NOT_FOUND")
    return pair


async def _owner_is_admin(request: web.Request, pair: ChannelPair) -> bool:
    """Admins (requester or pair owner) are not bound by plan limits, like in the bot UI."""
    return await _is_admin(request) or await db_manager.is_admin(pair.user_id)


def _spawn_job(coro):
    task = asyncio.create_task(coro)
    _background_jobs.add(task)

    def _done(t: asyncio.Task):
        _background_jobs.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error(f"Mini App background job failed: {t.exception()!r}", exc_info=t.exception())

    task.add_done_callback(_done)
    return task


def _serialize_pair(p: ChannelPair) -> Dict[str, Any]:
    return {
        "id": p.id,
        "user_id": p.user_id,
        "source_channel": p.source_channel,
        "source_title": p.source_title or p.source_channel,
        "target_channel": p.target_channel,
        "target_title": p.target_title or p.target_channel,
        "is_active": bool(p.is_active),
        "clone_mode": p.clone_mode,
        "clean_links": bool(p.clean_links),
        "custom_signature": p.custom_signature or "",
        "remove_signature": bool(p.remove_signature),
        "blacklist_words": p.blacklist_words or "",
        "replace_words": p.replace_words or "",
        "auto_translate": bool(p.auto_translate),
        "target_lang": p.target_lang or "uz",
        "source_lang": p.source_lang or "auto",
        "image_watermark_type": p.image_watermark_type or "none",
        "image_watermark_text": p.image_watermark_text or "",
        "image_watermark_pos": p.image_watermark_pos or "bottom_right",
        "video_watermark_type": p.video_watermark_type or "none",
        "video_watermark_text": p.video_watermark_text or "",
        "video_watermark_pos": p.video_watermark_pos or "bottom_right",
        "drip_delay_minutes": p.drip_delay_minutes or 0,
        "night_mode": p.night_mode or "off",
        "ai_paraphrase_mode": p.ai_paraphrase_mode or "off",
        "tone_of_voice": p.tone_of_voice or "standard",
        "ad_action": p.ad_action or "clean",
        "source_topic_id": p.source_topic_id,
        "target_topic_id": p.target_topic_id,
        "created_at": p.created_at,
    }


# Field validators for PUT /api/pairs/{id} (watermark texts are validated separately: they double as a
# logo path when the watermark type is "logo")
PAIR_FIELD_VALIDATORS: Dict[str, Callable[[Any], Any]] = {
    "is_active": lambda v: _bool_param(v, "is_active"),
    "clean_links": lambda v: _bool_param(v, "clean_links"),
    "clone_mode": lambda v: _choice_param(v, "clone_mode", CLONE_MODES),
    "custom_signature": lambda v: _text_param(v, "custom_signature", SIGNATURE_MAX_CHARS),
    "remove_signature": lambda v: _bool_param(v, "remove_signature"),
    "blacklist_words": lambda v: _text_param(v, "blacklist_words", BLACKLIST_MAX_CHARS),
    "replace_words": lambda v: _text_param(v, "replace_words", REPLACE_WORDS_MAX_CHARS),
    "auto_translate": lambda v: _bool_param(v, "auto_translate"),
    "target_lang": lambda v: _lang_param(v, "target_lang", allow_auto=False),
    "source_lang": lambda v: _lang_param(v, "source_lang", allow_auto=True),
    "image_watermark_type": lambda v: _choice_param(v, "image_watermark_type", WATERMARK_TYPES),
    "image_watermark_pos": lambda v: _choice_param(v, "image_watermark_pos", WATERMARK_POSITIONS),
    "video_watermark_type": lambda v: _choice_param(v, "video_watermark_type", WATERMARK_TYPES),
    "video_watermark_pos": lambda v: _choice_param(v, "video_watermark_pos", WATERMARK_POSITIONS),
    "drip_delay_minutes": lambda v: _int_param(v, "drip_delay_minutes", 0, DRIP_DELAY_MAX_MINUTES),
    "night_mode": lambda v: _choice_param(v, "night_mode", NIGHT_MODES),
    "ai_paraphrase_mode": lambda v: _choice_param(v, "ai_paraphrase_mode", PARAPHRASE_MODES),
    "tone_of_voice": lambda v: _choice_param(v, "tone_of_voice", TONES),
    "ad_action": lambda v: _choice_param(v, "ad_action", AD_ACTIONS),
    "source_topic_id": lambda v: _topic_param(v, "source_topic_id"),
    "target_topic_id": lambda v: _topic_param(v, "target_topic_id"),
}


def _same_value(new: Any, current: Any) -> bool:
    try:
        return bool(new == current)
    except Exception:
        return False


def _validate_pair_update(body: Dict[str, Any], pair: ChannelPair) -> Tuple[Dict[str, Any], bool]:
    """Returns (changes, known_fields_sent). Values equal to the stored ones are skipped, so a client that
    re-sends an unchanged form is never rejected because of values it did not touch."""
    current = _serialize_pair(pair)
    known = False
    updates: Dict[str, Any] = {}
    for field, validator in PAIR_FIELD_VALIDATORS.items():
        if field not in body:
            continue
        known = True
        if not _same_value(body[field], current.get(field)):
            updates[field] = validator(body[field])

    for kind in ("image", "video"):
        text_key, type_key = f"{kind}_watermark_text", f"{kind}_watermark_type"
        wm_type = updates.get(type_key, current[type_key])
        if text_key in body:
            known = True
            if not _same_value(body[text_key], current[text_key]):
                if wm_type == "logo":
                    updates[text_key] = _logo_path_param(body[text_key], text_key)
                else:
                    updates[text_key] = _watermark_text_param(body[text_key], text_key)
        if updates.get(type_key) == "logo" and text_key not in updates:
            # Switching to a logo watermark: the stored text must already be a valid logo path
            updates[text_key] = _logo_path_param(current[text_key], text_key)
    return updates, known


PRO_FEATURE_TIERS = ("pro", "vip")


def _watermark_enabled(kind: str) -> Callable[[Any, Dict[str, Any]], bool]:
    return lambda value, state: state[f"{kind}_watermark_type"] != "none"


# Paid pair features, mirroring the plan gates of the bot settings menu. Each entry is
# field -> (required tier, predicate(new_value, resulting_state), feature name). A change is gated only
# when it switches a feature on or edits a feature that stays on; switching a feature off is always
# allowed. Auto-translate and the ad filter action stay available during the trial.
PAIR_FEATURE_GATES: Dict[str, Tuple[str, Callable[[Any, Dict[str, Any]], bool], str]] = {
    "image_watermark_type": ("pro", lambda v, s: v != "none", "Rasmga suv belgisi (Watermark)"),
    "image_watermark_text": ("pro", _watermark_enabled("image"), "Rasmga suv belgisi (Watermark)"),
    "image_watermark_pos": ("pro", _watermark_enabled("image"), "Rasmga suv belgisi (Watermark)"),
    "video_watermark_type": ("pro", lambda v, s: v != "none", "Video suv belgisi (Watermark)"),
    "video_watermark_text": ("pro", _watermark_enabled("video"), "Video suv belgisi (Watermark)"),
    "video_watermark_pos": ("pro", _watermark_enabled("video"), "Video suv belgisi (Watermark)"),
    "ai_paraphrase_mode": ("pro", lambda v, s: v != "off", "AI Content Paraphraser"),
    "tone_of_voice": ("pro", lambda v, s: v not in ("standard", "off"), "AI ohang (Tone of Voice)"),
    "drip_delay_minutes": ("pro", lambda v, s: v > 0, "Drip Feed kechikishi"),
    "night_mode": ("pro", lambda v, s: v != "off", "Tungi rejim"),
}


async def _enforce_feature_gates(request: web.Request, pair: ChannelPair, updates: Dict[str, Any]) -> None:
    state = {**_serialize_pair(pair), **updates}
    required = [
        (tier, label)
        for field, (tier, enables, label) in PAIR_FEATURE_GATES.items()
        if field in updates and enables(updates[field], state)
    ]
    if not required or await _owner_is_admin(request, pair):
        return
    sub = await db_manager.get_user_subscription(pair.user_id)
    for tier, label in required:
        if tier == "vip" and not sub.is_vip:
            raise ApiError(402, f"{label} faqat VIP tarifida mavjud. 'Tariflar' bo'limidan VIP ga o'ting.",
                           "VIP_REQUIRED")
        if tier == "pro" and not (sub.is_active and sub.tier in PRO_FEATURE_TIERS):
            raise ApiError(402, f"{label} faqat PRO va VIP tariflarida mavjud. 'Tariflar' bo'limidan tarifni oshiring.",
                           "PRO_REQUIRED")


async def _set_pair_active(request: web.Request, pair: ChannelPair, active: bool) -> bool:
    """Owner pause/resume through db_manager.set_pair_active_by_owner (keeps paused_by_user and plan limits)."""
    new_state, error = await db_manager.set_pair_active_by_owner(
        pair.id, active, bypass_limits=await _is_admin(request)
    )
    if error == "not_found":
        raise ApiError(404, "Kanal juftligi topilmadi", "NOT_FOUND")
    if error == "self_loop":
        raise ApiError(409, "Manba va maqsad bir xil kanal — bunday juftlikni yoqib bo'lmaydi.", "SELF_LOOP")
    if error == "cycle":
        raise ApiError(409, "Bu juftlikni yoqish kanallar orasida cheksiz halqa (loop) hosil qiladi.", "PAIR_CYCLE")
    if error == "duplicate":
        raise ApiError(409, "Xuddi shu manba va maqsad bilan boshqa faol juftligingiz bor. Takror juftlikni o'chirib yuboring.", "DUPLICATE")
    if error == "subscription_inactive":
        raise ApiError(402, "Obuna yoki sinov muddati tugagan. Kanalni yoqish uchun 'Tariflar' bo'limidan tarifni yangilang.",
                       "SUBSCRIPTION_INACTIVE")
    if error == "plan_limit":
        sub = await db_manager.get_user_subscription(pair.user_id)
        raise ApiError(409, f"Tarifingiz {sub.max_channels} ta faol kanalga ruxsat beradi. Avval boshqa kanalni "
                            f"to'xtating yoki tarifni oshiring.", "PLAN_LIMIT")
    if error:
        raise ApiError(400, "Kanal holatini o'zgartirib bo'lmadi", "TOGGLE_FAILED")
    return bool(new_state)


# ---------------------------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------------------------

def _route_pattern(request: web.Request) -> str:
    route = getattr(request.match_info, "route", None)
    resource = getattr(route, "resource", None)
    return getattr(resource, "canonical", None) or request.path


def _enforce_rate_limits(request: web.Request, user_id: int) -> None:
    limiter = request.app.get(RATE_LIMITER_KEY)
    if limiter is None:
        return
    retry_after = limiter.hit(("*", user_id), GENERAL_RATE_LIMIT, RATE_LIMIT_WINDOW_SECONDS)
    if not retry_after:
        route_key = (request.method, _route_pattern(request))
        route_limit = ROUTE_RATE_LIMITS.get(route_key)
        if route_limit:
            retry_after = limiter.hit((route_key, user_id), route_limit, RATE_LIMIT_WINDOW_SECONDS)
    if retry_after:
        seconds = max(1, math.ceil(retry_after))
        raise ApiError(429, f"So'rovlar juda ko'p. {seconds} soniyadan so'ng qayta urinib ko'ring.", "RATE_LIMITED",
                       headers={"Retry-After": str(seconds)}, extra={"retry_after": seconds})


@web.middleware
async def api_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    try:
        public_audio = request.method in ("GET", "HEAD") and request.path.startswith("/api/audio-tracks/")
        unknown_route = getattr(request.match_info, "handler", None) is api_not_found
        if not public_audio and not unknown_route:
            tg_user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), settings.BOT_TOKEN)
            if not tg_user:
                raise ApiError(401, "Mini App faqat Telegram ichida ochiladi. Iltimos, botdan qayta oching.",
                               "TELEGRAM_AUTH_REQUIRED")
            user_id = int(tg_user["id"])
            request[TG_USER_KEY] = tg_user
            request[USER_ID_KEY] = user_id
            _enforce_rate_limits(request, user_id)
            if not await db_manager.can_user_access_bot(user_id):
                raise ApiError(403, "Bot yopiq rejimda. Kirish uchun administrator bilan bog'laning.", "PRIVATE_MODE")
        return await handler(request)
    except ApiError as err:
        payload: Dict[str, Any] = {"error": err.message}
        if err.code:
            payload["code"] = err.code
        if err.extra:
            payload.update(err.extra)
        return json_response(payload, err.status, headers=err.headers)
    except web.HTTPException as exc:
        if exc.status == 413:
            return json_response({"error": "So'rov hajmi juda katta", "code": "PAYLOAD_TOO_LARGE"}, 413)
        if exc.status >= 400:
            return json_response({"error": exc.reason or "So'rov bajarilmadi", "code": "HTTP_ERROR"}, exc.status)
        raise
    except Exception:
        logger.exception(f"Unhandled Mini App API error on {request.method} {request.path}")
        return json_response({"error": "Ichki server xatosi. Keyinroq urinib ko'ring.", "code": "INTERNAL"}, 500)


# ---------------------------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------------------------

def _billing_catalog() -> Dict[str, Any]:
    """Plan prices from config.plans, so the Mini App never shows a price that differs from the invoice."""
    return {
        "trial_days": TRIAL_DAYS,
        "plans": [
            {
                "key": key,
                "tier": plan["tier"],
                "title": plan["title"],
                "stars": plan["stars"],
                "days": plan["days"],
                "max_channels": TIER_MAX_CHANNELS.get(plan["tier"], 1),
            }
            for key, plan in PLANS.items()
            if key in ("pro", "vip")
        ],
    }


async def api_me(request: web.Request) -> web.Response:
    tg_user = request[TG_USER_KEY]
    user_id = _user_id(request)
    full_name = " ".join(filter(None, [tg_user.get("first_name"), tg_user.get("last_name")])).strip() or "Telegram User"
    user = await db_manager.get_or_create_user(user_id=user_id, full_name=full_name, username=tg_user.get("username"))
    is_admin = await db_manager.is_admin(user_id)
    sub = await db_manager.get_user_subscription(user_id)
    stats = await db_manager.get_user_stats(user_id)
    daily = await db_manager.get_user_daily_clone_counts(user_id, days=7)
    return json_response({
        "status": "ok",
        "user": {
            "id": user.user_id,
            "full_name": user.full_name,
            "username": user.username,
            "is_admin": is_admin,
            "created_at": user.created_at,
        },
        "subscription": {
            "tier": sub.tier,
            "is_active": sub.is_active,
            "is_trial_active": sub.is_trial_active,
            "is_vip": is_admin or sub.is_vip,
            "max_channels": 999 if is_admin else sub.max_channels,
            "trial_expires_at": sub.trial_expires_at,
            "expires_at": sub.expires_at,
            "stars_spent": sub.stars_spent,
        },
        "stats": {
            "channel_pairs_count": stats["total_pairs"],
            "active_pairs_count": stats["active_pairs"],
            "total_cloned_messages": stats["total_cloned"],
            "today_cloned_messages": stats["today_cloned"],
            "daily": daily,
        },
        "billing": _billing_catalog(),
    })


async def api_feed(request: web.Request) -> web.Response:
    feed = await db_manager.get_user_recent_clones(_user_id(request), limit=10)
    return json_response({"status": "ok", "feed": feed})


# ---------------------------------------------------------------------------------------------
# Channel pairs
# ---------------------------------------------------------------------------------------------

async def api_get_pairs(request: web.Request) -> web.Response:
    pairs = await db_manager.get_user_channel_pairs(_user_id(request))
    return json_response({"status": "ok", "pairs": [_serialize_pair(p) for p in pairs]})


# verify_destination_access() error code -> (HTTP status, API error code)
DESTINATION_ERRORS: Dict[str, Tuple[int, str]] = {
    "not_found": (400, "TARGET_NOT_FOUND"),
    "wrong_type": (400, "TARGET_INVALID"),
    "bot_not_admin": (403, "BOT_NOT_ADMIN"),
    "user_not_admin": (403, "USER_NOT_ADMIN"),
}


async def _verify_destination(target_ref: str, target_id: Optional[int], user_id: int):
    """The bot AND the requesting user must be able to publish in the target channel, otherwise any user
    could clone content into another customer's channel (the bot is an admin in all of them)."""
    from services.channel_access import DESTINATION_ERROR_TEXTS, verify_destination_access
    if target_ref.startswith("https://t.me/+"):
        raise ApiError(400, "Maqsad kanal uchun taklif havolasi emas, @username yoki kanal ID sini kiriting.",
                       "TARGET_INVALID")
    bot = _require_bot()
    ok, chat, error_code = await verify_destination_access(bot, target_id if target_id is not None else target_ref, user_id)
    if not ok:
        status, code = DESTINATION_ERRORS.get(error_code, (400, "TARGET_NOT_FOUND"))
        raise ApiError(status, DESTINATION_ERROR_TEXTS.get(error_code, DESTINATION_ERROR_TEXTS["not_found"]), code)
    if chat is None or not isinstance(getattr(chat, "id", None), int):
        raise ApiError(400, DESTINATION_ERROR_TEXTS["not_found"], "TARGET_NOT_FOUND")
    return chat


async def _resolve_source(source_ref: str) -> Tuple[Optional[int], Optional[str]]:
    from services.telethon_listener import telethon_listener
    if not telethon_listener.is_connected():
        return None, None
    try:
        entity = await telethon_listener.resolve_entity(source_ref)
    except Exception:
        entity = None
    if entity is None:
        raise ApiError(400, "Manba kanal topilmadi yoki yopiq", "SOURCE_NOT_FOUND")
    if type(entity).__name__ == "User":
        raise ApiError(400, "Manba shaxsiy profil bo'lishi mumkin emas — faqat kanal", "SOURCE_INVALID")
    return getattr(entity, "id", None), getattr(entity, "title", None)


async def _ensure_can_add_pair(user_id: int, is_admin: bool) -> None:
    can_add, max_allowed, current = await db_manager.can_user_add_channel(user_id, is_admin=is_admin)
    if can_add:
        return
    sub = await db_manager.get_user_subscription(user_id)
    if not sub.is_active:
        raise ApiError(402, "Obuna yoki sinov muddati tugagan. Yangi kanal ulash uchun 'Tariflar' bo'limidan tarifni yangilang.",
                       "SUBSCRIPTION_INACTIVE")
    raise ApiError(409, f"Tarifingiz bo'yicha {max_allowed} ta kanal ulash mumkin (hozir {current} ta). "
                        f"Ko'proq kanal uchun tarifni oshiring.", "PLAN_LIMIT")


async def _would_create_cycle(source_ref: str, source_id: Optional[int], target_ref: str, target_id: Optional[int],
                              target_username: Optional[str]) -> bool:
    """Checks the new edge against every form in which existing pairs may store the same channels
    (numeric id when it was known, otherwise the @username)."""
    sources = [(source_ref, source_id), (source_ref, None)]
    targets = [(target_ref, target_id), (target_ref, None)]
    if target_username:
        targets.append((f"@{target_username}", None))
    checked = set()
    for s_ref, s_id in sources:
        for t_ref, t_id in targets:
            key = (s_ref, s_id, t_ref, t_id)
            if key in checked:
                continue
            checked.add(key)
            if await db_manager.would_create_cycle(s_ref, t_ref, source_id=s_id, target_id=t_id):
                return True
    return False


async def api_create_pair(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    body = await _read_json(request)
    source_ref, source_id = normalize_channel_ref(body.get("source_channel"))
    target_ref, target_id_hint = normalize_channel_ref(body.get("target_channel"))
    clone_mode = _choice_param(body.get("clone_mode", "clean"), "clone_mode", CLONE_MODES)
    clean_links = _bool_param(body.get("clean_links", True), "clean_links")
    auto_translate = _bool_param(body.get("auto_translate", False), "auto_translate")
    source_title_input = _text_param(body.get("source_title"), "source_title", 128).strip()

    is_admin = await _is_admin(request)
    # Cheap plan check first, so users over their limit never trigger Telegram lookups
    await _ensure_can_add_pair(user_id, is_admin)

    chat = await _verify_destination(target_ref, target_id_hint, user_id)
    target_id = chat.id
    target_title = getattr(chat, "title", None) or target_ref
    target_username = getattr(chat, "username", None)

    resolved_id, source_title = await _resolve_source(source_ref)
    source_id = resolved_id or source_id

    source_name = db_manager._normalize_channel_name(source_ref)
    target_names = {db_manager._normalize_channel_name(target_ref)}
    if target_username:
        target_names.add(db_manager._normalize_channel_name(target_username))
    same_id = source_id is not None and db_manager.normalize_peer_id(source_id) == db_manager.normalize_peer_id(target_id)
    if source_name in target_names or same_id:
        raise ApiError(400, "Manba va maqsad kanali bir xil bo'lishi mumkin emas", "SELF_LOOP")

    # Re-check limits, duplicates and loops and insert under the write lock, so concurrent submissions
    # (double tap, two open Mini Apps) cannot exceed the plan or create the same pair twice.
    async with db_manager.write_transaction():
        await _ensure_can_add_pair(user_id, is_admin)
        existing = await db_manager.find_duplicate_pair(user_id, source_ref, target_ref, source_id, target_id)
        if existing:
            raise ApiError(409, f"Bu juftlik allaqachon mavjud (#{existing.id})", "DUPLICATE")
        if await _would_create_cycle(source_ref, source_id, target_ref, target_id, target_username):
            raise ApiError(409, "Bu juftlik klonlash halqasini hosil qiladi (masalan A → B va B → A). Postlar cheksiz "
                                "qayta ko'chirilmasligi uchun bunday ulanishga ruxsat berilmaydi.", "PAIR_CYCLE")
        pair_id = await db_manager.add_channel_pair(
            user_id=user_id,
            source_channel=source_ref,
            source_title=source_title or source_title_input or source_ref,
            source_id=source_id,
            target_channel=target_ref,
            target_title=target_title,
            target_id=target_id,
            clean_links=clean_links,
            clone_mode=clone_mode,
            auto_translate=auto_translate,
        )

    from services.telethon_listener import telethon_listener
    if telethon_listener.is_connected():
        _spawn_job(telethon_listener.join_and_monitor_channel(source_ref))

    pair = await db_manager.get_pair_by_id(pair_id)
    return json_response({
        "status": "ok",
        "message": "Kanal juftligi muvaffaqiyatli qo'shildi",
        "pair_id": pair_id,
        "pair": _serialize_pair(pair) if pair else None,
    }, 201)


async def api_get_pair_detail(request: web.Request) -> web.Response:
    pair = await _get_owned_pair(request)
    return json_response({"status": "ok", "pair": _serialize_pair(pair)})


async def api_update_pair(request: web.Request) -> web.Response:
    pair = await _get_owned_pair(request)
    updates, known = _validate_pair_update(await _read_json(request), pair)
    if not known:
        raise ApiError(400, "O'zgartirish uchun maydon yuborilmadi", "NO_FIELDS")
    # is_active never goes through the generic updater: owner pause/resume keeps paused_by_user and plan limits
    new_active = updates.pop("is_active", None)
    await _enforce_feature_gates(request, pair, updates)
    if new_active is not None and new_active != bool(pair.is_active):
        await _set_pair_active(request, pair, new_active)
    if updates:
        await db_manager.update_pair_fields(pair.id, updates)
    updated = await db_manager.get_pair_by_id(pair.id)
    if not updated:
        raise ApiError(404, "Kanal juftligi topilmadi", "NOT_FOUND")
    return json_response({"status": "ok", "message": "Sozlamalar saqlandi", "pair": _serialize_pair(updated)})


async def api_toggle_pair(request: web.Request) -> web.Response:
    pair = await _get_owned_pair(request)
    new_state = await _set_pair_active(request, pair, not pair.is_active)
    return json_response({"status": "ok", "is_active": new_state})


async def api_delete_pair(request: web.Request) -> web.Response:
    pair = await _get_owned_pair(request)
    await db_manager.delete_pair(pair.id)
    return json_response({"status": "ok", "message": "Kanal juftligi o'chirildi"})


async def api_test_post(request: web.Request) -> web.Response:
    from services.cloner_engine import cloner_engine
    pair = await _get_owned_pair(request)
    ok, message = await cloner_engine.send_test_post(pair)
    if not ok:
        raise ApiError(400, message, "TEST_POST_FAILED")
    return json_response({"status": "ok", "message": message})


BACKFILL_RESULT_TEXTS = {
    "completed": "✅ <b>Tarixiy postlarni ko'chirish yakunlandi</b> (#{pair_id})\nKo'chirildi: {cloned} ta • Xato: {failed} ta",
    "failed": "⚠️ <b>Tarixiy postlarni ko'chirib bo'lmadi</b> (#{pair_id})\nXato: {failed} ta",
    "all_cloned": "ℹ️ <b>Yangi ko'chiriladigan post topilmadi</b> (#{pair_id})\nTanlangan postlar allaqachon ko'chirilgan.",
    "source_not_found": "⚠️ <b>Tarixni ko'chirib bo'lmadi</b> (#{pair_id})\nManba kanal topilmadi yoki yopiq.",
    "client_not_connected": "⚠️ <b>Tarixni ko'chirib bo'lmadi</b> (#{pair_id})\nMTProto (userbot) ulanmagan. Keyinroq qayta urinib ko'ring.",
}


async def _run_backfill(pair: ChannelPair, count: int, requester_id: int):
    from services.telethon_listener import telethon_listener
    from services.cloner_engine import cloner_engine
    result = await telethon_listener.clone_history(pair, limit=count)
    logger.info(f"Mini App backfill for pair #{pair.id} finished: {result}")
    template = BACKFILL_RESULT_TEXTS.get(str(result.get("status")))
    bot = cloner_engine.bot
    if bot is None or template is None:
        return
    try:
        await bot.send_message(
            chat_id=requester_id,
            text=template.format(
                pair_id=int(pair.id or 0),
                cloned=int(result.get("cloned") or 0),
                failed=int(result.get("failed") or 0),
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Could not notify user about backfill result", exc_info=True)


async def api_backfill(request: web.Request) -> web.Response:
    from services.telethon_listener import telethon_listener
    pair = await _get_owned_pair(request)
    user_id = _user_id(request)
    body = await _read_json(request)

    privileged = await _owner_is_admin(request, pair)
    if not privileged:
        sub = await db_manager.get_user_subscription(pair.user_id)
        if not sub.is_active:
            raise ApiError(402, "Obuna yoki sinov muddati tugagan. Tarixni ko'chirish uchun tarifni yangilang.",
                           "SUBSCRIPTION_INACTIVE")
        privileged = sub.is_vip
    max_count = BACKFILL_MAX_MESSAGES_PRIVILEGED if privileged else BACKFILL_MAX_MESSAGES
    count = _int_param(body.get("count", body.get("limit", 20)), "count", 1, max_count)

    if not pair.is_active:
        raise ApiError(409, "Kanal juftligi to'xtatilgan. Tarixni ko'chirish uchun avval uni faollashtiring.",
                       "PAIR_INACTIVE")
    if not telethon_listener.is_connected():
        raise ApiError(503, "MTProto (userbot) hozir ulanmagan. Birozdan so'ng urinib ko'ring.", "MTPROTO_OFFLINE")
    running = telethon_listener.active_history_tasks.get(pair.id)
    if running is not None and not running.done():
        raise ApiError(409, "Bu kanal uchun ko'chirish allaqachon ketmoqda", "ALREADY_RUNNING")
    running_user = _user_backfills.get(user_id)
    if running_user is not None and not running_user.done():
        raise ApiError(409, "Sizda tarixni ko'chirish jarayoni allaqachon ketmoqda. U yakunlangach qayta urinib ko'ring.",
                       "ALREADY_RUNNING")

    task = _spawn_job(_run_backfill(pair, count, user_id))
    telethon_listener.active_history_tasks[pair.id] = task
    _user_backfills[user_id] = task

    def _forget(t: asyncio.Task, uid: int = user_id):
        if _user_backfills.get(uid) is t:
            _user_backfills.pop(uid, None)

    task.add_done_callback(_forget)
    return json_response({
        "status": "accepted",
        "message": f"{count} ta tarixiy postni ko'chirish boshlandi. Yakunlanganda bot xabar beradi.",
        "count": count,
    }, 202)


# ---------------------------------------------------------------------------------------------
# Story studio (VIP)
# ---------------------------------------------------------------------------------------------

def _serialize_story_settings(st: StorySettings) -> Dict[str, Any]:
    return {
        "user_id": st.user_id,
        "source_channel": st.source_channel or "",
        "source_title": st.source_title or "",
        "target_type": st.target_type or "self",
        "target_channel": st.target_channel or "",
        "min_price": st.min_price,
        "max_price": st.max_price,
        "require_photos": bool(st.require_photos),
        "require_price": bool(st.require_price),
        "background_style": st.background_style or "telegram_green",
        # Without a stored row nothing is monitored, whatever the model default says
        "is_active": bool(st.is_active) and st.created_at is not None,
        "prime_hours_enabled": bool(st.prime_hours_enabled),
        "prime_hours_start": st.prime_hours_start,
        "prime_hours_end": st.prime_hours_end,
        "drip_delay_minutes": st.drip_delay_minutes,
        "max_stories_per_day": st.max_stories_per_day,
        "enable_smart_badges": bool(st.enable_smart_badges),
        "pin_to_profile": bool(st.pin_to_profile),
        "video_duration": st.video_duration,
        "enable_ai_voice": bool(st.enable_ai_voice),
    }


async def api_get_story_settings(request: web.Request) -> web.Response:
    st = await db_manager.get_story_settings(_user_id(request))
    return json_response({"status": "ok", "settings": _serialize_story_settings(st)})


def _story_channel_param(value: Any, name: str) -> str:
    text = _text_param(value, name, 128).strip()
    return normalize_channel_ref(text)[0] if text else ""


STORY_FIELD_VALIDATORS: Dict[str, Callable[[Any], Any]] = {
    "source_channel": lambda v: _story_channel_param(v, "source_channel"),
    "source_title": lambda v: _text_param(v, "source_title", 128).strip(),
    "target_type": lambda v: _choice_param(v, "target_type", STORY_TARGET_TYPES),
    "target_channel": lambda v: _story_channel_param(v, "target_channel"),
    "min_price": lambda v: _float_param(v, "min_price", 0.0, 1e9),
    "max_price": lambda v: _float_param(v, "max_price", 0.0, 1e9),
    "require_photos": lambda v: _bool_param(v, "require_photos"),
    "require_price": lambda v: _bool_param(v, "require_price"),
    "background_style": lambda v: _choice_param(v, "background_style", STORY_BACKGROUNDS),
    "is_active": lambda v: _bool_param(v, "is_active"),
    "prime_hours_enabled": lambda v: _bool_param(v, "prime_hours_enabled"),
    "prime_hours_start": lambda v: _int_param(v, "prime_hours_start", 0, 23),
    "prime_hours_end": lambda v: _int_param(v, "prime_hours_end", 0, 23),
    "drip_delay_minutes": lambda v: _int_param(v, "drip_delay_minutes", 0, STORY_DRIP_DELAY_MAX_MINUTES),
    "max_stories_per_day": lambda v: _int_param(v, "max_stories_per_day", STORY_MAX_PER_DAY_MIN, STORY_MAX_PER_DAY_MAX),
    "enable_smart_badges": lambda v: _bool_param(v, "enable_smart_badges"),
    "pin_to_profile": lambda v: _bool_param(v, "pin_to_profile"),
    "video_duration": lambda v: _int_param(v, "video_duration", STORY_VIDEO_DURATION_MIN, STORY_VIDEO_DURATION_MAX),
    "enable_ai_voice": lambda v: _bool_param(v, "enable_ai_voice"),
}


async def api_save_story_settings(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    if not await db_manager.is_vip(user_id):
        raise ApiError(402, "Istoriya Kloner faqat VIP tarifda mavjud", "VIP_REQUIRED")
    body = await _read_json(request)
    st = await db_manager.get_story_settings(user_id)
    current = _serialize_story_settings(st)

    updates: Dict[str, Any] = {}
    for field, validator in STORY_FIELD_VALIDATORS.items():
        if field in body and not _same_value(body[field], current.get(field)):
            updates[field] = validator(body[field])
    if not updates:
        return json_response({"status": "ok", "message": "O'zgarish yo'q", "settings": current})

    effective = {**current, **updates}
    if effective["max_price"] and effective["max_price"] < effective["min_price"]:
        raise ApiError(400, "Maksimal narx minimal narxdan kichik bo'lishi mumkin emas", "INVALID_FIELD")
    # A changed channel invalidates the resolved Telegram id stored for the previous one
    if "source_channel" in updates:
        updates["source_id"] = None
    if "target_channel" in updates:
        updates["target_id"] = None

    was_active = current["is_active"]
    will_be_active = bool(effective["is_active"])
    if will_be_active and not was_active:
        # Same preconditions as the bot's story menu toggle
        session_info = await db_manager.get_user_session_info(user_id)
        if not session_info or not session_info.get("is_active"):
            raise ApiError(409, "Avto-monitoringni yoqish uchun avval botdagi Istoriya Kloner bo'limida Telegram "
                                "hisobingizni ulang.", "STORY_SESSION_REQUIRED")
        if not effective["source_channel"]:
            raise ApiError(400, "Avto-monitoringni yoqish uchun avval manba kanalni kiriting.", "STORY_SOURCE_REQUIRED")
    if st.created_at is None and "is_active" not in updates:
        # The first save creates the row; it must not switch monitoring on implicitly
        updates["is_active"] = False

    saved = await db_manager.update_story_settings(user_id, **updates)

    from services.story_cloner_service import story_cloner_service
    if will_be_active and (not was_active or "source_channel" in updates):
        _spawn_job(story_cloner_service.start_monitor_for_user(user_id))
    elif was_active and not will_be_active:
        story_cloner_service.stop_monitor_for_user(user_id)

    return json_response({"status": "ok", "message": "VIP Istoriya sozlamalari saqlandi",
                          "settings": _serialize_story_settings(saved)})


async def api_get_story_queue(request: web.Request) -> web.Response:
    user_id = _user_id(request)
    queue = await db_manager.get_user_story_queue(user_id)
    stats = await db_manager.get_story_stats(user_id)
    return json_response({
        "status": "ok",
        "queue": [
            {
                "id": item.id,
                "district": item.district or "—",
                "price": item.price or 0,
                "rooms": item.rooms or 0,
                "area": item.area or 0,
                "score": item.score,
                "status": item.status,
                "scheduled_at": item.scheduled_at,
            }
            for item in queue
        ],
        "posted": [
            {
                "id": idx,
                "price": rec.get("price") or 0,
                "caption": rec.get("caption_snippet") or "",
                "status": "success",
                "posted_at": rec.get("posted_at"),
            }
            for idx, rec in enumerate(stats.get("recent", []), 1)
        ],
        "stats": {"total_posted": stats.get("total_posted", 0), "today_posted": stats.get("today_posted", 0)},
    })


# ---------------------------------------------------------------------------------------------
# Audio library
# ---------------------------------------------------------------------------------------------

AUDIO_TITLES = {
    "01_luxury_corporate.mp3": ("Luxury Corporate Prestige", "Ambient Corporate"),
    "02_deep_lounge.mp3": ("Deep Penthouse Lounge", "Chill / Lounge"),
    "03_ambient_piano.mp3": ("Neoclassical Piano Dream", "Cinematic Piano"),
    "04_chill_lofi.mp3": ("Sunset Terrace Lo-Fi", "Lo-Fi Beats"),
    "05_chillout_jazz.mp3": ("Smooth Skyline Jazz", "Jazz / Mellow"),
    "06_serene_harmony.mp3": ("Serene Harmony Oasis", "Meditation Ambient"),
    "07_minimal_penthouse.mp3": ("Minimal Penthouse Groove", "Minimal Tech"),
    "08_sunset_terrace.mp3": ("Golden Hour Sunset", "Warm Chillout"),
    "09_neoclassical_estate.mp3": ("Royal Estate Strings", "Strings & Piano"),
    "10_urban_loft_groove.mp3": ("Urban Loft Modern", "Future Lounge"),
    "11_prestige_acoustic.mp3": ("Prestige Warm Acoustic", "Acoustic Guitar"),
    "12_rooftop_cocktail.mp3": ("Rooftop Cocktail Twilight", "Deep House Vibe"),
}


def _list_audio_tracks():
    if not os.path.isdir(AUDIO_DIR):
        return []
    tracks = []
    for name in sorted(os.listdir(AUDIO_DIR)):
        if not AUDIO_FILENAME_RE.fullmatch(name):
            continue
        default_title = re.sub(r"^\d+_", "", name[:-4]).replace("_", " ").title()
        title, genre = AUDIO_TITLES.get(name, (default_title, "Lounge"))
        tracks.append({"filename": name, "title": title, "genre": genre, "duration": "0:30"})
    return tracks


async def api_get_audio_tracks(request: web.Request) -> web.Response:
    return json_response({"status": "ok", "tracks": _list_audio_tracks()})


async def api_serve_audio_file(request: web.Request) -> web.StreamResponse:
    """Public preview stream (an <audio src> cannot send auth headers). Only whitelisted file names."""
    filename = request.match_info.get("filename", "")
    if not AUDIO_FILENAME_RE.fullmatch(filename):
        raise ApiError(404, "Fayl topilmadi", "NOT_FOUND")
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.isfile(path):
        raise ApiError(404, "Fayl topilmadi", "NOT_FOUND")
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------------------------------------
# System telemetry
# ---------------------------------------------------------------------------------------------

def _admin_telemetry(request: web.Request) -> Dict[str, Any]:
    """Host details (database size, memory, runtime, port) are only shown to administrators."""
    db_size_mb = 0.0
    try:
        db_size_mb = round(os.path.getsize(db_manager.db_path) / (1024 * 1024), 2)
    except OSError:
        pass
    ram_mb = None
    try:
        import psutil
        ram_mb = round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:
        logger.debug("psutil unavailable for RAM telemetry", exc_info=True)
    return {
        "keep_alive_port": request.url.port or settings.PORT,
        "db_type": "SQLite (WAL)",
        "db_size_mb": db_size_mb,
        "ram_mb": ram_mb,
        "runtime_version": f"Python {sys.version.split()[0]}",
    }


async def api_system_status(request: web.Request) -> web.Response:
    from services.telethon_listener import telethon_listener
    connected = telethon_listener.is_connected()
    is_admin = await _is_admin(request)
    telemetry: Dict[str, Any] = {
        "mtproto_connected": connected,
        "mtproto_status": "Ulangan (24/7)" if connected else "Ulanmagan",
        "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    logs: List[str] = []
    if is_admin:
        telemetry.update(_admin_telemetry(request))
        try:
            from services.log_viewer import log_viewer
            logs = log_viewer.get_recent_logs(40)
        except Exception:
            logger.debug("Log viewer unavailable", exc_info=True)
        if not logs:
            logs = await asyncio.to_thread(_tail_log_file, APP_LOG_FILE, 40)
    return json_response({"status": "ok", "is_admin": is_admin, "telemetry": telemetry, "logs": logs})


def _tail_log_file(path: str, lines: int) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            chunk = f.read().decode("utf-8", errors="ignore")
        return [ln.strip() for ln in chunk.splitlines()[-lines:] if ln.strip()]
    except OSError:
        return []


# ---------------------------------------------------------------------------------------------
# Billing (Telegram Stars)
# ---------------------------------------------------------------------------------------------

async def api_billing_checkout(request: web.Request) -> web.Response:
    from aiogram.types import LabeledPrice
    body = await _read_json(request)
    tier = body.get("tier")
    if not isinstance(tier, str) or tier not in ("pro", "vip"):
        raise ApiError(400, "Noto'g'ri tarif", "INVALID_FIELD")
    bot = _require_bot()
    plan = PLANS[tier]
    title = plan["title"][:32]
    try:
        link = await bot.create_invoice_link(
            title=title,
            description=plan["description"][:255],
            # Same payload format as the bot's own invoices (bot/handlers/stars_billing.py)
            payload=json.dumps({"t": tier, "u": _user_id(request)}),
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=plan["stars"])],
        )
    except Exception:
        logger.exception(f"Could not create a Stars invoice link for tier {tier}")
        raise ApiError(502, "To'lov hisobini yaratib bo'lmadi. Birozdan so'ng qayta urinib ko'ring.", "INVOICE_FAILED")
    return json_response({"status": "ok", "tier": tier, "stars": plan["stars"], "invoice_link": link,
                          "message": f"{plan['stars']} Stars to'lov hisobi yaratildi"})


# ---------------------------------------------------------------------------------------------
# Store (supplier catalogue, paid with Telegram Stars)
# ---------------------------------------------------------------------------------------------

# supplier_service.create_checkout() failure code -> HTTP status
STORE_ERROR_STATUS = {
    "NOT_FOUND": 404,
    "OUT_OF_STOCK": 409,
    "SUPPLIER_INACTIVE": 503,
    "INVOICE_FAILED": 502,
}


async def _supplier_margin(supplier_id: int, cache: Optional[Dict[int, float]] = None) -> float:
    if cache is not None and supplier_id in cache:
        return cache[supplier_id]
    cfg = await db_manager.get_supplier_config(supplier_id)
    margin = cfg.margin_percent if cfg else DEFAULT_SUPPLIER_MARGIN
    if cache is not None:
        cache[supplier_id] = margin
    return margin


async def api_get_store_products(request: web.Request) -> web.Response:
    from services.supplier_service import supplier_service
    category = _text_param(request.query.get("category"), "category", 100).strip() or None
    only_available = request.query.get("only_available") == "true"
    products = await db_manager.get_store_products(category=category, only_available=only_available)
    margins: Dict[int, float] = {}
    data = []
    for p in products:
        margin = await _supplier_margin(p.supplier_id, margins)
        display_quantity = supplier_service.display_quantity(p)
        data.append({
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "type": p.type,
            # Per-unit price, display only (fractional for per-1000 services)
            "price_stars": supplier_service.unit_price_stars(p, margin),
            # Exactly what an order of `display_quantity` units is charged
            "display_quantity": display_quantity,
            "display_price_stars": supplier_service.quote_price_stars(p, display_quantity, margin),
            "min_quantity": p.min_quantity,
            "max_quantity": p.max_quantity,
            "is_available": p.is_available,
            "stock_status": p.stock_status,
            "description": p.description,
        })
    return json_response({"status": "ok", "products": data, "count": len(data)})


async def api_get_store_categories(request: web.Request) -> web.Response:
    products = await db_manager.get_store_products()
    return json_response({"status": "ok", "categories": sorted({p.category for p in products if p.category})})


async def api_store_quote(request: web.Request) -> web.Response:
    from services.supplier_service import supplier_service
    product_id = _int_param(request.query.get("product_id"), "product_id", 1, MAX_ROW_ID)
    quantity = _int_param(request.query.get("quantity"), "quantity", 1, MAX_STORE_QUANTITY)
    product = await db_manager.get_store_product(product_id)
    if not product:
        raise ApiError(404, "Mahsulot topilmadi", "NOT_FOUND")
    min_q, max_q = supplier_service.quantity_bounds(product)
    if not (min_q <= quantity <= max_q):
        raise ApiError(400, f"Miqdor {min_q} dan {max_q} gacha bo'lishi kerak", "INVALID_QUANTITY")
    price = supplier_service.quote_price_stars(product, quantity, await _supplier_margin(product.supplier_id))
    return json_response({"status": "ok", "product_id": product_id, "quantity": quantity, "price_stars": price,
                          "min_quantity": min_q, "max_quantity": max_q})


async def api_buy_store_product(request: web.Request) -> web.Response:
    from services.supplier_service import supplier_service
    body = await _read_json(request)
    product_id = _int_param(body.get("product_id"), "product_id", 1, MAX_ROW_ID)
    quantity = _int_param(body.get("quantity", 1), "quantity", 1, MAX_STORE_QUANTITY)
    target_link = _text_param(body.get("target_link"), "target_link", 255).strip()
    bot = _require_bot()
    result = await supplier_service.create_checkout(bot, _user_id(request), product_id, quantity, target_link)
    if not result.get("success"):
        code = result.get("code") or "ORDER_REJECTED"
        raise ApiError(STORE_ERROR_STATUS.get(code, 400), result.get("message", "Buyurtma yaratilmadi"), code)
    return json_response(result)


async def api_get_store_orders(request: web.Request) -> web.Response:
    orders = await db_manager.get_user_store_orders(_user_id(request))
    return json_response({"status": "ok", "orders": [
        {
            "id": o.id,
            "product_id": o.product_id,
            "product_name": o.product_name,
            "quantity": o.quantity,
            "price_stars": o.price_stars,
            "target_link": o.target_link,
            "status": o.status,
            "created_at": o.created_at,
        }
        for o in orders
    ]})


async def api_get_supplier_balance(request: web.Request) -> web.Response:
    from services.supplier_service import supplier_service
    await _require_admin(request)
    cfg = await db_manager.get_supplier_config()
    balance = await supplier_service.fetch_balance(cfg)
    return json_response({
        "status": "ok",
        "balance": balance,
        "currency": cfg.currency if cfg else "USD",
        "last_synced_at": cfg.last_synced_at if cfg else None,
        "margin_percent": cfg.margin_percent if cfg else DEFAULT_SUPPLIER_MARGIN,
    })


async def api_trigger_supplier_sync(request: web.Request) -> web.Response:
    from services.supplier_service import supplier_service
    await _require_admin(request)
    return json_response(await supplier_service.sync_catalog())


# ---------------------------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------------------------

async def api_not_found(request: web.Request) -> web.Response:
    raise ApiError(404, f"API endpoint '{request.path}' topilmadi", "NOT_FOUND")


def register_api_routes(app: web.Application):
    """Registers all Mini App REST API routes, the authentication middleware and the rate limiter."""
    app[RATE_LIMITER_KEY] = SlidingWindowRateLimiter()
    app.middlewares.append(api_middleware)
    r = app.router

    r.add_get("/api/me", api_me)
    r.add_get("/api/feed", api_feed)

    r.add_get("/api/pairs", api_get_pairs)
    r.add_post("/api/pairs", api_create_pair)
    r.add_get("/api/pairs/{id}", api_get_pair_detail)
    r.add_put("/api/pairs/{id}", api_update_pair)
    r.add_delete("/api/pairs/{id}", api_delete_pair)
    r.add_post("/api/pairs/{id}/toggle", api_toggle_pair)
    r.add_post("/api/pairs/{id}/test-post", api_test_post)
    r.add_post("/api/pairs/{id}/backfill", api_backfill)

    r.add_get("/api/story/settings", api_get_story_settings)
    r.add_post("/api/story/settings", api_save_story_settings)
    r.add_get("/api/story/queue", api_get_story_queue)

    r.add_get("/api/audio-tracks", api_get_audio_tracks)
    r.add_get("/api/audio-tracks/{filename}", api_serve_audio_file)

    r.add_get("/api/system", api_system_status)
    r.add_post("/api/billing/checkout", api_billing_checkout)

    r.add_get("/api/store/products", api_get_store_products)
    r.add_get("/api/store/categories", api_get_store_categories)
    r.add_get("/api/store/quote", api_store_quote)
    r.add_post("/api/store/buy", api_buy_store_product)
    r.add_get("/api/store/orders", api_get_store_orders)
    r.add_get("/api/store/balance", api_get_supplier_balance)
    r.add_post("/api/store/sync", api_trigger_supplier_sync)

    r.add_route("*", "/api/{tail:.*}", api_not_found)
