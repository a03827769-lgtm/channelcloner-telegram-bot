import logging
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from admin_bot.keyboards.admin_keyboards import (
    get_auth_menu_keyboard,
    get_auth_cancel_keyboard,
    get_logout_confirm_keyboard,
    get_back_to_admin_keyboard
)
from services.telethon_listener import telethon_listener
from services.phone_utils import mask_phone_number
from services.custom_emojis import (
    KEY, SUCCESS, ERROR, WARN, LOADING, INFO, clean_for_alert
)

logger = logging.getLogger(__name__)
router = Router(name="admin_auth_router")

class AuthStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()

from bot.utils import safe_answer, html_escape

@router.callback_query(F.data == "admin_auth_status")
@router.message(F.text.contains("MTProto Hisob"))
async def cb_auth_status(event: CallbackQuery | Message, state: FSMContext):
    await state.clear()
    if isinstance(event, CallbackQuery):
        await safe_answer(event)

    me = await telethon_listener.get_me()
    is_auth = me is not None

    if is_auth:
        first_name = html_escape(me.first_name or '')
        last_name = html_escape(me.last_name or '')
        text = f"""
{KEY} <b>MTProto Markaziy Telegram Hisobi:</b>

├ <b>Holati:</b> {SUCCESS} Ulangan
├ <b>Ism:</b> {first_name} {last_name}
├ <b>Username:</b> @{me.username or 'mavjud_emas'}
├ <b>Telefon:</b> {mask_phone_number(getattr(me, 'phone', ''))}
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

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(
                text=text,
                parse_mode="HTML",
                reply_markup=get_auth_menu_keyboard(is_auth=is_auth)
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await event.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=get_auth_menu_keyboard(is_auth=is_auth)
        )

@router.callback_query(F.data == "auth_start_phone")
async def cb_start_phone(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(AuthStates.waiting_for_phone)

    text = f"""
{KEY} <b>Telefon raqamingizni kiriting:</b>

Telegram akkauntingizga bog'langan xalqaro formatdagi telefon raqamingizni yuboring:
<i>(Masalan: +998901234567 yoki 998901234567)</i>
"""
    try:
        await callback.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise

@router.message(AuthStates.waiting_for_phone)
async def process_phone_input(message: Message, state: FSMContext):
    if not message.text or message.text.strip().lower() in ("/cancel", "bekor qilish"):
        await state.clear()
        await message.answer(f"{INFO} Telefon kiritish bekor qilindi.", parse_mode="HTML", reply_markup=get_back_to_admin_keyboard())
        return

    phone = message.text.strip()
    user_id = message.from_user.id
    try:
        await message.delete()
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    msg_wait = await message.answer(f"{LOADING} Telegramga ulanish va kod so'rash yuborilmoqda...")

    success, res_msg = await telethon_listener.request_phone_code(user_id=user_id, phone=phone)
    await msg_wait.delete()

    if success:
        await state.update_data(phone=phone)
        await state.set_state(AuthStates.waiting_for_code)
        text = f"""
{SUCCESS} <b>Tasdiqlash kodi yuborildi!</b>

