"""Regression tests for the content-pipeline fixes (engine, text cleaning, media, drip feed, rate limiting).

Everything Telegram-related is mocked; files live in pytest's tmp_path."""
import asyncio
import io
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon.tl import types as tl

from database.db_manager import db_manager
from database.models import ChannelPair, Subscription
from services.cloner_engine import ClonerEngine, entities_to_html, extract_message_html
from services.text_processor import TextProcessor

FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
OWNER = 7_000_001


def _pair(**kw):
    base = dict(id=501, user_id=OWNER, source_channel="@src_fix", target_channel="@tgt_fix", target_id=-1001111111111,
                clean_links=True, ad_action="off", backup_enabled=False, enable_invisible_watermark=False)
    base.update(kw)
    return ChannelPair(**base)


def _sub(tier="free", trial=True):
    if tier == "free":
        return Subscription(user_id=OWNER, tier="free", trial_expires_at=FUTURE if trial else None)
    return Subscription(user_id=OWNER, tier=tier, expires_at=FUTURE)


def _privileges(tier="free", admin=False):
    return (
        patch.object(db_manager, "get_user_subscription", new=AsyncMock(return_value=_sub(tier))),
        patch.object(db_manager, "is_admin", new=AsyncMock(return_value=admin)),
    )


def _jpeg(path, size=(64, 64)):
    Image.new("RGB", size, (120, 30, 200)).save(path, format="JPEG")
    return str(path)


def _bot():
    bot = MagicMock()
    counter = {"n": 100}

    def _msg(*_a, **_k):
        counter["n"] += 1
        return SimpleNamespace(message_id=counter["n"], chat=SimpleNamespace(id=-1001111111111))

    for name in ("send_message", "send_photo", "send_video", "send_document", "send_audio", "send_voice",
                 "send_video_note", "send_animation", "send_sticker", "send_poll", "edit_message_caption"):
        setattr(bot, name, AsyncMock(side_effect=_msg))
    bot.send_media_group = AsyncMock(side_effect=lambda *a, **k: [_msg() for _ in k["media"]])
    return bot


# ---------------------------------------------------------------- text cleaning (E-C1, E-C3, E-L16, E-L6, E-H2, E-L12)

def test_link_cleaner_keeps_real_and_affiliate_anchors_and_unwraps_only_empty_ones():
    text = ('<a href="https://site.uz/a">Batafsil</a> <a href="___AFF_PROT_0___">Sotib olish</a> '
            '<a href="">bo\'sh</a> <a href="https://t.me/rival">Kanal</a>')
    out = TextProcessor.clean_links_and_usernames(text)
    assert '<a href="https://site.uz/a">Batafsil</a>' in out
    assert '<a href="___AFF_PROT_0___">Sotib olish</a>' in out
    assert "<a href=\"\">" not in out and "bo'sh" in out
    assert "t.me" not in out and "Kanal" in out


def test_link_cleaner_is_linear_on_blank_line_runs():
    started = time.monotonic()
    TextProcessor.clean_links_and_usernames("\n" * 20000 + "Kanalimiz: @x" + " " * 20000 + "\n" * 5000)
    assert time.monotonic() - started < 1.0


def test_link_patterns_have_boundaries_and_remove_query_residue():
    out = TextProcessor.clean_links_and_usernames("about.me/john https://t.me/x?start=ref. durov.t.me tg://resolve?domain=a&start=b")
    assert "about.me/john" in out
    assert "start=ref" not in out and "durov" not in out and "tg://" not in out
    assert out.startswith("about.me/john .")


def test_promo_line_removal_keeps_tags_balanced():
    assert TextProcessor.clean_links_and_usernames("<b>Title\nKanalimiz: @promo</b>\nBody") == "<b>Title</b>\nBody"


def test_source_signature_requires_separator():
    assert TextProcessor.strip_source_signature("Matn\nKanalizatsiya ta'mirlandi").endswith("ta'mirlandi")
    assert TextProcessor.strip_source_signature("Matn\nAdmin panel yangilandi").endswith("yangilandi")
    assert TextProcessor.strip_source_signature("Matn\nManba: @kun_uz") == "Matn"
    assert TextProcessor.strip_source_signature("<b>Matn\n@source_kanal</b>") == "<b>Matn</b>"


def test_blacklist_matches_visible_text():
    assert TextProcessor.contains_blacklisted_words("<b>casino</b> bonus", ["casino"])
    assert TextProcessor.contains_blacklisted_words("🎰casino", ["casino"])
    assert TextProcessor.contains_blacklisted_words("o&#x27;yin vaqti", ["o'yin"])
    assert not TextProcessor.contains_blacklisted_words("e'lonchi", ["e'lon"])


def test_ad_detection_needs_multiple_signals():
    assert not TextProcessor.is_commercial_ad("Aloqa uchun: @realtor. Depozit: 1 oylik. Bonus: konditsioner")
    assert not TextProcessor.is_commercial_ad("Promokod: SALE20 bilan chegirma https://shop.uz")
    assert not TextProcessor.is_commercial_ad("Aviator filmi premyerasi")
    assert TextProcessor.is_commercial_ad("1xbet orqali stavka qiling")
    assert TextProcessor.is_commercial_ad("Спонсорский пост: сервис")  # NFKC keeps 'й'


def test_attach_signature_never_truncates():
    body = "<b>" + "A" * 5000 + "</b>"
    out = TextProcessor.attach_signature(body, "<i>@sig</i>")
    assert out.startswith(body) and out.endswith("<i>@sig</i>")


def test_word_replacement_never_touches_urls_and_matches_unescaped_text():
    out = TextProcessor.apply_word_replacements("item https://site.com/item AT&amp;T @item", {"item": "product", "AT&T": "A<T"})
    assert "https://site.com/item" in out and "@item" in out
    assert out.startswith("product") and "A&lt;T" in out


# ---------------------------------------------------------------- HTML extraction (E-M1)

