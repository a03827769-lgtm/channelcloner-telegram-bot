import os
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock

from aiogram.types import InlineKeyboardMarkup

from services.button_remapper import SmartButtonRemapper
from services.session_pool import SessionPoolManager
from database.db_manager import DatabaseManager
from database.models import ChannelPair
from services.cloner_engine import ClonerEngine


class TestButtonRemapper(unittest.TestCase):

    def setUp(self):
        self.remapper = SmartButtonRemapper()

    def test_extract_telethon_buttons_with_markup(self):
        # Legacy Telethon format (btn.url)
        btn1 = MagicMock()
        btn1.text = "Kanalimiz"
        btn1.url = "https://t.me/competitor_channel"

        # Modern Telethon 1.45+ format (btn.type.url)
        btn2 = MagicMock()
        btn2.text = "Vebsayt"
        btn2.url = None
        btn2.type = MagicMock()
        btn2.type.url = "https://example.com/item"

        row = MagicMock()
        row.buttons = [btn1, btn2]
        markup = MagicMock()
        markup.rows = [row]

        msg = MagicMock()
        msg.reply_markup = markup

        buttons = self.remapper.extract_telethon_buttons(msg)
        self.assertIsNotNone(buttons)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(len(buttons[0]), 2)
        self.assertEqual(buttons[0][0]["text"], "Kanalimiz")
        self.assertEqual(buttons[0][0]["url"], "https://t.me/competitor_channel")
        self.assertEqual(buttons[0][1]["text"], "Vebsayt")
        self.assertEqual(buttons[0][1]["url"], "https://example.com/item")

    def test_extract_telethon_buttons_no_markup(self):
        msg = MagicMock()
        msg.reply_markup = None
        self.assertIsNone(self.remapper.extract_telethon_buttons(msg))

    def test_build_remapped_markup_remaps_competitor(self):
        source_buttons = [
            [
                {"text": "Obuna bo'ling", "url": "https://t.me/competitor_news"},
                {"text": "Rasmiy sayt", "url": "https://google.com"}
            ]
        ]
        target_link = "https://t.me/our_target_channel"
        markup = self.remapper.build_remapped_markup(
            source_buttons=source_buttons,
            target_channel_link=target_link,
            block_competitor_links=True
        )

        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(len(markup.inline_keyboard), 1)
        row = markup.inline_keyboard[0]
        self.assertEqual(len(row), 2)
        # First button pointing to competitor should be remapped
        self.assertEqual(row[0].url, target_link)
        # Second button pointing to external site should remain untouched
        self.assertEqual(row[1].url, "https://google.com")

    def test_build_remapped_markup_with_custom_cta(self):
        custom_cta = [
            {"text": "🔥 Admin bilan aloqa", "url": "https://t.me/admin_contact"}
        ]
        markup = self.remapper.build_remapped_markup(
            source_buttons=None,
            custom_cta_buttons=custom_cta
        )
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(markup.inline_keyboard[0][0].text, "🔥 Admin bilan aloqa")
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://t.me/admin_contact")

    def test_build_remapped_markup_empty_returns_none(self):
        self.assertIsNone(self.remapper.build_remapped_markup(None, None, None))


