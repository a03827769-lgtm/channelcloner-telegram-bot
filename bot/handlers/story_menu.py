import asyncio
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple, Union

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from bot.filters import WIZARD_INPUT
from bot.keyboards.story_keyboards import (
    STORY_COOLDOWN_PRESETS,
    STORY_PRICE_PRESETS,
    STORY_STYLE_CODES,
    get_story_auth_keyboard,
    get_story_back_keyboard,
    get_story_cancel_keyboard,
    get_story_channels_keyboard,
    get_story_cooldown_preset_keyboard,
    get_story_daily_limit_preset_keyboard,
    get_story_design_keyboard,
    get_story_duration_keyboard,
    get_story_filters_keyboard,
    get_story_logout_confirm_keyboard,
    get_story_main_menu_keyboard,
    get_story_price_preset_keyboard,
    get_story_queue_keyboard,
    get_story_vip_upgrade_keyboard,
)
from bot.states.story_states import StoryAuthSG, StorySettingsSG
from bot.utils import edit_or_send, html_escape, safe_answer, show_in_place
from config.limits import (
    STORY_DRIP_DELAY_MAX_MINUTES,
    STORY_MAX_PER_DAY_MAX,
    STORY_MAX_PER_DAY_MIN,
    STORY_VIDEO_DURATION_MAX,
    STORY_VIDEO_DURATION_MIN,
)
from database.db_manager import db_manager
from services.custom_emojis import (
    ARROW_DOWN, AUDIO, BAN, CALENDAR, CHANNEL, CLOCK, CROWN, DIAMOND, DOCUMENT, EDIT, ERROR, FLASH_GREEN,
    HISTORY_CLOCK, HOME, IMAGE, INFO, LINK, LOADING, LOCATION_RED, LOCK_LOCKED, LOCK_PASSWORD, MEDAL_BRONZE,
    MOBILE, MONEY, PALETTE, PARTY, PAUSE, PHONE, PIN, QUEUE, ROCKET, SERVER_CPU, SETTINGS, SIGNATURE, STAR_SPARKLE,
    STARS, STATS, STATS_GROWTH, SUCCESS, SWITCH_OFF, SWITCH_ON, TAG, TARGET, TIMER, TIP, TROPHY, USER_PROFILE,
    VIDEO, WARN,
)
from services.listing_analyzer import format_price_usd
from services.phone_utils import mask_phone_number
from services.story_cloner_service import story_cloner_service
from services.story_queue_service import NON_PREMIUM_DAILY_STORY_LIMIT, UZB_TZ, story_queue_service

logger = logging.getLogger(__name__)
router = Router(name="story_menu_router")
# The story tools are used in the private chat with the bot only
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

# Largest minimal price accepted by the price filter (USD)
STORY_MIN_PRICE_MAX = 10_000_000
# "Test Istoriya" renders a video: one run per user at a time and at most one start per minute
TEST_STORY_COOLDOWN_SECONDS = 60.0

_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_TG_LINK_PREFIX_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", re.IGNORECASE)
_INVITE_HASH_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")

STYLE_LABELS = {
    "telegram_green": f"{PALETTE} Telegram Yashil (Nativ)",
    "listing_blur": f"{IMAGE} Xiralashtirilgan Kvartira Rasmi",
    "luxury_dark": f"{CROWN} To'q Lux Gradiyent",
    "emerald": f"{DIAMOND} Zumrad Gradiyent",
}

_background_tasks: Set[asyncio.Task] = set()
_test_story_locks: Dict[int, asyncio.Lock] = {}
_last_test_story: Dict[int, float] = {}


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _restart_monitor_if_active(active: bool, user_id: int) -> None:
    if active:
        story_cloner_service.schedule_monitor_start(user_id)


def normalize_channel_ref(raw: Optional[str]) -> Optional[str]:
    """Canonical form of a channel reference typed by the user: '@username', '-100<id>' or an invite link
    'https://t.me/+HASH' (t.me/joinchat/HASH is accepted too). None when the text is none of these."""
    text = (raw or "").strip()
    if not text or len(text) > 256 or any(ch.isspace() for ch in text):
        return None
    link = _TG_LINK_PREFIX_RE.sub("", text).split("?")[0].strip("/")
    if link.startswith("+") or link.lower().startswith("joinchat/"):
        invite_hash = re.sub(r"^(?:\+|joinchat/)", "", link, flags=re.IGNORECASE).split("/")[0]
        return f"https://t.me/+{invite_hash}" if _INVITE_HASH_RE.fullmatch(invite_hash) else None
    if re.fullmatch(r"-?\d{5,}", link):
        raw_id = db_manager.normalize_peer_id(link)
        return f"-100{raw_id}" if raw_id else None
    parts = link.split("/")
    name = parts[1] if parts[0].lower() == "s" and len(parts) > 1 else parts[0]
    name = name.lstrip("@")
    return f"@{name}" if _USERNAME_RE.fullmatch(name) else None


def parse_price_input(raw: Optional[str]) -> Optional[float]:
    """Price typed by the user: '700', '$1,500', '1.500', '1 500', '850.50'. Groups of exactly three
    digits after '.', ',' or a space are thousands separators; None for anything else."""
    text = (raw or "").strip().lstrip("$").rstrip("$").strip()
    text = re.sub(r"(?i)\s*(usd|dollar|у\.е\.?)$", "", text)
    if re.fullmatch(r"\d{1,3}(?:[ ., ]\d{3})+", text):
        text = re.sub(r"[ ., ]", "", text)
    else:
        text = text.replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    value = float(text)
    return value if math.isfinite(value) and 0 < value <= STORY_MIN_PRICE_MAX else None


def _parse_int_suffix(data: Optional[str], prefix: str) -> Optional[int]:
    value = (data or "")[len(prefix):] if (data or "").startswith(prefix) else ""
    return int(value) if value.isdigit() else None


def get_story_vip_upgrade_text(user_tier: str = "free") -> str:
    tier_display = {
        "vip": f"{CROWN} VIP Cheksiz",
        "pro": f"{STARS} Pro Tarif",
        "free": f"{MEDAL_BRONZE} Bepul Sinov (Free Trial)"
    }.get(user_tier, html_escape(user_tier))

    return f"""
{CROWN} <b>VIP Cheksiz Tarif Talab Qilinadi!</b>

Sizning hozirgi tarifingiz: <b>{tier_display}</b>

{VIDEO} <b>Real Estate Auto-Story Cloner ($700+)</b> funksiyasi faqat <b>VIP Cheksiz</b> foydalanuvchilari uchun!

{STAR_SPARKLE} <b>VIP Tarif Imkoniyatlari:</b>
├ {HOME} <b>Avto-Istoriya Kloner:</b> kanallardagi belgilangan narxdan (standart $700) qimmat e'lonlarni avtomatik aniqlash
├ {VIDEO} <b>Video Istoriya:</b> {STORY_VIDEO_DURATION_MIN}–{STORY_VIDEO_DURATION_MAX} soniyalik kollaj va Ken Burns effektli video
├ {CLOCK} <b>Prime Time Navbat:</b> optimal ko'rish soatlarida avtomatik navbat bilan joylash
├ {TAG} <b>Aqlli Badjlar:</b> tuman, xonalar soni, maydoni va narx teglari
├ {SERVER_CPU} <b>14 Kunlik Xotira:</b> bir xil e'lon qayta joylanmaydi
├ {CHANNEL} <b>Cheksiz Kanallar Juftligi:</b> 999 tagacha kanalni parallel ko'chirish
└ {STAR_SPARKLE} <b>Telegram Premium Animatsion Emojilar</b>

<i>VIP tarifga o'ting va ko'chmas mulk biznesingizni yangi bosqichga olib chiqing:</i>
"""


