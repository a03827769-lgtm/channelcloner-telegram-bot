import asyncio
import logging
import re
from typing import Any, List, Optional, Set, Tuple, Union

from aiogram import Router, F, Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from telethon.tl.types import User as TelethonUser

from database.db_manager import db_manager
from bot.access import can_manage_pair, is_admin_user
from database.models import ChannelPair
from bot.filters import IsAdminFilter, WIZARD_INPUT
from bot.states.cloner_states import AddChannelPairSG
from bot.keyboards.inline_buttons import (
    MENU_CLONER,
    MENU_NEW_PAIR,
    get_cloner_menu_keyboard,
    get_pairs_list_keyboard,
    get_pair_detail_keyboard,
    get_delete_confirmation_keyboard,
    get_cancel_keyboard
)
from services.channel_access import verify_destination_access, DESTINATION_ERROR_TEXTS
from services.telethon_listener import telethon_listener
from services.cloner_engine import cloner_engine
from services.text_processor import TextProcessor
from services.custom_emojis import (
    REFRESH, SETTINGS, LOCK_LOCKED, LOCK_UNLOCKED, STARS, SUCCESS, ERROR, WARN,
    DOCUMENT, LINK, CLEAN, TRANSLATE, IMAGE, MONEY, SIGNATURE,
    STATS, STATS_GROWTH, PARTY, FLASH, INFO, NUM_1, NUM_2,
    ROCKET, HISTORY_CLOCK, SHIELD, STAR_SPARKLE, AI, INBOX, FORWARD,
    ID_STARS, ID_SETTINGS, ID_BACK
)

from bot.utils import (
    safe_answer, html_escape, parse_callback_id, preview, edit_or_send, show_in_place,
    is_not_modified_error, is_too_long_error, is_repeated_album_part
)

logger = logging.getLogger(__name__)

# Group/channel service events (chat migrations) are handled on this parent router; everything users
# interact with lives in the private-chat-only child router, so no cloner menu, wizard prompt or FSM
# state is ever produced inside a group the bot is a member of.
router = Router(name="cloner_menu_router")
menu_router = Router(name="cloner_menu_private_router")
menu_router.message.filter(F.chat.type == ChatType.PRIVATE)
menu_router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
router.include_router(menu_router)

PAIR_NOT_FOUND_TEXT = "Kanal juftligi topilmadi!"
PAIR_FORBIDDEN_TEXT = "Ruxsat berilmagan! Ushbu kanal sizga tegishli emas."
# Why a paused pair cannot be resumed (error codes of db_manager.set_pair_active_by_owner)
PAIR_RESUME_REFUSAL_TEXTS = {
    "self_loop": "Manba va maqsad bir xil kanal — bunday juftlikni yoqib bo'lmaydi. Uni o'chirib yuboring.",
    "cycle": "Bu juftlikni yoqish kanallar orasida cheksiz halqa (loop) hosil qiladi: postlar to'xtovsiz qayta ko'chiriladi.",
    "duplicate": "Xuddi shu manba va maqsad kanallari bilan boshqa faol juftligingiz bor. Takror juftlikni o'chirib yuboring.",
}

# Syntactic shapes of a channel reference after TextProcessor.normalize_channel_input()
INVITE_HASH_RE = re.compile(r"(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)([A-Za-z0-9_-]{8,64})", re.IGNORECASE)
USERNAME_REF_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,31}")
NUMERIC_REF_RE = re.compile(r"-?\d{5,20}")

# Longest single Telegram message we build for multi-line reports (the hard limit is 4096)
REPORT_CHUNK_CHARS = 3800

_background_tasks: Set[asyncio.Task] = set()
_catchup_running_users: Set[int] = set()
_test_posts_running: Set[int] = set()


# --- HELPERS ---

def safe_parse_id(data: str, index: int = -1) -> Optional[int]:
    """Safely extracts a positive integer ID from callback data string"""
    value = parse_callback_id(data, index)
    return value if value > 0 else None

async def _get_accessible_pair(callback: CallbackQuery, index: int) -> Optional[ChannelPair]:
    """The pair referenced by the callback data if the user may manage it; otherwise answers the
    callback with an alert (the only answer it gets) and returns None."""
    pair_id = safe_parse_id(callback.data, index)
    pair = await db_manager.get_pair_by_id(pair_id) if pair_id else None
    if not pair:
        await safe_answer(callback, PAIR_NOT_FOUND_TEXT, show_alert=True)
        return None
    if not can_manage_pair(pair, callback.from_user.id):
        await safe_answer(callback, PAIR_FORBIDDEN_TEXT, show_alert=True)
        return None
    return pair

def _invite_hash(reference: str) -> Optional[str]:
    match = INVITE_HASH_RE.search(reference or "")
    return match.group(1) if match else None

def _is_valid_chat_reference(reference: str) -> bool:
    """@username, numeric chat id or invite link — anything else (menu labels, typos with spaces or
    apostrophes, random words) cannot be a channel and is rejected before any lookup."""
    reference = (reference or "").strip()
    return bool(
        USERNAME_REF_RE.fullmatch(reference)
        or NUMERIC_REF_RE.fullmatch(reference)
        or _invite_hash(reference)
    )

def _is_forwarded(message: Message) -> bool:
    return getattr(message, "forward_origin", None) is not None or getattr(message, "forward_from_chat", None) is not None