class TestSessionPoolManager(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.pool = SessionPoolManager()

    async def test_session_registration_and_availability(self):
        client1 = MagicMock()
        client1.is_connected.return_value = True

        self.pool.register_client("account_1", client1)
        self.assertTrue(self.pool.is_available("account_1"))
        self.assertEqual(self.pool.get_available_client(), client1)

    async def test_session_flood_wait_failover(self):
        client1 = MagicMock()
        client1.is_connected.return_value = True

        client2 = MagicMock()
        client2.is_connected.return_value = True

        self.pool.register_client("account_1", client1)
        self.pool.register_client("account_2", client2)

        # Mark account_1 as FloodWait for 60 seconds
        self.pool.record_flood_wait("account_1", 60)
        self.assertFalse(self.pool.is_available("account_1"))
        self.assertTrue(self.pool.is_available("account_2"))

        # Failover: pool should immediately route to account_2
        selected = self.pool.get_available_client()
        self.assertEqual(selected, client2)

    async def test_session_disconnect_all(self):
        client1 = MagicMock()
        client1.is_connected.return_value = True
        client1.disconnect = AsyncMock()

        self.pool.register_client("acc1", client1)
        await self.pool.disconnect_all()
        client1.disconnect.assert_awaited_once()
        self.assertEqual(len(self.pool.get_all_clients()), 0)


class TestTopicsAndAdActionDB(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)
        self.db = DatabaseManager(self.temp_db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    async def test_db_pair_topic_and_ad_action_crud(self):
        await self.db.add_user(user_id=8888, full_name="Topic Test User")

        pair_id = await self.db.add_channel_pair(
            user_id=8888,
            source_channel="@forum_src",
            target_channel="@forum_tgt",
            source_topic_id=1001,
            target_topic_id=2002,
            ad_action="drop",
            show_caption_above=True
        )

        pair = await self.db.get_pair_by_id(pair_id)
        self.assertIsNotNone(pair)
        self.assertEqual(pair.source_topic_id, 1001)
        self.assertEqual(pair.target_topic_id, 2002)
        self.assertEqual(pair.ad_action, "drop")
        self.assertTrue(pair.show_caption_above)

        # Update topics
        await self.db.update_pair_topics(pair_id, source_topic_id=5555, target_topic_id=7777)
        pair_updated = await self.db.get_pair_by_id(pair_id)
        self.assertEqual(pair_updated.source_topic_id, 5555)
        self.assertEqual(pair_updated.target_topic_id, 7777)

        # Update ad_action
        await self.db.update_pair_ad_action(pair_id, "swap")
        pair_swap = await self.db.get_pair_by_id(pair_id)
        self.assertEqual(pair_swap.ad_action, "swap")

        # Toggle show_caption_above
        new_val = await self.db.toggle_show_caption_above(pair_id)
        self.assertFalse(new_val)
        pair_cap = await self.db.get_pair_by_id(pair_id)
        self.assertFalse(pair_cap.show_caption_above)


class TestClonerEngineTopicRouting(unittest.IsolatedAsyncioTestCase):

    async def test_dispatch_queued_payload_routes_topic_and_caption_above(self):
        engine = ClonerEngine()
        engine.bot = MagicMock()
        engine.bot.send_photo = AsyncMock()
        engine.bot.send_message = AsyncMock()

        pair = ChannelPair(
            id=77,
            user_id=123,
            source_channel="@src_chan",
            target_channel="@tgt_chan",
            target_id=-1001999999999,
            target_topic_id=42,
            show_caption_above=True
        )

        payload = {
            "media_type": "photo",
            "media_file_id": "dummy_photo_file_id",
            "text": "Xabar matni"
        }

        mock_sent_msg = MagicMock()
        mock_sent_msg.message_id = 9999
        engine.bot.send_photo.return_value = mock_sent_msg

        await engine.dispatch_queued_payload(engine.bot, pair, payload)

        engine.bot.send_photo.assert_awaited_once()
        _, kwargs = engine.bot.send_photo.call_args
        self.assertEqual(kwargs.get("message_thread_id"), 42)
        self.assertTrue(kwargs.get("show_caption_above_media"))

    async def test_dispatch_queued_payload_routes_topic_text(self):
        engine = ClonerEngine()
        engine.bot = MagicMock()
        engine.bot.send_message = AsyncMock()

        pair = ChannelPair(
            id=78,
            user_id=123,
            source_channel="@src_chan",
            target_channel="@tgt_chan",
            target_id=-1001999999999,
            target_topic_id=105,
            show_caption_above=False
        )

        payload = {
            "media_type": "text",
            "text": "Faqat matnli xabar"
        }

        mock_sent_msg = MagicMock()
        mock_sent_msg.message_id = 10001
        engine.bot.send_message.return_value = mock_sent_msg

        await engine.dispatch_queued_payload(engine.bot, pair, payload)

        engine.bot.send_message.assert_awaited_once()
        _, kwargs = engine.bot.send_message.call_args
        self.assertEqual(kwargs.get("message_thread_id"), 105)


if __name__ == "__main__":
    unittest.main()
