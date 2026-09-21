import os
import re
import time
import logging
import asyncio
from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta
import aiosqlite

from database.db_manager import db_manager

logger = logging.getLogger(__name__)


class MarketAnalyticsService:
    """
    Executive AI Market Analytics & Daily Evening Briefing Engine.
    Aggregates cloned listings, calculates average prices per district,
    identifies top luxury deals, and generates an executive text digest
    along with a neural voiceover podcast briefing.
    """

    async def get_daily_market_stats(self, days: int = 1) -> Dict[str, Any]:
        """Queries database for real estate metrics over the specified days"""
        async with db_manager.get_connection() as db:
            db.row_factory = aiosqlite.Row

            cur = await db.execute("""
                SELECT
                    COUNT(*) as total_count,
                    COALESCE(AVG(CASE WHEN price > 0 THEN price END), 0) as avg_price,
                    COALESCE(MIN(CASE WHEN price > 0 THEN price END), 0) as min_price,
                    COALESCE(MAX(CASE WHEN price > 0 THEN price END), 0) as max_price,
                    COUNT(CASE WHEN status = 'sold' THEN 1 END) as sold_count
                FROM cloned_messages
                WHERE cloned_at >= datetime('now', ?)
            """, (f"-{days} days",))
            row = await cur.fetchone()

            # District distribution
            dist_cur = await db.execute("""
                SELECT last_caption
                FROM cloned_messages
                WHERE cloned_at >= datetime('now', ?) AND last_caption IS NOT NULL
            """, (f"-{days} days",))
            dist_rows = await dist_cur.fetchall()

            districts = [
                "Chilonzor", "Yunusobod", "Mirzo Ulug'bek", "Yakkasaroy", "Mirobod", "Sergeli",
                "Shayxontohur", "Olmazor", "Uchtepa", "Yashnobod", "Bektemir", "Yangi Hayot"
            ]
            dist_counts: Dict[str, int] = {}
            for r in dist_rows:
                cap = r["last_caption"] or ""
                for d in districts:
                    if re.search(rf"\b{re.escape(d)}\b", cap, re.IGNORECASE):
                        dist_counts[d] = dist_counts.get(d, 0) + 1

            top_district = max(dist_counts.items(), key=lambda x: x[1])[0] if dist_counts else "Toshkent shahri"

            return {
                "total_listings": row["total_count"] if row else 0,
                "avg_price": round(float(row["avg_price"] if row else 0), 1),
                "min_price": round(float(row["min_price"] if row else 0), 1),
                "max_price": round(float(row["max_price"] if row else 0), 1),
                "sold_count": row["sold_count"] if row else 0,
                "top_district": top_district,
                "dist_counts": dist_counts
            }

    def format_digest_text(self, stats: Dict[str, Any]) -> str:
        """Formats statistics into an executive Telegram briefing message"""
        total = stats.get("total_listings", 0)
        avg_p = stats.get("avg_price", 0)
        max_p = stats.get("max_price", 0)
        min_p = stats.get("min_price", 0)
        sold = stats.get("sold_count", 0)
        top_dist = stats.get("top_district", "Toshkent")

        # Tashkent local timezone (UTC+5)
        tashkent_tz = timezone(timedelta(hours=5))
        today_str = datetime.now(tashkent_tz).strftime("%d-%m-%Y")

        text = f"""
📊 <b>KUNLIK KO'CHMAS MULK BOZORI TAHLILI ({today_str}):</b>
───────────────────────────
├ 🏢 <b>Jami e'lonlar:</b> <b>{total} ta</b>
├ 💰 <b>O'rtacha narx:</b> <b>${avg_p:g}</b>
├ 💎 <b>Eng qimmat taklif:</b> <b>${max_p:g}</b>
├ 🏷 <b>Eng arzon variant:</b> <b>${min_p:g}</b>
├ 📍 <b>Eng faol tuman:</b> <b>{top_dist}</b>
└ 🔴 <b>Sotilgan / Yopilgan:</b> <b>{sold} ta</b>
───────────────────────────
💡 <i>Bozor tendensiyasi: {top_dist} tumanida talab va takliflar eng yuqori dinamikani ko'rsatmoqda.</i>
"""
        return text.strip()

    def generate_briefing_voice_script(self, stats: Dict[str, Any]) -> str:
        """Creates spoken Uzbek text for Edge-TTS audio podcast briefing"""
        total = stats.get("total_listings", 0)
        avg_p = int(stats.get("avg_price", 0))
        top_dist = stats.get("top_district", "Toshkent")

        if total == 0:
            return "Assalomu alaykum. Bugungi kunda bazada yangi e'lonlar qayd etilmadi. Ertaga yangi tahlillarni taqdim etamiz."

        if avg_p >= 1000:
            if avg_p % 1000 == 0:
                avg_phrase = f"{avg_p // 1000} ming dollar"
            elif avg_p % 100 == 0:
                avg_phrase = f"{avg_p / 1000:g} ming dollar"
            else:
                avg_phrase = f"{avg_p // 1000} ming {avg_p % 1000} dollar"
        else:
            avg_phrase = f"{avg_p} dollar"
        return (
            f"Assalomu alaykum! Bugungi ko'chmas mulk dayjesti. "
            f"Bugun jami {total} ta yangi variant qayd etildi. "
            f"Bozordagi o'rtacha narx {avg_phrase}ni tashkil qildi. "
            f"Eng ko'p takliflar {top_dist} tumanida kuzatildi. "
            f"To'liq hisobot kanalimizda mavjud."
        )

    async def generate_audio_podcast(self, script_text: str, output_path: Optional[str] = None) -> Optional[str]:
        """Synthesizes briefing audio file via edge_tts"""
        try:
            from services.tts_narrator_service import tts_narrator_service
            temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_media")
            os.makedirs(temp_dir, exist_ok=True)
            if not output_path:
                output_path = os.path.join(temp_dir, f"evening_briefing_{int(time.time())}.mp3")
            return await tts_narrator_service.generate_voiceover_mp3(script_text, output_path=output_path)
        except Exception as e:
            logger.error(f"Failed to generate audio podcast: {e}")
            return None

    async def send_daily_briefing(self, bot, chat_id: int):
        """Generates and sends both text digest and audio podcast to chat"""
        stats = await self.get_daily_market_stats(days=1)
        text = self.format_digest_text(stats)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

        script = self.generate_briefing_voice_script(stats)
        audio_path = await self.generate_audio_podcast(script)
        if audio_path and os.path.exists(audio_path):
            try:
                from aiogram.types import FSInputFile
                voice_input = FSInputFile(audio_path, filename="kunlik_briefing.mp3")
                await bot.send_voice(
                    chat_id=chat_id,
                    voice=voice_input,
                    caption="🎙 <b>Executive AI Oqshomgi Audio Podkasti</b>",
                    parse_mode="HTML"
                )
            except Exception as se:
                logger.error(f"Error sending audio podcast: {se}")
            finally:
                try:
                    os.remove(audio_path)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)


market_analytics_service = MarketAnalyticsService()
