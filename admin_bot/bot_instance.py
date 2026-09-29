import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import SimpleEventIsolation
from database.fsm_storage import SQLiteStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from config.settings import settings
from admin_bot.middlewares.admin_auth_middleware import AdminStrictAuthMiddleware
from admin_bot.keyboards.admin_keyboards import (
    ADMIN_REPLY_MENU_LABELS, ADMIN_MENU_DASHBOARD, ADMIN_MENU_STATUS, ADMIN_MENU_MTPROTO,
    ADMIN_MENU_BROADCAST, ADMIN_MENU_USERS, ADMIN_MENU_BACKUP
)

from admin_bot.handlers.dashboard import (
    cmd_admin_start, cb_admin_dashboard, cb_restart_listener,
    cmd_admin_catchup, cmd_admin_help, cmd_admin_cancel, cb_admin_noop
)
from admin_bot.handlers.mtproto_auth import (
    cb_auth_status, cb_start_phone, process_phone_input,
    process_code_input, process_2fa_input, cb_logout_confirm, cb_logout_yes,
    AuthStates
)
from admin_bot.handlers.system_status import (
    cb_admin_system_status, cb_admin_view_logs, cmd_check_origin
)
from admin_bot.handlers.broadcast import (
    cb_broadcast_prompt, process_broadcast_message, cb_broadcast_confirm,
    cb_broadcast_cancel, cb_broadcast_stop, BroadcastStates
)
from admin_bot.handlers.backup import (
    cb_download_backup
)
from admin_bot.handlers.user_management import (
    cb_users_list, cb_start_user_search, process_user_search_query,
    cb_user_detail, cb_grant_tier, cb_revoke_tier, cb_revoke_tier_confirm,
    cb_grant_admin_privilege, cb_revoke_admin_privilege, AdminUserSG
)
from admin_bot.handlers.access_control import (
    cb_admin_bot_mode, cb_toggle_bot_mode, cb_set_support_user,
    process_support_user_input, cb_whitelist_list, cb_whitelist_stars,
    cb_whitelist_page, cb_whitelist_revoke, cb_whitelist_add,
    process_whitelist_user_input, AccessControlStates
)
from bot.filters.admin_filter import IsAdminFilter
from bot.bot_instance import ResilientAiohttpSession

logger = logging.getLogger(__name__)

# Text typed while an admin wizard waits for free-form input is treated as that input only when it
# is not a command and not one of the persistent reply-keyboard buttons. Otherwise tapping a menu
# button in the middle of a wizard would be consumed as the wizard's input (e.g. broadcast content).
_FREE_TEXT_INPUT = ~F.text.startswith("/") & ~F.text.in_(ADMIN_REPLY_MENU_LABELS)