def _forwarded_from_non_channel(message: Message) -> bool:
    """A forwarded message whose origin is a person (or hidden user), not a channel."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return False
    return getattr(origin, "chat", None) is None and getattr(message, "forward_from_chat", None) is None

async def _resolve_without_joining(reference: str) -> Any:
    """Telethon entity of a channel reference, resolved without the shared userbot ever joining it:
    invite links are only inspected (CheckChatInviteRequest). Joining happens only once a pair exists
    and its source is monitored. None when the reference cannot be resolved this way."""
    try:
        return await telethon_listener.resolve_entity(reference, join_invite=False)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug(f"Could not resolve channel reference {reference!r}", exc_info=True)
        return None

async def _pending_invite_title(invite_hash: str) -> Optional[str]:
    """Title of a valid invite link the userbot has not joined yet (read-only CheckChatInviteRequest);
    None when the link is invalid or expired."""
    client = telethon_listener.client
    if client is None or not telethon_listener.is_connected():
        return None
    from telethon.tl.functions.messages import CheckChatInviteRequest
    try:
        invite = await asyncio.wait_for(client(CheckChatInviteRequest(invite_hash)), timeout=20)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.info(f"Invite link check failed: {e}")
        return None
    return getattr(invite, "title", None) or getattr(getattr(invite, "chat", None), "title", None)

def _spawn(coro) -> asyncio.Task:
    """Runs `coro` as a tracked background task (the per-user update lock is not held while it runs)."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

async def _bot_username(bot: Bot) -> str:
    try:
        me = await bot.me()
        return me.username or ""
    except Exception:
        return ""

async def _destination_error_text(bot: Bot, code: str) -> str:
    text = f"{WARN} <b>Xatolik:</b> {html_escape(DESTINATION_ERROR_TEXTS.get(code, DESTINATION_ERROR_TEXTS['not_found']))}"
    if code in ("not_found", "bot_not_admin"):
        username = await _bot_username(bot)
        bot_ref = f"<code>@{html_escape(username)}</code>" if username else "botimizni"
        text += (
            f"\n\n1. Kanalingizga {bot_ref} <b>Administrator</b> qilib qo'shing va <b>Xabar yozish (Post Messages)</b> ruxsatini bering.\n"
            f"2. So'ngra kanalingizdan xabar forward qiling yoki username / ID ni qaytadan yuboring."
        )
    return text

def _chunk_report(header: str, lines: List[str], limit: int = REPORT_CHUNK_CHARS) -> List[str]:
    """Splits a header plus report lines into messages that stay below Telegram's length limit."""
    chunks: List[str] = []
    current = header
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# --- ROUTE HANDLERS ---

@menu_router.message(Command("cloner"))
@menu_router.callback_query(F.data == "menu_cloner")
@menu_router.message(F.text == MENU_CLONER, ~F.forward_origin)
async def show_cloner_menu(event: Union[CallbackQuery, Message], state: FSMContext):
    await state.clear()
    user_id = event.from_user.id
    pairs = await db_manager.get_user_channel_pairs(user_id)

    text = f"""
{REFRESH} <b>Kanal Kloner Boshqaruv Markazi</b>

Siz ulagan kanallar soni: <b>{len(pairs)} ta</b>

Quyidagi amallardan birini tanlang:
"""
    reply_markup = get_cloner_menu_keyboard(has_pairs=len(pairs) > 0)

    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await edit_or_send(event, text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await event.answer(text=text, parse_mode="HTML", reply_markup=reply_markup)

async def _show_pairs_list(callback: CallbackQuery, page: int = 0):
    pairs = await db_manager.get_user_channel_pairs(callback.from_user.id)
    if not pairs:
        await edit_or_send(
            callback,
            f"{DOCUMENT} <b>Hozircha hech qanday kanal ulanmagan.</b>\n\nYangi kanal qo'shish uchun quyidagi tugmani bosing:",
            parse_mode="HTML",
            reply_markup=get_cloner_menu_keyboard(has_pairs=False)
        )
        return

    text = f"""
{DOCUMENT} <b>Sizning Ulangan Kanallaringiz ({len(pairs)} ta):</b>

Boshqarish uchun kerakli kanalni tanlang:
"""
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_pairs_list_keyboard(pairs, page=page))

