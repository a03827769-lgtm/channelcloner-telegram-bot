import pytest
import asyncio
from datetime import datetime, timedelta, timezone
from database.models import Subscription, ChannelPair, ClonedMessage
from services.text_processor import TextProcessor
from services.dynamic_affiliate_engine import dynamic_affiliate_engine
from services.drip_feed_queue import drip_feed_service
from services.phone_utils import normalize_phone_number

@pytest.mark.asyncio
async def test_subscription_aware_datetime_parsing():
    """Verify offset-aware strings do not trigger TypeError when evaluated against naive UTC"""
    now = datetime.now(timezone.utc)
    future_aware = (now + timedelta(days=30)).isoformat()
    sub = Subscription(user_id=123, tier="pro", expires_at=future_aware)
    assert sub.is_active is True

    past_aware = (now - timedelta(days=5)).isoformat()
    sub_expired = Subscription(user_id=123, tier="pro", expires_at=past_aware)
    assert sub_expired.is_active is False

def test_word_replacement_with_backslashes_and_special_chars():
    """Verify regex escape errors do not happen with backslashes or escape patterns"""
    replace_dict = {
        "OldWord": "New\\1Value",
        "Path": "C:\\Windows\\System32",
        "Phone": "+998 90 123 45 67"
    }
    input_text = "Check OldWord and Path and Phone."
    result = TextProcessor.apply_word_replacements(input_text, replace_dict)
    assert "New\\1Value" in result
    assert "C:\\Windows\\System32" in result
    assert "+998 90 123 45 67" in result

def test_channel_pair_multiline_word_lists():
    """Verify blacklist and replacements parse correctly across multiple lines and commas"""
    pair = ChannelPair(
        id=1, user_id=10, source_channel="src", source_title="src",
        target_channel="tgt", target_title="tgt",
        blacklist_words="spam\nadvert,casino\r\nbet",
        replace_words="foo=bar\nhello=world,key=val"
    )
    assert "spam" in pair.blacklist_list
    assert "advert" in pair.blacklist_list
    assert "casino" in pair.blacklist_list
    assert "bet" in pair.blacklist_list

    assert pair.replace_dict.get("foo") == "bar"
    assert pair.replace_dict.get("hello") == "world"
    assert pair.replace_dict.get("key") == "val"

def test_dynamic_affiliate_url_validation():
    """Verify CTA keyboard builder ignores malformed URLs"""
    cta_items = [
        ("Valid Button", "https://uzum.uz/product/123"),
        ("Valid TG", "tg://resolve?domain=test"),
        ("Invalid Scheme", "javascript:alert(1)")
    ]
    keyboard = dynamic_affiliate_engine.build_cta_keyboard(cta_items)
    assert keyboard is not None
    button_urls = [btn.url for row in keyboard.inline_keyboard for btn in row]
    assert "https://uzum.uz/product/123" in button_urls
    assert "tg://resolve?domain=test" in button_urls
    assert "javascript:alert(1)" not in button_urls

def test_phone_normalization():
    """Verify various phone number formats normalize cleanly to E.164"""
    v1, p1, _ = normalize_phone_number("998901234567")
    assert v1 is True and p1 == "+998901234567"

    v2, p2, _ = normalize_phone_number("+998 90 123 45 67")
    assert v2 is True and p2 == "+998901234567"

    v3, p3, _ = normalize_phone_number("89012345678")
    assert v3 is True and p3 == "+79012345678"
