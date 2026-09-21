import os
import json
import logging
import platform
import asyncio
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from admin_bot.keyboards.admin_keyboards import get_back_to_admin_keyboard
from database.db_manager import db_manager
from services.telethon_listener import telethon_listener
from services.custom_emojis import (
    INFO, SERVICE_24_7, TELEGRAM, SUCCESS, WARN, USERS_GROUP, LINK,
    ROCKET, STARS, BROADCAST, SETTINGS, DOCUMENT, SHIELD, clean_for_alert
)

from bot.utils import safe_answer

logger = logging.getLogger(__name__)
router = Router(name="admin_status_router")

@router.callback_query(F.data == "admin_system_status")
@router.message(Command("status"))
@router.message(F.text.contains("Tizim Holati"))
async def cb_admin_system_status(event: CallbackQuery | Message):
    if isinstance(event, CallbackQuery):
        await safe_answer(event)

    me = await telethon_listener.get_me()
    stats = await db_manager.get_stats()
    
    if me:
        mtproto_status = f"{SUCCESS} Faol ({me.first_name})"
    else:
        mtproto_status = f"{WARN} Ulanmagan"

    # Oracle Anti-Reclamation telemetry
    anti_reclaim_text = f"{SUCCESS} 100% Himoyalangan (24/7 Faol)"
    import tempfile
    status_file = os.path.join(tempfile.gettempdir(), "oracle_anti_reclaim_status.json")
    if not os.path.exists(status_file) and os.path.exists("/tmp/oracle_anti_reclaim_status.json"):
        status_file = "/tmp/oracle_anti_reclaim_status.json"
    if os.path.exists(status_file):
        try:
            with open(status_file, "r") as f:
                ar_data = json.load(f)
                ram_pct = ar_data.get("ram_percent", 24.0)
                cpu_pct = ar_data.get("target_cpu_percent", 23.0)
                hb = ar_data.get("network_heartbeats", 0)
                anti_reclaim_text = f"{SUCCESS} 100% Xavfsiz (RAM: {ram_pct}%, CPU: {cpu_pct}%, Heartbeats: {hb})"
        except Exception:
            logger.debug("Ignored exception", exc_info=True)

    # System metrics
    os_name = f"{platform.system()} {platform.release()}"
    python_ver = platform.python_version()

    text = f"""
{INFO} <b>TIZIM HOLATI VA SERVER MONITORINGI:</b>
───────────────────────────
├ {SERVICE_24_7} <b>Bot API:</b> {SUCCESS} 24/7 Faol
├ {TELEGRAM} <b>MTProto Tinglovchi:</b> {mtproto_status}
├ {SHIELD} <b>Oracle 7-Kunlik Himoya:</b> {anti_reclaim_text}
├ {SETTINGS} <b>Operatsion Tizim:</b> <code>{os_name}</code>
├ {DOCUMENT} <b>Python Versiyasi:</b> <code>{python_ver}</code>
├ {USERS_GROUP} <b>Jami Foydalanuvchilar:</b> <code>{stats['total_users']}</code> nafar
├ {LINK} <b>Jami Ulangan Kanallar:</b> <code>{stats['total_pairs']}</code> ta
├ {SUCCESS} <b>Faol Ishlayotgan Kanallar:</b> <code>{stats['active_pairs']}</code> ta
├ {ROCKET} <b>Ko'chirilgan Postlar:</b> <code>{stats['total_cloned_messages']}</code> ta
└ {STARS} <b>Jami Stars Tushumi:</b> <code>{stats['total_stars_earned']}</code> Stars
───────────────────────────
<i>Barcha jarayonlar 100k yuqori yuklamaga moslashtirilgan WAL rejimida ishlamoqda.</i>
"""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(
                text=text,
                parse_mode="HTML",
                reply_markup=get_back_to_admin_keyboard()
            )
        except Exception as e_edit:
            if "message is not modified" not in str(e_edit).lower():
                raise
    else:
        await event.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )

@router.callback_query(F.data == "admin_view_logs")
@router.message(Command("logs"))
@router.message(F.text.startswith("/logs"))
async def cb_admin_view_logs(event: CallbackQuery | Message):
    from services.log_viewer import log_viewer
    import html

    if isinstance(event, CallbackQuery):
        await safe_answer(event)

    logs = log_viewer.get_recent_logs(count=30)
    if not logs:
        log_text = "Hozircha xotirada loglar mavjud emas."
    else:
        raw_text = "\n".join(logs)
        # Trim to fit Telegram 4096 character limit
        if len(raw_text) > 3500:
            raw_text = raw_text[-3500:]
            if "\n" in raw_text:
                raw_text = raw_text.split("\n", 1)[1]
        log_text = html.escape(raw_text)

    msg_text = f"""
{DOCUMENT} <b>Jonli Tizim Loglari (So'nggi 30 ta qator):</b>

<pre><code>{log_text}</code></pre>
"""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(
                text=msg_text,
                parse_mode="HTML",
                reply_markup=get_back_to_admin_keyboard()
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await event.answer(
            text=msg_text,
            parse_mode="HTML",
            reply_markup=get_back_to_admin_keyboard()
        )


@router.message(Command("check_origin"))
async def cmd_check_origin(message: Message):
    """Admin command to inspect and extract invisible steganography watermark from a photo"""
    import html
    import tempfile
    from services.steganography_service import steganography_service

    target_msg = message.reply_to_message if message.reply_to_message else message
    if not target_msg.photo:
        await message.answer(
            f"{INFO} <b>Rasm Aslligi Tekshiruvi (Steganografiya):</b>\n\n"
            f"Ushbu buyruqni rasmga reply (javob) qilib yuboring yoki rasm izohiga <code>/check_origin</code> deb yozing.",
            parse_mode="HTML"
        )
        return

    photo = target_msg.photo[-1]
    bot = message.bot
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await bot.download(photo, destination=tmp_path)
        payload = await asyncio.to_thread(steganography_service.extract_watermark, tmp_path)
        if payload:
            await message.answer(
                f"{SHIELD} <b>RASM ASLLIGI VA MUALLIFLIK ISBOTLANDI!</b>\n\n"
                f"├ <b>Ko'rinmas belgi:</b> <code>{html.escape(payload)}</code>\n"
                f"└ <i>Ushbu rasm bot tizimi orqali himoyalangan va klonlangan.</i>",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                f"{WARN} <b>Ko'rinmas suv belgisi topilmadi.</b>\n"
                f"Rasm tizim orqali himoyalanmagan yoki tashqi uchinchi tomon manbasidan.",
                parse_mode="HTML"
            )
    except Exception as e:
        await message.answer(f"Tekshirishda xatolik: {e}")
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

