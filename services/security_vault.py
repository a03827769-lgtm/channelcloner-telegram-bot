import base64
import hashlib
import logging
from typing import Optional
from cryptography.fernet import Fernet
from config.settings import settings

logger = logging.getLogger(__name__)

class SecurityVault:
    def __init__(self):
        # Derive a stable 32-byte key from BOT_TOKEN + TELEGRAM_API_ID
        seed = f"{settings.BOT_TOKEN}:{settings.TELEGRAM_API_ID}:{settings.TELEGRAM_API_HASH}"
        key_bytes = hashlib.sha256(seed.encode("utf-8")).digest()
        self._fernet_key = base64.urlsafe_b64encode(key_bytes)
        self._cipher = Fernet(self._fernet_key)

    def encrypt_secret(self, raw_secret: str) -> str:
        """Encrypts sensitive session strings with AES-128-CBC / HMAC-SHA256"""
        if not raw_secret:
            return ""
        try:
            encrypted = self._cipher.encrypt(raw_secret.encode("utf-8"))
            return "enc:" + encrypted.decode("utf-8")
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            return raw_secret

    def decrypt_secret(self, encrypted_secret: str) -> str:
        """Decrypts sensitive session strings; transparently handles unencrypted legacy strings"""
        if not encrypted_secret:
            return ""
        if not encrypted_secret.startswith("enc:"):
            # Plaintext legacy string
            return encrypted_secret
        try:
            cipher_text = encrypted_secret[4:].encode("utf-8")
            decrypted = self._cipher.decrypt(cipher_text)
            return decrypted.decode("utf-8")
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            return ""

security_vault = SecurityVault()
