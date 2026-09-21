import asyncio
import time
import logging
import os
import re
import html
import io
from typing import List, Optional, Union, Tuple, Dict, Any
from aiogram import Bot
from aiogram.types import (
    FSInputFile,
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    Message as AiogramMessage
)
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError, TelegramBadRequest
from telethon.tl.types import Message as TelethonMessage
from telethon.extensions import html as telethon_html
from database.models import ChannelPair
from database.db_manager import db_manager
from services.text_processor import TextProcessor
from services.media_handler import media_handler
from services.translator_service import translator_service
from services.watermark_service import watermark_service
from services.video_watermark_service import video_watermark_service
from services.ai_paraphraser import ai_paraphraser
from services.dynamic_affiliate_engine import dynamic_affiliate_engine
from services.drip_feed_queue import drip_feed_service
from services.disaster_recovery import disaster_recovery_service
from services.affiliate_replacer import affiliate_replacer
from services.rate_limiter import rate_limiter
from services.cache_manager import cache_manager
from services.emoji_converter import emoji_converter
from services.fast_telethon import fast_telethon
from services.button_remapper import button_remapper
from services.ai_ad_detector import ai_ad_detector
from config.settings import settings
from services.custom_emojis import (
    ROCKET, SUCCESS, ERROR, WARN, DOCUMENT, LINK, CLEAN, TRANSLATE, IMAGE, PARTY, STAR_SPARKLE
)

logger = logging.getLogger(__name__)

def extract_message_html(message: TelethonMessage) -> str:
    """Extracts rich-formatted HTML from TelethonMessage, preserving bold, italic, spoilers, and custom emojis"""
    if not message:
        return ""
    if hasattr(message, 'entities') and message.entities and hasattr(message, 'message') and message.message:
        try:
            return telethon_html.unparse(message.message, message.entities)
        except Exception:
            logger.debug("Ignored exception", exc_info=True)
    return getattr(message, 'text', '') or getattr(message, 'message', '') or ""

def is_private_chat_target(target_chat_id: Any) -> bool:
    """
    Returns True if target_chat_id corresponds to a private Telegram user.
    Telegram channels and supergroups ALWAYS have negative IDs (< 0, e.g. -100...).
    Positive IDs (> 0) exclusively belong to private Telegram users.
    """
    if target_chat_id is None:
        return False
    # Check for Telethon / Aiogram User entity instances
    type_name = type(target_chat_id).__name__
    if "User" in type_name and not ("Chat" in type_name or "Channel" in type_name):
        return True
    if isinstance(target_chat_id, int):
        return target_chat_id > 0
    if isinstance(target_chat_id, str):
        s = target_chat_id.strip()
        if s.startswith("-"):
            return False
        if s.isdigit():
            return True
    return False

