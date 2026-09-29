import unittest
import os
import tempfile
import uuid
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from database.db_manager import DatabaseManager
from services.text_processor import TextProcessor
from services.emoji_converter import emoji_converter
from services.translator_service import translator_service
from services.disaster_recovery import disaster_recovery_service

class TestAudit100FixesV2(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.test_db_path = os.path.join(self._tmp_dir.name, f"test_audit_{uuid.uuid4().hex[:8]}.db")
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        from tests.test_utils import safe_cleanup_db
        await safe_cleanup_db(self.test_db_path, self.db)
        self._tmp_dir.cleanup()

    async def test_get_user_subscription_concurrency(self):
        """DB-01: Multiple concurrent get_user_subscription calls should not lock SQLite"""
        user_id = 99112233
        await self.db.get_or_create_user(user_id, "Test User", "testuser")

        tasks = [self.db.get_user_subscription(user_id) for _ in range(10)]
        results = await asyncio.gather(*tasks)

        for sub in results:
            self.assertEqual(sub.user_id, user_id)
            self.assertEqual(sub.tier, "free")

    async def test_payment_replay_protection(self):
        """CB-04 / SR-04: Replay attack with same charge_id should be ignored"""
        user_id = 11223344
        await self.db.get_or_create_user(user_id, "Replay User", "replayuser")

        charge_id = "test_charge_unique_abc123"
        sub1 = await self.db.activate_subscription(user_id, "pro", 250, charge_id, days=30)
        self.assertEqual(sub1.tier, "pro")
        self.assertEqual(sub1.stars_spent, 250)

        # Attempt replay with the same charge_id
        sub2 = await self.db.activate_subscription(user_id, "pro", 250, charge_id, days=30)
        self.assertEqual(sub2.tier, "pro")
        self.assertEqual(sub2.stars_spent, 250)  # stars_spent should not be credited twice!

    async def test_revoke_subscription_deactivates_pairs(self):
        """AB-09: Revoking subscription should set is_active=0 for active pairs"""
        user_id = 55667788
        await self.db.get_or_create_user(user_id, "Active Pair User", "pairuser")
        await self.db.activate_subscription(user_id, "vip", 0, "admin_grant", days=30)

        pair_id = await self.db.add_channel_pair(
            user_id=user_id,
            source_channel="@src_chan",
            source_title="Source",
            target_channel="@tgt_chan",
            target_title="Target"
        )
        pair = await self.db.get_pair_by_id(pair_id)
        self.assertTrue(pair.is_active)

        # Revoke subscription
        await self.db.revoke_subscription(user_id)

        pair_after = await self.db.get_pair_by_id(pair_id)
        self.assertFalse(pair_after.is_active)

    async def test_stats_aggregated_queries(self):
        """DB-08: get_stats and get_user_stats should return correct aggregated metrics"""
        user_id = 99887766
        await self.db.get_or_create_user(user_id, "Stats User", "statsuser")
        await self.db.add_channel_pair(
            user_id=user_id,
            source_channel="@src_test",
            source_title="Source",
            target_channel="@tgt_test",
            target_title="Target"
        )

        stats = await self.db.get_stats()
        self.assertGreaterEqual(stats["total_users"], 1)
        self.assertGreaterEqual(stats["total_pairs"], 1)

        user_stats = await self.db.get_user_stats(user_id)
        self.assertEqual(user_stats["total_pairs"], 1)
        self.assertEqual(user_stats["active_pairs"], 1)
        self.assertEqual(user_stats["total_cloned"], 0)

    async def test_clean_old_cloned_messages_parameterized(self):
        """DB-03: clean_old_cloned_messages should execute without SQL syntax errors"""
        cloned, drip = await self.db.clean_old_cloned_messages(days=30)
        self.assertIsInstance(cloned, int)
        self.assertIsInstance(drip, int)

    def test_tg_deep_link_cleaning(self):
        """TX-01: tg:// deep links should be cleaned from messages"""
        raw_text = 'Kanalimizga kiring: tg://resolve?domain=testchannel va bot: tg://resolve?domain=testbot'
        cleaned = TextProcessor.clean_links_and_usernames(raw_text)
        self.assertNotIn("tg://", cleaned)
        self.assertNotIn("testchannel", cleaned)

        html_raw = '<a href="tg://resolve?domain=secretchannel">Maxfiy Kanal</a>'
        cleaned_html = TextProcessor.clean_links_and_usernames(html_raw)
        self.assertNotIn("tg://", cleaned_html)
        self.assertIn("Maxfiy Kanal", cleaned_html)

    def test_word_replacement_with_apostrophes(self):
        """TX-02: Words with apostrophes (e.g. so'z, ko'p) should match boundaries accurately"""
        raw_text = "Ushbu so'z va ko'p gaplar haqida gaplashamiz."
        replace_dict = {
            "so'z": "kalima",
            "ko'p": "ziyoda"
        }
        replaced = TextProcessor.apply_word_replacements(raw_text, replace_dict)
        self.assertIn("kalima", replaced)
        self.assertIn("ziyoda", replaced)
        self.assertNotIn("so'z", replaced)
        self.assertNotIn("ko'p", replaced)

    def test_duplicate_signature_prevention(self):
        """TX-13: Attaching signature to text that already ends with it should not duplicate"""
        sig = "@my_great_channel"
        text = f"Yangilik matni bu yerda.\n\n{sig}"
        result = TextProcessor.attach_signature(text, sig)
        self.assertEqual(result.count(sig), 1)

    def test_contains_blacklisted_words_unicode(self):
        """TX-11: Case-folding and Unicode normalization for blacklist filter"""
        text = "BU YERDA REKLAMA BOR"
        blacklist = ["reklama"]
        self.assertTrue(TextProcessor.contains_blacklisted_words(text, blacklist))

        # Cyrillic case folding
        text_cyr = "МАХСУЛОТ"
        blacklist_cyr = ["махсулот"]
        self.assertTrue(TextProcessor.contains_blacklisted_words(text_cyr, blacklist_cyr))

    def test_emoji_converter_preserves_comparison_and_emoticons(self):
        """TX-05: Non-HTML '<' emoticons (<3, < 5000) should be preserved"""
        text = "Narxi < 5000 so'm va sevgi <3 zo'r! 🔥"
        converted = emoji_converter.convert_to_premium_emojis(text)
        self.assertIn("< 5000", converted)
        self.assertIn("<3", converted)
        self.assertIn("<tg-emoji", converted)

    async def test_translator_chunking(self):
        """TX-06: translate_text handles long payloads (>4000 characters) via chunking"""
        long_paragraph = "Bu juda muhim xabar. " * 300  # ~6300 characters
        mock_translator = MagicMock()
        mock_translator.translate.side_effect = lambda t: f"TRANSLATED_{t[:20]}"

        with patch.object(translator_service, "_get_translator", return_value=mock_translator):
            result = await translator_service.translate_text(long_paragraph, target_lang="ru", source_lang="uz")
            self.assertIn("TRANSLATED_", result)
            self.assertGreaterEqual(mock_translator.translate.call_count, 2)

    async def test_disaster_recovery_fallback_handling(self):
        """MD-03: restore_channel handles video/document fallbacks for media_group"""
        mock_bot = AsyncMock()
        mock_bot.send_photo.side_effect = Exception("TelegramBadRequest: wrong type of file_id")
        mock_bot.send_video.return_value = MagicMock()

        owner_id = 4242
        mock_db = AsyncMock()
        mock_db.get_pair_by_id.return_value = MagicMock(user_id=owner_id)
        mock_db.get_channel_backup_count.return_value = 1
        mock_db.get_channel_backups.return_value = [
            {
                "text": "Caption video",
                "media_type": "media_group",
                "media_file_id": "BAADBAADvideo123",
                "message_id": 101
            }
        ]
        destination = MagicMock(id=-1001234567890)

        with patch("services.disaster_recovery.verify_destination_access",
                   new=AsyncMock(return_value=(True, destination, None))), \
             patch.object(type(disaster_recovery_service), "SEND_INTERVAL_SECONDS", 0):
            res = await disaster_recovery_service.restore_channel(
                bot=mock_bot,
                pair_id=1,
                new_target_channel="@target",
                db=mock_db,
                requester_id=owner_id
            )
        self.assertEqual(res["restored"], 1)
        self.assertEqual(res["failed"], 0)
        mock_bot.send_video.assert_called_once()
        self.assertEqual(mock_bot.send_video.call_args.kwargs["chat_id"], destination.id)
