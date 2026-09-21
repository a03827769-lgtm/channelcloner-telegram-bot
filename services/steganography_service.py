import logging
import os
import time
from typing import Optional, Dict, Any, Union, List
import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# 16-bit synchronization preamble to identify presence of watermark
PREAMBLE = "KLON"

class SteganographyService:
    """
    Embeds and extracts invisible, robust digital signatures into images
    using frequency-domain (DCT) block modulation.
    Allows proving copyright and provenance even after Telegram compression.
    """

    def __init__(self):
        pass

    @staticmethod
    def _safe_imread(path: str, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
        try:
            if not os.path.exists(path):
                return None
            data = np.fromfile(path, dtype=np.uint8)
            if data is None or len(data) == 0:
                return None
            return cv2.imdecode(data, flags)
        except Exception:
            return None

    @staticmethod
    def _safe_imwrite(path: str, img: np.ndarray, params: Optional[list] = None) -> bool:
        try:
            ext = os.path.splitext(path)[1].lower()
            if not ext:
                ext = ".png"
            params = params or []
            success, encoded = cv2.imencode(ext, img, params)
            if not success:
                return False
            encoded.tofile(path)
            return True
        except Exception:
            return False

    @staticmethod
    def _text_to_bits(text: str) -> List[int]:
        bits = []
        for char in text.encode("utf-8"):
            for i in range(8):
                bits.append((char >> (7 - i)) & 1)
        return bits

    @staticmethod
    def _bits_to_text(bits: List[int]) -> Optional[str]:
        if len(bits) % 8 != 0:
            bits = bits[: len(bits) - (len(bits) % 8)]
        byte_arr = bytearray()
        for i in range(0, len(bits), 8):
            byte = 0
            for j in range(8):
                byte = (byte << 1) | bits[i + j]
            byte_arr.append(byte)
        try:
            return byte_arr.decode("utf-8")
        except Exception:
            return None

    def embed_watermark(self, input_image_path: str, output_image_path: str, payload: str) -> bool:
        """
        Embeds invisible copyright payload into mid-frequency DCT coefficients of the image.
        Format: "KLON:<payload>:END"
        """
        try:
            if not os.path.exists(input_image_path):
                return False

            img = self._safe_imread(input_image_path)
            if img is None:
                return False

            full_payload = f"{PREAMBLE}:{payload}:END"
            bits = self._text_to_bits(full_payload)

            h, w = img.shape[:2]

            # Available 8x8 blocks
            blocks_h = h // 8
            blocks_w = w // 8
            total_blocks = blocks_h * blocks_w

            if len(bits) > total_blocks:
                logger.warning(f"Image too small ({w}x{h}) to hold watermark payload ({len(bits)} bits vs {total_blocks} blocks)")
                return False

            bit_idx = 0
            # Work directly on Blue channel (index 0 in BGR)
            b_float = img[:, :, 0].astype(np.float32)
            diff = 25.0  # Robustness delta

            for bh in range(blocks_h):
                for bw in range(blocks_w):
                    if bit_idx >= len(bits):
                        break

                    block = b_float[bh * 8 : (bh + 1) * 8, bw * 8 : (bw + 1) * 8]
                    dct_block = cv2.dct(block)

                    # Modulate mid-frequency coefficients [4, 3] and [3, 4]
                    bit = bits[bit_idx]
                    if bit == 1:
                        if dct_block[4, 3] <= dct_block[3, 4] + diff:
                            dct_block[4, 3] = dct_block[3, 4] + diff
                    else:
                        if dct_block[3, 4] <= dct_block[4, 3] + diff:
                            dct_block[3, 4] = dct_block[4, 3] + diff

                    idct_block = cv2.idct(dct_block)
                    b_float[bh * 8 : (bh + 1) * 8, bw * 8 : (bw + 1) * 8] = idct_block
                    bit_idx += 1

                if bit_idx >= len(bits):
                    break

            bgr_mod = img.copy()
            bgr_mod[:, :, 0] = np.clip(b_float, 0, 255).astype(np.uint8)

            ext = os.path.splitext(output_image_path)[1].lower()
            params = [int(cv2.IMWRITE_JPEG_QUALITY), 98] if ext in [".jpg", ".jpeg"] else []
            return self._safe_imwrite(output_image_path, bgr_mod, params)
        except Exception as e:
            logger.error(f"Error embedding invisible watermark: {e}", exc_info=True)
            return False

    def extract_watermark(self, image_path: str) -> Optional[str]:
        """
        Extracts hidden payload from DCT coefficients.
        Returns the decoded payload if PREAMBLE and :END markers match.
        """
        try:
            if not os.path.exists(image_path):
                return None

            img = self._safe_imread(image_path)
            if img is None:
                return None

            h, w = img.shape[:2]

            blocks_h = h // 8
            blocks_w = w // 8

            bits = []
            b_float = img[:, :, 0].astype(np.float32)

            # Read up to 800 bits (100 chars)
            max_bits = min(800, blocks_h * blocks_w)

            for bh in range(blocks_h):
                for bw in range(blocks_w):
                    if len(bits) >= max_bits:
                        break

                    block = b_float[bh * 8 : (bh + 1) * 8, bw * 8 : (bw + 1) * 8]
                    dct_block = cv2.dct(block)

                    if dct_block[4, 3] > dct_block[3, 4]:
                        bits.append(1)
                    else:
                        bits.append(0)

                if len(bits) >= max_bits:
                    break

            text = self._bits_to_text(bits)
            if text and PREAMBLE in text:
                # Extract between PREAMBLE: and :END
                parts = text.split(f"{PREAMBLE}:", 1)
                if len(parts) > 1:
                    inner = parts[1].split(":END", 1)[0]
                    return inner.strip()

            return None
        except Exception as e:
            logger.debug(f"Error extracting watermark: {e}")
            return None

    def create_provenance_payload(self, channel_id: Union[str, int], user_id: int) -> str:
        """Generates standard provenance payload: 'UID_<uid>_CH_<cid>_TS_<ts>'"""
        ts = int(time.time())
        clean_ch = str(channel_id).replace("-100", "").lstrip("@")
        return f"UID_{user_id}_CH_{clean_ch}_TS_{ts}"

steganography_service = SteganographyService()