def test_entities_to_html_keeps_pre_spoiler_and_escapes():
    text = "a<b & code\nx = {1}\nsecret"
    entities = [
        tl.MessageEntityBold(offset=0, length=3),
        tl.MessageEntityPre(offset=11, length=8, language="python"),
        tl.MessageEntitySpoiler(offset=19, length=6),
    ]
    out = entities_to_html(text, entities)
    assert out.startswith("<b>a&lt;b</b> &amp; code")
    assert '<pre><code class="language-python">x = {1}\n</code></pre>' in out
    assert "<tg-spoiler>secret</tg-spoiler>" in out
    assert "{}" not in out.replace("{1}", "")


def test_entities_to_html_handles_emoji_offsets_and_overlaps():
    out = entities_to_html("🔥 hot deal", [tl.MessageEntityBold(offset=0, length=6), tl.MessageEntityItalic(offset=3, length=8)])
    assert out == "<b>🔥 <i>hot</i></b><i> deal</i>"


def test_extract_message_html_escapes_plain_messages():
    msg = SimpleNamespace(message="Narx < 5000 & ok", entities=None)
    assert extract_message_html(msg) == "Narx &lt; 5000 &amp; ok"


# ---------------------------------------------------------------- process_post_text gates (E-M2, E-H3, B-L6, VIP emojis)

async def test_link_cleaning_follows_clean_links_toggle_only():
    engine = ClonerEngine()
    text = "Yangilik https://t.me/rival_channel"
    assert "t.me" in await engine.process_post_text(text, _pair(clean_links=False))
    assert "t.me" not in await engine.process_post_text(text, _pair(clean_links=True))


async def test_ad_caption_on_media_post_only_drops_caption():
    engine = ClonerEngine()
    ad = "1xbet orqali stavka qiling! Promokod: TOP https://1xbet.uz"
    assert await engine.process_post_text(ad, _pair(), has_media=False) is None
    assert await engine.process_post_text(ad, _pair(), has_media=True) == ""


async def test_affiliate_rules_and_ai_rewrite_need_paid_plan():
    engine = ClonerEngine()
    pair = _pair(affiliate_rules="shop.uz=https://aff.example/x", ai_paraphrase_mode="formal")
    p1, p2 = _privileges("free")
    with p1, p2:
        trial_out = await engine.process_post_text("Mahsulot https://shop.uz/item", pair)
    p1, p2 = _privileges("pro")
    with p1, p2:
        pro_out = await engine.process_post_text("Mahsulot https://shop.uz/item", pair)
    assert "aff.example" not in trial_out and "Rasmiy Axborot" not in trial_out
    assert "https://aff.example/x" in pro_out and "Rasmiy Axborot" in pro_out


async def test_premium_emojis_are_vip_only():
    engine = ClonerEngine()
    pair = _pair(auto_premium_emojis=True)
    p1, p2 = _privileges("pro")
    with p1, p2:
        assert "<tg-emoji" not in await engine.process_post_text("Zo'r 🔥", pair)
    p1, p2 = _privileges("vip")
    with p1, p2:
        assert "<tg-emoji" in await engine.process_post_text("Zo'r 🔥", pair)


async def test_image_watermark_and_cta_need_paid_plan(tmp_path):
    engine = ClonerEngine()
    pair = _pair(image_watermark_type="text", image_watermark_text="@brand", auto_cta_buttons=True)
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (10, 10, 10)).save(buf, format="JPEG")
    data = buf.getvalue()
    assert await engine._watermark_photo_bytes(data, pair, paid=False) == data
    assert await engine._watermark_photo_bytes(data, pair, paid=True) != data
    msg = SimpleNamespace(reply_markup=None)
    assert engine._build_markup(msg, pair, "https://shop.uz/x", paid=False) == (None, None)
    markup, _ = engine._build_markup(msg, pair, "https://shop.uz/x", paid=True)
    assert markup is not None and markup.inline_keyboard[0][0].icon_custom_emoji_id is None


# ---------------------------------------------------------------- watermark helpers (E-M3, E-M16, E-L14)

def test_logo_watermark_failure_returns_original(tmp_path):
    from services.watermark_service import WatermarkService
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (10, 10)).save(logo)
    assert WatermarkService.apply_logo_watermark(str(bad), str(logo)) == str(bad)


def test_watermark_text_fallback_never_uses_signature_or_raw_id():
    engine = ClonerEngine()
    pair = _pair(custom_signature='<tg-emoji emoji-id="1">🔥</tg-emoji> Sig', target_title="", target_channel="-1001234567890")
    assert engine._image_watermark_text(pair) is None
    assert engine._image_watermark_text(_pair(target_title="", target_channel="@brand")) == "@brand"
    assert engine._image_watermark_text(_pair(image_watermark_text="<b>X</b>")) == "X"


def test_logo_path_is_confined_to_assets(tmp_path):
    secret = tmp_path / "secret.png"
    Image.new("RGBA", (10, 10)).save(secret)
    resolved = ClonerEngine._resolve_logo_path(str(secret))
    assert resolved is None or os.path.basename(resolved) == "logo.png"
    assert ClonerEngine._resolve_logo_path("../../.env") in (None, ClonerEngine._resolve_logo_path(None))


# ---------------------------------------------------------------- Telethon sends (E-M5, E-M6, E-L1, lead notes)

def _telethon_listener_mock(is_bot=False):
    tlm = MagicMock()
    tlm.is_connected.return_value = True
    tlm.client.is_connected.return_value = True
    tlm.client.get_me = AsyncMock(return_value=SimpleNamespace(bot=is_bot))
    tlm.client.send_file = AsyncMock(return_value=SimpleNamespace(id=77, chat_id=-1001111111111))
    tlm.client.send_message = AsyncMock(return_value=SimpleNamespace(id=78, chat_id=-1001111111111))
    tlm.resolve_entity = AsyncMock(return_value=SimpleNamespace(id=1111111111))
    return tlm


