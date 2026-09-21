import html
import logging
import re
from datetime import datetime, timezone
from typing import Union, Callable, Dict, Any, Awaitable, Optional
from aiogram import Router, F, BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from database.db_manager import db_manager
from database.models import StorySettings, StorySourceChannel, StoryQueueItem
from services.story_cloner_service import story_cloner_service
from services.phone_utils import mask_phone_number
from services.listing_analyzer import listing_analyzer
from services.story_queue_service import story_queue_service, UZB_TZ
from bot.states.story_states import StoryAuthSG, StorySettingsSG
from bot.keyboards.story_keyboards import (
    get_story_main_menu_keyboard,
    get_story_auth_keyboard,
    get_story_filters_keyboard,
    get_story_price_preset_keyboard,
    get_story_design_keyboard,
    get_story_duration_keyboard,
    get_story_channel_keyboard,
    get_story_channels_keyboard,
    get_story_queue_keyboard,
    get_story_cooldown_preset_keyboard,
    get_story_daily_limit_preset_keyboard,
    get_story_cancel_keyboard,
    get_story_back_keyboard,
    get_story_vip_upgrade_keyboard,
    get_story_logout_confirm_keyboard
)

from services.custom_emojis import (
    CROWN, STARS, MEDAL_BRONZE, VIDEO, STAR_SPARKLE, HOME, CLOCK, TAG,
    SERVER_CPU, CHANNEL, ROCKET, SUCCESS, ERROR, USER_PROFILE, FLASH_GREEN,
    PALETTE, IMAGE, DIAMOND, SWITCH_ON, SWITCH_OFF, PAUSE, TARGET, MONEY,
    BAN, SETTINGS, TIMER, HISTORY_CLOCK, QUEUE, STATS, CALENDAR, STATS_GROWTH,
    ARROW_DOWN, MOBILE, LINK, PHONE, LOCK_LOCKED, LOADING, PARTY, LOCK_PASSWORD,
    WARN, PIN, EDIT, DOCUMENT, INFO, TIP, WRENCH, AUDIO, SIGNATURE, TELEGRAM,
    LOCATION_RED, TROPHY
)

logger = logging.getLogger(__name__)
router = Router(name="story_menu_router")


def get_story_vip_upgrade_text(user_tier: str = "free") -> str:
    tier_display = {
        "vip": f"{CROWN} VIP Cheksiz",
        "pro": f"{STARS} Pro Tarif",
        "free": f"{MEDAL_BRONZE} Bepul Sinov (Free Trial)"
    }.get(user_tier, user_tier)

    return f"""
{CROWN} <b>VIP Cheksiz Tarif Talab Qilinadi!</b>

Sizning hozirgi tarifingiz: <b>{tier_display}</b>

{VIDEO} <b>Real Estate Auto-Story Cloner ($700+)</b> funksiyasi faqat <b>VIP Cheksiz</b> foydalanuvchilari uchun maxsus ishlab chiqilgan eksklyuziv imkoniyatdir!

{STAR_SPARKLE} <b>VIP Tarif Imkoniyatlari:</b>
├ {HOME} <b>Avto-Istoriya Kloner:</b> Toshkentdagi $700+ hashamatli kvartiralarni kanallardan avtomatik aniqlash
├ {VIDEO} <b>Playwright 4K & Ken Burns:</b> 25 soniyalik hashamatli video va interaktiv vizual kollaj kartochkalari
├ {CLOCK} <b>Prime Time Drip Navbat:</b> Optimal ko'rish soatlarida (09:00 - 22:00) avtomatik navbat bilan joylash
├ {TAG} <b>Aqlli Badjlar:</b> Tuman, xonalar soni, maydoni va narx teglari
├ {SERVER_CPU} <b>14 Kunlik Xotira:</b> Qayta takrorlanishlardan 100% himoya
├ {CHANNEL} <b>Cheksiz Kanallar Juftligi:</b> 999 tagacha kanallarni parallel ko'chirish
├ {STAR_SPARKLE} <b>Telegram Premium Animatsion Emojilar:</b> Avtomatik konvertatsiya
└ {ROCKET} <b>0 Sekundlik Server Ustuvorligi:</b> Eng yuqori tezlik

<i>Hoziroq VIP tarifga o'ting va ko'chmas mulk biznesingizni yangi bosqichga olib chiqing:</i>
"""


