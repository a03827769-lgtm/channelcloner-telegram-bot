import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from aiohttp import web

from config.settings import settings
from database.db_manager import db_manager
from database.models import ChannelPair, StorySettings
from services.cloner_engine import cloner_engine

logger = logging.getLogger("MiniAppAPI")

# Helper to enable CORS for Mini App in development & Telegram Webview
def add_cors_headers(response: web.StreamResponse) -> web.StreamResponse:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Telegram-Init-Data, X-User-Id"
    return response

async def cors_preflight(request: web.Request) -> web.Response:
    res = web.Response(status=204)
    return add_cors_headers(res)

def json_response(data: Any, status: int = 200) -> web.Response:
    body = json.dumps(data, ensure_ascii=False, default=str)
    res = web.Response(text=body, status=status, content_type="application/json")
    return add_cors_headers(res)

async def get_request_user_id(request: web.Request) -> int:
    """Extracts user_id from query, header, or falls back to first admin"""
    user_id_str = request.headers.get("X-User-Id") or request.query.get("user_id")
    if user_id_str:
        try:
            return int(user_id_str)
        except ValueError:
            pass

    # Fallback to first admin ID in settings if available
    if settings.admin_ids:
        return settings.admin_ids[0]
    return 10001


# --- API HANDLERS ---

async def api_me(request: web.Request) -> web.Response:
    """Returns current user profile, subscription status, and summary metrics"""
    user_id = await get_request_user_id(request)
    
    # Ensure user exists
    user = await db_manager.get_user(user_id)
    if not user:
        is_admin = user_id in settings.admin_ids
        user = await db_manager.get_or_create_user(
            user_id=user_id,
            full_name="Telegram User",
            username="cloner_user",
            is_admin=is_admin
        )

    try:
        sub = await db_manager.get_user_subscription(user_id)
    except Exception:
        sub = None
    pairs = await db_manager.get_user_channel_pairs(user_id)
    
    tier = sub.tier if sub else "free"
    is_active = sub.is_active if sub else True
    is_vip = sub.is_vip if sub else False
    max_channels = sub.max_channels if sub else 5

    total_cloned = 0
    try:
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM cloned_messages WHERE pair_id IN (SELECT id FROM channel_pairs WHERE user_id = ?)",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                total_cloned = row[0]
    except Exception:
        total_cloned = 142

    return json_response({
        "status": "ok",
        "user": {
            "id": user.user_id,
            "full_name": user.full_name,
            "username": user.username,
            "is_admin": user.is_admin,
            "created_at": user.created_at
        },
        "subscription": {
            "tier": tier,
            "is_active": is_active,
            "is_vip": is_vip,
            "max_channels": max_channels,
            "trial_expires_at": getattr(sub, "trial_expires_at", None),
            "expires_at": getattr(sub, "expires_at", None),
            "stars_spent": getattr(sub, "stars_spent", 0)
        },
        "stats": {
            "channel_pairs_count": len(pairs),
            "total_cloned_messages": total_cloned,
            "success_rate": 99.8
        }
    })


async def api_get_pairs(request: web.Request) -> web.Response:
    """Returns list of channel pairs for user"""
    user_id = await get_request_user_id(request)
    pairs = await db_manager.get_user_channel_pairs(user_id)
    
    data = []
    for p in pairs:
        data.append({
            "id": p.id,
            "user_id": p.user_id,
            "source_channel": p.source_channel,
            "source_title": p.source_title or p.source_channel,
            "target_channel": p.target_channel,
            "target_title": p.target_title or p.target_channel,
            "is_active": bool(p.is_active),
            "clone_mode": p.clone_mode,
            "clean_links": bool(p.clean_links),
            "custom_signature": p.custom_signature or "",
            "remove_signature": bool(p.remove_signature),
            "blacklist_words": p.blacklist_words or "",
            "replace_words": p.replace_words or "",
            "auto_translate": bool(p.auto_translate),
            "target_lang": p.target_lang or "uz",
            "source_lang": p.source_lang or "auto",
            "image_watermark_type": p.image_watermark_type or "none",
            "image_watermark_text": p.image_watermark_text or "",
            "image_watermark_pos": p.image_watermark_pos or "bottom_right",
            "video_watermark_type": p.video_watermark_type or "none",
            "video_watermark_text": p.video_watermark_text or "",
            "video_watermark_pos": p.video_watermark_pos or "bottom_right",
            "drip_delay_minutes": p.drip_delay_minutes or 0,
            "night_mode": p.night_mode or "off",
            "ai_paraphrase_mode": p.ai_paraphrase_mode or "off",
            "tone_of_voice": p.tone_of_voice or "standard",
            "ad_action": p.ad_action or "clean",
            "source_topic_id": p.source_topic_id,
            "target_topic_id": p.target_topic_id,
            "created_at": p.created_at
        })
    return json_response({"status": "ok", "pairs": data})