@menu_router.callback_query(F.data == "cloner_list_pairs")
async def cb_list_pairs(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await state.clear()
    await _show_pairs_list(callback)

@menu_router.callback_query(F.data.startswith("cloner_pairs_page_"))
async def cb_pairs_page(callback: CallbackQuery, state: FSMContext):
    await safe_answer(callback)
    await _show_pairs_list(callback, page=max(0, parse_callback_id(callback.data)))

@menu_router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await safe_answer(callback)

# --- ADD CHANNEL PAIR WIZARD ---

def _plan_limit_view(max_allowed: int, current_count: int) -> Tuple[str, InlineKeyboardMarkup]:
    text = f"""
{LOCK_LOCKED} <b>Kanal Limiti Yetib Keldi!</b>

Sizning hozirgi tarifingiz bo'yicha maksimal <b>{max_allowed} ta</b> kanal ulash mumkin (Hozir ulangan: {current_count} ta).

Ko'proq kanal ulash uchun <b>{STARS} Tariflar & Obuna</b> bo'limidan obunangizni oshiring!
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Tariflar & Obuna",
            style="success",
            icon_custom_emoji_id=ID_STARS,
            callback_data="menu_stars"
        )],
        [InlineKeyboardButton(
            text="Orqaga",
            style="danger",
            icon_custom_emoji_id=ID_BACK,
            callback_data="menu_cloner"
        )]
    ])
    return text, kb

@menu_router.callback_query(F.data == "cloner_add_pair")
@menu_router.message(F.text == MENU_NEW_PAIR, ~F.forward_origin)
async def cb_start_add_pair(event: Union[CallbackQuery, Message], state: FSMContext):
    user_id = event.from_user.id
    is_admin = is_admin_user(user_id)

    can_add, max_allowed, current_count = await db_manager.can_user_add_channel(user_id, is_admin=is_admin)
    if not can_add:
        await state.clear()
        text, kb = _plan_limit_view(max_allowed, current_count)
        if isinstance(event, CallbackQuery):
            await safe_answer(event)
            await edit_or_send(event, text, parse_mode="HTML", reply_markup=kb)
        else:
            await event.answer(text=text, parse_mode="HTML", reply_markup=kb)
        return

    # A fresh wizard: nothing from an earlier, abandoned flow may leak into this one
    await state.clear()
    await state.set_state(AddChannelPairSG.waiting_for_source_channel)

    text = f"""
{DOCUMENT} <b>{NUM_1}-QADAM: Manba kanalni kiriting yoki xabar uzating</b>

Postlari ko'chirilishi kerak bo'lgan kanalni tanlashning 2 ta oson yo'li:

1. <b>Eng osoni:</b> O'sha kanaldan <b>istalgan bitta xabarni</b> ushbu botga <b>FORWARD (uzatish)</b> qiling {DOCUMENT}
2. Yoki kanal username/havolasini yozib yuboring:
   <i>Misol:</i> <code>@yangiliklar_kanali</code> yoki <code>https://t.me/yangiliklar_kanali</code>
"""
    if isinstance(event, CallbackQuery):
        await safe_answer(event)
        await edit_or_send(event, text, parse_mode="HTML", reply_markup=get_cancel_keyboard("menu_cloner"))
    else:
        await event.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard("menu_cloner")
        )

@menu_router.message(AddChannelPairSG.waiting_for_source_channel, WIZARD_INPUT)
async def process_source_channel(message: Message, state: FSMContext):
    if await is_repeated_album_part(message, state):
        return
    cancel_kb = get_cancel_keyboard("menu_cloner")

    if _forwarded_from_non_channel(message):
        await message.answer(
            f"{ERROR} Bu xabar kanaldan emas. Iltimos, manba <b>kanal</b>dan biror postni forward qiling yoki kanal username/havolasini yozing.",
            parse_mode="HTML",
            reply_markup=cancel_kb
        )
        return

    extracted = TextProcessor.extract_channel_from_message(message)
    if not extracted or not extracted[0]:
        await message.answer(f"{ERROR} Kanal aniqlanmadi. Iltimos, manba kanaldan biror xabarni forward qiling yoki username yozing.", parse_mode="HTML", reply_markup=cancel_kb)
        return

    source_channel, source_title, source_id = extracted
    source_channel = str(source_channel).strip()
    if not _is_forwarded(message) and not _is_valid_chat_reference(source_channel):
        await message.answer(
            f"{ERROR} <b>Noto'g'ri kanal manzili:</b> <code>{html_escape(source_channel[:64])}</code>\n\n"
            f"Kanal username (<code>@kanal_nomi</code>), havola (<code>https://t.me/kanal_nomi</code>), "
            f"taklif havolasi (<code>https://t.me/+...</code>) yoki kanal ID (<code>-100...</code>) yuboring "
            f"yoki kanaldan biror xabarni forward qiling.",
            parse_mode="HTML",
            reply_markup=cancel_kb
        )
        return

    verification_note = ""
    if telethon_listener.is_connected():
        entity = await _resolve_without_joining(source_channel)
        invite = _invite_hash(source_channel)
        if entity is None and invite:
            # A private channel the userbot has not joined yet: accepted as is (only inspected here),
            # the userbot joins it once the pair is created
            source_title = await _pending_invite_title(invite) or source_title
            verification_note = f"\n{INFO} <i>Yopiq kanal: tizim unga juftlik yaratilgach qo'shiladi.</i>\n"
        elif entity is None and USERNAME_REF_RE.fullmatch(source_channel):
            await message.answer(
                f"{ERROR} <b>Manba kanal topilmadi.</b>\n\n"
                f"Username to'g'ri yozilganini tekshiring. Kanal yopiq bo'lsa, uning taklif havolasini "
                f"(<code>https://t.me/+...</code>) yuboring.",
                parse_mode="HTML",
                reply_markup=cancel_kb
            )
            return
        elif entity is None:
            verification_note = (
                f"\n{WARN} <i>Kanalni hozircha tekshirib bo'lmadi (yopiq kanal bo'lishi mumkin). "
                f"Postlar kelmasa, kanalning taklif havolasini yuboring.</i>\n"
            )
        elif isinstance(entity, TelethonUser):
            await message.answer(
                f"{ERROR} <b>Xatolik:</b> Kiritilgan manba shaxsiy profil (foydalanuvchi) hisoblanadi!\n\n"
                f"Bot faqat <b>kanallar</b>dan xabar nusxalay oladi. Iltimos, Telegram kanal username yoki havolasini kiriting.",
                parse_mode="HTML",
                reply_markup=cancel_kb
            )
            return
        else:
            source_title = getattr(entity, "title", None) or source_title
            source_id = getattr(entity, "id", None) or source_id
    else:
        verification_note = f"\n{INFO} <i>Kanal hozircha tekshirilmadi: tizim ulanishi tiklangach avtomatik tekshiriladi.</i>\n"

    source_title = str(source_title or source_channel)
    await state.update_data(
        source_channel=source_channel,
        source_title=source_title,
        source_id=source_id
    )
    await state.set_state(AddChannelPairSG.waiting_for_target_channel)

    text = f"""
{SUCCESS} <b>Manba kanal qabul qilindi:</b>
{LINK} <b>Nomi:</b> {html_escape(source_title)} (<code>{html_escape(source_channel)}</code>)
{verification_note}
━━━━━━━━━━━━━━━━━━━━
{DOCUMENT} <b>{NUM_2}-QADAM: Maqsadli kanalni kiriting yoki xabar uzating</b>

Postlar qaysi kanalingizga tashlanishi kerak?

1. O'z kanalingizdan <b>istalgan bitta xabarni</b> shu yerga <b>FORWARD</b> qiling {DOCUMENT}
2. Yoki kanalingiz username yoki ID raqamini yozing.

{WARN} <b>Muhim:</b> Ushbu bot kanalingizda <b>administrator</b> bo'lishi kerak!
"""
    await message.answer(text=text, parse_mode="HTML", reply_markup=cancel_kb)

async def _monitor_new_source(source_channel: str, pair_id: int):
    try:
        await asyncio.wait_for(telethon_listener.join_and_monitor_channel(source_channel), timeout=60)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"Could not start monitoring source {source_channel!r} of pair #{pair_id}: {e}")

@menu_router.message(AddChannelPairSG.waiting_for_target_channel, WIZARD_INPUT)
async def process_target_channel(message: Message, state: FSMContext, bot: Bot):
    if await is_repeated_album_part(message, state):
        return
    cancel_kb = get_cancel_keyboard("menu_cloner")
    user_id = message.from_user.id

    if _forwarded_from_non_channel(message):
        await message.answer(
            f"{ERROR} Bu xabar kanaldan emas. Iltimos, o'z <b>kanal</b>ingizdan biror postni forward qiling yoki kanal username/ID sini yozing.",
            parse_mode="HTML",
            reply_markup=cancel_kb
        )
        return

    extracted = TextProcessor.extract_channel_from_message(message)
    if not extracted or not extracted[0]:
        await message.answer(f"{ERROR} Kanal aniqlanmadi. Iltimos, kanalingizdan xabar forward qiling yoki username yozing.", parse_mode="HTML", reply_markup=cancel_kb)
        return

    target_channel, target_title, target_id = extracted
    target_channel = str(target_channel).strip()
    if _invite_hash(target_channel):
        # The Bot API cannot address a chat by its invite link, and the userbot never joins targets
        await message.answer(
            f"{WARN} <b>Maqsad kanal uchun taklif havolasi qabul qilinmaydi.</b>\n\n"
            f"Iltimos, kanalingizdan biror xabarni shu yerga <b>forward</b> qiling yoki kanal username "
            f"(<code>@kanal_nomi</code>) / ID sini (<code>-100...</code>) yuboring.",
            parse_mode="HTML",
            reply_markup=cancel_kb
        )
        return
    if not _is_forwarded(message) and not _is_valid_chat_reference(target_channel):
        await message.answer(
            f"{ERROR} <b>Noto'g'ri kanal manzili:</b> <code>{html_escape(target_channel[:64])}</code>\n\n"
            f"Kanalingiz username (<code>@kanal_nomi</code>) yoki ID sini (<code>-100...</code>) yuboring "
            f"yoki kanalingizdan biror xabarni forward qiling.",
            parse_mode="HTML",
            reply_markup=cancel_kb
        )
        return

    data = await state.get_data()
    source_channel = data.get("source_channel")
    if not source_channel:
        await state.clear()
        await message.answer(
            f"{WARN} Sessiya eskirgan. Iltimos, kanal ulashni qaytadan boshlang.",
            parse_mode="HTML",
            reply_markup=get_cloner_menu_keyboard(has_pairs=True)
        )
        return
    source_title = data.get("source_title", source_channel)
    source_id = data.get("source_id")

    chat_target: Any = int(target_channel) if target_channel.lstrip("-").isdigit() else target_channel
    if isinstance(chat_target, int) and chat_target > 0:
        chat_target = int(f"-100{chat_target}")

    try:
        chat = await bot.get_chat(chat_target)
    except Exception as e:
        logger.info(f"Target channel {chat_target!r} is not accessible for the bot: {e}")
        await message.answer(await _destination_error_text(bot, "not_found"), parse_mode="HTML", reply_markup=cancel_kb)
        return

    target_id = chat.id
    target_title = getattr(chat, "title", None) or (target_title if isinstance(target_title, str) else None) or target_channel

    # 1. Loop prevention check: Source and target cannot be the same channel
    norm_source = db_manager._normalize_channel_name(source_channel)
    norm_target = db_manager._normalize_channel_name(target_channel)
    is_same_channel = False
    if source_id is not None and target_id is not None and (
        source_id == target_id or db_manager.normalize_peer_id(source_id) == db_manager.normalize_peer_id(target_id)
    ):
        is_same_channel = True
    elif norm_source and norm_target and norm_source == norm_target:
        is_same_channel = True
    elif source_id is not None and norm_target == str(source_id):
        is_same_channel = True
    elif target_id is not None and norm_source == str(target_id):
        is_same_channel = True

    if is_same_channel:
        await message.answer(
            f"{WARN} <b>Xatolik:</b> Manba va Maqsad kanali bir xil bo'lishi mumkin emas!\n\n"
            f"Kanalni o'zidan o'ziga klonlash cheksiz takrorlanish (loop) xavfini keltirib chiqaradi. "
            f"Iltimos, boshqa maqsad kanalini yuboring yoki bekor qiling.",
            parse_mode="HTML",
            reply_markup=cancel_kb
        )
        return

    # 2. Duplicate pair check: Check if this pair already exists
    existing_pair = await db_manager.find_duplicate_pair(
        user_id=user_id,
        source_channel=source_channel,
        target_channel=target_channel,
        source_id=source_id,
        target_id=target_id
    )
    if existing_pair:
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"Kanalni boshqarish #{existing_pair.id}",
                style="primary",
                icon_custom_emoji_id=ID_SETTINGS,
                callback_data=f"pair_view_{existing_pair.id}"
            )],
            [InlineKeyboardButton(
                text="Kanallar ro'yxati",
                style="danger",
                icon_custom_emoji_id=ID_BACK,
                callback_data="menu_cloner"
            )]
        ])
        await message.answer(
            f"{WARN} <b>Ushbu kanal juftligi allaqachon mavjud!</b>\n\n"
            f"├ {LINK} <b>Manba:</b> {html_escape(existing_pair.source_title)} (<code>{html_escape(existing_pair.source_channel)}</code>)\n"
            f"├ {LINK} <b>Maqsad:</b> {html_escape(existing_pair.target_title)} (<code>{html_escape(existing_pair.target_channel)}</code>)\n"
            f"└ {SUCCESS} <b>ID:</b> #{existing_pair.id}\n\n"
            f"Bir xil manbadan bir xil maqsadga bir necha bor ulash mumkin emas. "
            f"Mavjud kanal sozlamalarini quyidagi tugma orqali boshqarishingiz mumkin:",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    # 3. Ownership: both the bot and the requesting user must be able to post in the target. The bot is an
    #    admin in every customer's channel, so the bot check alone would let anyone publish into them.
    ok, _, error_code = await verify_destination_access(bot, target_id, user_id)
    if not ok:
        await message.answer(await _destination_error_text(bot, error_code), parse_mode="HTML", reply_markup=cancel_kb)
        return

    # 4. Cycles across pairs (A -> B while B -> A exists, or longer loops of any users)
    if await db_manager.would_create_cycle(source_channel, target_channel, source_id, target_id):
        await message.answer(
            f"{WARN} <b>Xatolik:</b> Bu ulanish klonlash halqasini (loop) hosil qiladi!\n\n"
            f"Maqsad kanaldagi postlar allaqachon (to'g'ridan-to'g'ri yoki boshqa kanallar orqali) manba kanalga ko'chirilmoqda. "
            f"Bunday ulanish postlarni cheksiz takrorlaydi. Iltimos, boshqa maqsad kanalini tanlang.",
            parse_mode="HTML",
            reply_markup=cancel_kb
        )
        return

    # 5. Plan limit: re-checked right before inserting (another pair may have been added since step 1)
    is_admin = is_admin_user(user_id)
    can_add, max_allowed, current_count = await db_manager.can_user_add_channel(user_id, is_admin=is_admin)
    if not can_add:
        await state.clear()
        text, kb = _plan_limit_view(max_allowed, current_count)
        await message.answer(text=text, parse_mode="HTML", reply_markup=kb)
        return

    pair_id = await db_manager.add_channel_pair(
        user_id=user_id,
        source_channel=source_channel,
        source_title=source_title,
        source_id=source_id,
        target_channel=str(target_channel),
        target_title=target_title,
        target_id=target_id,
        clean_links=True,
        custom_signature="",
        blacklist_words="",
        clone_mode="clean"
    )

    await state.clear()
    if telethon_listener.is_connected():
        # Joining/monitoring the source can take a while (resolution, join): done in the background
        _spawn(_monitor_new_source(source_channel, pair_id))

    pair = await db_manager.get_pair_by_id(pair_id)

    text = f"""
{PARTY} <b>Kanal juftligi muvaffaqiyatli ulandi!</b>

├ {LINK} <b>Manba:</b> {html_escape(source_title)} (<code>{html_escape(source_channel)}</code>)
├ {LINK} <b>Maqsad:</b> {html_escape(target_title)} (<code>{html_escape(target_channel)}</code>)
├ {SUCCESS} <b>Holat:</b> Faol (Avtomatik kuzatuv yoqildi)
└ {CLEAN} <b>Reklama tozalash:</b> Yoqilgan

<i>Endi manba kanaldagi yangi xabarlar avtomatik ravishda yetib keladi!</i>
"""
    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=get_pair_detail_keyboard(pair) if pair else get_cloner_menu_keyboard(has_pairs=True)
    )

# --- PAIR DETAILS ---

def _on_off(value: bool, on: str = "Yoqilgan", off: str = "O'chirilgan") -> str:
    return f"{SUCCESS} {on}" if value else f"{ERROR} {off}"

def build_pair_detail_text(pair: ChannelPair, compact: bool = False) -> str:
    """Control panel text of a pair. Long user values are shown shortened; `compact` drops them entirely
    (used when even the shortened panel does not fit into one Telegram message)."""
    status_icon = f"{SUCCESS} Faol" if pair.is_active else f"{ERROR} To'xtatilgan"
    trans_icon = f"{SUCCESS} {html_escape(pair.target_lang.upper())}" if pair.auto_translate else f"{ERROR} O'chirilgan"
    wm_icon = f"{SUCCESS} {preview(pair.image_watermark_text, 64) or 'ON'}" if pair.image_watermark_type != "none" else f"{ERROR} O'chirilgan"
    vwm_icon = (
        f"{SUCCESS} {preview(pair.video_watermark_text, 64) or 'ON'} ({html_escape(pair.video_watermark_pos)})"
        if pair.video_watermark_type != "none" else f"{ERROR} O'chirilgan"
    )
    drip_status = (
        f"{pair.drip_delay_minutes}m (Tun: {html_escape(pair.night_mode.upper())})"
        if (pair.drip_delay_minutes > 0 or pair.night_mode != "off") else f"{ERROR} O'chirilgan"
    )
    ai_status = f"{SUCCESS} {html_escape(pair.ai_paraphrase_mode.upper())}" if pair.ai_paraphrase_mode != "off" else f"{ERROR} O'chirilgan"
    prot_icon = f"{LOCK_UNLOCKED} Yoqilgan" if pair.is_protected_source else f"{LOCK_LOCKED} O'chirilgan"
    aff_icon = f"{SUCCESS} O'rnatilgan" if pair.affiliate_rules else f"{ERROR} O'rnatilmagan"
    emoji_icon = f"{SUCCESS} Yoqilgan (VIP)" if pair.auto_premium_emojis else f"{ERROR} O'chirilgan"
    last_seen_str = f"#{pair.last_seen_msg_id}" if pair.last_seen_msg_id else "<i>Aniqlanmagan</i>"

    if compact:
        sig_text = "O'rnatilgan" if pair.custom_signature else "<i>O'rnatilmagan</i>"
        rep_text = "O'rnatilgan" if pair.replace_words else "<i>Bo'sh</i>"
        bl_text = "O'rnatilgan" if pair.blacklist_words else "<i>Bo'sh</i>"
    else:
        # Signatures are stored as HTML: show their visible text, not the raw tags
        sig_text = f"<code>{preview(pair.custom_signature, strip_tags=True)}</code>" if pair.custom_signature else "<i>O'rnatilmagan</i>"
        rep_text = f"<code>{preview(pair.replace_words)}</code>" if pair.replace_words else "<i>Bo'sh</i>"
        bl_text = f"<code>{preview(pair.blacklist_words)}</code>" if pair.blacklist_words else "<i>Bo'sh</i>"
    src_title_safe = preview(pair.source_title or pair.source_channel, 100)
    tgt_title_safe = preview(pair.target_title or pair.target_channel, 100)

    return f"""
{SETTINGS} <b>Kanal Juftligi Boshqaruvi (ID: #{pair.id}):</b>

├ {LINK} <b>Manba:</b> {src_title_safe} (<code>{preview(pair.source_channel, 100)}</code>)
├ {LINK} <b>Maqsad:</b> {tgt_title_safe} (<code>{preview(pair.target_channel, 100)}</code>)
├ {FLASH} <b>Holati:</b> {status_icon}
├ {CLEAN} <b>Linklarni tozalash:</b> {_on_off(pair.clean_links)}
├ {TRANSLATE} <b>Avto-Tarjima:</b> {trans_icon}
├ {IMAGE} <b>Rasmga Watermark:</b> {wm_icon}
├ {ROCKET} <b>Video Watermark:</b> {vwm_icon}
├ {HISTORY_CLOCK} <b>Drip Feed / Tungi Rejim:</b> {drip_status}
├ {AI} <b>AI Paraphrase:</b> {ai_status}
├ {MONEY} <b>CTA Tugmalar:</b> {_on_off(pair.auto_cta_buttons)}
├ {LINK} <b>Referal Almashtirgich:</b> {aff_icon}
├ {LOCK_UNLOCKED} <b>Protected Content Mode:</b> {prot_icon}
├ {STAR_SPARKLE} <b>Telegram Premium Emojilar:</b> {emoji_icon}
├ {SHIELD} <b>Avto-Zaxira:</b> {_on_off(pair.backup_enabled)}
├ {REFRESH} <b>Oflayn Yetkazish (Catch-Up):</b> {_on_off(getattr(pair, "auto_catchup", True))} (Oxirgi ID: {last_seen_str})
├ {SIGNATURE} <b>Manba imzosini tozalash:</b> {_on_off(pair.remove_signature)}
├ {SIGNATURE} <b>Matn imzosi:</b> {sig_text}
├ {REFRESH} <b>So'z/Raqam almashtirish:</b> {rep_text}
└ {ERROR} <b>Qora ro'yxat:</b> {bl_text}

Quyidagi tugmalar orqali sozlamalarni o'zgartiring:
"""

async def render_pair_detail(pair, message_obj):
    """Shows the pair control panel in place of `message_obj` (as a new message when that one is
    inaccessible), falling back to the compact panel when the full one is too long."""
    if message_obj is None:
        return
    keyboard = get_pair_detail_keyboard(pair)
    for compact in (False, True):
        try:
            await show_in_place(message_obj, build_pair_detail_text(pair, compact=compact), parse_mode="HTML", reply_markup=keyboard)
            return
        except TelegramBadRequest as e:
            if not compact and is_too_long_error(e):
                continue
            if not is_not_modified_error(e):
                logger.warning(f"Error rendering pair #{pair.id} detail: {e}")
            return
        except Exception as e:
            logger.warning(f"Error rendering pair #{pair.id} detail: {e}")
            return

@menu_router.callback_query(F.data.startswith("pair_view_"))
async def cb_view_pair(callback: CallbackQuery, state: FSMContext = None):
    pair = await _get_accessible_pair(callback, 2)
    if not pair:
        return
    await safe_answer(callback)
    if state:
        await state.clear()
    await render_pair_detail(pair, callback.message)

@menu_router.callback_query(F.data.startswith("pair_stats_"))
async def cb_pair_stats(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, 2)
    if not pair:
        return

    await safe_answer(callback)
    analytics = await db_manager.get_pair_analytics(pair.id)
    src_title_safe = preview(pair.source_title or pair.source_channel, 100)
    tgt_title_safe = preview(pair.target_title or pair.target_channel, 100)

    text = f"""
{STATS} <b>Kanal Statistikasi (#{pair.id}):</b>

├ {LINK} <b>Manba:</b> {src_title_safe}
└ {LINK} <b>Maqsad:</b> {tgt_title_safe}

{STATS_GROWTH} <b>Ko'rsatkichlar:</b>
├ {DOCUMENT} <b>Jami ko'chirilgan postlar:</b> <code>{analytics['total_cloned']}</code> ta
├ {INFO} <b>Bugun ko'chirilgan postlar:</b> <code>{analytics['today_cloned']}</code> ta
├ {IMAGE} <b>Rasmli postlar:</b> <code>{analytics['photos_cloned']}</code> ta
└ {DOCUMENT} <b>Videoli postlar:</b> <code>{analytics['videos_cloned']}</code> ta
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Sozlamalarga Qaytish",
            style="primary",
            icon_custom_emoji_id=ID_BACK,
            callback_data=f"pair_view_{pair.id}"
        )]
    ])
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=kb)