def create_admin_router(name: str = "admin_router", is_dedicated: bool = True) -> Router:
    """
    Creates the admin router tree.

    * Dedicated admin bot: every update passes AdminStrictAuthMiddleware.
    * Embedded in the public bot (no ADMIN_BOT_TOKEN): gated by IsAdminFilter.

    Admin tooling only works in private chats, so admin commands typed in a group where the bot is a
    member can never post user lists, logs or backups into that group.
    """
    r = Router(name=name)
    r.message.filter(F.chat.type == ChatType.PRIVATE)
    r.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    if is_dedicated:
        admin_mw = AdminStrictAuthMiddleware()
        r.message.middleware(admin_mw)
        r.callback_query.middleware(admin_mw)
        r.message.register(cmd_admin_start, CommandStart())
        # In the public bot /help and /cancel belong to the public routers.
        r.message.register(cmd_admin_help, Command("help"))
        r.message.register(cmd_admin_cancel, Command("cancel"))
        r.message.register(cmd_admin_cancel, F.text.lower() == "bekor qilish")
    else:
        r.message.filter(IsAdminFilter())
        r.callback_query.filter(IsAdminFilter())

    # Dashboard & navigation
    r.message.register(cmd_admin_start, Command("admin"))
    r.message.register(cmd_admin_start, F.text == ADMIN_MENU_DASHBOARD)
    r.message.register(cmd_admin_catchup, Command("catchup"))
    r.callback_query.register(cb_admin_dashboard, F.data.in_(["admin_main_dashboard", "menu_admin"]))
    r.callback_query.register(cb_restart_listener, F.data == "admin_restart_listener")
    r.callback_query.register(cb_admin_noop, F.data == "noop")

    # MTProto in-bot OTP login
    r.callback_query.register(cb_auth_status, F.data == "admin_auth_status")
    r.message.register(cb_auth_status, F.text == ADMIN_MENU_MTPROTO)
    r.callback_query.register(cb_start_phone, F.data == "auth_start_phone")
    r.callback_query.register(cb_logout_confirm, F.data == "auth_logout_confirm")
    r.callback_query.register(cb_logout_yes, F.data == "auth_logout_yes")

    # System status, steganography check & live logs
    r.callback_query.register(cb_admin_system_status, F.data == "admin_system_status")
    r.message.register(cb_admin_system_status, Command("status"))
    r.message.register(cb_admin_system_status, F.text == ADMIN_MENU_STATUS)
    r.callback_query.register(cb_admin_view_logs, F.data == "admin_view_logs")
    r.message.register(cb_admin_view_logs, Command("logs"))
    r.message.register(cmd_check_origin, Command("check_origin"))

    # Broadcast (content -> preview -> explicit confirmation)
    r.callback_query.register(cb_broadcast_prompt, F.data == "admin_broadcast_prompt")
    r.message.register(cb_broadcast_prompt, F.text == ADMIN_MENU_BROADCAST)
    r.callback_query.register(cb_broadcast_confirm, F.data == "admin_broadcast_confirm")
    r.callback_query.register(cb_broadcast_cancel, F.data == "admin_broadcast_cancel")
    r.callback_query.register(cb_broadcast_stop, F.data == "admin_broadcast_stop")

    # Database snapshot backup
    r.callback_query.register(cb_download_backup, F.data == "admin_download_backup")
    r.message.register(cb_download_backup, Command("backup"))
    r.message.register(cb_download_backup, F.text == ADMIN_MENU_BACKUP)

    # Users & subscriptions
    r.callback_query.register(cb_users_list, F.data == "admin_users_list")
    r.callback_query.register(cb_users_list, F.data.startswith("admin_users_page_"))
    r.message.register(cb_users_list, Command("users"))
    r.message.register(cb_users_list, F.text == ADMIN_MENU_USERS)
    r.callback_query.register(cb_start_user_search, F.data == "adm_search_user")
    r.callback_query.register(cb_user_detail, F.data.startswith("adm_user_"))
    r.callback_query.register(cb_grant_tier, F.data.startswith("adm_grant_"))
    r.callback_query.register(cb_revoke_tier_confirm, F.data.startswith("adm_revoke_ok_"))
    r.callback_query.register(cb_revoke_tier, F.data.startswith("adm_revoke_"))
    r.callback_query.register(cb_grant_admin_privilege, F.data.startswith("adm_admin_grant_"))
    r.callback_query.register(cb_revoke_admin_privilege, F.data.startswith("adm_admin_revoke_"))

    # Bot access mode & whitelist
    r.callback_query.register(cb_admin_bot_mode, F.data == "admin_bot_mode")
    r.message.register(cb_admin_bot_mode, Command("mode"))
    r.message.register(cb_admin_bot_mode, Command("access"))
    r.callback_query.register(cb_toggle_bot_mode, F.data == "admin_toggle_bot_mode")
    r.callback_query.register(cb_set_support_user, F.data == "admin_set_support_user")
    r.callback_query.register(cb_whitelist_list, F.data == "admin_whitelist_list")
    r.callback_query.register(cb_whitelist_stars, F.data == "admin_whitelist_stars")
    r.callback_query.register(cb_whitelist_page, F.data.startswith("adm_wl_page_"))
    r.callback_query.register(cb_whitelist_revoke, F.data.startswith("adm_wl_rm_"))
    r.callback_query.register(cb_whitelist_add, F.data == "admin_whitelist_add")

    # Wizard inputs are registered last, so commands and menu buttons above always win.
    r.message.register(process_phone_input, AuthStates.waiting_for_phone, _FREE_TEXT_INPUT)
    r.message.register(process_code_input, AuthStates.waiting_for_code, _FREE_TEXT_INPUT)
    r.message.register(process_2fa_input, AuthStates.waiting_for_2fa, _FREE_TEXT_INPUT)
    r.message.register(process_broadcast_message, BroadcastStates.waiting_for_message, _FREE_TEXT_INPUT)
    r.message.register(process_user_search_query, AdminUserSG.waiting_for_user_query, _FREE_TEXT_INPUT)
    r.message.register(process_support_user_input, AccessControlStates.waiting_for_support_user, _FREE_TEXT_INPUT)
    r.message.register(process_whitelist_user_input, AccessControlStates.waiting_for_whitelist_user, _FREE_TEXT_INPUT)

    return r



def create_admin_bot() -> Bot:
    """Creates dedicated Aiogram Bot instance for Super Admins"""
    session = ResilientAiohttpSession(proxy=settings.PROXY_URL) if settings.PROXY_URL else ResilientAiohttpSession()
    return Bot(
        token=settings.ADMIN_BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)
    )

def create_admin_dispatcher(storage=None) -> Dispatcher:
    """Creates dedicated Aiogram Dispatcher with strict security middleware"""
    fsm_storage = storage if storage is not None else SQLiteStorage()
    # Serializes updates per user/chat so wizard steps (and FSM data updates) can never interleave
    dp = Dispatcher(storage=fsm_storage, events_isolation=SimpleEventIsolation())
    dp.include_router(create_admin_router("dedicated_admin_router"))
    return dp
