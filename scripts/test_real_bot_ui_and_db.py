import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from database.db_manager import db_manager
from database.models import StorySettings
from bot.handlers.story_menu import (
    cb_story_set_duration,
    cb_story_menu_duration,
    process_custom_video_duration_input
)
from bot.states.story_states import StorySettingsSG


async def run_bot_ui_and_db_tests():
    print("=" * 60)
    print("RUNNING REAL BOT UI & DB HANDLER TESTS")
    print("=" * 60)

    import time
    test_uid = int(time.time())
    await db_manager.init_db()

    # 1. Verify default in DB is 25s
    st = await db_manager.get_story_settings(test_uid)
    print(f"[DB] Initial video_duration for user {test_uid}: {st.video_duration}s (Expected: 25s)")
    assert st.video_duration == 25, "Default must be 25s"

    # 2. Test cb_story_set_duration handler (preset 15s)
    mock_cb_15 = MagicMock()
    mock_cb_15.from_user.id = test_uid
    mock_cb_15.data = "story_set_duration_15"
    mock_cb_15.answer = AsyncMock()
    mock_cb_15.message = MagicMock()
    mock_cb_15.message.edit_reply_markup = AsyncMock()

    await cb_story_set_duration(mock_cb_15)
    st_after_15 = await db_manager.get_story_settings(test_uid)
    print(f"[UI Handler] Set duration 15s -> DB value: {st_after_15.video_duration}s")
    assert st_after_15.video_duration == 15

    # 3. Test cb_story_set_duration handler (preset 40s)
    mock_cb_40 = MagicMock()
    mock_cb_40.from_user.id = test_uid
    mock_cb_40.data = "story_set_duration_40"
    mock_cb_40.answer = AsyncMock()
    mock_cb_40.message = MagicMock()
    mock_cb_40.message.edit_reply_markup = AsyncMock()

    await cb_story_set_duration(mock_cb_40)
    st_after_40 = await db_manager.get_story_settings(test_uid)
    print(f"[UI Handler] Set duration 40s -> DB value: {st_after_40.video_duration}s")
    assert st_after_40.video_duration == 40

    # 4. Test custom input validation: Reject < 15
    mock_state = MagicMock()
    mock_state.clear = AsyncMock()

    mock_msg_under = MagicMock()
    mock_msg_under.from_user.id = test_uid
    mock_msg_under.text = "10"
    mock_msg_under.answer = AsyncMock()

    await process_custom_video_duration_input(mock_msg_under, mock_state)
    mock_msg_under.answer.assert_called_once()
    err_text_under = mock_msg_under.answer.call_args[0][0]
    print(f"[Validation Test <15s] Input: '10' -> Bot Response: {err_text_under[:60]}...")
    assert "minimum 15 sekund" in err_text_under
    mock_state.clear.assert_not_called()  # State should NOT clear on error

    # 5. Test custom input validation: Reject > 40
    mock_msg_over = MagicMock()
    mock_msg_over.from_user.id = test_uid
    mock_msg_over.text = "45"
    mock_msg_over.answer = AsyncMock()

    await process_custom_video_duration_input(mock_msg_over, mock_state)
    err_text_over = mock_msg_over.answer.call_args[0][0]
    print(f"[Validation Test >40s] Input: '45' -> Bot Response: {err_text_over[:60]}...")
    assert "maximum 40 sekund" in err_text_over
    mock_state.clear.assert_not_called()

    # 6. Test custom input validation: Accept valid 32s
    mock_msg_valid = MagicMock()
    mock_msg_valid.from_user.id = test_uid
    mock_msg_valid.text = "32"
    mock_msg_valid.answer = AsyncMock()

    await process_custom_video_duration_input(mock_msg_valid, mock_state)
    st_after_custom = await db_manager.get_story_settings(test_uid)
    print(f"[Validation Test Valid] Input: '32' -> DB value: {st_after_custom.video_duration}s")
    assert st_after_custom.video_duration == 32
    mock_state.clear.assert_called_once()

    print("\n[PASS] ALL REAL BOT UI & DB HANDLER TESTS PASSED 100%!")


if __name__ == "__main__":
    asyncio.run(run_bot_ui_and_db_tests())
