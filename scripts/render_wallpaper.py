import os
import re
from playwright.sync_api import sync_playwright

with open("assets/story_backgrounds/telegram_pattern.svg", "r", encoding="utf-8") as f:
    svg_data = f.read()

# Replace .st0{fill:none;} with fill
svg_data = re.sub(r'\.st0\{[^}]*\}', '.st0{fill: #ffffff; fill-opacity: 0.16;}', svg_data)
svg_data = svg_data.replace('<path ', '<path fill="#ffffff" fill-opacity="0.16" ')

with open("assets/story_backgrounds/telegram_pattern_fixed.svg", "w", encoding="utf-8") as f:
    f.write(svg_data)

html_content = """<!DOCTYPE html>
<html>
<head>
<style>
  body, html {
    margin: 0;
    padding: 0;
    width: 1080px;
    height: 1920px;
    overflow: hidden;
    background: #80af6f;
  }
  .bg {
    position: absolute;
    top: 0;
    left: 0;
    width: 1080px;
    height: 1920px;
    background: radial-gradient(circle at 50% 30%, #a4cd93 0%, #7fab6e 65%, #669654 100%);
  }
  .pattern {
    position: absolute;
    top: 0;
    left: 0;
    width: 1080px;
    height: 1920px;
    background-image: url('telegram_pattern_fixed.svg');
    background-size: 540px 1169px;
    background-repeat: repeat;
  }
</style>
</head>
<body>
  <div class="bg"></div>
  <div class="pattern"></div>
</body>
</html>"""

out_html = os.path.abspath("assets/story_backgrounds/render_bg.html")
with open(out_html, "w", encoding="utf-8") as f:
    f.write(html_content)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1080, "height": 1920})
    file_url = "file:///" + out_html.replace("\\", "/")
    page.goto(file_url)
    page.wait_for_timeout(500)
    out_jpg = os.path.abspath("assets/story_backgrounds/telegram_green.jpg")
    page.screenshot(path=out_jpg, type="jpeg", quality=95)
    browser.close()

if os.path.exists(out_html):
    os.remove(out_html)

print("Rendered telegram_green.jpg successfully!")