Telegram orqali <code>{mask_phone_number(phone)}</code> raqamiga yuborilgan 5 xonali tasdiqlash kodini kiriting.
<i>(Kod xavfsizligi uchun raqamlar orasiga bo'sh joy qo'ysangiz ham bo'ladi, masalan: <code>1 2 3 4 5</code>)</i>
"""
        await message.answer(text=text, parse_mode="HTML", reply_markup=get_auth_cancel_keyboard())
    else:
        await message.answer(f"{ERROR} <b>Xatolik yuz berdi:</b>\n{res_msg}\n\nQaytadan urinib ko'ring:", parse_mode="HTML", reply_markup=get_auth_cancel_keyboard())

@router.message(AuthStates.waiting_for_code)
async def process_code_input(message: Message, state: FSMContext):
    if not message.text or message.text.strip().lower() in ("/cancel", "bekor qilish"):
        await state.clear()
        await message.answer(f"{INFO} Kod kiritish bekor qilindi.", parse_mode="HTML", reply_markup=get_back_to_admin_keyboard())
        return

    code = message.text.strip()
    data = await state.get_data()
    phone = data.get("phone", "")
    user_id = message.from_user.id
    try:
        await message.delete()
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    msg_wait = await message.answer(f"{LOADING} Kod tekshirilmoqda...")
    ok, res_msg, status = await telethon_listener.submit_phone_code(user_id=user_id, code=code)
    await msg_wait.delete()

    if status == "success":
        await state.clear()
        me = await telethon_listener.get_me()
        me_title = f"{me.first_name} (@{me.username or 'yoq'})" if me else phone
        await message.answer(
            text=f"{SUCCESS} <b>Tabriklaymiz! Telegram hisob muvaffaqiyatli ulandi!</b>\n\nHisob: <b>{me_title}</b>\n\nKloner barcha faol kanallarni monitoring qilishni boshladi.",
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )
    elif status == "needs_2fa":
        await state.set_state(AuthStates.waiting_for_2fa)
        await message.answer(
            text=f"{WARN} <b>Ikki bosqichli autentifikatsiya (2FA) yoqilgan!</b>\n\nIltimos, Telegram hisobingizning 2FA bulutli parolini (Cloud Password) kiriting:",
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )
    else:
        await message.answer(
            text=f"{ERROR} <b>Kod noto'g'ri yoki eskirgan:</b>\n{res_msg}\n\nQaytadan kodni kiriting:",
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )

@router.message(AuthStates.waiting_for_2fa)
async def process_2fa_input(message: Message, state: FSMContext):
    if not message.text or message.text.strip().lower() in ("/cancel", "bekor qilish"):
        await state.clear()
        await message.answer(f"{INFO} 2FA parol kiritish bekor qilindi.", parse_mode="HTML", reply_markup=get_back_to_admin_keyboard())
        return

    password = message.text.strip()
    try:
        await message.delete()
    except Exception:
        logger.debug("Ignored exception", exc_info=True)
    user_id = message.from_user.id
    msg_wait = await message.answer(f"{LOADING} 2FA parol tekshirilmoqda...")
    success, res_msg = await telethon_listener.submit_2fa_password(user_id=user_id, password=password)
    await msg_wait.delete()

    if success:
        await state.clear()
        me = await telethon_listener.get_me()
        me_title = f"{me.first_name} (@{me.username or 'yoq'})" if me else ""
        await message.answer(
            text=f"{SUCCESS} <b>2FA parol tasdiqlandi! MTProto hisob muvaffaqiyatli ulandi!</b>\n\nHisob: <b>{me_title}</b>",
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )
    else:
        await message.answer(
            text=f"{ERROR} <b>2FA parol noto'g'ri:</b>\n{res_msg}\n\nQaytadan parolni kiriting:",
            parse_mode="HTML",
            reply_markup=get_auth_cancel_keyboard()
        )

@router.callback_query(F.data == "auth_logout_confirm")
async def cb_logout_confirm(callback: CallbackQuery):
    await safe_answer(callback)
    try:
        await callback.message.edit_text(
            text=f"{WARN} <b>Haqiqatan ham markaziy Telegram hisobidan chiqmoqchimisiz?</b>\n\nChiqilsa, barcha kanallarni real-vaqtda klonlash to'xtatiladi!",
            parse_mode="HTML",
            reply_markup=get_logout_confirm_keyboard()
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise

@router.callback_query(F.data == "auth_logout_yes")
async def cb_logout_yes(callback: CallbackQuery):
    await safe_answer(callback)
    await telethon_listener.logout()
    try:
        await callback.message.edit_text(
            text=f"{SUCCESS} <b>Telegram hisobidan muvaffaqiyatli chiqildi.</b>\nSessiya xavfsiz o'chirildi.",
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
