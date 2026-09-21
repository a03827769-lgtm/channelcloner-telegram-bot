import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.exceptions import TelegramBadRequest
from database.models import ChannelPair
from services.text_processor import TextProcessor
from services.emoji_converter import emoji_converter
from services.cloner_engine import ClonerEngine

class TestCaptionFitAndPremiumEmojis(unittest.TestCase):
    def test_caption_limit_does_not_split_heavy_html_tags_with_small_visible_text(self):
        """
        Real estate listing with many animated emoji tags and links:
        Raw HTML length exceeds 1500 chars, but visible text is only ~450 chars.
        fit_caption_limit must NOT split it into two separate posts!
        """
        raw_listing = (
            "✨✨✨ СДАЁТСЯ ✨✨✨\n\n"
            "⚜️ Район: Яшнабад\n"
            "🏢 Адрес: Дархан\n"
            "📍 Ориентир: Метро Хамид Олимжан\n\n"
            "Информация:\n"
            "• Тип: Аренда, Жилой\n"
            "• Этажность: 9\n"
            "• Этаж: 2\n"
            "• Комнат: 2\n"
            "• Площадь: 56 m²\n"
            "• Ремонт: Евро ремонт\n"
            "• Кондиционер: да\n"
            "• Телевизор: да\n"
            "• Стиральная машина: да\n\n"
            "Способы оплаты:\n"
            "💸 Предоплата: нет\n"
            "💳 Депозит: да\n"
            "💰 Цена: $700.00\n\n"
            "🗓️ Время освобождения: 4.9.2026\n\n"
            "📢 Имеются альтернативные варианты по всему городу.\n\n"
            '🔗 <a href="https://t.me/c/12345/6789">Переходите по ссылке:</a>\n'
            "🆔: 21235 887887011"
        )

        # Convert to premium emojis (adds ~55 chars per emoji)
        converted = emoji_converter.convert_to_premium_emojis(raw_listing)
        self.assertGreater(len(converted), 1024, "Raw HTML string length should exceed 1024 due to tags")

        vis_len = TextProcessor.get_visible_text_length(converted)
        self.assertLess(vis_len, 1020, "Visible text length must be well within Telegram's 1024 limit")

        caption, overflow = TextProcessor.fit_caption_limit(converted, max_limit=1020)
        self.assertIsNone(overflow, "Overflow must be None because visible text fits in one caption!")
        self.assertEqual(caption, converted, "Caption must remain completely intact without splitting!")

    def test_caption_limit_splits_when_visible_text_genuinely_exceeds_1020(self):
        """
        When visible plain text genuinely exceeds 1020 chars, it must split gracefully
        and balance HTML tags between caption and overflow.
        """
        long_body = "<b>" + ("Bu juda uzun post matni. " * 60) + "</b>\n\nOxirgi qism."
        vis_len = TextProcessor.get_visible_text_length(long_body)
        self.assertGreater(vis_len, 1020)

        caption, overflow = TextProcessor.fit_caption_limit(long_body, max_limit=1020)
        self.assertIsNotNone(overflow, "Must produce overflow when visible text genuinely exceeds limit")
        self.assertLessEqual(TextProcessor.get_visible_text_length(caption), 1020)
        self.assertTrue(caption.endswith("</b>"), "Caption must close the <b> tag cleanly")
        self.assertTrue(overflow.startswith("<b>"), "Overflow must reopen the <b> tag cleanly")

    def test_all_user_screenshot_emojis_convert_properly(self):
        """
        Verify all emojis from the user's real screenshot are mapped and converted.
        """
        test_str = "⚜️ VIP 💸 Pul 🗓️ Sana 🆔 ID 🏗️ Qurilish 🏙️ Shahar"
        converted = emoji_converter.convert_to_premium_emojis(test_str)

        self.assertIn('<tg-emoji emoji-id="5229011542011299168">⚜️</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5201873447554145566">💸</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5413879192267805083">🗓️</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5332679880599418983">🆔</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5319084384962248505">🏗️</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5319084384962248505">🏙️</tg-emoji>', converted)

    def test_send_with_retry_tier1_graceful_degradation(self):
        """
        When Bot API rejects <tg-emoji> on an unboosted channel with:
        'TelegramBadRequest: can't parse entities: Unsupported start tag "tg-emoji"'
        _send_with_retry must strip ONLY <tg-emoji> into Unicode emojis and keep <b>, <i>, <a> intact!
        """
        async def run():
            cloner = ClonerEngine()
            mock_send = AsyncMock()

            # 1st call fails with unsupported start tag "tg-emoji"
            # 2nd call (Tier 1 retry) succeeds
            mock_send.side_effect = [
                TelegramBadRequest(method=MagicMock(), message="Bad Request: can't parse entities: Unsupported start tag \"tg-emoji\""),
                MagicMock(message_id=9999)
            ]

            sample_caption = '<b>Super Taklif!</b> <tg-emoji emoji-id="5463289097336405244">✨</tg-emoji> <a href="https://t.me">Kanal</a>'
            result = await cloner._send_with_retry(
                mock_send,
                chat_id=-1001234567,
                caption=sample_caption,
                parse_mode="HTML"
            )

            self.assertIsNotNone(result)
            self.assertEqual(mock_send.call_count, 2)

            # Check 2nd call arguments: <tg-emoji> should be stripped to ✨, but <b> and <a> MUST BE PRESERVED!
            second_call_kwargs = mock_send.call_args_list[1][1]
            self.assertEqual(second_call_kwargs.get("parse_mode"), "HTML")
            self.assertIn("<b>Super Taklif!</b>", second_call_kwargs["caption"])
            self.assertIn('<a href="https://t.me">Kanal</a>', second_call_kwargs["caption"])
            self.assertNotIn("<tg-emoji", second_call_kwargs["caption"])
            self.assertIn("✨", second_call_kwargs["caption"])

        asyncio.run(run())

    def test_telethon_send_post_invoked_when_premium_emojis_enabled(self):
        """
        When auto_premium_emojis is True and Telethon client is connected,
        ClonerEngine should send via Telethon MTProto userbot.
        """
        async def run():
            cloner = ClonerEngine()

            with patch("services.telethon_listener.telethon_listener") as mock_tl:
                mock_tl.client.is_connected.return_value = True
                mock_tl.resolve_entity = AsyncMock(return_value=MagicMock(id=987654321))
                mock_tl.client.send_message = AsyncMock(return_value=MagicMock(id=555))

                sent = await cloner._telethon_send_post(
                    target_chat_id="-100987654321",
                    media_type="text",
                    caption='<tg-emoji emoji-id="5463289097336405244">✨</tg-emoji> Salom!'
                )

                self.assertIsNotNone(sent)
                self.assertEqual(sent.id, 555)
                mock_tl.client.send_message.assert_called_once()
                call_args = mock_tl.client.send_message.call_args
                self.assertEqual(call_args[1]["parse_mode"], "html")

        asyncio.run(run())

    def test_caption_limit_up_to_2048_does_not_split_for_telethon(self):
        """
        When sending via Telethon (cap_limit=2048), a 1400-char post with property details and footer
        must remain in ONE combined message without any overflow.
        """
        property_post = (
            "🏢 <b>Premium Kvartira Ijaraga Beriladi!</b>\n\n"
            + ("• Xona ma'lumoti: Yangi ta'mirdan chiqqan qulay kvartira.\n" * 20)
            + "\n📣 Имеются альтернативные варианты по всему городу...\n"
            + "🔗 Переходите по ссылке: https://t.me/arieltoruz\n"
            + "🆔: 20180 👉 Batafsil ma'lumot yuqorida keltirilgan!"
        )
        converted = emoji_converter.convert_to_premium_emojis(property_post)
        vis_len = TextProcessor.get_visible_text_length(converted)
        self.assertGreater(vis_len, 1024, "Should exceed 1024 to verify 2048 handling")
        self.assertLess(vis_len, 2048, "Should be under 2048")

        caption, overflow = TextProcessor.fit_caption_limit(converted, max_limit=2048)
        self.assertIsNone(overflow, "Must NOT split when within 2048 chars for Telethon")
        self.assertEqual(caption, converted, "Caption must remain 100% intact")

    def test_fit_text_limit_splits_long_plain_text_at_4096(self):
        """
        Plain text posts exceeding 4096 characters are split into clean chunks.
        """
        huge_text = ("Bu juda uzun matn bloki. " * 300)
        chunks = TextProcessor.fit_text_limit(huge_text, max_limit=4096)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(TextProcessor.get_visible_text_length(chunk), 4096)

    def test_extra_emojis_conversion(self):
        """
        Verify newly added emojis (e.g. 📣, 🔗, 🆔, 👉, ⬆️, ⬇️, 🌴, 🌊, 📋, 🚨, 📶, 📮, 📧, 📹, 📺, 🪪) convert to tg-emoji tags.
        """
        sample = "📣 E'lon 🔗 Havola 🆔 Raqam 👉 Bu yerda ⬆️ Yuqori 🌴 Daraxt 📋 Ro'yxat 🚨 Shoshilinch 📶 Internet 📮 Pochta 📧 Email 📹 Video 📺 TV 🪪 IDKarta"
        converted = emoji_converter.convert_to_premium_emojis(sample)
        self.assertIn('<tg-emoji emoji-id="5309984423003823246">📣</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5260450573768990626">🔗</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5332679880599418983">🆔</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5332819376842226496">👉</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5332348837405145999">⬆️</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5778184941154078090">🌴</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5233237686751355290">📋</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5447644880824181073">🚨</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5285063442204481914">📶</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5332811182044627428">📮</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5332811182044627428">📧</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5355325791452281549">📹</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5355325791452281549">📺</tg-emoji>', converted)
        self.assertIn('<tg-emoji emoji-id="5332679880599418983">🪪</tg-emoji>', converted)

    def test_send_test_post_uses_telethon_when_vip_emojis_enabled(self):
        """
        send_test_post should attempt _telethon_send_post when auto_premium_emojis is True.
        """
        async def run():
            cloner = ClonerEngine(bot=MagicMock())
            pair = ChannelPair(
                id=1,
                user_id=123,
                source_channel="@source",
                target_channel="@target",
                auto_premium_emojis=True
            )
            with patch.object(cloner, "_telethon_send_post", new=AsyncMock(return_value=MagicMock(id=777))) as mock_send:
                ok, msg = await cloner.send_test_post(pair)
                self.assertTrue(ok)
                self.assertIn("777", msg)
                mock_send.assert_called_once()

        asyncio.run(run())

    def test_telethon_send_media_group_chunks_over_10_files(self):
        """
        Telegram MTProto allows max 10 files per media album.
        _telethon_send_media_group must chunk 15 files into two calls (10 and 5).
        """
        async def run():
            cloner = ClonerEngine()
            with patch("services.telethon_listener.telethon_listener") as mock_tl:
                mock_tl.client.is_connected.return_value = True
                mock_tl.resolve_entity = AsyncMock(return_value=MagicMock(id=123))
                mock_tl.client.send_file = AsyncMock(side_effect=[
                    [MagicMock(id=1), MagicMock(id=2)],
                    [MagicMock(id=3)]
                ])

                files = [f"temp_media/test_{i}.jpg" for i in range(15)]
                sent = await cloner._telethon_send_media_group(
                    target_chat_id="-100123456",
                    file_paths=files,
                    caption="Mening albomim"
                )

                self.assertIsNotNone(sent)
                self.assertEqual(len(sent), 3)
                self.assertEqual(mock_tl.client.send_file.call_count, 2)
                # First call had caption, second call had empty caption
                first_call_kwargs = mock_tl.client.send_file.call_args_list[0][1]
                second_call_kwargs = mock_tl.client.send_file.call_args_list[1][1]
                self.assertEqual(first_call_kwargs["caption"], "Mening albomim")
                self.assertEqual(second_call_kwargs["caption"], "")
                self.assertEqual(len(first_call_kwargs["file"]), 10)
                self.assertEqual(len(second_call_kwargs["file"]), 5)

        asyncio.run(run())

    def test_telethon_resolve_entity_with_integer_channel_id(self):
        """
        resolve_entity must accept integer channel IDs without crashing with AttributeError.
        """
        async def run():
            from services.telethon_listener import TelethonListener
            tl = TelethonListener()
            tl.client = MagicMock()
            tl.client.is_connected.return_value = True
            tl.client.get_entity = AsyncMock(return_value=MagicMock(id=1001234567890))

            res = await tl.resolve_entity(-1001234567890)
            self.assertIsNotNone(res)
            self.assertEqual(res.id, 1001234567890)

        asyncio.run(run())

    def test_cloner_engine_with_premium_emojis_and_cta_buttons_passes_buttons_and_never_splits(self):
        """
        Verifies that when both auto_premium_emojis and auto_cta_buttons are enabled:
        1. Telethon _telethon_send_post is called WITH buttons.
        2. If fallback to Bot API occurs, tg-emoji tags are stripped so caption fits <= 1024
           and no second overflow message is sent.
        """
        async def run():
            from database.models import Subscription
            cloner = ClonerEngine(bot=MagicMock())
            pair = ChannelPair(
                id=19,
                user_id=8881989487,
                source_channel="@cityjoyestateuz",
                target_channel="@aRieltor_uz",
                auto_premium_emojis=True,
                auto_cta_buttons=True
            )

            # Test text with emojis and URL
            raw_text = "✨ Super taklif! https://uzum.uz/product/123 Narxi: 💸 100$"
            active_sub = Subscription(user_id=8881989487, tier="vip", expires_at="2099-01-01T00:00:00Z")

            with patch("database.db_manager.db_manager.get_user_subscription", new=AsyncMock(return_value=active_sub)), \
                 patch("database.db_manager.db_manager.is_admin", new=AsyncMock(return_value=True)):

                # Case 1: Telethon is available and connected
                with patch("services.telethon_listener.telethon_listener") as mock_tl:
                    mock_tl.is_connected.return_value = True
                    with patch.object(cloner, "_telethon_send_post", new=AsyncMock(return_value=MagicMock(id=555))) as mock_telethon:
                        mock_msg = MagicMock()
                        mock_msg.id = 101
                        mock_msg.message = raw_text
                        mock_msg.text = raw_text
                        mock_msg.entities = None
                        mock_msg.media = None

                        with patch("services.media_handler.media_handler.get_media_type", return_value="text"):
                            with patch("database.db_manager.db_manager.is_message_cloned", new=AsyncMock(return_value=False)):
                                with patch("database.db_manager.db_manager.record_cloned_message", new=AsyncMock()):
                                    with patch("services.disaster_recovery.disaster_recovery_service.archive_message", new=AsyncMock()):
                                        res = await cloner.clone_single_message(mock_msg, pair)
                                        self.assertTrue(res)
                                        mock_telethon.assert_called_once()
                                        _, kwargs = mock_telethon.call_args
                                        self.assertIsNotNone(kwargs.get("buttons"))

                # Case 2: Bot API fallback cleanly strips <tg-emoji> without overflow
                with patch("services.telethon_listener.telethon_listener") as mock_tl:
                    mock_tl.is_connected.return_value = False
                    mock_send_message = AsyncMock(return_value=MagicMock(message_id=777))
                    cloner.bot.send_message = mock_send_message

                    mock_msg = MagicMock()
                    mock_msg.id = 102
                    mock_msg.message = raw_text
                    mock_msg.text = raw_text
                    mock_msg.entities = None
                    mock_msg.media = None

                    with patch("services.media_handler.media_handler.get_media_type", return_value="text"):
                        with patch("database.db_manager.db_manager.is_message_cloned", new=AsyncMock(return_value=False)):
                            with patch("database.db_manager.db_manager.record_cloned_message", new=AsyncMock()):
                                with patch("services.disaster_recovery.disaster_recovery_service.archive_message", new=AsyncMock()):
                                    res = await cloner.clone_single_message(mock_msg, pair)
                                    self.assertTrue(res)
                                    # Exactly one message sent, no overflow second bubble
                                    self.assertEqual(mock_send_message.call_count, 1)
                                    called_text = mock_send_message.call_args[1]["text"]
                                    self.assertNotIn("<tg-emoji", called_text)

        asyncio.run(run())

    def test_clean_links_and_usernames_removes_dangling_cta_lead_in(self):
        """
        Verify that stripping usernames from lines like:
        '🔗 Переходите по ссылке: @cityjoyestateuz\n🆔: 20812'
        removes the entire dangling prompt '🔗 Переходите по ссылке:'
        and leaves the remaining content '🆔: 20812' intact.
        """
        raw_text = (
            "🏢 2-xona kvartira\n"
            "💰 Narxi: $700\n\n"
            "🔗 Переходите по ссылке: @cityjoyestateuz\n"
            "🆔: 20812"
        )
        cleaned = TextProcessor.clean_links_and_usernames(raw_text)
        self.assertNotIn("@cityjoyestateuz", cleaned)
        self.assertNotIn("Переходите по ссылке", cleaned)
        self.assertIn("🆔: 20812", cleaned)
        self.assertIn("🏢 2-xona kvartira", cleaned)

        # Test with HTML anchor link
        html_text = (
            "🏢 2-xona kvartira\n"
            '🔗 <a href="https://t.me/cityjoyestateuz">Переходите по ссылке:</a>\n'
            "🆔: 20812"
        )
        cleaned_html = TextProcessor.clean_links_and_usernames(html_text)
        self.assertNotIn("t.me/cityjoyestateuz", cleaned_html)
        self.assertNotIn("Переходите по ссылке", cleaned_html)
        self.assertIn("🆔: 20812", cleaned_html)

if __name__ == "__main__":
    unittest.main()