async def get_story_vip_upgrade_text_and_keyboard(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    sub = await db_manager.get_user_subscription(user_id)
    tier = sub.tier if sub else "free"
    return get_story_vip_upgrade_text(tier), get_story_vip_upgrade_keyboard()


async def check_is_vip(user_id: int) -> bool:
    return await db_manager.is_vip(user_id)


class StoryVipMiddleware(BaseMiddleware):
    """Every Story Cloner handler is VIP-only: other users get the VIP upgrade prompt instead (and any
    unfinished story wizard is ended)."""
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

        if await check_is_vip(user.id):
            return await handler(event, data)

        state: Optional[FSMContext] = data.get("state")
        if state:
            await state.clear()
        await story_cloner_service.cancel_login(user.id)

        text, kb = await get_story_vip_upgrade_text_and_keyboard(user.id)
        if isinstance(event, CallbackQuery):
            await safe_answer(event, "Bu funksiya faqat VIP Cheksiz tarif egalari uchun!")
            try:
                await edit_or_send(event, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("Could not show the VIP upgrade prompt", exc_info=True)
        elif isinstance(event, Message):
            await event.answer(text=text, parse_mode="HTML", reply_markup=kb)
        return None


router.message.middleware(StoryVipMiddleware())
router.callback_query.middleware(StoryVipMiddleware())


async def _show(event: Union[CallbackQuery, Message], text: str, reply_markup: InlineKeyboardMarkup) -> None:
    """Shows a story screen: in place of the menu message after a button tap, as a reply to a message."""
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=reply_markup)


def _prime_window(st) -> str:
    return f"{st.prime_hours_start:02d}:00 - {st.prime_hours_end:02d}:00"


# --- MAIN MENU ---

async def render_story_main_menu(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Dashboard text and keyboard of the Story Cloner (the VIP prompt for other users)."""
    if not await check_is_vip(user_id):
        return await get_story_vip_upgrade_text_and_keyboard(user_id)

    session_info = await db_manager.get_user_session_info(user_id)
    is_auth = bool(session_info and session_info.get("is_active"))
    st = await db_manager.get_story_settings(user_id)
    stats = await db_manager.get_story_stats(user_id)
    extra_channels = await db_manager.get_story_source_channels(user_id)
    queue_items = await db_manager.get_user_story_queue(user_id)

    auth_status = f"{SUCCESS} Ulangan" if is_auth else f"{ERROR} Ulanmagan"
    if is_auth:
        user_name = html_escape(session_info.get("first_name", "") or "")
        phone_masked = html_escape(mask_phone_number(session_info.get("phone", "") or ""))
        account_line = f"├ {USER_PROFILE} <b>Hisob:</b> {user_name} ({phone_masked})"
    else:
        account_line = f"├ {USER_PROFILE} <b>Hisob:</b> <i>Ulanmagan</i>"

    src_channel = html_escape(st.source_title or st.source_channel) if st.source_channel else "<i>Sozlanmagan</i>"
    if extra_channels:
        src_line = f"├ {CHANNEL} <b>Manba kanallar:</b> {src_channel} <i>(+{len(extra_channels)} ta qo'shimcha)</i>"
    else:
        src_line = f"├ {CHANNEL} <b>Manba kanal:</b> {src_channel}"

    if st.target_type == "self":
        target_text = "Shaxsiy Istoriya (Profile Story)"
    else:
        target_text = f"Kanal ({html_escape(st.target_channel) if st.target_channel else 'Sozlanmagan'})"

    active_label = f"Faol (Kuzatuvda) {SWITCH_ON}" if st.is_active else f"To'xtatilgan {PAUSE}"
    prime_status = f"Yoqilgan ({_prime_window(st)}) {SUCCESS}" if st.prime_hours_enabled else f"24/7 (Cheklovsiz) {SWITCH_OFF}"
    badges_status = f"Yoqilgan {TAG}" if st.enable_smart_badges else f"O'chiq {SWITCH_OFF}"
    min_price = format_price_usd(st.min_price, "$0")

    text = f"""
{CROWN} <b>Real Estate Auto-Story Cloner — Boshqaruv Markazi</b>

Tizim kanallaringizdagi <b>{min_price}+</b> variantlarni avtomatik aniqlab, 14 kunlik xotira bilan takrorlanishlarsiz interaktiv video Istoriya qilib joylaydi.

{MOBILE} <b>Telegram Akkaunt (Userbot):</b>
{account_line}
└ {FLASH_GREEN} <b>Holati:</b> {auth_status}

{CHANNEL} <b>Kanal va Filtr Sozlamalari:</b>
{src_line}
├ {TARGET} <b>Joylash joyi:</b> {target_text}
├ {MONEY} <b>Minimal narx:</b> <b>{min_price} dan boshlanadigan</b>
├ {IMAGE} <b>Rasmli postlar:</b> {f"Majburiy {SUCCESS}" if st.require_photos else f"Ixtiyoriy {SWITCH_OFF}"}
├ {BAN} <b>Qidiruvlarni filtrlash:</b> {f"Faol {SUCCESS}" if st.filter_demands else f"O'chiq {SWITCH_OFF}"}
├ {PALETTE} <b>Istoriya foni:</b> {STYLE_LABELS.get(st.background_style, html_escape(st.background_style))}
└ {SETTINGS} <b>Avto-Monitoring:</b> <b>{active_label}</b>

{TIMER} <b>Smart Navbat & Prime Time:</b>
├ {CLOCK} <b>Prime Time:</b> <b>{prime_status}</b>
├ {HISTORY_CLOCK} <b>Oraliq:</b> {st.drip_delay_minutes} daq | kuniga max {st.max_stories_per_day} ta
├ {TAG} <b>Aqlli badjlar:</b> {badges_status}
└ {QUEUE} <b>Navbatdagi e'lonlar:</b> <b>{len(queue_items)} ta</b>

{STATS} <b>Statistika:</b>
├ {CALENDAR} Bugun joylangan: <b>{stats.get('today_posted', 0)} ta</b>
└ {STATS_GROWTH} Jami joylangan: <b>{stats.get('total_posted', 0)} ta</b>

{ARROW_DOWN} <i>Quyidagi tugmalardan birini tanlang:</i>
"""
    kb = get_story_main_menu_keyboard(is_auth=is_auth, is_active=st.is_active, has_source=bool(st.source_channel))
    return text, kb


@router.message(Command("story", "istoriya"))
@router.callback_query(F.data == "story_main_menu")
async def show_story_menu(event: Union[CallbackQuery, Message], state: FSMContext):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
    current_state = await state.get_state()
    await state.clear()
    user_id = event.from_user.id
    if current_state and current_state.startswith(StoryAuthSG.__name__):
        # "Bekor Qilish" during the account login: the temporary login client is released
        await story_cloner_service.cancel_login(user_id)
    text, kb = await render_story_main_menu(user_id)
    await _show(event, text, kb)


# --- TELEGRAM ACCOUNT (USERBOT) LOGIN ---

@router.callback_query(F.data == "story_menu_auth")
async def cb_story_auth_menu(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    if (await state.get_state() or "").startswith(StoryAuthSG.__name__):
        await story_cloner_service.cancel_login(callback.from_user.id)
    await state.clear()
    session_info = await db_manager.get_user_session_info(callback.from_user.id)
    is_auth = bool(session_info and session_info.get("is_active"))

    if is_auth:
        user_name = html_escape(session_info.get("first_name", "") or "")
        username = session_info.get("username", "") or ""
        phone = html_escape(mask_phone_number(session_info.get("phone", "") or ""))
        username_line = f"@{html_escape(username)}" if username else "Mavjud emas"
        text = f"""
{MOBILE} <b>Ulangan Telegram Akkaunt (Userbot):</b>

├ {USER_PROFILE} <b>Ism:</b> {user_name}
├ {LINK} <b>Username:</b> {username_line}
├ {PHONE} <b>Telefon:</b> <code>{phone}</code>
└ {SWITCH_ON} <b>Holati:</b> Ulangan va faol

<i>Ushbu hisob nomidan kanaldagi sara postlar Telegram Istoriyalariga avtomatik joylanadi.</i>
"""
    else:
        text = f"""
{MOBILE} <b>Telegram Akkauntni Ulash (Userbot):</b>

Saralangan variantlarni profilingizga yoki kanalingizga avtomatik Istoriya qilib joylash uchun Telegram akkauntingizni ulashingiz kerak.

{LOCK_LOCKED} <b>Xavfsizlik:</b>
- Sessiya kaliti shifrlangan holda saqlanadi.
- Hisobingiz faqat siz belgilagan kanallardagi e'lonlarni Istoriyaga joylash uchun ishlatiladi.
- Xohlagan vaqtda bitta tugma bilan hisobdan chiqishingiz mumkin (sessiya o'chiriladi).

Hisobni ulash uchun quyidagi tugmani bosing:
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_story_auth_keyboard(is_auth=is_auth))


@router.callback_query(F.data == "story_auth_start")
async def cb_story_auth_start(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StoryAuthSG.waiting_for_phone)
    text = f"""
{PHONE} <b>Telefon raqamingizni kiriting:</b>

Telegram akkauntingizga bog'langan telefon raqamini xalqaro formatda yuboring:
<i>(Masalan: <code>+998901234567</code>)</i>
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.message(StoryAuthSG.waiting_for_phone, WIZARD_INPUT)
async def process_story_phone_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, telefon raqamingizni matn shaklida yuboring.", reply_markup=get_story_cancel_keyboard())
        return

    phone = message.text.strip()[:32]
    user_id = message.from_user.id
    msg_wait = await message.answer(f"{LOADING} Telegramga ulanish va tasdiqlash kodi so'ralmoqda...")
    ok, res_msg = await story_cloner_service.request_otp_code(user_id=user_id, phone=phone)
    try:
        await msg_wait.delete()
    except Exception:
        logger.debug("Could not delete the progress message", exc_info=True)

    if not ok:
        await message.answer(f"{ERROR} <b>Xatolik:</b>\n{html_escape(res_msg)}\n\nQaytadan urinib ko'ring:",
                             parse_mode="HTML", reply_markup=get_story_cancel_keyboard())
        return

    await state.set_state(StoryAuthSG.waiting_for_code)
    text = f"""
{SUCCESS} <b>Tasdiqlash kodi yuborildi!</b>

Telegram ilovangizga <code>{html_escape(mask_phone_number(phone))}</code> raqami uchun kelgan kodni kiriting.

{WARN} <b>Muhim:</b> kod raqamlari orasiga bo'sh joy qo'yib yuboring (masalan: <code>1 2 3 4 5</code>). Kod bitta yaxlit xabar bo'lib yuborilsa, Telegram uni xavfsizlik uchun bekor qiladi.
"""
    await message.answer(text=text, parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.message(StoryAuthSG.waiting_for_code, WIZARD_INPUT)
async def process_story_code_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, tasdiqlash kodini matn shaklida yuboring.", reply_markup=get_story_cancel_keyboard())
        return

    code = message.text.strip()[:32]
    user_id = message.from_user.id
    try:
        await message.delete()  # the login code must not stay in the chat
    except Exception:
        logger.debug("Could not delete the login code message", exc_info=True)

    msg_wait = await message.answer(f"{LOADING} Kod tekshirilmoqda...")
    ok, res_msg, status = await story_cloner_service.submit_otp_code(user_id=user_id, code=code)
    try:
        await msg_wait.delete()
    except Exception:
        logger.debug("Could not delete the progress message", exc_info=True)

    if status == "success":
        await state.clear()
        text, kb = await render_story_main_menu(user_id)
        await message.answer(
            f"{PARTY} <b>Muborakbod etamiz!</b>\n\n{html_escape(res_msg)}\n"
            f"Endi bot sizning hisobingiz nomidan eng sara variantlarni Istoriyaga avtomatik joylaydi!",
            parse_mode="HTML"
        )
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
    elif status == "needs_2fa":
        await state.set_state(StoryAuthSG.waiting_for_2fa)
        await message.answer(
            f"{LOCK_PASSWORD} <b>Ikki bosqichli autentifikatsiya (2FA) yoqilgan!</b>\n\n"
            f"Telegram hisobingizning bulutli parolini (Cloud Password) kiriting:",
            parse_mode="HTML",
            reply_markup=get_story_cancel_keyboard()
        )
    else:
        await message.answer(f"{ERROR} <b>Xatolik:</b>\n{html_escape(res_msg)}\n\nKodni qaytadan kiriting:",
                             parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.message(StoryAuthSG.waiting_for_2fa, WIZARD_INPUT)
async def process_story_2fa_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, 2FA parolingizni matn shaklida yuboring.", reply_markup=get_story_cancel_keyboard())
        return

    password = message.text.strip()
    try:
        await message.delete()  # the password must not stay in the chat
    except Exception:
        logger.debug("Could not delete the password message", exc_info=True)

    user_id = message.from_user.id
    msg_wait = await message.answer(f"{LOADING} 2FA parol tekshirilmoqda...")
    ok, res_msg = await story_cloner_service.submit_2fa_password(user_id=user_id, password=password)
    try:
        await msg_wait.delete()
    except Exception:
        logger.debug("Could not delete the progress message", exc_info=True)

    if ok:
        await state.clear()
        text, kb = await render_story_main_menu(user_id)
        await message.answer(f"{PARTY} <b>Muvaffaqiyatli!</b>\n{html_escape(res_msg)}", parse_mode="HTML")
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(f"{ERROR} <b>Xatolik:</b>\n{html_escape(res_msg)}\n\nParolni qaytadan kiriting:",
                             parse_mode="HTML", reply_markup=get_story_cancel_keyboard())


@router.callback_query(F.data == "story_auth_logout_confirm")
async def cb_story_logout_confirm(callback: CallbackQuery):
    await safe_answer(callback)
    await edit_or_send(
        callback,
        f"{WARN} <b>Haqiqatan ham ulangan Telegram akkauntdan chiqmoqchimisiz?</b>\n\n"
        f"Avtomatik istoriya monitoringi to'xtatiladi va saqlangan sessiya o'chiriladi.",
        parse_mode="HTML",
        reply_markup=get_story_logout_confirm_keyboard()
    )


@router.callback_query(F.data == "story_auth_logout_yes")
async def cb_story_logout_yes(callback: CallbackQuery):
    await safe_answer(callback, "Hisob uzilmoqda...")
    await story_cloner_service.disconnect_user(callback.from_user.id)
    await edit_or_send(
        callback,
        f"{SUCCESS} <b>Telegram akkaunt uzildi!</b>\n\n"
        f"Sessiya o'chirildi va avtomatik monitoring to'xtatildi. Xohlagan vaqtda hisobni qayta ulashingiz mumkin.",
        parse_mode="HTML",
        reply_markup=get_story_back_keyboard()
    )


# --- SOURCE CHANNELS & STORY TARGET ---

async def _render_channels_screen(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = await db_manager.get_story_settings(user_id)
    extra_channels = await db_manager.get_story_source_channels(user_id)

    src = html_escape(st.source_title or st.source_channel) if st.source_channel else "<i>Sozlanmagan</i>"
    if st.target_type == "self":
        target_desc = "Sizning shaxsiy profilingiz"
    else:
        target_desc = f"Telegram kanal ({html_escape(st.target_channel) if st.target_channel else 'Sozlanmagan'})"

    extra_lines = ""
    if extra_channels:
        extra_lines = f"\n\n{PIN} <b>Qo'shimcha kuzatilayotgan kanallar:</b>\n"
        for i, ch in enumerate(extra_channels, 1):
            status = f"{SWITCH_ON} Faol" if ch.is_active else f"{PAUSE} To'xtatilgan"
            title_part = f" ({html_escape(ch.channel_title)})" if ch.channel_title and ch.channel_title != ch.channel_username else ""
            extra_lines += f"{i}. <code>{html_escape(ch.channel_username)}</code>{title_part} — {status}\n"

    text = f"""
{CHANNEL} <b>Manba Kanallar Boshqaruvi (Multi-Manba):</b>

Tizim bir nechta kanalni parallel kuzatadi. Bir xil e'lon turli kanallarda qayta chiqsa, 14 kunlik xotira uni aniqlab, takroriy istoriya joylamaydi.

├ {CHANNEL} <b>Asosiy kanal:</b> {src}
└ {TARGET} <b>Istoriya joylanadigan joy:</b> {target_desc}{extra_lines}

<i>Kanal qo'shish yoki o'chirish uchun quyidagi tugmalardan foydalaning:</i>
"""
    kb = get_story_channels_keyboard(st.source_channel, extra_channels, st.target_type, st.target_channel)
    return text, kb


@router.callback_query(F.data.in_(["story_menu_channels", "story_menu_channel"]))
async def cb_story_menu_channels(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    text, kb = await _render_channels_screen(callback.from_user.id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


_CHANNEL_INPUT_HINT = (
    "<i>Kanal username'i, havolasi yoki ID'si (masalan: <code>@toshkent_kvartiralari</code>, "
    "<code>https://t.me/toshkent_kvartiralari</code>, yopiq kanal uchun <code>https://t.me/+taklif_havola</code>)</i>"
)


@router.callback_query(F.data == "story_edit_channel")
async def cb_story_edit_channel(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StorySettingsSG.waiting_for_source_channel)
    await edit_or_send(
        callback,
        f"{CHANNEL} <b>Asosiy manba kanalni yuboring:</b>\n\n{_CHANNEL_INPUT_HINT}",
        parse_mode="HTML",
        reply_markup=get_story_cancel_keyboard("story_menu_channels")
    )


async def _resolve_source_input(message: Message) -> Optional[Tuple[str, str, Optional[int], bool]]:
    """(reference to store, title, raw channel id, verified) for a source channel typed by the user, or None
    after telling the user what is wrong. With the user's account connected the channel is resolved with it
    (and must exist); without it the syntactically valid reference is kept and verified later."""
    ref = normalize_channel_ref(message.text)
    if not ref:
        await message.answer(
            f"{WARN} <b>Noto'g'ri format.</b> Kanalni quyidagicha yuboring:\n{_CHANNEL_INPUT_HINT}",
            parse_mode="HTML",
            reply_markup=get_story_cancel_keyboard("story_menu_channels")
        )
        return None
    entity, code = await story_cloner_service.resolve_channel_for_user(message.from_user.id, ref)
    if entity is not None:
        label = story_cloner_service.channel_label(entity)
        return label, getattr(entity, "title", None) or label, db_manager.normalize_peer_id(getattr(entity, "id", None)), True
    if code == "not_connected":
        if ref.startswith("https://t.me/+"):
            await message.answer(
                f"{WARN} Yopiq kanal taklif havolasini faqat Telegram hisobingiz ulangandan keyin qo'shish mumkin "
                f"(hisob kanalga shu havola orqali a'zo bo'ladi).",
                parse_mode="HTML",
                reply_markup=get_story_cancel_keyboard("story_menu_channels")
            )
            return None
        return ref, ref, None, False
    reason = ("Bu kanal emas (foydalanuvchi yoki oddiy guruh)." if code == "not_channel"
              else "Kanal topilmadi yoki hisobingiz unga kira olmaydi. Havola / username to'g'riligini tekshiring.")
    await message.answer(f"{WARN} <b>{html_escape(reason)}</b>\n\n{_CHANNEL_INPUT_HINT}", parse_mode="HTML",
                         reply_markup=get_story_cancel_keyboard("story_menu_channels"))
    return None


@router.message(StorySettingsSG.waiting_for_source_channel, WIZARD_INPUT)
async def process_source_channel_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, kanal havolasi yoki username'ini matn shaklida yuboring.",
                             reply_markup=get_story_cancel_keyboard("story_menu_channels"))
        return
    resolved = await _resolve_source_input(message)
    if resolved is None:
        return
    label, title, raw_id, verified = resolved
    await state.clear()
    user_id = message.from_user.id
    # The previous channel's id is replaced (or cleared): stories never keep coming from the old channel
    st = await db_manager.update_story_settings(user_id, source_channel=label, source_title=title, source_id=raw_id)
    _restart_monitor_if_active(st.is_active, user_id)

    confirm = f"{SUCCESS} <b>Asosiy manba kanal saqlandi:</b> <code>{html_escape(label)}</code>"
    if title and title != label:
        confirm += f" (<b>{html_escape(title)}</b>)"
    if not verified:
        confirm += f"\n{INFO} <i>Telegram hisobingiz ulanmagan: kanal hisob ulangach tekshiriladi.</i>"
    await message.answer(confirm, parse_mode="HTML")
    text, kb = await _render_channels_screen(user_id)
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "story_add_extra_channel")
async def cb_story_add_extra_channel(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StorySettingsSG.waiting_for_add_channel)
    await edit_or_send(
        callback,
        f"{ROCKET} <b>Yangi manba kanal qo'shish (Multi-Manba):</b>\n\n{_CHANNEL_INPUT_HINT}",
        parse_mode="HTML",
        reply_markup=get_story_cancel_keyboard("story_menu_channels")
    )


@router.message(StorySettingsSG.waiting_for_add_channel, WIZARD_INPUT)
async def process_add_extra_channel_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(f"{WARN} Iltimos, kanal havolasi yoki username'ini matn shaklida yuboring.",
                             reply_markup=get_story_cancel_keyboard("story_menu_channels"))
        return
    resolved = await _resolve_source_input(message)
    if resolved is None:
        return
    label, title, raw_id, verified = resolved
    await state.clear()
    user_id = message.from_user.id

    row_id = await db_manager.add_story_source_channel(user_id, label, title, raw_id)
    if row_id:
        st = await db_manager.get_story_settings(user_id)
        _restart_monitor_if_active(st.is_active, user_id)
        note = "" if verified else f"\n{INFO} <i>Kanal Telegram hisobingiz ulangach tekshiriladi.</i>"
        await message.answer(
            f"{SUCCESS} <b>Yangi manba kanal qo'shildi:</b> <code>{html_escape(label)}</code> (<b>{html_escape(title)}</b>){note}",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"{INFO} Ushbu kanal allaqachon ro'yxatda: <code>{html_escape(label)}</code>", parse_mode="HTML")

    text, kb = await _render_channels_screen(user_id)
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("story_del_src_"))
async def cb_story_del_src(callback: CallbackQuery):
    user_id = callback.from_user.id
    ch_id = _parse_int_suffix(callback.data, "story_del_src_")
    if ch_id is None or not await db_manager.delete_story_source_channel(user_id, ch_id):
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    await safe_answer(callback, "Kanal o'chirildi")
    st = await db_manager.get_story_settings(user_id)
    _restart_monitor_if_active(st.is_active, user_id)
    text, kb = await _render_channels_screen(user_id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("story_toggle_src_"))
async def cb_story_toggle_src(callback: CallbackQuery):
    user_id = callback.from_user.id
    ch_id = _parse_int_suffix(callback.data, "story_toggle_src_")
    channels = await db_manager.get_story_source_channels(user_id)
    channel = next((c for c in channels if c.id == ch_id), None) if ch_id is not None else None
    if channel is None:
        await safe_answer(callback, "Kanal topilmadi!", show_alert=True)
        return
    await db_manager.set_story_source_channel_active(user_id, channel.id, not channel.is_active)
    await safe_answer(callback, "Kanal to'xtatildi" if channel.is_active else "Kanal yoqildi")
    st = await db_manager.get_story_settings(user_id)
    _restart_monitor_if_active(st.is_active, user_id)
    text, kb = await _render_channels_screen(user_id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "story_channel_primary_info")
async def cb_story_primary_info(callback: CallbackQuery):
    await safe_answer(callback, "Bu asosiy manba kanal. Uni o'zgartirish uchun 'O'zgartirish' tugmasini bosing.", show_alert=True)


async def _ask_target_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StorySettingsSG.waiting_for_target_channel)
    await edit_or_send(
        callback,
        f"{TARGET} <b>Istoriyalar joylanadigan kanalni yuboring:</b>\n\n"
        f"Ulangan Telegram hisobingiz shu kanalning administratori bo'lishi va istoriya joylash huquqiga ega "
        f"bo'lishi kerak (kanal istoriyalari uchun Telegram odatda kanal Boost darajasini ham talab qiladi).\n\n"
        f"{_CHANNEL_INPUT_HINT}",
        parse_mode="HTML",
        reply_markup=get_story_cancel_keyboard("story_menu_channels")
    )


@router.callback_query(F.data == "story_toggle_target")
async def cb_story_toggle_target(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    if st.target_type == "channel":
        await db_manager.update_story_settings(user_id, target_type="self")
        await safe_answer(callback, "Istoriyalar shaxsiy profilingizga joylanadi")
        text, kb = await _render_channels_screen(user_id)
        await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)
        return
    if not st.target_channel:
        # A channel target is switched on only after a usable channel was given
        await safe_answer(callback)
        await _ask_target_channel(callback, state)
        return
    await db_manager.update_story_settings(user_id, target_type="channel")
    await safe_answer(callback, "Istoriyalar kanalga joylanadi")
    text, kb = await _render_channels_screen(user_id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "story_set_target_channel")
async def cb_story_set_target_channel(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await _ask_target_channel(callback, state)


@router.message(StorySettingsSG.waiting_for_target_channel, WIZARD_INPUT)
async def process_target_channel_input(message: Message, state: FSMContext):
    ref = normalize_channel_ref(message.text)
    if not ref:
        await message.answer(f"{WARN} <b>Noto'g'ri format.</b>\n{_CHANNEL_INPUT_HINT}", parse_mode="HTML",
                             reply_markup=get_story_cancel_keyboard("story_menu_channels"))
        return
    user_id = message.from_user.id
    msg_wait = await message.answer(f"{LOADING} Kanal va istoriya joylash huquqi tekshirilmoqda...")
    entity, reason = await story_cloner_service.check_story_target(user_id, ref)
    try:
        await msg_wait.delete()
    except Exception:
        logger.debug("Could not delete the progress message", exc_info=True)
    if entity is None:
        await message.answer(
            f"{ERROR} <b>Bu kanalga istoriya joylab bo'lmaydi:</b>\n{html_escape(reason or '')}\n\n"
            f"Boshqa kanal yuboring yoki bekor qiling.",
            parse_mode="HTML",
            reply_markup=get_story_cancel_keyboard("story_menu_channels")
        )
        return

    await state.clear()
    label = story_cloner_service.channel_label(entity)
    st = await db_manager.update_story_settings(
        user_id, target_type="channel", target_channel=label,
        target_id=db_manager.normalize_peer_id(getattr(entity, "id", None))
    )
    _restart_monitor_if_active(st.is_active, user_id)
    title = getattr(entity, "title", None) or label
    await message.answer(f"{SUCCESS} <b>Istoriyalar endi kanalga joylanadi:</b> {html_escape(title)} "
                         f"(<code>{html_escape(label)}</code>)", parse_mode="HTML")
    text, kb = await _render_channels_screen(user_id)
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)


# --- QUEUE & PRIME TIME ---

async def _render_queue_screen(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = await db_manager.get_story_settings(user_id)
    queue_items = await db_manager.get_user_story_queue(user_id)

    window = _prime_window(st)
    prime_str = f"YOQILGAN {SUCCESS} ({window} Toshkent vaqti)" if st.prime_hours_enabled else f"O'CHIRILGAN {SWITCH_OFF} (24/7)"
    badges_str = f"YOQILGAN {TAG} ({DIAMOND} Premium, {LOCATION_RED} Tuman, {HOME} Xona)" if st.enable_smart_badges else f"O'CHIRILGAN {SWITCH_OFF}"
    pin_str = f"YOQILGAN {SUCCESS} (Profilda qoladi)" if st.pin_to_profile else f"O'CHIRILGAN {SWITCH_OFF} (Faqat arxivga tushadi)"
    now_uzb = story_queue_service.get_uzb_now().strftime("%H:%M")
    night_note = (f"{TIP} <i>Prime Time oynasidan tashqarida chiqqan e'lonlar navbatga olinadi va oyna ochilganda "
                  f"sifat bali bo'yicha ketma-ket joylanadi.</i>") if st.prime_hours_enabled else ""

    text = f"""
{TIMER} <b>Smart Navbat va Prime Time Sozlamalari:</b>

├ {CLOCK} <b>Prime Time rejimi:</b> {prime_str}
├ {HISTORY_CLOCK} <b>Istoriyalar oralig'i:</b> <b>har {st.drip_delay_minutes} daqiqada</b>
├ {CALENDAR} <b>Kunlik maksimal limit:</b> <b>{st.max_stories_per_day} ta istoriya</b>
├ {TAG} <b>Aqlli badjlar:</b> {badges_str}
├ {PIN} <b>Profilga saqlash (Pin):</b> {pin_str}
├ {CLOCK} <b>Hozirgi Toshkent vaqti:</b> <b>{now_uzb}</b>
└ {QUEUE} <b>Navbatda kutayotgan e'lonlar:</b> <b>{len(queue_items)} ta</b>

{night_note}
"""
    return text, get_story_queue_keyboard(st, len(queue_items))


async def _render_filters_screen(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = await db_manager.get_story_settings(user_id)
    text = f"""
{SETTINGS} <b>Narx va Sifat Filtrlari:</b>

Kanaldan qanday postlar Istoriyaga chiqishini sozlang:

├ {MONEY} <b>Minimal narx:</b> <b>{format_price_usd(st.min_price, '$0')}</b> <i>(arzonroq variantlar o'tkazib yuboriladi)</i>
├ {IMAGE} <b>Rasmli postlar:</b> <b>{"Majburiy " + SUCCESS if st.require_photos else "Ixtiyoriy " + SWITCH_OFF}</b>
├ {MONEY} <b>Narx ko'rsatilgan bo'lishi:</b> <b>{"Majburiy " + SUCCESS if st.require_price else "Ixtiyoriy " + SWITCH_OFF}</b>
├ {BAN} <b>Mijoz talablari (qidiruv postlari):</b> <b>{"Filtrlanadi " + SUCCESS if st.filter_demands else "Filtrlanmaydi " + SWITCH_OFF}</b>
└ {TAG} <b>Aqlli badjlar:</b> <b>{"Yoqilgan " + SUCCESS if st.enable_smart_badges else "O'chiq " + SWITCH_OFF}</b>

{ARROW_DOWN} <i>Quyidagi tugmalar orqali sozlamalarni o'zgartiring:</i>
"""
    return text, get_story_filters_keyboard(st)


@router.callback_query(F.data == "story_menu_queue")
async def cb_story_menu_queue(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    text, kb = await _render_queue_screen(callback.from_user.id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


async def _toggle_setting(callback: CallbackQuery, field: str, on_text: str, off_text: str,
                          render: Callable[[int], Awaitable[Tuple[str, InlineKeyboardMarkup]]]) -> None:
    """Flips a boolean story setting and redraws the screen (text and keyboard) it was changed on."""
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)
    new_value = not bool(getattr(st, field))
    await db_manager.update_story_settings(user_id, **{field: new_value})
    await safe_answer(callback, on_text if new_value else off_text)
    text, kb = await render(user_id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "story_toggle_prime_hours")
async def cb_story_toggle_prime_hours(callback: CallbackQuery):
    await _toggle_setting(callback, "prime_hours_enabled", "Prime Time: YOQILDI", "Prime Time: O'CHIRILDI (24/7)", _render_queue_screen)


@router.callback_query(F.data.in_(["story_toggle_badges_q", "story_toggle_badges"]))
async def cb_story_toggle_badges(callback: CallbackQuery):
    await _toggle_setting(callback, "enable_smart_badges", "Aqlli badjlar: YOQILDI", "Aqlli badjlar: O'CHIRILDI", _render_queue_screen)


@router.callback_query(F.data == "story_toggle_badges_f")
async def cb_story_toggle_badges_filters(callback: CallbackQuery):
    await _toggle_setting(callback, "enable_smart_badges", "Aqlli badjlar: YOQILDI", "Aqlli badjlar: O'CHIRILDI", _render_filters_screen)


@router.callback_query(F.data == "story_toggle_pin")
async def cb_story_toggle_pin(callback: CallbackQuery):
    await _toggle_setting(callback, "pin_to_profile", "Profilga saqlash: YOQILDI", "Profilga saqlash: O'CHIRILDI (arxiv)", _render_queue_screen)


@router.callback_query(F.data == "story_menu_cooldown")
async def cb_story_menu_cooldown(callback: CallbackQuery):
    await safe_answer(callback)
    st = await db_manager.get_story_settings(callback.from_user.id)
    await edit_or_send(
        callback,
        f"{TIMER} <b>Istoriyalar orasidagi vaqt oralig'ini tanlang:</b>\n\n"
        f"Hozirgi oraliq: <b>{st.drip_delay_minutes} daqiqa</b>\n\n"
        f"<i>Obunachilarni zeriktirmaslik uchun kamida 30-45 daqiqalik oraliq tavsiya etiladi.</i>",
        parse_mode="HTML",
        reply_markup=get_story_cooldown_preset_keyboard(st.drip_delay_minutes)
    )


@router.callback_query(F.data.startswith("story_set_cooldown_"))
async def cb_story_set_cooldown(callback: CallbackQuery, state: FSMContext):
    value = _parse_int_suffix(callback.data, "story_set_cooldown_")
    if value is None or value not in STORY_COOLDOWN_PRESETS or not 0 <= value <= STORY_DRIP_DELAY_MAX_MINUTES:
        await safe_answer(callback, "Noto'g'ri qiymat!", show_alert=True)
        return
    await db_manager.update_story_settings(callback.from_user.id, drip_delay_minutes=value)
    await safe_answer(callback, f"Oraliq: {value} daqiqa o'rnatildi")
    await cb_story_menu_queue(callback, state)


def _daily_limit_text(current: int) -> str:
    return f"""
{CALENDAR} <b>Bir kundagi maksimal istoriyalar soni:</b>

Hozirgi chegara: <b>kuniga max {current} ta</b>

{STARS} <b>Telegram limitlari:</b>
├ {CROWN} <b>Telegram Premium hisob:</b> kuniga <b>{STORY_MAX_PER_DAY_MAX} tagacha</b>
├ {USER_PROFILE} <b>Oddiy hisob:</b> kuniga <b>{NON_PREMIUM_DAILY_STORY_LIMIT} ta</b> (tizim buni avtomatik hisobga oladi)
└ {CHANNEL} <b>Kanallar:</b> kanalning Boost darajasiga bog'liq

{TIP} <i>Ko'chmas mulk profillari uchun kuniga 5–30 ta eng sara variant tavsiya etiladi.</i>
"""


@router.callback_query(F.data == "story_menu_daily_limit")
async def cb_story_menu_daily_limit(callback: CallbackQuery):
    await safe_answer(callback)
    st = await db_manager.get_story_settings(callback.from_user.id)
    await edit_or_send(callback, _daily_limit_text(st.max_stories_per_day), parse_mode="HTML",
                       reply_markup=get_story_daily_limit_preset_keyboard(st.max_stories_per_day))


@router.callback_query(F.data.startswith("story_set_limit_"))
async def cb_story_set_limit(callback: CallbackQuery, state: FSMContext):
    value = _parse_int_suffix(callback.data, "story_set_limit_")
    if value is None or not STORY_MAX_PER_DAY_MIN <= value <= STORY_MAX_PER_DAY_MAX:
        await safe_answer(callback, "Noto'g'ri qiymat!", show_alert=True)
        return
    await db_manager.update_story_settings(callback.from_user.id, max_stories_per_day=value)
    await safe_answer(callback, f"Kunlik limit: {value} ta o'rnatildi")
    await cb_story_menu_queue(callback, state)


@router.callback_query(F.data == "story_custom_daily_limit")
async def cb_story_custom_daily_limit(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StorySettingsSG.waiting_for_custom_daily_limit)
    await edit_or_send(
        callback,
        f"{EDIT} <b>Kunlik maksimal istoriyalar sonini kiriting:</b>\n\n"
        f"<b>{STORY_MAX_PER_DAY_MIN}</b> dan <b>{STORY_MAX_PER_DAY_MAX}</b> gacha butun son yuboring "
        f"<i>(masalan: 10, 25, 50)</i>:",
        parse_mode="HTML",
        reply_markup=get_story_cancel_keyboard("story_menu_queue")
    )


@router.message(StorySettingsSG.waiting_for_custom_daily_limit, WIZARD_INPUT)
async def process_custom_daily_limit(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer(
            f"{ERROR} <b>Noto'g'ri qiymat!</b> Iltimos, {STORY_MAX_PER_DAY_MIN} dan {STORY_MAX_PER_DAY_MAX} gacha butun son kiriting.",
            parse_mode="HTML", reply_markup=get_story_cancel_keyboard("story_menu_queue")
        )
        return
    value = int(text)
    if not STORY_MAX_PER_DAY_MIN <= value <= STORY_MAX_PER_DAY_MAX:
        await message.answer(
            f"{ERROR} <b>Cheklovdan oshib ketdi!</b> Kunlik istoriya limiti <b>{STORY_MAX_PER_DAY_MIN} dan "
            f"{STORY_MAX_PER_DAY_MAX} gacha</b> bo'lishi kerak.",
            parse_mode="HTML", reply_markup=get_story_cancel_keyboard("story_menu_queue")
        )
        return
    await db_manager.update_story_settings(message.from_user.id, max_stories_per_day=value)
    await state.clear()
    await message.answer(
        f"{SUCCESS} <b>Kunlik limit saqlandi:</b> bir kunda ko'pi bilan <b>{value} ta</b> istoriya joylanadi.",
        parse_mode="HTML",
        reply_markup=get_story_back_keyboard("story_menu_queue")
    )


def _format_queue_time(scheduled_at: Optional[str]) -> str:
    if not scheduled_at:
        return "—"
    try:
        dt_utc = datetime.fromisoformat(str(scheduled_at).replace(" ", "T"))
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(UZB_TZ).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return html_escape(str(scheduled_at))


@router.callback_query(F.data == "story_view_queue_list")
async def cb_story_view_queue_list(callback: CallbackQuery):
    await safe_answer(callback)
    queue_items = await db_manager.get_user_story_queue(callback.from_user.id)

    if not queue_items:
        text = (f"{QUEUE} <b>Navbatdagi E'lonlar:</b>\n\nHozir navbatda e'lon yo'q. Kanallaringizga mos e'lonlar "
                f"tushganda ular shu yerda ko'rinadi.")
    else:
        lines = [f"{QUEUE} <b>Navbatdagi E'lonlar ({len(queue_items)} ta):</b>\n"]
        for i, item in enumerate(queue_items[:10], 1):
            district = f"{LOCATION_RED} {html_escape(item.district)}" if item.district else ""
            score = f"{TROPHY} {item.score} ball" if item.score else ""
            lines.append(
                f"{i}. <b>{format_price_usd(item.price, 'Narxsiz')}</b> {district} {score}\n"
                f"   {CLOCK} Rejalashtirilgan (Toshkent): <code>{_format_queue_time(item.scheduled_at)}</code>\n"
                f"   {CHANNEL} Manba: {html_escape(item.source_channel or '')}\n"
            )
        if len(queue_items) > 10:
            lines.append(f"<i>... va yana {len(queue_items) - 10} ta</i>")
        text = "\n".join(lines)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_story_back_keyboard("story_menu_queue"))


# --- PRICE & FILTERS ---

@router.callback_query(F.data == "story_menu_filters")
async def cb_story_menu_filters(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    text, kb = await _render_filters_screen(callback.from_user.id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "story_filter_price_menu")
async def cb_story_filter_price_menu(callback: CallbackQuery):
    await safe_answer(callback)
    st = await db_manager.get_story_settings(callback.from_user.id)
    await edit_or_send(
        callback,
        f"{MONEY} <b>Minimal narx chegarasini tanlang:</b>\n\nHozirgi chegara: <b>{format_price_usd(st.min_price, '$0')}</b>\n\n"
        f"<i>Tayyor narxlardan birini tanlang yoki o'zingiz summa kiriting:</i>",
        parse_mode="HTML",
        reply_markup=get_story_price_preset_keyboard(st.min_price)
    )


@router.callback_query(F.data.startswith("story_set_price_"))
async def cb_story_set_price_preset(callback: CallbackQuery, state: FSMContext):
    action = (callback.data or "")[len("story_set_price_"):]
    if action == "custom":
        await safe_answer(callback)
        await state.set_state(StorySettingsSG.waiting_for_custom_price)
        await edit_or_send(
            callback,
            f"{SIGNATURE} <b>Minimal narxni kiriting (USD):</b>\n\nFaqat raqam yuboring (masalan: <code>700</code>, "
            f"<code>850</code>, <code>1 500</code>):",
            parse_mode="HTML",
            reply_markup=get_story_cancel_keyboard("story_menu_filters")
        )
        return
    value = int(action) if action.isdigit() else None
    if value is None or value not in STORY_PRICE_PRESETS:
        await safe_answer(callback, "Noto'g'ri qiymat!", show_alert=True)
        return
    await db_manager.update_story_settings(callback.from_user.id, min_price=float(value))
    await safe_answer(callback, f"Minimal narx: {format_price_usd(value)}")
    await cb_story_menu_filters(callback, state)


@router.message(StorySettingsSG.waiting_for_custom_price, WIZARD_INPUT)
async def process_custom_price_input(message: Message, state: FSMContext):
    value = parse_price_input(message.text)
    if value is None:
        await message.answer(
            f"{WARN} Noto'g'ri narx! Musbat son kiriting (masalan: <code>700</code> yoki <code>1 500</code>).",
            parse_mode="HTML", reply_markup=get_story_cancel_keyboard("story_menu_filters")
        )
        return
    user_id = message.from_user.id
    await db_manager.update_story_settings(user_id, min_price=value)
    await state.clear()
    await message.answer(f"{SUCCESS} <b>Minimal narx o'rnatildi: {format_price_usd(value)}</b>", parse_mode="HTML")
    text, kb = await _render_filters_screen(user_id)
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "story_toggle_photos")
async def cb_story_toggle_photos(callback: CallbackQuery):
    await _toggle_setting(callback, "require_photos", "Rasmli postlar: MAJBURIY", "Rasmli postlar: IXTIYORIY", _render_filters_screen)


@router.callback_query(F.data == "story_toggle_price_req")
async def cb_story_toggle_price_req(callback: CallbackQuery):
    await _toggle_setting(callback, "require_price", "Narx bo'lishi: MAJBURIY", "Narx bo'lishi: IXTIYORIY", _render_filters_screen)


@router.callback_query(F.data == "story_toggle_demands")
async def cb_story_toggle_demands(callback: CallbackQuery):
    await _toggle_setting(callback, "filter_demands", "Qidiruv postlari filtrlanadi", "Qidiruv postlari filtrlanmaydi", _render_filters_screen)


# --- DESIGN & VIDEO ---

async def _render_design_screen(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = await db_manager.get_story_settings(user_id)
    text = f"""
{PALETTE} <b>Istoriya Dizayni va Video Sozlamalari:</b>

├ {SWITCH_ON} <b>Telegram Yashil:</b> Telegram chat foniga o'xshash yashil naqsh.
├ {IMAGE} <b>Kvartira Rasmini Xiralashtirish:</b> e'lonning o'z rasmi yumshoq xiralashtirilgan fon bo'ladi.
├ {STAR_SPARKLE} <b>To'q Lux Gradiyent:</b> zamonaviy to'q uslub.
├ {DIAMOND} <b>Zumrad Yashil:</b> yorqin zumrad gradiyent.
├ {CLOCK} <b>Video davomiyligi:</b> <b>{st.video_duration} soniya</b> <i>({STORY_VIDEO_DURATION_MIN}–{STORY_VIDEO_DURATION_MAX} soniya)</i>
└ {AUDIO} <b>Fon musiqasi:</b> luxury treklar avtomatik almashib turadi

<b>Tanlangan fon:</b> {STYLE_LABELS.get(st.background_style, html_escape(st.background_style))}

{ARROW_DOWN} <i>Kerakli parametrni tanlang:</i>
"""
    return text, get_story_design_keyboard(st.background_style, st.video_duration)


@router.callback_query(F.data == "story_menu_design")
async def cb_story_menu_design(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    text, kb = await _render_design_screen(callback.from_user.id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("story_set_style_"))
async def cb_story_set_style(callback: CallbackQuery):
    style_code = (callback.data or "")[len("story_set_style_"):]
    if style_code not in STORY_STYLE_CODES:
        await safe_answer(callback, "Noto'g'ri uslub!", show_alert=True)
        return
    await db_manager.update_story_settings(callback.from_user.id, background_style=style_code)
    await safe_answer(callback, "Fon uslubi tanlandi")
    text, kb = await _render_design_screen(callback.from_user.id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "story_music_info")
async def cb_story_music_info(callback: CallbackQuery):
    await safe_answer(
        callback,
        "Har bir videoga luxury lounge / chillout treklardan biri avtomatik qo'yiladi; ketma-ket istoriyalarda "
        "bir xil trek takrorlanmaydi.",
        show_alert=True
    )


async def _render_duration_screen(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = await db_manager.get_story_settings(user_id)
    text = (
        f"{CLOCK} <b>Istoriya video davomiyligi:</b>\n\nHozirgi davomiylik: <b>{st.video_duration} soniya</b>\n\n"
        f"<i>Video davomiyligi {STORY_VIDEO_DURATION_MIN} dan {STORY_VIDEO_DURATION_MAX} soniyagacha bo'lishi mumkin.</i>"
    )
    return text, get_story_duration_keyboard(st.video_duration)


@router.callback_query(F.data == "story_menu_duration")
async def cb_story_menu_duration(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    text, kb = await _render_duration_screen(callback.from_user.id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "story_set_duration_custom")
async def cb_story_set_duration_custom(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.set_state(StorySettingsSG.waiting_for_video_duration)
    await edit_or_send(
        callback,
        f"{SIGNATURE} <b>Video davomiyligini soniyalarda kiriting:</b>\n\n"
        f"{STORY_VIDEO_DURATION_MIN} dan {STORY_VIDEO_DURATION_MAX} gacha butun son yuboring:",
        parse_mode="HTML",
        reply_markup=get_story_cancel_keyboard("story_menu_design")
    )


@router.callback_query(F.data.startswith("story_set_duration_"))
async def cb_story_set_duration(callback: CallbackQuery):
    value = _parse_int_suffix(callback.data, "story_set_duration_")
    if value is None or not STORY_VIDEO_DURATION_MIN <= value <= STORY_VIDEO_DURATION_MAX:
        await safe_answer(callback, "Noto'g'ri qiymat!", show_alert=True)
        return
    await db_manager.update_story_settings(callback.from_user.id, video_duration=value)
    await safe_answer(callback, f"Video davomiyligi: {value} soniya")
    text, kb = await _render_duration_screen(callback.from_user.id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


@router.message(StorySettingsSG.waiting_for_video_duration, WIZARD_INPUT)
async def process_custom_video_duration_input(message: Message, state: FSMContext):
    text_val = (message.text or "").strip()
    if not text_val.isdigit() or not STORY_VIDEO_DURATION_MIN <= int(text_val) <= STORY_VIDEO_DURATION_MAX:
        await message.answer(
            f"{WARN} Iltimos, {STORY_VIDEO_DURATION_MIN} dan {STORY_VIDEO_DURATION_MAX} gacha butun son kiriting.",
            reply_markup=get_story_cancel_keyboard("story_menu_design")
        )
        return
    value = int(text_val)
    await state.clear()
    user_id = message.from_user.id
    await db_manager.update_story_settings(user_id, video_duration=value)
    await message.answer(f"{SUCCESS} <b>Video davomiyligi saqlandi: {value} soniya.</b>", parse_mode="HTML")
    text, kb = await _render_design_screen(user_id)
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)


# --- AUTO-MONITORING ---

@router.callback_query(F.data == "story_toggle_active")
async def cb_story_toggle_active(callback: CallbackQuery):
    user_id = callback.from_user.id
    st = await db_manager.get_story_settings(user_id)

    if not st.is_active:
        session_info = await db_manager.get_user_session_info(user_id)
        if not session_info or not session_info.get("is_active"):
            await safe_answer(callback, "Avto-monitoringni yoqish uchun avval Telegram hisobingizni ulang ('Telegram Hisob' tugmasi).", show_alert=True)
            return
        if not st.source_channel:
            await safe_answer(callback, "Avto-monitoringni yoqish uchun avval manba kanalni kiriting ('Manba Kanallar' bo'limi).", show_alert=True)
            return
        if st.target_type == "channel" and not st.target_channel:
            await safe_answer(callback, "Istoriya kanali sozlanmagan: 'Manba Kanallar' bo'limida kanalni kiriting yoki shaxsiy profilni tanlang.", show_alert=True)
            return

    new_active = not st.is_active
    await db_manager.update_story_settings(user_id, is_active=new_active)
    if new_active:
        story_cloner_service.schedule_monitor_start(user_id)
    else:
        story_cloner_service.stop_monitor_for_user(user_id)
    await safe_answer(callback, "Avto-monitoring YOQILDI" if new_active else "Avto-monitoring TO'XTATILDI")

    text, kb = await render_story_main_menu(user_id)
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)


# --- STATS & HELP ---

@router.callback_query(F.data == "story_menu_stats")
async def cb_story_menu_stats(callback: CallbackQuery):
    await safe_answer(callback)
    user_id = callback.from_user.id
    stats = await db_manager.get_story_stats(user_id)
    st = await db_manager.get_story_settings(user_id)

    lines = []
    for idx, item in enumerate(stats.get("recent", []) or [], 1):
        channel = html_escape(item.get("source_channel") or "")
        posted = html_escape(str(item.get("posted_at") or "")[:16])
        lines.append(f"{idx}. <b>{channel}</b> (xabar #{item.get('source_msg_id')}) — "
                     f"<b>{format_price_usd(item.get('price'), 'Narxsiz')}</b> ({posted})")
    recent_text = "\n".join(lines) if lines else "<i>Hozircha tarix mavjud emas.</i>"

    text = f"""
{STATS} <b>Istoriyalar Statistikasi va Tarixi:</b>

├ {CALENDAR} <b>Bugun joylangan:</b> {stats.get('today_posted', 0)} ta
├ {STATS_GROWTH} <b>Jami joylangan:</b> {stats.get('total_posted', 0)} ta
└ {FLASH_GREEN} <b>Avto-monitoring:</b> {"Faol" if st.is_active else "To'xtatilgan"}

{DOCUMENT} <b>Oxirgi joylangan variantlar:</b>
{recent_text}
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_story_back_keyboard())


@router.callback_query(F.data == "story_menu_help")
async def cb_story_menu_help(callback: CallbackQuery):
    await safe_answer(callback)
    text = f"""
{INFO} <b>Story Cloner Qo'llanmasi:</b>

1. <b>Qanday ishlaydi?</b>
Tizim manba kanallardagi yangi postlarni kuzatadi, narx, rasm va tavsifni aniqlaydi. Post filtrlaringizga mos kelsa, uni Telegram hisobingiz nomidan video Istoriya qilib joylaydi (Prime Time va kunlik limit hisobga olinadi).

2. <b>Istoriyadan postga o'tish</b>
Istoriyada original postning interaktiv kartochkasi bo'ladi: uni bosgan foydalanuvchi to'g'ridan-to'g'ri kanaldagi e'longa o'tadi.

3. <b>Limitlar</b>
Telegram oddiy hisoblarga kuniga {NON_PREMIUM_DAILY_STORY_LIMIT} ta, Premium hisoblarga ko'proq istoriya ruxsat beradi. Kanal istoriyalari kanal Boost darajasiga bog'liq.

4. <b>Xavfsizlik</b>
Sessiya shifrlangan holda saqlanadi. Hisobingiz faqat siz belgilagan kanallardagi e'lonlarni istoriyaga joylash uchun ishlatiladi; istalgan vaqtda hisobdan chiqib, sessiyani o'chirishingiz mumkin.
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_story_back_keyboard())


# --- TEST STORY ---

async def _run_test_story(user_id: int, status_msg: Message, lock: asyncio.Lock) -> None:
    """Background run of the test publish (rendering takes a while), reported in `status_msg`."""
    async with lock:
        try:
            ok, res_msg, detail = await story_cloner_service.test_publish_latest_post(user_id)
        except Exception as e:
            logger.exception(f"Test story of user {user_id} failed")
            ok, res_msg, detail = False, f"Kutilmagan xatolik: {e}", None

    if ok and detail:
        url = detail.get("story_url")
        url_line = f"\n{LINK} <a href=\"{html_escape(url)}\">Istoriyani Telegramda ko'rish</a>" if url else ""
        text = f"""
{SUCCESS} <b>Test istoriya joylandi!</b>

├ {TAG} <b>Story ID:</b> <code>{html_escape(str(detail.get('story_id')))}</code>
├ {CHANNEL} <b>Manba:</b> {html_escape(str(detail.get('source_channel') or ''))} (xabar #{detail.get('msg_id')})
└ {MONEY} <b>Narx:</b> {format_price_usd(detail.get('price'), 'Aniqlanmadi')}{url_line}

{MOBILE} <i>Telegram ilovangizda istoriyani tekshirib ko'ring. Bu e'lon qayta joylanmaydi.</i>
"""
    else:
        text = f"{ERROR} <b>Test istoriya joylanmadi:</b>\n\n{html_escape(res_msg or 'Nomalum xatolik')}"
    try:
        await show_in_place(status_msg, text, parse_mode="HTML", reply_markup=get_story_back_keyboard())
    except Exception:
        logger.debug("Could not report the test story result", exc_info=True)


@router.message(Command("story_test"))
@router.callback_query(F.data == "story_menu_test_post")
async def trigger_test_story_post(event: Union[Message, CallbackQuery], state: FSMContext):
    """Posts the newest matching listing of the source channel as a story right away (one run at a time per
    user, at most one start per minute); the result is reported when rendering and upload are done."""
    await state.clear()
    user_id = event.from_user.id
    lock = _test_story_locks.setdefault(user_id, asyncio.Lock())
    elapsed = time.monotonic() - _last_test_story.get(user_id, float("-inf"))
    if lock.locked() or elapsed < TEST_STORY_COOLDOWN_SECONDS:
        wait_hint = "Oldingi test istoriya hali tayyorlanmoqda." if lock.locked() else \
            f"Keyingi test istoriyani {int(TEST_STORY_COOLDOWN_SECONDS - elapsed) + 1} soniyadan keyin boshlash mumkin."
        if isinstance(event, CallbackQuery):
            await safe_answer(event, wait_hint, show_alert=True)
        else:
            await event.answer(f"{WARN} {wait_hint}")
        return

    st = await db_manager.get_story_settings(user_id)
    if not st.source_channel:
        warning = "Avval 'Manba Kanallar' bo'limida manba kanalni kiriting."
        if isinstance(event, CallbackQuery):
            await safe_answer(event, warning, show_alert=True)
        else:
            await event.answer(f"{WARN} {warning}")
        return

    _last_test_story[user_id] = time.monotonic()
    wait_text = (
        f"{LOADING} <b>{st.video_duration} soniyalik test video istoriya tayyorlanmoqda...</b>\n\n"
        f"<i>Manba kanaldagi eng yangi mos e'lon olinadi, kollaj va musiqali video tayyorlanib joylanadi. "
        f"Bu bir necha daqiqa davom etishi mumkin.</i>"
    )
    if isinstance(event, CallbackQuery):
        await safe_answer(event, "Test istoriya boshlandi")
        status_msg = await event.bot.send_message(chat_id=user_id, text=wait_text, parse_mode="HTML")
    else:
        status_msg = await event.answer(wait_text, parse_mode="HTML")
    _spawn(_run_test_story(user_id, status_msg, lock))
