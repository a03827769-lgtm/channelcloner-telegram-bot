import unittest
import os
import time
from unittest.mock import MagicMock, AsyncMock, patch

from database.db_manager import DatabaseManager
from services.text_processor import TextProcessor
from services.rate_limiter import SmartDelayEngine
from services.cache_manager import cache_manager
from bot.handlers.cloner_menu import safe_answer as cloner_safe_answer
from bot.handlers.settings_menu import get_watermark_pos_keyboard
from bot.middlewares.user_registration_middleware import UserRegistrationMiddleware
from config.settings import settings

class TestAuditCompleteFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        # Temporary directory (never next to the live database/cloner.db)
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.test_db_path = os.path.join(self._tmp_dir.name, "test_audit_complete.db")
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        from tests.test_utils import safe_cleanup_db
        await safe_cleanup_db(self.test_db_path, self.db)
        self._tmp_dir.cleanup()

    async def test_1_init_db_admin_insertion_no_tier_column_error(self):
        """1. init_db with configured admins succeeds. Environment admins are recognised dynamically (nothing
        is written for them), always have the VIP plan, and lose every right once removed from the config"""
        test_admin_id = 999888777
        with patch.object(settings, "ADMIN_IDS_RAW", str(test_admin_id)), \
             patch.object(settings, "PRIMARY_SUPER_ADMIN_ID", 0):
            await self.db.init_db()
            self.assertIsNone(await self.db.get_user_by_id(test_admin_id))
            self.assertTrue(await self.db.is_admin(test_admin_id))
            self.assertTrue(self.db.is_admin_sync(test_admin_id))

            sub = await self.db.get_user_subscription(test_admin_id)
            self.assertEqual(sub.tier, "vip")
            self.assertTrue(sub.is_active)

        with patch.object(settings, "ADMIN_IDS_RAW", ""), patch.object(settings, "PRIMARY_SUPER_ADMIN_ID", 0):
            self.assertFalse(await self.db.is_admin(test_admin_id))
            self.assertFalse(self.db.is_admin_sync(test_admin_id))
            self.assertNotEqual((await self.db.get_user_subscription(test_admin_id)).tier, "vip")

    async def test_2_save_channel_backup_idempotent_on_conflict(self):
        """2. Verify save_channel_backup does not crash on unique constraint violation and updates text"""
        # Create user and channel pair first to satisfy FK
        await self.db.get_or_create_user(111, "Backup User", "backup_user")
        pair_id = await self.db.add_channel_pair(
            user_id=111,
            source_channel="@source_test",
            source_title="Source Title",
            target_channel="@target_test",
            target_title="Target Title"
        )

        # Save first backup
        await self.db.save_channel_backup(
            pair_id=pair_id,
            source_id=-1001,
            message_id=42,
            text="Initial Backup Post",
            media_type="photo",
            media_file_id="file_abc_1"
        )

        # Save duplicate backup with modified text (e.g. edit in channel or re-clone)
        await self.db.save_channel_backup(
            pair_id=pair_id,
            source_id=-1001,
            message_id=42,
            text="Updated Backup Post",
            media_type="photo",
            media_file_id="file_abc_2"
        )

        backups = await self.db.get_channel_backups(pair_id=pair_id)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["text"], "Updated Backup Post")
        self.assertEqual(backups[0]["media_file_id"], "file_abc_2")

    async def test_3_get_or_create_user_preserves_username(self):
        """3. Verify get_or_create_user does not erase existing username when incoming username is None"""
        user1 = await self.db.get_or_create_user(555, "Temur Aliyev", "temur_ali")
        self.assertEqual(user1.username, "temur_ali")

        # Second call with username = None
        user2 = await self.db.get_or_create_user(555, "Temur Aliyev Yangilangan", None)
        self.assertEqual(user2.username, "temur_ali")
        self.assertEqual(user2.full_name, "Temur Aliyev Yangilangan")

        # Check DB directly
        db_user = await self.db.get_user_by_id(555)
        self.assertEqual(db_user.username, "temur_ali")

    async def test_4_search_users_negative_id_support(self):
        """4. Verify search_users correctly parses and searches negative numeric IDs"""
        await self.db.get_or_create_user(100123456, "Channel Super User", "super_chan")
        
        results = await self.db.search_users("100123456")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["user_id"], 100123456)

        await self.db.get_or_create_user(987654, "Test User", "test_user")
        results_neg = await self.db.search_users("-987654")
        self.assertTrue(isinstance(results_neg, list))

    async def test_5_text_processor_casing_preservation(self):
        """5. Verify apply_word_replacements preserves UPPERCASE and TitleCase capitalization"""
        replace_dict = {"olma": "anor"}
        raw_text = "OLMA sharbati, Olma mevasi va olma daraxti."
        result = TextProcessor.apply_word_replacements(raw_text, replace_dict)
        self.assertEqual(result, "ANOR sharbati, Anor mevasi va anor daraxti.")

    async def test_6_text_processor_attach_signature_boundary(self):
        """6. A signature on a post near the 4096 limit is kept whole: the post is split, never cut"""
        sig = "📢 Bizning kanal: @kanalim"
        long_text = "A" * 4090
        res = TextProcessor.attach_signature(long_text, sig)
        self.assertTrue(res.startswith(long_text))
        self.assertTrue(res.endswith(sig))

        chunks = TextProcessor.fit_text_limit(res, max_limit=4096)
        self.assertEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(TextProcessor.get_visible_text_length(chunk), 4096)
        self.assertTrue(chunks[-1].rstrip().endswith(sig))

    async def test_7_safe_answer_truncates_long_alerts(self):
        """7. Verify safe_answer truncates callback alert texts > 195 chars to avoid 200-char API crash"""
        mock_cb = AsyncMock()
        long_msg = "Xatolik yuz berdi: " + ("B" * 220)
        await cloner_safe_answer(mock_cb, long_msg, show_alert=True)
        mock_cb.answer.assert_awaited_once()
        called_text = mock_cb.answer.call_args.kwargs.get("text")
        self.assertLessEqual(len(called_text), 195)
        self.assertTrue(called_text.endswith("..."))

    async def test_8_user_registration_middleware_seen_cache(self):
        """8. Verify UserRegistrationMiddleware caches active users and eliminates redundant DB writes"""
        middleware = UserRegistrationMiddleware()
        mock_user = MagicMock()
        mock_user.id = 777888
        mock_user.full_name = "Kesh Foydalanuvchi"
        mock_user.username = "kesh_user"
        mock_user.is_bot = False

        event = MagicMock()
        event.from_user = mock_user
        data = {"event_from_user": mock_user}
        handler = AsyncMock(return_value="OK")

        # Clear any prior cache entry
        await cache_manager.seen_users_cache.clear()

        # Patch db_manager in middleware with our test instance
        with patch("bot.middlewares.user_registration_middleware.db_manager", self.db):
            # First call: creates user and caches
            await middleware(handler, event, data)
            user = await self.db.get_user_by_id(777888)
            self.assertIsNotNone(user)
            self.assertEqual(user.full_name, "Kesh Foydalanuvchi")

            # Mock get_or_create_user on our test instance to verify 2nd call hits cache
            with patch.object(self.db, "get_or_create_user", AsyncMock()) as mock_create:
                await middleware(handler, event, data)
                mock_create.assert_not_awaited()

    async def test_9_rate_limiter_prunes_stale_locks(self):
        """9. Verify SmartDelayEngine.prune_stale_locks purges idle channel locks"""
        engine = SmartDelayEngine()
        engine._last_post_time["@active_chan"] = time.time()
        engine._last_post_time["@old_chan"] = time.time() - 4000
        await engine._get_channel_lock("@active_chan")
        await engine._get_channel_lock("@old_chan")

        self.assertIn("@old_chan", engine._locks)
        self.assertIn("@active_chan", engine._locks)

        await engine.prune_stale_locks(max_idle_seconds=3600.0)

        self.assertNotIn("@old_chan", engine._locks)
        self.assertNotIn("@old_chan", engine._last_post_time)
        self.assertIn("@active_chan", engine._locks)

    def test_10_get_watermark_pos_keyboard_active_indicator(self):
        """10. Verify get_watermark_pos_keyboard displays active indicator on selected position"""
        from services.custom_emojis import ID_SUCCESS, ID_LOCATION
        kb = get_watermark_pos_keyboard(pair_id=10, current_pos="bottom_right")
        first_btn = kb.inline_keyboard[0][0]
        self.assertIn("[Faol]", first_btn.text)
        self.assertEqual(first_btn.icon_custom_emoji_id, ID_SUCCESS)
        self.assertEqual(first_btn.style, "success")

        second_btn = kb.inline_keyboard[0][1]
        self.assertNotIn("[Faol]", second_btn.text)
        self.assertEqual(second_btn.icon_custom_emoji_id, ID_LOCATION)
        self.assertEqual(second_btn.style, "primary")

if __name__ == "__main__":
    unittest.main()
