import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from services.text_processor import TextProcessor
from services.cloner_engine import ClonerEngine, is_private_chat_target
from database.models import ChannelPair

class TestAdFilterAndSecurityGuards(unittest.IsolatedAsyncioTestCase):
    def test_commercial_ad_detection(self):
        # 1. Betting / Casino
        self.assertTrue(TextProcessor.is_commercial_ad('1xbet orqali stavka qiling va yuting!'))
        self.assertTrue(TextProcessor.is_commercial_ad('Aviator o\'yinida bugun katta yutuq!'))
        self.assertTrue(TextProcessor.is_commercial_ad('Melbet va Mostbet rasmiy kanali'))
        self.assertTrue(TextProcessor.is_commercial_ad('Depozitga 500% bonus olish uchun promokod'))
        
        # 2. Ad tags
        self.assertTrue(TextProcessor.is_commercial_ad('Yangi mahsulotimiz! #reklama batafsil'))
        self.assertTrue(TextProcessor.is_commercial_ad('Kompaniya aksiyasi. #ad'))
        self.assertTrue(TextProcessor.is_commercial_ad('Реклама: Самый лучший сервис в Ташкенте'))
        
        # 3. Bulletin spam
        self.assertTrue(TextProcessor.is_commercial_ad('Toshkent - Samarqand moshina bor odam bor'))

        # 4. Clean normal post
        self.assertFalse(TextProcessor.is_commercial_ad('Bugun Toshkent shahrida havo harorati 25 daraja bo\'ladi.'))
        self.assertFalse(TextProcessor.is_commercial_ad('Chilonzor 9-mavzeda 2 xonali kvartira ijaraga beriladi. Narxi: 400$'))

    def test_clean_links_and_usernames_removes_web_urls_and_ad_tags(self):
        text = 'Bizning do\'kon: https://example.com/shop va @channel_name #reklama'
        cleaned = TextProcessor.clean_links_and_usernames(text, remove_web_urls=True)
        self.assertNotIn('https://example.com/shop', cleaned)
        self.assertNotIn('@channel_name', cleaned)
        self.assertNotIn('#reklama', cleaned)
        self.assertIn('Bizning do\'kon:', cleaned)

    def test_process_text_drops_commercial_ads(self):
        pair = ChannelPair(
            id=1,
            user_id=100,
            source_channel='@source',
            target_channel='@target',
            clean_links=True
        )
        ad_post = 'Katta aksiya! 1win orqali pul ishlang! Depozitga bonus!'
        res = TextProcessor.process_text(ad_post, pair)
        self.assertIsNone(res, 'Commercial ad post must be dropped (return None)')

        normal_post = 'Ertaga elektr energiyasi ta\'mirlash sababli vaqtincha o\'chiriladi.'
        res_normal = TextProcessor.process_text(normal_post, pair)
        self.assertIsNotNone(res_normal)
        self.assertIn('elektr energiyasi', res_normal)

    def test_is_private_chat_target(self):
        # Positive IDs are private users
        self.assertTrue(is_private_chat_target(8881989487))
        self.assertTrue(is_private_chat_target(123456789))
        self.assertTrue(is_private_chat_target('8881989487'))
        self.assertTrue(is_private_chat_target('123456789'))

        # Channels/supergroups have negative IDs or usernames
        self.assertFalse(is_private_chat_target(-1004401815902))
        self.assertFalse(is_private_chat_target(-1001234567890))
        self.assertFalse(is_private_chat_target('@my_channel'))
        self.assertFalse(is_private_chat_target('my_channel'))
        self.assertFalse(is_private_chat_target(None))

    async def test_cloner_engine_blocks_private_user_target(self):
        engine = ClonerEngine(bot=MagicMock())
        private_pair = ChannelPair(
            id=99,
            user_id=100,
            source_channel='@src',
            target_channel='@user_dm',
            target_id=8881989487, # private user!
            clean_links=True
        )

        # 1. send_test_post must block
        ok, msg = await engine.send_test_post(private_pair)
        self.assertFalse(ok)
        self.assertIn('Shaxsiy profil', msg)

        # 2. clone_single_message must block
        mock_telethon_msg = MagicMock()
        mock_telethon_msg.id = 123
        mock_telethon_msg.text = 'Test message'
        ok_clone = await engine.clone_single_message(mock_telethon_msg, private_pair)
        self.assertFalse(ok_clone)

        # 3. _telethon_send_post must block
        sent = await engine._telethon_send_post(target_chat_id=8881989487, media_type='text', caption='Hello')
        self.assertIsNone(sent)

if __name__ == '__main__':
    unittest.main()
