import re
import html
import logging
from typing import List
from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from database.db_manager import db_manager
import aiosqlite
from services.custom_emojis import ID_CHANNEL

logger = logging.getLogger(__name__)
router = Router(name="inline_search_router")


@router.inline_query()
async def inline_real_estate_search(inline_query: InlineQuery):
    """
    Telegram Inline Mode real estate property search.
    Enables users to type '@bot <query>' in any chat to find and share active listings.
    Supports filtering by:
    - District / Location (e.g. '@bot Chilonzor', '@bot Yunusobod')
    - Price (e.g. '@bot 50000', '@bot 700')
    - Rooms / Type (e.g. '@bot 3 xona', '@bot arenda', '@bot sotiladi')
    """
    raw_query = (inline_query.query or "").strip()
    results: List[InlineQueryResultArticle] = []

    bot_username = "KlonerBot"
    try:
        bot_user = await inline_query.bot.get_me()
        if bot_user and bot_user.username:
            bot_username = bot_user.username
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    try:
        async with db_manager.get_connection() as db:
            db.row_factory = aiosqlite.Row

            sql = """
                SELECT id, source_channel, target_channel, target_msg_id, status, price, last_caption, cloned_at
                FROM cloned_messages
                WHERE last_caption IS NOT NULL AND TRIM(last_caption) != ''
            """
            params = []

            # Distinguish phone numbers from property prices
            is_phone = bool(re.search(r'(?:\+?998|\b\d{9}\b|\b\d{2}[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}\b)', raw_query))
            price_match = None if is_phone else re.search(r'(?<!\+)(?<!\d)(\d{3,7})\s*(?:\$|usd|dollar|y\.e|k\b)?(?!\d)', raw_query, re.IGNORECASE)
            text_filter = raw_query.strip()

            if price_match:
                target_p = float(price_match.group(1))
                text_filter = raw_query[:price_match.start()] + " " + raw_query[price_match.end():]
                text_filter = re.sub(r'\s+', ' ', text_filter).strip()
                # Search within ±30% range or when price matches
                sql += " AND price > 0 AND price BETWEEN ? AND ?"
                params.extend([target_p * 0.7, target_p * 1.3])

            if text_filter:
                escaped_filter = text_filter.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                sql += " AND last_caption LIKE ? ESCAPE '\\'"
                params.append(f"%{escaped_filter}%")

            sql += " ORDER BY id DESC LIMIT 15"

            cursor = await db.execute(sql, tuple(params))
            rows = await cursor.fetchall()

            for r in rows:
                item_id = str(r["id"])
                caption = r["last_caption"] or "Ko'chmas mulk e'loni"
                price_val = r["price"]
                status = r["status"] or "active"
                target_ch = r["target_channel"] or ""
                target_msg_id = r["target_msg_id"]

                # Clean caption snippet for description
                clean_desc = re.sub(r'<[^>]+>', ' ', caption)
                clean_desc = re.sub(r'\s+', ' ', clean_desc).strip()
                if len(clean_desc) > 90:
                    clean_desc = clean_desc[:87] + "..."

                price_tag = f"${price_val:g}" if price_val and price_val > 0 else "Kelishilgan"
                status_icon = "🔴 [SOTILGAN]" if status == "sold" else "🟢 [FAOL]"
                title = f"{status_icon} {price_tag} - {clean_desc[:40]}"

                # Channel link button
                reply_markup = None
                if target_ch and target_msg_id:
                    clean_ch = target_ch.lstrip("@").strip()
                    if clean_ch.startswith("-100"):
                        post_url = f"https://t.me/c/{clean_ch[4:]}/{target_msg_id}"
                    elif clean_ch.startswith("-") or clean_ch.isdigit():
                        post_url = f"https://t.me/c/{clean_ch.lstrip('-')}/{target_msg_id}"
                    else:
                        post_url = f"https://t.me/{clean_ch}/{target_msg_id}"
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
├ 💰 <b>Narxi:</b> <b>{price_tag}</b>
├ 📊 <b>Holat:</b> {status.upper()}
└ 📍 <b>Tavsif:</b>
{html.escape(clean_desc[:400])}
───────────────────────────
<i>Topildi: @{bot_username} qidiruvi orqali</i>
"""

                results.append(
                    InlineQueryResultArticle(
                        id=f"listing_{item_id}",
                        title=title,
                        description=clean_desc,
                        input_message_content=InputTextMessageContent(
                            message_text=message_text,
                            parse_mode="HTML"
                        ),
                        reply_markup=reply_markup
                    )
                )

    except Exception as e:
        logger.error(f"Error executing inline search query '{raw_query}': {e}", exc_info=True)

    if not results:
        # Provide friendly helper article if no results found
        results.append(
            InlineQueryResultArticle(
                id="no_results",
                title="🔍 Natija topilmadi",
                description="Boshqa tuman yoki narx kiritib ko'ring (masalan: '@bot Chilonzor', '@bot 50000')",
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
