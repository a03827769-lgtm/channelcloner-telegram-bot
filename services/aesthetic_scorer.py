import logging
import os
from typing import List, Tuple, Union, Optional
import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Photos are scored on a thumbnail: the metrics are relative rankings, and a full-resolution float64 copy of a
# 12 MP photo needs ~870 MB (OOM on 512 MB hosts).
SCORING_MAX_SIDE = 512
# Albums are capped before scoring (only the first photos are ever used by the story renderer)
MAX_PHOTOS_TO_SCORE = 10


class AestheticScorer:
    """
    Lightweight, deterministic aesthetic and visual quality scorer.
    Evaluates sharpness, colorfulness, contrast, and brightness to select
    the most attractive cover photo for albums and video stories without heavy neural networks.
    """

    @staticmethod
    def _reduced_decode_flag(width: int, height: int) -> int:
        """JPEG decoders can downscale by 2/4/8 while decoding, which avoids ever allocating the full image"""
        longest = max(width, height)
        if longest >= SCORING_MAX_SIDE * 8:
            return cv2.IMREAD_REDUCED_COLOR_8
        if longest >= SCORING_MAX_SIDE * 4:
            return cv2.IMREAD_REDUCED_COLOR_4
        if longest >= SCORING_MAX_SIDE * 2:
            return cv2.IMREAD_REDUCED_COLOR_2
        return cv2.IMREAD_COLOR

    @staticmethod
    def _image_dimensions(path: str) -> Optional[Tuple[int, int]]:
        """Reads (width, height) from the file header without decoding pixels"""
        try:
            with Image.open(path) as im:
                return im.size
        except Exception:
            return None

    @staticmethod
    def _shrink(img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        longest = max(h, w)
        if longest <= SCORING_MAX_SIDE:
            return img
        scale = SCORING_MAX_SIDE / float(longest)
        return cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)

    @classmethod
    def _load_for_scoring(cls, image_input: Union[str, bytes, np.ndarray, Image.Image]) -> Tuple[Optional[np.ndarray], int]:
        """Returns (BGR thumbnail <= SCORING_MAX_SIDE, original pixel count)"""
        if isinstance(image_input, str):
            if not os.path.exists(image_input):
                return None, 0
            dims = cls._image_dimensions(image_input)
            flag = cls._reduced_decode_flag(*dims) if dims else cv2.IMREAD_COLOR
            img = None
            try:
                with open(image_input, "rb") as f:
                    data = np.frombuffer(f.read(), dtype=np.uint8)
                if data is not None and len(data) > 0:
                    img = cv2.imdecode(data, flag)
                    if img is None and flag != cv2.IMREAD_COLOR:
                        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            except Exception:
                logger.debug("Ignored exception", exc_info=True)
            if img is None:
                return None, 0
            pixels = dims[0] * dims[1] if dims else img.shape[0] * img.shape[1]
            return cls._shrink(img), pixels
        if isinstance(image_input, bytes):
            nparr = np.frombuffer(image_input, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return None, 0
            return cls._shrink(img), img.shape[0] * img.shape[1]
        if isinstance(image_input, Image.Image):
            pixels = image_input.width * image_input.height
            thumb = image_input.convert("RGB")
            thumb.thumbnail((SCORING_MAX_SIDE, SCORING_MAX_SIDE))
            return cv2.cvtColor(np.array(thumb), cv2.COLOR_RGB2BGR), pixels
        if isinstance(image_input, np.ndarray):
            return cls._shrink(image_input), image_input.shape[0] * image_input.shape[1]
        return None, 0

    @staticmethod
    def _to_cv2(image_input: Union[str, bytes, np.ndarray, Image.Image]) -> Optional[np.ndarray]:
        """Backward compatible loader returning a (downscaled) BGR image"""
        img, _ = AestheticScorer._load_for_scoring(image_input)
        return img

    def calculate_sharpness(self, gray: np.ndarray) -> float:
        """Computes Laplacian variance. Sharp images score higher; blurry images score lower."""
        try:
            var = float(cv2.Laplacian(gray, cv2.CV_32F).var())
            # Scale logarithmically to 0..30
            return min(30.0, max(0.0, float(np.log1p(var) * 4.0)))
        except Exception:
            return 10.0

    def calculate_colorfulness(self, bgr: np.ndarray) -> float:
        """Hasler & Süsstrunk metric for perceived colorfulness"""
        try:
            (B, G, R) = cv2.split(bgr.astype(np.float32))
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

    @staticmethod
    def _resolution_bonus_for_pixels(pixels: int) -> float:
        if pixels >= 1920 * 1080:
            return 10.0
        if pixels >= 1280 * 720:
            return 7.0
        if pixels >= 800 * 600:
            return 4.0
        return 1.0

    def calculate_resolution_bonus(self, bgr: np.ndarray) -> float:
        """Awards up to 10 points for high resolution images"""
        try:
            h, w = bgr.shape[:2]
            return self._resolution_bonus_for_pixels(h * w)
        except Exception:
            return 2.0

    def score_image(self, image_input: Union[str, bytes, np.ndarray, Image.Image]) -> float:
        """Computes comprehensive visual score from 0.0 to 100.0"""
        bgr, original_pixels = self._load_for_scoring(image_input)
        if bgr is None or bgr.size == 0:
            return 0.0

        try:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            sharpness = self.calculate_sharpness(gray)
            color = self.calculate_colorfulness(bgr)
            lighting = self.calculate_brightness_and_contrast(gray)
            # The bonus rates the ORIGINAL resolution, not the scoring thumbnail
            res_bonus = self._resolution_bonus_for_pixels(original_pixels)

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

        candidates = valid_items[:MAX_PHOTOS_TO_SCORE]
        scores = [self.score_image(p) for p in candidates]
        best_idx = int(np.argmax(scores))

        logger.info(f"Aesthetic Scoring: Best cover photo is index {best_idx} (score: {scores[best_idx]:.1f}) among {len(candidates)} photos.")

        # If best photo is already at index 0, return unchanged
        if best_idx == 0:
            return valid_items

        # Move best photo to front
        best_photo = valid_items[best_idx]
        remaining = [p for i, p in enumerate(valid_items) if i != best_idx]
        return [best_photo] + remaining


aesthetic_scorer = AestheticScorer()
