import unittest
from database.models import ChannelPair
from services.text_processor import TextProcessor

class TestTextProcessor(unittest.TestCase):
    def setUp(self):
        self.pair = ChannelPair(
            id=1,
            user_id=1001,
            source_channel="@source_channel",
            target_channel="@target_channel",
            clean_links=True,
            custom_signature="👉 Bizning kanal: @target_channel",
            blacklist_words="reklama,1xbet,kazino",
            replace_words="salom=assalomu alaykum"
        )

    def test_normalize_channel_input(self):
        self.assertEqual(TextProcessor.normalize_channel_input("https://t.me/kunuzofficial/12345"), "@kunuzofficial")
        self.assertEqual(TextProcessor.normalize_channel_input("https://t.me/kunuzofficial"), "@kunuzofficial")
        self.assertEqual(TextProcessor.normalize_channel_input("t.me/kunuzofficial"), "@kunuzofficial")
        self.assertEqual(TextProcessor.normalize_channel_input("kunuzofficial"), "@kunuzofficial")
        self.assertEqual(TextProcessor.normalize_channel_input("@kunuzofficial"), "@kunuzofficial")
        self.assertEqual(TextProcessor.normalize_channel_input("-1001234567890"), "-1001234567890")
        self.assertEqual(TextProcessor.normalize_channel_input("https://t.me/+AbCdEfGh"), "https://t.me/+AbCdEfGh")

    def test_clean_usernames_and_links(self):
        text = "Yangi xabar! Batafsil @kunuz va https://t.me/kunuz/123 da o'qing. Kanalimiz: @kunuz"
        cleaned = TextProcessor.clean_links_and_usernames(text)
        self.assertNotIn("@kunuz", cleaned)
        self.assertNotIn("https://t.me/kunuz/123", cleaned)
        self.assertIn("Yangi xabar! Batafsil", cleaned)

    def test_blacklist_filtering(self):
        text_with_ad = "Katta aksiya! 1xbet orqali pul ishlang!"
        result = TextProcessor.process_text(text_with_ad, self.pair)
        self.assertIsNone(result, "Blacklisted message must return None")

        clean_text = "Bugun havo juda yaxshi bo'ladi."
        result2 = TextProcessor.process_text(clean_text, self.pair)
        self.assertIsNotNone(result2)
        self.assertIn("Bugun havo juda yaxshi", result2)

    def test_signature_attachment(self):
        text = "Muhim yangilik!"
        result = TextProcessor.process_text(text, self.pair)
        self.assertIn("👉 Bizning kanal: @target_channel", result)
        self.assertTrue(result.startswith("Muhim yangilik!"))

    def test_word_replacement(self):
        text = "salom hammaga!"
        result = TextProcessor.process_text(text, self.pair)
        self.assertIn("assalomu alaykum hammaga!", result)

if __name__ == "__main__":
    unittest.main()
