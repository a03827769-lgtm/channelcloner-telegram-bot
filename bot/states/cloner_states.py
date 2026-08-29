from aiogram.fsm.state import State, StatesGroup

class AddChannelPairSG(StatesGroup):
    waiting_for_source_channel = State()
    waiting_for_target_channel = State()
    waiting_for_custom_signature = State()

class EditSettingsSG(StatesGroup):
    waiting_for_signature = State()
    waiting_for_blacklist = State()
    waiting_for_replacements = State()
    waiting_for_wm_text = State()
    waiting_for_vwm_text = State()
    waiting_for_affiliate_rules = State()
    waiting_for_restore_target = State()

class HistoryCloneSG(StatesGroup):
    waiting_for_count = State()

class AuthSG(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()

class AdminSG(StatesGroup):
    waiting_for_broadcast_text = State()
