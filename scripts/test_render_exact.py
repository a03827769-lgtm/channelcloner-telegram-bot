import os
import sys
from playwright.sync_api import sync_playwright

bg_path = os.path.abspath("assets/story_backgrounds/telegram_green.jpg").replace("\\", "/")
output_path = os.path.abspath("temp_media/test_pixel_perfect_card.jpg")

html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    width: 1080px;
    height: 1920px;
    background: url("{bg_path}") no-repeat center center;
    background-size: cover;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
}}
.card-wrapper {{
    position: relative;
    width: 820px;
    margin-left: 70px;
}}
.card {{
    background: #ffffff;
    border-radius: 22px;
    overflow: hidden;
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.20);
}}
.header {{
    padding: 18px 24px 14px 24px;
}}
.channel-title {{
    color: #8f327e;
    font-size: 28px;
    font-weight: 700;
    letter-spacing: -0.2px;
}}
.forward-info {{
    color: #8e8e93;
    font-size: 21px;
    margin-top: 4px;
}}
.forward-channel {{
    color: #c67d32;
    font-weight: 600;
}}
.collage {{
    width: 820px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    background: #ffffff;
}}
.collage-row {{
    display: flex;
    gap: 2px;
    width: 100%;
}}
.dummy-photo {{
    display: flex;
    justify-content: center;
    align-items: center;
    color: #ffffff;
    font-weight: bold;
    font-size: 20px;
}}
.caption-box {{
    padding: 20px 24px 18px 24px;
}}
.caption-text {{
    font-size: 25px;
    line-height: 1.42;
    color: #000000;
    word-break: break-word;
}}
.hashtag {{
    color: #2481cc;
    font-weight: 500;
}}
.more-btn {{
    color: #2481cc;
    font-weight: 500;
}}
.footer {{
    display: flex;
    justify-content: flex-end;
    align-items: center;
    margin-top: 8px;
}}
.timestamp {{
    color: #8e8e93;
    font-size: 19px;
}}
.tail-svg {{
    position: absolute;
    bottom: 0px;
    left: -27px;
    z-index: 10;
}}
.avatar-badge {{
    position: absolute;
    bottom: -6px;
    left: -92px;
    width: 82px;
    height: 82px;
    border-radius: 50%;
    border: 3.5px solid #ffffff;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.20);
    z-index: 20;
    background: #ffffff;
    display: flex;
    justify-content: center;
    align-items: center;
    font-weight: bold;
    font-size: 30px;
    color: #8f327e;
}}
</style>
</head>
<body>
<div class="card-wrapper" id="card-wrapper">
    <div class="card" id="card">
        <div class="header">
            <div class="channel-title">Квартиры Юнусабада</div>
            <div class="forward-info">Переслано от <span class="forward-channel">1️⃣ 1_КОМНАТНАЯ</span></div>
        </div>
        <div class="collage">
            <div class="collage-row" style="height: 360px;">
                <div class="dummy-photo" style="flex: 1; height: 360px; background: #8e7c68;">ROOM 1</div>
                <div class="dummy-photo" style="flex: 1; height: 360px; background: #687e8e;">ROOM 2</div>
            </div>
            <div class="collage-row" style="height: 240px;">
                <div class="dummy-photo" style="flex: 1; height: 240px; background: #7a8e68;">ROOM 3</div>
                <div class="dummy-photo" style="flex: 1; height: 240px; background: #8e6888;">ROOM 4</div>
                <div class="dummy-photo" style="flex: 1; height: 240px; background: #688e8e;">ROOM 5</div>
            </div>
            <div class="collage-row" style="height: 240px;">
                <div class="dummy-photo" style="flex: 1; height: 240px; background: #8e8868;">ROOM 6</div>
                <div class="dummy-photo" style="flex: 1; height: 240px; background: #787878;">ROOM 7</div>
                <div class="dummy-photo" style="flex: 1; height: 240px; background: #8e6868;">ROOM 8</div>
            </div>
        </div>
        <div class="caption-box">
            <div class="caption-text">
                <span class="hashtag">#1_комнатная</span><br>
                🏡 Юнусабад 6 мавзе 1-2/4/4<br>
                📍 Мулжал : Канечка<br>
                📍 1 хона 2 хона килинган<br>
                📍 4-кават... <span class="more-btn">Подробнее</span>
            </div>
            <div class="footer">
                <span class="timestamp">16 сен, 21:26</span>
            </div>
        </div>
    </div>
    <svg class="tail-svg" width="28" height="24" viewBox="0 0 28 24">
        <path d="M 28 0 C 26 12 14 20 0 24 L 28 24 Z" fill="#ffffff"/>
    </svg>
    <div class="avatar-badge">🏛️</div>
</div>
</body>
</html>"""

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1080, "height": 1920})
    page.set_content(html_content, wait_until="load")
    page.screenshot(path=output_path, quality=95, type="jpeg")
    browser.close()

print("Saved test image successfully to:", output_path)