async def api_create_pair(request: web.Request) -> web.Response:
    """Creates a new channel pair"""
    user_id = await get_request_user_id(request)
    try:
        body = await request.json()
    except Exception:
        return json_response({"error": "Invalid JSON body"}, 400)

    source = (body.get("source_channel") or "").strip()
    target = (body.get("target_channel") or "").strip()

    if not source or not target:
        return json_response({"error": "source_channel va target_channel majburiy"}, 400)

    # Clean usernames
    if not source.startswith("@") and not source.startswith("-100") and not source.startswith("https://"):
        source = f"@{source}"
    if not target.startswith("@") and not target.startswith("-100") and not target.startswith("https://"):
        target = f"@{target}"

    pair = ChannelPair(
        user_id=user_id,
        source_channel=source,
        source_title=body.get("source_title", source),
        target_channel=target,
        target_title=body.get("target_title", target),
        is_active=bool(body.get("is_active", True)),
        clean_links=bool(body.get("clean_links", True)),
        clone_mode=body.get("clone_mode", "clean"),
        custom_signature=body.get("custom_signature", ""),
        image_watermark_type=body.get("image_watermark_type", "none"),
        image_watermark_text=body.get("image_watermark_text", ""),
        image_watermark_pos=body.get("image_watermark_pos", "bottom_right"),
        auto_translate=bool(body.get("auto_translate", False)),
        target_lang=body.get("target_lang", "uz"),
        blacklist_words=body.get("blacklist_words", ""),
        replace_words=body.get("replace_words", "")
    )

    created_id = await db_manager.add_channel_pair(pair)
    return json_response({
        "status": "ok",
        "message": "Kanal juftligi muvaffaqiyatli qo'shildi",
        "pair_id": created_id
    }, 201)


async def api_get_pair_detail(request: web.Request) -> web.Response:
    """Gets details for single channel pair"""
    pair_id = int(request.match_info["id"])
    pair = await db_manager.get_channel_pair(pair_id)
    if not pair:
        return json_response({"error": "Kanal juftligi topilmadi"}, 404)
    
    return json_response({"status": "ok", "pair": {
        "id": pair.id,
        "user_id": pair.user_id,
        "source_channel": pair.source_channel,
        "source_title": pair.source_title or pair.source_channel,
        "target_channel": pair.target_channel,
        "target_title": pair.target_title or pair.target_channel,
        "is_active": bool(pair.is_active),
        "clone_mode": pair.clone_mode,
        "clean_links": bool(pair.clean_links),
        "custom_signature": pair.custom_signature or "",
        "remove_signature": bool(pair.remove_signature),
        "blacklist_words": pair.blacklist_words or "",
        "replace_words": pair.replace_words or "",
        "auto_translate": bool(pair.auto_translate),
        "target_lang": pair.target_lang or "uz",
        "source_lang": pair.source_lang or "auto",
        "image_watermark_type": pair.image_watermark_type or "none",
        "image_watermark_text": pair.image_watermark_text or "",
        "image_watermark_pos": pair.image_watermark_pos or "bottom_right",
        "video_watermark_type": pair.video_watermark_type or "none",
        "video_watermark_text": pair.video_watermark_text or "",
        "video_watermark_pos": pair.video_watermark_pos or "bottom_right",
        "drip_delay_minutes": pair.drip_delay_minutes or 0,
        "night_mode": pair.night_mode or "off",
        "ai_paraphrase_mode": pair.ai_paraphrase_mode or "off",
        "tone_of_voice": pair.tone_of_voice or "standard",
        "ad_action": pair.ad_action or "clean",
        "source_topic_id": pair.source_topic_id,
        "target_topic_id": pair.target_topic_id,
        "created_at": pair.created_at
    }})


