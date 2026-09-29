import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from database.models import ChannelPair
from services.text_processor import TextProcessor
from services.media_handler import MediaHandler
from services.telethon_listener import TelethonListener
from bot.bot_instance import create_dispatcher
from admin_bot.bot_instance import create_admin_dispatcher

class TestFixedIssuesE2E(unittest.IsolatedAsyncioTestCase):
    async def test_admin_and_main_dispatchers_coexistence(self):
        dp_main = create_dispatcher()
        dp_admin = create_admin_dispatcher()
        self.assertIsNotNone(dp_main)
        self.assertIsNotNone(dp_admin)
        self.assertGreater(len(dp_main.sub_routers), 0)
        self.assertGreater(len(dp_admin.sub_routers), 0)

    async def test_media_handler_detects_proper_extensions(self):
        mh = MediaHandler(temp_dir="temp_media")
        
        msg_photo = MagicMock()
        msg_photo.media = True
        msg_photo.photo = True
        msg_photo.video = False
        msg_photo.voice = False
        msg_photo.audio = False
        msg_photo.sticker = False
        msg_photo.gif = False
        msg_photo.document = None
        msg_photo.file = MagicMock()
        msg_photo.file.name = None
        msg_photo.file.ext = ".jpg"
        msg_photo.id = 12345
        msg_photo.download_media = AsyncMock(return_value="temp_media/test_12345.jpg")

        res = await mh.download_telethon_media(msg_photo)
        self.assertTrue(res.endswith(".jpg"))

    async def test_history_clone_groups_albums_correctly(self):
        tl = TelethonListener()
        tl.client = MagicMock()
        tl.client.is_connected = MagicMock(return_value=True)
        tl.client.is_user_authorized = AsyncMock(return_value=True)

        tl.resolve_entity = AsyncMock(return_value=MagicMock(id=999))

        # Content messages (no service action)
        m3 = MagicMock(id=300, grouped_id=None, action=None)
        m2_c = MagicMock(id=203, grouped_id=555, action=None)
        m2_b = MagicMock(id=202, grouped_id=555, action=None)
        m2_a = MagicMock(id=201, grouped_id=555, action=None)
        m1 = MagicMock(id=100, grouped_id=None, action=None)

        async def mock_iter_messages(*args, **kwargs):
            for msg in [m3, m2_c, m2_b, m2_a, m1]:
                yield msg

        tl.client.iter_messages = mock_iter_messages

        pair = ChannelPair(
            id=1,
            user_id=123,
            source_channel="@src",
            target_channel="@tgt"
        )

        progress_records = []
        async def mock_progress(cur, total, status):
            progress_records.append((cur, total, status))

        # The run re-reads the pair before every post (a paused or deleted pair stops it): it stays active here
        with patch("services.cloner_engine.cloner_engine.clone_single_message", AsyncMock(return_value=True)), \
             patch("services.cloner_engine.cloner_engine.clone_media_group", AsyncMock(return_value=True)), \
             patch.object(tl, "_current_pair", AsyncMock(return_value=pair)):
            
            result = await tl.clone_history(pair, limit=2, progress_callback=mock_progress)

            self.assertEqual(result["total"], 2)
            self.assertEqual(result["cloned"], 2)
            self.assertEqual(result["status"], "completed")

    def test_text_processor_preserves_content_and_prices(self):
        raw_text = "Orientir: Shoxsaroy\nNarxi: .00\nKanal: @cityjoyestateuz"
        cleaned = TextProcessor.clean_links_and_usernames(raw_text)
        self.assertIn(".00", cleaned)
        self.assertIn("Orientir: Shoxsaroy", cleaned)
        self.assertNotIn("@cityjoyestateuz", cleaned)

if __name__ == "__main__":
    unittest.main()