async def test_telethon_media_send_keeps_source_attributes_topic_and_silence(tmp_path):
    engine = ClonerEngine()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 1000)
    attrs = [tl.DocumentAttributeVideo(duration=12, w=1280, h=720, supports_streaming=True), tl.DocumentAttributeFilename("clip.mp4")]
    source = SimpleNamespace(media=SimpleNamespace(document=tl.Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="video/mp4", size=1000, dc_id=2, attributes=attrs)))
    tlm = _telethon_listener_mock()
    with patch("services.telethon_listener.telethon_listener", tlm):
        sent = await engine._telethon_send_post(-1001111111111, "video", file_path=str(video), caption="x",
                                                supports_streaming=True, topic_id=9, silent=True, source_message=source)
    assert sent.id == 77
    kwargs = tlm.client.send_file.call_args.kwargs
    assert kwargs["attributes"] == attrs and kwargs["mime_type"] == "video/mp4"
    assert kwargs["reply_to"] == 9 and kwargs["silent"] is True
    assert tlm.resolve_entity.call_args.kwargs.get("join_invite") is False


async def test_telethon_user_account_defers_buttons_to_bot_api():
    engine = ClonerEngine()
    tlm = _telethon_listener_mock(is_bot=False)
    with patch("services.telethon_listener.telethon_listener", tlm):
        assert await engine._telethon_send_post(-1001111111111, "text", caption="x", buttons=[["b"]]) is None
        assert await engine._telethon_send_post(-1001111111111, "text", caption="x", topic_id=5) is not None
    tlm.client.send_message.assert_awaited_once()
    assert tlm.client.send_message.call_args.kwargs["reply_to"] == 5


# ---------------------------------------------------------------- recording & delivery (E-M4, E-M8, E-M9, E-M15, E-L2, E-L9)

async def test_text_post_records_first_chunk_silently_and_remembers_targets():
    engine = ClonerEngine(bot=_bot())
    pair = _pair(night_mode="silent", clean_links=False)
    message = SimpleNamespace(id=10, message=("Uzun matn. " * 500).strip(), entities=None, media=None, reply_markup=None)
    record = AsyncMock()
    p1, p2 = _privileges("pro")
    with p1, p2, patch.object(db_manager, "is_message_cloned", new=AsyncMock(return_value=False)), \
         patch.object(db_manager, "record_cloned_message", new=record), \
         patch("services.cloner_engine.rate_limiter.wait_for_slot", new=AsyncMock()), \
         patch("services.cloner_engine.drip_feed_service.is_night_time", return_value=True):
        assert await engine.clone_single_message(message, pair) is True
    calls = engine.bot.send_message.call_args_list
    assert len(calls) == 2 and all(c.kwargs.get("disable_notification") is True for c in calls)
    assert record.call_args.kwargs["target_msg_id"] == 101
    assert (1111111111, 101) in engine.recent_sent_targets and (1111111111, 102) in engine.recent_sent_targets


async def test_video_note_records_the_note_not_the_caption_message(tmp_path):
    engine = ClonerEngine(bot=_bot())
    note = tmp_path / "n.mp4"
    note.write_bytes(b"0" * 100)
    record = AsyncMock()
    msg = SimpleNamespace(id=11, media=None)
    ctx = engine._context(_pair(), False)
    with patch("services.cloner_engine.media_handler.download_telethon_media", new=AsyncMock(return_value=str(note))), \
         patch("services.cloner_engine.rate_limiter.wait_for_slot", new=AsyncMock()), \
         patch.object(db_manager, "record_cloned_message", new=record):
        ok = await engine._clone_media_post(msg, ctx, "video_note", "Izoh", None, None, False, False, [])
    assert ok is True
    assert record.await_count == 1 and record.call_args.kwargs["target_msg_id"] == 101
    assert record.call_args.kwargs["last_caption"] is None
    engine.bot.send_message.assert_awaited_once()


def test_backup_file_id_ignores_telethon_messages():
    telethon_like = SimpleNamespace(photo=SimpleNamespace(id=1), id=5)
    assert ClonerEngine._bot_file_id(telethon_like) is None


async def test_album_records_only_delivered_items_and_skips_oversized_without_telethon(tmp_path):
    engine = ClonerEngine(bot=_bot())
    paths = {m: _jpeg(tmp_path / f"{m}.jpg") for m in (1, 2, 3)}
    msgs = [SimpleNamespace(id=m, grouped_id=77, message="Albom" if m == 1 else "", entities=None, media=None, file=None) for m in (1, 2, 3, 4)]
    record = AsyncMock()

    async def _download(msg):
        return paths.get(msg.id)  # item 4 fails to download

    real_size = ClonerEngine._file_size
    p1, p2 = _privileges("pro")
    with p1, p2, patch.object(db_manager, "is_message_cloned", new=AsyncMock(return_value=False)), \
         patch.object(db_manager, "record_cloned_message", new=record), \
         patch("services.cloner_engine.rate_limiter.wait_for_slot", new=AsyncMock()), \
         patch("services.cloner_engine.media_handler.download_telethon_media", new=AsyncMock(side_effect=_download)), \
         patch("services.cloner_engine.media_handler.get_media_type", return_value="photo"), \
         patch.object(ClonerEngine, "_file_size", staticmethod(lambda p: 60 * 1024 * 1024 if p == paths[3] else real_size(p))), \
         patch("services.image_hasher.image_hasher.check_listing_duplicate", new=AsyncMock(return_value=(False, False, None))), \
         patch("services.image_hasher.image_hasher.save_listing_hashes", new=AsyncMock()):
        assert await engine.clone_media_group(msgs, _pair(clean_links=False)) is True
    recorded = sorted(c.kwargs["source_msg_id"] for c in record.call_args_list)
    assert recorded == [1, 2]
    media = engine.bot.send_media_group.call_args.kwargs["media"]
    assert len(media) == 2 and media[0].caption == "Albom" and media[1].caption is None


# ---------------------------------------------------------------- polls (E-L17, E-L11)