async def get_story_vip_upgrade_text_and_keyboard(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    sub = await db_manager.get_user_subscription(user_id)
    tier = sub.tier if sub else "free"
    return get_story_vip_upgrade_text(tier), get_story_vip_upgrade_keyboard()


async def check_is_vip(user_id: int) -> bool:
    return await db_manager.is_vip(user_id)


class StoryVipMiddleware(BaseMiddleware):
    """
    Guarantees strict VIP Tier Only restriction across all Story Cloner handlers.
    Non-VIP users attempting any action on the story router are blocked immediately
    and presented with the VIP upgrade prompt with a direct Stars billing button.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if not user and hasattr(event, "from_user"):
            user = getattr(event, "from_user")
        if not user or getattr(user, "is_bot", False):
            return await handler(event, data)

        is_vip = await check_is_vip(user.id)
        if is_vip:
            return await handler(event, data)

        # Clear any active FSM state
        state: Optional[FSMContext] = data.get("state")
        if state:
            await state.clear()

        # Non-VIP user blocked! Display VIP upgrade prompt
        text, kb = await get_story_vip_upgrade_text_and_keyboard(user.id)

        if isinstance(event, CallbackQuery):
            await safe_answer(event, "Bu funksiya faqat VIP Cheksiz tarif egalari uchun!", show_alert=False)
            if event.message:
                await safe_edit_text(event.message, text=text, parse_mode="HTML", reply_markup=kb)
            return None
        elif isinstance(event, Message):
            await event.answer(text=text, parse_mode="HTML", reply_markup=kb)
            return None

        return None


# Attach VIP middleware to all message and callback_query events on story router
router.message.middleware(StoryVipMiddleware())
router.callback_query.middleware(StoryVipMiddleware())


async def safe_answer(cb: CallbackQuery, text: str = None, show_alert: bool = False):
    try:
        await cb.answer(text=text, show_alert=show_alert)
    except Exception:
        logger.debug("Ignored exception", exc_info=True)


async def safe_edit_text(message: Message, text: str, **kwargs):
    try:
        await message.edit_text(text=text, **kwargs)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return
        logger.warning(f"safe_edit_text note: {e}")
        try:
            await message.answer(text=text, **kwargs)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)


async def render_story_main_menu(user_id: int) -> tuple[str, any]:
    """Prepares the main dashboard text and keyboard for the user"""
    if not await check_is_vip(user_id):
        return await get_story_vip_upgrade_text_and_keyboard(user_id)

    session_info = await db_manager.get_user_session_info(user_id)
    is_auth = bool(session_info and session_info.get("is_active"))
    st = await db_manager.get_story_settings(user_id)
    stats = await db_manager.get_story_stats(user_id)
    extra_channels = await db_manager.get_story_source_channels(user_id)
    queue_items = await db_manager.get_user_story_queue(user_id)

    auth_status = f"{SUCCESS} Ulangan" if is_auth else f"{ERROR} Ulanmagan"
    user_name = html.escape(session_info.get("first_name", "") if session_info else "")
    phone_masked = mask_phone_number(session_info.get("phone", "") if session_info else "")
    account_line = f"├ {USER_PROFILE} <b>Hisob:</b> {user_name} ({phone_masked})" if is_auth else f"├ {USER_PROFILE} <b>Hisob:</b> <i>Ulanmagan</i>"

    src_channel = html.escape(st.source_channel) if st.source_channel else "<i>Sozlanmagan</i>"
    if extra_channels:
        src_line = f"├ {CHANNEL} <b>Manba kanallar:</b> {src_channel} <i>(+{len(extra_channels)} ta qo'shimcha)</i>"
    else:
        src_line = f"├ {CHANNEL} <b>Manba kanal:</b> {src_channel}"

    target_text = "Shaxsiy Istoriya (Profile Story)" if st.target_type == "self" else f"Kanal ({st.target_channel or 'Sozlanmagan'})"

    style_names = {
        "telegram_green": f"{PALETTE} Telegram Yashil (Nativ)",
        "listing_blur": f"{IMAGE} Xiralashtirilgan Kvartira Rasmi",
        "luxury_dark": f"{CROWN} To'q Lux Gradiyent",
        "emerald": f"{DIAMOND} Zumrad Gradiyent"
    }
    style_label = style_names.get(st.background_style, st.background_style)

    active_label = f"Faol (Kuzatuvda) {SWITCH_ON}" if st.is_active else f"To'xtatilgan {PAUSE}"
    prime_status = f"Yoqilgan (09:00 - 22:00) {SUCCESS}" if st.prime_hours_enabled else f"24/7 (Cheklovsiz) {SWITCH_OFF}"
    badges_status = f"Yoqilgan {TAG}" if st.enable_smart_badges else f"O'chiq {SWITCH_OFF}"

    text = f"""
{CROWN} <b>Real Estate Auto-Story Cloner — Boshqaruv Markazi</b>

Ushbu tizim kanallaringizdagi eng sara <b>${st.min_price:g}+</b> variantlarni avtomatik aniqlab, 14 kunlik xotira bilan takrorlanishlarsiz xuddi Telegramning o'zidan repost qilingandek interaktiv video Istoriya joylaydi!

{MOBILE} <b>Telegram Akkaunt (Userbot):</b>
{account_line}
└ {FLASH_GREEN} <b>Holati:</b> {auth_status}

{CHANNEL} <b>Kanal va Filtr Sozlamalari:</b>
{src_line}
├ {TARGET} <b>Joylash joyi:</b> {target_text}
├ {MONEY} <b>Minimal narx:</b> <b>${st.min_price:g} dan boshlanadigan</b>
├ {IMAGE} <b>Rasmli postlar:</b> {f"Majburiy {SUCCESS}" if st.require_photos else f"Ixtiyoriy {SWITCH_OFF}"}
├ {BAN} <b>Qidiruvlarni filtrlash:</b> {f"Faol {SUCCESS}" if st.filter_demands else f"O'chiq {SWITCH_OFF}"}
├ {PALETTE} <b>Istoriya foni:</b> {style_label}
└ {SETTINGS} <b>Avto-Monitoring:</b> <b>{active_label}</b>

{TIMER} <b>Smart Navbat & Prime Time:</b>
├ {CLOCK} <b>Prime Time:</b> <b>{prime_status}</b>
├ {HISTORY_CLOCK} <b>Drip oralig'i:</b> {st.drip_delay_minutes} daq | kuniga max {st.max_stories_per_day} ta
├ {TAG} <b>Aqlli badjlar (Chips):</b> {badges_status}
└ {QUEUE} <b>Navbatdagi e'lonlar:</b> <b>{len(queue_items)} ta</b>

{STATS} <b>Statistika:</b>
├ {CALENDAR} Bugun joylangan: <b>{stats.get('today_posted', 0)} ta</b>
└ {STATS_GROWTH} Jami joylangan: <b>{stats.get('total_posted', 0)} ta</b>

{ARROW_DOWN} <i>Quyidagi tugmalardan birini tanlang:</i>
"""
    kb = get_story_main_menu_keyboard(is_auth=is_auth, is_active=st.is_active, has_source=bool(st.source_channel))
    return text, kb


# --- ENTRY HANDLERS ---

@router.message(Command("story"))
@router.message(Command("istoriya"))
@router.message(F.text.contains("Istoriya Kloner"))
@router.callback_query(F.data == "story_main_menu")
async def show_story_menu(event: Union[CallbackQuery, Message], state: FSMContext):
    await state.clear()
    user_id = event.from_user.id
    text, kb = await render_story_main_menu(user_id)

    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await safe_edit_text(event.message, text=text, parse_mode="HTML", reply_markup=kb)
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=kb)


# --- TELEGRAM ACCOUNT (USERBOT) AUTH FLOW ---

@router.callback_query(F.data == "story_menu_auth")
async def cb_story_auth_menu(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id
    session_info = await db_manager.get_user_session_info(user_id)
    is_auth = bool(session_info and session_info.get("is_active"))

    if is_auth:
        user_name = html.escape(session_info.get("first_name", "") or "")
        username = session_info.get("username", "")
        phone = mask_phone_number(session_info.get("phone", "") or "")
        username_line = f"@{username}" if username else "Mavjud emas"

        text = f"""
{MOBILE} <b>Ulangan Telegram Akkaunt (Userbot):</b>

├ {USER_PROFILE} <b>Ism:</b> {user_name}
├ {LINK} <b>Username:</b> {username_line}
├ {PHONE} <b>Telefon:</b> <code>{phone}</code>
└ {SWITCH_ON} <b>Holati:</b> Ulangan va faol

<i>Ushbu hisob nomidan kanaldagi sara postlar Telegram Istoriyalariga avtomatik tarzda joylanadi.</i>
"""
    else:
        text = f"""
{MOBILE} <b>Telegram Akkauntni Ulash (Userbot):</b>

Kanaldan saralangan variantlarni profilingizga avtomatik Istoriya qilib joylash uchun Telegram akkauntingizni ulashingiz lozim.

{LOCK_LOCKED} <b>Xavfsizlik kafolati:</b>
- Sessiya kaliti maxsus AES-128 Fernet shifrlash orqali himoyalanadi.
- Bot faqatgina ko'rsatilgan kanallardan o'zingiz belgilagan postlarni istoriyaga repost qilish huquqidan foydalanadi.
- Xohlagan vaqtda birgina tugma orqali akkauntdan chiqib ketishingiz mumkin.

Hisobni ulash uchun quyidagi tugmani bosing:
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_auth_keyboard(is_auth=is_auth)
    )


@router.callback_query(F.data == "story_auth_start")
async def cb_story_auth_start(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StoryAuthSG.waiting_for_phone)
    text = f"""
{PHONE} <b>Telefon raqamingizni kiriting:</b>

Telegram akkauntingizga bog'langan xalqaro formatdagi telefon raqamingizni yuboring:
<i>(Masalan: <code>+998901234567</code> yoki <code>998901234567</code>)</i>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.message(StoryAuthSG.waiting_for_phone)
async def process_story_phone_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, telefon raqamingizni matn shaklida yuboring.", reply_markup=get_story_cancel_keyboard())
        return

    phone = message.text.strip()
    user_id = message.from_user.id

    msg_wait = await message.answer(f"{LOADING} Telegramga ulanish va tasdiqlash kodi so'ralmoqda...")
    ok, res_msg = await story_cloner_service.request_otp_code(user_id=user_id, phone=phone)
    await msg_wait.delete()

    if ok:
        await state.update_data(phone=phone)
        await state.set_state(StoryAuthSG.waiting_for_code)
        text = f"""
{SUCCESS} <b>Tasdiqlash kodi yuborildi!</b>