async def _run_test_post(bot: Bot, pair: ChannelPair, chat_id: int):
    """Sends the test post and reports the result to `chat_id` (runs as a background task)."""
    try:
        # Pairs created before destination checks existed are re-verified against their owner, so a
        # test post can never be published into a channel the owner does not manage
        ok, _, error_code = await verify_destination_access(bot, pair.target_id or pair.target_channel, pair.user_id)
        if not ok:
            result_text = await _destination_error_text(bot, error_code)
        else:
            success, msg = await cloner_engine.send_test_post(pair)
            result_text = f"{SUCCESS if success else ERROR} {html_escape(msg)}"
        await bot.send_message(chat_id=chat_id, text=result_text, parse_mode="HTML")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(f"Test post for pair #{pair.id} failed", exc_info=True)
    finally:
        _test_posts_running.discard(pair.id)

@menu_router.callback_query(F.data.startswith("pair_test_post_"))
async def cb_test_post(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, 3)
    if not pair:
        return
    if pair.id in _test_posts_running:
        await safe_answer(callback, "Test post allaqachon yuborilmoqda, iltimos kuting.")
        return

    # Answer right away (sending can take seconds); the result arrives as a separate message
    await safe_answer(callback, "Test post yuborilmoqda...")
    _test_posts_running.add(pair.id)
    chat_id = callback.message.chat.id if callback.message is not None else callback.from_user.id
    _spawn(_run_test_post(callback.bot, pair, chat_id))