async def test_quiz_poll_is_anonymous_with_correct_answer_and_utf16_limits():
    engine = ClonerEngine(bot=_bot())
    answers = [SimpleNamespace(text=SimpleNamespace(text=t), option=bytes([i])) for i, t in enumerate(["A", "B", "🔥" * 60])]
    media = SimpleNamespace(
        poll=SimpleNamespace(question=SimpleNamespace(text="Q&A <?>"), answers=answers, quiz=True, multiple_choice=False, public_voters=True),
        results=SimpleNamespace(results=[SimpleNamespace(option=bytes([1]), correct=True)], solution="Chunki"))
    await engine._send_poll(SimpleNamespace(id=1, media=media), engine._context(_pair(), False))
    kw = engine.bot.send_poll.call_args.kwargs
    assert kw["is_anonymous"] is True and kw["type"] == "quiz" and kw["correct_option_ids"] == [1]
    assert kw["question_parse_mode"] is None and kw["question"] == "Q&A <?>"
    assert TextProcessor.utf16_len(kw["options"][2].text) <= 100


async def test_quiz_without_known_answer_falls_back_to_regular_poll():
    engine = ClonerEngine(bot=_bot())
    answers = [SimpleNamespace(text=SimpleNamespace(text=t), option=bytes([i])) for i, t in enumerate(["A", "B"])]
    media = SimpleNamespace(poll=SimpleNamespace(question="Q", answers=answers, quiz=True, multiple_choice=False), results=None)
    await engine._send_poll(SimpleNamespace(id=1, media=media), engine._context(_pair(), False))
    assert "type" not in engine.bot.send_poll.call_args.kwargs


def test_alert_text_is_limited_in_utf16_units():
    from services.custom_emojis import clean_for_alert
    assert len(clean_for_alert("🔥" * 150).encode("utf-16-le")) // 2 <= 200


# ---------------------------------------------------------------- price drop (E-M13, E-M14)

def test_badge_fitting_is_tag_safe_and_utf16_aware():
    old = "<b>" + "🔥" * 700 + "</b>"
    out = ClonerEngine._smart_fit_caption_with_badge(old, "\n\n🔥 <b>NARX</b>")
    assert TextProcessor.get_visible_text_length(out) <= 1024
    assert out.count("<b>") == out.count("</b>")


async def test_price_drop_edits_only_same_owner_and_destination_by_numeric_id():
    engine = ClonerEngine(bot=_bot())
    pair = _pair()
    rows = [
        {"id": 1, "pair_user_id": OWNER, "pair_is_active": 1, "target_msg_id": 50, "status": "active", "last_caption": "Uy 1000$",
         "pair_target_channel": "@tgt_fix", "pair_target_id": 1111111111, "target_channel": "https://t.me/+invite"},
        {"id": 2, "pair_user_id": 999, "pair_is_active": 1, "target_msg_id": 60, "status": "active", "last_caption": "x",
         "pair_target_channel": "@other", "pair_target_id": 2222222222},
        {"id": 3, "pair_user_id": OWNER, "pair_is_active": 1, "target_msg_id": 70, "status": "active", "last_caption": "x",
         "pair_target_channel": "@else", "pair_target_id": 3333333333},
    ]
    with patch.object(db_manager, "get_pair_by_id", new=AsyncMock(return_value=None)), \
         patch.object(db_manager, "get_cloned_messages_for_source", new=AsyncMock(return_value=rows)) as lookup, \
         patch.object(db_manager, "update_cloned_message_price", new=AsyncMock()), \
         patch.object(db_manager, "update_cloned_message_caption", new=AsyncMock()):
        await engine._apply_price_drop(pair, {"matched_msg_id": 9, "matched_channel": "src_fix", "previous_price": 1000.0}, 800.0, None)
    assert lookup.await_args.args[0] == 9
    engine.bot.edit_message_caption.assert_awaited_once()
    kw = engine.bot.edit_message_caption.call_args.kwargs
    assert kw["chat_id"] == -1001111111111 and kw["message_id"] == 50 and "NARX ARZONLASHDI" in kw["caption"]


async def test_listing_dedup_is_tenant_and_destination_scoped_and_tracks_latest_price(tmp_path):
    from database.db_manager import DatabaseManager
    from services.image_hasher import ImageHasherService
    db = DatabaseManager(str(tmp_path / "hash.db"))
    await db.init_db()
    try:
        await db.execute("INSERT INTO users (user_id, full_name) VALUES (1, 'A'), (2, 'B')")
        await db.execute("""INSERT INTO channel_pairs (id, user_id, source_channel, target_channel, target_id, is_active)
                            VALUES (1, 1, '@s1', '@t1', -100111, 1), (2, 1, '@s2', '@t1', -100111, 1),
                                   (3, 2, '@s3', '@t1', -100111, 1), (4, 1, '@s4', '@t2', -100222, 1)""")
        hasher = ImageHasherService()
        hashes = ["0123456789abcdef", "fedcba9876543210", "00ff00ff00ff00ff"]
        with patch("database.db_manager.db_manager", db):
            await hasher.save_listing_hashes(hashes, "@s1", 10, pair_id=1, price=1000.0)
            dup, drop, info = await hasher.check_listing_duplicate(hashes, 800.0, pair_id=2, source_channel="@s2", source_msg_id=5)
            assert dup and drop and info["previous_price"] == 1000.0
            await hasher.update_listing_price(info, 800.0)
            dup, drop, info = await hasher.check_listing_duplicate(hashes, 950.0, pair_id=2, source_channel="@s2", source_msg_id=6)
            assert dup and not drop and info["previous_price"] == 800.0
            assert (await hasher.check_listing_duplicate(hashes, 500.0, pair_id=3, source_channel="@s3", source_msg_id=7))[0] is False
            assert (await hasher.check_listing_duplicate(hashes, 500.0, pair_id=4, source_channel="@s4", source_msg_id=8))[0] is False
            # One shared photo out of three is not a duplicate album
            assert (await hasher.check_listing_duplicate([hashes[0], "1111111111111111", "2222222222222222"], None,
                                                         pair_id=2, source_channel="@s2", source_msg_id=9))[0] is False
    finally:
        await db.close()


