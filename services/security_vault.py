import os
import base64
import hashlib
import logging
from cryptography.fernet import Fernet
from config.settings import settings

logger = logging.getLogger(__name__)

class SecurityVault:
    def __init__(self):
        # Support independent master ENCRYPTION_KEY while maintaining backwards compatibility
        legacy_seed = f"{settings.BOT_TOKEN}:{settings.TELEGRAM_API_ID}:{settings.TELEGRAM_API_HASH}"
        legacy_key_bytes = hashlib.sha256(legacy_seed.encode("utf-8")).digest()
        self._legacy_cipher = Fernet(base64.urlsafe_b64encode(legacy_key_bytes))

        self._fallback_ciphers = []
        self._disk_cipher = None

        enc_key = (getattr(settings, "ENCRYPTION_KEY", "") or "").strip()
        vault_key_path = getattr(settings, "VAULT_KEY_PATH", "database/.vault_key")

        # Load disk key if available for fallback or primary use
        disk_key = ""
        if os.path.exists(vault_key_path):
            try:
                with open(vault_key_path, "r", encoding="utf-8") as f:
                    disk_key = f.read().strip()
            except Exception:
                logger.debug("Ignored exception", exc_info=True)

        if disk_key:
            disk_master_bytes = hashlib.sha256(disk_key.encode("utf-8")).digest()
            self._disk_cipher = Fernet(base64.urlsafe_b64encode(disk_master_bytes))

        if enc_key:
            master_key_bytes = hashlib.sha256(enc_key.encode("utf-8")).digest()
            self._fernet_key = base64.urlsafe_b64encode(master_key_bytes)
            self._cipher = Fernet(self._fernet_key)
            if self._disk_cipher and self._disk_cipher._signing_key != self._cipher._signing_key:
                self._fallback_ciphers.append(self._disk_cipher)
        else:
            if not disk_key:
                try:
                    os.makedirs(os.path.dirname(vault_key_path) or ".", exist_ok=True)
                    if os.path.exists(vault_key_path):
                        with open(vault_key_path, "r", encoding="utf-8") as f:
                            disk_key = f.read().strip()
                    if not disk_key:
                        generated_key = Fernet.generate_key().decode("utf-8")
                        try:
                            with open(vault_key_path, "x", encoding="utf-8") as f:
                                f.write(generated_key)
                            disk_key = generated_key
                        except FileExistsError:
                            with open(vault_key_path, "r", encoding="utf-8") as f:
                                disk_key = f.read().strip()
                        if hasattr(os, "chmod"):
                            try:
                                os.chmod(vault_key_path, 0o600)
                            except Exception:
                                logger.debug("Ignored exception", exc_info=True)
                except Exception:
                    logger.debug("Ignored exception", exc_info=True)

            if disk_key:
                master_key_bytes = hashlib.sha256(disk_key.encode("utf-8")).digest()
                self._fernet_key = base64.urlsafe_b64encode(master_key_bytes)
                self._cipher = Fernet(self._fernet_key)
                
                # Detect ephemeral cloud platforms where local disk keys might not persist
                is_cloud_ephemeral = bool(
                    os.getenv("KOYEB_APP_NAME")
                    or os.getenv("RENDER")
                    or os.getenv("DYNO")
                    or os.getenv("FLY_APP_NAME")
                    or os.getenv("KUBERNETES_SERVICE_HOST")
                )
                if is_cloud_ephemeral:
                    logger.critical(
                        "⚠️ CRITICAL SECURITY WARNING: Running on an ephemeral cloud platform without ENCRYPTION_KEY! "
                        "A container restart without persistent volume will generate a new key and permanently invalidate "
                        "all saved user MTProto sessions. Please configure ENCRYPTION_KEY in your cloud environment settings."
                    )
                else:
                    logger.warning(
                        "ENCRYPTION_KEY not set — using machine/disk-secured vault key. "
                        "Set ENCRYPTION_KEY in .env for explicit master key control."
                    )
            else:
                self._fernet_key = base64.urlsafe_b64encode(legacy_key_bytes)
                self._cipher = self._legacy_cipher

        # Always include legacy cipher as final fallback
        if self._legacy_cipher._signing_key != self._cipher._signing_key:
            if self._legacy_cipher not in self._fallback_ciphers:
                self._fallback_ciphers.append(self._legacy_cipher)

    def encrypt_bytes(self, data: bytes) -> bytes:
        """Encrypts arbitrary raw bytes using Fernet (AES-128-CBC + HMAC-SHA256)."""
        if not data:
            return b""
        return self._cipher.encrypt(data)

    def decrypt_bytes(self, encrypted_data: bytes) -> bytes:
        """Decrypts bytes, automatically falling back from primary cipher to fallback and legacy ciphers."""
        if not encrypted_data:
            return b""
        try:
            return self._cipher.decrypt(encrypted_data)
        except Exception as e:
            for fallback in self._fallback_ciphers:
                try:
                    return fallback.decrypt(encrypted_data)
                except Exception:
                    continue
            logger.error(f"Byte decryption failed with all available ciphers: {e}")
            raise ValueError("Decryption failed: invalid key or corrupted ciphertext")

    def encrypt_secret(self, raw_secret: str) -> str:
        """Encrypts sensitive session strings using Fernet (AES-128-CBC + HMAC-SHA256). Idempotent if already encrypted."""
        if not raw_secret:
            return ""
        # If already encrypted, verify if valid decryptable token
        if raw_secret.startswith("enc:"):
            clean_secret = raw_secret
            while clean_secret.startswith("enc:enc:"):
                clean_secret = "enc:" + clean_secret[8:]
            dec = self.decrypt_secret(clean_secret)
            if dec:
                return clean_secret
            # Decryption failed with all available ciphers. Do NOT re-encrypt ciphertext to prevent data corruption!
            logger.warning("Session string is already encrypted but could not be decrypted with current keys. Retaining ciphertext to prevent data loss.")
            return clean_secret
        try:
            encrypted = self._cipher.encrypt(raw_secret.encode("utf-8"))
            return "enc:" + encrypted.decode("utf-8")
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise RuntimeError(f"Cryptographic failure: unable to encrypt session safely ({e})")

    def decrypt_secret(self, encrypted_secret: str) -> str:
        """Decrypts sensitive session strings; transparently handles unencrypted legacy strings and key migrations"""
        if not encrypted_secret:
            return ""
        if not encrypted_secret.startswith("enc:"):
            # Plaintext legacy string
            return encrypted_secret
        while encrypted_secret.startswith("enc:enc:"):
            encrypted_secret = "enc:" + encrypted_secret[8:]
        cipher_text = encrypted_secret[4:].encode("utf-8")
        try:
            decrypted = self._cipher.decrypt(cipher_text)
            res = decrypted.decode("utf-8")
            if res.startswith("enc:"):
                return self.decrypt_secret(res)
            return res
        except Exception as e:
            for fallback in self._fallback_ciphers:
                try:
                    decrypted = fallback.decrypt(cipher_text)
                    res = decrypted.decode("utf-8")
                    if res.startswith("enc:"):
                        return self.decrypt_secret(res)
                    return res
                except Exception:
                    continue
            logger.error(f"Decryption failed with all available ciphers: {e}")
            return ""

security_vault = SecurityVault()
