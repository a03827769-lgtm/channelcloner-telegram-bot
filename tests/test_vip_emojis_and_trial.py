import unittest
import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch
from config.settings import settings
from database.db_manager import DatabaseManager
from database.models import ChannelPair
from services.emoji_converter import emoji_converter
from services.cloner_engine import ClonerEngine

class TestVipEmojisAndTrial(unittest.TestCase):
    def setUp(self):
        # Tests below assign settings.ADMIN_IDS_RAW; the patcher restores the original value afterwards
        self._admin_ids_patch = patch.object(settings, "ADMIN_IDS_RAW", settings.ADMIN_IDS_RAW)
        self._admin_ids_patch.start()
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self._tmp_dir.name, f"test_vip_trial_{uuid.uuid4().hex[:8]}.db")
        self.db = DatabaseManager(self.db_path)
        asyncio.run(self.db.init_db())

    def tearDown(self):
        from tests.test_utils import safe_cleanup_db
        asyncio.run(safe_cleanup_db(self.db_path, self.db))
        self._tmp_dir.cleanup()
        self._admin_ids_patch.stop()

    def test_emoji_converter_preserves_html(self):
        raw_text = "Salom! ✅ To'lov o'tdi. 🚀 <a href=\"https://t.me\">Kanal 🔗</a> va <code>⚠️ Code</code>."
        converted = emoji_converter.convert_to_premium_emojis(raw_text)

        self.assertIn('<tg-emoji emoji-id="5456432998092133477">✅</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5372917041193828849">🚀</tg-emoji>', converted)
        self.assertIn('<a href="https://t.me">', converted)
        self.assertIn('<code>⚠️ Code</code>', converted)

    def test_14_day_trial_lifecycle(self):
        async def run():
            user_id = 777222
            await self.db.get_or_create_user(user_id, "Trial User")

            # 1. New user has active 14-day trial
            sub = await self.db.get_user_subscription(user_id)
            self.assertEqual(sub.tier, "free")
            self.assertTrue(sub.is_trial_active)
            self.assertTrue(sub.is_active)
            self.assertIsNotNone(sub.trial_expires_at)

            # 2. Simulate trial expiration (15 days in past)
            past_date = (datetime.utcnow() - timedelta(days=1)).isoformat()
            async with self.db.get_connection() as db:
                await db.execute("UPDATE subscriptions SET trial_expires_at = ? WHERE user_id = ?", (past_date, user_id))
                await db.commit()

            # Clear cache & reload
            from services.cache_manager import cache_manager
            await cache_manager.sub_cache.delete(f"sub_{user_id}")

            expired_sub = await self.db.get_user_subscription(user_id)
            self.assertFalse(expired_sub.is_trial_active)
            self.assertFalse(expired_sub.is_active)
            self.assertEqual(expired_sub.max_channels, 0)

            # Check adding channel is blocked
            can_add, max_allowed, _ = await self.db.can_user_add_channel(user_id)
            self.assertFalse(can_add)
            self.assertEqual(max_allowed, 0)

            # 3. Check notification query
            expired_users = await self.db.get_expired_trial_users_to_notify()
            self.assertEqual(len(expired_users), 1)
            self.assertEqual(expired_users[0][0], user_id)

            # 4. Mark trial notified
            await self.db.mark_trial_notified(user_id)
            remaining_to_notify = await self.db.get_expired_trial_users_to_notify()
            self.assertEqual(len(remaining_to_notify), 0)

            # 5. Activate Pro 100 Stars restores full active status
            pro_sub = await self.db.activate_subscription(user_id, tier="pro", stars=100, charge_id="pro_chg", days=30)
            self.assertTrue(pro_sub.is_active)
            self.assertEqual(pro_sub.tier, "pro")
            self.assertEqual(pro_sub.max_channels, 5)

        asyncio.run(run())

    def test_vip_emoji_toggle(self):
        async def run():
            user_id = 888333
            await self.db.get_or_create_user(user_id, "VIP User")
            pair_id = await self.db.add_channel_pair(user_id, "@src_ch", "Source", "@tgt_ch", "Target")

            # Toggle emoji on
            status = await self.db.toggle_premium_emojis(pair_id)
            self.assertTrue(status)

            pair = await self.db.get_pair_by_id(pair_id)
            self.assertTrue(pair.auto_premium_emojis)

            # Toggle emoji off
            status_off = await self.db.toggle_premium_emojis(pair_id)
            self.assertFalse(status_off)

            pair = await self.db.get_pair_by_id(pair_id)
            self.assertFalse(pair.auto_premium_emojis)

        asyncio.run(run())

    def test_html_aware_link_cleaner(self):
        from services.text_processor import TextProcessor
        raw_html = '<a href="https://t.me/old_channel">Kanalimizga kiring</a> va @old_channel https://t.me/joinchat/Abc12345'
        cleaned = TextProcessor.clean_links_and_usernames(raw_html)
        self.assertNotIn('https://t.me', cleaned)
        self.assertNotIn('@old_channel', cleaned)
        self.assertNotIn('<a href="">', cleaned)
        self.assertIn('Kanalimizga kiring', cleaned)

    def test_media_group_buffer_isolation(self):
        async def run():
            from services.media_handler import MediaGroupBuffer
            from unittest.mock import MagicMock

            buffer = MediaGroupBuffer(debounce_delay=0.1)
            msg1 = MagicMock()
            msg1.id = 101
            msg2 = MagicMock()
            msg2.id = 102

            results = {}

            async def callback1(key, msgs):
                results["cb1"] = [m.id for m in msgs]

            async def callback2(key, msgs):
                results["cb2"] = [m.id for m in msgs]

            # Same grouped_id (999), but different pair_ids (pair 1 vs pair 2)
            buffer.add_message((1, 999), msg1, callback1)
            buffer.add_message((1, 999), msg2, callback1)
            buffer.add_message((2, 999), msg1, callback2)
            buffer.add_message((2, 999), msg2, callback2)

            await asyncio.sleep(0.3)

            self.assertEqual(results.get("cb1"), [101, 102])
            self.assertEqual(results.get("cb2"), [101, 102])

        asyncio.run(run())

    def test_emoji_converter_all_categories_and_edge_cases(self):
        # 1. Flags
        text_flags = "Davlatlar: 🇺🇿 O'zbekiston, 🇷🇺 Rossiya, 🇹🇷 Turkiya, 🇩🇪 Germaniya, 🇺🇸 AQSH."
        conv_flags = emoji_converter.convert_to_premium_emojis(text_flags)
        self.assertIn('<tg-emoji emoji-id="5445378486711623503">🇺🇿</tg-emoji>', conv_flags)
        self.assertIn('<tg-emoji emoji-id="5398017006165305287">🇷🇺</tg-emoji>', conv_flags)
        self.assertIn('<tg-emoji emoji-id="5474150171979822886">🇹🇷</tg-emoji>', conv_flags)

        # 2. 3D Numbers (with and without variation selector)
        text_nums = "Qadamlar: 1️⃣ Ro'yxat, 2️⃣ To'lov, 3️⃣ Ishga tushirish!"
        conv_nums = emoji_converter.convert_to_premium_emojis(text_nums)
        self.assertIn('<tg-emoji emoji-id="5208519140645551525">1️⃣</tg-emoji>', conv_nums)
        self.assertIn('<tg-emoji emoji-id="5208431510427810832">2️⃣</tg-emoji>', conv_nums)
        self.assertIn('<tg-emoji emoji-id="5210941321811871048">3️⃣</tg-emoji>', conv_nums)

        # 3. Variation selector matching (e.g. ⚙ without \ufe0f vs ⚙️ with \ufe0f)
        text_gear_clean = "Sozlamalar: \u2699 va \u26a0"
        conv_gear = emoji_converter.convert_to_premium_emojis(text_gear_clean)
        self.assertIn('5350396951407895212', conv_gear)
        self.assertIn('5447644880824181073', conv_gear)

        # 4. Strict Anchor tag protection (no nested tg-emoji inside <a>)
        text_link = 'Batafsil ma\'lumot: <a href="https://example.com/post?id=1">Kanalga o\'tish 🚀</a>.'
        conv_link = emoji_converter.convert_to_premium_emojis(text_link)
        self.assertNotIn('<a href="https://example.com/post?id=1">Kanalga o\'tish <tg-emoji', conv_link)
        self.assertIn('<a href="https://example.com/post?id=1">Kanalga o\'tish 🚀</a>', conv_link)

        # 5. Idempotence: No double-wrapping on already converted tg-emoji
        already_converted = '<tg-emoji emoji-id="5456432998092133477">✅</tg-emoji> All ready! 🚀'
        conv_idempotent = emoji_converter.convert_to_premium_emojis(already_converted)
        self.assertNotIn('<tg-emoji emoji-id="5456432998092133477"><tg-emoji', conv_idempotent)
        self.assertIn('<tg-emoji emoji-id="5372917041193828849">🚀</tg-emoji>', conv_idempotent)

    def test_cloner_engine_process_post_text_vip_emoji_signature(self):
        async def run():
            from config.settings import settings
            user_id = 999111
            settings.ADMIN_IDS_RAW = f"{user_id}"

            cloner = ClonerEngine()
            pair = ChannelPair(
                id=1,
                user_id=user_id,
                source_channel="@src",
                target_channel="@tgt",
                auto_premium_emojis=True,
                custom_signature="👉 Obuna: @tgt_channel 🚀"
            )

            raw_post = "Yangi yangilik! 🔥 Tezkor xabar ⚡"
            processed = await cloner.process_post_text(raw_post, pair)

            # Both body emojis AND signature emojis should be converted to Telegram Premium custom emojis
            self.assertIn('<tg-emoji emoji-id="5402406965252989103">🔥</tg-emoji>', processed)
            self.assertIn('<tg-emoji emoji-id="5285063442204481914">⚡</tg-emoji>', processed)
            self.assertIn('<tg-emoji emoji-id="5332819376842226496">👉</tg-emoji>', processed)
            self.assertIn('<tg-emoji emoji-id="5372917041193828849">🚀</tg-emoji>', processed)

        asyncio.run(run())

if __name__ == "__main__":
    unittest.main()