# ---------------------------------------------------------------- drip feed (E-M12, LS-M6, LS-L7)

async def test_queued_single_dispatch_is_resumable_restores_buttons_and_completes_record(tmp_path):
    engine = ClonerEngine()
    bot = _bot()
    photo = _jpeg(tmp_path / "queued_p.jpg")
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ko'rish", url="https://shop.uz")]])
    payload = {"version": 2, "pair_id": 501, "source_msg_ids": [42], "text": "Narx 100$", "media_type": "photo",
               "media_path": photo, "reply_markup": engine._markup_to_payload(markup), "progress": {}}
    delivered = AsyncMock()
    with patch("services.cloner_engine.rate_limiter.wait_for_slot", new=AsyncMock()), \
         patch.object(db_manager, "mark_cloned_message_delivered", new=delivered):
        payload["progress"]["media_done"] = False
        await engine.dispatch_queued_payload(bot, _pair(), payload)
        assert bot.send_photo.call_args.kwargs["reply_markup"].inline_keyboard[0][0].url == "https://shop.uz"
        assert delivered.await_args.args[:3] == (501, 42, 101)
        await engine.dispatch_queued_payload(bot, _pair(), payload)
    bot.send_photo.assert_awaited_once()


async def test_queued_missing_file_sends_text_or_fails_permanently():
    engine = ClonerEngine()
    bot = _bot()
    with patch("services.cloner_engine.rate_limiter.wait_for_slot", new=AsyncMock()):
        await engine.dispatch_queued_payload(bot, _pair(), {"media_type": "photo", "media_path": "temp_media/queued_gone.jpg", "text": "Matn"})
        bot.send_photo.assert_not_awaited()
        bot.send_message.assert_awaited_once()
        with pytest.raises(FileNotFoundError):
            await engine.dispatch_queued_payload(bot, _pair(), {"media_type": "photo", "media_path": "temp_media/queued_gone.jpg", "text": ""})


async def test_queued_album_uses_matching_methods(tmp_path):
    engine = ClonerEngine()
    bot = _bot()
    single = {"media_type": "media_group", "text": "Cap", "media_files": [{"path": _jpeg(tmp_path / "a.jpg"), "type": "photo", "source_msg_id": 1}]}
    docs = []
    for name in ("a.pdf", "b.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4")
        docs.append({"path": str(tmp_path / name), "type": "document", "source_msg_id": len(docs) + 2})
    with patch("services.cloner_engine.rate_limiter.wait_for_slot", new=AsyncMock()):
        await engine.dispatch_queued_payload(bot, _pair(), single)
        await engine.dispatch_queued_payload(bot, _pair(), {"media_type": "media_group", "text": "", "media_files": docs})
    bot.send_photo.assert_awaited_once()
    media = bot.send_media_group.call_args.kwargs["media"]
    assert [type(m).__name__ for m in media] == ["InputMediaDocument", "InputMediaDocument"]


async def test_drip_schedule_is_capped_and_kept_out_of_the_night():
    from services.drip_feed_queue import DripFeedQueueService, UZB_TZ
    queue = DripFeedQueueService()
    noon = datetime(2026, 9, 10, 12, 0, tzinfo=UZB_TZ)
    far = (noon + timedelta(hours=40)).astimezone(timezone.utc)
    with patch.object(queue, "get_current_time", return_value=noon), \
         patch("database.db_manager.db_manager.get_latest_scheduled_drip_time", new=AsyncMock(return_value=far)):
        assert await queue.calculate_scheduled_time(_pair(drip_delay_minutes=60)) == noon + timedelta(hours=24)
    evening = datetime(2026, 9, 10, 22, 50, tzinfo=UZB_TZ)
    with patch.object(queue, "get_current_time", return_value=evening), \
         patch("database.db_manager.db_manager.get_latest_scheduled_drip_time", new=AsyncMock(return_value=None)):
        scheduled = await queue.calculate_scheduled_time(_pair(drip_delay_minutes=30, night_mode="buffer"))
    assert scheduled.hour == 8 and scheduled.date() == datetime(2026, 9, 11).date()


async def test_drip_worker_releases_undelivered_records():
    from services.drip_feed_queue import DripFeedQueueService
    with patch("database.db_manager.db_manager.forget_cloned_messages", new=AsyncMock()) as forget:
        await DripFeedQueueService._release_undelivered(5, {"source_msg_ids": [1, 2], "progress": {}})
        await DripFeedQueueService._release_undelivered(5, {"source_msg_ids": [3], "progress": {"delivered_target_id": 9}})
    forget.assert_awaited_once_with(5, [1, 2])


# ---------------------------------------------------------------- media handler (E-M7, E-L8, E-L18, LS-M7)

def test_media_type_prefers_sticker_and_gif_over_video():
    from services.media_handler import MediaHandler
    doc = object()
    base = dict(media=SimpleNamespace(), photo=None, voice=None, video_note=None, audio=None, poll=None,
                contact=None, geo=None, venue=None, document=doc, video=doc)
    assert MediaHandler.get_media_type(SimpleNamespace(**base, sticker=doc, gif=None)) == "sticker"
    assert MediaHandler.get_media_type(SimpleNamespace(**base, sticker=None, gif=doc)) == "animation"
    assert MediaHandler.get_media_type(SimpleNamespace(**base, sticker=None, gif=None)) == "video"


def test_original_filename_is_sanitized():
    from services.media_handler import MediaHandler
    assert MediaHandler.get_original_filename(SimpleNamespace(file=SimpleNamespace(name="../Hisobot 2026?.pdf"))) == "Hisobot 2026_.pdf"
    assert MediaHandler.get_original_filename(SimpleNamespace(file=None)) is None


