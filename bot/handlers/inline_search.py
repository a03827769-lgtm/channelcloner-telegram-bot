import re
import html
import time
import logging
from collections import deque
from typing import Any, Deque, Dict, List, Optional
from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from database.db_manager import db_manager
from services.custom_emojis import ID_CHANNEL

logger = logging.getLogger(__name__)
router = Router(name="inline_search_router")

# Shorter queries are not searched: every keystroke would otherwise scan the caption archive
INLINE_MIN_QUERY_LENGTH = 3
# Per-user sliding window for inline queries (clients send one query per keystroke)
INLINE_RATE_WINDOW_SECONDS = 10.0
INLINE_RATE_MAX_QUERIES = 12
INLINE_RESULTS_LIMIT = 15

_recent_queries: Dict[int, Deque[float]] = {}


_PHONE_RE = re.compile(r'(?:\+?998|\b\d{9}\b|\b\d{2}[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}\b)')
_PRICE_RE = re.compile(r'(?<!\+)(?<!\d)(\d{3,7})\s*(?:\$|usd|dollar|y\.e|k\b)?(?!\d)', re.IGNORECASE)
_PUBLIC_USERNAME_RE = re.compile(r'@?([A-Za-z][A-Za-z0-9_]{3,31})')


def listing_post_url(row: Dict[str, Any]) -> Optional[str]:
    """t.me link of a cloned post: by the public @username of the destination, else by its numeric
    chat id (t.me/c/..., opens for channel members). Invite links cannot address a post: no link then."""
    target_msg_id = row.get("target_msg_id")
    if not target_msg_id:
        return None
    for ref in (row.get("target_channel"), row.get("pair_target_channel")):
        match = _PUBLIC_USERNAME_RE.fullmatch(str(ref or "").strip())
        if match:
            return f"https://t.me/{match.group(1)}/{target_msg_id}"
    raw_id = db_manager.normalize_peer_id(row.get("pair_target_id"))
    if raw_id is None:
        for ref in (row.get("target_channel"), row.get("pair_target_channel")):
            text = str(ref or "").strip()
            if text.lstrip("-").isdigit():
                raw_id = db_manager.normalize_peer_id(text)
                break
    return f"https://t.me/c/{raw_id}/{target_msg_id}" if raw_id else None


def _allow_inline_query(user_id: int) -> bool:
    now = time.monotonic()
    if len(_recent_queries) > 10000:
        cutoff = now - INLINE_RATE_WINDOW_SECONDS
        for uid in [uid for uid, stamps in _recent_queries.items() if not stamps or stamps[-1] < cutoff]:
            _recent_queries.pop(uid, None)
    stamps = _recent_queries.setdefault(user_id, deque())
    while stamps and now - stamps[0] > INLINE_RATE_WINDOW_SECONDS:
        stamps.popleft()
    if len(stamps) >= INLINE_RATE_MAX_QUERIES:
        return False
    stamps.append(now)
    return True


def _help_article() -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id="search_help",
        title="🔍 Kamida 3 ta belgi kiriting",
        description="Tuman, narx yoki xona soni bo'yicha qidiring (masalan: 'Chilonzor', '50000', '3 xona')",
        input_message_content=InputTextMessageContent(
            message_text="ℹ️ <i>Qidiruv uchun kamida 3 ta belgi kiriting.</i>",
            parse_mode="HTML"
        )
    )


async def _bot_username(inline_query: InlineQuery) -> str:
    try:
        me = await inline_query.bot.me()  # cached by aiogram after the first call
        if me and me.username:
            return me.username
    except Exception:
        logger.debug("Ignored exception", exc_info=True)
    return "KlonerBot"


