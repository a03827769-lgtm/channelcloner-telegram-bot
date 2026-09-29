from aiogram import F

from bot.keyboards.inline_buttons import MAIN_REPLY_MENU_LABELS
from .admin_filter import IsAdminFilter, is_admin_user

# Free-form wizard input: never a command and never a persistent reply-keyboard button, so tapping a
# menu button (or sending /help) in the middle of a wizard navigates instead of being consumed as the
# wizard's answer. Messages without text (forwards of media, photos) always pass.
WIZARD_INPUT = ~F.text.startswith("/") & ~F.text.in_(MAIN_REPLY_MENU_LABELS)

# Settings editors additionally accept the "/clear" keyword
SETTINGS_INPUT = (~F.text.startswith("/") | (F.text == "/clear")) & ~F.text.in_(MAIN_REPLY_MENU_LABELS)

__all__ = ["IsAdminFilter", "is_admin_user", "WIZARD_INPUT", "SETTINGS_INPUT"]
