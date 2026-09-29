"""The story card caption markup built by the production renderer (StoryCardRenderer._build_caption_html)."""
from services.story_renderer import story_card_renderer


SAMPLE = """#1_комнатная

🏠Юнусабад 6 мавзе 1-2/4/4
📍 Мулжал : Канечка
📍 1 хона 2 хона килинган
📍 4-кават
💰 Цена: 500$
Тел: +998901234567"""


def test_build_caption_html():
    res = story_card_renderer._build_caption_html(SAMPLE, 500.0)
    assert '<span class="hashtag">#1_комнатная</span>' in res
    assert '🏠Юнусабад 6 мавзе 1-2/4/4' in res
    assert '<span class="more-btn">Подробнее</span>' in res
    # Contact lines never reach the card
    assert 'Тел' not in res and '+998901234567' not in res


def test_caption_html_escapes_user_text():
    res = story_card_renderer._build_caption_html("<script>alert(1)</script> & 2 xona", None)
    assert "<script>" not in res
    assert "&lt;script&gt;" in res and "&amp;" in res


def test_empty_caption_gets_a_placeholder():
    assert story_card_renderer._build_caption_html("", None)