@menu_router.callback_query(F.data.regexp(r"^pair_toggle_(\d+)$"))
async def cb_toggle_pair(callback: CallbackQuery, state: FSMContext = None):
    pair = await _get_accessible_pair(callback, 2)
    if not pair:
        return

    requester_is_admin = is_admin_user(callback.from_user.id)
    new_status, error = await db_manager.set_pair_active_by_owner(pair.id, not pair.is_active, bypass_limits=requester_is_admin)
    if error == "not_found":
        await safe_answer(callback, PAIR_NOT_FOUND_TEXT, show_alert=True)
        return
    if error in PAIR_RESUME_REFUSAL_TEXTS:
        await safe_answer(callback, PAIR_RESUME_REFUSAL_TEXTS[error], show_alert=True)
        return
    if error in ("subscription_inactive", "plan_limit"):
        await safe_answer(callback)
        if error == "subscription_inactive":
            reason = (
                f"{LOCK_LOCKED} <b>Obunangiz faol emas!</b>\n\n"
                f"Kanal klonlashni qayta yoqish uchun sinov muddati yoki tarif faol bo'lishi kerak. "
                f"<b>{STARS} Tariflar & Obuna</b> bo'limidan tarif tanlang."
            )
        else:
            sub = await db_manager.get_user_subscription(pair.user_id)
            reason = (
                f"{LOCK_LOCKED} <b>Faol kanallar limiti to'lgan!</b>\n\n"
                f"Tarifingiz bo'yicha bir vaqtda <b>{sub.max_channels} ta</b> kanal faol bo'lishi mumkin. "
                f"Boshqa kanalni to'xtating yoki <b>{STARS} Tariflar & Obuna</b> bo'limidan tarifni oshiring."
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Tariflar & Obuna", style="success", icon_custom_emoji_id=ID_STARS, callback_data="menu_stars")],
            [InlineKeyboardButton(text="Orqaga", style="danger", icon_custom_emoji_id=ID_BACK, callback_data=f"pair_view_{pair.id}")]
        ])
        await edit_or_send(callback, reason, parse_mode="HTML", reply_markup=kb)
        return

    await safe_answer(callback, "Kanal klonlash faollashtirildi" if new_status else "Kanal klonlash to'xtatildi")
    pair = await db_manager.get_pair_by_id(pair.id)
    if pair:
        await render_pair_detail(pair, callback.message)

