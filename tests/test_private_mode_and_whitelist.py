import unittest
import os
import tempfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from aiogram.types import Message
from database.db_manager import DatabaseManager
from config.settings import settings
from bot.middlewares.private_mode_middleware import PrivateModeGatekeeperMiddleware
from bot.handlers.stars_billing import (
    cb_private_unlock_stars_50,
    process_pre_checkout_query,
    process_successful_payment
)
from bot.handlers.start import cb_check_private_access
from admin_bot.handlers.access_control import (
    render_bot_mode_view,
    cb_toggle_bot_mode,
    process_support_user_input,
    cb_whitelist_revoke,
    process_whitelist_user_input
)
from services.cache_manager import cache_manager
from tests.test_utils import safe_cleanup_db


class TestPrivateModeAndWhitelist(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.test_db_path = os.path.join(self._tmp_dir.name, f"test_priv_{uuid.uuid4().hex[:8]}.db")
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

        await cache_manager.settings_cache.clear()
        await cache_manager.sub_cache.clear()

        # Only the modules under test are pointed at this test's database (patching the singleton in
        # database.db_manager itself would leak into every module imported for the first time meanwhile)
        self.patchers = [
            patch("bot.middlewares.private_mode_middleware.db_manager", self.db),
            patch("bot.handlers.stars_billing.db_manager", self.db),
            patch("bot.handlers.start.db_manager", self.db),
            patch("admin_bot.handlers.access_control.db_manager", self.db),
            patch("admin_bot.handlers.dashboard.db_manager", self.db)
        ]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        for p in self.patchers:
            p.stop()
        await cache_manager.settings_cache.clear()
        await cache_manager.sub_cache.clear()
        await safe_cleanup_db(self.test_db_path, self.db)
        self._tmp_dir.cleanup()

    async def test_public_mode_allows_all(self):
        await self.db.set_private_mode(False)
        self.assertFalse(await self.db.is_private_mode())

        # Any random user should have access in public mode
        user_id = 999111
        self.assertTrue(await self.db.can_user_access_bot(user_id))

    async def test_private_mode_allows_admin(self):
        await self.db.set_private_mode(True)
        self.assertTrue(await self.db.is_private_mode())

        admin_id = 12345
        with patch.object(settings, "ADMIN_IDS_RAW", str(admin_id)):
            self.assertTrue(await self.db.can_user_access_bot(admin_id))

    async def test_private_mode_grandfather_existing_users(self):
        # 1. Register an existing user in public mode
        existing_user_id = 112233
        past_time = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, full_name, username, created_at) VALUES (?, ?, ?, ?)",
                (existing_user_id, "Old User", "olduser", past_time)
            )
            await conn.commit()

        # 2. Enable private mode now
        await self.db.set_private_mode(True)
        self.assertTrue(await self.db.is_private_mode())

        # Existing user should have access (grandfathered)
        self.assertTrue(await self.db.can_user_access_bot(existing_user_id))

        # 3. Create a brand new user after private mode was enabled
        new_user_id = 445566
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, full_name, username, created_at) VALUES (?, ?, ?, ?)",
                (new_user_id, "New User", "newuser", future_time)
            )
            await conn.commit()

        # New user must be blocked!
        self.assertFalse(await self.db.can_user_access_bot(new_user_id))

    async def test_private_mode_allows_active_subscribers_and_channels(self):
        await self.db.set_private_mode(True)

        # User with Pro subscription
        pro_user_id = 778899
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, full_name, created_at) VALUES (?, ?, ?)",
                (pro_user_id, "Pro User", future_time)
            )
            await conn.commit()

        # Without subscription: blocked
        self.assertFalse(await self.db.can_user_access_bot(pro_user_id))

        # Grant pro subscription
        await self.db.activate_subscription(user_id=pro_user_id, tier="pro", stars=100, charge_id="ch_pro_1", days=30)
        self.assertTrue(await self.db.can_user_access_bot(pro_user_id))

        # User with channel pairs configured
        pair_user_id = 334455
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, full_name, created_at) VALUES (?, ?, ?)",
                (pair_user_id, "Pair User", future_time)
            )
            await conn.commit()
        await self.db.add_channel_pair(user_id=pair_user_id, source_channel="-1001", target_channel="-1002")
        self.assertTrue(await self.db.can_user_access_bot(pair_user_id))

    async def test_whitelist_management(self):
        await self.db.set_private_mode(True)

        user_id = 556677
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, full_name, username, created_at) VALUES (?, ?, ?, ?)",
                (user_id, "Whitelisted User", "wl_user", future_time)
            )
            await conn.commit()

        # Initially blocked
        self.assertFalse(await self.db.can_user_access_bot(user_id))
        self.assertFalse(await self.db.is_user_whitelisted(user_id))

        # Add to whitelist
        await self.db.add_user_to_whitelist(user_id=user_id, added_by=1, source="admin", note="Approved by admin")
        self.assertTrue(await self.db.is_user_whitelisted(user_id))
        self.assertTrue(await self.db.can_user_access_bot(user_id))

        # Get whitelisted users
        wl_list = await self.db.get_whitelisted_users()
        self.assertEqual(len(wl_list), 1)
        self.assertEqual(wl_list[0]["user_id"], user_id)
        self.assertEqual(wl_list[0]["username"], "wl_user")

        # Remove from whitelist
        removed = await self.db.remove_user_from_whitelist(user_id)
        self.assertTrue(removed)
        self.assertFalse(await self.db.is_user_whitelisted(user_id))
        self.assertFalse(await self.db.can_user_access_bot(user_id))

    async def test_private_mode_gatekeeper_middleware(self):
        await self.db.set_private_mode(True)

        mw = PrivateModeGatekeeperMiddleware()

        # Mock handler
        handler = AsyncMock()

        # 1. Allowed user (admin)
        admin_user = MagicMock()
        admin_user.id = 12345
        admin_user.is_bot = False
        with patch.object(settings, "ADMIN_IDS_RAW", "12345"):
            msg = MagicMock(spec=Message)
            msg.from_user = admin_user
            msg.successful_payment = None
            await mw(handler, msg, {"event_from_user": admin_user})
            self.assertTrue(handler.called)

        # 2. Blocked new user
        handler.reset_mock()
        blocked_user = MagicMock()
        blocked_user.id = 888999
        blocked_user.is_bot = False

        future_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, full_name, created_at) VALUES (?, ?, ?)",
                (blocked_user.id, "Blocked User", future_time)
            )
            await conn.commit()

        blocked_msg = MagicMock(spec=Message)
        blocked_msg.from_user = blocked_user
        blocked_msg.successful_payment = None
        blocked_msg.answer = AsyncMock()

        res = await mw(handler, blocked_msg, {"event_from_user": blocked_user})
        self.assertIsNone(res)
        self.assertFalse(handler.called)
        self.assertTrue(blocked_msg.answer.called)
        call_args = blocked_msg.answer.call_args
        self.assertTrue("Yopiq" in call_args[1]["text"] or "Shaxsiy" in call_args[1]["text"])

    async def test_50_stars_unlock_flow(self):
        await self.db.set_private_mode(True)

        user_id = 998877
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, full_name, created_at) VALUES (?, ?, ?)",
                (user_id, "Stars Unlocker", future_time)
            )
            await conn.commit()

        # User is initially blocked
        self.assertFalse(await self.db.can_user_access_bot(user_id))

        # 1. Invoice request via callback query
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.bot = AsyncMock()
        cb.bot.send_invoice = AsyncMock()
        cb.answer = AsyncMock()

        await cb_private_unlock_stars_50(cb)
        self.assertTrue(cb.bot.send_invoice.called)
        inv_kwargs = cb.bot.send_invoice.call_args[1]
        self.assertEqual(inv_kwargs["chat_id"], user_id)
        self.assertEqual(inv_kwargs["currency"], "XTR")
        self.assertEqual(inv_kwargs["prices"][0].amount, 50)

        # 2. Pre-checkout query validation
        pcq = MagicMock()
        pcq.from_user.id = user_id
        pcq.invoice_payload = f'{{"t": "private_50", "u": {user_id}}}'
        pcq.total_amount = 50
        pcq.answer = AsyncMock()

        await process_pre_checkout_query(pcq)
        pcq.answer.assert_called_once_with(ok=True)

        # 3. Successful payment processing
        msg = MagicMock()
        msg.from_user.id = user_id
        msg.from_user.full_name = "Stars Unlocker"
        msg.successful_payment.invoice_payload = f'{{"t": "private_50", "u": {user_id}}}'
        msg.successful_payment.total_amount = 50
        msg.successful_payment.telegram_payment_charge_id = "test_ch_50_stars"
        msg.answer = AsyncMock()
        msg.bot = AsyncMock()

        await process_successful_payment(msg)

        # User should now be whitelisted and have 14 days free tier
        self.assertTrue(await self.db.is_user_whitelisted(user_id))
        self.assertTrue(await self.db.can_user_access_bot(user_id))

        sub = await self.db.get_user_subscription(user_id)
        self.assertTrue(sub.is_active)
        self.assertIsNotNone(sub.trial_expires_at)

        # Check whitelist record
        wl = await self.db.get_whitelisted_users(source="stars_50")
        self.assertEqual(len(wl), 1)
        self.assertEqual(wl[0]["user_id"], user_id)

    async def test_admin_access_control_panel(self):
        state = AsyncMock()
        admin_id = 111
        target_user_id = 888111

        with patch.object(settings, "ADMIN_IDS_RAW", str(admin_id)),              patch.object(settings, "PRIMARY_SUPER_ADMIN_ID", 0):
            # 1. Render view
            text, kb = await render_bot_mode_view()
            self.assertIn("BOT REJIMI", text)
            self.assertTrue("Public" in text or "Ommaviy" in text)

            # 2. Toggle mode
            cb = MagicMock()
            cb.from_user.id = admin_id
            cb.message = MagicMock()
            cb.message.edit_text = AsyncMock()
            cb.answer = AsyncMock()

            await cb_toggle_bot_mode(cb)
            self.assertTrue(await self.db.is_private_mode())
            self.assertTrue(cb.answer.called)

            # 3. Change support username
            msg_sup = MagicMock()
            msg_sup.from_user.id = admin_id
            msg_sup.text = "@my_support_channel"
            msg_sup.answer = AsyncMock()
            await process_support_user_input(msg_sup, state)
            self.assertEqual(await self.db.get_support_username(), "my_support_channel")

            # 4. Add user by ID
            msg_add = MagicMock()
            msg_add.from_user.id = admin_id
            msg_add.text = str(target_user_id)
            msg_add.answer = AsyncMock()
            await process_whitelist_user_input(msg_add, state)

            self.assertTrue(await self.db.is_user_whitelisted(target_user_id))
            self.assertTrue(await self.db.can_user_access_bot(target_user_id))

            # 5. Revoke user
            cb_rev = MagicMock()
            cb_rev.from_user.id = admin_id
            cb_rev.data = f"adm_wl_rm_{target_user_id}"
            cb_rev.answer = AsyncMock()
            cb_rev.message = MagicMock()
            cb_rev.message.edit_text = AsyncMock()

            await cb_whitelist_revoke(cb_rev)
            self.assertFalse(await self.db.is_user_whitelisted(target_user_id))

        # A delegated (non-super) admin cannot switch the bot mode
        cb_other = MagicMock()
        cb_other.from_user.id = 222
        cb_other.answer = AsyncMock()
        await cb_toggle_bot_mode(cb_other)
        self.assertTrue(await self.db.is_private_mode())

    async def test_check_private_access_callback(self):
        await self.db.set_private_mode(True)

        user_id = 667788
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, full_name, created_at) VALUES (?, ?, ?)",
                (user_id, "Test User", future_time)
            )
            await conn.commit()

        cb = MagicMock()
        cb.from_user.id = user_id
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.delete = AsyncMock()
        cb.message.answer = AsyncMock()
        state = AsyncMock()

        # Initially blocked -> alert says still blocked
        await cb_check_private_access(cb, state)
        cb.answer.assert_called_with(text="Hali ruxsat berilmagan. Iltimos, admin javobini kuting yoki 50 Stars to'lang.", show_alert=True)

        # Admin whitelists user
        await self.db.add_user_to_whitelist(user_id, added_by=1)

        # Now check access -> granted!
        cb.answer.reset_mock()
        await cb_check_private_access(cb, state)
        cb.answer.assert_called_with(text="Ruxsat tasdiqlandi! Xush kelibsiz!", show_alert=True)
        self.assertTrue(cb.message.answer.called)


if __name__ == "__main__":
    unittest.main()