Telegram orqali <code>{phone}</code> hisobingizga yuborilgan 5 xonali tasdiqlash kodini kiriting.
<i>(Masalan: <code>1 2 3 4 5</code>)</i>
"""
        await message.answer(text=text, parse_mode="HTML", reply_markup=get_story_cancel_keyboard())
    else:
        await message.answer(f"{ERROR} <b>Xatolik:</b>\n{res_msg}\n\nQaytadan urinib ko'ring:", parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.message(StoryAuthSG.waiting_for_code)
async def process_story_code_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, tasdiqlash kodini matn shaklida yuboring.", reply_markup=get_story_cancel_keyboard())
        return

    code = message.text.strip()
    user_id = message.from_user.id

    msg_wait = await message.answer(f"{LOADING} Kod tekshirilmoqda...")
    ok, res_msg, status = await story_cloner_service.submit_otp_code(user_id=user_id, code=code)
    await msg_wait.delete()

    if status == "success":
        await state.clear()
        text, kb = await render_story_main_menu(user_id)
        await message.answer(
            f"{PARTY} <b>Muborakbod etamiz!</b>\n\n{res_msg}\nEndi bot sizning hisobingiz nomidan eng sara variantlarni Istoriyaga avtomatik joylaydi!",
            parse_mode="HTML"
        )
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
    elif status == "needs_2fa":
        await state.set_state(StoryAuthSG.waiting_for_2fa)
        text = f"""
{LOCK_PASSWORD} <b>Ikki bosqichli autentifikatsiya (2FA) yoqilgan!</b>

Telegram hisobingizning 2FA bulutli parolini (Cloud Password) kiriting:
"""
        await message.answer(text=text, parse_mode="HTML", reply_markup=get_story_cancel_keyboard())
    else:
        await message.answer(f"{ERROR} <b>Xatolik:</b>\n{res_msg}\n\nQaytadan kodni kiriting:", parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.message(StoryAuthSG.waiting_for_2fa)
async def process_story_2fa_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, 2FA parolingizni matn shaklida yuboring.", reply_markup=get_story_cancel_keyboard())
        return

    password = message.text.strip()
    try:
        await message.delete()
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    user_id = message.from_user.id
    msg_wait = await message.answer(f"{LOADING} 2FA parol tekshirilmoqda...")
    ok, res_msg = await story_cloner_service.submit_2fa_password(user_id=user_id, password=password)
    await msg_wait.delete()

    if ok:
        await state.clear()
        text, kb = await render_story_main_menu(user_id)
        await message.answer(f"{PARTY} <b>Muvaffaqiyatli!</b>\n{res_msg}", parse_mode="HTML")
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(f"{ERROR} <b>Xatolik:</b>\n{res_msg}\n\nQaytadan parolni kiriting:", parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.callback_query(F.data == "story_auth_logout_confirm")
async def cb_story_logout_confirm(callback: CallbackQuery):
    await safe_answer(callback)
    await callback.message.edit_text(
        f"{WARN} <b>Haqiqatan ham ulangan Telegram akkauntdan chiqmoqchimisiz?</b>\n\n"
        f"Chiqilsa, ushbu hisob nomidan avtomatik istoriya monitoringi to'xtatiladi va saqlangan sessiya xavfsiz o'chiriladi.",
        parse_mode="HTML",
        reply_markup=get_story_logout_confirm_keyboard()
    )


@router.callback_query(F.data == "story_auth_logout_yes")
async def cb_story_logout_yes(callback: CallbackQuery):
    await safe_answer(callback, "Hisob uzildi")
    user_id = callback.from_user.id
    await story_cloner_service.disconnect_user(user_id)
    await callback.message.edit_text(
        f"{SUCCESS} <b>Telegram akkaunt muvaffaqiyatli uzildi!</b>\n\n"
        f"Sessiya xotiradan va bazadan to'liq o'chirildi, avtomatik monitoring to'xtatildi.\n"
        f"Xohlagan vaqtingizda yangi yoki boshqa hisobingizni qayta ulashingiz mumkin.",
        parse_mode="HTML",
        reply_markup=get_story_back_keyboard()
    )


# --- MULTI-SOURCE CHANNELS CONFIGURATION ---

@router.callback_query(F.data.in_(["story_menu_channels", "story_menu_channel"]))
async def cb_story_menu_channels(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    extra_channels = await db_manager.get_story_source_channels(user_id)

    src = html.escape(st.source_channel) if st.source_channel else "<i>Sozlanmagan</i>"
    target_desc = "Sizning shaxsiy profilingiz (Makler brendi)" if st.target_type == "self" else f"Telegram Kanal ({st.target_channel or 'Sozlanmagan'})"

    extra_lines = ""
    if extra_channels:
        extra_lines = f"\n\n{PIN} <b>Qo'shimcha kuzatilayotgan kanallar:</b>\n"
        for i, ch in enumerate(extra_channels, 1):
            st_icon = f"{SWITCH_ON} Faol" if ch.is_active else f"{PAUSE} To'xtatilgan"
            title_part = f" ({html.escape(ch.channel_title)})" if ch.channel_title else ""
            extra_lines += f"{i}. <code>{ch.channel_username}</code>{title_part} — {st_icon}\n"

    text = f"""
{CHANNEL} <b>Manba Kanallar Boshqaruvi (Multi-Manba):</b>

Tizim ko'rsatilgan bir nechta kanallarni parallel kuzatib boradi. Agar bir xil kvartira e'loni turli kanallarda qayta chiqsa, 14 kunlik xotira tizimi uni avtomatik aniqlab, takroriy post qo'ymaydi!

├ {CHANNEL} <b>Asosiy kanal:</b> {src}
└ {TARGET} <b>Istoriya joylanadigan joy:</b> {target_desc}{extra_lines}

<i>Kanal qo'shish yoki o'chirish uchun quyidagi tugmalardan foydalaning:</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_channels_keyboard(st.source_channel, extra_channels, st.target_type)
    )


@router.callback_query(F.data == "story_edit_channel")
async def cb_story_edit_channel(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StorySettingsSG.waiting_for_source_channel)
    text = f"""
{CHANNEL} <b>Asosiy manba kanal manzilini kiriting:</b>

Variantlar olinishi kerak bo'lgan asosiy kanalning username yoki havolasini yuboring:
<i>(Masalan: <code>@yunsabod_kvartiralari</code> yoki <code>https://t.me/yunsabod_kvartiralari</code>)</i>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


async def save_source_channel_for_user(user_id: int, raw_input: str) -> tuple[str, str, any]:
    """Helper to sanitize, resolve, persist source channel, and return confirmation + menu"""
    clean_channel = raw_input.strip()
    if "t.me/" in clean_channel:
        clean_channel = clean_channel.split("t.me/")[-1].split("/")[0].split("?")[0]
        if not clean_channel.startswith("+"):
            clean_channel = f"@{clean_channel.lstrip('@')}"
    elif not clean_channel.startswith("@") and not clean_channel.startswith("-100") and not clean_channel.startswith("+"):
        clean_channel = f"@{clean_channel}"

    st = await db_manager.get_story_settings(user_id)
    st.source_channel = clean_channel
    st.source_title = clean_channel

    # Try resolving title and ID from client if connected
    try:
        client = await story_cloner_service.get_client_for_user(user_id)
        if client and client.is_connected():
            ent = await client.get_entity(clean_channel)
            if ent:
                st.source_id = getattr(ent, "id", None)
                st.source_title = getattr(ent, "title", "") or clean_channel
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    await db_manager.save_story_settings(st)

    # Start live userbot monitoring if active
    if st.is_active:
        import asyncio
        asyncio.create_task(story_cloner_service.start_monitor_for_user(user_id))

    text, kb = await render_story_main_menu(user_id)
    confirm_msg = f"{SUCCESS} <b>Asosiy manba kanal saqlandi:</b> <code>{clean_channel}</code>"
    if st.source_title and st.source_title != clean_channel:
        confirm_msg += f" (<b>{html.escape(st.source_title)}</b>)"
    return confirm_msg, text, kb


@router.message(StorySettingsSG.waiting_for_source_channel)
async def process_source_channel_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, kanal havolasi yoki usernamesini matn shaklida yuboring.", reply_markup=get_story_cancel_keyboard())
        return

    await state.clear()
    user_id = message.from_user.id
    confirm_msg, text, kb = await save_source_channel_for_user(user_id, message.text)
    await message.answer(confirm_msg, parse_mode="HTML")
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "story_add_extra_channel")
async def cb_story_add_extra_channel(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StorySettingsSG.waiting_for_add_channel)
    text = f"""
{ROCKET} <b>Yangi manba kanal qo'shish (Multi-Manba):</b>

