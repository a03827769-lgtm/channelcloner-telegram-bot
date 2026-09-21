import html
import re
import pytest

def build_caption_html(caption: str, price: float = None) -> str:
    if not caption:
        return "✨ Yangi e'lon"
    raw_lines = [l.strip() for l in caption.split('\n') if l.strip()]
    cleaned = []
    price_line = None
    for l in raw_lines:
        low = l.lower()
        if any(k in low for k in ['цена', 'нарх', 'narxi', 'стоимость', 'ijara']) and any(c.isdigit() for c in l):
            price_line = l
            continue
        if any(low.startswith(p) for p in ['тел', 'tel', 'aloqa', 'контакт', 'contact', 'админ', 'admin', 'http', 't.me']):
            continue
        cleaned.append(l)

    display = []
    for l in cleaned:
        if len(display) >= 4:
            break
        display.append(l)

    if price_line and price_line not in display:
        display.append(price_line)
    elif price:
        if not any('$' in l or 'usd' in l.lower() or 'нарх' in l.lower() or 'цена' in l.lower() for l in display):
            display.append(f"💰 Цена: ${price:g}")

    lines_html = []
    total_to_show = display[:5]
    for i, line in enumerate(total_to_show):
        escaped = html.escape(line)
        escaped = re.sub(r'(#[A-Za-z0-9_а-яА-ЯёЁ]+)', r'<span class="hashtag">\1</span>', escaped)
        if i == len(total_to_show) - 1 and len(raw_lines) > len(total_to_show):
            escaped += ' ... <span class="more-btn">Подробнее</span>'
        lines_html.append(escaped)

    return '<br>'.join(lines_html)


def test_build_caption_html():
    sample = """#1_комнатная

🏠Юнусабад 6 мавзе 1-2/4/4
📍 Мулжал : Канечка
📍 1 хона 2 хона килинган
📍 4-кават
💰 Цена: 500$
Тел: +998901234567"""

    res = build_caption_html(sample, 500.0)
    assert '<span class="hashtag">#1_комнатная</span>' in res
    assert '🏠Юнусабад 6 мавзе 1-2/4/4' in res
    assert '<span class="more-btn">Подробнее</span>' in res
    assert 'Тел' not in res