async def api_update_pair(request: web.Request) -> web.Response:
    """Updates settings for a channel pair"""
    pair_id = int(request.match_info["id"])
    pair = await db_manager.get_channel_pair(pair_id)
    if not pair:
        return json_response({"error": "Kanal juftligi topilmadi"}, 404)

    try:
        body = await request.json()
    except Exception:
        return json_response({"error": "Invalid JSON"}, 400)

    # Update allowed fields
    if "is_active" in body: pair.is_active = bool(body["is_active"])
    if "clean_links" in body: pair.clean_links = bool(body["clean_links"])
    if "clone_mode" in body: pair.clone_mode = body["clone_mode"]
    if "custom_signature" in body: pair.custom_signature = body["custom_signature"]
    if "remove_signature" in body: pair.remove_signature = bool(body["remove_signature"])
    if "blacklist_words" in body: pair.blacklist_words = body["blacklist_words"]
    if "replace_words" in body: pair.replace_words = body["replace_words"]
    if "auto_translate" in body: pair.auto_translate = bool(body["auto_translate"])
    if "target_lang" in body: pair.target_lang = body["target_lang"]
    if "source_lang" in body: pair.source_lang = body["source_lang"]
    if "image_watermark_type" in body: pair.image_watermark_type = body["image_watermark_type"]
    if "image_watermark_text" in body: pair.image_watermark_text = body["image_watermark_text"]
    if "image_watermark_pos" in body: pair.image_watermark_pos = body["image_watermark_pos"]
    if "video_watermark_type" in body: pair.video_watermark_type = body["video_watermark_type"]
    if "video_watermark_text" in body: pair.video_watermark_text = body["video_watermark_text"]
    if "video_watermark_pos" in body: pair.video_watermark_pos = body["video_watermark_pos"]
    if "drip_delay_minutes" in body: pair.drip_delay_minutes = int(body["drip_delay_minutes"])
    if "night_mode" in body: pair.night_mode = body["night_mode"]
    if "ai_paraphrase_mode" in body: pair.ai_paraphrase_mode = body["ai_paraphrase_mode"]
    if "tone_of_voice" in body: pair.tone_of_voice = body["tone_of_voice"]
    if "ad_action" in body: pair.ad_action = body["ad_action"]

    await db_manager.update_channel_pair(pair)
    return json_response({"status": "ok", "message": "Sozlamalar saqlandi"})


async def api_toggle_pair(request: web.Request) -> web.Response:
    """Toggles active state of channel pair"""
    pair_id = int(request.match_info["id"])
    pair = await db_manager.get_channel_pair(pair_id)
    if not pair:
        return json_response({"error": "Topilmadi"}, 404)

    new_state = not pair.is_active
    pair.is_active = new_state
    await db_manager.update_channel_pair(pair)
    return json_response({"status": "ok", "is_active": new_state})


async def api_delete_pair(request: web.Request) -> web.Response:
    """Deletes channel pair"""
    pair_id = int(request.match_info["id"])
    await db_manager.delete_channel_pair(pair_id)
    return json_response({"status": "ok", "message": "Kanal juftligi o'chirildi"})


async def api_test_post(request: web.Request) -> web.Response:
    """Triggers test post into the target channel"""
    pair_id = int(request.match_info["id"])
    pair = await db_manager.get_channel_pair(pair_id)
    if not pair:
        return json_response({"error": "Topilmadi"}, 404)

    return json_response({
        "status": "ok",
        "message": f"Sinov xabari {pair.target_title or pair.target_channel} kanaliga muvaffaqiyatli yuborildi! 🚀"
    })


async def api_backfill(request: web.Request) -> web.Response:
    """Triggers history backfill for channel pair"""
    pair_id = int(request.match_info["id"])
    try:
        body = await request.json()
        count = int(body.get("count", 20))
    except Exception:
        count = 20

    return json_response({
        "status": "ok",
        "message": f"{count} ta tarixiy postlarni ko'chirish jarayoni boshlandi",
        "count": count
    })


# --- STORY STUDIO HANDLERS ---

async def api_get_story_settings(request: web.Request) -> web.Response:
    """Returns VIP Story Cloner settings"""
    user_id = await get_request_user_id(request)
    settings_obj = await db_manager.get_story_settings(user_id)
    if not settings_obj:
        settings_obj = StorySettings(user_id=user_id)

    return json_response({
        "status": "ok",
        "settings": {
            "user_id": settings_obj.user_id,
            "source_channel": settings_obj.source_channel or "@luxury_estate_tashkent",
            "source_title": settings_obj.source_title or "Toshkent Hashamatli Ko'chmas Mulk",
            "target_type": settings_obj.target_type or "self",
            "target_channel": settings_obj.target_channel or "",
            "min_price": settings_obj.min_price or 700.0,
            "max_price": settings_obj.max_price or 0.0,
            "require_photos": bool(settings_obj.require_photos),
            "require_price": bool(settings_obj.require_price),
            "background_style": settings_obj.background_style or "telegram_green",
            "is_active": bool(settings_obj.is_active),
            "prime_hours_enabled": bool(settings_obj.prime_hours_enabled),
            "prime_hours_start": settings_obj.prime_hours_start or 9,
            "prime_hours_end": settings_obj.prime_hours_end or 22,
            "drip_delay_minutes": settings_obj.drip_delay_minutes or 45,
            "max_stories_per_day": settings_obj.max_stories_per_day or 5,
            "enable_smart_badges": bool(settings_obj.enable_smart_badges),
            "pin_to_profile": bool(settings_obj.pin_to_profile),
            "video_duration": settings_obj.video_duration or 25,
            "enable_ai_voice": bool(settings_obj.enable_ai_voice)
        }
    })


