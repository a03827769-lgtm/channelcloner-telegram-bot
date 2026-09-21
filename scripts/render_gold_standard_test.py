import os
import sys
import glob
import re
import time
from PIL import Image, ImageDraw, ImageFont, ImageFilter

font_dir = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts')
font_title = ImageFont.truetype(os.path.join(font_dir, 'segoeuib.ttf'), 30)
font_sub = ImageFont.truetype(os.path.join(font_dir, 'segoeui.ttf'), 20)
font_text = ImageFont.truetype(os.path.join(font_dir, 'segoeui.ttf'), 23)
font_text_bold = ImageFont.truetype(os.path.join(font_dir, 'segoeuib.ttf'), 23)
font_time = ImageFont.truetype(os.path.join(font_dir, 'segoeui.ttf'), 19)
font_emoji = ImageFont.truetype(os.path.join(font_dir, 'seguiemj.ttf'), 21)

EMOJI_REGEX = re.compile(r'([\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f]+)')

def is_emoji_char(c):
    o = ord(c)
    return (0x1F000 <= o <= 0x1FFFF) or (0x2600 <= o <= 0x27BF) or (0x2B50 <= o <= 0x2B55) or (0x231A <= o <= 0x23F3)

def fit_and_crop(img, target_w, target_h):
    sw, sh = img.size
    scale = max(target_w / sw, target_h / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    res = img.resize((nw, nh), Image.Resampling.LANCZOS)
    cx, cy = (nw - target_w) // 2, (nh - target_h) // 2
    return res.crop((cx, cy, cx + target_w, cy + target_h))

def create_collage(photo_paths, width=820, height=760):
    collage = Image.new('RGB', (width, height), (240, 240, 242))
    imgs = [Image.open(p).convert('RGB') for p in photo_paths if os.path.exists(p)]
    n = len(imgs)
    gap = 4
    if n == 0:
        return collage
    if n == 1:
        return fit_and_crop(imgs[0], width, height)
    if n == 2:
        cw = (width - gap) // 2
        collage.paste(fit_and_crop(imgs[0], cw, height), (0, 0))
        collage.paste(fit_and_crop(imgs[1], cw, height), (cw + gap, 0))
        return collage
    if n == 3:
        h1 = int(height * 0.55)
        h2 = height - h1 - gap
        cw = (width - gap) // 2
        collage.paste(fit_and_crop(imgs[0], width, h1), (0, 0))
        collage.paste(fit_and_crop(imgs[1], cw, h2), (0, h1 + gap))
        collage.paste(fit_and_crop(imgs[2], cw, h2), (cw + gap, h1 + gap))
        return collage
    if n == 4:
        cw = (width - gap) // 2
        rh = (height - gap) // 2
        collage.paste(fit_and_crop(imgs[0], cw, rh), (0, 0))
        collage.paste(fit_and_crop(imgs[1], cw, rh), (cw + gap, 0))
        collage.paste(fit_and_crop(imgs[2], cw, rh), (0, rh + gap))
        collage.paste(fit_and_crop(imgs[3], cw, rh), (cw + gap, rh + gap))
        return collage
    if n in (5, 6):
        h1 = (height - gap) // 2
        h2 = height - h1 - gap
        if n == 6:
            cw = (width - 2 * gap) // 3
            for i in range(3):
                collage.paste(fit_and_crop(imgs[i], cw, h1), (i * (cw + gap), 0))
                collage.paste(fit_and_crop(imgs[3 + i], cw, h2), (i * (cw + gap), h1 + gap))
        else:
            cw_top = (width - gap) // 2
            cw_bot = (width - 2 * gap) // 3
            collage.paste(fit_and_crop(imgs[0], cw_top, h1), (0, 0))
            collage.paste(fit_and_crop(imgs[1], cw_top, h1), (cw_top + gap, 0))
            for i in range(3):
                collage.paste(fit_and_crop(imgs[2 + i], cw_bot, h2), (i * (cw_bot + gap), h1 + gap))
        return collage
    # 7 or 8 photos (exact layout from user's gold reference!)
    h1 = int(height * 0.38)
    h2 = (height - h1 - 2 * gap) // 2
    h3 = height - h1 - h2 - 2 * gap
    cw2 = (width - gap) // 2
    cw3 = (width - 2 * gap) // 3
    collage.paste(fit_and_crop(imgs[0], cw2, h1), (0, 0))
    collage.paste(fit_and_crop(imgs[1], cw2, h1), (cw2 + gap, 0))
    collage.paste(fit_and_crop(imgs[2], cw3, h2), (0, h1 + gap))
    collage.paste(fit_and_crop(imgs[3], cw3, h2), (cw3 + gap, h1 + gap))
    collage.paste(fit_and_crop(imgs[4], cw3, h2), (2 * (cw3 + gap), h1 + gap))
    rem = min(n - 5, 3)
    cw_rem = (width - (rem - 1) * gap) // rem
    y3 = h1 + gap + h2 + gap
    for i in range(rem):
        collage.paste(fit_and_crop(imgs[5 + i], cw_rem, h3), (i * (cw_rem + gap), y3))
    return collage

def draw_styled_line(draw, x, y, line_str, max_w=760):
    cur_x = x
    # Parse tokens: hashtags, urls, emojis, normal text
    tokens = re.findall(r'(#[A-Za-z0-9_а-яА-ЯёЁ]+|https?://\S+|@\w+|[\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f]+|[^\s#@\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f]+|\s+)', line_str)
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith('#') or tok.startswith('@') or tok.startswith('http'):
            # Telegram Blue link
            draw.text((cur_x, y), tok, font=font_text_bold, fill=(36, 129, 204))
            cur_x += font_text_bold.getlength(tok)
        elif any(is_emoji_char(c) for c in tok):
            # Native color emoji
            try:
                draw.text((cur_x, y - 2), tok, font=font_emoji, embedded_color=True)
                cur_x += font_emoji.getlength(tok)
            except Exception:
                draw.text((cur_x, y), tok, font=font_text, fill=(28, 28, 30))
                cur_x += font_text.getlength(tok)
        else:
            draw.text((cur_x, y), tok, font=font_text, fill=(28, 28, 30))
            cur_x += font_text.getlength(tok)
        if cur_x >= x + max_w:
            break
    return cur_x

def main():
    target_w, target_h = 1080, 1920
    bg_path = 'assets/story_backgrounds/telegram_green.jpg'
    bg = Image.open(bg_path).convert('RGB')
    if bg.size != (target_w, target_h):
        bg = bg.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # 1. Dimensions
    card_w = 820
    card_top = 220
    card_left = (target_w - card_w) // 2  # 130px
    card_r = 28

    header_h = 64
    collage_h = 760

    # Photos
    photo_paths = sorted(glob.glob('temp_test_5719/photo_*.jpg'))
    collage = create_collage(photo_paths, width=card_w, height=collage_h)

    # Clean text lines from real post 5719
    raw_caption = """✨✨✨ СДАЁТСЯ ✨✨✨

⚜️  Район: Шайхантахур
🏬 Адрес: Себзор
📍 Ориентир: Ат Термизий

Информация:
• Тип: Аренда, Жилой
• Этажность: 5
• Этаж: 1
• Комнат: 2
• Площадь: 60 m²
• Ремонт: Евро ремонт
• Кондиционер: да
• Телевизор: да
• Стиральная машина: да

Способы оплаты:
💸 Предоплата: нет
💳 Депозит: да
💰 Цена: $800.00 

🗓 Время освобождения: 16.9.2026

📢 Имеются альтернативные варианты по всему городу.

🆔: 21568

Aloqa: +998955055516
Telegram: @realtorAbdulloh"""

    # Extract clean lines in exact original sequence
    clean_lines = []
    for l in raw_caption.split('\n'):
        cl = re.sub(r'[\u200b-\u200f\ufeff]', '', l).strip()
        if cl:
            clean_lines.append(cl)

    # Display first 5 lines (fits perfectly without overflowing)
    display_lines = clean_lines[:5]
    line_h = 35
    pad_top = 16
    pad_bottom = 32
    caption_h = pad_top + (len(display_lines) * line_h) + pad_bottom
    card_h = header_h + collage_h + caption_h

    # Card Image
    card_img = Image.new('RGBA', (card_w, card_h), (0, 0, 0, 0))
    card_draw = ImageDraw.Draw(card_img)
    card_draw.rounded_rectangle([0, 0, card_w, card_h], radius=card_r, fill=(255, 255, 255, 255))

    # Header: Channel Title
    card_draw.text((32, 16), 'ARENDA UY', font=font_title, fill=(138, 61, 128))

    # Paste Collage flush with card edges
    card_img.paste(collage, (0, header_h))

    # Draw Caption Lines
    y_text = header_h + collage_h + pad_top
    for idx, line in enumerate(display_lines):
        is_last = (idx == len(display_lines) - 1)
        if is_last and len(clean_lines) > len(display_lines):
            cur_x = draw_styled_line(card_draw, 32, y_text, line, max_w=card_w - 200)
            card_draw.text((cur_x + 4, y_text), '... ', font=font_text, fill=(28, 28, 30))
            card_draw.text((cur_x + 30, y_text), 'Подробнее', font=font_text_bold, fill=(36, 129, 204))
        else:
            draw_styled_line(card_draw, 32, y_text, line, max_w=card_w - 64)
        y_text += line_h

    # Timestamp in bottom right
    d_text = '17 сен, 11:58'
    card_draw.text((card_w - 145, card_h - 28), d_text, font=font_time, fill=(142, 142, 147))

    # Drop Shadow
    shadow = Image.new('RGBA', (card_w + 40, card_h + 40), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([20, 20, card_w + 20, card_h + 20], radius=card_r, fill=(0, 0, 0, 45))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))

    # Composite on background
    bg_rgba = bg.convert('RGBA')
    bg_rgba.paste(shadow, (card_left - 20, card_top - 12), shadow)

    card_mask = Image.new('L', (card_w, card_h), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle([0, 0, card_w, card_h], radius=card_r, fill=255)
    bg_rgba.paste(card_img, (card_left, card_top), card_mask)

    # Speech Bubble Tail on bottom-left pointing towards avatar
    tail_poly = [
        (card_left + 16, card_top + card_h),
        (card_left - 14, card_top + card_h + 1),
        (card_left, card_top + card_h - 22)
    ]
    ImageDraw.Draw(bg_rgba).polygon(tail_poly, fill=(255, 255, 255, 255))

    # Circular Avatar Badge in left margin
    av_path = 'temp_test_5719/avatar.jpg'
    if os.path.exists(av_path):
        av_dia = 68
        av = Image.open(av_path).convert('RGBA').resize((av_dia, av_dia), Image.Resampling.LANCZOS)
        mask = Image.new('L', (av_dia, av_dia), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, av_dia, av_dia), fill=255)

        # White border around avatar
        border_dia = av_dia + 6
        av_bordered = Image.new('RGBA', (border_dia, border_dia), (0, 0, 0, 0))
        ImageDraw.Draw(av_bordered).ellipse((0, 0, border_dia, border_dia), fill=(255, 255, 255, 255))
        av_bordered.paste(av, (3, 3), mask)

        # Position avatar in margin left of the tail
        av_x = card_left - 58
        av_y = card_top + card_h - 48
        bg_rgba.paste(av_bordered, (av_x, av_y), av_bordered)

    out_res = bg_rgba.convert('RGB')
    out_path = 'temp_test_5719/gold_standard_test.jpg'
    out_res.save(out_path, quality=95)
    print('Generated gold standard test image:', out_path)

    # Print coordinates
    center_y_pct = round((card_top + card_h / 2.0) / target_h * 100.0, 1)
    w_pct = round(card_w / target_w * 100.0, 1)
    h_pct = round(card_h / target_h * 100.0, 1)
    print(f'MTProto Coordinates: x=50.0, y={center_y_pct}, w={w_pct}, h={h_pct}')

if __name__ == '__main__':
    main()
