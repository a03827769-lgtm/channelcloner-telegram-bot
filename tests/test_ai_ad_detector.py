import unittest

from services.ai_ad_detector import AIAdDetector
from database.models import ChannelPair
from services.cloner_engine import ClonerEngine

class TestAIAdDetector(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.detector = AIAdDetector()
        self.pair = ChannelPair(
            id=101,
            user_id=12345,
            source_channel="@source_test",
            target_channel="@target_test",
            clone_mode="mirror",
            clean_links=False,
            ad_action="drop"
        )

    def test_detect_casino_and_betting_heuristics(self):
        text1 = "1xBet rasmiy saytida ro'yxatdan o'ting, BONUS777 promokodini kiriting va 2,000,000 so'm oling! https://1xbet.com"
        res1 = self.detector.analyze_heuristic(text1)
        self.assertTrue(res1["is_ad"])
        self.assertEqual(res1["ad_type"], "casino")

        text2 = "Aviator va Lucky Jet o'yinlarida katta yutuqlar! Kafolatlangan signallar kanalimizda: t.me/aviator_win"
        res2 = self.detector.analyze_heuristic(text2)
        self.assertTrue(res2["is_ad"])
        self.assertEqual(res2["ad_type"], "casino")

    def test_detect_referral_and_promo_heuristics(self):
        text = "Yangi bot orqali kuniga 500 ming pul ishlang! Referal orqali kiring: https://t.me/pulishlashbot?start=998877"
        res = self.detector.analyze_heuristic(text)
        self.assertTrue(res["is_ad"])
        self.assertIn(res["ad_type"], ["casino", "sponsor"])

    def test_clean_news_not_flagged(self):
        news_text = (
            "<b>O'zbekistonda yangi energetika loyihasi ishga tushirildi</b>\n\n"
            "Navoiy viloyatida 500 MVt quvvatga ega quyosh fotoelektr stansiyasi muvaffaqiyatli tarmoqqa ulandi. "
            "Bu loyiha har yili 1 milliard kilovatt-soat elektr energiyasi ishlab chiqarish imkonini beradi."
        )
        res = self.detector.analyze_heuristic(news_text)
        self.assertFalse(res["is_ad"])
        self.assertEqual(res["ad_type"], "clean")

    async def test_process_ad_action_drop(self):
        ad_text = "1xBet bukmekerlik kompaniyasi! Promokod: TOP100 https://1xbet.uz"
        should_pub, result = await self.detector.process_ad_action(ad_text, action="drop")
        self.assertFalse(should_pub)
        self.assertEqual(result, "")

    async def test_process_ad_action_clean(self):
        ad_text = (
            "Bugungi eng muhim xabarlar to'plami.\n"
            "1xBet o'yinlarida ishtirok eting va yuting! https://1xbet.uz\n"
            "Prezident farmoni bilan yangi tartiblar belgilandi."
        )
        should_pub, result = await self.detector.process_ad_action(ad_text, action="clean")
        self.assertTrue(should_pub)
        self.assertNotIn("1xbet", result.lower())
        self.assertIn("Bugungi eng muhim", result)
        self.assertIn("Prezident farmoni", result)

    async def test_process_ad_action_swap(self):
        ad_text = (
            "Bugungi muhim yangiliklar tavsifi.\n"
            "Bizning hamkorimiz: 1xBet rasmiy bukmekerlik sayti https://1xbet.com"
        )
        target_sig = "👉 Bizning rasmiy kanal: @target_test"
        should_pub, result = await self.detector.process_ad_action(ad_text, action="swap", swap_signature=target_sig)
        self.assertTrue(should_pub)
        self.assertNotIn("1xbet", result.lower())
        self.assertIn(target_sig, result)

    async def test_cloner_engine_integration_blocks_ad(self):
        engine = ClonerEngine()
        self.pair.clone_mode = "mirror"
        self.pair.clean_links = False
        self.pair.ad_action = "drop"
        casino_post = "Melbet va 1win orqali katta stavka qiling! Promokod: VIPJACKPOT https://1win.xyz"
        processed = await engine.process_post_text(casino_post, self.pair)
        self.assertIsNone(processed)

    async def test_cloner_engine_integration_cleans_ad(self):
        engine = ClonerEngine()
        self.pair.clone_mode = "mirror"
        self.pair.clean_links = False
        self.pair.ad_action = "clean"
        post_with_ad = (
            "Toshkentda yangi texnopark ochildi.\n"
            "1xbet saytiga kiring https://1xbet.com promokod: 777\n"
            "Yangi ish o'rinlari yaratilishi kutilmoqda."
        )
        processed = await engine.process_post_text(post_with_ad, self.pair)
        self.assertIsNotNone(processed)
        self.assertNotIn("1xbet", processed.lower())
        self.assertIn("Toshkentda yangi texnopark", processed)

if __name__ == "__main__":
    unittest.main()
