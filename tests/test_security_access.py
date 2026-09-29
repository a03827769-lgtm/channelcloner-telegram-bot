import unittest
import os
import tempfile
import uuid
from unittest.mock import patch
from config.settings import settings
from database.db_manager import DatabaseManager
from bot.filters.admin_filter import is_admin_user
from bot.keyboards.inline_buttons import get_main_reply_keyboard, get_main_menu_keyboard, get_quickstart_keyboard

# Fake Telegram IDs (never real accounts)
ADMIN_A = 7100000001
ADMIN_B = 7100000002
REGULAR_USER = 7100000003

class TestSecurityAndAccessControl(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Patched with automatic restore, so the admin list never leaks into other tests
        self._settings_patches = [
            patch.object(settings, "ADMIN_IDS_RAW", f"{ADMIN_A},{ADMIN_B}"),
            patch.object(settings, "PRIMARY_SUPER_ADMIN_ID", 0),
        ]
        for p in self._settings_patches:
            p.start()
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.test_db_path = os.path.join(self._tmp_dir.name, f"test_sec_access_{uuid.uuid4().hex[:8]}.db")
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        from tests.test_utils import safe_cleanup_db
        await safe_cleanup_db(self.test_db_path, self.db)
        self._tmp_dir.cleanup()
        for p in reversed(self._settings_patches):
            p.stop()

    async def test_is_admin_user(self):
        self.assertTrue(is_admin_user(ADMIN_A))
        self.assertTrue(is_admin_user(ADMIN_B))
        self.assertFalse(is_admin_user(REGULAR_USER))
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

        # 4. Even when is_admin=True is passed, public bot keyboards never include admin panel (moved to Admin Bot)
        admin_reply_kb = get_main_reply_keyboard(is_admin=True)
        admin_buttons = [btn.text for row in admin_reply_kb.keyboard for btn in row]
        self.assertNotIn("👑 Boshqaruv Paneli", admin_buttons)
        self.assertNotIn("Admin Panel", admin_buttons)

        admin_inline_kb = get_main_menu_keyboard(is_admin=True)
        admin_inline_btns = [btn.text for row in admin_inline_kb.inline_keyboard for btn in row]
        self.assertNotIn("Super Admin Paneli", admin_inline_btns)
        self.assertNotIn("Admin Panel", admin_inline_btns)

    async def test_user_db_admin_sync(self):
        # Register regular user
        user = await self.db.get_or_create_user(REGULAR_USER, "Regular User", "reg_user")
        self.assertFalse(user.is_admin)
        self.assertFalse(await self.db.is_user_admin(REGULAR_USER))

        # Register admin user
        admin = await self.db.get_or_create_user(ADMIN_A, "Admin User", "admin_user")
        self.assertTrue(admin.is_admin)
        self.assertTrue(await self.db.is_user_admin(ADMIN_A))

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
