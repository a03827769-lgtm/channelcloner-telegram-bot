import unittest
import os
import tempfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from database.db_manager import DatabaseManager
from database.models import ChannelPair
from services.telethon_listener import TelethonListener

class TestOfflineCatchup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.test_db_path = os.path.join(self._tmp_dir.name, f"test_catchup_{uuid.uuid4().hex[:8]}.db")
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()

        self.listener = TelethonListener()

    async def asyncTearDown(self):
        from tests.test_utils import safe_cleanup_db
        await safe_cleanup_db(self.test_db_path, self.db)
        self._tmp_dir.cleanup()

    async def test_channel_pair_catchup_defaults_and_toggle(self):
        """Tests that auto_catchup defaults to True and can be toggled in DB"""
        await self.db.get_or_create_user(1001, "Test User", "testuser")
        pair_id = await self.db.add_channel_pair(
            user_id=1001,
            source_channel="@src_chan",
            source_title="Source Chan",
            target_channel="@tgt_chan",
            target_title="Target Chan"
        )
        self.assertIsNotNone(pair_id)

        pair = await self.db.get_pair_by_id(pair_id)
        self.assertTrue(pair.auto_catchup)
        self.assertIsNone(pair.last_seen_msg_id)

        # Toggle OFF
        new_val = await self.db.toggle_auto_catchup(pair_id)
        self.assertFalse(new_val)
        pair = await self.db.get_pair_by_id(pair_id)
        self.assertFalse(pair.auto_catchup)

        # Toggle ON
        new_val = await self.db.toggle_auto_catchup(pair_id)
        self.assertTrue(new_val)
        pair = await self.db.get_pair_by_id(pair_id)
        self.assertTrue(pair.auto_catchup)

    async def test_last_seen_msg_id_tracking(self):
        """Tests get_effective_last_source_msg_id and monotonic advance in DB"""
        await self.db.get_or_create_user(1002, "User 2", "user2")
        pair_id = await self.db.add_channel_pair(
            user_id=1002,
            source_channel="@src_track",
            source_title="Source Track",
            target_channel="@tgt_track",
            target_title="Target Track"
        )

        # Initially None
        last_id = await self.db.get_effective_last_source_msg_id(pair_id)
        self.assertIsNone(last_id)

        # Update manually
        await self.db.update_pair_last_seen_msg_id(pair_id, 200)
        last_id = await self.db.get_effective_last_source_msg_id(pair_id)
        self.assertEqual(last_id, 200)

        # Record cloned message with higher ID -> MAX updates last_seen_msg_id
        await self.db.record_cloned_message(pair_id, 250, target_msg_id=999)
        last_id = await self.db.get_effective_last_source_msg_id(pair_id)
        self.assertEqual(last_id, 250)

        # Record cloned message with lower ID -> last_seen_msg_id does not regress
        await self.db.record_cloned_message(pair_id, 220, target_msg_id=998)
        last_id = await self.db.get_effective_last_source_msg_id(pair_id)
        self.assertEqual(last_id, 250)

    async def test_catchup_disconnected_client(self):
        """Catch-up exits cleanly with status not_connected when client is offline"""
        pair = ChannelPair(
            id=1,
            user_id=1001,
            source_channel="@src",
            target_channel="@tgt",
            is_active=True,
            auto_catchup=True
        )
        res = await self.listener.catch_up_pair_messages(pair)
        self.assertEqual(res["status"], "not_connected")
        self.assertEqual(res["caught_up"], 0)

    async def test_catchup_disabled_pair(self):
        """Catch-up returns status disabled when auto_catchup is False"""
        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        self.listener.client = mock_client

        pair = ChannelPair(
            id=1,
            user_id=1001,
            source_channel="@src",
            target_channel="@tgt",
            is_active=True,
            auto_catchup=False
        )
        res = await self.listener.catch_up_pair_messages(pair)
        self.assertEqual(res["status"], "disabled")
        self.assertEqual(res["caught_up"], 0)

    async def test_catchup_baseline_establishment(self):
        """When pair has never been cloned, catch-up sets baseline without dumping old history"""
        await self.db.get_or_create_user(1003, "User 3", "user3")
        pair_id = await self.db.add_channel_pair(
            user_id=1003,
            source_channel="@src_new",
            source_title="Source New",
            target_channel="@tgt_new",
            target_title="Target New"
        )
        pair = await self.db.get_pair_by_id(pair_id)

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=True)

        mock_head = MagicMock()
        mock_head.id = 850
        mock_client.get_messages = AsyncMock(return_value=[mock_head])

        with patch("services.telethon_listener.db_manager", self.db), \
             patch.object(self.listener, "resolve_entity", AsyncMock(return_value=MagicMock())):
            self.listener.client = mock_client
            res = await self.listener.catch_up_pair_messages(pair)

            self.assertEqual(res["status"], "baseline_established")
            self.assertEqual(res["last_id"], 850)

            # DB should now have last_seen_msg_id = 850
            updated_pair = await self.db.get_pair_by_id(pair_id)
            self.assertEqual(updated_pair.last_seen_msg_id, 850)

    async def test_catchup_up_to_date(self):
        """When channel head equals last_seen_msg_id, status is up_to_date"""
        await self.db.get_or_create_user(1004, "User 4", "user4")
        pair_id = await self.db.add_channel_pair(
            user_id=1004,
            source_channel="@src_uptodate",
            source_title="Source UpToDate",
            target_channel="@tgt_uptodate",
            target_title="Target UpToDate"
        )
        await self.db.update_pair_last_seen_msg_id(pair_id, 900)
        pair = await self.db.get_pair_by_id(pair_id)

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=True)

        mock_head = MagicMock()
        mock_head.id = 900
        mock_client.get_messages = AsyncMock(return_value=[mock_head])

        with patch("services.telethon_listener.db_manager", self.db), \
             patch.object(self.listener, "resolve_entity", AsyncMock(return_value=MagicMock())):
            self.listener.client = mock_client
            res = await self.listener.catch_up_pair_messages(pair)

            self.assertEqual(res["status"], "up_to_date")
            self.assertEqual(res["caught_up"], 0)
            self.assertEqual(res["last_id"], 900)

    async def test_catchup_retrieves_gap_and_handles_albums(self):
        """Detects gap, processes album and single messages in chronological order, deduplicates"""
        await self.db.get_or_create_user(1005, "User 5", "user5")
        pair_id = await self.db.add_channel_pair(
            user_id=1005,
            source_channel="@src_gap",
            source_title="Source Gap",
            target_channel="@tgt_gap",
            target_title="Target Gap"
        )
        await self.db.update_pair_last_seen_msg_id(pair_id, 100)
        await self.db.record_cloned_message(pair_id, 101)

        pair = await self.db.get_pair_by_id(pair_id)

        msg101 = MagicMock(id=101, grouped_id=None, text="already cloned", media=None, action=None)
        msg102 = MagicMock(id=102, grouped_id=777, text="album photo 1", media=MagicMock(), action=None)
        msg103 = MagicMock(id=103, grouped_id=777, text="album photo 2", media=MagicMock(), action=None)
        msg104 = MagicMock(id=104, grouped_id=None, text="single news post", media=None, action=None)

        missed_msgs = [msg101, msg102, msg103, msg104]

        async def mock_iter_messages(entity, min_id=None, limit=None, reverse=True):
            for m in missed_msgs:
                if min_id is None or m.id > min_id:
                    yield m

        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=True)

        mock_head = MagicMock(id=104)
        mock_client.get_messages = AsyncMock(return_value=[mock_head])
        mock_client.iter_messages = mock_iter_messages

        with patch("services.telethon_listener.db_manager", self.db), \
             patch("services.telethon_listener.cloner_engine.clone_media_group", AsyncMock(return_value=True)) as mock_clone_album, \
             patch("services.telethon_listener.cloner_engine.clone_single_message", AsyncMock(return_value=True)) as mock_clone_single, \
             patch.object(self.listener, "resolve_entity", AsyncMock(return_value=MagicMock())):

            self.listener.client = mock_client
            res = await self.listener.catch_up_pair_messages(pair)

            self.assertEqual(res["status"], "completed")
            self.assertEqual(res["caught_up"], 3)
            self.assertEqual(res["failed"], 0)
            self.assertEqual(res["last_id"], 104)

            self.assertEqual(mock_clone_album.call_count, 1)
            album_arg = mock_clone_album.call_args[0][0]
            self.assertEqual([m.id for m in album_arg], [102, 103])

            self.assertEqual(mock_clone_single.call_count, 1)
            single_arg = mock_clone_single.call_args[0][0]
            self.assertEqual(single_arg.id, 104)

            updated_pair = await self.db.get_pair_by_id(pair_id)
            self.assertEqual(updated_pair.last_seen_msg_id, 104)

    async def test_catch_up_all_active_pairs(self):
        """Runs catch-up across active pairs with auto_catchup=True, skipping disabled pairs"""
        await self.db.get_or_create_user(1006, "User 6", "user6")
        p1_id = await self.db.add_channel_pair(user_id=1006, source_channel="@src_p1", source_title="P1", target_channel="@tgt_p1", target_title="P1")
        p2_id = await self.db.add_channel_pair(user_id=1006, source_channel="@src_p2", source_title="P2", target_channel="@tgt_p2", target_title="P2")
        await self.db.toggle_auto_catchup(p2_id)

        mock_catchup_pair = AsyncMock(return_value={"status": "up_to_date", "caught_up": 0})

        with patch("services.telethon_listener.db_manager", self.db), \
             patch.object(self.listener, "is_connected", return_value=True), \
             patch.object(self.listener, "catch_up_pair_messages", mock_catchup_pair):

            results = await self.listener.catch_up_all_active_pairs()
            self.assertIn(p1_id, results)
            self.assertNotIn(p2_id, results)
            self.assertEqual(mock_catchup_pair.call_count, 1)

if __name__ == "__main__":
    unittest.main()
