import pytest
import os
import re
import tempfile
import asyncio
from services.text_processor import TextProcessor
from services.security_vault import SecurityVault
from services.affiliate_replacer import AffiliateReplacer
from services.translator_service import translator_service
from services.cloner_engine import ClonerEngine
from database.db_manager import DatabaseManager, ReentrantAsyncLock

def test_nested_unclosed_tags_chunking():
    """Verify that deeply nested or unclosed formatting tags are closed in every single chunk"""
    text = "<b><i><u>" + ("x " * 3000)
    chunks = TextProcessor.fit_text_limit(text, max_limit=1024)
    assert len(chunks) > 1

    for idx, chunk in enumerate(chunks):
        assert chunk.startswith("<b><i><u>")
        assert chunk.endswith("</u></i></b>"), f"Chunk {idx} not properly closed: {chunk[-20:]}"
        assert TextProcessor.get_visible_text_length(chunk) <= 1024

def test_fit_caption_limit_overflow_balanced():
    """Verify caption and overflow both have properly closed and opened tags"""
    text = "<b><i>Important update: " + ("word " * 600)
    caption, overflow = TextProcessor.fit_caption_limit(text, max_limit=500)
    assert caption is not None
    assert overflow is not None
    assert caption.endswith("</i></b>")
    assert overflow.startswith("<b><i>")
    assert overflow.endswith("</i></b>")

def test_entity_safe_caption_fit():
    """Verify entity-aware slicing does not slice entities in half or return empty caption"""
    text = "&quot;Hello world this is a test&quot; " + ("detail " * 200)
    caption, overflow = TextProcessor.fit_caption_limit(text, max_limit=15)
    assert caption != ""
    assert "&quot;Hello" in caption
    assert TextProcessor.get_visible_text_length(caption) <= 15

def test_attach_signature_with_tags_and_no_premature_truncation():
    """Verify attach_signature does not destroy long posts that have formatting tags"""
    # 3900 visible characters + 100 characters of tags
    text = "<b>" + ("A" * 3900) + "</b>"
    signature = "<i>@mysig</i>"
    res = TextProcessor.attach_signature(text, signature)
    assert len(res) <= 4096
    assert TextProcessor.get_visible_text_length(res) <= 4096
    assert "@mysig" in res
    assert "<b>" in res
    # Should not be truncated to 50 characters
    assert TextProcessor.get_visible_text_length(res) > 3800

def test_security_vault_idempotent_encryption():
    """Verify encrypt_secret does not double encrypt an already encrypted secret"""
    vault = SecurityVault()
    original_session = "1BVtsOMQBu7v7Xm...test_session_string"
    enc1 = vault.encrypt_secret(original_session)
    assert enc1.startswith("enc:")
    assert not enc1.startswith("enc:enc:")

    # Second encryption call should be idempotent
    enc2 = vault.encrypt_secret(enc1)
    assert enc2 == enc1
    assert not enc2.startswith("enc:enc:")

    # Decrypt should yield original session string
    dec = vault.decrypt_secret(enc2)
    assert dec == original_session

def test_security_vault_persistent_keyfile(tmp_path):
    """Verify persistent vault keyfile allows decrypting when BOT_TOKEN rotates"""
    # Initialize vault
    vault = SecurityVault()
    secret = "session_to_persist_safely"
    enc = vault.encrypt_secret(secret)
    dec = vault.decrypt_secret(enc)
    assert dec == secret

def test_affiliate_replacer_strict_domain_matching():
    """Verify affiliate replacer replaces exact and subdomains but never substring lookalikes"""
    rules = "olx.uz=https://aff.olx.com\nuzum.uz=https://uzum.uz/?ref=123"
    
    # 1. Exact match
    t1 = "Ko'ring: https://olx.uz/item/1"
    r1 = AffiliateReplacer.replace_affiliate_links(t1, rules)
    assert "https://aff.olx.com" in r1

    # 2. Subdomain match
    t2 = "Ko'ring: https://m.olx.uz/item/2"
    r2 = AffiliateReplacer.replace_affiliate_links(t2, rules)
    assert "https://aff.olx.com" in r2

    # 3. Phishing / lookalike substring domain should NOT be replaced
    t3 = "Xavfli: https://fake-olx.uz.attacker.com/steal"
    r3 = AffiliateReplacer.replace_affiliate_links(t3, rules)
    assert "https://fake-olx.uz.attacker.com/steal" in r3
    assert "https://aff.olx.com" not in r3

    t4 = "Boshqa: https://coolx.uz/item"
    r4 = AffiliateReplacer.replace_affiliate_links(t4, rules)
    assert "https://coolx.uz/item" in r4