@menu_router.callback_query(F.data.regexp(r"^pair_toggle_clean_(\d+)$"))
async def cb_toggle_clean(callback: CallbackQuery, state: FSMContext = None):
    pair = await _get_accessible_pair(callback, 3)
    if not pair:
        return

    new_clean = await db_manager.toggle_clean_links(pair.id)
    await safe_answer(callback, "Link tozalash yoqildi" if new_clean else "Link tozalash o'chirildi")
    pair = await db_manager.get_pair_by_id(pair.id)
    if pair:
        await render_pair_detail(pair, callback.message)

@menu_router.callback_query(F.data.regexp(r"^pair_toggle_catchup_(\d+)$"))
async def cb_toggle_catchup(callback: CallbackQuery, state: FSMContext = None):
    pair = await _get_accessible_pair(callback, 3)
    if not pair:
        return

    new_val = await db_manager.toggle_auto_catchup(pair.id)
    await safe_answer(callback, "Avto-yetkazish (Catch-Up) yoqildi" if new_val else "Avto-yetkazish o'chirildi")
    pair = await db_manager.get_pair_by_id(pair.id)
    if pair:
        await render_pair_detail(pair, callback.message)

async def _run_user_catchup(status_msg: Message, pairs: List[ChannelPair], user_id: int):
    """Background catch-up of one user's active pairs; the report replaces the status message."""
    try:
        total_caught = 0
        details = []
        for p in pairs:
            src_name = preview(p.source_title or p.source_channel, 60)
            tgt_name = preview(p.target_title or p.target_channel, 60)
            try:
                res = await telethon_listener.catch_up_pair_messages(p)
                c = res.get("caught_up", 0)
                status = res.get("status", "unknown")
                total_caught += c
                if c > 0:
                    details.append(f"• <b>{src_name} {FORWARD} {tgt_name}:</b> {c} ta post yetkazildi")
                elif status == "up_to_date":
                    details.append(f"• <b>{src_name} {FORWARD} {tgt_name}:</b> yangi post yo'q (to'liq)")
                elif status == "baseline_established":
                    details.append(f"• <b>{src_name} {FORWARD} {tgt_name}:</b> boshlang'ich chegara o'rnatildi")
                else:
                    details.append(f"• <b>{src_name} {FORWARD} {tgt_name}:</b> {html_escape(str(status)[:100])}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Catchup error on pair #{p.id}: {e}")
                details.append(f"• #{p.id}: Xatolik ({html_escape(str(e)[:100])})")

        header = (
            f"{SUCCESS} <b>Oflayn xabarlarni yetkazish yakunlandi!</b>\n\n"
            f"{INBOX} Jami yetkazilgan postlar: <b>{total_caught} ta</b>\n"
        )
        chunks = _chunk_report(header, details)
        try:
            await show_in_place(status_msg, chunks[0], parse_mode="HTML")
        except Exception:
            await status_msg.answer(chunks[0], parse_mode="HTML")
        for chunk in chunks[1:]:
            await status_msg.answer(chunk, parse_mode="HTML")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"User catch-up for {user_id} failed: {e}", exc_info=True)
    finally:
        _catchup_running_users.discard(user_id)

