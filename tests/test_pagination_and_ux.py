import unittest
from aiogram.types import InlineKeyboardMarkup
from database.models import ChannelPair
from bot.keyboards.inline_buttons import get_pairs_list_keyboard


class TestPaginationAndUX(unittest.TestCase):
    def test_pagination_under_page_size(self):
        pairs = [
            ChannelPair(id=i, user_id=1, source_channel=f"@src_{i}", target_channel=f"@tgt_{i}")
            for i in range(1, 5)
        ]
        kb = get_pairs_list_keyboard(pairs, page=0, page_size=6)
        self.assertIsInstance(kb, InlineKeyboardMarkup)
        
        # 4 channel buttons + 1 action row = 5 rows
        self.assertEqual(len(kb.inline_keyboard), 5)
        
        # No pagination navigation buttons
        all_cb = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertNotIn("noop", all_cb)
        self.assertFalse(any("cloner_pairs_page_" in cb for cb in all_cb))

    def test_pagination_over_page_size_page_0(self):
        pairs = [
            ChannelPair(id=i, user_id=1, source_channel=f"@src_{i}", target_channel=f"@tgt_{i}")
            for i in range(1, 15)  # 14 pairs -> 3 pages with page_size=6
        ]
        kb = get_pairs_list_keyboard(pairs, page=0, page_size=6)
        
        # Page 0 has 6 items
        item_rows = kb.inline_keyboard[:6]
        self.assertEqual(len(item_rows), 6)
        self.assertEqual(item_rows[0][0].callback_data, "pair_view_1")
        self.assertEqual(item_rows[5][0].callback_data, "pair_view_6")
        
        # Next row is navigation
        nav_row = kb.inline_keyboard[6]
        callbacks = [btn.callback_data for btn in nav_row]
        self.assertIn("noop", callbacks)
        self.assertIn("cloner_pairs_page_1", callbacks)
        # No previous button on page 0
        self.assertNotIn("cloner_pairs_page_-1", callbacks)

    def test_pagination_over_page_size_page_1(self):
        pairs = [
            ChannelPair(id=i, user_id=1, source_channel=f"@src_{i}", target_channel=f"@tgt_{i}")
            for i in range(1, 15)  # 14 pairs
        ]
        kb = get_pairs_list_keyboard(pairs, page=1, page_size=6)
        
        # Page 1 has items 7 to 12
        item_rows = kb.inline_keyboard[:6]
        self.assertEqual(item_rows[0][0].callback_data, "pair_view_7")
        self.assertEqual(item_rows[5][0].callback_data, "pair_view_12")
        
        # Navigation has both Prev and Next
        nav_row = kb.inline_keyboard[6]
        callbacks = [btn.callback_data for btn in nav_row]
        self.assertIn("cloner_pairs_page_0", callbacks)
        self.assertIn("noop", callbacks)
        self.assertIn("cloner_pairs_page_2", callbacks)

    def test_pagination_last_page(self):
        pairs = [
            ChannelPair(id=i, user_id=1, source_channel=f"@src_{i}", target_channel=f"@tgt_{i}")
            for i in range(1, 15)  # 14 pairs: page 2 has items 13, 14 (2 items)
        ]
        kb = get_pairs_list_keyboard(pairs, page=2, page_size=6)
        
        item_rows = kb.inline_keyboard[:2]
        self.assertEqual(len(item_rows), 2)
        self.assertEqual(item_rows[0][0].callback_data, "pair_view_13")
        self.assertEqual(item_rows[1][0].callback_data, "pair_view_14")
        
        # Navigation has Prev, but no Next
        nav_row = kb.inline_keyboard[2]
        callbacks = [btn.callback_data for btn in nav_row]
        self.assertIn("cloner_pairs_page_1", callbacks)
        self.assertIn("noop", callbacks)
        self.assertFalse(any("cloner_pairs_page_3" in cb for cb in callbacks))
