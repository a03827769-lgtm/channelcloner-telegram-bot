import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from database.db_manager import db_manager
from services.listing_analyzer import format_price_usd, normalize_listing_text

logger = logging.getLogger(__name__)

TASHKENT_TZ = timezone(timedelta(hours=5))

# Tashkent districts counted in the digest (matched on the normalized caption, apostrophe variants unified)
TASHKENT_DISTRICTS = (
    "Chilonzor", "Yunusobod", "Mirzo Ulug'bek", "Yakkasaroy", "Mirobod", "Sergeli",
    "Shayxontohur", "Olmazor", "Uchtepa", "Yashnobod", "Bektemir", "Yangi Hayot",
)
_DISTRICT_PATTERNS = [(d, re.compile(rf"\b{re.escape(d)}\b", re.IGNORECASE)) for d in TASHKENT_DISTRICTS]


class MarketAnalyticsService:
    """
    Daily real estate digest of ONE customer: counts and prices of the listings cloned by that customer's own
    channel pairs (other customers' data is never included) and the most active Tashkent district.
    """

    async def get_daily_market_stats(self, user_id: int, days: int = 1) -> Dict[str, Any]:
        """Listing metrics of the user's pairs over the last `days` days."""
        window = f"-{max(1, int(days))} days"
        async with db_manager.get_connection() as db:
            cur = await db.execute("""
                SELECT
                    COUNT(*) AS total_count,
                    COALESCE(AVG(CASE WHEN cm.price > 0 THEN cm.price END), 0) AS avg_price,
                    COALESCE(MIN(CASE WHEN cm.price > 0 THEN cm.price END), 0) AS min_price,
                    COALESCE(MAX(CASE WHEN cm.price > 0 THEN cm.price END), 0) AS max_price,
                    COUNT(CASE WHEN cm.status = 'sold' THEN 1 END) AS sold_count
                FROM cloned_messages cm
                JOIN channel_pairs cp ON cp.id = cm.pair_id
                WHERE cp.user_id = ? AND cm.cloned_at >= datetime('now', ?)
            """, (user_id, window))
            row = await cur.fetchone()

            dist_cur = await db.execute("""
                SELECT cm.last_caption
                FROM cloned_messages cm
                JOIN channel_pairs cp ON cp.id = cm.pair_id
                WHERE cp.user_id = ? AND cm.cloned_at >= datetime('now', ?) AND cm.last_caption IS NOT NULL
            """, (user_id, window))
            captions: List[str] = [r["last_caption"] or "" for r in await dist_cur.fetchall()]

        dist_counts: Dict[str, int] = {}
        for caption in captions:
            text = normalize_listing_text(re.sub(r"<[^>]+>", " ", caption))
            for district, pattern in _DISTRICT_PATTERNS:
                if pattern.search(text):
                    dist_counts[district] = dist_counts.get(district, 0) + 1

        top_district = max(dist_counts.items(), key=lambda x: x[1])[0] if dist_counts else None
        return {
            "total_listings": row["total_count"] if row else 0,
            "avg_price": round(float(row["avg_price"] if row else 0), 1),
            "min_price": round(float(row["min_price"] if row else 0), 1),
            "max_price": round(float(row["max_price"] if row else 0), 1),
            "sold_count": row["sold_count"] if row else 0,
            "top_district": top_district,
            "dist_counts": dist_counts,
        }

    def format_digest_text(self, stats: Dict[str, Any], now: Optional[datetime] = None) -> str:
        """Telegram (HTML) digest of the statistics; the date is the Tashkent date."""
        total = stats.get("total_listings", 0)
        today_str = (now or datetime.now(TASHKENT_TZ)).astimezone(TASHKENT_TZ).strftime("%d.%m.%Y")
        if not total:
            return (f"📊 <b>KUNLIK KO'CHMAS MULK BOZORI TAHLILI ({today_str}):</b>\n\n"
                    f"<i>Oxirgi 24 soatda kanallaringizga yangi e'lon ko'chirilmadi.</i>")

        top_dist = stats.get("top_district")
        trend = (f"💡 <i>Eng ko'p e'lonlar {top_dist} tumaniga tegishli.</i>" if top_dist
                 else "💡 <i>E'lonlarda tuman nomi aniqlanmadi.</i>")
        text = f"""
📊 <b>KUNLIK KO'CHMAS MULK BOZORI TAHLILI ({today_str}):</b>
───────────────────────────
├ 🏢 <b>Jami e'lonlar:</b> <b>{total} ta</b>
├ 💰 <b>O'rtacha narx:</b> <b>{format_price_usd(stats.get("avg_price"), "—")}</b>
├ 💎 <b>Eng qimmat taklif:</b> <b>{format_price_usd(stats.get("max_price"), "—")}</b>
├ 🏷 <b>Eng arzon variant:</b> <b>{format_price_usd(stats.get("min_price"), "—")}</b>
├ 📍 <b>Eng faol tuman:</b> <b>{top_dist or "Aniqlanmadi"}</b>
└ 🔴 <b>Sotilgan / Yopilgan:</b> <b>{stats.get("sold_count", 0)} ta</b>
───────────────────────────
{trend}
"""
        return text.strip()

    async def send_daily_briefing(self, bot, chat_id: int, user_id: Optional[int] = None) -> None:
        """Sends the digest of `user_id` (default: the chat's user) to the chat."""
        stats = await self.get_daily_market_stats(user_id if user_id is not None else chat_id, days=1)
        await bot.send_message(chat_id=chat_id, text=self.format_digest_text(stats), parse_mode="HTML")


market_analytics_service = MarketAnalyticsService()