class ClonerEngine:
    def __init__(self, bot: Optional[Bot] = None):
        self.bot = bot
        self._telethon_admin_required_targets: Dict[str, float] = {}

    def _is_telethon_admin_required(self, target_key: str) -> bool:
        if target_key in self._telethon_admin_required_targets:
            if time.time() - self._telethon_admin_required_targets[target_key] < 300.0:
                return True
            try:
                del self._telethon_admin_required_targets[target_key]
            except KeyError:
                pass
        return False

    def clear_telethon_admin_required_targets(self):
        """Clears temporary admin permission blocklist during channel refresh"""
        self._telethon_admin_required_targets.clear()

    def set_bot(self, bot: Bot):
        self.bot = bot

    async def send_test_post(self, pair: ChannelPair) -> Tuple[bool, str]:
        """Sends a verification test message to the target channel with animated emojis"""
        if not self.bot:
            return False, "Bot ishga tushmagan!"

        target_chat_id = pair.target_id or self._normalize_chat_id(pair.target_channel)
        if is_private_chat_target(target_chat_id):
            return False, "Xatolik: Shaxsiy profilga test xabari yuborib bo'lmaydi! Maqsad faqat kanal yoki superguruh bo'lishi shart."
        
        clean_status = f"{SUCCESS} Yoqilgan" if pair.clean_links else f"{ERROR} O'chirilgan"
        trans_status = f"{SUCCESS} {pair.target_lang.upper()}" if pair.auto_translate else f"{ERROR} O'chirilgan"
        wm_status = pair.image_watermark_text if pair.image_watermark_text else (f"{SUCCESS} Yoqilgan" if pair.image_watermark_type != "none" else f"{ERROR} O'chirilgan")
        emoji_status = f"{SUCCESS} Yoqilgan (VIP)" if pair.auto_premium_emojis else f"{ERROR} O'chirilgan"

        src_title = html.escape(pair.source_title or pair.source_channel)
        tgt_title = html.escape(pair.target_title or pair.target_channel)

        sample_text = f"""
{ROCKET} <b>Telegram Kloner — Test Xabari!</b>

Kanalingiz botga muvaffaqiyatli ulandi va sozlamalar tekshirildi.

{DOCUMENT} <b>Juftlik ma'lumotlari:</b>
├ {LINK} <b>Manba kanal:</b> {src_title} (<code>{pair.source_channel}</code>)
├ {LINK} <b>Maqsadli kanal:</b> {tgt_title} (<code>{pair.target_channel}</code>)
├ {CLEAN} <b>Reklama tozalash:</b> {clean_status}
├ {TRANSLATE} <b>Avto-Tarjima:</b> {trans_status}
├ {IMAGE} <b>Suv belgisi (Watermark):</b> {wm_status}
└ {STAR_SPARKLE} <b>Telegram Premium Emojilar:</b> {emoji_status}

<i>Endi manba kanaldagi yangi xabarlar to'g'ridan-to'g'ri shu yerga nusxalanadi!</i>
"""
        if pair.custom_signature and not pair.remove_signature:
            sample_text = TextProcessor.attach_signature(sample_text, pair.custom_signature)

        if pair.auto_premium_emojis:
            sample_text = emoji_converter.convert_to_premium_emojis(sample_text)

        # Try Telethon first for full VIP custom emoji support
        sent = None
        if pair.auto_premium_emojis:
            sent = await self._telethon_send_post(
                target_chat_id=target_chat_id,
                media_type="text",
                caption=sample_text
            )

        if not sent:
            clean_sample_text = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', sample_text, flags=re.DOTALL | re.IGNORECASE)
            try:
                sent = await self._send_with_retry(
                    self.bot.send_message,
                    chat_id=target_chat_id,
                    text=clean_sample_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except TelegramBadRequest as e:
                return False, f"Xatolik: Bot kanalda administrator emas yoki ruxsat yetarli emas! ({e})"
            except Exception as e:
                return False, f"Xatolik: {e}"

        if hasattr(sent, 'id') and isinstance(sent.id, int):
            msg_id = sent.id
        elif hasattr(sent, 'message_id') and isinstance(sent.message_id, int):
            msg_id = sent.message_id
        else:
            msg_id = getattr(sent, 'message_id', None) or getattr(sent, 'id', 'OK')
        return True, f"Test xabari {tgt_title} kanaliga muvaffaqiyatli yuborildi! (ID: {msg_id})"

    async def process_post_text(self, raw_text: str, pair: ChannelPair) -> Optional[str]:
        if raw_text is None:
            raw_text = ""

        if pair.blacklist_list and TextProcessor.contains_blacklisted_words(raw_text, pair.blacklist_list):
            return None

        current_text = raw_text

        protected_aff_urls = {}
        if pair.affiliate_rules and current_text.strip():
            current_text = affiliate_replacer.replace_affiliate_links(current_text, pair.affiliate_rules)
            try:
                parsed_rules = affiliate_replacer.parse_rules(str(pair.affiliate_rules))
                for idx, (_, target_url) in enumerate(parsed_rules.items()):
                    if target_url in current_text:
                        placeholder = f"___AFF_PROT_{idx}___"
                        protected_aff_urls[placeholder] = target_url
                        current_text = current_text.replace(target_url, placeholder)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        if (pair.clean_links or getattr(pair, "clone_mode", "clean") == "clean") and current_text.strip():
            if TextProcessor.is_commercial_ad(current_text):
                logger.info(f"Commercial advertisement blocked for pair #{pair.id}: {current_text[:60]}...")
                return None
            current_text = TextProcessor.clean_links_and_usernames(current_text)

        # AI Semantic Ad & Casino Shield
        ad_action = getattr(pair, "ad_action", "clean") or "clean"
        if ad_action != "off" and current_text.strip():
            should_pub, current_text = await ai_ad_detector.process_ad_action(
                current_text,
                action=ad_action,
                swap_signature=pair.custom_signature
            )
            if not should_pub or not current_text:
                logger.info(f"AI Ad Shield dropped commercial/casino post for pair #{pair.id}")
                return None

        if protected_aff_urls:
            for placeholder, target_url in protected_aff_urls.items():
                current_text = current_text.replace(placeholder, target_url)

        if pair.replace_dict and current_text.strip():
            current_text = TextProcessor.apply_word_replacements(current_text, pair.replace_dict)

        if pair.auto_translate and current_text.strip():
            try:
                async with cache_manager.translate_semaphore:
                    current_text = await translator_service.translate_text(
                        current_text,
                        target_lang=pair.target_lang or "uz",
                        source_lang=pair.source_lang or "auto"
                    )
            except Exception as e:
                logger.error(f"Auto-translation failed: {e}")

        # AI Paraphraser & Tone Shifter & VIP Premium Emojis (Subscription-gated)
        sub = None
        is_admin = False
        active_tone = pair.tone_of_voice if getattr(pair, "tone_of_voice", "standard") not in ["standard", "off", None] else pair.ai_paraphrase_mode
        need_sub_check = (active_tone and active_tone != "off") or pair.auto_premium_emojis
        if need_sub_check and current_text.strip():
            sub = await db_manager.get_user_subscription(pair.user_id)
            is_admin = pair.user_id in settings.admin_ids or await db_manager.is_admin(pair.user_id)

            # AI Paraphraser & Tone Shifter (Pro/VIP exclusive with active status)
            if active_tone and active_tone != "off":
                if is_admin or (sub.is_active and sub.tier in ["pro", "vip"]):
                    try:
                        current_text = await ai_paraphraser.paraphrase_async(current_text, mode=active_tone)
                    except Exception as e_para:
                        logger.warning(f"AI paraphrasing notice for pair #{pair.id}: {e_para}")

        if pair.remove_signature and current_text:
            current_text = TextProcessor.strip_source_signature(current_text)

        if pair.custom_signature and current_text:
            current_text = TextProcessor.attach_signature(current_text, pair.custom_signature)

        # VIP / Pro / Trial: Convert standard Unicode emojis into animated Telegram Premium custom emojis
        if pair.auto_premium_emojis and current_text.strip():
            if sub is None:
                sub = await db_manager.get_user_subscription(pair.user_id)
                is_admin = pair.user_id in settings.admin_ids or await db_manager.is_admin(pair.user_id)
            if is_admin or (sub and sub.is_active and (sub.tier in ["vip", "pro"] or sub.is_trial_active)):
                current_text = emoji_converter.convert_to_premium_emojis(current_text)

        return current_text

    @staticmethod
    def _smart_fit_caption_with_badge(old_cap: str, badge: str, max_len: int = 1024) -> str:
        """Intelligently fits a badge onto an existing caption, trimming old text if needed so total <= max_len."""
        combined = (old_cap or "") + badge
        if len(combined) <= max_len:
            return combined
        allowed_old = max_len - len(badge) - 3
        if allowed_old > 0:
            return (old_cap[:allowed_old]).rstrip() + "..." + badge
        return badge[:max_len]

    @staticmethod
    def _is_self_loop(pair: ChannelPair) -> bool:
        s_norm = db_manager._normalize_channel_name(pair.source_channel)
        t_norm = db_manager._normalize_channel_name(pair.target_channel)
        s_id_str = str(pair.source_id).replace("-100", "").lstrip("-") if pair.source_id else ""
        t_id_str = str(pair.target_id).replace("-100", "").lstrip("-") if pair.target_id else ""
        if s_id_str and t_id_str and s_id_str == t_id_str:
            return True
        if s_norm and t_norm and s_norm == t_norm:
            return True
        if (s_id_str and s_id_str == t_norm) or (t_id_str and t_id_str == s_norm):
            return True
        return False

    async def clone_single_message(self, message: TelethonMessage, pair: ChannelPair) -> bool:
        if not self.bot:
            logger.error("Bot instance not set in ClonerEngine.")
            return False

        if not pair.is_active:
            return False

        # Safeguard against self-cloning loops
        if self._is_self_loop(pair):
            logger.error(f"Infinite loop detected: pair #{pair.id} has identical source and target ({pair.source_channel}). Skipping.")
            return False

        # Verify active subscription / 14-day trial for channel owner
        sub = await db_manager.get_user_subscription(pair.user_id)
        is_admin = pair.user_id in settings.admin_ids or await db_manager.is_admin(pair.user_id)
        if not is_admin and not sub.is_active:
            logger.warning(f"Subscription or 14-day trial expired for user {pair.user_id}. Skipping post {message.id} for pair #{pair.id}")
            return False

        if await db_manager.is_message_cloned(pair.id, message.id):
            logger.debug(f"Message {message.id} already cloned for pair {pair.id}. Skipping.")
            return False

        media_type = media_handler.get_media_type(message)
        raw_text = extract_message_html(message)
        processed_text = await self.process_post_text(raw_text, pair)

        if processed_text is None and raw_text:
            logger.info(f"Message {message.id} blocked by blacklist for pair {pair.id}.")
            return False

        # Build dynamic CTA buttons and remap source buttons if present
        cta_markup = None
        telethon_buttons = None
        source_buttons = button_remapper.extract_telethon_buttons(message)

        if pair.auto_cta_buttons and processed_text:
            processed_text, cta_links = dynamic_affiliate_engine.extract_and_convert_links(processed_text, pair.affiliate_rules)
            cta_markup = dynamic_affiliate_engine.build_cta_keyboard(cta_links)
            telethon_buttons = dynamic_affiliate_engine.build_telethon_buttons(cta_links)

        # If source message contains inline buttons, remap them and merge
        if source_buttons:
            target_link = f"https://t.me/{pair.target_channel.lstrip('@')}" if str(pair.target_channel or "").startswith("@") else None
            remapped_kb = button_remapper.build_remapped_markup(
                source_buttons=source_buttons,
                target_channel_link=target_link,
                block_competitor_links=True
            )
            if remapped_kb:
                if cta_markup and hasattr(cta_markup, "inline_keyboard"):
                    cta_markup.inline_keyboard = remapped_kb.inline_keyboard + cta_markup.inline_keyboard
                else:
                    cta_markup = remapped_kb

        # Ensure Telethon can also send buttons if telethon_buttons is empty
        if cta_markup and not telethon_buttons:
            try:
                from telethon import Button
                tb_grid = []
                for row in getattr(cta_markup, "inline_keyboard", []):
                    row_btns = [Button.url(text=b.text, url=b.url) for b in row if getattr(b, "url", None)]
                    if row_btns:
                        tb_grid.append(row_btns)
                if tb_grid:
                    telethon_buttons = tb_grid
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        # Apply smart rate limiter delay
        await rate_limiter.wait_for_slot(pair.target_channel)

        target_chat_id = pair.target_id or self._normalize_chat_id(pair.target_channel)
        target_topic_id = getattr(pair, "target_topic_id", None)
        show_caption_above = bool(getattr(pair, "show_caption_above", False))
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Pair #{pair.id} has private user target {target_chat_id}! Aborting clone.")
            return False
        # Check Drip Feed / Night Buffer queueing for text and media posts
        if (pair.drip_delay_minutes > 0 or (pair.night_mode == "buffer" and drip_feed_service.is_night_time())):
            if media_type == "text":
                if not processed_text:
                    return False
                payload = {
                    "text": processed_text,
                    "media_type": "text",
                    "media_file_id": None
                }
                await drip_feed_service.enqueue_post(pair, payload)
                await db_manager.record_cloned_message(
                    pair_id=pair.id,
                    source_msg_id=message.id,
                    target_msg_id=None,
                    media_type="text"
                )
                logger.info(f"Message {message.id} enqueued to drip feed for pair {pair.id}")
                return True
            elif media_type in ["photo", "video", "document", "audio", "animation"]:
                async with cache_manager.media_semaphore:
                    temp_media_file = await media_handler.download_telethon_media(message)
                if temp_media_file and os.path.exists(temp_media_file):
                    if media_type == "photo" and pair.image_watermark_type != "none":
                        wm_text = pair.image_watermark_text or pair.custom_signature or pair.target_channel
                        if wm_text:
                            if pair.image_watermark_type == "logo":
                                temp_media_file = await asyncio.to_thread(
                                    watermark_service.apply_logo_watermark,
                                    temp_media_file,
                                    pair.image_watermark_text or "assets/logo.png",
                                    pair.image_watermark_pos or "bottom_right"
                                )
                            else:
                                temp_media_file = await asyncio.to_thread(
                                    watermark_service.apply_text_watermark,
                                    temp_media_file,
                                    wm_text,
                                    pair.image_watermark_pos or "bottom_right"
                                )
                    elif media_type == "video" and pair.video_watermark_type != "none":
                        wm_text = pair.video_watermark_text or pair.custom_signature or pair.target_channel
                        if wm_text:
                            if pair.video_watermark_type == "logo":
                                wm_video = await video_watermark_service.apply_video_logo_watermark(
                                    input_video_path=temp_media_file,
                                    logo_image_path=pair.video_watermark_text or "assets/logo.png",
                                    pos=pair.video_watermark_pos or "bottom_right"
                                )
                            else:
                                wm_video = await video_watermark_service.apply_video_text_watermark(
                                    input_video_path=temp_media_file,
                                    watermark_text=wm_text,
                                    pos=pair.video_watermark_pos or "bottom_right"
                                )
                            if wm_video and os.path.exists(wm_video):
                                temp_media_file = wm_video
                    queued_path = os.path.join(os.path.dirname(temp_media_file), f"queued_{os.path.basename(temp_media_file)}")
                    try:
                        os.replace(temp_media_file, queued_path)
                        temp_media_file = queued_path
                    except Exception:
                        logger.debug("Ignored exception", exc_info=True)
                    payload = {
                        "text": processed_text or "",
                        "media_type": media_type,
                        "media_file_id": temp_media_file,
                        "watermarked": True
                    }
                    await drip_feed_service.enqueue_post(pair, payload)
                    await db_manager.record_cloned_message(
                        pair_id=pair.id,
                        source_msg_id=message.id,
                        target_msg_id=None,
                        media_type=media_type
                    )
                    logger.info(f"Message {message.id} ({media_type}) enqueued to drip feed for pair {pair.id}")
                    return True

        files_to_cleanup: List[str] = []
        sent_msg: Optional[AiogramMessage] = None
        overflow_text: Optional[str] = None
        detected_single_price: Optional[float] = None  # initialized here to prevent scope error at record_cloned_message

        try:
            if media_type == "text":
                if not processed_text:
                    logger.debug("Empty text message after cleaning. Skipping.")
                    return False
                text_chunks = TextProcessor.fit_text_limit(processed_text, max_limit=4096)
                for chunk_i, chunk_text in enumerate(text_chunks):
                    chunk_markup = cta_markup if chunk_i == len(text_chunks) - 1 else None
                    chunk_telethon_buttons = telethon_buttons if chunk_i == len(text_chunks) - 1 else None
                    chunk_sent = None
                    if pair.auto_premium_emojis:
                        chunk_sent = await self._telethon_send_post(
                            target_chat_id=target_chat_id,
                            media_type="text",
                            file_path=None,
                            caption=chunk_text,
                            buttons=chunk_telethon_buttons
                        )
                    if not chunk_sent:
                        clean_chunk = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', chunk_text, flags=re.DOTALL | re.IGNORECASE)
                        if not clean_chunk.strip():
                            continue
                        send_kw = {
                            "chat_id": target_chat_id,
                            "text": clean_chunk,
                            "parse_mode": "HTML",
                            "reply_markup": chunk_markup,
                            "disable_web_page_preview": False
                        }
                        if target_topic_id:
                            send_kw["message_thread_id"] = target_topic_id
                        chunk_sent = await self._send_with_retry(
                            self.bot.send_message,
                            **send_kw
                        )
                    if not sent_msg:
                        sent_msg = chunk_sent

            elif media_type in ["photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation"]:
                photo_ram_bytes: Optional[bytes] = None
                async with cache_manager.media_semaphore:
                    if media_type == "photo" and pair.image_watermark_type != "logo":
                        photo_ram_bytes = await media_handler.download_telethon_media_bytes(message, max_size_bytes=20 * 1024 * 1024)
                
                if True:

                    if photo_ram_bytes:
                        if pair.image_watermark_type != "none":
                            wm_text = pair.image_watermark_text or pair.custom_signature or pair.target_channel
                            if wm_text:
                                wm_bytes = await asyncio.to_thread(
                                    watermark_service.apply_text_watermark_bytes,
                                    photo_ram_bytes,
                                    wm_text,
                                    pair.image_watermark_pos or "bottom_right"
                                )
                                if wm_bytes:
                                    photo_ram_bytes = wm_bytes

                        detected_single_price = None
                        try:
                            from services.story_cloner_service import story_cloner_service
                            detected_single_price = story_cloner_service.extract_price(processed_text or "")
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)

                        try:
                            from services.image_hasher import image_hasher
                            single_hash = await image_hasher.get_phash_async(photo_ram_bytes)
                            if single_hash:
                                is_dup, is_price_drop, match_info = await image_hasher.check_listing_duplicate(
                                    [single_hash],
                                    current_price=detected_single_price,
                                    pair_id=pair.id,
                                    source_channel=pair.source_channel,
                                    source_msg_id=message.id
                                )
                                if is_dup:
                                    if is_price_drop and match_info:
                                        prev_price = match_info.get("previous_price", 0.0)
                                        logger.info(f"🔥 PRICE DROP DETECTED for RAM photo post #{message.id} (${prev_price} -> ${detected_single_price})")
                                        matched_recs = await db_manager.get_cloned_messages_by_source(
                                            source_channel=match_info.get("matched_channel") or pair.source_channel,
                                            source_msg_id=match_info.get("matched_msg_id")
                                        )
                                        for m_rec in matched_recs:
                                            t_msg_id = m_rec.get("target_msg_id")
                                            t_chan = m_rec.get("target_channel") or m_rec.get("pair_target_channel")
                                            if t_msg_id and t_chan:
                                                drop_badge = f"\n\n🔥 <b>NARX ARZONLASHDI:</b> <s>${prev_price:,.0f}</s> ➡️ <b>${detected_single_price:,.0f}</b>"
                                                new_cap = self._smart_fit_caption_with_badge(old_cap, drop_badge)
                                                try:
                                                    await self.bot.edit_message_caption(chat_id=t_chan, message_id=t_msg_id, caption=new_cap, parse_mode="HTML")
                                                    await db_manager.update_cloned_message_price(m_rec["id"], detected_single_price)
                                                except Exception as pe:
                                                    logger.debug(f"Could not update price drop caption: {pe}")
                                        return True
                                    else:
                                        logger.info(f"pHash: Skipping duplicate RAM photo message #{message.id} for pair #{pair.id}")
                                        return True
                                else:
                                    await image_hasher.save_listing_hashes(
                                        hashes=[single_hash],
                                        source_channel=pair.source_channel,
                                        source_msg_id=message.id,
                                        pair_id=pair.id,
                                        price=detected_single_price
                                    )
                        except Exception as he:
                            logger.warning(f"RAM photo pHash deduplication error: {he}")

                        from services.telethon_listener import telethon_listener
                        is_telethon_avail = telethon_listener.is_connected()
                        if is_telethon_avail and (pair.auto_premium_emojis or (processed_text and len(processed_text) > 1024)):
                            caption_tl, overflow_tl = TextProcessor.fit_caption_limit(processed_text or "", max_limit=2048)
                            sent_msg = await self._telethon_send_post(
                                target_chat_id=target_chat_id,
                                media_type="photo",
                                file_path=photo_ram_bytes,
                                caption=caption_tl or None,
                                buttons=telethon_buttons,
                                topic_id=target_topic_id
                            )
                            if sent_msg and overflow_tl:
                                overflow_text = overflow_tl

                        if not sent_msg:
                            bot_clean_text = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', processed_text or "", flags=re.DOTALL | re.IGNORECASE)
                            caption, overflow = TextProcessor.fit_caption_limit(bot_clean_text, max_limit=1024)
                            overflow_text = overflow
                            photo_kw = {
                                "chat_id": target_chat_id,
                                "photo": BufferedInputFile(photo_ram_bytes, filename="photo.jpg"),
                                "caption": caption or None,
                                "parse_mode": "HTML",
                                "reply_markup": cta_markup
                            }
                            if target_topic_id:
                                photo_kw["message_thread_id"] = target_topic_id
                            if show_caption_above:
                                photo_kw["show_caption_above_media"] = True
                            sent_msg = await self._send_with_retry(
                                self.bot.send_photo,
                                **photo_kw
                            )

                    # Fallback or standard disk pipeline for video, docs, audio, or when RAM download was skipped
                    if not sent_msg and not photo_ram_bytes:
                        async with cache_manager.media_semaphore:
                            temp_file = await media_handler.download_telethon_media(message)
                        if not temp_file or not os.path.exists(temp_file):
                            logger.error(f"Failed to download media for message {message.id}")
                            return False
                        files_to_cleanup.append(temp_file)

                        if media_type == "photo" and pair.image_watermark_type != "none":
                            wm_text = pair.image_watermark_text or pair.custom_signature or pair.target_channel
                            if wm_text:
                                if pair.image_watermark_type == "logo":
                                    wm_photo = await asyncio.to_thread(
                                        watermark_service.apply_logo_watermark,
                                        temp_file,
                                        pair.image_watermark_text or "assets/logo.png",
                                        pair.image_watermark_pos or "bottom_right"
                                    )
                                else:
                                    wm_photo = await asyncio.to_thread(
                                        watermark_service.apply_text_watermark,
                                        temp_file,
                                        wm_text,
                                        pair.image_watermark_pos or "bottom_right"
                                    )
                                if wm_photo and os.path.exists(wm_photo):
                                    if wm_photo not in files_to_cleanup:
                                        files_to_cleanup.append(wm_photo)
                                    temp_file = wm_photo

                        elif media_type == "video" and pair.video_watermark_type != "none":
                            if is_admin or sub.tier in ["pro", "vip"]:
                                wm_text = pair.video_watermark_text or pair.image_watermark_text or pair.target_channel
                                if wm_text:
                                    if pair.video_watermark_type == "logo":
                                        wm_video = await video_watermark_service.apply_video_logo_watermark(
                                            input_video_path=temp_file,
                                            logo_image_path=pair.video_watermark_text or "assets/logo.png",
                                            pos=pair.video_watermark_pos or "bottom_right"
                                        )
                                    else:
                                        wm_video = await video_watermark_service.apply_video_text_watermark(
                                            input_video_path=temp_file,
                                            watermark_text=wm_text,
                                            pos=pair.video_watermark_pos or "bottom_right"
                                        )
                                    if wm_video and os.path.exists(wm_video):
                                        files_to_cleanup.append(wm_video)
                                        temp_file = wm_video

                        if media_type == "photo":
                            detected_single_price = None
                            try:
                                from services.story_cloner_service import story_cloner_service
                                detected_single_price = story_cloner_service.extract_price(processed_text or "")
                            except Exception:
                                logger.debug("Ignored exception", exc_info=True)

                            try:
                                from services.image_hasher import image_hasher
                                single_hash = await image_hasher.get_phash_async(temp_file)
                                if single_hash:
                                    is_dup, is_price_drop, match_info = await image_hasher.check_listing_duplicate(
                                        [single_hash],
                                        current_price=detected_single_price,
                                        pair_id=pair.id,
                                        source_channel=pair.source_channel,
                                        source_msg_id=message.id
                                    )
                                    if is_dup:
                                        if is_price_drop and match_info:
                                            prev_price = match_info.get("previous_price", 0.0)
                                            logger.info(f"🔥 PRICE DROP DETECTED for single photo post #{message.id} (${prev_price} -> ${detected_single_price})")
                                            matched_recs = await db_manager.get_cloned_messages_by_source(
                                                source_channel=match_info.get("matched_channel") or pair.source_channel,
                                                source_msg_id=match_info.get("matched_msg_id")
                                            )
                                            for m_rec in matched_recs:
                                                t_msg_id = m_rec.get("target_msg_id")
                                                t_chan = m_rec.get("target_channel") or m_rec.get("pair_target_channel")
                                                if t_msg_id and t_chan:
                                                    old_cap = m_rec.get("last_caption") or ""
                                                    drop_badge = f"\n\n🔥 <b>NARX ARZONLASHDI:</b> <s>${prev_price:,.0f}</s> ➡️ <b>${detected_single_price:,.0f}</b>"
                                                    new_cap = self._smart_fit_caption_with_badge(old_cap, drop_badge)
                                                    try:
                                                        await self.bot.edit_message_caption(chat_id=t_chan, message_id=t_msg_id, caption=new_cap, parse_mode="HTML")
                                                        await db_manager.update_cloned_message_price(m_rec["id"], detected_single_price)
                                                    except Exception as pe:
                                                        logger.debug(f"Could not update price drop caption: {pe}")
                                            return True
                                        else:
                                            logger.info(f"pHash: Skipping duplicate photo message #{message.id} for pair #{pair.id}")
                                            return True
                                    else:
                                        await image_hasher.save_listing_hashes(
                                            hashes=[single_hash],
                                            source_channel=pair.source_channel,
                                            source_msg_id=message.id,
                                            pair_id=pair.id,
                                            price=detected_single_price
                                        )
                            except Exception as he:
                                logger.warning(f"Single photo pHash deduplication error: {he}")

                        from services.telethon_listener import telethon_listener
                        is_telethon_avail = telethon_listener.is_connected()
                        file_size_mb = os.path.getsize(temp_file) / (1024 * 1024)

                        if is_telethon_avail and (pair.auto_premium_emojis or (processed_text and len(processed_text) > 1024) or file_size_mb > 49.0) and media_type not in ["sticker", "video_note"]:
                            caption_tl, overflow_tl = TextProcessor.fit_caption_limit(processed_text or "", max_limit=2048)
                            sent_msg = await self._telethon_send_post(
                                target_chat_id=target_chat_id,
                                media_type=media_type,
                                file_path=temp_file,
                                caption=caption_tl or None,
                                supports_streaming=(media_type == "video"),
                                buttons=telethon_buttons,
                                topic_id=target_topic_id
                            )
                            if sent_msg and overflow_tl:
                                overflow_text = overflow_tl

                        if not sent_msg:
                            bot_clean_text = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', processed_text or "", flags=re.DOTALL | re.IGNORECASE)
                            caption, overflow = TextProcessor.fit_caption_limit(bot_clean_text, max_limit=1024)
                            overflow_text = overflow
                            input_file = FSInputFile(temp_file)

                            base_media_kw = {"chat_id": target_chat_id}
                            if target_topic_id:
                                base_media_kw["message_thread_id"] = target_topic_id

                            if file_size_mb > 49.0:
                                sent_msg = await self._telethon_send_fallback(target_chat_id, temp_file, caption)
                            elif media_type == "photo":
                                photo_kw = dict(base_media_kw, photo=input_file, caption=caption or None, parse_mode="HTML", reply_markup=cta_markup)
                                if show_caption_above:
                                    photo_kw["show_caption_above_media"] = True
                                sent_msg = await self._send_with_retry(
                                    self.bot.send_photo,
                                    **photo_kw
                                )
                            elif media_type == "video":
                                video_kw = dict(base_media_kw, video=input_file, caption=caption or None, parse_mode="HTML", reply_markup=cta_markup, supports_streaming=True)
                                if show_caption_above:
                                    video_kw["show_caption_above_media"] = True
                                sent_msg = await self._send_with_retry(
                                    self.bot.send_video,
                                    **video_kw
                                )
                            elif media_type == "voice":
                                voice_duration = None
                                try:
                                    if message.media and hasattr(message.media, 'document') and message.media.document.attributes:
                                        for attr in message.media.document.attributes:
                                            if hasattr(attr, 'duration'):
                                                voice_duration = attr.duration
                                                break
                                except Exception:
                                    logger.debug("Ignored exception", exc_info=True)
                                sent_msg = await self._send_with_retry(
                                    self.bot.send_voice,
                                    **base_media_kw,
                                    voice=input_file,
                                    caption=caption or None,
                                    parse_mode="HTML",
                                    duration=voice_duration
                                )
                            elif media_type == "video_note":
                                sent_msg = await self._send_with_retry(
                                    self.bot.send_video_note,
                                    **base_media_kw,
                                    video_note=input_file
                                )
                                if caption and caption.strip():
                                    clean_caption = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', caption, flags=re.DOTALL | re.IGNORECASE)
                                    caption_msg = await self._send_with_retry(
                                        self.bot.send_message,
                                        **base_media_kw,
                                        text=clean_caption,
                                        parse_mode="HTML"
                                    )
                                    if caption_msg:
                                        cap_id = getattr(caption_msg, 'message_id', None) or getattr(caption_msg, 'id', None)
                                        if cap_id:
                                            try:
                                                await db_manager.record_cloned_message(
                                                    pair_id=pair.id,
                                                    source_msg_id=message.id,
                                                    target_msg_id=cap_id,
                                                    media_type="text"
                                                )
                                            except Exception:
                                                logger.debug("Ignored exception", exc_info=True)
                            elif media_type == "audio":
                                sent_msg = await self._send_with_retry(
                                    self.bot.send_audio,
                                    **base_media_kw,
                                    audio=input_file,
                                    caption=caption or None,
                                    parse_mode="HTML",
                                    reply_markup=cta_markup
                                )
                            elif media_type == "sticker":
                                try:
                                    sent_msg = await self._send_with_retry(
                                        self.bot.send_sticker,
                                        **base_media_kw,
                                        sticker=input_file
                                    )
                                except Exception as e_stk:
                                    logger.warning(f"send_sticker failed ({e_stk}), trying send_document fallback")
                                    sent_msg = await self._send_with_retry(
                                        self.bot.send_document,
                                        **base_media_kw,
                                        document=input_file
                                    )
                            elif media_type == "animation":
                                anim_kw = dict(base_media_kw, animation=input_file, caption=caption or None, parse_mode="HTML", reply_markup=cta_markup)
                                if show_caption_above:
                                    anim_kw["show_caption_above_media"] = True
                                sent_msg = await self._send_with_retry(
                                    self.bot.send_animation,
                                    **anim_kw
                                )
                            elif media_type == "document":
                                sent_msg = await self._send_with_retry(
                                    self.bot.send_document,
                                    **base_media_kw,
                                    document=input_file,
                                    caption=caption or None,
                                    parse_mode="HTML",
                                    reply_markup=cta_markup
                                )

            elif media_type == "poll":
                poll_media = message.media
                if not poll_media or not hasattr(poll_media, 'poll'):
                    logger.warning(f"Could not extract poll from message {message.id}")
                    return False
                poll = poll_media.poll
                
                raw_q = getattr(poll, 'question', '') or ''
                if hasattr(raw_q, 'text'):
                    question = str(raw_q.text)
                elif isinstance(raw_q, bytes):
                    question = raw_q.decode('utf-8', errors='ignore')
                else:
                    question = str(raw_q)

                answers = []
                for ans in (poll.answers or []):
                    a_val = getattr(ans, 'text', '')
                    if hasattr(a_val, 'text'):
                        answers.append(str(a_val.text))
                    elif isinstance(a_val, bytes):
                        answers.append(a_val.decode('utf-8', errors='ignore'))
                    else:
                        answers.append(str(a_val))

                # Telegram Bot API limits: question max 300, answers max 10 of 100 chars
                question = question[:300] if len(question) > 300 else question
                answers = [ans[:100] for ans in answers[:10]]
                if len(answers) < 2:
                    logger.warning(f"Poll has fewer than 2 valid options ({len(answers)}). Skipping poll for message {message.id}")
                    return False

                is_anonymous = not getattr(poll, 'public_voters', False)
                multiple_answers = getattr(poll, 'multiple_choice', False)
                poll_kw = {
                    "chat_id": target_chat_id,
                    "question": question,
                    "options": answers,
                    "is_anonymous": is_anonymous,
                    "allows_multiple_answers": multiple_answers
                }
                if target_topic_id:
                    poll_kw["message_thread_id"] = target_topic_id
                sent_msg = await self._send_with_retry(
                    self.bot.send_poll,
                    **poll_kw
                )
            elif media_type == "contact" and message.media:
                c = message.media
                contact_kw = {
                    "chat_id": target_chat_id,
                    "phone_number": getattr(c, 'phone_number', '') or '',
                    "first_name": getattr(c, 'first_name', '') or '',
                    "last_name": getattr(c, 'last_name', '') or None
                }
                if target_topic_id:
                    contact_kw["message_thread_id"] = target_topic_id
                sent_msg = await self._send_with_retry(
                    self.bot.send_contact,
                    **contact_kw
                )

            elif media_type == "location" and message.media:
                geo = getattr(message.media, 'geo', None)
                if not geo:
                    logger.warning(f"No valid geo coordinates found for message {message.id}")
                    return False

                if hasattr(message.media, 'title') and hasattr(message.media, 'address'):
                    # Venue
                    venue_kw = {
                        "chat_id": target_chat_id,
                        "latitude": geo.lat,
                        "longitude": geo.long,
                        "title": getattr(message.media, 'title', ''),
                        "address": getattr(message.media, 'address', '')
                    }
                    if target_topic_id:
                        venue_kw["message_thread_id"] = target_topic_id
                    sent_msg = await self._send_with_retry(
                        self.bot.send_venue,
                        **venue_kw
                    )
                else:
                    loc_kw = {
                        "chat_id": target_chat_id,
                        "latitude": geo.lat,
                        "longitude": geo.long
                    }
                    if target_topic_id:
                        loc_kw["message_thread_id"] = target_topic_id
                    sent_msg = await self._send_with_retry(
                        self.bot.send_location,
                        **loc_kw
                    )

            if not sent_msg:
                if media_type not in ["photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation", "poll", "venue", "location"] and processed_text:
                    fallback_kw = {
                        "chat_id": target_chat_id,
                        "text": processed_text,
                        "parse_mode": "HTML",
                        "reply_markup": cta_markup,
                        "disable_web_page_preview": False
                    }
                    if target_topic_id:
                        fallback_kw["message_thread_id"] = target_topic_id
                    sent_msg = await self._send_with_retry(
                        self.bot.send_message,
                        **fallback_kw
                    )
                else:
                    logger.error(f"Failed to clone message {message.id} ({media_type}) to {target_chat_id} - media delivery failed")
                    return False

            if sent_msg:
                if overflow_text and TextProcessor.get_visible_text_length(overflow_text) > 0:
                    try:
                        clean_overflow = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', overflow_text, flags=re.DOTALL | re.IGNORECASE)
                        for o_chunk in TextProcessor.fit_text_limit(clean_overflow, max_limit=4096):
                            overflow_kw = {
                                "chat_id": target_chat_id,
                                "text": o_chunk,
                                "parse_mode": "HTML"
                            }
                            if target_topic_id:
                                overflow_kw["message_thread_id"] = target_topic_id
                            await self._send_with_retry(
                                self.bot.send_message,
                                **overflow_kw
                            )
                    except Exception as e:
                        logger.warning(f"Could not send overflow text: {e}")

                target_id = getattr(sent_msg, 'message_id', None) or getattr(sent_msg, 'id', None)
                await db_manager.record_cloned_message(
                    pair_id=pair.id,
                    source_msg_id=message.id,
                    target_msg_id=target_id,
                    media_type=media_type,
                    source_channel=pair.source_channel,
                    target_channel=pair.target_channel,
                    price=float(detected_single_price or 0.0),
                    last_caption=processed_text
                )

                if pair.backup_enabled:
                    # BUG #24 & #41 fix: Extract true Telegram file_id for restoration
                    real_file_id = None
                    if hasattr(sent_msg, 'photo') and sent_msg.photo:
                        real_file_id = sent_msg.photo[-1].file_id
                    elif hasattr(sent_msg, 'video') and sent_msg.video:
                        real_file_id = sent_msg.video.file_id
                    elif hasattr(sent_msg, 'document') and sent_msg.document:
                        real_file_id = sent_msg.document.file_id
                    elif hasattr(sent_msg, 'audio') and sent_msg.audio:
                        real_file_id = sent_msg.audio.file_id
                    elif hasattr(sent_msg, 'voice') and sent_msg.voice:
                        real_file_id = sent_msg.voice.file_id
                    elif hasattr(sent_msg, 'animation') and sent_msg.animation:
                        real_file_id = sent_msg.animation.file_id
                    elif hasattr(sent_msg, 'sticker') and sent_msg.sticker:
                        real_file_id = sent_msg.sticker.file_id

                    await disaster_recovery_service.archive_message(
                        pair_id=pair.id,
                        source_id=pair.source_id,
                        message_id=message.id,
                        text=processed_text or "",
                        media_type=media_type,
                        media_file_id=real_file_id
                    )

                logger.info(f"Successfully cloned message {message.id} -> {target_chat_id} (msg {target_id})")
                return True

        except Exception as e:
            logger.error(f"Error cloning message {message.id} to {target_chat_id}: {e}", exc_info=True)
            return False
        finally:
            if files_to_cleanup:
                await media_handler.cleanup_files(files_to_cleanup)

        return False

    async def dispatch_queued_payload(self, bot: Bot, pair: ChannelPair, payload: Dict[str, Any], disable_notification: bool = False):
        """Dispatches an enqueued message payload for drip feed or night buffer delivery"""
        target_chat_id = pair.target_id or self._normalize_chat_id(pair.target_channel)
        target_topic_id = getattr(pair, "target_topic_id", None)
        show_caption_above = bool(getattr(pair, "show_caption_above", False))
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Refusing to dispatch queued payload to private user {target_chat_id}")
            return
        text = payload.get("text", "")
        media_type = payload.get("media_type", "text")
        media_file_id = payload.get("media_file_id")

        try:
            await rate_limiter.wait_for_slot(pair.target_channel)
            base_kw = {"chat_id": target_chat_id, "disable_notification": disable_notification}
            if target_topic_id:
                base_kw["message_thread_id"] = target_topic_id

            if media_type == "text" and text:
                text_chunks = TextProcessor.fit_text_limit(text, max_limit=4096)
                for chunk in text_chunks:
                    clean_chunk = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', chunk, flags=re.DOTALL | re.IGNORECASE)
                    if clean_chunk.strip():
                        await self._send_with_retry(bot.send_message, **base_kw, text=clean_chunk, parse_mode="HTML")
            elif media_type in ["photo", "video", "document", "audio", "animation"] and media_file_id:
                clean_text = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', text or "", flags=re.DOTALL | re.IGNORECASE)
                caption, overflow = TextProcessor.fit_caption_limit(clean_text, max_limit=1024)
                input_file = FSInputFile(media_file_id) if isinstance(media_file_id, str) and os.path.exists(media_file_id) else media_file_id
                if media_type == "photo":
                    p_kw = dict(base_kw, photo=input_file, caption=caption or None, parse_mode="HTML")
                    if show_caption_above:
                        p_kw["show_caption_above_media"] = True
                    await self._send_with_retry(bot.send_photo, **p_kw)
                elif media_type == "video":
                    v_kw = dict(base_kw, video=input_file, caption=caption or None, parse_mode="HTML")
                    if show_caption_above:
                        v_kw["show_caption_above_media"] = True
                    await self._send_with_retry(bot.send_video, **v_kw)
                elif media_type == "document":
                    await self._send_with_retry(bot.send_document, **base_kw, document=input_file, caption=caption or None, parse_mode="HTML")
                elif media_type == "audio":
                    await self._send_with_retry(bot.send_audio, **base_kw, audio=input_file, caption=caption or None, parse_mode="HTML")
                elif media_type == "animation":
                    a_kw = dict(base_kw, animation=input_file, caption=caption or None, parse_mode="HTML")
                    if show_caption_above:
                        a_kw["show_caption_above_media"] = True
                    await self._send_with_retry(bot.send_animation, **a_kw)
                if overflow:
                    await self._send_with_retry(bot.send_message, **base_kw, text=overflow, parse_mode="HTML")
            elif media_type == "media_group" and payload.get("media_files"):
                media_files = payload.get("media_files", [])
                clean_text = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', text or "", flags=re.DOTALL | re.IGNORECASE)
                caption, overflow = TextProcessor.fit_caption_limit(clean_text, max_limit=1024)
                media_group_items = []
                for idx, mf in enumerate(media_files):
                    m_path = mf.get("path")
                    m_type = mf.get("type", "photo")
                    if m_path and os.path.exists(m_path):
                        input_f = FSInputFile(m_path)
                        cap = (caption or None) if idx == 0 else None
                        item_kw = {"media": input_f, "caption": cap, "parse_mode": "HTML"}
                        if show_caption_above and cap:
                            item_kw["show_caption_above_media"] = True
                        if m_type == "video":
                            media_group_items.append(InputMediaVideo(**item_kw))
                        else:
                            media_group_items.append(InputMediaPhoto(**item_kw))
                if media_group_items:
                    await self._send_with_retry(bot.send_media_group, **base_kw, media=media_group_items)
                    if overflow:
                        await self._send_with_retry(bot.send_message, **base_kw, text=overflow, parse_mode="HTML")
                for mf in media_files:
                    m_path = mf.get("path")
                    if m_path and os.path.exists(m_path):
                        try:
                            os.remove(m_path)
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
            elif text:
                text_chunks = TextProcessor.fit_text_limit(text, max_limit=4096)
                for chunk in text_chunks:
                    clean_chunk = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', chunk, flags=re.DOTALL | re.IGNORECASE)
                    if clean_chunk.strip():
                        await self._send_with_retry(bot.send_message, **base_kw, text=clean_chunk, parse_mode="HTML")

            # On successful dispatch, remove temporary media file
            if isinstance(media_file_id, str) and os.path.exists(media_file_id):
                try:
                    os.remove(media_file_id)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)
        except Exception:
            raise

    async def clone_media_group(self, messages: List[TelethonMessage], pair: ChannelPair) -> bool:
        if not self.bot or not messages:
            return False

        if not pair.is_active:
            return False

        # Safeguard against self-cloning loops
        if self._is_self_loop(pair):
            logger.error(f"Infinite loop detected: pair #{pair.id} has identical source and target ({pair.source_channel}). Skipping.")
            return False

        # Verify active subscription / 14-day trial for channel owner
        sub = await db_manager.get_user_subscription(pair.user_id)
        is_admin = pair.user_id in settings.admin_ids or await db_manager.is_admin(pair.user_id)
        if not is_admin and not sub.is_active:
            logger.warning(f"Subscription or 14-day trial expired for user {pair.user_id}. Skipping media group for pair #{pair.id}")
            return False

        uncloned = [m for m in messages if not await db_manager.is_message_cloned(pair.id, m.id)]
        if not uncloned:
            return False

        # Apply rate limiter
        await rate_limiter.wait_for_slot(pair.target_channel)

        target_chat_id = pair.target_id or self._normalize_chat_id(pair.target_channel)
        target_topic_id = getattr(pair, "target_topic_id", None)
        show_caption_above = bool(getattr(pair, "show_caption_above", False))
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Media group target {target_chat_id} is a private user! Aborting clone.")
            return False

        disable_notification = bool(pair.night_mode == "silent" and drip_feed_service.is_night_time())

        raw_caption = ""
        for msg in uncloned:
            msg_html = extract_message_html(msg)
            if msg_html:
                raw_caption = msg_html
                break

        processed_caption = await self.process_post_text(raw_caption, pair)
        if processed_caption is None and raw_caption:
            logger.info(f"Media group blocked by blacklist for pair {pair.id}.")
            return False

        from services.telethon_listener import telethon_listener
        is_telethon_avail = telethon_listener.is_connected()
        caption_tl, overflow_tl = TextProcessor.fit_caption_limit(processed_caption or "", max_limit=2048)
        downloaded_files: List[str] = []

        try:
            detected_mg_price: Optional[float] = None  # initialized at function scope to avoid locals() antipattern
            if True:
                async def _download_and_prep(msg, idx):
                    try:
                        async with cache_manager.media_semaphore:
                            t_path = await media_handler.download_telethon_media(msg)
                        if not t_path or not os.path.exists(t_path):
                            return None
                        if t_path not in downloaded_files:
                            downloaded_files.append(t_path)
                        m_type = media_handler.get_media_type(msg)
                        if m_type == "photo" and pair.image_watermark_type != "none":
                            wm_text = pair.image_watermark_text or pair.custom_signature or pair.target_channel
                            if wm_text:
                                if pair.image_watermark_type == "logo":
                                    wm_photo = await asyncio.to_thread(
                                        watermark_service.apply_logo_watermark,
                                        t_path,
                                        pair.image_watermark_text or "assets/logo.png",
                                        pair.image_watermark_pos or "bottom_right"
                                    )
                                else:
                                    wm_photo = await asyncio.to_thread(
                                        watermark_service.apply_text_watermark,
                                        t_path,
                                        wm_text,
                                        pair.image_watermark_pos or "bottom_right"
                                    )
                                if wm_photo and os.path.exists(wm_photo):
                                    if wm_photo not in downloaded_files:
                                        downloaded_files.append(wm_photo)
                                    t_path = wm_photo
                        elif m_type == "video" and pair.video_watermark_type != "none":
                            if is_admin or sub.tier in ["pro", "vip"]:
                                wm_text = pair.video_watermark_text or pair.image_watermark_text or pair.target_channel
                                if wm_text:
                                    if pair.video_watermark_type == "logo":
                                        wm_video = await video_watermark_service.apply_video_logo_watermark(
                                            input_video_path=t_path,
                                            logo_image_path=pair.video_watermark_text or "assets/logo.png",
                                            pos=pair.video_watermark_pos or "bottom_right"
                                        )
                                    else:
                                        wm_video = await video_watermark_service.apply_video_text_watermark(
                                            input_video_path=t_path,
                                            watermark_text=wm_text,
                                            pos=pair.video_watermark_pos or "bottom_right"
                                        )
                                    if wm_video and os.path.exists(wm_video):
                                        if wm_video not in downloaded_files:
                                            downloaded_files.append(wm_video)
                                        t_path = wm_video
                        return (idx, msg, t_path, m_type)
                    except Exception as prep_err:
                        logger.error(f"Error prepping media item {msg.id}: {prep_err}")
                        return None

                tasks = [_download_and_prep(m, i) for i, m in enumerate(uncloned)]
                raw_results = await asyncio.gather(*tasks, return_exceptions=True)

                valid_items = [r for r in raw_results if isinstance(r, tuple) and r is not None]
                valid_items.sort(key=lambda x: x[0])

                # Check Drip Feed / Night Buffer queueing for media groups
                if (pair.drip_delay_minutes > 0 or (pair.night_mode == "buffer" and drip_feed_service.is_night_time())):
                    queued_media_files = []
                    for idx, msg, t_path, m_type in valid_items:
                        queued_p = os.path.join(os.path.dirname(t_path), f"queued_{os.path.basename(t_path)}")
                        try:
                            os.replace(t_path, queued_p)
                            t_path = queued_p
                        except Exception:
                            logger.debug("Ignored exception", exc_info=True)
                        queued_media_files.append({"path": t_path, "type": m_type})

                    if queued_media_files:
                        payload = {
                            "text": processed_caption or "",
                            "media_type": "media_group",
                            "media_files": queued_media_files,
                            "watermarked": True
                        }
                        await drip_feed_service.enqueue_post(pair, payload)
                        for m in uncloned:
                            await db_manager.record_cloned_message(
                                pair_id=pair.id,
                                source_msg_id=m.id,
                                target_msg_id=None,
                                media_type="media_group"
                            )
                        logger.info(f"Media group ({len(queued_media_files)} items) enqueued to drip feed for pair #{pair.id}")
                        return True

                # Partition valid_items into mutually compatible groups for Telegram API:
                # - Visual (photos & videos) can be in the same media group
                # - Audio can only be grouped with audio
                # - Documents can only be grouped with documents
                visual_items = [x for x in valid_items if x[3] in ("photo", "video")]
                audio_items = [x for x in valid_items if x[3] == "audio"]
                doc_items = [x for x in valid_items if x[3] == "document"]
                other_items = [x for x in valid_items if x[3] not in ("photo", "video", "audio", "document")]

                # Feature 6 & 7: Media Group pHash Deduplication and Price Arbitrage
                photo_paths = [temp_path for (_, _, temp_path, m_type) in valid_items if m_type == "photo" and temp_path and os.path.exists(temp_path)]
                detected_mg_price = None
                try:
                    from services.story_cloner_service import story_cloner_service
                    detected_mg_price = story_cloner_service.extract_price(processed_caption or "")
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

                if photo_paths:
                    try:
                        from services.image_hasher import image_hasher
                        mg_hashes = await image_hasher.get_multiple_phashes_async(photo_paths)

                        first_msg_id = valid_items[0][1].id if valid_items else None
                        is_dup, is_price_drop, match_info = await image_hasher.check_listing_duplicate(
                            mg_hashes,
                            current_price=detected_mg_price,
                            pair_id=pair.id,
                            source_channel=pair.source_channel,
                            source_msg_id=first_msg_id
                        )

                        if is_dup:
                            if is_price_drop and match_info:
                                prev_price = match_info.get("previous_price", 0.0)
                                logger.info(f"🔥 PRICE DROP DETECTED for media group in pair #{pair.id}: ${prev_price:,.0f} -> ${detected_mg_price:,.0f}!")
                                matched_recs = await db_manager.get_cloned_messages_by_source(
                                    source_channel=match_info.get("matched_channel") or pair.source_channel,
                                    source_msg_id=match_info.get("matched_msg_id")
                                )
                                for m_rec in matched_recs:
                                    t_msg_id = m_rec.get("target_msg_id")
                                    t_chan = m_rec.get("target_channel") or m_rec.get("pair_target_channel")
                                    if t_msg_id and t_chan:
                                        old_cap = m_rec.get("last_caption") or ""
                                        drop_badge = f"\n\n🔥 <b>NARX ARZONLASHDI:</b> <s>${prev_price:,.0f}</s> ➡️ <b>${detected_mg_price:,.0f}</b>"
                                        new_cap = self._smart_fit_caption_with_badge(old_cap, drop_badge)
                                        try:
                                            await self.bot.edit_message_caption(chat_id=t_chan, message_id=t_msg_id, caption=new_cap, parse_mode="HTML")
                                            await db_manager.update_cloned_message_price(m_rec["id"], detected_mg_price)
                                        except Exception as pe:
                                            logger.debug(f"Could not update price drop caption: {pe}")
                                for f in [x[2] for x in valid_items if x[2]]:
                                    if f and os.path.exists(f):
                                        try: os.remove(f)
                                        except Exception: logger.debug("Ignored exception", exc_info=True)
                                return True
                            else:
                                logger.info(f"pHash: Skipping duplicate media group for pair #{pair.id} (matched {match_info.get('matched_channel')} #{match_info.get('matched_msg_id')})")
                                for f in [x[2] for x in valid_items if x[2]]:
                                    if f and os.path.exists(f):
                                        try: os.remove(f)
                                        except Exception: logger.debug("Ignored exception", exc_info=True)
                                return True
                        else:
                            await image_hasher.save_listing_hashes(
                                hashes=mg_hashes,
                                source_channel=pair.source_channel,
                                source_msg_id=uncloned[0].id,
                                pair_id=pair.id,
                                price=detected_mg_price
                            )
                    except Exception as he:
                        logger.warning(f"Media group pHash deduplication error: {he}")

                partitions = [p for p in (visual_items, audio_items, doc_items, other_items) if p]
                if not partitions:
                    return False

                sent_message_ids: Dict[int, Optional[int]] = {}
                sent_message_file_ids: Dict[int, Optional[str]] = {}
                caption_attached = False
                bot_caption_attached = False
                overflow_sent = False

                for part in partitions:
                    part_media_items = []
                    for (_orig_idx, msg, temp_path, m_type) in part:
                        downloaded_files.append(temp_path)
                        input_file = FSInputFile(temp_path)
                        item_caption = None
                        if not caption_attached and caption_tl:
                            item_caption = caption_tl
                            caption_attached = True

                        if m_type == "photo":
                            p_kw = {"media": input_file, "caption": item_caption, "parse_mode": "HTML"}
                            if show_caption_above and item_caption:
                                p_kw["show_caption_above_media"] = True
                            part_media_items.append(InputMediaPhoto(**p_kw))
                        elif m_type == "video":
                            v_kw = {"media": input_file, "caption": item_caption, "parse_mode": "HTML"}
                            if show_caption_above and item_caption:
                                v_kw["show_caption_above_media"] = True
                            part_media_items.append(InputMediaVideo(**v_kw))
                        elif m_type == "audio":
                            part_media_items.append(InputMediaAudio(media=input_file, caption=item_caption, parse_mode="HTML"))
                        elif m_type == "document":
                            part_media_items.append(InputMediaDocument(media=input_file, caption=item_caption, parse_mode="HTML"))
                        else:
                            part_media_items.append(InputMediaDocument(media=input_file, caption=item_caption, parse_mode="HTML"))

                    # If auto_premium_emojis is enabled or caption exceeds standard Bot API limit, try sending via Telethon userbot
                    telethon_sent = None
                    if is_telethon_avail and (pair.auto_premium_emojis or (processed_caption and len(processed_caption) > 1024)):
                        part_file_paths = [temp_path for (_, _, temp_path, _) in part]
                        telethon_sent = await self._telethon_send_media_group(
                            target_chat_id=target_chat_id,
                            file_paths=part_file_paths,
                            caption=caption_tl or None,
                            overflow_text=overflow_tl,
                            topic_id=target_topic_id
                        )
                        if telethon_sent:
                            for j, s_msg in enumerate(telethon_sent):
                                if j < len(part):
                                    sent_message_ids[part[j][1].id] = getattr(s_msg, 'id', None)

                    overflow_bot = None
                    if not telethon_sent:
                        # Fallback to Bot API: Strip <tg-emoji> so Bot API entities don't error and caption fits <= 1024
                        bot_clean_caption = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', processed_caption or "", flags=re.DOTALL | re.IGNORECASE)
                        caption_bot, overflow_bot = TextProcessor.fit_caption_limit(bot_clean_caption, max_limit=1024)
                        if not bot_caption_attached:
                            if part_media_items:
                                update_dict = {"caption": caption_bot or None}
                                if show_caption_above and caption_bot and isinstance(part_media_items[0], (InputMediaPhoto, InputMediaVideo)):
                                    update_dict["show_caption_above_media"] = True
                                part_media_items[0] = part_media_items[0].model_copy(update=update_dict)
                            bot_caption_attached = True
                        else:
                            if part_media_items:
                                part_media_items[0] = part_media_items[0].model_copy(update={"caption": None})
                        # Chunk up to 10 items
                        p_total = len(part_media_items)
                        if p_total <= 10:
                            chunks = [part_media_items]
                            chunk_msgs_list = [part]
                        else:
                            num_chunks = (p_total + 9) // 10
                            base_size = p_total // num_chunks
                            rem = p_total % num_chunks
                            chunks = []
                            chunk_msgs_list = []
                            cur_idx = 0
                            for c_i in range(num_chunks):
                                c_sz = base_size + (1 if c_i < rem else 0)
                                chunks.append(part_media_items[cur_idx:cur_idx + c_sz])
                                chunk_msgs_list.append(part[cur_idx:cur_idx + c_sz])
                                cur_idx += c_sz

                        for chunk, c_items in zip(chunks, chunk_msgs_list):
                            chunk_src_msgs = [item[1] for item in c_items]
                            if len(chunk) == 1:
                                item = chunk[0]
                                src_msg = chunk_src_msgs[0]
                                result_msg = None
                                base_item_kw = {
                                    "chat_id": target_chat_id,
                                    "caption": item.caption,
                                    "parse_mode": item.parse_mode,
                                    "disable_notification": disable_notification
                                }
                                if target_topic_id:
                                    base_item_kw["message_thread_id"] = target_topic_id

                                if isinstance(item, InputMediaPhoto):
                                    if show_caption_above and item.caption:
                                        base_item_kw["show_caption_above_media"] = True
                                    result_msg = await self._send_with_retry(
                                        self.bot.send_photo,
                                        photo=item.media,
                                        **base_item_kw
                                    )
                                elif isinstance(item, InputMediaVideo):
                                    if show_caption_above and item.caption:
                                        base_item_kw["show_caption_above_media"] = True
                                    result_msg = await self._send_with_retry(
                                        self.bot.send_video,
                                        video=item.media,
                                        **base_item_kw
                                    )
                                elif isinstance(item, InputMediaAudio):
                                    result_msg = await self._send_with_retry(
                                        self.bot.send_audio,
                                        audio=item.media,
                                        **base_item_kw
                                    )
                                else:
                                    result_msg = await self._send_with_retry(
                                        self.bot.send_document,
                                        document=item.media,
                                        **base_item_kw
                                    )
                                if result_msg:
                                    tid = getattr(result_msg, 'message_id', None) or getattr(result_msg, 'id', None)
                                    sent_message_ids[src_msg.id] = tid
                                    fid = None
                                    if hasattr(result_msg, 'photo') and result_msg.photo:
                                        fid = result_msg.photo[-1].file_id
                                    elif hasattr(result_msg, 'video') and result_msg.video:
                                        fid = result_msg.video.file_id
                                    elif hasattr(result_msg, 'document') and result_msg.document:
                                        fid = result_msg.document.file_id
                                    elif hasattr(result_msg, 'audio') and result_msg.audio:
                                        fid = result_msg.audio.file_id
                                    if fid:
                                        sent_message_file_ids[src_msg.id] = fid
                            else:
                                try:
                                    send_mg_kw = {
                                        "chat_id": target_chat_id,
                                        "media": chunk,
                                        "disable_notification": disable_notification
                                    }
                                    if target_topic_id:
                                        send_mg_kw["message_thread_id"] = target_topic_id
                                    result_msgs = await self._send_with_retry(
                                        self.bot.send_media_group,
                                        **send_mg_kw
                                    )
                                    if result_msgs and isinstance(result_msgs, (list, tuple)):
                                        for j, r_msg in enumerate(result_msgs):
                                            if j < len(chunk_src_msgs):
                                                s_id = chunk_src_msgs[j].id
                                                tid = getattr(r_msg, 'message_id', None) or getattr(r_msg, 'id', None)
                                                sent_message_ids[s_id] = tid
                                                fid = None
                                                if hasattr(r_msg, 'photo') and r_msg.photo:
                                                    fid = r_msg.photo[-1].file_id
                                                elif hasattr(r_msg, 'video') and r_msg.video:
                                                    fid = r_msg.video.file_id
                                                elif hasattr(r_msg, 'document') and r_msg.document:
                                                    fid = r_msg.document.file_id
                                                elif hasattr(r_msg, 'audio') and r_msg.audio:
                                                    fid = r_msg.audio.file_id
                                                if fid:
                                                    sent_message_file_ids[s_id] = fid
                                except TelegramBadRequest as tbr:
                                    logger.warning(f"send_media_group failed ({tbr}), falling back to individual sends.")
                                    for it, s_msg in zip(chunk, chunk_src_msgs):
                                        res = None
                                        fb_kw = {"chat_id": target_chat_id, "caption": it.caption, "parse_mode": it.parse_mode}
                                        if target_topic_id:
                                            fb_kw["message_thread_id"] = target_topic_id
                                        if isinstance(it, InputMediaPhoto):
                                            if show_caption_above and it.caption:
                                                fb_kw["show_caption_above_media"] = True
                                            res = await self._send_with_retry(self.bot.send_photo, photo=it.media, **fb_kw)
                                        elif isinstance(it, InputMediaVideo):
                                            if show_caption_above and it.caption:
                                                fb_kw["show_caption_above_media"] = True
                                            res = await self._send_with_retry(self.bot.send_video, video=it.media, **fb_kw)
                                        elif isinstance(it, InputMediaAudio):
                                            res = await self._send_with_retry(self.bot.send_audio, audio=it.media, **fb_kw)
                                        else:
                                            res = await self._send_with_retry(self.bot.send_document, document=it.media, **fb_kw)
                                        if res:
                                            s_id = s_msg.id
                                            sent_message_ids[s_id] = getattr(res, 'message_id', None) or getattr(res, 'id', None)
                                            fid = None
                                            if hasattr(res, 'photo') and res.photo:
                                                fid = res.photo[-1].file_id
                                            elif hasattr(res, 'video') and res.video:
                                                fid = res.video.file_id
                                            elif hasattr(res, 'document') and res.document:
                                                fid = res.document.file_id
                                            elif hasattr(res, 'audio') and res.audio:
                                                fid = res.audio.file_id
                                            if fid:
                                                sent_message_file_ids[s_id] = fid

                        if overflow_bot and TextProcessor.get_visible_text_length(overflow_bot) > 0:
                            try:
                                clean_overflow = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', overflow_bot, flags=re.DOTALL | re.IGNORECASE)
                                for o_chunk in TextProcessor.fit_text_limit(clean_overflow, max_limit=4096):
                                    over_kw = {"chat_id": target_chat_id, "text": o_chunk, "parse_mode": "HTML"}
                                    if target_topic_id:
                                        over_kw["message_thread_id"] = target_topic_id
                                    await self._send_with_retry(
                                        self.bot.send_message,
                                        **over_kw
                                    )
                            except Exception as e:
                                logger.warning(f"Could not send media group overflow text: {e}")
                            overflow_bot = ""

                group_id_str = str(messages[0].grouped_id or "")
                for msg in uncloned:
                    # BUG #5 fix: include target_msg_id from tracking dict
                    target_msg_id = sent_message_ids.get(msg.id)
                    await db_manager.record_cloned_message(
                        pair_id=pair.id,
                        source_msg_id=msg.id,
                        target_msg_id=target_msg_id,
                        media_group_id=group_id_str,
                        media_type="media_group",
                        source_channel=pair.source_channel,
                        target_channel=pair.target_channel,
                        price=float(detected_mg_price or 0.0),
                        last_caption=processed_caption
                    )
                    if pair.backup_enabled:
                        msg_text = extract_message_html(msg) if msg == uncloned[0] else ""
                        item_type = "photo" if msg.photo else ("video" if (msg.video or msg.video_note) else ("document" if msg.document else "media_group"))
                        await disaster_recovery_service.archive_message(
                            pair_id=pair.id,
                            source_id=pair.source_id,
                            message_id=msg.id,
                            text=msg_text,
                            media_type=item_type,
                            media_file_id=sent_message_file_ids.get(msg.id),
                            media_group_id=group_id_str
                        )

                logger.info(f"Successfully cloned media group ({len(uncloned)} items) to {target_chat_id}")
                return True

        except Exception as e:
            logger.error(f"Error cloning media group to {target_chat_id}: {e}", exc_info=True)
            return False
        finally:
            await media_handler.cleanup_files(downloaded_files)

    @staticmethod
    def _normalize_chat_id(target_chat: str) -> Union[int, str]:
        target_chat = target_chat.strip()
        if target_chat.startswith("-100") or (target_chat.startswith("-") and target_chat[1:].isdigit()):
            return int(target_chat)
        elif target_chat.isdigit():
            if len(target_chat) >= 10:
                return int(f"-100{target_chat}")
            return int(target_chat)
        return target_chat

    async def _telethon_send_fallback(self, target_chat_id: Union[int, str], file_path: str, caption: Optional[str]):
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Blocked Telethon fallback send to private user {target_chat_id}")
            return None
        from services.telethon_listener import telethon_listener
        from telethon.tl.types import User as TelethonUser
        if telethon_listener.client and telethon_listener.client.is_connected():
            try:
                target_entity = await telethon_listener.resolve_entity(str(target_chat_id))
                if target_entity:
                    if isinstance(target_entity, TelethonUser):
                        logger.error(f"SECURITY ALERT: Target entity {target_chat_id} is a private User! Aborting.")
                        return None
                    msg = await telethon_listener.client.send_file(
                        target_entity,
                        file=file_path,
                        caption=caption or "",
                        parse_mode="html"
                    )
                    return msg
            except Exception as e:
                logger.error(f"Telethon large file fallback failed: {e}")
        return None

    async def _telethon_send_post(
        self,
        target_chat_id: Union[int, str],
        media_type: str,
        file_path: Optional[Union[str, bytes]] = None,
        caption: Optional[str] = None,
        supports_streaming: bool = False,
        buttons: Optional[List[Any]] = None,
        topic_id: Optional[int] = None
    ) -> Any:
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Blocked Telethon send to private user {target_chat_id}")
            return None
        from services.telethon_listener import telethon_listener
        from telethon.tl.types import User as TelethonUser
        if not telethon_listener.client or not telethon_listener.client.is_connected():
            return None
        target_key = str(target_chat_id).strip()
        if self._is_telethon_admin_required(target_key):
            return None
        try:
            target_entity = await telethon_listener.resolve_entity(target_key)
            if not target_entity:
                return None
            if isinstance(target_entity, TelethonUser):
                logger.error(f"SECURITY ALERT: Resolved entity for {target_key} is a private User, not a Channel! Aborting.")
                return None
            # Telegram MTProto only permits bots to send inline buttons. Userbot accounts raise BotMethodInvalidError.
            send_buttons = None
            if buttons:
                try:
                    me = await telethon_listener.client.get_me()
                    if me and getattr(me, 'bot', False):
                        send_buttons = buttons
                except Exception:
                    send_buttons = None

            if media_type == "text":
                return await telethon_listener.client.send_message(
                    target_entity,
                    message=caption or "",
                    parse_mode="html",
                    link_preview=False,
                    buttons=send_buttons,
                    reply_to=topic_id
                )
            elif file_path:
                is_voice = (media_type == "voice")
                is_doc = (media_type == "document")
                if isinstance(file_path, bytes):
                    file_to_send = io.BytesIO(file_path)
                    file_to_send.name = "media.jpg"
                elif os.path.exists(file_path):
                    f_size = os.path.getsize(file_path)
                    file_to_send = file_path
                    # Fast MTProto Parallel Upload for large media (> 10MB)
                    if f_size > 10 * 1024 * 1024:
                        try:
                            uploaded = await fast_telethon.upload_file_parallel(
                                client=telethon_listener.client,
                                file_input=file_path,
                                file_name=os.path.basename(file_path)
                            )
                            if uploaded:
                                file_to_send = uploaded
                        except Exception as up_err:
                            logger.debug(f"FastTelethon upload fallback: {up_err}")
                else:
                    return None

                return await telethon_listener.client.send_file(
                    target_entity,
                    file=file_to_send,
                    caption=caption or "",
                    parse_mode="html",
                    supports_streaming=supports_streaming,
                    voice_note=is_voice,
                    force_document=is_doc,
                    buttons=send_buttons,
                    reply_to=topic_id
                )
        except Exception as e:
            err_str = str(e).lower()
            if "admin" in err_str or "chatadmin" in err_str or "privilege" in err_str:
                self._telethon_admin_required_targets[target_key] = time.time()
                logger.info(f"Telethon userbot lacks admin rights in {target_key}. Switched to Bot API directly.")
            else:
                logger.warning(f"Telethon send post failed: {e}, falling back to Bot API")
        return None

    async def _telethon_send_media_group(
        self,
        target_chat_id: Union[int, str],
        file_paths: List[str],
        caption: Optional[str] = None,
        overflow_text: Optional[str] = None,
        topic_id: Optional[int] = None
    ) -> Optional[List[Any]]:
        if is_private_chat_target(target_chat_id):
            logger.error(f"SECURITY ALERT: Blocked Telethon media group send to private user {target_chat_id}")
            return None
        from services.telethon_listener import telethon_listener
        from telethon.tl.types import User as TelethonUser
        if not telethon_listener.client or not telethon_listener.client.is_connected() or not file_paths:
            return None
        target_key = str(target_chat_id).strip()
        if self._is_telethon_admin_required(target_key):
            return None
        try:
            target_entity = await telethon_listener.resolve_entity(target_key)
            if not target_entity:
                return None
            if isinstance(target_entity, TelethonUser):
                logger.error(f"SECURITY ALERT: Resolved entity for {target_key} is a private User, not a Channel! Aborting.")
                return None
            sent_all = []
            if len(file_paths) <= 10:
                sent = await telethon_listener.client.send_file(
                    target_entity,
                    file=file_paths,
                    caption=caption or "",
                    parse_mode="html",
                    reply_to=topic_id
                )
                sent_all.extend(sent if isinstance(sent, (list, tuple)) else [sent])
            else:
                for chunk_idx in range(0, len(file_paths), 10):
                    f_chunk = file_paths[chunk_idx:chunk_idx + 10]
                    c_cap = (caption or "") if chunk_idx == 0 else ""
                    sent = await telethon_listener.client.send_file(
                        target_entity,
                        file=f_chunk,
                        caption=c_cap,
                        parse_mode="html",
                        reply_to=topic_id
                    )
                    sent_all.extend(sent if isinstance(sent, (list, tuple)) else [sent])

            if overflow_text and TextProcessor.get_visible_text_length(overflow_text) > 0:
                try:
                    for o_chunk in TextProcessor.fit_text_limit(overflow_text, max_limit=4096):
                        await telethon_listener.client.send_message(
                            target_entity,
                            message=o_chunk,
                            parse_mode="html",
                            link_preview=False,
                            reply_to=topic_id
                        )
                except Exception as oe:
                    logger.warning(f"Telethon overflow send failed: {oe}")
            return sent_all if sent_all else None
        except Exception as e:
            err_str = str(e).lower()
            if "admin" in err_str or "chatadmin" in err_str or "privilege" in err_str:
                self._telethon_admin_required_targets[target_key] = time.time()
                logger.info(f"Telethon userbot lacks admin rights in {target_key}. Switched to Bot API directly.")
            else:
                logger.warning(f"Telethon media group send failed: {e}, falling back to Bot API")
        return None

    async def _send_with_retry(self, send_func, *args, max_retries: int = 3, **kwargs):
        for attempt in range(1, max_retries + 1):
            try:
                return await send_func(*args, **kwargs)
            except TelegramRetryAfter as e:
                wait_time = e.retry_after
                if wait_time > 300:
                    logger.error(f"Telegram FloodWait too long ({wait_time}s > 300s). Aborting attempt to prevent worker freeze.")
                    raise
                logger.warning(f"Telegram FloodWait: waiting {wait_time}s (attempt {attempt}/{max_retries})")
                await asyncio.sleep(wait_time + 1)
                if attempt == max_retries:
                    try:
                        return await send_func(*args, **kwargs)
                    except Exception as last_flood_err:
                        logger.error(f"Final retry after FloodWait failed: {last_flood_err}")
                        raise
            except TelegramBadRequest as e:
                err_str = str(e).lower()
                if ("parse" in err_str or "tag" in err_str or "unsupported" in err_str or "emoji" in err_str or "character" in err_str):
                    # Tier 1: Try stripping ONLY <tg-emoji> tags to keep <b>, <i>, <a>, <code> intact
                    logger.warning(f"Telegram entity/tag error ({e}), retrying with custom emojis converted to unicode...")
                    def strip_tg_emoji(s):
                        if not isinstance(s, str):
                            return s
                        cleaned = re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', s, flags=re.DOTALL | re.IGNORECASE)
                        return re.sub(r'</?tg-emoji\b[^>]*>', '', cleaned, flags=re.IGNORECASE)
                    t1_kwargs = dict(kwargs)
                    if 'text' in t1_kwargs:
                        t1_kwargs['text'] = strip_tg_emoji(t1_kwargs['text'])
                    if 'caption' in t1_kwargs:
                        t1_kwargs['caption'] = strip_tg_emoji(t1_kwargs['caption'])
                    if 'media' in t1_kwargs and isinstance(t1_kwargs['media'], (list, tuple)):
                        new_media = []
                        for m_item in t1_kwargs['media']:
                            if hasattr(m_item, 'caption') and isinstance(m_item.caption, str):
                                if hasattr(m_item, 'model_copy'):
                                    new_media.append(m_item.model_copy(update={'caption': strip_tg_emoji(m_item.caption)}))
                                else:
                                    m_item.caption = strip_tg_emoji(m_item.caption)
                                    new_media.append(m_item)
                            else:
                                new_media.append(m_item)
                        t1_kwargs['media'] = new_media
                    if 'reply_markup' in t1_kwargs and t1_kwargs['reply_markup']:
                        rm = t1_kwargs['reply_markup']
                        if hasattr(rm, 'inline_keyboard'):
                            new_rows = []
                            for row in rm.inline_keyboard:
                                new_row = []
                                for btn in row:
                                    if hasattr(btn, 'model_copy'):
                                        new_row.append(btn.model_copy(update={'icon_custom_emoji_id': None}))
                                    else:
                                        if hasattr(btn, 'icon_custom_emoji_id'):
                                            btn.icon_custom_emoji_id = None
                                        new_row.append(btn)
                                new_rows.append(new_row)
                            if hasattr(rm, 'model_copy'):
                                t1_kwargs['reply_markup'] = rm.model_copy(update={'inline_keyboard': new_rows})
                    try:
                        return await send_func(*args, **t1_kwargs)
                    except TelegramBadRequest as t1_err:
                        # Tier 1.5: Fix naked ampersands before destroying all formatting
                        def fix_ampersands(s):
                            if not isinstance(s, str):
                                return s
                            return re.sub(r'&(?!(?:[a-zA-Z0-9]+|#[0-9]+|#x[0-9a-fA-F]+);)', '&amp;', s)
                        t15_kwargs = dict(t1_kwargs)
                        if 'text' in t15_kwargs:
                            t15_kwargs['text'] = fix_ampersands(t15_kwargs['text'])
                        if 'caption' in t15_kwargs:
                            t15_kwargs['caption'] = fix_ampersands(t15_kwargs['caption'])
                        if 'media' in t15_kwargs and isinstance(t15_kwargs['media'], (list, tuple)):
                            new_m15 = []
                            for m_item in t15_kwargs['media']:
                                if hasattr(m_item, 'caption') and isinstance(m_item.caption, str):
                                    if hasattr(m_item, 'model_copy'):
                                        new_m15.append(m_item.model_copy(update={'caption': fix_ampersands(m_item.caption)}))
                                    else:
                                        m_item.caption = fix_ampersands(m_item.caption)
                                        new_m15.append(m_item)
                                else:
                                    new_m15.append(m_item)
                            t15_kwargs['media'] = new_m15
                        try:
                            return await send_func(*args, **t15_kwargs)
                        except TelegramBadRequest:
                            pass
                        # Tier 2: General HTML syntax error, fallback to stripping all HTML tags
                        logger.warning(f"HTML entity retry failed ({t1_err}), retrying without any HTML formatting...")
                        clean_kwargs = dict(kwargs)
                        clean_kwargs['parse_mode'] = None
                        if 'text' in clean_kwargs and isinstance(clean_kwargs['text'], str):
                            clean_kwargs['text'] = html.unescape(re.sub(r'<[^>]+>', '', clean_kwargs['text']))
                        if 'caption' in clean_kwargs and isinstance(clean_kwargs['caption'], str):
                            clean_kwargs['caption'] = html.unescape(re.sub(r'<[^>]+>', '', clean_kwargs['caption']))
                        if 'media' in clean_kwargs and isinstance(clean_kwargs['media'], (list, tuple)):
                            new_clean_media = []
                            for m_item in clean_kwargs['media']:
                                updates = {}
                                if hasattr(m_item, 'parse_mode'):
                                    updates['parse_mode'] = None
                                if hasattr(m_item, 'caption') and isinstance(m_item.caption, str):
                                    updates['caption'] = html.unescape(re.sub(r'<[^>]+>', '', m_item.caption))
                                if updates and hasattr(m_item, 'model_copy'):
                                    new_clean_media.append(m_item.model_copy(update=updates))
                                else:
                                    if hasattr(m_item, 'parse_mode'):
                                        m_item.parse_mode = None
                                    if hasattr(m_item, 'caption') and isinstance(m_item.caption, str):
                                        m_item.caption = html.unescape(re.sub(r'<[^>]+>', '', m_item.caption))
                                    new_clean_media.append(m_item)
                            clean_kwargs['media'] = new_clean_media
                        try:
                            return await send_func(*args, **clean_kwargs)
                        except Exception as retry_err:
                            logger.error(f"Fallback plain text send also failed: {retry_err}")
                logger.error(f"Telegram BadRequest: {e}")
                raise
            except TelegramAPIError as e:
                logger.error(f"Telegram API Error (attempt {attempt}): {e}")
                if attempt == max_retries:
                    raise
                await asyncio.sleep(2 * attempt)
            except Exception as e:
                logger.error(f"Unexpected error sending message: {e}")
                if attempt == max_retries:
                    raise
                await asyncio.sleep(1)
        return None

cloner_engine = ClonerEngine()