Kuzatuvga qo'shmoqchi bo'lgan ko'chmas mulk kanalining username yoki havolasini yuboring:
<i>(Masalan: <code>@toshkent_kvartiralari</code> yoki <code>https://t.me/toshkent_kvartiralari</code>)</i>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.message(StorySettingsSG.waiting_for_add_channel)
async def process_add_extra_channel_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, kanal havolasi yoki usernamesini matn shaklida yuboring.", reply_markup=get_story_cancel_keyboard())
        return

    await state.clear()
    user_id = message.from_user.id
    raw_input = message.text.strip()
    clean_channel = raw_input
    if "t.me/" in clean_channel:
        clean_channel = clean_channel.split("t.me/")[-1].split("/")[0].split("?")[0]
        if not clean_channel.startswith("+"):
            clean_channel = f"@{clean_channel.lstrip('@')}"
    elif not clean_channel.startswith("@") and not clean_channel.startswith("-100") and not clean_channel.startswith("+"):
        clean_channel = f"@{clean_channel}"

    title = clean_channel
    channel_id = None
    try:
        client = await story_cloner_service.get_client_for_user(user_id)
        if client and client.is_connected():
            ent = await client.get_entity(clean_channel)
            if ent:
                channel_id = getattr(ent, "id", None)
                title = getattr(ent, "title", "") or clean_channel
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    channel_row_id = await db_manager.add_story_source_channel(user_id, clean_channel, title, channel_id)
    if channel_row_id:
        st = await db_manager.get_story_settings(user_id)
        if st.is_active:
            import asyncio
            asyncio.create_task(story_cloner_service.start_monitor_for_user(user_id))

        await message.answer(f"{SUCCESS} <b>Yangi manba kanal qo'shildi:</b> <code>{clean_channel}</code> (<b>{html.escape(title)}</b>)", parse_mode="HTML")
    else:
        await message.answer(f"{INFO} Ushbu kanal allaqachon ro'yxatda mavjud: <code>{clean_channel}</code>", parse_mode="HTML")

    text, kb = await render_story_main_menu(user_id)
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("story_del_src_"))
async def cb_story_del_src(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        ch_id = int(callback.data.replace("story_del_src_", ""))
        await db_manager.delete_story_source_channel(user_id, ch_id)
        await safe_answer(callback, "Kanal o'chirildi")
        st = await db_manager.get_story_settings(user_id)
        if st.is_active:
            import asyncio
            asyncio.create_task(story_cloner_service.start_monitor_for_user(user_id))
    except Exception as e:
        await safe_answer(callback, f"Xatolik: {e}", show_alert=True)

    extra_channels = await db_manager.get_story_source_channels(user_id)
    st = await db_manager.get_story_settings(user_id)
    await callback.message.edit_reply_markup(
        reply_markup=get_story_channels_keyboard(st.source_channel, extra_channels, st.target_type)
    )


@router.callback_query(F.data.startswith("story_toggle_src_"))
async def cb_story_toggle_src(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        ch_id = int(callback.data.replace("story_toggle_src_", ""))
        extra_channels = await db_manager.get_story_source_channels(user_id)
        target_ch = next((c for c in extra_channels if c.id == ch_id), None)
        if target_ch:
            new_state = 0 if target_ch.is_active else 1
            await db_manager.execute("UPDATE story_source_channels SET is_active = ? WHERE id = ? AND user_id = ?", (new_state, ch_id, user_id))
            status_text = "yoqildi" if new_state else "to'xtatildi"
            await safe_answer(callback, f"Kanal {status_text}")
            st = await db_manager.get_story_settings(user_id)
            if st.is_active:
                import asyncio
                asyncio.create_task(story_cloner_service.start_monitor_for_user(user_id))
    except Exception as e:
        await safe_answer(callback, f"Xatolik: {e}", show_alert=True)

    extra_channels = await db_manager.get_story_source_channels(user_id)
    st = await db_manager.get_story_settings(user_id)
    await callback.message.edit_reply_markup(
        reply_markup=get_story_channels_keyboard(st.source_channel, extra_channels, st.target_type)
    )


@router.callback_query(F.data == "story_channel_primary_info")
async def cb_story_primary_info(callback: CallbackQuery):
    await safe_answer(callback, "Bu asosiy manba kanal. Uni o'zgartirish uchun 'O'zgartirish' tugmasini bosing.", show_alert=True)



@router.callback_query(F.data == "story_toggle_target")
async def cb_story_toggle_target(callback: CallbackQuery):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    st.target_type = "channel" if st.target_type == "self" else "self"
    await db_manager.save_story_settings(st)
    await safe_answer(callback, f"Joylash joyi: {st.target_type.upper()}")

    extra_channels = await db_manager.get_story_source_channels(user_id)
    await callback.message.edit_reply_markup(
        reply_markup=get_story_channels_keyboard(st.source_channel, extra_channels, st.target_type)
    )


# --- QUEUE & PRIME TIME SETTINGS ---

@router.callback_query(F.data == "story_menu_queue")
async def cb_story_menu_queue(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    queue_items = await db_manager.get_user_story_queue(user_id)

    prime_str = f"YOQILGAN {SUCCESS} (09:00 - 22:00 Toshkent vaqti)" if st.prime_hours_enabled else f"O'CHIRILGAN {SWITCH_OFF} (24/7 har qanday vaqtda)"
    badges_str = f"YOQILGAN {TAG} ({DIAMOND} PREMYUM, {LOCATION_RED} Tuman, {HOME} Xona)" if st.enable_smart_badges else f"O'CHIRILGAN {SWITCH_OFF}"
    pin_str = f"YOQILGAN {SUCCESS} (Profil va kanal postlarida qoladi)" if st.pin_to_profile else f"O'CHIRILGAN {SWITCH_OFF} (Faqat arxivga tushadi)"

    now_uzb = story_queue_service.get_uzb_now().strftime("%H:%M")

    text = f"""
{TIMER} <b>Smart Navbat va Prime Time Sozlamalari:</b>

Ko'chmas mulk qidirayotgan mijozlar e'tiborini maksimal jalb qilish va profilingizni spamsiz professional yuritish uchun aqlli navbat tizimi:

├ {CLOCK} <b>Prime Time rejimi:</b> {prime_str}
├ {HISTORY_CLOCK} <b>Postlar oralig'i:</b> <b>har {st.drip_delay_minutes} daqiqada</b>
├ {CALENDAR} <b>Kunlik maksimal limit:</b> <b>{st.max_stories_per_day} ta istoriya</b>
├ {TAG} <b>Aqlli badjlar (Pill chips):</b> {badges_str}
├ {PIN} <b>Profilga saqlash (Pin):</b> {pin_str}
├ {CLOCK} <b>Hozirgi Toshkent vaqti:</b> <b>{now_uzb}</b>
└ {QUEUE} <b>Navbatda kutayotgan e'lonlar:</b> <b>{len(queue_items)} ta</b>

{TIP} <i>Tungi vaqtda (22:00 dan keyin) chiqqan sara e'lonlar avtomatik navbatga olinadi va ertalab 09:00 da sifat bali bo'yicha ketma-ket joylanadi!</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_queue_keyboard(st, len(queue_items))
    )


@router.callback_query(F.data == "story_toggle_prime_hours")
async def cb_story_toggle_prime_hours(callback: CallbackQuery):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    st.prime_hours_enabled = not st.prime_hours_enabled
    await db_manager.save_story_settings(st)
    status_label = "YOQILDI (09:00-22:00)" if st.prime_hours_enabled else "O'CHIRILDI (24/7)"
    await safe_answer(callback, f"Prime Time: {status_label}")
    queue_items = await db_manager.get_user_story_queue(user_id)
    await callback.message.edit_reply_markup(reply_markup=get_story_queue_keyboard(st, len(queue_items)))


@router.callback_query(F.data == "story_toggle_badges")
async def cb_story_toggle_badges(callback: CallbackQuery):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    st.enable_smart_badges = not st.enable_smart_badges
    await db_manager.save_story_settings(st)
    status_label = "YOQILDI" if st.enable_smart_badges else "O'CHIRILDI"
    await safe_answer(callback, f"Aqlli badjlar: {status_label}")
    queue_items = await db_manager.get_user_story_queue(user_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=get_story_queue_keyboard(st, len(queue_items)))
    except Exception:
        await callback.message.edit_reply_markup(reply_markup=get_story_filters_keyboard(st))


@router.callback_query(F.data == "story_toggle_pin")
async def cb_story_toggle_pin(callback: CallbackQuery):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    st.pin_to_profile = not st.pin_to_profile
    await db_manager.save_story_settings(st)
    status_label = "YOQILDI (Profilda qoladi)" if st.pin_to_profile else "O'CHIRILDI (Arxivga tushadi)"
    await safe_answer(callback, f"Profilga saqlash: {status_label}")
    queue_items = await db_manager.get_user_story_queue(user_id)
    await callback.message.edit_reply_markup(reply_markup=get_story_queue_keyboard(st, len(queue_items)))


@router.callback_query(F.data == "story_menu_cooldown")
async def cb_story_menu_cooldown(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    text = f"""
{TIMER} <b>Istoriyalar orasidagi vaqt oralig'ini tanlang:</b>

Hozirgi oraliq: <b>{st.drip_delay_minutes} daqiqa</b>

<i>Muxlislarni zeriktirmaslik uchun kamida 30-45 daqiqalik oraliq tavsiya etiladi.</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_cooldown_preset_keyboard(st.drip_delay_minutes)
    )


@router.callback_query(F.data.startswith("story_set_cooldown_"))
async def cb_story_set_cooldown(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    try:
        val = int(callback.data.replace("story_set_cooldown_", ""))
        st = await db_manager.get_story_settings(user_id)
        st.drip_delay_minutes = val
        await db_manager.save_story_settings(st)
        await safe_answer(callback, f"Oraliq: {val} daqiqa o'rnatildi")
    except Exception as e:
        await safe_answer(callback, f"Xatolik: {e}", show_alert=True)
    await cb_story_menu_queue(callback, state)


@router.callback_query(F.data == "story_menu_daily_limit")
async def cb_story_menu_daily_limit(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    text = f"""
{CALENDAR} <b>Bir kundagi maksimal istoriyalar chegarasi:</b>

Hozirgi chegara: <b>kuniga max {st.max_stories_per_day} ta</b>

{STARS} <b>Telegram Rasmiy Istoriya Limitlari:</b>
├ {CROWN} <b>Telegram Premium:</b> Kuniga maksimal <b>100 tagacha</b> istoriya (24 soatda)
├ {USER_PROFILE} <b>Oddiy akkauntlar:</b> Kuniga maksimal <b>3 ta</b> istoriya
└ {CHANNEL} <b>Kanallar:</b> Har 1 ta Boost Level uchun kuniga <b>1 ta</b> istoriya

{TIP} <i>Tavsiya: Ko'chmas mulk va savdo profillari uchun kuniga 5–30 ta eng sara variant eng yuqori qamrov va konversiyani beradi. O'zingizga ma'qul limitni tanlang yoki qo'lda kiriting:</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_daily_limit_preset_keyboard(st.max_stories_per_day)
    )


@router.callback_query(F.data.startswith("story_set_limit_"))
async def cb_story_set_limit(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    try:
        val = int(callback.data.replace("story_set_limit_", ""))
        val = max(1, min(val, 100))
        st = await db_manager.get_story_settings(user_id)
        st.max_stories_per_day = val
        await db_manager.save_story_settings(st)
        await safe_answer(callback, f"Kunlik limit: {val} ta o'rnatildi")
    except Exception as e:
        await safe_answer(callback, f"Xatolik: {e}", show_alert=True)
    await cb_story_menu_queue(callback, state)


@router.callback_query(F.data == "story_custom_daily_limit")
async def cb_story_custom_daily_limit(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StorySettingsSG.waiting_for_custom_daily_limit)
    text = f"""
{EDIT} <b>Kunlik maksimal istoriyalar sonini kiriting:</b>

Telegram Premium hisoblarida bir kecha-kunduzda (24 soat) maksimal <b>100 tagacha</b> istoriya ruxsat etiladi.

Iltimos, <b>1</b> dan <b>100</b> gacha bo'lgan butun son yuboring:
<i>(Masalan: 10, 25, 50 yoki 100)</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_cancel_keyboard()
    )


@router.message(StorySettingsSG.waiting_for_custom_daily_limit)
async def process_custom_daily_limit(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer(
            f"{ERROR} <b>Noto'g'ri qiymat!</b> Iltimos, 1 dan 100 gacha bo'lgan butun son kiriting.",
            parse_mode="HTML"
        )
        return
    val = int(text)
    if val < 1 or val > 100:
        await message.answer(
            f"{ERROR} <b>Cheklovdan oshib ketdi!</b> Telegram Premium bo'yicha kunlik istoriya limiti <b>1 dan 100 gacha</b> bo'lishi kerak.",
            parse_mode="HTML"
        )
        return

    st = await db_manager.get_story_settings(message.from_user.id)
    st.max_stories_per_day = val
    await db_manager.save_story_settings(st)
    await state.clear()
    await message.answer(
        f"{SUCCESS} <b>Kunlik limit saqlandi:</b> Bir kunda maksimal <b>{val} ta</b> istoriya joylanadi!",
        parse_mode="HTML",
        reply_markup=get_story_back_keyboard("story_menu_queue")
    )


@router.callback_query(F.data == "story_view_queue_list")
async def cb_story_view_queue_list(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = callback.from_user.id
    queue_items = await db_manager.get_user_story_queue(user_id)

    if not queue_items:
        text = f"""
{QUEUE} <b>Navbatdagi E'lonlar Ro'yxati:</b>

Hozirda navbatda kutayotgan e'lonlar yo'q.
Kanallaringizga yangi $700+ sara e'lonlar tushganda, ular avtomatik shu yerda navbatga joylashadi.
"""
    else:
        text = f"{QUEUE} <b>Navbatdagi E'lonlar Ro'yxati ({len(queue_items)} ta):</b>\n\n"
        for i, item in enumerate(queue_items[:10], 1):
            price_str = f"${item.price:g}" if item.price else "Narxsiz"
            district_str = f"{LOCATION_RED} {item.district}" if item.district else ""
            score_str = f"{TROPHY} {item.score} ball" if item.score else ""
            try:
                dt_utc = datetime.fromisoformat(item.scheduled_at.replace(' ', 'T')).replace(tzinfo=timezone.utc)
                dt_uzb = dt_utc.astimezone(UZB_TZ).strftime("%H:%M (%d-%b)")
            except Exception:
                dt_uzb = item.scheduled_at

            text += f"{i}. <b>{price_str}</b> | {district_str} {score_str}\n   {CLOCK} Rejalashtirilgan: <code>{dt_uzb}</code>\n   {CHANNEL} Manba: {item.source_channel}\n\n"

    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_back_keyboard("story_menu_queue")
    )



# --- PRICE & FILTER SETTINGS ---

@router.callback_query(F.data == "story_menu_filters")
async def cb_story_menu_filters(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)

    text = f"""
{SETTINGS} <b>Narx va Sifat Filtrlari Sozlamasi:</b>

Bu yerda kanaldan aynan qanday postlar saralanib Istoriyaga chiqishini sozlashingiz mumkin:

├ {MONEY} <b>Minimal narx:</b> <b>${st.min_price:g}</b> <i>(undan arzon variantlar avtomatik tashlanadi)</i>
├ {IMAGE} <b>Rasmli postlar:</b> <b>{"Majburiy " + SUCCESS if st.require_photos else "Ixtiyoriy " + SWITCH_OFF}</b>
├ {MONEY} <b>Narx ko'rsatilgan bo'lishi:</b> <b>{"Majburiy " + SUCCESS if st.require_price else "Ixtiyoriy " + SWITCH_OFF}</b>
└ {BAN} <b>Qidiruv/Mijoz talabi filtr:</b> <b>{"Faol " + SUCCESS + " (chetlab o'tiladi)" if st.filter_demands else "O'chiq " + SWITCH_OFF}</b>

{ARROW_DOWN} <i>Quyidagi tugmalar orqali sozlamalarni o'zgartiring:</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_filters_keyboard(st)
    )


@router.callback_query(F.data == "story_filter_price_menu")
async def cb_story_filter_price_menu(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)

    text = f"""
{MONEY} <b>Minimal Narx Chegarasini Tanlang:</b>

Hozirgi chegara: <b>${st.min_price:g}</b>

<i>Tayyor narxlardan birini tanlang yoki o'zingiz xohlagan summani kiriting:</i>
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_price_preset_keyboard(st.min_price)
    )


@router.callback_query(F.data.startswith("story_set_price_"))
async def cb_story_set_price_preset(callback: CallbackQuery, state: FSMContext):
    action = callback.data.replace("story_set_price_", "")
    user_id = callback.from_user.id

    if action == "custom":
        await safe_answer(callback)
        await state.set_state(StorySettingsSG.waiting_for_custom_price)
        text = f"""
{SIGNATURE} <b>Ixtiyoriy minimal narxni kiriting:</b>

Faqat raqam yuboring (masalan: <code>700</code>, <code>850</code>, <code>1000</code>):
"""
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_story_cancel_keyboard())
        return

    try:
        new_price = float(action)
        st = await db_manager.get_story_settings(user_id)
        st.min_price = new_price
        await db_manager.save_story_settings(st)
        await safe_answer(callback, f"Minimal narx o'rnatildi: ${new_price:g}")
        await cb_story_menu_filters(callback, state)
    except Exception as e:
        await safe_answer(callback, f"Xatolik: {e}", show_alert=True)


@router.message(StorySettingsSG.waiting_for_custom_price)
async def process_custom_price_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, summani raqam ko'rinishida yuboring.", reply_markup=get_story_cancel_keyboard())
        return

    raw = re.sub(r'[^\d.]', '', message.text.strip())
    try:
        val = float(raw)
        if val <= 0:
            raise ValueError()
        user_id = message.from_user.id
        st = await db_manager.get_story_settings(user_id)
        st.min_price = val
        await db_manager.save_story_settings(st)
        await state.clear()

        await message.answer(f"{SUCCESS} <b>Minimal narx o'rnatildi: ${val:g}</b>", parse_mode="HTML")
        text, kb = await render_story_main_menu(user_id)
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await message.answer(f"{WARN} Noto'g'ri narx formati! Musbat raqam kiriting (masalan: 700):", reply_markup=get_story_cancel_keyboard())


@router.callback_query(F.data == "story_toggle_photos")
async def cb_story_toggle_photos(callback: CallbackQuery):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    st.require_photos = not st.require_photos
    await db_manager.save_story_settings(st)
    status_str = "MAJBURIY" if st.require_photos else "IXTIYORIY"
    await safe_answer(callback, f"Rasmli postlar: {status_str}")
    await callback.message.edit_reply_markup(reply_markup=get_story_filters_keyboard(st))


@router.callback_query(F.data == "story_toggle_price_req")
async def cb_story_toggle_price_req(callback: CallbackQuery):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    st.require_price = not st.require_price
    await db_manager.save_story_settings(st)
    status_str = "MAJBURIY" if st.require_price else "IXTIYORIY"
    await safe_answer(callback, f"Narx bo'lishi: {status_str}")
    await callback.message.edit_reply_markup(reply_markup=get_story_filters_keyboard(st))


@router.callback_query(F.data == "story_toggle_demands")
async def cb_story_toggle_demands(callback: CallbackQuery):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    st.filter_demands = not st.filter_demands
    await db_manager.save_story_settings(st)
    status_str = "YOQILGAN" if st.filter_demands else "O'CHIRILGAN"
    await safe_answer(callback, f"Qidiruvlarni filtrlash: {status_str}")
    await callback.message.edit_reply_markup(reply_markup=get_story_filters_keyboard(st))


# --- DESIGN & BACKGROUND STYLE ---

@router.callback_query(F.data == "story_menu_design")
async def cb_story_menu_design(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)

    text = f"""
{PALETTE} <b>Istoriya Dizayni va Video Sozlamalari:</b>

Telegram Istoriyasida post orqasida ko'rinadigan fon uslubi va video davomiyligini tanlang:

├ {SWITCH_ON} <b>Telegram Yashil:</b> Nativ Telegram chat fonidagi yashil doodle naqsh (namunadagi kabi 100% bir xil!).
├ {IMAGE} <b>Kvartira Rasmini Xiralashtirish:</b> Kvartiraning o'z rasmini orqa fonga yumshoq blur qilib qo'yadi.
├ {STAR_SPARKLE} <b>To'q Lux Gradiyent:</b> Zamonaviy, boy va jiddiy qora uslub.
├ {DIAMOND} <b>Zumrad Yashil:</b> Yorqin va e'tiborni tortuvchi zumrad gradiyent.
├ {CLOCK} <b>Video Davomiyligi:</b> <b>{st.video_duration} soniya</b> <i>(15s dan 40s gacha)</i>
└ {AUDIO} <b>Fon Musiqasi:</b> <b>20 ta Luxury Trek (Stereo AAC, Avto-Loop)</b>

{ARROW_DOWN} <i>Kerakli parametrni tanlang:</i>
"""
    await safe_edit_text(
        callback.message,
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_design_keyboard(st.background_style, st.video_duration, False)
    )


@router.callback_query(F.data.startswith("story_set_style_"))
async def cb_story_set_style(callback: CallbackQuery):
    style_code = callback.data.replace("story_set_style_", "")
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    st.background_style = style_code
    await db_manager.save_story_settings(st)
    await safe_answer(callback, f"Fon uslubi tanlandi: {style_code}")
    await callback.message.edit_reply_markup(reply_markup=get_story_design_keyboard(st.background_style, st.video_duration, False))


@router.callback_query(F.data == "story_music_info")
async def cb_story_music_info(callback: CallbackQuery):
    await safe_answer(
        callback,
        "🎵 20 ta Luxury Lounge & Chillout treklari har bir videoga ketma-ket avtomatik ulanadi (takrorlanmaslik kafolati bilan)!",
        show_alert=True
    )


@router.callback_query(F.data == "story_toggle_ai_voice")
async def cb_story_toggle_ai_voice(callback: CallbackQuery):
    await safe_answer(
        callback,
        "AI Ovozli diktor o'chirilgan! Istoriyalarda faqat yuqori sifatli Luxury musiqa ishlatiladi.",
        show_alert=True
    )


@router.callback_query(F.data == "story_menu_duration")
async def cb_story_menu_duration(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)

    text = f"""
{CLOCK} <b>Telegram Istoriya Video Davomiyligi Sozlamasi:</b>

Hozirgi davomiylik: <b>{st.video_duration} soniya</b>

<i>Telegram Stories uchun video davomiyligi qat'iy ravishda <b>minimum 15 sekund</b> va <b>maximum 40 sekund</b> bo'lishi kerak.</i>

Tayyor tugmalardan birini tanlang, qadamlar bilan o'zgartiring yoki o'zingiz xohlagan sonni kiriting:
"""
    await callback.message.edit_text(
        text=text,
        parse_mode="HTML",
        reply_markup=get_story_duration_keyboard(st.video_duration)
    )


@router.callback_query(F.data.startswith("story_set_duration_") & ~F.data.endswith("_custom"))
async def cb_story_set_duration(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        val = int(callback.data.replace("story_set_duration_", ""))
        val = max(15, min(40, val))
        st = await db_manager.get_story_settings(user_id)
        st.video_duration = val
        await db_manager.save_story_settings(st)
        await safe_answer(callback, f"Video davomiyligi: {val} soniya o'rnatildi")
        await callback.message.edit_reply_markup(reply_markup=get_story_duration_keyboard(st.video_duration))
    except Exception as e:
        await safe_answer(callback, f"Xatolik: {e}", show_alert=True)


@router.callback_query(F.data == "story_set_duration_custom")
async def cb_story_set_duration_custom(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StorySettingsSG.waiting_for_video_duration)
    text = f"""
{SIGNATURE} <b>Video istoriya davomiyligini kiriting (sekundda):</b>

Minimal: <b>15 sekund</b>
Maksimal: <b>40 sekund</b>

<i>Faqat 15 dan 40 gacha bo'lgan butun son yuboring (masalan: <code>15</code>, <code>20</code>, <code>25</code>, <code>30</code>, <code>35</code>, <code>40</code>):</i>
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.message(StorySettingsSG.waiting_for_video_duration)
async def process_custom_video_duration_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, son yuboring (15 dan 40 gacha).", reply_markup=get_story_cancel_keyboard())
        return

    text_val = message.text.strip()
    try:
        val = int(text_val)
        if val < 15 or val > 40:
            await message.answer(
                f"{WARN} <b>Xato:</b> Video davomiyligi <b>minimum 15 sekund</b> va <b>maximum 40 sekund</b> bo'lishi shart!\n\nIltimos, 15 dan 40 gacha butun son kiriting:",
                parse_mode="HTML",
                reply_markup=get_story_cancel_keyboard()
            )
            return
    except ValueError:
        await message.answer(
            f"{WARN} Iltimos, faqat 15 dan 40 gacha butun son kiriting.",
            reply_markup=get_story_cancel_keyboard()
        )
        return

    await state.clear()
    user_id = message.from_user.id
    st = await db_manager.get_story_settings(user_id)
    st.video_duration = val
    await db_manager.save_story_settings(st)

    await message.answer(
        f"{SUCCESS} <b>Video istoriya davomiyligi muvaffaqiyatli saqlandi: {val} soniya!</b>",
        parse_mode="HTML"
    )

    design_text = f"""
{PALETTE} <b>Istoriya Dizayni va Video Sozlamalari:</b>

Telegram Istoriyasida post orqasida ko'rinadigan fon uslubi va video davomiyligini tanlang:

├ {SWITCH_ON} <b>Telegram Yashil:</b> Nativ Telegram chat fonidagi yashil doodle naqsh.
├ {IMAGE} <b>Kvartira Rasmini Xiralashtirish:</b> Kvartiraning o'z rasmini orqa fonga yumshoq blur qilib qo'yadi.
├ {STAR_SPARKLE} <b>To'q Lux Gradiyent:</b> Zamonaviy qora uslub.
├ {DIAMOND} <b>Zumrad Yashil:</b> Yorqin zumrad gradiyent.
└ {CLOCK} <b>Video Davomiyligi:</b> <b>{st.video_duration} soniya</b>

{ARROW_DOWN} <i>Kerakli sozlamani tanlang:</i>
"""
    await message.answer(
        text=design_text,
        parse_mode="HTML",
        reply_markup=get_story_design_keyboard(st.background_style, st.video_duration)
    )


# --- AUTO-MONITORING TOGGLE ---

@router.callback_query(F.data == "story_toggle_active")
async def cb_story_toggle_active(callback: CallbackQuery):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)

    # If turning ON, validate that an account is connected and a source channel is set
    if not st.is_active:
        session_info = await db_manager.get_user_session_info(user_id)
        if not session_info or not session_info.get("is_active"):
            await safe_answer(
                callback,
                "⚠️ Avto-monitoringni yoqish uchun avval Telegram hisobingizni ulashingiz lozim! ([Telegram Hisob] tugmasi)",
                show_alert=True
            )
            return

        if not st.source_channel:
            await safe_answer(
                callback,
                "⚠️ Avto-monitoringni yoqish uchun avval kamida bitta manba kanalni kiritishingiz lozim! ([Manba Kanallar] bo'limi)",
                show_alert=True
            )
            return

    st.is_active = not st.is_active
    await db_manager.save_story_settings(st)

    import asyncio
    if st.is_active:
        asyncio.create_task(story_cloner_service.start_monitor_for_user(user_id))
    else:
        story_cloner_service.stop_monitor_for_user(user_id)

    status_str = f"YOQILDI {SWITCH_ON}" if st.is_active else f"O'CHIRILDI {PAUSE}"
    await safe_answer(callback, f"Avto-monitoring {status_str}")

    text, kb = await render_story_main_menu(user_id)
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)


# --- STATS & HISTORY ---

@router.callback_query(F.data == "story_menu_stats")
async def cb_story_menu_stats(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = callback.from_user.id
    stats = await db_manager.get_story_stats(user_id)

    recent_items = stats.get("recent", [])
    recent_text = ""
    if recent_items:
        for idx, item in enumerate(recent_items, 1):
            p_val = f"${item['price']:g}" if item.get('price') else "Narxsiz"
            chan = html.escape(item.get('source_channel', ''))
            time_str = item.get('posted_at', '')[:16]
            recent_text += f"{idx}. <b>{chan}</b> (msg #{item['source_msg_id']}) — <b>{p_val}</b> ({time_str})\n"
    else:
        recent_text = "<i>Hozircha tarix mavjud emas.</i>\n"

    text = f"""
{STATS} <b>Istoriyalar Statistikasi va Tarixi:</b>

├ {CALENDAR} <b>Bugun joylangan:</b> {stats.get('today_posted', 0)} ta
├ {STATS_GROWTH} <b>Jami joylangan:</b> {stats.get('total_posted', 0)} ta
└ {FLASH_GREEN} <b>Holati:</b> Faol ishlamoqda

{DOCUMENT} <b>Oxirgi joylangan variantlar:</b>
{recent_text}
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_story_back_keyboard())


# --- HELP GUIDE ---

@router.callback_query(F.data == "story_menu_help")
async def cb_story_menu_help(callback: CallbackQuery):
    await safe_answer(callback)
    text = f"""
{INFO} <b>Story Cloner Haqida To'liq Qo'llanma:</b>

1. <b>Qanday ishlaydi?</b>
Tizim ko'chmas mulk kanallaridagi yangi xabarlarni soniya sayin kuzatadi. Har bir yangi postdan narxni ($700+) va rasmlarni aniqlaydi. Agar post mos kelsa, darhol sizning Telegram profilingizga Istoriya qilib joylaydi.

2. <b>Nega xuddi namunadagidek chiqadi?</b>
Biz Telegram MTProto ning eng ilg'or <code>InputMediaAreaChannelPost</code> texnologiyasidan foydalanamiz. Bu Telegram ilovasida xabarni avtomatik tarzda markaziy kartochka va pastki qismida kanal tugmasi ko'rinishida chiqaradi.

3. <b>Bosganda postga o'tishi qanday ta'minlanadi?</b>
Telegram Istoriyasida post ustiga yoki pastki <code>{CHANNEL} Kanal</code> tugmasiga bosgan har qanday foydalanuvchi to'g'ridan-to'g'ri o'sha original xabarga o'tadi!

4. <b>Akkaunt xavfsizligi:</b>
Sessiyangiz xavfsiz harbiy darajadagi AES-128 shifrlash orqali saqlanadi. Bot sizning shaxsiy xabarlaringizga kirmaydi va faqat siz belgilagan kanallardan istoriya chiqarish uchun xizmat qiladi.
"""
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=get_story_back_keyboard())


# --- TEST STORY EXECUTION HANDLER ---

@router.message(Command("story_test"))
@router.callback_query(F.data == "story_menu_test_post")
async def trigger_test_story_post(event: Union[Message, CallbackQuery], state: FSMContext):
    """Executes an instant test run creating a video story with music and channel post link"""
    await state.clear()
    user_id = event.from_user.id
    st = await db_manager.get_story_settings(user_id)
    duration = getattr(st, "video_duration", 25) or 25

    status_msg = None
    wait_text = (
        f"{LOADING} <b>{duration} soniyalik hashamatli musiqali video istoriya yaratilmoqda...</b>\n\n"
        "<i>Iltimos kuting: xona suratlari, Playwright kartochkasi, ambient crossfade va tanlangan relaks musiqa birlashtirilmoqda (taxminan 10-15 soniya)...</i>"
    )
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        status_msg = await event.message.answer(wait_text, parse_mode="HTML")
    else:
        status_msg = await event.answer(wait_text, parse_mode="HTML")

    # Check userbot session
    client = await story_cloner_service.get_user_client(user_id)
    if not client:
        await status_msg.edit_text(
            f"{ERROR} <b>Telegram hisob (Userbot) hali ulanmagan!</b>\n\n"
            f"Istoriyani o'z profilingizga joylash uchun avval <b>{MOBILE} Telegram Hisob</b> bo'limi orqali hisobingizni ulang.",
            parse_mode="HTML",
            reply_markup=get_story_auth_keyboard(is_auth=False)
        )
        return

    if not client.is_connected():
        try:
            await client.connect()
        except Exception as conn_err:
            logger.warning(f"Failed to reconnect client in trigger_test_story_post: {conn_err}")

    source_channel = (st.source_channel or "@realtor_abdulloh").strip()
    if source_channel.startswith("https://t.me/"):
        source_channel = "@" + source_channel[len("https://t.me/"):].strip("/")
    elif source_channel.startswith("t.me/"):
        source_channel = "@" + source_channel[len("t.me/"):].strip("/")
    elif not source_channel.startswith("@") and not source_channel.startswith("-100") and not source_channel.lstrip("-").isdigit():
        source_channel = f"@{source_channel}"

    try:
        entity = await client.get_entity(source_channel)
        target_msg = None
        async for m in client.iter_messages(entity, limit=20):
            if m.photo or m.grouped_id:
                price = story_cloner_service.extract_price(m.message or "")
                if price and price >= st.min_price:
                    target_msg = m
                    break
                elif not target_msg and (m.photo or m.grouped_id):
                    target_msg = m

        if not target_msg:
            await status_msg.edit_text(
                f"{ERROR} <b>{source_channel} kanalida mos rasmli e'lon topilmadi!</b>",
                parse_mode="HTML",
                reply_markup=get_story_back_keyboard()
            )
            return

        success, story_id, info, story_url = await story_cloner_service.post_story_from_channel(
            user_id=user_id,
            source_channel=source_channel,
            msg_id=target_msg.id,
            target_type=st.target_type,
            target_channel=st.target_channel,
            bg_style=st.background_style
        )

        if success:
            url_text = f"\n{LINK} <a href='{story_url}'>Telegramda Istoriyani Ko'rish</a>" if story_url else ""
            res_text = f"""
{SUCCESS} <b>{duration} soniyalik hashamatli video istoriya muvaffaqiyatli joylandi!</b>

├ {TAG} <b>Story ID:</b> <code>{story_id}</code>
├ {CHANNEL} <b>Manba:</b> {source_channel} (xabar #{target_msg.id})
├ {TIMER} <b>Davomiyligi:</b> {duration} soniya (Full HD 1080x1920)
├ {AUDIO} <b>Musiqa:</b> Luxury Lounge / Chillout stereo AAC
└ {TARGET} <b>Interaktiv havola:</b> Telegram post kartochkasi orqali postga o'tish faol!{url_text}

{MOBILE} <i>Telegram mobil ilovangizda profilingizdagi hikoyani tekshirib ko'ring!</i>
"""
            await status_msg.edit_text(
                res_text,
                parse_mode="HTML",
                reply_markup=get_story_back_keyboard()
            )
        else:
            await status_msg.edit_text(
                f"{ERROR} <b>Istoriya joylashda xatolik:</b>\n\n<code>{info}</code>",
                parse_mode="HTML",
                reply_markup=get_story_back_keyboard()
            )
    except Exception as e:
        logger.exception("Error in trigger_test_story_post")
        await status_msg.edit_text(
            f"{ERROR} <b>Xatolik yuz berdi:</b>\n\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=get_story_back_keyboard()
        )

