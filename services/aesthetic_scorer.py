import logging
import os
from typing import List, Tuple, Union, Optional
import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

class AestheticScorer:
    """
    Lightweight, deterministic aesthetic and visual quality scorer.
    Evaluates sharpness, colorfulness, contrast, and brightness to select
    the most attractive cover photo for albums and video stories without heavy neural networks.
    """

    @staticmethod
    def _to_cv2(image_input: Union[str, bytes, np.ndarray, Image.Image]) -> Optional[np.ndarray]:
        if isinstance(image_input, str):
            if not os.path.exists(image_input):
                return None
            try:
                with open(image_input, "rb") as f:
                    data = np.frombuffer(f.read(), dtype=np.uint8)
                if data is not None and len(data) > 0:
                    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
                    if img is not None:
                        return img
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            try:
                return cv2.imread(image_input)
            except Exception:
                return None
        elif isinstance(image_input, bytes):
            nparr = np.frombuffer(image_input, np.uint8)
            return cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(image_input, Image.Image):
            rgb = np.array(image_input.convert("RGB"))
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        elif isinstance(image_input, np.ndarray):
            return image_input
        return None

    def calculate_sharpness(self, gray: np.ndarray) -> float:
        """Computes Laplacian variance. Sharp images score higher; blurry images score lower."""
        try:
            var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            # Scale logarithmically to 0..30
            return min(30.0, max(0.0, float(np.log1p(var) * 4.0)))
        except Exception:
            return 10.0

    def calculate_colorfulness(self, bgr: np.ndarray) -> float:
        """Hasler & Süsstrunk metric for perceived colorfulness"""
        try:
            (B, G, R) = cv2.split(bgr.astype("float"))
            rg = np.absolute(R - G)
            yb = np.absolute(0.5 * (R + G) - B)

            std_root = np.sqrt((np.std(rg) ** 2) + (np.std(yb) ** 2))
            mean_root = np.sqrt((np.mean(rg) ** 2) + (np.mean(yb) ** 2))
            colorfulness = std_root + (0.3 * mean_root)
            # Scale to 0..30
            return min(30.0, max(0.0, float(colorfulness * 0.35)))
        except Exception:
            return 10.0

    def calculate_brightness_and_contrast(self, gray: np.ndarray) -> float:
        """Scores balanced lighting (penalizes dark or washed-out images) and good contrast"""
        try:
            mean_val = float(np.mean(gray))
            std_val = float(np.std(gray))

            # Optimal brightness is between 100 and 160
            brightness_score = 15.0 - (abs(mean_val - 130.0) / 130.0 * 15.0)
            brightness_score = max(0.0, brightness_score)

            # High contrast gives up to 15 points
            contrast_score = min(15.0, (std_val / 64.0) * 15.0)
            return brightness_score + contrast_score
        except Exception:
            return 15.0

    def calculate_resolution_bonus(self, bgr: np.ndarray) -> float:
        """Awards up to 10 points for high resolution images"""
        try:
            h, w = bgr.shape[:2]
            pixels = h * w
            if pixels >= 1920 * 1080:
                return 10.0
            elif pixels >= 1280 * 720:
                return 7.0
            elif pixels >= 800 * 600:
                return 4.0
            return 1.0
        except Exception:
            return 2.0

    def score_image(self, image_input: Union[str, bytes, np.ndarray, Image.Image]) -> float:
        """Computes comprehensive visual score from 0.0 to 100.0"""
        bgr = self._to_cv2(image_input)
        if bgr is None or bgr.size == 0:
            return 0.0

        try:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            sharpness = self.calculate_sharpness(gray)
            color = self.calculate_colorfulness(bgr)
            lighting = self.calculate_brightness_and_contrast(gray)
            res_bonus = self.calculate_resolution_bonus(bgr)

            total = sharpness + color + lighting + res_bonus
            return min(100.0, max(0.0, float(total)))
        except Exception as e:
            logger.debug(f"Error scoring image: {e}")
            return 20.0

    def reorder_photos_by_aesthetic(self, photo_paths: List[str]) -> List[str]:
        """
        Ranks photos and promotes the highest-scoring (most attractive) photo to index 0 (Cover Photo).
        Preserves original relative order of the remaining photos.
        """
        if not photo_paths or len(photo_paths) <= 1:
            return photo_paths

        valid_items = [p for p in photo_paths if p and os.path.exists(p)]
        if len(valid_items) <= 1:
            return photo_paths

        scores = [self.score_image(p) for p in valid_items]
        best_idx = int(np.argmax(scores))

        logger.info(f"Aesthetic Scoring: Best cover photo is index {best_idx} (score: {scores[best_idx]:.1f}) among {len(valid_items)} photos.")

        # If best photo is already at index 0, return unchanged
        if best_idx == 0:
            return valid_items

        # Move best photo to front
        best_photo = valid_items[best_idx]
        remaining = [p for i, p in enumerate(valid_items) if i != best_idx]
        return [best_photo] + remaining

aesthetic_scorer = AestheticScorer()
