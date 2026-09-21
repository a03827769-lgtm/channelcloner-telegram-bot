from aiogram.fsm.state import State, StatesGroup

class StoryAuthSG(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()

class StorySettingsSG(StatesGroup):
    waiting_for_source_channel = State()
    waiting_for_add_channel = State()
    waiting_for_custom_price = State()
    waiting_for_target_channel = State()
    waiting_for_custom_daily_limit = State()
    waiting_for_video_duration = State()

