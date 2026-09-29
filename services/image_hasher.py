import logging
import os
import asyncio
from typing import List, Optional, Tuple, Dict, Any, Union
try:
    import cv2
except Exception:
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


def _hash_ints(hashes: List[str]) -> List[int]:
    values = []
    for h in hashes:
        try:
            values.append(int(h, 16))
        except (TypeError, ValueError):
            continue
    return values


def _best_listing_match(
    grouped_listings: Dict[Tuple[str, int], Dict[str, Any]],
    new_hashes: List[str],
    clean_input_src: str,
    source_msg_id: Optional[int],
    threshold: int,
    min_matching_photos: int = 2
) -> Optional[Tuple[Dict[str, Any], int]]:
    """CPU-bound comparison (runs in a worker thread). A listing matches when a strict majority of the new post's
    photos (and at least min_matching_photos of them) is found in it; among matches the one with most matching
    photos, then the most recent, wins. One shared photo (a logo, a stock picture) never makes an album a duplicate."""
    new_values = _hash_ints([h for h in new_hashes if h])
    if not new_values:
        return None
    required = min(max(1, min_matching_photos), len(new_values))
    best: Optional[Tuple[Dict[str, Any], int]] = None
    for key, listing in grouped_listings.items():
        # Don't match against the exact same message
        if clean_input_src and key[0] == clean_input_src:
            if source_msg_id is None or key[1] == source_msg_id:
                continue
        listing_values = _hash_ints(listing["hashes"])
        match_count = 0
        for nv in new_values:
            if any(bin(nv ^ ev).count("1") <= threshold for ev in listing_values):
                match_count += 1  # One match per new photo is enough
        if match_count * 2 <= len(new_values) or match_count < required:
            continue
        if best is None or (match_count, listing["last_seen"]) > (best[1], best[0]["last_seen"]):
            best = (listing, match_count)
    return best


class ImageHasherService:
    """
    Service responsible for perceptual image hashing, listing deduplication,
    and cross-channel price arbitrage detection.
    """

    def __init__(self, match_threshold: int = 8, min_matching_photos: int = 2):
        self.match_threshold = match_threshold  # Hamming distance <= 8 considered visual match
        self.min_matching_photos = min_matching_photos
        # One lock per tenant: "check duplicate" and "save hashes" must be atomic, otherwise two posts with the same
        # photos processed concurrently would both pass the check.
        self._tenant_locks: Dict[Any, asyncio.Lock] = {}

    def listing_lock(self, tenant_id: Any) -> asyncio.Lock:
        lock = self._tenant_locks.get(tenant_id)
        if lock is None:
            if len(self._tenant_locks) > 1000:
                for key, old in list(self._tenant_locks.items()):
                    if not old.locked():
                        self._tenant_locks.pop(key, None)
            lock = asyncio.Lock()
            self._tenant_locks[tenant_id] = lock
        return lock

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
        source_msg_id: Optional[int] = None,
        user_id: Optional[int] = None
    ) -> Tuple[bool, bool, Optional[Dict[str, Any]]]:
        """
        Checks if a set of photo hashes matches an existing listing in the DB (last 30 days).
        Only listings of the same tenant that were published into the same destination are compared.
        Returns:
            (is_duplicate: bool, is_price_drop: bool, match_info: Optional[dict])
            - match_info contains: {'matched_msg_id', 'matched_channel', 'previous_price', 'current_price', ...}
        """
        if not new_hashes:
            return False, False, None

        from database.db_manager import db_manager

        # Resolve current pair details if in DB
        current_pair = None
        if pair_id is not None:
            try:
                current_pair = await db_manager.get_pair_by_id(pair_id)
            except Exception:
                current_pair = None

        tenant_id = user_id if user_id is not None else (current_pair.user_id if current_pair else None)
        existing_records = await db_manager.get_recent_image_hashes(days=30, user_id=tenant_id)
        if not existing_records:
            return False, False, None

        clean_input_src = (source_channel or "").lstrip("@").lower().strip()

        # Destination scope: a listing published into another destination channel is not a duplicate here
        target_key = None
        if current_pair:
            target_key = db_manager.pair_endpoint_key(current_pair.target_channel, current_pair.target_id)
        pair_cache: Dict[int, Any] = {}
        if current_pair and pair_id is not None:
            pair_cache[pair_id] = current_pair

        # Group existing hashes by (source_channel, source_msg_id)
        grouped_listings: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for row in existing_records:
            row_pair_id = row.get("pair_id")
            if target_key and row_pair_id != pair_id:
                if row_pair_id is None:
                    continue
                if row_pair_id not in pair_cache:
                    try:
                        pair_cache[row_pair_id] = await db_manager.get_pair_by_id(row_pair_id)
                    except Exception:
                        pair_cache[row_pair_id] = None
                row_pair = pair_cache.get(row_pair_id)
                if row_pair is None or db_manager.pair_endpoint_key(row_pair.target_channel, row_pair.target_id) != target_key:
                    continue

            clean_row_chan = (row.get("source_channel") or "").lstrip("@").lower().strip()
            key = (clean_row_chan, row["source_msg_id"])
            listing = grouped_listings.get(key)
            if listing is None:
                listing = grouped_listings[key] = {
                    "source_channel": clean_row_chan,
                    "source_msg_id": row["source_msg_id"],
                    "price": row.get("price") or 0.0,
                    "pair_id": row_pair_id,
                    "last_seen": str(row.get("created_at") or ""),
                    "hashes": []
                }
            listing["hashes"].append(row["phash"])
            created = str(row.get("created_at") or "")
            if created > listing["last_seen"]:
                listing["last_seen"] = created

        if not grouped_listings:
            return False, False, None

        best = await asyncio.to_thread(
            _best_listing_match, grouped_listings, new_hashes, clean_input_src, source_msg_id,
            self.match_threshold, self.min_matching_photos
        )
        if best is None:
            return False, False, None

        listing, match_count = best
        prev_price = float(listing.get("price") or 0.0)
        is_price_drop = bool(current_price and prev_price > 0 and current_price < prev_price)
        match_info = {
            "matched_channel": listing["source_channel"],
            "matched_msg_id": listing["source_msg_id"],
            "matched_pair_id": listing.get("pair_id"),
            "previous_price": prev_price,
            "current_price": current_price or 0.0,
            "matches_found": match_count
        }
        return True, is_price_drop, match_info

    async def update_listing_price(self, match_info: Optional[Dict[str, Any]], price: Optional[float]):
        """Stores the latest seen price of a known listing, so the next repost is compared with the current price
        and not with the price of the first publication (1000 -> 800 -> 950 is not a price drop)."""
        if not match_info or not price or price <= 0:
            return
        from database.db_manager import db_manager
        try:
            await db_manager.execute(
                "UPDATE image_hashes SET price = ? WHERE source_channel = ? AND source_msg_id = ?",
                (float(price), match_info.get("matched_channel") or "", match_info.get("matched_msg_id"))
            )
        except Exception:
            logger.debug("Could not update listing price baseline", exc_info=True)

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
