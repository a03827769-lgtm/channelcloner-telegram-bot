import unittest
import asyncio
from unittest.mock import MagicMock, patch
from services.translator_service import translator_service

class TestTranslatorService(unittest.TestCase):
    def test_placeholder_masking_and_translation(self):
        async def run():
            translator_service._cache.clear()
            translator_service._translators.clear()
            # Mock GoogleTranslator.translate to return translated text with tags preserved
            with patch("services.translator_service.GoogleTranslator") as MockGT:
                mock_inst = MagicMock()
                mock_inst.translate.side_effect = lambda t: t.replace("Hello", "Salom")
                MockGT.return_value = mock_inst

                text = "Hello world! Follow https://t.me/example and @mychannel 🚀"
                res = await translator_service.translate_text(text, target_lang="uz")
                self.assertIn("Salom", res)
                self.assertIn("https://t.me/example", res)
                self.assertIn("@mychannel", res)

        asyncio.run(run())

if __name__ == "__main__":
    unittest.main()
