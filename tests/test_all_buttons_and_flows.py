import os
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram import Bot, Dispatcher
from aiogram.types import User, Chat, Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey

from database.db_manager import DatabaseManager
from config.settings import settings

import bot.handlers.start as start_mod
import bot.handlers.help_guide as help_guide_mod
import bot.handlers.cloner_menu as cloner_menu_mod
import bot.handlers.settings_menu as settings_menu_mod
import bot.handlers.stars_billing as stars_billing_mod
import bot.handlers.history_clone as history_clone_mod

import admin_bot.handlers.dashboard as admin_dashboard_mod
import admin_bot.handlers.system_status as admin_status_mod
import admin_bot.handlers.user_management as admin_user_mod
import admin_bot.handlers.broadcast as admin_broadcast_mod
import admin_bot.handlers.backup as admin_backup_mod
import admin_bot.handlers.mtproto_auth as admin_auth_mod

class TestAllButtonsAndFlows(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.test_db_path = "test_e2e_flows.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
            
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

        # Patch global db_manager across all handlers
        self.patchers = [
            patch("bot.handlers.start.db_manager", self.db),
            patch("bot.handlers.cloner_menu.db_manager", self.db),
            patch("bot.handlers.settings_menu.db_manager", self.db),
            patch("bot.handlers.stars_billing.db_manager", self.db),
            patch("bot.handlers.history_clone.db_manager", self.db),
            patch("admin_bot.handlers.dashboard.db_manager", self.db),
            patch("admin_bot.handlers.system_status.db_manager", self.db),
            patch("admin_bot.handlers.user_management.db_manager", self.db),
            patch("admin_bot.handlers.broadcast.db_manager", self.db),
            patch("admin_bot.handlers.backup.db_manager", self.db),
            patch("services.disaster_recovery.db_manager", self.db),
        ]
        for p in self.patchers:
            p.start()

        # Setup user and pair
        self.user_id = 7770001
        self.admin_id = 9990001
        settings.ADMIN_IDS_RAW = f"{self.admin_id}"

        await self.db.get_or_create_user(self.user_id, "Test User", "testuser", is_admin=False)
        await self.db.get_or_create_user(self.admin_id, "Super Admin", "superadmin", is_admin=True)
        await self.db.activate_subscription(self.user_id, "vip", 300, "test_vip_charge", days=30)

        self.pair_id = await self.db.add_channel_pair(
            user_id=self.user_id,
            source_channel="@source_channel",
            source_title="Source Channel",
            target_channel="@target_channel",
            target_title="Target Channel"
        )

        self.storage = MemoryStorage()

    async def asyncTearDown(self):
        for p in self.patchers:
            p.stop()
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    def _create_mock_message(self, user_id: int, text: str = "") -> Message:
        msg = MagicMock(spec=Message)
        msg.message_id = 100
        msg.from_user = User(id=user_id, is_bot=False, first_name="Test", last_name="User", username="testuser")
        msg.chat = Chat(id=user_id, type="private")
        msg.text = text
        msg.caption = ""
        msg.entities = []
        msg.caption_entities = []
        msg.answer = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.edit_reply_markup = AsyncMock()
        msg.delete = AsyncMock()
        msg.copy_to = AsyncMock()
        msg.bot = AsyncMock(spec=Bot)
        return msg

    def _create_mock_callback(self, user_id: int, data: str) -> CallbackQuery:
        cb = MagicMock(spec=CallbackQuery)
        cb.id = "cb_123"
        cb.from_user = User(id=user_id, is_bot=False, first_name="Test", last_name="User", username="testuser")
        cb.data = data
        cb.answer = AsyncMock()
        cb.bot = AsyncMock(spec=Bot)

        msg = self._create_mock_message(user_id)
        cb.message = msg
        return cb

    def _get_fsm_context(self, user_id: int) -> FSMContext:
        key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
        return FSMContext(storage=self.storage, key=key)

    # --- TEST CLIENT BOT FLOWS ---

    async def test_start_and_navigation_buttons(self):
        msg = self._create_mock_message(self.user_id, "/start")
        state = self._get_fsm_context(self.user_id)
        await start_mod.cmd_start(msg, state)
        self.assertEqual(msg.answer.call_count, 2)

        # Quickstart callback
        cb_qs = self._create_mock_callback(self.user_id, "menu_quickstart")
        await start_mod.cb_quickstart(cb_qs)
        cb_qs.message.edit_text.assert_called_once()

        # Stats callback
        cb_st = self._create_mock_callback(self.user_id, "menu_stats")
        await start_mod.cb_stats(cb_st)
        cb_st.message.edit_text.assert_called_once()

        # Guide callback
        cb_gd = self._create_mock_callback(self.user_id, "menu_guide")
        await help_guide_mod.cb_guide(cb_gd)
        cb_gd.message.edit_text.assert_called_once()

        # Back to main menu
        cb_mm = self._create_mock_callback(self.user_id, "menu_main")
        await start_mod.cb_main_menu(cb_mm, state)
        cb_mm.message.edit_text.assert_called_once()

    async def test_stars_billing_buttons(self):
        state = self._get_fsm_context(self.user_id)
        cb_stars = self._create_mock_callback(self.user_id, "menu_stars")
        await stars_billing_mod.cb_billing_menu(cb_stars, state)
        cb_stars.message.edit_text.assert_called_once()

        # Test buying pro plan
        cb_pro = self._create_mock_callback(self.user_id, "buy_plan_pro")
        cb_pro.bot.send_invoice = AsyncMock()
        await stars_billing_mod.cb_buy_plan(cb_pro)
        cb_pro.bot.send_invoice.assert_called_once()

        # Test buying vip plan
        cb_vip = self._create_mock_callback(self.user_id, "buy_plan_vip")
        cb_vip.bot.send_invoice = AsyncMock()
        await stars_billing_mod.cb_buy_plan(cb_vip)
        cb_vip.bot.send_invoice.assert_called_once()

    async def test_cloner_menu_and_pair_settings(self):
        state = self._get_fsm_context(self.user_id)

        # 1. Cloner menu
        cb_cloner = self._create_mock_callback(self.user_id, "menu_cloner")
        await cloner_menu_mod.show_cloner_menu(cb_cloner, state)
        cb_cloner.message.edit_text.assert_called_once()

        # 2. List pairs
        cb_list = self._create_mock_callback(self.user_id, "cloner_list_pairs")
        await cloner_menu_mod.cb_list_pairs(cb_list, state)
        cb_list.message.edit_text.assert_called_once()

        # 3. View pair
        cb_view = self._create_mock_callback(self.user_id, f"pair_view_{self.pair_id}")
        await cloner_menu_mod.cb_view_pair(cb_view)
        cb_view.message.edit_text.assert_called_once()

        # 4. Toggle active status
        cb_toggle = self._create_mock_callback(self.user_id, f"pair_toggle_{self.pair_id}")
        await cloner_menu_mod.cb_toggle_pair(cb_toggle, state)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertFalse(p.is_active)

        # 5. Toggle clean links
        cb_clean = self._create_mock_callback(self.user_id, f"pair_toggle_clean_{self.pair_id}")
        await cloner_menu_mod.cb_toggle_clean(cb_clean, state)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertFalse(p.clean_links)

        # 6. Auto-translation menu & setting
        cb_trans_menu = self._create_mock_callback(self.user_id, f"pair_trans_menu_{self.pair_id}")
        await settings_menu_mod.cb_trans_menu(cb_trans_menu)
        cb_trans_menu.message.edit_text.assert_called_once()

        cb_trans_set = self._create_mock_callback(self.user_id, f"trans_set_{self.pair_id}_uz")
        await settings_menu_mod.cb_set_translate_lang(cb_trans_set)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertTrue(p.auto_translate)
        self.assertEqual(p.target_lang, "uz")

        # 7. Video Watermark menu, toggle, pos
        cb_vwm_menu = self._create_mock_callback(self.user_id, f"pair_vwm_menu_{self.pair_id}")
        await settings_menu_mod.cb_vwm_menu(cb_vwm_menu)
        cb_vwm_menu.message.edit_text.assert_called_once()

        cb_vwm_toggle = self._create_mock_callback(self.user_id, f"vwm_toggle_{self.pair_id}")
        await settings_menu_mod.cb_vwm_toggle(cb_vwm_toggle)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertEqual(p.video_watermark_type, "text")

        cb_vwm_pos = self._create_mock_callback(self.user_id, f"vwm_pos_{self.pair_id}_center")
        await settings_menu_mod.cb_vwm_pos(cb_vwm_pos)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertEqual(p.video_watermark_pos, "center")

        # 8. Drip Feed & Night Mode
        cb_drip_menu = self._create_mock_callback(self.user_id, f"pair_drip_menu_{self.pair_id}")
        await settings_menu_mod.cb_drip_menu(cb_drip_menu)
        cb_drip_menu.message.edit_text.assert_called_once()

        cb_drip_delay = self._create_mock_callback(self.user_id, f"drip_delay_{self.pair_id}_15")
        await settings_menu_mod.cb_drip_delay(cb_drip_delay)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertEqual(p.drip_delay_minutes, 15)

        cb_drip_night = self._create_mock_callback(self.user_id, f"drip_toggle_night_{self.pair_id}")
        await settings_menu_mod.cb_drip_toggle_night(cb_drip_night)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertIn(p.night_mode, ["silent", "buffer"])

        # 9. AI Paraphraser
        cb_ai_menu = self._create_mock_callback(self.user_id, f"pair_ai_menu_{self.pair_id}")
        await settings_menu_mod.cb_ai_menu(cb_ai_menu)
        cb_ai_menu.message.edit_text.assert_called_once()

        cb_ai_set = self._create_mock_callback(self.user_id, f"ai_set_{self.pair_id}_formal")
        await settings_menu_mod.cb_ai_set(cb_ai_set)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertEqual(p.ai_paraphrase_mode, "formal")

        # 10. Dynamic CTA
        cb_cta = self._create_mock_callback(self.user_id, f"pair_toggle_cta_{self.pair_id}")
        await settings_menu_mod.cb_toggle_cta(cb_cta)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertTrue(p.auto_cta_buttons)

        # 11. Protected content mode
        cb_prot = self._create_mock_callback(self.user_id, f"pair_toggle_prot_{self.pair_id}")
        await settings_menu_mod.cb_toggle_protected(cb_prot)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertTrue(p.is_protected_source)

        # 12. Disaster Backup menu & toggle
        cb_backup_menu = self._create_mock_callback(self.user_id, f"pair_backup_menu_{self.pair_id}")
        await settings_menu_mod.cb_backup_menu(cb_backup_menu)
        cb_backup_menu.message.edit_text.assert_called_once()

        cb_backup_toggle = self._create_mock_callback(self.user_id, f"backup_toggle_{self.pair_id}")
        await settings_menu_mod.cb_backup_toggle(cb_backup_toggle)
        p = await self.db.get_pair_by_id(self.pair_id)
        self.assertFalse(p.backup_enabled)

        # 13. Channel Pair Stats & Preview
        cb_stats = self._create_mock_callback(self.user_id, f"pair_stats_{self.pair_id}")
        await cloner_menu_mod.cb_pair_stats(cb_stats)
        cb_stats.message.edit_text.assert_called_once()

        cb_preview = self._create_mock_callback(self.user_id, f"pair_preview_{self.pair_id}")
        await cloner_menu_mod.cb_preview_pair(cb_preview)
        cb_preview.message.edit_text.assert_called_once()

    # --- TEST ADMIN BOT FLOWS ---

    async def test_admin_dashboard_and_controls(self):
        state = self._get_fsm_context(self.admin_id)

        # 1. Admin start
        msg_adm = self._create_mock_message(self.admin_id, "/admin")
        await admin_dashboard_mod.cmd_admin_start(msg_adm, state)
        self.assertEqual(msg_adm.answer.call_count, 2)

        # 2. Admin system status
        cb_sys = self._create_mock_callback(self.admin_id, "admin_system_status")
        await admin_status_mod.cb_admin_system_status(cb_sys)
        cb_sys.message.edit_text.assert_called_once()

        # 3. Admin users list
        cb_users = self._create_mock_callback(self.admin_id, "admin_users_list")
        await admin_user_mod.cb_users_list(cb_users)
        cb_users.message.edit_text.assert_called_once()

        # 4. Admin user detail
        cb_udet = self._create_mock_callback(self.admin_id, f"adm_user_{self.user_id}")
        await admin_user_mod.cb_user_detail(cb_udet)
        cb_udet.message.edit_text.assert_called_once()

        # 5. Grant VIP to user
        cb_grant = self._create_mock_callback(self.admin_id, f"adm_grant_{self.user_id}_vip")
        await admin_user_mod.cb_grant_tier(cb_grant)
        sub = await self.db.get_user_subscription(self.user_id)
        self.assertEqual(sub.tier, "vip")

        # 6. Revoke tier
        cb_revoke = self._create_mock_callback(self.admin_id, f"adm_revoke_{self.user_id}")
        await admin_user_mod.cb_revoke_tier(cb_revoke)
        sub = await self.db.get_user_subscription(self.user_id)
        self.assertEqual(sub.tier, "free")

        # 7. Backup download
        cb_bkp = self._create_mock_callback(self.admin_id, "admin_download_backup")
        cb_bkp.bot.send_document = AsyncMock()
        await admin_backup_mod.cb_download_backup(cb_bkp)
        self.assertTrue(cb_bkp.bot.send_document.called or cb_bkp.message.answer.called)

        # 8. Restart listener
        cb_restart = self._create_mock_callback(self.admin_id, "admin_restart_listener")
        await admin_dashboard_mod.cb_restart_listener(cb_restart)
        cb_restart.message.edit_text.assert_called_once()

if __name__ == "__main__":
    unittest.main()
