import unittest
from datetime import datetime, timezone
from aiogram.types import User as AiogramUser, Message, Chat
from config.settings import settings
from admin_bot.middlewares.admin_auth_middleware import AdminStrictAuthMiddleware
from admin_bot.keyboards.admin_keyboards import get_admin_reply_keyboard, get_admin_dashboard_keyboard

class TestAdminBotSecurity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings.ADMIN_IDS_RAW = "8881989487,1686689830"
        self.middleware = AdminStrictAuthMiddleware()

    async def test_admin_keyboard_structure(self):
        reply_kb = get_admin_reply_keyboard()
        buttons = [btn.text for row in reply_kb.keyboard for btn in row]
        self.assertIn("Boshqaruv Paneli", buttons)
        self.assertIn("Tizim Holati & Server", buttons)
        self.assertIn("MTProto Hisob", buttons)
        self.assertIn("Xabar Tarqatish", buttons)
        self.assertIn("Foydalanuvchilar", buttons)
        self.assertIn("Baza Nusxasi (Backup)", buttons)

        inline_kb = get_admin_dashboard_keyboard(is_auth=True)
        inline_buttons = [btn.text for row in inline_kb.inline_keyboard for btn in row]
        self.assertIn("MTProto: Ulangan", inline_buttons)
        self.assertIn("Server Holati", inline_buttons)
        self.assertIn("Baza Backup (.db)", inline_buttons)

    async def test_non_admin_blocked_by_middleware(self):
        unauthorized_user = AiogramUser(id=999999999, is_bot=False, first_name="Stranger")
        chat = Chat(id=999999999, type="private")
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
        admin_user = AiogramUser(id=8881989487, is_bot=False, first_name="Admin")
        chat = Chat(id=8881989487, type="private")
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

if __name__ == "__main__":
    unittest.main()
