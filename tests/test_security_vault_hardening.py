import os
import unittest
from services.security_vault import SecurityVault

class TestSecurityVaultHardening(unittest.TestCase):
    def setUp(self):
        self.vault = SecurityVault()

    def test_encrypt_and_decrypt_bytes(self):
        sample_data = b"SQLite database snapshot binary payload test 12345"
        encrypted = self.vault.encrypt_bytes(sample_data)
        self.assertNotEqual(sample_data, encrypted)
        self.assertTrue(len(encrypted) > 0)

        decrypted = self.vault.decrypt_bytes(encrypted)
        self.assertEqual(sample_data, decrypted)

    def test_empty_bytes_handling(self):
        self.assertEqual(self.vault.encrypt_bytes(b""), b"")
        self.assertEqual(self.vault.decrypt_bytes(b""), b"")

    def test_string_secret_backwards_compatibility(self):
        secret = "1BQANAAABBBCCCDDDEEEFFF_sample_telethon_session_string"
        encrypted = self.vault.encrypt_secret(secret)
        self.assertTrue(encrypted.startswith("enc:"))
        
        # Idempotency check
        encrypted_again = self.vault.encrypt_secret(encrypted)
        self.assertEqual(encrypted, encrypted_again)

        # Decrypt check
        decrypted = self.vault.decrypt_secret(encrypted)
        self.assertEqual(secret, decrypted)

    def test_invalid_bytes_decryption_raises(self):
        with self.assertRaises(ValueError):
            self.vault.decrypt_bytes(b"invalid_corrupt_fernet_bytes_here")

if __name__ == "__main__":
    unittest.main()
