import logging
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from admin_bot.keyboards.admin_keyboards import (
    get_auth_menu_keyboard,
    get_auth_cancel_keyboard,
    get_logout_confirm_keyboard,
    get_back_to_admin_keyboard
)
from admin_bot.permissions import ensure_super_admin
from services.telethon_listener import telethon_listener
from services.phone_utils import mask_phone_number
from services.custom_emojis import KEY, SUCCESS, ERROR, WARN, LOADING
from bot.utils import safe_answer, html_escape
from admin_bot.ui import is_cancel_text, show_screen

logger = logging.getLogger(__name__)


class AuthStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()


async def _edit_or_answer(event: CallbackQuery | Message, text: str, reply_markup) -> None:
    await show_screen(event, text, reply_markup)


async def _cancel_login_wizard(message: Message, state: FSMContext) -> None:
    """Ends the login wizard and releases the temporary login client of this admin."""
    await state.clear()
    await telethon_listener.cancel_login(message.from_user.id)
    await message.answer(f"{WARN} MTProto hisobini ulash bekor qilindi.", reply_markup=get_back_to_admin_keyboard())


async def _delete_quietly(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete message", exc_info=True)


def _account_title(me) -> str:
    if not me:
        return ""
    name = html_escape(" ".join(filter(None, [getattr(me, "first_name", None), getattr(me, "last_name", None)])) or "Hisob")
    username = getattr(me, "username", None)
    return f"{name} (@{html_escape(username)})" if username else name


async def cb_auth_status(event: CallbackQuery | Message, state: FSMContext):
    await state.clear()
    user = getattr(event, "from_user", None)
    if user:
        # Leaving the login screen (Cancel button or menu) abandons any half-finished login.
        await telethon_listener.cancel_login(user.id)
    if isinstance(event, CallbackQuery):
        await safe_answer(event)

    me = await telethon_listener.get_me()
    is_auth = me is not None

    if is_auth:
        text = f"""
{KEY} <b>MTProto Markaziy Telegram Hisobi:</b>

├ <b>Holati:</b> {SUCCESS} Ulangan
├ <b>Hisob:</b> {_account_title(me)}
├ <b>Telefon:</b> {html_escape(mask_phone_number(getattr(me, 'phone', '') or ''))}
└ <b>User ID:</b> <code>{me.id}</code>

<i>Ushbu hisob barcha kanallarni fon rejimida kuzatib boradi.</i>
"""
    else:
        text = f"""
{KEY} <b>MTProto Markaziy Telegram Hisobi:</b>

├ <b>Holati:</b> {ERROR} Ulanmagan
└ <b>Tavsif:</b> Kloner kanallarni tinglashi uchun bitta Telegram hisobini ulashingiz lozim.

Quyidagi tugma orqali telefon raqamingizni kiriting:
"""
    await _edit_or_answer(event, text, get_auth_menu_keyboard(is_auth=is_auth))


async def cb_start_phone(callback: CallbackQuery, state: FSMContext):
    if not await ensure_super_admin(callback):
        return
    await safe_answer(callback)
    await state.set_state(AuthStates.waiting_for_phone)
    text = f"""
{KEY} <b>Telefon raqamingizni kiriting:</b>

Telegram akkauntingizga bog'langan xalqaro formatdagi telefon raqamingizni yuboring:
<i>(Masalan: +998901234567 yoki 998901234567)</i>
"""
    await _edit_or_answer(callback, text, get_auth_cancel_keyboard())


async def process_phone_input(message: Message, state: FSMContext):
    if not await ensure_super_admin(message):
        await state.clear()
        return
    if is_cancel_text(message):
        await _cancel_login_wizard(message, state)
        return
    if not message.text:
        await message.answer(f"{WARN} Telefon raqamini matn ko'rinishida yuboring.", reply_markup=get_auth_cancel_keyboard())
        return

    phone = message.text.strip()
    user_id = message.from_user.id
    await _delete_quietly(message)

    msg_wait = await message.answer(f"{LOADING} Telegramga ulanish va kod so'rash yuborilmoqda...")
    success, res_msg = await telethon_listener.request_phone_code(user_id=user_id, phone=phone)
    await _delete_quietly(msg_wait)

    if success:
        # The phone number itself is never written to the FSM storage (SQLite); the in-memory login
        # session inside telethon_listener keeps it only until the login finishes or is cancelled.
        await state.set_state(AuthStates.waiting_for_code)
        text = f"""
{SUCCESS} <b>Tasdiqlash kodi yuborildi!</b>

Telegram orqali <code>{html_escape(mask_phone_number(phone))}</code> raqamiga yuborilgan tasdiqlash kodini kiriting.
<i>(Raqamlar orasiga bo'sh joy qo'ysangiz ham bo'ladi, masalan: <code>1 2 3 4 5</code>)</i>
"""
        await message.answer(text=text, parse_mode="HTML", reply_markup=get_auth_cancel_keyboard())
    else:
        await message.answer(
            f"{ERROR} <b>Xatolik yuz berdi:</b>\n{html_escape(res_msg)}\n\nTelefon raqamni qaytadan kiriting:",
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )


async def process_code_input(message: Message, state: FSMContext):
    if not await ensure_super_admin(message):
        await state.clear()
        return
    if is_cancel_text(message):
        await _cancel_login_wizard(message, state)
        return
    if not message.text:
        await message.answer(f"{WARN} Tasdiqlash kodini matn ko'rinishida yuboring.", reply_markup=get_auth_cancel_keyboard())
        return

    code = message.text.strip()
    user_id = message.from_user.id
    await _delete_quietly(message)

    msg_wait = await message.answer(f"{LOADING} Kod tekshirilmoqda...")
    ok, res_msg, status = await telethon_listener.submit_phone_code(user_id=user_id, code=code)
    await _delete_quietly(msg_wait)

    if status == "success":
        await state.clear()
        me = await telethon_listener.get_me()
        await message.answer(
            text=f"{SUCCESS} <b>Tabriklaymiz! Telegram hisob muvaffaqiyatli ulandi!</b>\n\n"
                 f"Hisob: <b>{_account_title(me) or 'ulandi'}</b>\n\nKloner barcha faol kanallarni monitoring qilishni boshladi.",
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )
    elif status == "needs_2fa":
        await state.set_state(AuthStates.waiting_for_2fa)
        await message.answer(
            text=f"{WARN} <b>Ikki bosqichli autentifikatsiya (2FA) yoqilgan!</b>\n\nIltimos, Telegram hisobingizning bulutli parolini (Cloud Password) kiriting:",
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )
    elif status == "expired":
        # The login cannot continue (code expired / session lost after a restart): start over from the phone step.
        await state.set_state(AuthStates.waiting_for_phone)
        await message.answer(
            text=f"{WARN} <b>{html_escape(res_msg)}</b>",
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )
    else:
        await message.answer(
            text=f"{ERROR} <b>{html_escape(res_msg)}</b>\n\nKodni qaytadan kiriting:",
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )


async def process_2fa_input(message: Message, state: FSMContext):
    if not await ensure_super_admin(message):
        await state.clear()
        return
    if is_cancel_text(message):
        await _cancel_login_wizard(message, state)
        return
    if not message.text:
        await message.answer(f"{WARN} Parolni matn ko'rinishida yuboring.", reply_markup=get_auth_cancel_keyboard())
        return

    password = message.text.strip()
    await _delete_quietly(message)
    user_id = message.from_user.id
    msg_wait = await message.answer(f"{LOADING} 2FA parol tekshirilmoqda...")
    success, res_msg = await telethon_listener.submit_2fa_password(user_id=user_id, password=password)
    await _delete_quietly(msg_wait)

    if success:
        await state.clear()
        me = await telethon_listener.get_me()
        await message.answer(
            text=f"{SUCCESS} <b>2FA parol tasdiqlandi! MTProto hisob muvaffaqiyatli ulandi!</b>\n\nHisob: <b>{_account_title(me) or 'ulandi'}</b>",
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )
    elif not telethon_listener.has_login_session(user_id):
        await state.set_state(AuthStates.waiting_for_phone)
        await message.answer(
            text=f"{WARN} <b>{html_escape(res_msg)}</b>\n\nTelefon raqamni qaytadan kiriting:",
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )
    else:
        await message.answer(
            text=f"{ERROR} <b>{html_escape(res_msg)}</b>\n\nParolni qaytadan kiriting:",
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )


async def cb_logout_confirm(callback: CallbackQuery):
    if not await ensure_super_admin(callback):
        return
    await safe_answer(callback)
    await _edit_or_answer(
        callback,
        f"{WARN} <b>Haqiqatan ham markaziy Telegram hisobidan chiqmoqchimisiz?</b>\n\nChiqilsa, barcha kanallarni real-vaqtda klonlash to'xtatiladi!",
        get_logout_confirm_keyboard()
    )


async def cb_logout_yes(callback: CallbackQuery):
    if not await ensure_super_admin(callback):
        return
    await safe_answer(callback, "Hisobdan chiqilmoqda...")
    revoked = await telethon_listener.logout()
    if revoked:
        text = f"{SUCCESS} <b>Telegram hisobidan muvaffaqiyatli chiqildi.</b>\nSessiya Telegram serverida bekor qilindi va bazadan o'chirildi."
    else:
        text = (
            f"{WARN} <b>Sessiya bazadan o'chirildi, lekin Telegram chiqishni tasdiqlamadi.</b>\n"
            f"Xavfsizlik uchun Telegram ilovasida <i>Sozlamalar → Qurilmalar</i> bo'limidan ushbu seansni yakunlang."
        )
    await _edit_or_answer(callback, text, get_back_to_admin_keyboard())
