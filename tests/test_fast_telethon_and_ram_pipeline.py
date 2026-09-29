import unittest
import io
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock
from PIL import Image

from services.fast_telethon import FastTelethonEngine, fast_telethon
from services.watermark_service import watermark_service
from services.media_handler import media_handler

class TestFastTelethonAndRamPipeline(unittest.IsolatedAsyncioTestCase):

    def test_fast_telethon_adaptive_workers(self):
        # Small files (<5MB) use 4 workers
        self.assertEqual(FastTelethonEngine.get_optimal_worker_count(2 * 1024 * 1024), 4)
        # 30MB uses 8 workers
        self.assertEqual(FastTelethonEngine.get_optimal_worker_count(30 * 1024 * 1024), 8)
        # 150MB uses 12 workers
        self.assertEqual(FastTelethonEngine.get_optimal_worker_count(150 * 1024 * 1024), 12)
        # 600MB uses 16 workers
        self.assertEqual(FastTelethonEngine.get_optimal_worker_count(600 * 1024 * 1024), 16)

    def test_watermark_service_apply_text_watermark_bytes(self):
        # Create a simple RGB test image in RAM
        img = Image.new("RGB", (400, 300), color=(100, 150, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        # Apply in-memory watermark
        wm_bytes = watermark_service.apply_text_watermark_bytes(
            raw_bytes,
            watermark_text="@TestChannel",
            pos="bottom_right"
        )
        self.assertIsNotNone(wm_bytes)
        self.assertIsInstance(wm_bytes, bytes)
        self.assertGreater(len(wm_bytes), 0)

        # Verify output is a valid readable image
        out_img = Image.open(io.BytesIO(wm_bytes))
        self.assertEqual(out_img.size, (400, 300))

    async def test_download_telethon_media_bytes_success(self):
        mock_msg = MagicMock()
        mock_msg.media = MagicMock()
        mock_msg.client = MagicMock()
        mock_msg.file = MagicMock()
        mock_msg.file.size = 1024 * 50  # 50 KB

        test_data = b"Simulated image binary payload"
        async def fake_download(file):
            if isinstance(file, io.BytesIO):
                file.write(test_data)
                return file
            return test_data

        mock_msg.download_media = AsyncMock(side_effect=fake_download)

        result_bytes = await media_handler.download_telethon_media_bytes(mock_msg, max_size_bytes=1024 * 1024)
        self.assertEqual(result_bytes, test_data)

    async def test_download_telethon_media_bytes_exceeds_ram_limit(self):
        mock_msg = MagicMock()
        mock_msg.media = MagicMock()
        mock_msg.file = MagicMock()
        mock_msg.file.size = 50 * 1024 * 1024  # 50 MB, exceeds 20MB limit

        result = await media_handler.download_telethon_media_bytes(mock_msg, max_size_bytes=20 * 1024 * 1024)
        self.assertIsNone(result)

    async def test_fast_telethon_upload_parallel_mock(self):
        # Create a small temp file to test upload_file_parallel
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
            f.write(b"0" * (1024 * 1024))  # 1MB file
            temp_path = f.name

        try:
            mock_client = MagicMock()
            mock_client.upload_file = AsyncMock(return_value="mock_input_file")
            uploaded = await fast_telethon.upload_file_parallel(mock_client, temp_path, max_workers=2)
            self.assertEqual(uploaded, "mock_input_file")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

if __name__ == "__main__":
    unittest.main()
