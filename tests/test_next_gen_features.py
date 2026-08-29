import os
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from database.db_manager import DatabaseManager
from database.models import ChannelPair
from services.ai_paraphraser import ai_paraphraser
from services.dynamic_affiliate_engine import dynamic_affiliate_engine
from services.drip_feed_queue import drip_feed_service
from services.video_watermark_service import video_watermark_service
from services.disaster_recovery import disaster_recovery_service

class TestNextGenFeatures(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.test_db_path = "test_next_gen.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()
        await self.db.get_or_create_user(12345, "Test User", "testuser", is_admin=True)

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    async def test_ai_paraphraser_formal(self):
        raw = "Salom hammaga! Bugun yangi qonun qabul qilindi. Tafsilotlar tez orada."
        res = ai_paraphraser.paraphrase(raw, mode="formal")
        self.assertIn("Rasmiy Axborot", res)
        self.assertIn("yangi qonun", res.lower())

    async def test_ai_paraphraser_hype(self):
        raw = "Yangi iPhone 17 narxi e'lon qilindi."
        res = ai_paraphraser.paraphrase(raw, mode="hype")
        self.assertTrue(any(k in res for k in ["SHOSHILINCH", "DIQQAT", "EKSKLYUZIV"]))
        self.assertIn("Batafsil ma'lumot", res)

    async def test_ai_paraphraser_short_tldr(self):
        raw = "Birinchi yangilik\nIkkinchi xabar\nUchinchi xulosa\nTo'rtinchi fikr"
        res = ai_paraphraser.paraphrase(raw, mode="short")
        self.assertIn("Eng Asosiy Ma'lumotlar", res)
        self.assertIn("├", res)

    async def test_ai_paraphraser_preserves_html(self):
        raw = '<b>Muhim xabar:</b> <a href="https://t.me/test">Kanalimiz</a> va <tg-emoji emoji-id="5364125616801073577">✈️</tg-emoji>'
        res = ai_paraphraser.paraphrase(raw, mode="formal")
        self.assertIn('<b>', res)
        self.assertIn('<a href="https://t.me/test">', res)
        self.assertIn('<tg-emoji emoji-id="5364125616801073577">✈️</tg-emoji>', res)

    async def test_dynamic_affiliate_engine(self):
        text = "Yangi krossovka https://uzum.uz/product/123 va https://aliexpress.com/item/456 chegirmada!"
        rules = "uzum.uz=myref123, aliexpress.com=aliref456"
        mod_text, cta_buttons = dynamic_affiliate_engine.extract_and_convert_links(text, rules)

        self.assertEqual(len(cta_buttons), 2)
        self.assertIn("Uzum", cta_buttons[0][0])
        self.assertIn("ref=myref123", cta_buttons[0][1])
        self.assertIn("AliExpress", cta_buttons[1][0])
        self.assertIn("ref=aliref456", cta_buttons[1][1])

        kb = dynamic_affiliate_engine.build_cta_keyboard(cta_buttons)
        self.assertIsNotNone(kb)
        self.assertEqual(len(kb.inline_keyboard), 2)

    async def test_video_watermark_coordinates(self):
        x, y = video_watermark_service._get_drawtext_coordinates("bottom_right")
        self.assertEqual(x, "w-tw-30")
        self.assertEqual(y, "h-th-30")

        x_c, y_c = video_watermark_service._get_drawtext_coordinates("center")
        self.assertEqual(x_c, "(w-tw)/2")
        self.assertEqual(y_c, "(h-th)/2")

    async def test_drip_feed_queue_and_db(self):
        pair_id = await self.db.add_channel_pair(
            user_id=12345,
            source_channel="@src_test",
            source_title="Source",
            target_channel="@dst_test",
            target_title="Target"
        )
        pair = await self.db.get_pair_by_id(pair_id)

        # Update drip settings
        await self.db.update_drip_settings(pair_id, delay_minutes=15, night_mode="buffer")
        pair = await self.db.get_pair_by_id(pair_id)
        self.assertEqual(pair.drip_delay_minutes, 15)
        self.assertEqual(pair.night_mode, "buffer")

        # Test queue insertion
        item_id = await self.db.add_drip_queue_item(pair_id, '{"text": "salom"}', "2020-01-01 00:00:00")
        due = await self.db.get_due_drip_items()
        self.assertGreaterEqual(len(due), 1)
        self.assertEqual(due[0]["id"], item_id)

        await self.db.mark_drip_item_done(item_id, "sent")
        due_after = await self.db.get_due_drip_items()
        self.assertEqual(len(due_after), 0)

    async def test_disaster_recovery_archive_and_count(self):
        pair_id = await self.db.add_channel_pair(
            user_id=12345,
            source_channel="@src_test",
            source_title="Source",
            target_channel="@dst_test",
            target_title="Target"
        )

        await self.db.save_channel_backup(
            pair_id=pair_id,
            source_id=1001,
            message_id=555,
            text="Test post text",
            media_type="photo",
            media_file_id="AgACAgIAAxkBAAI..."
        )

        cnt = await self.db.get_channel_backup_count(pair_id)
        self.assertEqual(cnt, 1)

        backups = await self.db.get_channel_backups(pair_id)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["text"], "Test post text")

        # Test mock restore
        mock_bot = AsyncMock()
        res = await disaster_recovery_service.restore_channel(mock_bot, pair_id, "@restored_chan", db=self.db)
        self.assertEqual(res["total_archived"], 1)
        self.assertEqual(res["restored"], 1)
        mock_bot.send_photo.assert_called_once()

if __name__ == "__main__":
    unittest.main()
