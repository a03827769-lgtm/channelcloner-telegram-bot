import unittest
import os
from PIL import Image
from services.watermark_service import watermark_service

class TestWatermarkService(unittest.TestCase):
    def setUp(self):
        self.test_img_path = "test_image.jpg"
        # Create a simple test image
        img = Image.new("RGB", (400, 300), color=(100, 150, 200))
        img.save(self.test_img_path)

    def tearDown(self):
        if os.path.exists(self.test_img_path):
            os.remove(self.test_img_path)

    def test_apply_text_watermark(self):
        out_path = watermark_service.apply_text_watermark(
            image_path=self.test_img_path,
            text="@my_channel_brand",
            position="bottom_right"
        )
        self.assertTrue(os.path.exists(out_path))
        with Image.open(out_path) as res_img:
            self.assertEqual(res_img.size, (400, 300))

if __name__ == "__main__":
    unittest.main()
