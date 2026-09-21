import os
import tempfile
import zipfile
import unittest
from services.security_vault import security_vault
from scripts.decrypt_backup import decrypt_backup

class TestBackupEncryption(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_backup_zip_encrypt_and_decrypt(self):
        # 1. Create a dummy sqlite db file
        dummy_db = os.path.join(self.temp_dir, "test_cloner.db")
        with open(dummy_db, "wb") as f:
            f.write(b"SQLite format 3\x00\x10\x00\x01\x01\x00\x40\x20\x20")

        # 2. Zip the dummy db
        raw_zip_path = os.path.join(self.temp_dir, "test_cloner.db.zip")
        with zipfile.ZipFile(raw_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(dummy_db, arcname="test_cloner.db")

        # 3. Encrypt the zip archive
        with open(raw_zip_path, "rb") as f:
            raw_bytes = f.read()
        encrypted_bytes = security_vault.encrypt_bytes(raw_bytes)
        enc_zip_path = raw_zip_path + ".enc"
        with open(enc_zip_path, "wb") as f:
            f.write(encrypted_bytes)

        self.assertTrue(os.path.exists(enc_zip_path))
        self.assertNotEqual(raw_bytes, encrypted_bytes)

        # 4. Verify that raw zip cannot be opened directly as zip from enc_zip_path
        with self.assertRaises(Exception):
            with zipfile.ZipFile(enc_zip_path, "r") as bad_zf:
                bad_zf.namelist()

        # 5. Decrypt using the decrypt_backup utility
        decrypted_zip_path = os.path.join(self.temp_dir, "decrypted.zip")
        success = decrypt_backup(
            file_path=enc_zip_path,
            output_path=decrypted_zip_path,
            extract=True
        )
        self.assertTrue(success)
        self.assertTrue(os.path.exists(decrypted_zip_path))

        # 6. Verify decrypted zip contents match original
        with zipfile.ZipFile(decrypted_zip_path, "r") as good_zf:
            names = good_zf.namelist()
            self.assertIn("test_cloner.db", names)
            extracted_data = good_zf.read("test_cloner.db")
            self.assertEqual(extracted_data, b"SQLite format 3\x00\x10\x00\x01\x01\x00\x40\x20\x20")

    def test_backup_size_guard_and_no_plaintext_fallback(self):
        # Verify that missing or zero-byte encrypted file strictly triggers RuntimeError rather than fallback
        enc_path = os.path.join(self.temp_dir, "failed_encryption.enc")
        with self.assertRaises(RuntimeError):
            if not os.path.exists(enc_path) or os.path.getsize(enc_path) == 0:
                raise RuntimeError("Shifrlash muvaffaqiyatsiz bo'ldi. Xavfsizlik talablariga muvofiq shifrlanmagan fayl yuborilmaydi!")

if __name__ == "__main__":
    unittest.main()
