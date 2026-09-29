import unittest
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from database.models import ChannelPair
from bot.keyboards.inline_buttons import (
    get_main_reply_keyboard,
    get_main_menu_keyboard,
    get_quickstart_keyboard,
    get_cloner_menu_keyboard,
    get_pair_detail_keyboard,
    get_translate_lang_keyboard,
    get_history_count_keyboard,
    get_video_watermark_keyboard,
    get_drip_feed_keyboard,
    get_ai_paraphrase_keyboard,
    get_backup_restore_keyboard,
    get_upgrade_prompt_keyboard,
)
from bot.keyboards.stars_keyboards import (
    get_stars_plans_keyboard,
)
from admin_bot.keyboards.admin_keyboards import (
    get_admin_dashboard_keyboard,
)
from services.custom_emojis import (
    ID_SUCCESS, ID_ERROR, ID_STARS, ID_CROWN, ID_FLASH,
    ID_ROCKET, ID_STATS, ID_TRASH, ID_FLAG_UZ, ID_REFRESH
)

class TestButtonEmojisAndStyles(unittest.TestCase):

    def test_main_reply_keyboard_has_emojis_and_styles(self):
        kb = get_main_reply_keyboard()
        self.assertIsInstance(kb, ReplyKeyboardMarkup)
        flat_buttons = [btn for row in kb.keyboard for btn in row]
        
        # Verify custom emojis and styles are present
        has_emoji = any(btn.icon_custom_emoji_id for btn in flat_buttons)
        has_style = any(btn.style for btn in flat_buttons)
        self.assertTrue(has_emoji, "Main reply keyboard should have icon_custom_emoji_id")
        self.assertTrue(has_style, "Main reply keyboard should have style")
        
        # Verify text keywords remain intact for handler compatibility
        texts = [btn.text for btn in flat_buttons]
        self.assertTrue(any("Kanal Kloner" in t for t in texts))
        self.assertTrue(any("Yangi Kanal" in t for t in texts))
        self.assertTrue(any("Tariflar" in t for t in texts))
        self.assertTrue(any("Statistika" in t for t in texts))
        self.assertTrue(any("Qo'llanma" in t for t in texts))

    def test_main_menu_inline_keyboard(self):
        kb = get_main_menu_keyboard()
        self.assertIsInstance(kb, InlineKeyboardMarkup)
        flat_buttons = [btn for row in kb.inline_keyboard for btn in row]
        
        # Verify icon_custom_emoji_id on buttons
        emojis = [btn.icon_custom_emoji_id for btn in flat_buttons if btn.icon_custom_emoji_id]
        self.assertIn(ID_ROCKET, emojis)
        self.assertIn(ID_FLASH, emojis)
        self.assertIn(ID_CROWN, emojis)
        self.assertIn(ID_STATS, emojis)

    def test_pair_detail_keyboard_dynamic_styles(self):
        # Active pair
        active_pair = ChannelPair(
            id=1, user_id=100, source_channel="-1001", target_channel="-1002",
            is_active=True, clean_links=True, auto_translate=True, target_lang="uz",
            image_watermark_type="text", video_watermark_type="text",
            drip_delay_minutes=5, ai_paraphrase_mode="formal", auto_cta_buttons=True,
            auto_premium_emojis=True, is_protected_source=True, backup_enabled=True
        )
        kb_active = get_pair_detail_keyboard(active_pair)
        flat_active = [btn for row in kb_active.inline_keyboard for btn in row]
        
        # Status button should be danger (To'xtatish)
        status_btn = flat_active[0]
        self.assertEqual(status_btn.style, "danger")
        self.assertEqual(status_btn.icon_custom_emoji_id, ID_ERROR)
        
        # Delete button should be danger (O'chirish)
        delete_btn = [btn for btn in flat_active if "O'chirish" in btn.text][0]
        self.assertEqual(delete_btn.style, "danger")
        self.assertEqual(delete_btn.icon_custom_emoji_id, ID_TRASH)

        # Inactive pair
        inactive_pair = ChannelPair(
            id=2, user_id=100, source_channel="-1001", target_channel="-1002",
            is_active=False
        )
        kb_inactive = get_pair_detail_keyboard(inactive_pair)
        status_btn_inactive = kb_inactive.inline_keyboard[0][0]
        self.assertEqual(status_btn_inactive.style, "success")
        self.assertEqual(status_btn_inactive.icon_custom_emoji_id, ID_SUCCESS)

    def test_translate_lang_keyboard_flags_and_styles(self):
        kb = get_translate_lang_keyboard(pair_id=10)
        flat_buttons = [btn for row in kb.inline_keyboard for btn in row]
        
        uz_btn = [btn for btn in flat_buttons if "O'zbekcha" in btn.text][0]
        self.assertEqual(uz_btn.icon_custom_emoji_id, ID_FLAG_UZ)
        self.assertEqual(uz_btn.style, "primary")
        
        off_btn = [btn for btn in flat_buttons if "Tarjimani O'chirish" in btn.text][0]
        self.assertEqual(off_btn.style, "danger")
        self.assertEqual(off_btn.icon_custom_emoji_id, ID_ERROR)

    def test_stars_plans_keyboard(self):
        from database.models import Subscription
        sub = Subscription(user_id=100, tier="free", expires_at="2099-01-01")
        kb = get_stars_plans_keyboard(sub)
        flat_buttons = [btn for row in kb.inline_keyboard for btn in row]
        
        pro_btn = [btn for btn in flat_buttons if "Pro" in btn.text][0]
        self.assertEqual(pro_btn.icon_custom_emoji_id, ID_STARS)
        self.assertEqual(pro_btn.style, "primary")
        
        vip_btn = [btn for btn in flat_buttons if "VIP" in btn.text][0]
        self.assertEqual(vip_btn.icon_custom_emoji_id, ID_CROWN)
        self.assertEqual(vip_btn.style, "success")

    def test_admin_dashboard_keyboard_styles(self):
        # When authenticated
        kb_auth = get_admin_dashboard_keyboard(is_auth=True)
        auth_btn = [btn for row in kb_auth.inline_keyboard for btn in row if "MTProto" in btn.text][0]
        self.assertEqual(auth_btn.style, "success")
        self.assertEqual(auth_btn.icon_custom_emoji_id, ID_SUCCESS)
        
        # When unauthenticated
        kb_unauth = get_admin_dashboard_keyboard(is_auth=False)
        unauth_btn = [btn for row in kb_unauth.inline_keyboard for btn in row if "MTProto" in btn.text][0]
        self.assertEqual(unauth_btn.style, "danger")
        self.assertEqual(unauth_btn.icon_custom_emoji_id, ID_ERROR)

        # Restart listener button
        restart_btn = [btn for row in kb_auth.inline_keyboard for btn in row if "Tinglovchini Qayta" in btn.text][0]
        self.assertEqual(restart_btn.style, "danger")
        self.assertEqual(restart_btn.icon_custom_emoji_id, ID_REFRESH)

    def test_button_structure_and_styling(self):
        """Verify buttons maintain valid structure, styles, and non-empty text across menus."""
        mock_pair = ChannelPair(
            id=1, user_id=100, source_channel="-1001", target_channel="-1002",
            is_active=True, video_watermark_type="text", video_watermark_text="Watermark",
            video_watermark_pos="bottom_right"
        )

        keyboards_to_test = [
            get_main_reply_keyboard(),
            get_main_menu_keyboard(),
            get_quickstart_keyboard(),
            get_cloner_menu_keyboard(),
            get_history_count_keyboard(pair_id=1),
            get_video_watermark_keyboard(pair_id=1, pair=mock_pair),
            get_drip_feed_keyboard(pair_id=1, pair=mock_pair),
            get_ai_paraphrase_keyboard(pair_id=1, pair=mock_pair),
            get_backup_restore_keyboard(pair_id=1, count=10, pair=mock_pair),
            get_upgrade_prompt_keyboard(pair_id=1),
        ]

        for kb in keyboards_to_test:
            if isinstance(kb, ReplyKeyboardMarkup):
                buttons = [btn for row in kb.keyboard for btn in row]
            else:
                buttons = [btn for row in kb.inline_keyboard for btn in row]
            
            for btn in buttons:
                self.assertTrue(len(btn.text.strip()) > 0, "Button text must not be empty")
                self.assertTrue(btn.style or btn.icon_custom_emoji_id or len(btn.text) > 0)

    def test_no_double_emojis_in_button_texts(self):
        """Verify buttons with icon_custom_emoji_id do not include leading static emojis in text."""
        emoji_chars = ["⚡", "🚀", "👑", "📊", "📖", "➕", "⚙️", "⚙", "🗑️", "🗑", "🔙", "⬅️", "✅", "❌", "💎", "⭐", "🔒", "🔄", "🎵", "➖"]
        
        mock_pair = ChannelPair(
            id=1, user_id=100, source_channel="-1001", target_channel="-1002",
            is_active=True, video_watermark_type="text", video_watermark_text="Watermark",
            video_watermark_pos="bottom_right"
        )

        keyboards_to_test = [
            get_main_reply_keyboard(),
            get_main_menu_keyboard(),
            get_quickstart_keyboard(),
            get_cloner_menu_keyboard(),
            get_history_count_keyboard(pair_id=1),
            get_video_watermark_keyboard(pair_id=1, pair=mock_pair),
            get_drip_feed_keyboard(pair_id=1, pair=mock_pair),
            get_ai_paraphrase_keyboard(pair_id=1, pair=mock_pair),
            get_backup_restore_keyboard(pair_id=1, count=10, pair=mock_pair),
            get_upgrade_prompt_keyboard(pair_id=1),
        ]

        for kb in keyboards_to_test:
            if isinstance(kb, ReplyKeyboardMarkup):
                buttons = [btn for row in kb.keyboard for btn in row]
            else:
                buttons = [btn for row in kb.inline_keyboard for btn in row]
            
            for btn in buttons:
                if btn.icon_custom_emoji_id:
                    for char in emoji_chars:
                        self.assertFalse(
                            btn.text.startswith(char),
                            f"Button '{btn.text}' starts with static emoji '{char}' while icon_custom_emoji_id is set!"
                        )

    def test_dynamic_affiliate_cta_keyboard_has_custom_emoji_and_style(self):
        """Verify affiliate engine CTA buttons have style and icon_custom_emoji_id."""
        from services.dynamic_affiliate_engine import dynamic_affiliate_engine
        links = [("Uzum'da ko'rish", "https://uzum.uz/p/123", "5438630043818302484")]
        kb = dynamic_affiliate_engine.build_cta_keyboard(links)
        self.assertIsNotNone(kb)
        btn = kb.inline_keyboard[0][0]
        self.assertEqual(btn.style, "primary")
        self.assertEqual(btn.icon_custom_emoji_id, "5438630043818302484")
        self.assertEqual(btn.text, "Uzum'da ko'rish")

if __name__ == "__main__":
    unittest.main()
