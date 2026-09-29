"""
Dedicated admin bot access control.

Runs against a throw-away DatabaseManager in a temporary directory with fake Telegram IDs; the admin
settings are patched with automatic restore, so nothing leaks into the shared test database or into
other tests (and no real account is ever granted or revoked).
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from aiogram.types import User as AiogramUser, Message, Chat
from config.settings import settings
from database.db_manager import DatabaseManager
from admin_bot.middlewares.admin_auth_middleware import AdminStrictAuthMiddleware
from admin_bot.keyboards.admin_keyboards import get_admin_reply_keyboard, get_admin_dashboard_keyboard

SUPER_ADMIN_ID = 7000000001
SECOND_SUPER_ADMIN_ID = 7000000002
STRANGER_ID = 7000000999
PROMOTED_USER_ID = 7000000123


class TestAdminBotSecurity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self._tmp_dir.name, "admin_bot_security.db")
        self.db = DatabaseManager(self.db_path)
        # Settings first: init_db() seeds the admin cache from the configured super admins
        self._patches = [
            patch.object(settings, "ADMIN_IDS_RAW", f"{SUPER_ADMIN_ID},{SECOND_SUPER_ADMIN_ID}"),
            patch.object(settings, "PRIMARY_SUPER_ADMIN_ID", 0),
            patch("admin_bot.middlewares.admin_auth_middleware.db_manager", self.db),
        ]
        for p in self._patches:
            p.start()
        await self.db.init_db()
        self.middleware = AdminStrictAuthMiddleware()

    async def asyncTearDown(self):
        from tests.test_utils import safe_cleanup_db
        for p in reversed(self._patches):
            p.stop()
        await safe_cleanup_db(self.db_path, self.db)
        self._tmp_dir.cleanup()

    async def test_admin_keyboard_structure(self):
        reply_kb = get_admin_reply_keyboard()
        buttons = [btn.text for row in reply_kb.keyboard for btn in row]
        self.assertTrue(any("Boshqaruv Paneli" in b for b in buttons))
        self.assertTrue(any("Tizim Holati & Server" in b for b in buttons))
        self.assertTrue(any("MTProto Hisob" in b for b in buttons))
        self.assertTrue(any("Xabar Tarqatish" in b for b in buttons))
        self.assertTrue(any("Foydalanuvchilar" in b for b in buttons))
        self.assertTrue(any("Baza Nusxasi" in b for b in buttons))

        inline_kb = get_admin_dashboard_keyboard(is_auth=True)
        inline_buttons = [btn.text for row in inline_kb.inline_keyboard for btn in row]
        self.assertTrue(any("MTProto: Ulangan" in b for b in inline_buttons))
        self.assertTrue(any("Server Holati" in b for b in inline_buttons))
        self.assertTrue(any("Baza Backup" in b for b in inline_buttons))

    async def test_non_admin_blocked_by_middleware(self):
        unauthorized_user = AiogramUser(id=STRANGER_ID, is_bot=False, first_name="Stranger")
        chat = Chat(id=STRANGER_ID, type="private")
        fake_message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=unauthorized_user,
            text="/start"
        )

        handler_called = False

        async def mock_handler(event, data):
            nonlocal handler_called
            handler_called = True
            return "ok"

        # Non-admin user tries to interact with Admin Bot
        result = await self.middleware(mock_handler, fake_message, {"event_from_user": unauthorized_user})
        self.assertIsNone(result)
        self.assertFalse(handler_called, "Non-admin must NEVER reach the handler!")

    async def test_admin_allowed_by_middleware(self):
        admin_user = AiogramUser(id=SUPER_ADMIN_ID, is_bot=False, first_name="Admin")
        chat = Chat(id=SUPER_ADMIN_ID, type="private")
        fake_message = Message(
            message_id=2,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=admin_user,
            text="/admin"
        )

        handler_called = False

        async def mock_handler(event, data):
            nonlocal handler_called
            handler_called = True
            return "admin_ok"

        # Super admin interacts with Admin Bot
        result = await self.middleware(mock_handler, fake_message, {"event_from_user": admin_user})
        self.assertEqual(result, "admin_ok")
        self.assertTrue(handler_called, "Admin user must pass through middleware cleanly.")

    async def test_set_admin_status_grant_and_revoke_with_cache_invalidation(self):
        test_uid = PROMOTED_USER_ID
        # Initialize user as normal user
        await self.db.get_or_create_user(test_uid, "Test Non Admin", "testnonadmin", is_admin=False)
        self.assertFalse(await self.db.is_admin(test_uid))
        self.assertNotIn(test_uid, self.db._admin_cache)

        # Grant admin
        ok = await self.db.set_admin_status(test_uid, True)
        self.assertTrue(ok)
        self.assertTrue(await self.db.is_admin(test_uid))
        self.assertIn(test_uid, self.db._admin_cache)

        # Revoke admin
        ok = await self.db.set_admin_status(test_uid, False)
        self.assertTrue(ok)
        self.assertFalse(await self.db.is_admin(test_uid))
        self.assertNotIn(test_uid, self.db._admin_cache)

        # Ensure subsequent normal interaction does not resurrect admin status
        user = await self.db.get_or_create_user(test_uid, "Test Non Admin Updated", "testnonadmin")
        self.assertFalse(user.is_admin)
        self.assertFalse(await self.db.is_admin(test_uid))
        self.assertNotIn(test_uid, self.db._admin_cache)

    async def test_super_admin_cannot_be_revoked(self):
        ok = await self.db.set_admin_status(SUPER_ADMIN_ID, False)
        self.assertFalse(ok)
        self.assertTrue(await self.db.is_admin(SUPER_ADMIN_ID))

    async def test_settings_restored_after_test(self):
        # The patched super admins are visible inside the test only (restored in asyncTearDown)
        self.assertEqual(settings.admin_ids, {SUPER_ADMIN_ID, SECOND_SUPER_ADMIN_ID})


if __name__ == "__main__":
    unittest.main()
