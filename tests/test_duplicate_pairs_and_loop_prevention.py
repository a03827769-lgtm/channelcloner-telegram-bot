import unittest
import os
import uuid
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from aiogram.types import Message, Chat, User as AiogramUser
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from database.db_manager import DatabaseManager
from database.models import ChannelPair
from services.cloner_engine import ClonerEngine
from services.custom_emojis import WARN, LINK, SUCCESS
from bot.handlers.cloner_menu import process_target_channel
from bot.states.cloner_states import AddChannelPairSG

TEST_DB_PATH = "database/test_duplicate_and_loops.db"

class TestDuplicatePairsAndLoopPrevention(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = f"database/test_duplicate_{uuid.uuid4().hex[:8]}.db"
        self.db = DatabaseManager(self.test_db_path)
        await self.db.init_db()
        self.storage = MemoryStorage()

    async def asyncTearDown(self):
        from tests.test_utils import safe_cleanup_db
        await safe_cleanup_db(self.test_db_path, self.db)

    def _create_mock_message(self, user_id: int, text: str = "") -> Message:
        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock(spec=AiogramUser)
        msg.from_user.id = user_id
        msg.from_user.full_name = "Test User"
        msg.from_user.username = "testuser"
        msg.chat = MagicMock(spec=Chat)
        msg.chat.id = user_id
        msg.text = text
        msg.forward_from_chat = None
        msg.answer = AsyncMock()
        return msg

    async def test_1_channel_name_normalization(self):
        """Verify _normalize_channel_name strips https, t.me, @, spaces, and case"""
        self.assertEqual(DatabaseManager._normalize_channel_name("@MyChannel"), "mychannel")
        self.assertEqual(DatabaseManager._normalize_channel_name("https://t.me/MyChannel"), "mychannel")
        self.assertEqual(DatabaseManager._normalize_channel_name("http://t.me/MyChannel/"), "mychannel/")
        self.assertEqual(DatabaseManager._normalize_channel_name("t.me/mychannel"), "mychannel")
        self.assertEqual(DatabaseManager._normalize_channel_name("  @My_Channel  "), "my_channel")
        self.assertEqual(DatabaseManager._normalize_channel_name(""), "")
        self.assertEqual(DatabaseManager._normalize_channel_name(None), "")

    async def test_2_find_duplicate_pair_and_idempotent_add(self):
        """Verify find_duplicate_pair finds existing pairs and add_channel_pair does not insert duplicates"""
        user_id = 10001
        await self.db.get_or_create_user(user_id, "User 1", "user1")

        # 1. Add initial pair
        pair_1 = await self.db.add_channel_pair(
            user_id=user_id,
            source_channel="@SourceChannel",
            source_title="Source Channel",
            target_channel="@TargetChannel",
            target_title="Target Channel",
            source_id=-100111111,
            target_id=-100222222
        )
        self.assertIsNotNone(pair_1)

        # 2. Add exact duplicate with different URL formatting
        pair_dup_1 = await self.db.add_channel_pair(
            user_id=user_id,
            source_channel="https://t.me/sourcechannel",
            source_title="Source Channel",
            target_channel="https://t.me/targetchannel",
            target_title="Target Channel"
        )
        self.assertEqual(pair_1, pair_dup_1, "add_channel_pair must return existing pair ID instead of inserting duplicate")

        # 3. Add duplicate matching by numeric chat ID
        pair_dup_2 = await self.db.add_channel_pair(
            user_id=user_id,
            source_channel="random_name",
            source_title="Source Title",
            target_channel="random_target",
            target_title="Target Title",
            source_id=-100111111,
            target_id=-100222222
        )
        self.assertEqual(pair_1, pair_dup_2, "add_channel_pair must match existing pair by numeric IDs")

        # 4. Ensure user still only has 1 pair in database
        pairs = await self.db.get_user_channel_pairs(user_id)
        self.assertEqual(len(pairs), 1)

    async def test_3_deduplicate_existing_pairs_on_startup(self):
        """Verify _deduplicate_existing_pairs cleans up existing redundant pairs and self-loops"""
        user_id = 10002
        await self.db.get_or_create_user(user_id, "User 2", "user2")

        # Manually force-insert 8 identical pairs (replicating the user's issue before the fix)
        async with self.db.get_connection() as conn:
            for i in range(8):
                await conn.execute("""
                    INSERT INTO channel_pairs (user_id, source_channel, source_title, target_channel, target_title, is_active)
                    VALUES (?, '@ArendaKvartir', 'Аренда квартир', '@aRieltorUz', 'aRieltor Uz', 1)
                """, (user_id,))
            # Also insert an invalid self-loop pair
            await conn.execute("""
                INSERT INTO channel_pairs (user_id, source_channel, source_title, target_channel, target_title, is_active)
                VALUES (?, '@SameChannel', 'Same Channel', '@SameChannel', 'Same Channel', 1)
            """, (user_id,))
            await conn.commit()

        # Check raw count before cleanup
        async with self.db.get_connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM channel_pairs WHERE user_id = ?", (user_id,))
            count_before = (await cur.fetchone())[0]
        self.assertEqual(count_before, 9)

        # Run deduplication
        async with self.db.get_connection() as conn:
            await self.db._deduplicate_existing_pairs(conn)

        # Check count after cleanup: 8 duplicates -> 1 canonical, self-loop -> 0. Total = 1!
        pairs_after = await self.db.get_user_channel_pairs(user_id)
        self.assertEqual(len(pairs_after), 1)
        self.assertEqual(pairs_after[0].source_channel, "@ArendaKvartir")
        self.assertEqual(pairs_after[0].target_channel, "@aRieltorUz")

    async def test_4_wizard_rejects_self_cloning_loop(self):
        """Verify process_target_channel blocks setting source == target"""
        user_id = 10003
        msg = self._create_mock_message(user_id=user_id, text="@SameChannel")
        mock_bot = MagicMock()
        mock_bot.id = 999999

        chat_mock = MagicMock()
        chat_mock.id = -100555555
        chat_mock.title = "Same Channel"
        mock_bot.get_chat = AsyncMock(return_value=chat_mock)

        member_mock = MagicMock()
        member_mock.status = "administrator"
        mock_bot.get_chat_member = AsyncMock(return_value=member_mock)

        from aiogram.fsm.storage.base import StorageKey
        fsm_key = StorageKey(bot_id=mock_bot.id, chat_id=user_id, user_id=user_id)
        state = FSMContext(storage=self.storage, key=fsm_key)
        await state.set_state(AddChannelPairSG.waiting_for_target_channel)
        await state.update_data(source_channel="@SameChannel", source_title="Same Channel", source_id=-100555555)

        with patch("bot.handlers.cloner_menu.db_manager", self.db):
            await process_target_channel(msg, state, mock_bot)

        # Verify bot responded with error warning
        self.assertTrue(msg.answer.called)
        sent_text = msg.answer.call_args.kwargs.get("text") or (msg.answer.call_args.args[0] if msg.answer.call_args.args else "")
        self.assertIn("Xatolik:</b> Manba va Maqsad kanali bir xil bo'lishi mumkin emas!", sent_text)
        self.assertIn(WARN, sent_text)

    async def test_5_wizard_rejects_duplicate_pair_and_provides_manage_button(self):
        """Verify process_target_channel blocks duplicate pair and presents inline button to existing pair"""
        user_id = 10004
        await self.db.get_or_create_user(user_id, "User 4", "user4")
        existing_pair_id = await self.db.add_channel_pair(
            user_id=user_id,
            source_channel="@Source4",
            source_title="Source 4",
            target_channel="@Target4",
            target_title="Target 4",
            source_id=-100777777,
            target_id=-100888888
        )

        msg = self._create_mock_message(user_id=user_id, text="@Target4")
        mock_bot = MagicMock()
        mock_bot.id = 999999

        chat_mock = MagicMock()
        chat_mock.id = -100888888
        chat_mock.title = "Target 4"
        mock_bot.get_chat = AsyncMock(return_value=chat_mock)

        member_mock = MagicMock()
        member_mock.status = "administrator"
        mock_bot.get_chat_member = AsyncMock(return_value=member_mock)

        from aiogram.fsm.storage.base import StorageKey
        fsm_key = StorageKey(bot_id=mock_bot.id, chat_id=user_id, user_id=user_id)
        state = FSMContext(storage=self.storage, key=fsm_key)
        await state.set_state(AddChannelPairSG.waiting_for_target_channel)
        await state.update_data(source_channel="@Source4", source_title="Source 4", source_id=-100777777)

        with patch("bot.handlers.cloner_menu.db_manager", self.db):
            await process_target_channel(msg, state, mock_bot)

        # Verify bot warned about duplicate and gave direct link to manage
        self.assertTrue(msg.answer.called)
        sent_text = msg.answer.call_args.kwargs.get("text") or (msg.answer.call_args.args[0] if msg.answer.call_args.args else "")
        self.assertIn("Ushbu kanal juftligi allaqachon mavjud!", sent_text)
        self.assertIn(f"#{existing_pair_id}", sent_text)
        reply_markup = msg.answer.call_args.kwargs.get("reply_markup")
        button_callbacks = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
        self.assertIn(f"pair_view_{existing_pair_id}", button_callbacks)

    async def test_6_cloner_engine_aborts_on_self_loop(self):
        """Verify ClonerEngine refuses to clone messages if source == target"""
        mock_bot = MagicMock()
        engine = ClonerEngine(mock_bot)

        loop_pair = ChannelPair(
            id=99,
            user_id=123,
            source_channel="@InfiniteLoop",
            target_channel="@InfiniteLoop",
            source_id=-100999,
            target_id=-100999
        )

        mock_msg = MagicMock()
        mock_msg.id = 1
        mock_msg.message = "Test loop"

        with patch("services.cloner_engine.db_manager", self.db):
            result_single = await engine.clone_single_message(mock_msg, loop_pair)
            self.assertFalse(result_single)

            result_media = await engine.clone_media_group([mock_msg], loop_pair)
            self.assertFalse(result_media)
