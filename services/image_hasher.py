import logging
import os
import asyncio
from typing import List, Optional, Tuple, Dict, Any, Union
try:
    import cv2
except Exception as _cv_err:
    cv2 = None
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

def calculate_phash(image_input: Union[str, bytes, Image.Image, np.ndarray]) -> Optional[str]:
    """
    Computes a robust 64-bit DCT perceptual hash (pHash) as a 16-character hex string.
    Works with file paths, raw bytes, PIL Images, or numpy arrays.
    Hamming distance <= 8 indicates visually identical or slightly compressed/watermarked images.
    """
    if cv2 is None:
        return None
    try:
        img_gray: Optional[np.ndarray] = None

        if isinstance(image_input, str):
            if not os.path.exists(image_input):
                return None
            try:
                data = np.fromfile(image_input, dtype=np.uint8)
                if data is not None and len(data) > 0:
                    img_gray = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            except Exception:
                img_gray = None
            if img_gray is None:
                img_gray = cv2.imread(image_input, cv2.IMREAD_GRAYSCALE)
        elif isinstance(image_input, bytes):
            nparr = np.frombuffer(image_input, np.uint8)
            img_gray = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        elif isinstance(image_input, Image.Image):
            rgb = np.array(image_input.convert("L"))
            img_gray = rgb
        elif isinstance(image_input, np.ndarray):
            if len(image_input.shape) == 3:
                img_gray = cv2.cvtColor(image_input, cv2.COLOR_BGR2GRAY)
            else:
                img_gray = image_input

        if img_gray is None or img_gray.size == 0:
            return None

        # 1. Resize to 32x32 for high-frequency normalization
        resized = cv2.resize(img_gray, (32, 32), interpolation=cv2.INTER_AREA)

        # 2. Compute 2D Discrete Cosine Transform (DCT)
        dct = cv2.dct(np.float32(resized))

        # 3. Extract top-left 8x8 low frequencies (represents overall structure)
        dct_low = dct[:8, :8]

        # 4. Exclude DC component [0, 0] when calculating median
        med = float(np.median(dct_low.flatten()[1:]))

        # 5. Build 64-bit bitmask based on whether coefficient > median
        mask = (dct_low > med).flatten()
        hash_int = 0
        for i, val in enumerate(mask):
            if val:
                hash_int |= (1 << i)

        # Return formatted 16-char hex string (64 bits)
        return f"{hash_int:016x}"
    except Exception as e:
        logger.error(f"Error calculating image pHash: {e}", exc_info=True)
        return None

def hamming_distance(hash1: str, hash2: str) -> int:
    """Calculates bitwise Hamming distance between two 16-char hex hashes"""
    try:
        val1 = int(hash1, 16)
        val2 = int(hash2, 16)
        xor_val = val1 ^ val2
        return bin(xor_val).count("1")
    except Exception:
        return 64