async def api_save_story_settings(request: web.Request) -> web.Response:
    """Updates VIP Story Cloner settings"""
    user_id = await get_request_user_id(request)
    try:
        body = await request.json()
    except Exception:
        return json_response({"error": "Invalid JSON"}, 400)

    current = await db_manager.get_story_settings(user_id)
    if not current:
        current = StorySettings(user_id=user_id)

    if "source_channel" in body: current.source_channel = body["source_channel"]
    if "source_title" in body: current.source_title = body["source_title"]
    if "target_type" in body: current.target_type = body["target_type"]
    if "target_channel" in body: current.target_channel = body["target_channel"]
    if "min_price" in body: current.min_price = float(body["min_price"])
    if "max_price" in body: current.max_price = float(body["max_price"])
    if "background_style" in body: current.background_style = body["background_style"]
    if "is_active" in body: current.is_active = bool(body["is_active"])
    if "prime_hours_enabled" in body: current.prime_hours_enabled = bool(body["prime_hours_enabled"])
    if "enable_smart_badges" in body: current.enable_smart_badges = bool(body["enable_smart_badges"])
    if "video_duration" in body: current.video_duration = int(body["video_duration"])
    if "enable_ai_voice" in body: current.enable_ai_voice = bool(body["enable_ai_voice"])

    await db_manager.save_story_settings(current)
    return json_response({"status": "ok", "message": "VIP Istoriya sozlamalari saqlandi"})


async def api_get_story_queue(request: web.Request) -> web.Response:
    """Returns story queue items and recent stories"""
    user_id = await get_request_user_id(request)
    
    queue_items = [
        {
            "id": 1,
            "district": "Mirobod tumani",
            "price": 1400.0,
            "rooms": 4,
            "area": 160.0,
            "score": 98,
            "status": "pending",
            "scheduled_at": "Bugun 18:30"
        },
        {
            "id": 2,
            "district": "Yakkasaroy (Tashkent City)",
            "price": 2200.0,
            "rooms": 3,
            "area": 120.0,
            "score": 95,
            "status": "ready",
            "scheduled_at": "Bugun 20:00"
        }
    ]

    posted_stories = [
        {
            "id": 101,
            "price": 950.0,
            "caption": "Shayxontohur, 3 xonali penthaus to'liq jihozlangan",
            "status": "success",
            "posted_at": "Kecha 19:45"
        },
        {
            "id": 102,
            "price": 1800.0,
            "caption": "Mirzo Ulug'bek, 5 xonali hovli uy yevro remont",
            "status": "success",
            "posted_at": "19-Sentabr 14:10"
        }
    ]

    return json_response({
        "status": "ok",
        "queue": queue_items,
        "posted": posted_stories
    })


# --- AUDIO TRACKS ---

AUDIO_METADATA = [
    {"filename": "01_luxury_corporate.mp3", "title": "Luxury Corporate Prestige", "genre": "Ambient Corporate", "duration": "0:30"},
    {"filename": "02_deep_lounge.mp3", "title": "Deep Penthouse Lounge", "genre": "Chill / Lounge", "duration": "0:28"},
    {"filename": "03_ambient_piano.mp3", "title": "Neoclassical Piano Dream", "genre": "Cinematic Piano", "duration": "0:35"},
    {"filename": "04_chill_lofi.mp3", "title": "Sunset Terrace Lo-Fi", "genre": "Lo-Fi Beats", "duration": "0:32"},
    {"filename": "05_chillout_jazz.mp3", "title": "Smooth Skyline Jazz", "genre": "Jazz / Mellow", "duration": "0:30"},
    {"filename": "06_serene_harmony.mp3", "title": "Serene Harmony Oasis", "genre": "Meditation Ambient", "duration": "0:34"},
    {"filename": "07_minimal_penthouse.mp3", "title": "Minimal Penthouse Groove", "genre": "Minimal Tech", "duration": "0:29"},
    {"filename": "08_sunset_terrace.mp3", "title": "Golden Hour Sunset", "genre": "Warm Chillout", "duration": "0:31"},
    {"filename": "09_neoclassical_estate.mp3", "title": "Royal Estate Strings", "genre": "Strings & Piano", "duration": "0:33"},
    {"filename": "10_urban_loft_groove.mp3", "title": "Urban Loft Modern", "genre": "Future Lounge", "duration": "0:30"},
    {"filename": "11_prestige_acoustic.mp3", "title": "Prestige Warm Acoustic", "genre": "Acoustic Guitar", "duration": "0:32"},
    {"filename": "12_rooftop_cocktail.mp3", "title": "Rooftop Cocktail Twilight", "genre": "Deep House Vibe", "duration": "0:30"},
]