@router.inline_query()
async def inline_real_estate_search(inline_query: InlineQuery):
    """
    Telegram Inline Mode real estate property search.
    Enables users to type '@bot <query>' in any chat to find and share listings from THEIR OWN channel
    pairs (the archive of other customers is never searched).
    Supports filtering by:
    - District / Location (e.g. '@bot Chilonzor', '@bot Yunusobod')
    - Price (e.g. '@bot 50000', '@bot 700')
    - Rooms / Type (e.g. '@bot 3 xona', '@bot arenda', '@bot sotiladi')
    """
    raw_query = (inline_query.query or "").strip()
    user_id = getattr(inline_query.from_user, "id", None)
    if not isinstance(user_id, int):
        return

    # Inline mode is part of the bot: private mode applies to it like to every other entry point
    try:
        allowed = await db_manager.can_user_access_bot(user_id)
    except Exception:
        logger.debug("Access check failed for inline query", exc_info=True)
        allowed = False
    if not allowed:
        await inline_query.answer(results=[], cache_time=60, is_personal=True)
        return

    if len(raw_query) < INLINE_MIN_QUERY_LENGTH:
        await inline_query.answer(results=[_help_article()], cache_time=300, is_personal=True)
        return

    if not _allow_inline_query(user_id):
        logger.debug(f"Inline query from {user_id} throttled")
        return

    results: List[InlineQueryResultArticle] = []
    bot_username = await _bot_username(inline_query)

    # A phone number is not a property price
    is_phone = bool(_PHONE_RE.search(raw_query))
    price_match = None if is_phone else _PRICE_RE.search(raw_query)
    text_filter = raw_query
    price_range = None
    if price_match:
        target_price = float(price_match.group(1))
        # Listings within +-30% of the requested price
        price_range = (target_price * 0.7, target_price * 1.3)
        text_filter = raw_query[:price_match.start()] + " " + raw_query[price_match.end():]
    text_filter = re.sub(r'\s+', ' ', text_filter).strip()

    try:
        rows = await db_manager.search_user_listings(
            user_id, text=text_filter, price_range=price_range, limit=INLINE_RESULTS_LIMIT
        )
    except Exception as e:
        logger.error(f"Error executing inline search query for user {user_id}: {e}", exc_info=True)
        rows = []

    for r in rows:
        caption = r["last_caption"] or "Ko'chmas mulk e'loni"
        price_val = r["price"]
        status = r["status"] or "active"

        # Clean caption snippet for description
        clean_desc = html.unescape(re.sub(r'<[^>]+>', ' ', caption))
        clean_desc = re.sub(r'\s+', ' ', clean_desc).strip()
        if len(clean_desc) > 90:
            clean_desc = clean_desc[:87] + "..."

        price_tag = f"${price_val:g}" if price_val and price_val > 0 else "Kelishilgan"
        status_icon = "🔴 [SOTILGAN]" if status == "sold" else "🟢 [FAOL]"
        title = f"{status_icon} {price_tag} - {clean_desc[:40]}"

        reply_markup = None
        post_url = listing_post_url(r)
        if post_url:
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="Kanalda ko'rish",
                    url=post_url,
                    style="primary",
                    icon_custom_emoji_id=ID_CHANNEL
                )
            ]])

        message_text = f"""
🏢 <b>KO'CHMAS MULK VARIANTI:</b>
───────────────────────────
├ 💰 <b>Narxi:</b> <b>{html.escape(price_tag)}</b>
├ 📊 <b>Holat:</b> {html.escape(str(status).upper())}
└ 📍 <b>Tavsif:</b>
{html.escape(clean_desc[:400])}
───────────────────────────
<i>Topildi: @{html.escape(bot_username)} qidiruvi orqali</i>
"""

        results.append(
            InlineQueryResultArticle(
                id=f"listing_{r['id']}",
                title=title,
                description=clean_desc,
                input_message_content=InputTextMessageContent(
                    message_text=message_text,
                    parse_mode="HTML"
                ),
                reply_markup=reply_markup
            )
        )

    if not results:
        # Provide friendly helper article if no results found
        results.append(
            InlineQueryResultArticle(
                id="no_results",
                title="🔍 Natija topilmadi",
                description="Boshqa tuman yoki narx kiritib ko'ring (masalan: 'Chilonzor', '50000')",
                input_message_content=InputTextMessageContent(
                    message_text="ℹ️ <i>Qidiruv bo'yicha mos e'lonlar topilmadi.</i>",
                    parse_mode="HTML"
                )
            )
        )

    await inline_query.answer(
        results=results,
        cache_time=10,
        is_personal=True
    )
