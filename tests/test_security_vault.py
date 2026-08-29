import unittest
from services.security_vault import security_vault

class TestSecurityVault(unittest.TestCase):
    def test_encrypt_and_decrypt(self):
        original = "1BVtsOMQBu89X2...sample_telethon_session_string..."
        encrypted = security_vault.encrypt_secret(original)
        self.assertTrue(encrypted.startswith("enc:"))
        self.assertNotEqual(encrypted, original)

        decrypted = security_vault.decrypt_secret(encrypted)
        self.assertEqual(decrypted, original)

    def test_legacy_plaintext_passthrough(self):
        legacy = "legacy_unencrypted_session_123"
        decrypted = security_vault.decrypt_secret(legacy)
        self.assertEqual(decrypted, legacy)

if __name__ == "__main__":
    unittest.main()
