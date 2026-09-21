import unittest
import asyncio
import os
from unittest.mock import MagicMock, AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest

from database.models import ChannelPair, Subscription
from services.telethon_listener import TelethonListener
from services.media_handler import MediaHandler
from services.dynamic_affiliate_engine import DynamicAffiliateEngine
from services.translator_service import TranslatorService
from services.cloner_engine import ClonerEngine
from bot.handlers.settings_menu import cb_toggle_protected

class MockWebPageMedia:
    pass

class MockEmptyMedia:
    pass

class TestAuditProductionFixes(unittest.IsolatedAsyncioTestCase):
    async def test_telethon_listener_core_methods(self):
        tl = TelethonListener()
        # 1. is_connected returns False initially
        self.assertFalse(tl.is_connected())

        # 2. get_me returns None when client is not connected
        me = await tl.get_me()
        self.assertIsNone(me)

        # 3. When connected and authorized
        tl._is_running = True
        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_me = MagicMock(id=12345, first_name="ClonerBot")
        mock_client.get_me = AsyncMock(return_value=mock_me)
        mock_client.disconnect = AsyncMock()
        tl.client = mock_client

        self.assertTrue(tl.is_connected())
        res_me = await tl.get_me()
        self.assertEqual(res_me.id, 12345)

        # 4. stop() disconnects cleanly
        await tl.stop()
        self.assertFalse(tl._is_running)
        mock_client.disconnect.assert_awaited_once()

    async def test_media_handler_webpage_and_empty_media(self):
        mh = MediaHandler(temp_dir="temp_media")

        # Message with web page preview (e.g. real estate post with channel link)
        msg_webpage = MagicMock()
        msg_webpage.media = MockWebPageMedia()
        msg_webpage.photo = False
        msg_webpage.video = False
        msg_webpage.voice = False
        msg_webpage.video_note = False
        msg_webpage.audio = False
        msg_webpage.sticker = False
        msg_webpage.gif = False
        msg_webpage.document = None
        msg_webpage.poll = None
        msg_webpage.contact = None
        msg_webpage.geo = None
        msg_webpage.venue = None
        msg_webpage.text = "📍 Orientir: Shoxsaroy restorani\nNarxi: $450"

        # WebPage media must return 'text' so the post is never dropped
        mtype = mh.get_media_type(msg_webpage)
        self.assertEqual(mtype, "text")

        # Message with None media
        msg_plain = MagicMock()
        msg_plain.media = None
        self.assertEqual(mh.get_media_type(msg_plain), "text")

        # Unknown media with text fallback
        class UnknownMedia:
            pass
        msg_unk = MagicMock()
        msg_unk.media = UnknownMedia()
        msg_unk.photo = False
        msg_unk.video = False
        msg_unk.voice = False
        msg_unk.video_note = False
        msg_unk.audio = False
        msg_unk.sticker = False
        msg_unk.gif = False
        msg_unk.document = None
        msg_unk.poll = None
        msg_unk.contact = None
        msg_unk.geo = None
        msg_unk.venue = None
        msg_unk.text = "Fallback text content"
        self.assertEqual(mh.get_media_type(msg_unk), "text")

    async def test_dynamic_affiliate_engine_newline_splitting(self):
        engine = DynamicAffiliateEngine()
        text = "Yaxshi mahsulot: https://uzum.uz/product/123 va yana https://aliexpress.com/item/456"
        # Test rules separated by newline
        rules_newline = "uzum.uz=partner_uzum\naliexpress.com=partner_ali"
        mod_text, cta_buttons = engine.extract_and_convert_links(text, rules_newline)

        self.assertIn("p=partner_uzum", mod_text)
        self.assertIn("aff_id=partner_ali", mod_text)
        self.assertEqual(len(cta_buttons), 2)

    async def test_translator_service_resilient_placeholder_restoration(self):
        ts = TranslatorService()
        mock_translator = MagicMock()

        # Simulate Google Translate converting brackets from ⟦990000⟧ to [ 990000 ]
        def fake_translate(text):
            return text.replace("⟦990000⟧", "[ 990000 ]").replace("⟦990001⟧", "{990001}")
        mock_translator.translate = MagicMock(side_effect=fake_translate)
        ts._get_translator = MagicMock(return_value=mock_translator)

        input_text = "Salom <b>dunyo</b> va https://t.me/kanal"
        translated = await ts.translate_text(input_text, target_lang="uz", source_lang="auto")

        # Placeholders must be successfully restored despite bracket modifications
        self.assertIn("<b>dunyo</b>", translated)
        self.assertIn("https://t.me/kanal", translated)
        self.assertNotIn("990000", translated)
        self.assertNotIn("990001", translated)

    async def test_cloner_engine_affiliate_before_clean_links(self):
        engine = ClonerEngine()
        pair = ChannelPair(
            id=1,
            user_id=123,
            source_channel="@src",
            target_channel="@tgt",
            clean_links=True,
            affiliate_rules="aliexpress.com=https://s.click.aliexpress.com/e/_my_promo"
        )

        raw_text = "Mana bu havola: https://aliexpress.com/item/100 and telegram: @bad_channel"
        processed = await engine.process_post_text(raw_text, pair)

        # Affiliate tag must be added
        self.assertIn("https://s.click.aliexpress.com/e/_my_promo", processed)
        # Telegram username must be cleaned
        self.assertNotIn("@bad_channel", processed)

    async def test_cloner_engine_media_group_compatible_partitioning(self):
        bot = MagicMock()
        bot.send_media_group = AsyncMock(return_value=[MagicMock(message_id=1), MagicMock(message_id=2)])
        bot.send_document = AsyncMock(return_value=MagicMock(message_id=3))

        engine = ClonerEngine(bot=bot)
        pair = ChannelPair(id=1, user_id=123, source_channel="@src", target_channel="@tgt")

        # Create 2 photos and 1 document
        p1 = MagicMock(id=101, grouped_id=999, photo=True, video=False, audio=False, document=None, media=MagicMock())
        p2 = MagicMock(id=102, grouped_id=999, photo=True, video=False, audio=False, document=None, media=MagicMock())
        doc = MagicMock(id=103, grouped_id=999, photo=False, video=False, audio=False, document=MagicMock(), media=MagicMock())

        with patch("services.cloner_engine.db_manager.is_message_cloned", AsyncMock(return_value=False)), \
             patch("services.cloner_engine.db_manager.record_cloned_message", AsyncMock()), \
             patch("services.cloner_engine.db_manager.get_user_subscription", AsyncMock(return_value=MagicMock(is_active=True))), \
             patch("services.cloner_engine.rate_limiter.wait_for_slot", AsyncMock()), \
             patch("services.cloner_engine.media_handler.download_telethon_media", AsyncMock(side_effect=lambda m: f"temp_media/{m.id}.dat")), \
             patch("services.cloner_engine.media_handler.get_media_type", side_effect=lambda m: "photo" if m.photo else "document"), \
             patch("services.cloner_engine.media_handler.cleanup_files", AsyncMock()), \
             patch("services.cloner_engine.disaster_recovery_service.archive_message", AsyncMock()), \
             patch("os.path.exists", return_value=True):

            ok = await engine.clone_media_group([p1, p2, doc], pair)
            self.assertTrue(ok)
            # send_media_group should be called for photos (partition 1)
            bot.send_media_group.assert_awaited_once()
            # send_document should be called for the document (partition 2 with 1 item)
            bot.send_document.assert_awaited_once()

    async def test_protected_mode_subscription_gate(self):
        cb = MagicMock()
        cb.data = "pair_toggle_prot_42"
        cb.from_user.id = 99999
        cb.answer = AsyncMock()

        pair = ChannelPair(id=42, user_id=99999, source_channel="@src", target_channel="@tgt")

        with patch("bot.handlers.settings_menu.db_manager.get_pair_by_id", AsyncMock(return_value=pair)), \
             patch("bot.handlers.settings_menu.db_manager.get_user_subscription", AsyncMock(return_value=Subscription(user_id=99999, tier="free", expires_at="2026-09-01T00:00:00"))), \
             patch("bot.handlers.settings_menu.db_manager.toggle_protected_mode", AsyncMock()) as mock_toggle:

            await cb_toggle_protected(cb)
            # Free tier user should be rejected
            mock_toggle.assert_not_called()
            cb.answer.assert_awaited_once()
            self.assertIn("VIP", cb.answer.call_args[1].get("text", ""))

    async def test_premium_emoji_trial_and_real_estate_conversion(self):
        engine = ClonerEngine()
        pair = ChannelPair(
            id=11,
            user_id=777888,
            source_channel="@cityjoyestateuz",
            target_channel="@arieltor_uz",
            auto_premium_emojis=True
        )

        # User on active 14-day trial (tier free, is_trial_active True)
        mock_sub = Subscription(
            user_id=777888,
            tier="free",
            trial_expires_at="2099-01-01T00:00:00"
        )

        with patch("services.cloner_engine.db_manager.get_user_subscription", AsyncMock(return_value=mock_sub)), \
             patch("services.cloner_engine.db_manager.is_admin", AsyncMock(return_value=False)):

            post_text = "🏢 Bino sotiladi! 🔹 3 xona ❗️ Chegirma bor"
            processed = await engine.process_post_text(post_text, pair)

            # Assert all emojis were converted into tg-emoji tags
            self.assertIn('<tg-emoji emoji-id="5319084384962248505">🏢</tg-emoji>', processed)
            self.assertIn('<tg-emoji emoji-id="5427168083074628963">🔹</tg-emoji>', processed)
            self.assertIn('<tg-emoji emoji-id="5447644880824181073">❗️</tg-emoji>', processed)

if __name__ == "__main__":
    unittest.main()
