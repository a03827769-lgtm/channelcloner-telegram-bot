import os
import sys
import glob
import re
import time
from PIL import Image, ImageDraw, ImageFont, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

from services.story_renderer import story_card_renderer

font_dir = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts')
font_title = ImageFont.truetype(os.path.join(font_dir, 'segoeuib.ttf'), 34)
font_sub = ImageFont.truetype(os.path.join(font_dir, 'segoeui.ttf'), 22)
font_text = ImageFont.truetype(os.path.join(font_dir, 'segoeui.ttf'), 26)
font_text_bold = ImageFont.truetype(os.path.join(font_dir, 'segoeuib.ttf'), 26)
font_time = ImageFont.truetype(os.path.join(font_dir, 'segoeui.ttf'), 22)
font_emoji = ImageFont.truetype(os.path.join(font_dir, 'seguiemj.ttf'), 24)

TOKEN_PATTERN = re.compile(
    r'(#[A-Za-z0-9_а-яА-ЯёЁ]+|[\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f]+|[^\s#\U00010000-\U0010ffff\u2600-\u27bf\u2b50\u231a-\u23f3\u25aa-\u25fe\u200d\ufe0f]+|\s+)'
)

def draw_styled_line(draw, x, y, line_str, max_w=840):
    cur_x = x
    tokens = TOKEN_PATTERN.findall(line_str)
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith('#'):
            draw.text((cur_x, y), tok, font=font_text_bold, fill=(36, 129, 204))
            cur_x += font_text_bold.getlength(tok)
        elif any(ord(c) > 0x2300 for c in tok) and not any(0x0400 <= ord(c) <= 0x04ff for c in tok):
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
    bg_path = 'assets/story_backgrounds/telegram_green.jpg'
    bg = Image.open(bg_path).convert('RGB')
    target_w, target_h = 1080, 1920

    photo_paths = sorted(glob.glob('temp_test_album/photo_*.jpg'))
    avatar_path = 'temp_test_album/channel_avatar.jpg' if os.path.exists('temp_test_album/channel_avatar.jpg') else None

    card_w = 920
    collage_h = 860
    collage = story_card_renderer.create_photo_collage(photo_paths, width=card_w, height=collage_h)

    raw_msg = '''✨✨✨ СДАЁТСЯ ✨✨✨

⚜️  Район: Шайхантахур
🏬 Адрес: Ц-15
📍 Ориентир: Метро Гафур Гулям

Информация:
• Тип: Аренда, Жилой
• Этажность: 8
• Этаж: 3
• Комнат: 2
• Площадь: 70 m²
• Ремонт: Евро ремонт
• Кондиционер: да
• Телевизор: да
• Стиральная машина: да

Способы оплаты:
💸 Предоплата: нет
💳 Депозит: да
💰 Цена: $800.00 

🗓 Время освобождения: 17.9.2026'''

    filtered_lines = []
    price_line = None
    for l in raw_msg.split('\n'):
        cl = re.sub(r'[ \t]+', ' ', l).strip()
        if not cl:
            continue
        if 'цена' in cl.lower() or 'нарх' in cl.lower():
            price_line = cl
        elif len(filtered_lines) < 4 and not cl.startswith('•') and not cl.startswith('Способы') and not cl.startswith('Информация'):
            filtered_lines.append(cl)

    if price_line and price_line not in filtered_lines:
        filtered_lines.append(price_line)

    print('Lines count:', len(filtered_lines))

    header_h = 68
    line_h = 38
    caption_padding_top = 18
    caption_padding_bottom = 26
    caption_h = caption_padding_top + (len(filtered_lines) * line_h) + caption_padding_bottom
    card_h = header_h + collage_h + caption_h

    card_left = (target_w - card_w) // 2
    card_top = (target_h - card_h) // 2 - 20

    card_img = Image.new('RGBA', (card_w, card_h), (0, 0, 0, 0))
    card_draw = ImageDraw.Draw(card_img)
    card_r = 32
    card_draw.rounded_rectangle([0, 0, card_w, card_h], radius=card_r, fill=(255, 255, 255, 255))

    card_draw.text((36, 18), 'ARENDA UY', font=font_title, fill=(120, 50, 115))
    card_img.paste(collage, (0, header_h))

    y_text = header_h + collage_h + caption_padding_top
    for idx, line in enumerate(filtered_lines):
        is_last = (idx == len(filtered_lines) - 1)
        if is_last:
            cur_x = draw_styled_line(card_draw, 36, y_text, line, max_w=card_w - 240)
            card_draw.text((cur_x + 6, y_text), '... ', font=font_text, fill=(28, 28, 30))
            card_draw.text((cur_x + 36, y_text), 'Подробнее', font=font_text_bold, fill=(36, 129, 204))
        else:
            draw_styled_line(card_draw, 36, y_text, line, max_w=card_w - 72)
        y_text += line_h

    date_text = '17 сен, 09:56'
    card_draw.text((card_w - 170, card_h - 36), date_text, font=font_time, fill=(142, 142, 147))

    shadow = Image.new('RGBA', (card_w + 40, card_h + 40), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([20, 20, card_w + 20, card_h + 20], radius=card_r, fill=(0, 0, 0, 60))
    shadow = shadow.filter(ImageFilter.GaussianBlur(16))

    bg_rgba = bg.convert('RGBA')
    bg_rgba.paste(shadow, (card_left - 20, card_top - 10), shadow)

    card_mask = Image.new('L', (card_w, card_h), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle([0, 0, card_w, card_h], radius=card_r, fill=255)
    bg_rgba.paste(card_img, (card_left, card_top), card_mask)

    tail_poly = [
        (card_left + 16, card_top + card_h - 28),
        (card_left - 10, card_top + card_h - 6),
        (card_left + 42, card_top + card_h)
    ]
    ImageDraw.Draw(bg_rgba).polygon(tail_poly, fill=(255, 255, 255, 255))

    if avatar_path:
        av = Image.open(avatar_path).convert('RGBA').resize((76, 76), Image.Resampling.LANCZOS)
        mask = Image.new('L', (76, 76), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 76, 76), fill=255)
        av_bordered = Image.new('RGBA', (84, 84), (0, 0, 0, 0))
        ImageDraw.Draw(av_bordered).ellipse((0, 0, 84, 84), fill=(255, 255, 255, 255))
        av_bordered.paste(av, (4, 4), mask)
        bg_rgba.paste(av_bordered, (card_left - 24, card_top + card_h - 52), av_bordered)

    out_res = bg_rgba.convert('RGB')
    out_path = 'temp_test_album/perfect_render_preview.jpg'
    out_res.save(out_path, quality=95)
    print('Generated:', out_path)

if __name__ == '__main__':
    main()
