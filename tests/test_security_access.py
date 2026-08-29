import unittest
import os
import uuid
from config.settings import settings
from database.db_manager import DatabaseManager
from bot.filters.admin_filter import is_admin_user
from bot.keyboards.inline_buttons import get_main_reply_keyboard, get_main_menu_keyboard, get_quickstart_keyboard

class TestSecurityAndAccessControl(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings.ADMIN_IDS_RAW = "8881989487,1686689830"
        self.test_db_path = f"temp_media/test_sec_access_{uuid.uuid4().hex[:8]}.db"
        os.makedirs("temp_media", exist_ok=True)
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    async def test_is_admin_user(self):
        self.assertTrue(is_admin_user(8881989487))
        self.assertTrue(is_admin_user(1686689830))
        self.assertFalse(is_admin_user(8414426548))
        self.assertFalse(is_admin_user(9999999999))
        self.assertFalse(is_admin_user(0))
        self.assertFalse(is_admin_user(None))

    async def test_user_menu_isolation(self):
        # 1. Public user reply keyboard must NEVER have Admin Panel, MTProto Hisob, or Tizim Holati
        user_reply_kb = get_main_reply_keyboard()
        user_buttons = [btn.text for row in user_reply_kb.keyboard for btn in row]
        self.assertNotIn("Admin Panel", user_buttons)
        self.assertNotIn("MTProto Hisob", user_buttons)
        self.assertNotIn("Tizim Holati", user_buttons)

        # 2. Public user inline keyboard must NEVER have Admin Panel, MTProto Ulash, or Tizim Holati
        user_inline_kb = get_main_menu_keyboard()
        user_inline_btns = [btn.text for row in user_inline_kb.inline_keyboard for btn in row]
        self.assertNotIn("Admin Panel", user_inline_btns)
        self.assertNotIn("MTProto Ulash", user_inline_btns)
        self.assertNotIn("MTProto Ulangan", user_inline_btns)
        self.assertNotIn("Tizim Holati & Server", user_inline_btns)

        # 3. Quickstart keyboard for public user must not have auth step
        user_qs_kb = get_quickstart_keyboard()
        user_qs_btns = [btn.text for row in user_qs_kb.inline_keyboard for btn in row]
        self.assertNotIn("1. Telegram Akkauntni Ulash", user_qs_btns)

    async def test_user_db_admin_sync(self):
        # Register regular user
        user = await self.db.get_or_create_user(8414426548, "Regular User", "reg_user")
        self.assertFalse(user.is_admin)
        self.assertFalse(await self.db.is_user_admin(8414426548))

        # Register admin user
        admin = await self.db.get_or_create_user(8881989487, "Admin User", "admin_user")
        self.assertTrue(admin.is_admin)
        self.assertTrue(await self.db.is_user_admin(8881989487))

    async def test_user_stats_isolation(self):
        user_a = 111000
        user_b = 222000
        await self.db.get_or_create_user(user_a, "User A")
        await self.db.get_or_create_user(user_b, "User B")

        # User A has 2 pairs
        pair_a1 = await self.db.add_channel_pair(user_a, "@src_a1", "Src A1", "@tgt_a1", "Tgt A1")
        pair_a2 = await self.db.add_channel_pair(user_a, "@src_a2", "Src A2", "@tgt_a2", "Tgt A2")

        # User B has 1 pair
        pair_b1 = await self.db.add_channel_pair(user_b, "@src_b1", "Src B1", "@tgt_b1", "Tgt B1")

        # Record messages
        await self.db.record_cloned_message(pair_a1, 101)
        await self.db.record_cloned_message(pair_a1, 102)
        await self.db.record_cloned_message(pair_a2, 103)
        await self.db.record_cloned_message(pair_b1, 201)

        stats_a = await self.db.get_user_stats(user_a)
        stats_b = await self.db.get_user_stats(user_b)

        self.assertEqual(stats_a["total_pairs"], 2)
        self.assertEqual(stats_a["total_cloned"], 3)
        self.assertEqual(stats_b["total_pairs"], 1)
        self.assertEqual(stats_b["total_cloned"], 1)

if __name__ == "__main__":
    unittest.main()