class ImageHasherService:
    """
    Service responsible for perceptual image hashing, listing deduplication,
    and cross-channel price arbitrage detection.
    """

    def __init__(self, match_threshold: int = 8, min_matching_photos: int = 2):
        self.match_threshold = match_threshold  # Hamming distance <= 8 considered visual match
        self.min_matching_photos = min_matching_photos

    def get_phash(self, image_input: Union[str, bytes, Image.Image, np.ndarray]) -> Optional[str]:
        return calculate_phash(image_input)

    async def get_phash_async(self, image_input: Union[str, bytes, Image.Image, np.ndarray]) -> Optional[str]:
        """Calculates pHash offloaded to worker thread to prevent event loop lag."""
        return await asyncio.to_thread(calculate_phash, image_input)

    async def get_multiple_phashes_async(self, paths: List[str]) -> List[str]:
        """Calculates pHashes for multiple image paths off the main thread."""
        def _compute():
            hashes = []
            for p in paths:
                if p and os.path.exists(p):
                    h = calculate_phash(p)
                    if h:
                        hashes.append(h)
            return hashes
        return await asyncio.to_thread(_compute)

    async def check_listing_duplicate(
        self,
        new_hashes: List[str],
        current_price: Optional[float] = None,
        pair_id: Optional[int] = None,
        source_channel: Optional[str] = None,
        source_msg_id: Optional[int] = None
    ) -> Tuple[bool, bool, Optional[Dict[str, Any]]]:
        """
        Checks if a set of photo hashes matches an existing listing in the DB (last 30 days).
        Returns:
            (is_duplicate: bool, is_price_drop: bool, match_info: Optional[dict])
            - match_info contains: {'matched_msg_id', 'matched_channel', 'previous_price', 'target_msg_id', ...}
        """
        if not new_hashes:
            return False, False, None

        from database.db_manager import db_manager
        existing_records = await db_manager.get_recent_image_hashes(days=30)
        if not existing_records:
            return False, False, None

        clean_input_src = (source_channel or "").lstrip("@").lower().strip()

        # Resolve current pair details if in DB
        current_pair = None
        if pair_id is not None:
            try:
                current_pair = await db_manager.get_pair_by_id(pair_id)
            except Exception:
                current_pair = None

        # Cache pair lookups during iteration
        pair_cache: Dict[int, Any] = {}
        if current_pair and pair_id is not None:
            pair_cache[pair_id] = current_pair

        # Group existing hashes by (source_channel, source_msg_id)
        grouped_listings: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for row in existing_records:
            row_pair_id = row.get("pair_id")

            # Multi-user isolation:
            # If both current pair and row pair are known in the database, ensure they belong
            # to the same user or target the same destination channel. Different users' channels
            # must never block each other!
            if current_pair and row_pair_id is not None and row_pair_id != pair_id:
                if row_pair_id not in pair_cache:
                    try:
                        pair_cache[row_pair_id] = await db_manager.get_pair_by_id(row_pair_id)
                    except Exception:
                        pair_cache[row_pair_id] = None
                row_pair = pair_cache.get(row_pair_id)
                if row_pair is None or (row_pair.user_id != current_pair.user_id and row_pair.target_channel != current_pair.target_channel):
                    continue

            clean_row_chan = (row.get("source_channel") or "").lstrip("@").lower().strip()
            key = (clean_row_chan, row["source_msg_id"])
            if key not in grouped_listings:
                grouped_listings[key] = {
                    "source_channel": clean_row_chan,
                    "source_msg_id": row["source_msg_id"],
                    "price": row.get("price") or 0.0,
                    "pair_id": row_pair_id,
                    "hashes": []
                }
            grouped_listings[key]["hashes"].append(row["phash"])

        # Compare new hashes against each existing listing
        for key, listing in grouped_listings.items():
            # Don't match against the exact same message
            if clean_input_src and key[0] == clean_input_src:
                if source_msg_id is not None:
                    if key[1] == source_msg_id:
                        continue
                else:
                    continue

            match_count = 0
            for nh in new_hashes:
                if not nh:
                    continue
                for eh in listing["hashes"]:
                    if hamming_distance(nh, eh) <= self.match_threshold:
                        match_count += 1
                        break  # One match per new photo is enough

            if match_count >= self.min_matching_photos or (len(new_hashes) == 1 and match_count >= 1):
                prev_price = float(listing.get("price") or 0.0)
                is_price_drop = False
                if current_price and prev_price > 0 and current_price < prev_price:
                    is_price_drop = True

                match_info = {
                    "matched_channel": listing["source_channel"],
                    "matched_msg_id": listing["source_msg_id"],
                    "previous_price": prev_price,
                    "current_price": current_price or 0.0,
                    "matches_found": match_count
                }
                return True, is_price_drop, match_info

        return False, False, None

    async def save_listing_hashes(
        self,
        hashes: List[str],
        source_channel: str,
        source_msg_id: int,
        pair_id: Optional[int] = None,
        price: Optional[float] = None
    ):
        """Persists calculated image hashes for the listing in DB"""
        if not hashes:
            return
        from database.db_manager import db_manager
        valid_hashes = [h for h in hashes if h and len(h) == 16]
        if valid_hashes:
            await db_manager.save_image_hashes(
                pair_id=pair_id,
                source_channel=source_channel,
                source_msg_id=source_msg_id,
                hashes=valid_hashes,
                price=price or 0.0
            )

image_hasher = ImageHasherService()