def test_cleaner_keeps_held_recent_and_queued_referenced_files(tmp_path):
    from services.media_handler import MediaHandler, MIN_ORPHAN_AGE_SECONDS
    handler = MediaHandler(temp_dir=str(tmp_path))
    held = tmp_path / "a.jpg"
    held.write_bytes(b"x")
    handler.hold_path(held)
    assert not handler._is_expired(str(held), "a.jpg", 10 ** 6, 0, set())
    handler.release_path(held)
    assert handler._is_expired(str(held), "a.jpg", 10 ** 6, 300, set())
    queued = tmp_path / "queued_b.jpg"
    queued.write_bytes(b"x")
    refs = {handler._path_key(queued)}
    assert not handler._is_expired(str(queued), "queued_b.jpg", 10 ** 6, 300, refs)
    assert handler._is_expired(str(queued), "queued_b.jpg", MIN_ORPHAN_AGE_SECONDS + 1, 300, set())


async def test_album_buffer_uses_sliding_debounce():
    from services.media_handler import MediaGroupBuffer
    buffer = MediaGroupBuffer(debounce_delay=0.15, max_wait=2.0)
    flushed = []

    async def done(key, msgs):
        flushed.append([m.id for m in msgs])

    for i in range(3):
        buffer.add_message("g", SimpleNamespace(id=i), done)
        await asyncio.sleep(0.1)
    assert flushed == []
    await asyncio.sleep(0.25)
    assert flushed == [[0, 1, 2]]


# ---------------------------------------------------------------- retries (E-L3, E-L7)

def _bad_request(msg):
    return TelegramBadRequest(method=MagicMock(), message=msg)


async def test_send_with_retry_does_not_repeat_timeouts_or_413():
    engine = ClonerEngine()
    timeout = AsyncMock(side_effect=TelegramNetworkError(method=MagicMock(), message="Request timeout error"))
    with pytest.raises(TelegramNetworkError):
        await engine._send_with_retry(timeout, chat_id=1)
    assert timeout.await_count == 1
    too_large = AsyncMock(side_effect=TelegramEntityTooLarge(method=MagicMock(), message="Request Entity Too Large"))
    with pytest.raises(TelegramEntityTooLarge):
        await engine._send_with_retry(too_large, chat_id=1)
    assert too_large.await_count == 1


async def test_send_with_retry_drops_rejected_buttons_once():
    engine = ClonerEngine()
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", url="https://x.uz")]])
    send = AsyncMock(side_effect=[_bad_request("Bad Request: BUTTON_URL_INVALID"), "ok"])
    assert await engine._send_with_retry(send, chat_id=1, text="t", reply_markup=markup) == "ok"
    assert send.call_args.kwargs["reply_markup"] is None


async def test_plain_text_tier_keeps_icon_removal_and_waits_for_flood():
    engine = ClonerEngine()
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", url="https://x.uz", icon_custom_emoji_id="123")]])
    send = AsyncMock(side_effect=[
        _bad_request("Bad Request: can't parse entities"),
        _bad_request("Bad Request: can't parse entities"),
        _bad_request("Bad Request: can't parse entities"),
        TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=1),
        "ok",
    ])
    with patch("services.cloner_engine.asyncio.sleep", new=AsyncMock()) as slept:
        assert await engine._send_with_retry(send, chat_id=1, text="<b>x</b>", parse_mode="HTML", reply_markup=markup) == "ok"
    final = send.call_args.kwargs
    assert final["parse_mode"] is None and final["text"] == "x"
    assert final["reply_markup"].inline_keyboard[0][0].icon_custom_emoji_id is None
    slept.assert_awaited()


def test_button_remapper_drops_or_remaps_every_telegram_link():
    from services.button_remapper import SmartButtonRemapper
    buttons = [[{"text": "A", "url": "tg://resolve?domain=rival"}, {"text": "B", "url": "https://rival.t.me"},
                {"text": "C", "url": "https://telegram.dog/rival"}, {"text": "D", "url": "https://site.uz"}]]
    kept = SmartButtonRemapper.build_remapped_markup(buttons, target_channel_link=None)
    assert [b.url for b in kept.inline_keyboard[0]] == ["https://site.uz"]
    remapped = SmartButtonRemapper.build_remapped_markup(buttons, target_channel_link="https://t.me/mine")
    assert [b.url for b in remapped.inline_keyboard[0]][:3] == ["https://t.me/mine"] * 3


def test_cta_keyboard_can_omit_icons():
    from services.dynamic_affiliate_engine import dynamic_affiliate_engine
    kb = dynamic_affiliate_engine.build_cta_keyboard([("A", "https://x.uz", "123")], with_icons=False)
    assert kb.inline_keyboard[0][0].icon_custom_emoji_id is None


# ---------------------------------------------------------------- URLs (E-M20)

def test_affiliate_rewrite_unescapes_and_reescapes_urls():
    from services.affiliate_replacer import AffiliateReplacer
    text, protected = AffiliateReplacer.replace_and_protect('<a href="https://amazon.com/dp/X?a=1&amp;b=2">Buy</a>', "amazon.com=tag=me")
    assert text == '<a href="___AFF_PROT_0___">Buy</a>'
    assert protected["___AFF_PROT_0___"] == "https://amazon.com/dp/X?a=1&amp;b=2&amp;tag=me"


def test_cta_links_are_not_cut_at_escaped_ampersands():
    from services.dynamic_affiliate_engine import dynamic_affiliate_engine
    _text, buttons = dynamic_affiliate_engine.extract_and_convert_links("Ko'ring https://shop.uz/x?a=1&amp;b=2 bugun", "")
    assert buttons[0][1] == "https://shop.uz/x?a=1&b=2"


# ---------------------------------------------------------------- AI output validation (E-M21)

async def test_ai_paraphrase_rejects_outputs_that_drop_tags():
    from services.ai_paraphraser import AIParaphraserService
    service = AIParaphraserService()
    with patch("services.ai_paraphraser.settings") as fake_settings, \
         patch.object(service, "_paraphrase_with_gemini_async", new=AsyncMock(return_value="tagless rewrite")):
        fake_settings.GEMINI_API_KEY = "k"
        out = await service.paraphrase_async("<b>Muhim</b> yangilik matni shu yerda", mode="formal")
    assert "<b>Muhim</b>" in out and "tagless" not in out


