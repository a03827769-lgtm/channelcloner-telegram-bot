import unittest
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import CallbackQuery, Message, User, Chat
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from config.settings import settings
from database.db_manager import DatabaseManager
from database.models import ChannelPair, Subscription
import bot.handlers.settings_menu as settings_menu_mod
import admin_bot.handlers.user_management as admin_user_mgmt_mod
from services.cloner_engine import cloner_engine

class TestTierRestrictionsAndAdminGrant(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "database/test_tier_admin_cloner.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

        self.db = DatabaseManager(db_path=self.test_db_path)
        await self.db.init_db()

        self.db_patcher = patch("bot.handlers.settings_menu.db_manager", self.db)
        self.db_patcher_admin = patch("admin_bot.handlers.user_management.db_manager", self.db)
        self.db_patcher_engine = patch("services.cloner_engine.db_manager", self.db)
        self.db_patcher.start()
        self.db_patcher_admin.start()
        self.db_patcher_engine.start()

        self.storage = MemoryStorage()
        self.free_user_id = 1110001
        self.pro_user_id = 2220002
        self.admin_id = 9990001
        settings.ADMIN_IDS_RAW = f"{self.admin_id}"

        # Register users
        await self.db.get_or_create_user(self.free_user_id, "Free User", "freeuser", is_admin=False)
        await self.db.get_or_create_user(self.pro_user_id, "Pro User", "prouser", is_admin=False)
        await self.db.get_or_create_user(self.admin_id, "Admin User", "adminuser", is_admin=True)

        # Give Pro User a Pro subscription
        await self.db.activate_subscription(self.pro_user_id, "pro", 100, "test_pro_charge", days=30)

        # Create channel pairs
        self.free_pair_id = await self.db.add_channel_pair(
            user_id=self.free_user_id,
            source_channel="@source_free",
            source_title="Source Free",
            target_channel="@target_free",
            target_title="Target Free"
        )

        self.pro_pair_id = await self.db.add_channel_pair(
            user_id=self.pro_user_id,
            source_channel="@source_pro",
            source_title="Source Pro",
            target_channel="@target_pro",
            target_title="Target Pro"
        )

    async def asyncTearDown(self):
        self.db_patcher.stop()
        self.db_patcher_admin.stop()
        self.db_patcher_engine.stop()
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    def _create_mock_callback(self, user_id: int, data: str):
        cb = AsyncMock(spec=CallbackQuery)
        cb.id = "cb_test_id"
        cb.data = data
        cb.from_user = User(id=user_id, is_bot=False, first_name="Test", username="tester")
        cb.message = AsyncMock(spec=Message)
        cb.message.chat = Chat(id=user_id, type="private")
        cb.message.edit_text = AsyncMock()
        cb.message.edit_reply_markup = AsyncMock()
        cb.answer = AsyncMock()
        return cb

    def _create_mock_message(self, user_id: int, text: str):
        msg = AsyncMock(spec=Message)
        msg.message_id = 999
        msg.from_user = User(id=user_id, is_bot=False, first_name="Test", username="tester")
        msg.chat = Chat(id=user_id, type="private")
        msg.text = text
        msg.answer = AsyncMock()
        return msg

    def _get_fsm_context(self, user_id: int):
        key = StorageKey(bot_id=12345, chat_id=user_id, user_id=user_id)
        return FSMContext(storage=self.storage, key=key)

    async def test_free_user_video_watermark_blocked(self):
        """Free user opening vwm menu sees upgrade paywall, toggle gives alert"""
        cb_menu = self._create_mock_callback(self.free_user_id, f"pair_vwm_menu_{self.free_pair_id}")
        await settings_menu_mod.cb_vwm_menu(cb_menu)
        cb_menu.message.edit_text.assert_called_once()
        text_arg = cb_menu.message.edit_text.call_args[1]["text"]
        self.assertIn("Pulli Funksiya", text_arg)
        self.assertIn("Free (Sinov)", text_arg)

        cb_toggle = self._create_mock_callback(self.free_user_id, f"vwm_toggle_{self.free_pair_id}")
        await settings_menu_mod.cb_vwm_toggle(cb_toggle)
        cb_toggle.answer.assert_called_with(text="🔒 Video Watermark faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)

    async def test_free_user_ai_paraphraser_blocked(self):
        """Free user opening AI menu sees upgrade paywall, setting mode gives alert"""
        cb_menu = self._create_mock_callback(self.free_user_id, f"pair_ai_menu_{self.free_pair_id}")
        await settings_menu_mod.cb_ai_menu(cb_menu)
        cb_menu.message.edit_text.assert_called_once()
        text_arg = cb_menu.message.edit_text.call_args[1]["text"]
        self.assertIn("Pulli Funksiya", text_arg)

        cb_set = self._create_mock_callback(self.free_user_id, f"ai_set_{self.free_pair_id}_hype")
        await settings_menu_mod.cb_ai_set(cb_set)
        cb_set.answer.assert_called_with(text="🔒 AI Content Paraphraser faqat PRO va VIP tariflarida mavjud! 'Tariflar & Obuna' bo'limidan faollashtiring.", show_alert=True)

    async def test_pro_user_can_access_video_watermark_and_ai(self):
        """Pro user can open vwm & ai menus and toggle modes without blocks"""
        cb_vwm_menu = self._create_mock_callback(self.pro_user_id, f"pair_vwm_menu_{self.pro_pair_id}")
        await settings_menu_mod.cb_vwm_menu(cb_vwm_menu)
        text_arg = cb_vwm_menu.message.edit_text.call_args[1]["text"]
        self.assertNotIn("Pulli Funksiya", text_arg)

        cb_vwm_toggle = self._create_mock_callback(self.pro_user_id, f"vwm_toggle_{self.pro_pair_id}")
        await settings_menu_mod.cb_vwm_toggle(cb_vwm_toggle)
        cb_vwm_toggle.message.edit_reply_markup.assert_called_once()

        cb_ai_set = self._create_mock_callback(self.pro_user_id, f"ai_set_{self.pro_pair_id}_hype")
        await settings_menu_mod.cb_ai_set(cb_ai_set)
        p = await self.db.get_pair_by_id(self.pro_pair_id)
        self.assertEqual(p.ai_paraphrase_mode, "hype")

    async def test_cloner_engine_tier_enforcement(self):
        """Engine skips AI paraphrase for Free tier and applies it for Pro/VIP"""
        pair_free = await self.db.get_pair_by_id(self.free_pair_id)
        pair_free.ai_paraphrase_mode = "hype"
        
        sample_post = "Assalomu alaykum! Bugun ajoyib yangiliklar bor."
        processed_free = await cloner_engine.process_post_text(sample_post, pair_free)
        # Should NOT have hype transformation because user is Free
        self.assertNotIn("EKSKLYUZIV", processed_free)

        pair_pro = await self.db.get_pair_by_id(self.pro_pair_id)
        pair_pro.ai_paraphrase_mode = "hype"
        processed_pro = await cloner_engine.process_post_text(sample_post, pair_pro)
        self.assertIn("EKSKLYUZIV", processed_pro)

    async def test_admin_search_and_grant_multi_durations(self):
        """Admin can search user by ID or username and grant 30d, 90d, 365d, 3650d VIP/PRO"""
        state = self._get_fsm_context(self.admin_id)
        
        # 1. Search by ID
        msg_search_id = self._create_mock_message(self.admin_id, f"{self.free_user_id}")
        await admin_user_mgmt_mod.process_user_search_query(msg_search_id, state)
        msg_search_id.answer.assert_called_once()
        self.assertIn(f"{self.free_user_id}", msg_search_id.answer.call_args[1]["text"])

        # 2. Search by Username
        msg_search_uname = self._create_mock_message(self.admin_id, "@prouser")
        await admin_user_mgmt_mod.process_user_search_query(msg_search_uname, state)
        msg_search_uname.answer.assert_called_once()
        self.assertIn(f"{self.pro_user_id}", msg_search_uname.answer.call_args[1]["text"])

        # 3. Grant 90 days VIP to Free User
        cb_grant = self._create_mock_callback(self.admin_id, f"adm_grant_{self.free_user_id}_vip_90")
        with patch("admin_bot.handlers.user_management.Bot") as MockBot:
            mock_bot_instance = AsyncMock()
            MockBot.return_value = mock_bot_instance
            await admin_user_mgmt_mod.cb_grant_tier(cb_grant)
        
        sub = await self.db.get_user_subscription(self.free_user_id)
        self.assertEqual(sub.tier, "vip")
        self.assertTrue(sub.is_active)

        # 4. Revoke back to Free
        cb_revoke = self._create_mock_callback(self.admin_id, f"adm_revoke_{self.free_user_id}")
        await admin_user_mgmt_mod.cb_revoke_tier(cb_revoke)
        sub_revoked = await self.db.get_user_subscription(self.free_user_id)
        self.assertEqual(sub_revoked.tier, "free")

if __name__ == "__main__":
    unittest.main()
