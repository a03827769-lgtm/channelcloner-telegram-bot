import unittest
import os
import uuid
from database.db_manager import DatabaseManager

class TestDatabaseManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = f"temp_media/test_cloner_{uuid.uuid4().hex[:8]}.db"
        os.makedirs("temp_media", exist_ok=True)
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    async def test_user_operations(self):
        user = await self.db.get_or_create_user(12345, "Test User", "testuser", is_admin=True)
        self.assertEqual(user.user_id, 12345)
        self.assertEqual(user.full_name, "Test User")
        self.assertTrue(user.is_admin)

    async def test_channel_pair_operations(self):
        pair_id = await self.db.add_channel_pair(
            user_id=12345,
            source_channel="@src_test",
            source_title="Source Test",
            target_channel="@tgt_test",
            target_title="Target Test",
            clean_links=True,
            custom_signature="My Signature",
            blacklist_words="ad1,ad2"
        )
        self.assertIsNotNone(pair_id)

        pair = await self.db.get_pair_by_id(pair_id)
        self.assertEqual(pair.source_channel, "@src_test")
        self.assertEqual(pair.custom_signature, "My Signature")
        self.assertTrue(pair.is_active)

        # Toggle active
        new_status = await self.db.toggle_pair_active(pair_id)
        self.assertFalse(new_status)

        # Toggle clean
        new_clean = await self.db.toggle_clean_links(pair_id)
        self.assertFalse(new_clean)

        # List pairs
        pairs = await self.db.get_user_channel_pairs(12345)
        self.assertEqual(len(pairs), 1)

    async def test_cloned_message_tracking(self):
        pair_id = await self.db.add_channel_pair(
            user_id=12345,
            source_channel="@src",
            source_title="Source",
            target_channel="@tgt",
            target_title="Target"
        )

        # Check not cloned initially
        self.assertFalse(await self.db.is_message_cloned(pair_id, 999))

        # Record cloned
        await self.db.record_cloned_message(pair_id, 999, target_msg_id=1001)

        # Check now cloned
        self.assertTrue(await self.db.is_message_cloned(pair_id, 999))

    async def test_pair_advanced_updates(self):
        pair_id = await self.db.add_channel_pair(
            user_id=12345,
            source_channel="@src_adv",
            source_title="Source Adv",
            target_channel="@tgt_adv",
            target_title="Target Adv"
        )

        # Test update_pair_source_id
        await self.db.update_pair_source_id(pair_id, 1001999)
        pair = await self.db.get_pair_by_id(pair_id)
        self.assertEqual(pair.source_id, 1001999)

        # Test update_pair_blacklist
        await self.db.update_pair_blacklist(pair_id, "spam,promo,badword")
        pair = await self.db.get_pair_by_id(pair_id)
        self.assertEqual(pair.blacklist_words, "spam,promo,badword")

        # Test set_auto_translate
        await self.db.set_auto_translate(pair_id, enabled=True, target_lang="ru")
        pair = await self.db.get_pair_by_id(pair_id)
        self.assertTrue(pair.auto_translate)
        self.assertEqual(pair.target_lang, "ru")

        # Test can_user_add_channel admin vs user
        can_add, max_ch, curr = await self.db.can_user_add_channel(12345, is_admin=True)
        self.assertTrue(can_add)
        self.assertEqual(max_ch, 999)

    async def test_stats(self):
        await self.db.get_or_create_user(1, "User 1")
        await self.db.get_or_create_user(2, "User 2")
        p_id = await self.db.add_channel_pair(1, "@src1", "S1", "@tgt1", "T1")
        await self.db.record_cloned_message(p_id, 10)

        stats = await self.db.get_stats()
        self.assertEqual(stats["total_users"], 2)
        self.assertEqual(stats["total_pairs"], 1)
        self.assertEqual(stats["active_pairs"], 1)
        self.assertEqual(stats["total_cloned_messages"], 1)

if __name__ == "__main__":
    unittest.main()