def test_ai_ad_detector_rejects_truncated_or_tagged_rewrites():
    from services.ai_ad_detector import AIAdDetector
    original = "Yangilik matni uzun va muhim. " * 5
    assert AIAdDetector._accept_ai_cleaned_text(original, "Yangi") is None
    assert AIAdDetector._accept_ai_cleaned_text("<b>x</b> " + original, original) is None
    assert AIAdDetector._accept_ai_cleaned_text(original, original.strip() + " &") == original.strip() + " &amp;"


# ---------------------------------------------------------------- translator (E-H1, E-M17)

async def test_translator_times_out_uses_fresh_instances_and_does_not_cache_failures():
    from services.translator_service import TranslatorService
    service = TranslatorService()
    instances = []

    def _factory(source="auto", target="uz"):
        inst = MagicMock()
        inst.translate.side_effect = lambda text: time.sleep(0.6) or "tarjima"
        instances.append(inst)
        return inst

    with patch.object(service, "_get_translator", side_effect=_factory), \
         patch("services.translator_service.TRANSLATE_TIMEOUT_SECONDS", 0.1):
        assert await service.translate_text("Hello world", target_lang="uz") == "Hello world"
    assert not service._cache
    first = service._get_translator("auto", "uz")
    assert first is not service._get_translator("auto", "uz")


# ---------------------------------------------------------------- emoji clusters (E-L5)

def test_emoji_converter_keeps_sequences_whole_and_drops_wrong_mappings():
    from services.emoji_converter import emoji_converter
    assert emoji_converter.convert_to_premium_emojis("👍🏽 ❤️\u200d🔥 👨\u200d💻") == "👍🏽 ❤️\u200d🔥 👨\u200d💻"
    assert emoji_converter.convert_to_premium_emojis("🔟 🔴 ➕ 🚗 🛏️") == "🔟 🔴 ➕ 🚗 🛏️"
    out = emoji_converter.convert_to_premium_emojis("🇫🇷🇺🇿")
    assert out.startswith("🇫🇷<tg-emoji") and "🇷🇺" not in out


# ---------------------------------------------------------------- video watermark (E-L13)

def test_video_watermark_escaping_and_timeout_scaling(tmp_path):
    from services.video_watermark_service import VideoWatermarkService
    # Option level: \ ' : escaped; filtergraph level: single-quoted with ' written as '\''
    assert VideoWatermarkService._escape_filter_value("C:\\it's\\a.txt") == "'C\\:/it\\'\\''s/a.txt'"
    clip = tmp_path / "v.mp4"
    clip.write_bytes(b"0" * (60 * 1024 * 1024))
    assert VideoWatermarkService._timeout_for(str(clip)) > 120
    assert VideoWatermarkService._timeout_for(str(clip), duration=10 ** 6) == 1800.0


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg is not installed")
def test_video_watermark_escaped_paths_are_accepted_by_ffmpeg(tmp_path):
    """The escaped textfile path must survive both FFmpeg parsing levels, even with ' [ ] , ; in it."""
    import subprocess
    from services.video_watermark_service import VideoWatermarkService
    weird_dir = tmp_path / "it's [a],b;c"
    weird_dir.mkdir()
    text_file = weird_dir / "wm.txt"
    text_file.write_text("O'zbek: 100% [test]", encoding="utf-8")
    vf = (f"drawtext=textfile={VideoWatermarkService._escape_filter_value(str(text_file))}"
          f":expansion=none:fontsize=12:fontcolor=white:x=1:y=1")
    font = VideoWatermarkService()._find_default_fontfile()
    if font:
        vf += f":fontfile={VideoWatermarkService._escape_filter_value(font)}"
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
         "-vf", vf, "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 0, res.stderr


async def test_video_text_watermark_uses_textfile(tmp_path):
    from services.video_watermark_service import VideoWatermarkService
    service = VideoWatermarkService()
    clip = tmp_path / "v.mp4"
    clip.write_bytes(b"0" * 100)
    proc = AsyncMock()
    proc.communicate.return_value = (b"", b"")
    proc.returncode = 1
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        assert await service.apply_video_text_watermark(str(clip), "O'zbek: 100%", output_dir=str(tmp_path)) is None
    vf = spawn.call_args.args[spawn.call_args.args.index("-vf") + 1]
    assert "textfile=" in vf and "expansion=none" in vf and "O'zbek" not in vf
    assert not [f for f in os.listdir(tmp_path) if f.startswith("wm_text_")]


# ---------------------------------------------------------------- steganography (E-L15)

def test_invisible_watermark_roundtrip_on_real_photo(tmp_path):
    from services.steganography_service import steganography_service
    import numpy as np
    rng = np.random.default_rng(3)
    src = tmp_path / "noise.png"
    Image.fromarray(rng.integers(0, 255, (320, 320, 3), dtype=np.uint8)).save(src)
    out = tmp_path / "wm.png"
    assert steganography_service.embed_watermark(str(src), str(out), "UID_1_CH_2")
    assert steganography_service.extract_watermark(str(out)) == "UID_1_CH_2"
    assert steganography_service.embed_watermark_bytes(src.read_bytes(), "UID_1_CH_2")


# ---------------------------------------------------------------- rate limiter (LS-L9)

async def test_rate_limiter_shares_budget_across_spellings_and_counts_album_items():
    from services.rate_limiter import SmartDelayEngine
    limiter = SmartDelayEngine(min_delay=0.1, max_delay=0.1)
    started = time.monotonic()
    await limiter.wait_for_slot("-1001234567890", cost=2)
    await limiter.wait_for_slot("@name", peer_id=1234567890)
    assert time.monotonic() - started >= 0.19
    assert limiter._normalize_channel_key("-1001234567890") == limiter._normalize_channel_key("1234567890")