# Admins get the global catch-up of the embedded admin router for /catchup
@menu_router.message(Command("catchup"), ~IsAdminFilter())
async def cmd_catchup(message: Message):
    """Manually triggers catch-up for all active pairs belonging to the user"""
    user_id = message.from_user.id
    if not telethon_listener.is_connected():
        await message.answer(
            f"{WARN} <b>Tizimning MTProto ulanishi hozir faol emas.</b>\n"
            f"Iltimos, birozdan so'ng qayta urinib ko'ring. Muammo davom etsa, administratorga murojaat qiling.",
            parse_mode="HTML"
        )
        return
    if user_id in _catchup_running_users:
        await message.answer(f"{INFO} Oflayn postlarni tekshirish allaqachon davom etmoqda. Iltimos, tugashini kuting.", parse_mode="HTML")
        return

    user_pairs = await db_manager.get_user_channel_pairs(user_id)
    active_user_pairs = [p for p in user_pairs if p.is_active]
    if not active_user_pairs:
        await message.answer(f"{INFO} <b>Sizda faol kanallar juftligi mavjud emas.</b>", parse_mode="HTML")
        return

    status_msg = await message.answer(
        f"{REFRESH} <b>Oflayn qolgan postlar tekshirilmoqda...</b>\n\n"
        f"Jami tekshiriladigan faol kanallar: <b>{len(active_user_pairs)} ta</b>\n"
        f"Iltimos, kuting...",
        parse_mode="HTML"
    )
    # Runs in the background: the per-user update lock must not be held for the whole catch-up
    _catchup_running_users.add(user_id)
    _spawn(_run_user_catchup(status_msg, active_user_pairs, user_id))