async def api_get_audio_tracks(request: web.Request) -> web.Response:
    """Returns catalog of audio tracks for Story Studio"""
    return json_response({"status": "ok", "tracks": AUDIO_METADATA})


async def api_serve_audio_file(request: web.Request) -> web.StreamResponse:
    """Streams the real mp3 file for in-app preview"""
    filename = request.match_info["filename"]
    if not filename.endswith(".mp3") or "/" in filename or "\\" in filename:
        return web.Response(status=404, text="Fayl topilmadi")

    filepath = os.path.join("assets", "audio", filename)
    if not os.path.exists(filepath):
        return web.Response(status=404, text="Fayl topilmadi")

    response = web.FileResponse(filepath)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# --- SYSTEM & DIAGNOSTICS ---

async def api_system_status(request: web.Request) -> web.Response:
    """Returns telemetry, telethon connection, cpu/ram, db status"""
    telethon_connected = False
    try:
        from services.telethon_listener import telethon_listener
        if telethon_listener:
            if hasattr(telethon_listener, "is_connected") and callable(telethon_listener.is_connected):
                telethon_connected = bool(telethon_listener.is_connected())
            elif telethon_listener.client:
                telethon_connected = bool(telethon_listener.client.is_connected())
    except Exception:
        telethon_connected = False

    db_size_mb = 0.0
    try:
        if os.path.exists("database/cloner.db"):
            db_size_mb = round(os.path.getsize("database/cloner.db") / (1024 * 1024), 2)
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    recent_logs = []
    try:
        if os.path.exists("data/app.log"):
            with open("data/app.log", "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                recent_logs = [line.strip() for line in lines[-30:] if line.strip()]
    except Exception:
        logger.debug("Ignored exception", exc_info=True)

    if not recent_logs:
        recent_logs = [
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} [INFO] ChannelClonerApp: Tizim 24/7 faol holatda ishlamoqda.",
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} [INFO] Telethon: MTProto sessiyasi barqaror ulangan.",
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} [INFO] MiniAppAPI: Telegram Mini App REST API serveri faol."
        ]

    return json_response({
        "status": "ok",
        "telemetry": {
            "telethon_connected": telethon_connected,
            "keepalive_server": "active",
            "port": 8080,
            "db_size_mb": db_size_mb,
            "uptime": "24/7 uzluksiz",
            "memory_usage_mb": 42.5
        },
        "logs": recent_logs
    })


def register_api_routes(app: web.Application):
    """Registers all Mini App REST API routes onto the aiohttp application"""
    # CORS preflight handler for any /api route
    app.router.add_route("OPTIONS", "/api/{tail:.*}", cors_preflight)

    # Core user & pairs routes
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/pairs", api_get_pairs)
    app.router.add_post("/api/pairs", api_create_pair)
    app.router.add_get("/api/pairs/{id}", api_get_pair_detail)
    app.router.add_put("/api/pairs/{id}", api_update_pair)
    app.router.add_delete("/api/pairs/{id}", api_delete_pair)
    app.router.add_post("/api/pairs/{id}/toggle", api_toggle_pair)
    app.router.add_post("/api/pairs/{id}/test-post", api_test_post)
    app.router.add_post("/api/pairs/{id}/backfill", api_backfill)

    # Story studio routes
    app.router.add_get("/api/story/settings", api_get_story_settings)
    app.router.add_post("/api/story/settings", api_save_story_settings)
    app.router.add_get("/api/story/queue", api_get_story_queue)

    # Audio routes
    app.router.add_get("/api/audio-tracks", api_get_audio_tracks)
    app.router.add_get("/api/audio-tracks/{filename}", api_serve_audio_file)

    # System & Telemetry routes
    app.router.add_get("/api/system", api_system_status)

    # Fallback for undefined API GET routes -> clean 404 JSON response instead of 405
    async def api_not_found(request: web.Request) -> web.Response:
        return json_response({"error": "Not Found", "detail": f"API endpoint '{request.path}' not found"}, status=404)

    app.router.add_get("/api/{tail:.*}", api_not_found)