async def test_rate_limiter_never_prunes_a_lock_in_use():
    from services.rate_limiter import SmartDelayEngine
    limiter = SmartDelayEngine(min_delay=0.01, max_delay=0.01)
    lock = await limiter._acquire_channel_lock("chan")
    limiter._last_post_time["chan"] = time.monotonic() - 10_000
    await limiter.prune_stale_locks(max_idle_seconds=1)
    assert limiter._locks.get("chan") is lock
    limiter._release_lock_user("chan")
    await limiter.prune_stale_locks(max_idle_seconds=1)
    assert "chan" not in limiter._locks


# ---------------------------------------------------------------- fast telethon (LS-L4b)

async def test_parallel_download_aborts_on_long_flood_wait():
    from telethon.errors import FloodWaitError
    from services.fast_telethon import FastTelethonEngine

    class _Client:
        session = None

        async def __call__(self, request):
            raise FloodWaitError(request=None, capture=90)

    started = time.monotonic()
    ok = await FastTelethonEngine.download_file_parallel(
        _Client(), tl.InputDocumentFileLocation(id=1, access_hash=1, file_reference=b"", thumb_size=""),
        io.BytesIO(), file_size=5 * 1024 * 1024, max_workers=2)
    assert ok is False and time.monotonic() - started < 2
    assert FastTelethonEngine.get_optimal_worker_count(600 * 1024 * 1024, premium=False) == 3


# ---------------------------------------------------------------- test post & loop hook (lead notes)

async def test_test_post_escapes_user_values():
    engine = ClonerEngine(bot=_bot())
    pair = _pair(source_channel="@a<b", source_title="S&P", image_watermark_text="<x>")
    ok, _msg = await engine.send_test_post(pair)
    text = engine.bot.send_message.call_args.kwargs["text"]
    assert ok and "S&amp;P" in text and "@a&lt;b" in text and "&lt;x&gt;" in text


def test_recent_sent_targets_membership():
    engine = ClonerEngine()
    engine._remember_sent(SimpleNamespace(message_id=55, chat=SimpleNamespace(id=-1001234567890)), None)
    engine._remember_sent(SimpleNamespace(id=56, chat_id=-1001234567890), None)
    assert (1234567890, 55) in engine.recent_sent_targets and (1234567890, 56) in engine.recent_sent_targets
    assert (1234567890, 57) not in engine.recent_sent_targets


async def test_extract_price_runs_off_loop_on_capped_plain_text():
    engine = ClonerEngine()
    seen = {}

    def _fake(text):
        import threading
        seen["thread"] = threading.current_thread() is threading.main_thread()
        seen["text"] = text
        return 700.0

    with patch("services.story_cloner_service.story_cloner_service.extract_price", side_effect=_fake):
        assert await engine._extract_price("<b>Narxi</b>: 700$ " + "x" * 5000) == 700.0
    assert seen["thread"] is False and len(seen["text"]) == 1500 and "<b>" not in seen["text"]


# ---------------------------------------------------------------- caption / message split points

def _plain(html_text):
    return TextProcessor.html_to_plain(html_text)


def test_split_prefers_paragraph_then_line_then_sentence_then_word():
    para = "A" * 40 + "\n\n" + "B" * 30 + "\n" + "C" * 20
    caption, overflow = TextProcessor.fit_caption_limit(para, max_limit=80)
    assert caption == "A" * 40 and overflow.startswith("B" * 30)

    lines = "A" * 60 + "\n" + "B" * 60
    caption, overflow = TextProcessor.fit_caption_limit(lines, max_limit=100)
    assert caption == "A" * 60 and overflow == "B" * 60

    sentences = "Birinchi gap tugadi. Ikkinchi gap esa ancha uzunroq davom etadi"
    caption, overflow = TextProcessor.fit_caption_limit(sentences, max_limit=35)
    assert caption == "Birinchi gap tugadi." and overflow.startswith("Ikkinchi")

    words = "so'z " * 30
    caption, overflow = TextProcessor.fit_caption_limit(words, max_limit=52)
    assert all(part == "so'z" for part in caption.split()) and all(part == "so'z" for part in overflow.split())


def test_split_ignores_a_line_break_that_would_waste_most_of_the_caption():
    text = "Sarlavha\n" + "uzun matn " * 30
    caption, overflow = TextProcessor.fit_caption_limit(text, max_limit=100)
    assert TextProcessor.get_visible_text_length(caption) > 50
    assert caption.startswith("Sarlavha\n")


def test_split_keeps_closing_tags_in_the_caption_and_reopens_open_formatting():
    caption, overflow = TextProcessor.fit_caption_limit(
        "<b>Birinchi gap tugadi.</b> Ikkinchi gap ham bor, lekin u sig'maydi.", max_limit=30)
    assert caption == "<b>Birinchi gap tugadi.</b>" and not overflow.startswith("<b>")

    caption, overflow = TextProcessor.fit_caption_limit(
        '<a href="https://x.uz">havola matni</a> va <i>boshqa uzun kursiv matn davom etadi</i>', max_limit=25)
    assert caption.endswith("<i>boshqa</i>") and caption.count("<i>") == caption.count("</i>")
    assert overflow == "<i>uzun kursiv matn davom etadi</i>"


def test_split_never_cuts_entities_or_leaves_empty_elements():
    text = "&quot;" * 5 + "<tg-emoji emoji-id=\"1\">🔥</tg-emoji>" + " yakun"
    caption, overflow = TextProcessor.fit_caption_limit(text, max_limit=6)
    assert "&quo" not in caption.replace("&quot;", "")
    assert "<tg-emoji emoji-id=\"1\"></tg-emoji>" not in caption
    assert _plain(caption) + _plain(overflow).strip() in ('"""""🔥 yakun', '"""""🔥yakun')


def test_fit_text_limit_splits_long_unbroken_text_without_losing_anything():
    text = "<b>" + "X" * 9000 + "</b>"
    chunks = TextProcessor.fit_text_limit(text, max_limit=4096)
    assert len(chunks) == 3
    assert all(TextProcessor.get_visible_text_length(c) <= 4096 for c in chunks)
    assert all(c.startswith("<b>") and c.endswith("</b>") for c in chunks)
    assert sum(c.count("X") for c in chunks) == 9000
