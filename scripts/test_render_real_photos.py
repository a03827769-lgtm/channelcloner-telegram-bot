import os
import sys
import base64
from playwright.sync_api import sync_playwright

def to_base64(path):
    if not os.path.exists(path):
        return ""
    ext = os.path.splitext(path)[1].lower().replace(".", "")
    mime = "image/png" if ext == "png" else "image/jpeg"
    with open(path, "rb") as f:
        return f"data:{mime};base64,{base64.b64encode(f.read()).decode('utf-8')}"

bg_uri = to_base64("assets/story_backgrounds/telegram_green.jpg")
avatar_uri = to_base64("temp_media/test_photos_real/avatar.png")

p_uris = [
    to_base64("temp_media/test_photos_real/p1.jpg"),
    to_base64("temp_media/test_photos_real/p2.jpg"),
    to_base64("temp_media/test_photos_real/p3.jpg"),
    to_base64("temp_media/test_photos_real/p4.jpg"),
    to_base64("temp_media/test_photos_real/p5.jpg"),
    to_base64("temp_media/test_photos_real/p6.jpg"),
]

output_path = os.path.abspath("temp_media/perfect_story_real_photos.jpg")

html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    width: 1080px;
    height: 1920px;
    background: url("{bg_uri}") no-repeat center center;
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
    width: 860px;
    margin-left: 70px;
    filter: drop-shadow(0 16px 36px rgba(0, 0, 0, 0.22));
}}
.card {{
    background: #ffffff;
    border-radius: 24px;
    overflow: hidden;
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
.collage {{
    width: 860px;
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
.collage-img {{
    object-fit: cover;
    display: block;
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
    left: -96px;
    width: 84px;
    height: 84px;
    border-radius: 50%;
    border: 3.5px solid #ffffff;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18);
    z-index: 20;
    object-fit: cover;
    background: #ffffff;
}}
</style>
</head>
<body>
<div class="card-wrapper" id="card-wrapper">
    <div class="card" id="card">
        <div class="header">
            <div class="channel-title">ARENDA UY</div>
        </div>
        <div class="collage">
            <div class="collage-row" style="height: 380px;">
                <img class="collage-img" style="width: calc(50% - 1px); height: 380px;" src="{p_uris[0]}">
                <img class="collage-img" style="width: calc(50% - 1px); height: 380px;" src="{p_uris[1]}">
            </div>
            <div class="collage-row" style="height: 250px;">
                <img class="collage-img" style="width: calc(25% - 1.5px); height: 250px;" src="{p_uris[2]}">
                <img class="collage-img" style="width: calc(25% - 1.5px); height: 250px;" src="{p_uris[3]}">
                <img class="collage-img" style="width: calc(25% - 1.5px); height: 250px;" src="{p_uris[4]}">
                <img class="collage-img" style="width: calc(25% - 1.5px); height: 250px;" src="{p_uris[5]}">
            </div>
        </div>
        <div class="caption-box">
            <div class="caption-text">
                ✨✨✨ СДАЁТСЯ ✨✨✨<br>
                ⚜️ Район: Шайхантахур<br>
                🏬 Адрес: Себзор<br>
                📍 Ориентир: Ат Термизий<br>
                💰 Цена: $800 ... <span class="more-btn">Подробнее</span>
            </div>
            <div class="footer">
                <span class="timestamp">17 сен, 11:58</span>
            </div>
        </div>
    </div>
    <svg class="tail-svg" width="28" height="24" viewBox="0 0 28 24">
        <path d="M 28 0 C 26 12 14 20 0 24 L 28 24 Z" fill="#ffffff"/>
    </svg>
    <img class="avatar-badge" src="{avatar_uri}">
</div>
</body>
</html>"""

if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1080, "height": 1920})
        page.set_content(html_content, wait_until="load")
        page.screenshot(path=output_path, quality=95, type="jpeg")
        browser.close()

    print("Rendered successfully:", output_path)