@menu_router.callback_query(F.data.startswith("pair_delete_confirm_"))
async def cb_delete_confirm(callback: CallbackQuery):
    pair = await _get_accessible_pair(callback, 3)
    if not pair:
        return

    await safe_answer(callback)
    src_title = preview(pair.source_title or pair.source_channel, 100)
    tgt_title = preview(pair.target_title or pair.target_channel, 100)
    text = (
        f"{WARN} <b>Haqiqatan ham #{pair.id} raqamli kanal juftligini o'chirmoqchimisiz?</b>\n\n"
        f"├ <b>Manba:</b> {src_title}\n"
        f"└ <b>Maqsad:</b> {tgt_title}"
    )
    await edit_or_send(callback, text, parse_mode="HTML", reply_markup=get_delete_confirmation_keyboard(pair.id))

@menu_router.callback_query(F.data.startswith("pair_delete_yes_"))
async def cb_delete_yes(callback: CallbackQuery, state: FSMContext = None):
    pair = await _get_accessible_pair(callback, 3)
    if not pair:
        return

    await safe_answer(callback, "Kanal muvaffaqiyatli o'chirildi")
    await db_manager.delete_pair(pair.id)
    try:
        telethon_listener.cancel_history_clone(pair.id)
    except Exception:
        logger.debug("Ignored exception", exc_info=True)
    # The listener re-reads the active pairs lazily; no global re-join / catch-up is needed for a deletion
    telethon_listener.invalidate_pairs_cache()
    if state:
        await state.clear()
    await _show_pairs_list(callback)


@router.message(F.migrate_to_chat_id)
async def handle_aiogram_chat_migration(message: Message):
    """
    Handles group to supergroup migration in Telegram Bot API.
    Updates target_id and source_id for all matching channel pairs.
    """
    old_chat_id = message.chat.id
    new_chat_id = message.migrate_to_chat_id
    if not new_chat_id:
        return
    logger.info(f"🔄 Aiogram group migration detected: {old_chat_id} -> {new_chat_id}")
    try:
        all_pairs = await db_manager.get_all_active_pairs()
        migrated_count = 0
        for p in all_pairs:
            if p.source_id == old_chat_id:
                await db_manager.update_pair_source_id(p.id, new_chat_id)
                migrated_count += 1
            if p.target_id == old_chat_id:
                await db_manager.update_pair_target_id(p.id, new_chat_id)
                migrated_count += 1

        if migrated_count > 0:
            telethon_listener.invalidate_pairs_cache()
            logger.info(f"Aiogram updated {migrated_count} channel pair(s) following migration from {old_chat_id} to {new_chat_id}")
    except Exception as e:
        logger.error(f"Error handling Aiogram chat migration: {e}", exc_info=True)