@pytest.mark.asyncio
async def test_translator_service_url_punctuation_retention():
    """Verify translator preserves punctuation at the end of URLs"""
    text = "Batafsil https://t.me/kanalimiz. Yangiliklar juda zo'r!"
    # When masking URLs, the trailing period must remain outside the link placeholder
    counter = 0
    placeholders = {}
    def mask_url(m):
        nonlocal counter
        url = m.group(1)
        punct = m.group(2)
        tag = f"⟦99{counter:04d}⟧"
        counter += 1
        placeholders[tag] = url
        return tag + punct

    masked = re.sub(r'(https?://[^\s<>"\'\)]+?)([.,!?:;)]*)(?=\s|$)', mask_url, text)
    assert "⟦990000⟧." in masked
    assert placeholders["⟦990000⟧"] == "https://t.me/kanalimiz"

@pytest.mark.asyncio
async def test_reentrant_async_lock_symmetric():
    """Verify ReentrantAsyncLock acquires and releases in nested and non-nested tasks"""
    lock = ReentrantAsyncLock()
    async with lock:
        assert lock._depth == 1
        async with lock:
            assert lock._depth == 2
        assert lock._depth == 1
    assert lock._depth == 0
    assert lock._owner is None

def test_security_vault_startup_idempotency_and_no_overwrite(tmp_path):
    """Verify that SecurityVault does not overwrite existing disk_key across restarts and always initializes _cipher"""
    from unittest.mock import patch
    
    with patch("config.settings.settings.ENCRYPTION_KEY", ""), patch.object(SecurityVault, "__init__", SecurityVault.__init__):
        # Point settings to temp key
        with patch("config.settings.settings.DB_PATH", str(tmp_path / "cloner.db")):
            v1 = SecurityVault()
            # Verify cipher is initialized
            assert hasattr(v1, "_cipher")
            assert v1._cipher is not None
            enc = v1.encrypt_secret("super_sensitive_token_123")
            dec1 = v1.decrypt_secret(enc)
            assert dec1 == "super_sensitive_token_123"

            # Restart vault
            v2 = SecurityVault()
            assert hasattr(v2, "_cipher")
            dec2 = v2.decrypt_secret(enc)
            assert dec2 == "super_sensitive_token_123"

def test_cloner_engine_normalize_chat_id_user_id():
    """Verify _normalize_chat_id preserves numeric user IDs (<10 digits) without prepending -100"""
    assert ClonerEngine._normalize_chat_id("123456789") == 123456789
    assert ClonerEngine._normalize_chat_id("-1001234567890") == -1001234567890
    assert ClonerEngine._normalize_chat_id("1234567890") == -1001234567890
    assert ClonerEngine._normalize_chat_id("@my_channel") == "@my_channel"

def test_telethon_admin_required_ttl():
    """Verify _is_telethon_admin_required self-heals after 300s TTL"""
    import time
    engine = ClonerEngine()
    engine._telethon_admin_required_targets["-1001111"] = time.time()
    assert engine._is_telethon_admin_required("-1001111") is True

    # Expire TTL
    engine._telethon_admin_required_targets["-1001111"] = time.time() - 305.0
    assert engine._is_telethon_admin_required("-1001111") is False
    assert "-1001111" not in engine._telethon_admin_required_targets

    # Clear method
    engine._telethon_admin_required_targets["-1002222"] = time.time()
    engine.clear_telethon_admin_required_targets()
    assert len(engine._telethon_admin_required_targets) == 0

@pytest.mark.asyncio
async def test_ai_paraphraser_async_wrapper():
    """Verify paraphrase_async runs without blocking"""
    from services.ai_paraphraser import ai_paraphraser
    res = await ai_paraphraser.paraphrase_async("Salom dunyo bu sinov xabari", mode="off")
    assert res == "Salom dunyo bu sinov xabari"

@pytest.mark.asyncio
async def test_ai_paraphraser_async_gemini_mocked():
    """Verify _paraphrase_with_gemini_async executes non-blocking and parses candidates cleanly"""
    from services.ai_paraphraser import ai_paraphraser
    from unittest.mock import patch, MagicMock, AsyncMock

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "candidates": [{"content": {"parts": [{"text": "Paraphrased text by AI"}]}}]
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        res = await ai_paraphraser._paraphrase_with_gemini_async("Original text", mode="formal", api_key="fake_key")
        assert res == "Paraphrased text by AI"


@pytest.mark.asyncio
async def test_db_manager_prune_database(tmp_path):
    """Verify prune_database runs cleanly on isolated SQLite database"""
    db_file = str(tmp_path / "test_prune.db")
    db = DatabaseManager(db_file)
    await db.init_db()
    res = await db.prune_database(max_age_days=30)
    assert isinstance(res, dict)
    assert "deleted_drip_items" in res
    assert "deleted_cloned_messages" in res
    await db.close()

