import pytest
import os
import html
import asyncio
from PIL import Image

from config.settings import Settings
from database.models import ChannelPair
from database.db_manager import DatabaseManager
from services.rate_limiter import SmartDelayEngine
from services.watermark_service import WatermarkService
from services.text_processor import TextProcessor, TG_USERNAME_PATTERN
from services.affiliate_replacer import AffiliateReplacer
from services.ai_paraphraser import AIParaphraserService

class TestMasterAuditFixes:
    def test_settings_empty_string_coercion(self):
        s = Settings(
            TELEGRAM_API_ID="",
            TELEGRAM_API_HASH="",
            TELETHON_SESSION="   ",
            PORT=""
        )
        assert s.TELEGRAM_API_ID is None
        assert s.TELEGRAM_API_HASH is None
        assert s.TELETHON_SESSION is None
        assert s.PORT == 8080

    def test_replace_dict_empty_key_protection(self):
        pair = ChannelPair(
            id=1,
            user_id=100,
            source_channel="src",
            source_title="Source",
            target_channel="tgt",
            target_title="Target",
            replace_words="=bar, foo=baz,   =invalid"
        )
        r_dict = pair.replace_dict
        assert "" not in r_dict
        assert "foo" in r_dict
        assert r_dict["foo"] == "baz"
        assert len(r_dict) == 1

        text = "hello world"
        result = TextProcessor.apply_word_replacements(text, r_dict)
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_add_channel_pair_auto_premium_emojis(self, tmp_path):
        test_db = str(tmp_path / "test_cloner.db")
        db = DatabaseManager(test_db)
        await db.init_db()
        await db.add_user(user_id=12345, full_name="Test User")

        pair_id = await db.add_channel_pair(
            user_id=12345,
            source_channel="@src_test",
            source_title="Source Test",
            target_channel="@tgt_test",
            target_title="Target Test",
            auto_premium_emojis=True
        )

        pair = await db.get_pair_by_id(pair_id)
        assert pair is not None
        assert pair.auto_premium_emojis is True
        await db.close()

    def test_rate_limiter_channel_normalization(self):
        engine = SmartDelayEngine()
        key1 = engine._normalize_channel_key("@MyChannel")
        key2 = engine._normalize_channel_key("https://t.me/mychannel")
        key3 = engine._normalize_channel_key("t.me/MyChannel")
        key4 = engine._normalize_channel_key("http://t.me/mychannel")

        assert key1 == "mychannel"
        assert key2 == "mychannel"
        assert key3 == "mychannel"
        assert key4 == "mychannel"

    def test_watermark_windows_file_overwrite(self, tmp_path):
        img_path = str(tmp_path / "sample.jpg")
        img = Image.new("RGB", (400, 300), color=(100, 150, 200))
        img.save(img_path, format="JPEG")

        res_path = WatermarkService.apply_text_watermark(
            image_path=img_path,
            text="@WatermarkBot",
            position="bottom_right",
            output_path=None
        )
        assert res_path == img_path
        assert os.path.exists(img_path)
        with Image.open(img_path) as check_img:
            assert check_img.size == (400, 300)

    def test_text_processor_url_case_preservation(self):
        url_replacement = "https://t.me/MyCaseSensitiveLink"
        matched = TextProcessor._match_case("VIP", url_replacement)
        assert matched == "https://t.me/MyCaseSensitiveLink"

        bot_replacement = "@MySpecialBot"
        matched_bot = TextProcessor._match_case("BOT", bot_replacement)
        assert matched_bot == "@MySpecialBot"

        word_replacement = "olma"
        assert TextProcessor._match_case("NOK", word_replacement) == "OLMA"
        assert TextProcessor._match_case("Nok", word_replacement) == "Olma"

    def test_text_processor_email_protection(self):
        sample_text = "Murojaat uchun: support@company.com yoki admin.team@domain.org ga yozing."
        matches = TG_USERNAME_PATTERN.findall(sample_text)
        assert len(matches) == 0

        tg_text = "Bizning adminimiz: @super_admin_bot va @channel_support"
        tg_matches = TG_USERNAME_PATTERN.findall(tg_text)
        assert "super_admin_bot" in tg_matches
        assert "channel_support" in tg_matches

    def test_affiliate_replacer_key_value_params(self):
        replacer = AffiliateReplacer()
        rules = {"amazon.com": "tag=partner2026", "aliexpress.com": "subid=999"}
        text = "Xarid qiling: https://www.amazon.com/dp/B000123 va https://aliexpress.com/item/456.html"
        result = replacer.replace_affiliate_links(text, rules)

        assert "tag=partner2026" in result
        assert "subid=999" in result
        assert "ref=tag%3Dpartner2026" not in result

    def test_ai_paraphraser_idempotency(self):
        service = AIParaphraserService()
        initial_text = "Yangi texnologiyalar taqdimoti bo'loo taqidimoti bo 'odti."

        hype1 = service.paraphrase(initial_text, mode="hype")
        hype2 = service.paraphrase(hype1, mode="hype")
        assert hype1 == hype2
        assert hype2.count("Batafsil ma'lumot yuqorida keltirilgan!") == 1

        short1 = service.paraphrase(initial_text, mode="short")
        short2 = service.paraphrase(short1, mode="short")
        assert short1 == short2

        formal1 = service.paraphrase(initial_text, mode="formal")
        formal2 = service.paraphrase(formal1, mode="formal")
        assert formal1 == formal2
        assert formal2.count("Rasmiy Axborot:") == 1

    def test_system_status_log_html_escaping_order(self):
        raw_lines = [f"2026-09-08 Line {i}: <error code='u00%'> & 'critical'" for i in range(100)]
        raw_text = "\n".join(raw_lines)
        if len(raw_text) > 3500:
            raw_text = raw_text[-3500:]
            if "\n" in raw_text:
                raw_text = raw_text.split("\n", 1)[1]
        log_text = html.escape(raw_text)

        assert "<error" not in log_text
        assert "&lt;error" in log_text
        assert "&amp;" in log_text
        assert not log_text.startswith("t;")
        assert not log_text.startswith("mp;")
