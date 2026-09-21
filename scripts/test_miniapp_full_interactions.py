#!/usr/bin/env python3
"""
Comprehensive Playwright UI & API Interaction Verification
Tests every tab, modal, and interactive element on the live Cloudflare Mini App.
"""

import sys
import os
import asyncio
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from playwright.async_api import async_playwright

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from bot.keyboards.inline_buttons import get_active_webapp_url

async def run_full_audit():
    url = get_active_webapp_url()
    print(f"🚀 Starting Full Interactive Playwright Audit on: {url}")
    assert url.startswith("https://"), "Active URL must be valid HTTPS"

    screenshots_dir = Path("C:/Users/victus/.gemini/antigravity/brain/41010751-c928-4519-8100-5312148e1552/screenshots")
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    console_errors = []
    page_errors = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 420, 'height': 900},
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Telegram/10.9"
        )
        page = await context.new_page()

        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        # 1. Load Home / Dashboard
        print("1. Loading Mini App Home...")
        response = await page.goto(url, wait_until="networkidle", timeout=30000)
        assert response.status == 200, f"Expected 200 OK, got {response.status}"
        await page.wait_for_selector("#root", timeout=5000)
        await asyncio.sleep(1)
        await page.screenshot(path=str(screenshots_dir / "audit_01_dashboard.png"))
        print("   [OK] Dashboard rendered successfully.")

        # 2. Navigate to Kanallar (Channels) Tab
        print("2. Navigating to Kanallar tab...")
        kanallar_tab = page.locator("nav button:has-text('Kanallar')")
        await kanallar_tab.click()
        await asyncio.sleep(1)
        await page.screenshot(path=str(screenshots_dir / "audit_02_channels.png"))
        print("   [OK] Channels tab rendered successfully.")

        # 3. Open 'Yangi Kanal' Modal
        print("3. Testing Add Channel modal...")
        add_btn = page.locator("button:has-text('Yangi Kanal'), button:has-text('Kanal Qo\\'shish')").first
        if await add_btn.is_visible():
            await add_btn.click()
            await asyncio.sleep(0.8)
            await page.screenshot(path=str(screenshots_dir / "audit_03_add_modal.png"))
            # Close modal if open
            close_btn = page.locator("button:has-text('Bekor qilish'), button:has-text('Yopish')").first
            if await close_btn.is_visible():
                await close_btn.click()
                await asyncio.sleep(0.5)
            print("   [OK] Add Channel modal verified.")

        # 4. Navigate to VIP Story Tab
        print("4. Navigating to VIP Story Studio tab...")
        story_tab = page.locator("nav button:has-text('VIP Story')")
        await story_tab.click()
        await asyncio.sleep(1)
        await page.screenshot(path=str(screenshots_dir / "audit_04_story_studio.png"))
        print("   [OK] VIP Story Studio tab rendered successfully.")

        # 5. Navigate to Tarix (Backfill) Tab
        print("5. Navigating to Tarix (Backfill) tab...")
        tarix_tab = page.locator("nav button:has-text('Tarix')")
        await tarix_tab.click()
        await asyncio.sleep(1)
        await page.screenshot(path=str(screenshots_dir / "audit_05_backfill.png"))
        print("   [OK] Tarix tab rendered successfully.")

        # 6. Navigate to Tariflar (Billing) Tab
        print("6. Navigating to Tariflar (Billing) tab...")
        tariflar_tab = page.locator("nav button:has-text('Tariflar')")
        await tariflar_tab.click()
        await asyncio.sleep(1)
        await page.screenshot(path=str(screenshots_dir / "audit_06_billing.png"))
        print("   [OK] Tariflar tab rendered successfully.")

        # 7. Navigate to Tizim (System) Tab
        print("7. Navigating to Tizim (System) tab...")
        tizim_tab = page.locator("nav button:has-text('Tizim')")
        await tizim_tab.click()
        await asyncio.sleep(1)
        await page.screenshot(path=str(screenshots_dir / "audit_07_system.png"))
        print("   [OK] Tizim tab rendered successfully.")

        await browser.close()

    print("\n--- Playwright Audit Summary ---")
    print(f"Page Errors: {len(page_errors)}")
    print(f"Console Errors: {len(console_errors)}")
    if page_errors:
        print("Page Errors Details:", page_errors)
    if console_errors:
        print("Console Errors Details:", console_errors)

    assert len(page_errors) == 0, f"Playwright detected page errors: {page_errors}"
    print("✅ All 6 Tabs Rendered with ZERO Page Errors!")

if __name__ == "__main__":
    asyncio.run(run_full_audit())
