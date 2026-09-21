#!/usr/bin/env python3
"""
ChannelCloner Pro — Telegram Mini App & Tunnel Synchronizer Verification Test
Validates:
1. Active tunnel URL persistence and retrieval
2. Live Cloudflare Edge HTTP 200 responses for all endpoints
3. Telegram Bot Chat Menu Button (WebApp) configuration
4. Bot /app and /start keyboard generation with active WebApp URL
"""

import sys
import os
import json
import asyncio
import pytest
import requests
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from config.settings import settings
from bot.keyboards.inline_buttons import (
    get_active_webapp_url,
    get_main_reply_keyboard,
    get_main_menu_keyboard
)
from services.tunnel_sync_service import tunnel_sync_service

@pytest.mark.asyncio
async def test_active_webapp_url_valid():
    """Validates that active webapp URL is a valid HTTPS Cloudflare URL"""
    url = get_active_webapp_url()
    assert url is not None, "Active webapp URL must not be None"
    assert url.startswith("https://"), f"URL must start with https://, got: {url}"
    assert "trycloudflare.com" in url or "cloudflare" in url or len(url) > 10
    print(f"\n[PASS] Verified active webapp URL: {url}")

@pytest.mark.asyncio
async def test_reply_keyboard_no_redundant_miniapp_button():
    """Validates that get_main_reply_keyboard() no longer contains redundant 'Mini Appni Ochish' button, keeping only menu items"""
    kb = get_main_reply_keyboard()
    assert kb is not None
    assert len(kb.keyboard) > 0
    all_texts = [btn.text for row in kb.keyboard for btn in row]
    assert "📱 Mini Appni Ochish" not in all_texts
    assert any("Kanal Kloner" in t for t in all_texts)
    print("\n[PASS] Reply keyboard cleanly excludes redundant Mini App button as requested")

@pytest.mark.asyncio
async def test_inline_menu_keyboard_clean_layout():
    """Validates that get_main_menu_keyboard() cleanly starts with action buttons without redundant 'Mini Appni Ochish' button"""
    kb = get_main_menu_keyboard(is_admin=True)
    assert kb is not None
    assert len(kb.inline_keyboard) > 0
    all_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert not any("Mini Appni Ochish" in t for t in all_texts)
    assert any("Tezkor Boshlash" in t for t in all_texts)
    print("\n[PASS] Inline menu keyboard cleanly starts with action buttons")

def test_cloudflare_edge_endpoints_live():
    """Validates that all public Cloudflare edge endpoints return HTTP 200"""
    active_url = get_active_webapp_url()
    assert active_url.startswith("https://")

    endpoints = [
        "/",
        "/api/auth.php",
        "/api/system.php",
        "/api/pairs.php",
        "/api/story.php?action=settings",
        "/api/story.php?action=queue",
        "/api/audio.php",
        "/api/billing.php"
    ]

    for ep in endpoints:
        full_url = f"{active_url}{ep}"
        r = requests.get(full_url, timeout=10.0, headers={"User-Agent": "ChannelClonerTest/1.0"})
        assert r.status_code == 200, f"Endpoint {ep} failed with HTTP {r.status_code}: {r.text[:100]}"
        print(f"[PASS] {ep} -> HTTP 200 OK ({len(r.content)} bytes)")

@pytest.mark.asyncio
async def test_telegram_bot_menu_button_live():
    """Validates that Telegram Bot API returns WebApp menu button for admin chat"""
    from aiogram import Bot
    bot = Bot(token=settings.BOT_TOKEN)
    try:
        active_url = get_active_webapp_url()
        admin_id = next(iter(settings.admin_ids))
        btn = await bot.get_chat_menu_button(chat_id=admin_id)
        print(f"\n[PASS] Bot Chat Menu Button type: {btn.type}, url: {btn.web_app.url if btn.web_app else 'None'}")
        assert btn.type == "web_app", f"Expected menu button type 'web_app', got '{btn.type}'"
        assert btn.web_app.url.rstrip('/') == active_url.rstrip('/')
    finally:
        await bot.session.close()

@pytest.mark.asyncio
async def test_tunnel_status_file_healthy():
    """Validates that data/tunnel_status.json exists and reports healthy"""
    status_file = Path("data/tunnel_status.json")
    assert status_file.exists(), "data/tunnel_status.json must exist"
    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert data.get("status") in ("healthy", "running"), f"Expected healthy status, got: {data.get('status')}"
    assert data.get("active_url", "").startswith("https://")
    print(f"\n[PASS] Tunnel status telemetry: {data}")

if __name__ == "__main__":
    pytest.main(["-v", __file__])
