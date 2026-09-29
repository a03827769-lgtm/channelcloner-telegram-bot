import re
import hashlib
import unicodedata
import logging
from typing import Optional, List, Tuple, Pattern
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Listings longer than this are cut before the regex passes (Telegram captions are <= 1024, posts <= 4096)
ANALYZE_MAX_CHARS = 4096

# Uzbek apostrophe variants (o‘, gʻ, U+02BB/U+02BC, typographic quotes, backticks) are unified to "'"
_APOSTROPHES_RE = re.compile(r"[`´ʻʼʽ‘’′]")
_INVISIBLE_RE = re.compile(r"[​-‏﻿‪-‮⁠-⁯]")


def normalize_listing_text(text: str) -> str:
    """NFKC normalization (keeps й ё ў қ ғ ҳ composed, folds fancy/full-width fonts), unified apostrophes,
    no invisible characters."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", text)
    norm = _INVISIBLE_RE.sub("", norm)
    return _APOSTROPHES_RE.sub("'", norm)


def format_price_usd(price: Optional[float], empty: str = "Aniqlangan") -> str:
    """Human readable USD amount for bot texts: "$850", "$65,000", "$4,500,000" (never exponent notation)."""
    if price is None:
        return empty
    try:
        value = float(price)
    except (TypeError, ValueError):
        return empty
    if value != value or value <= 0:  # NaN / zero
        return empty
    return f"${value:,.0f}"


@dataclass
class ListingMetadata:
    district: Optional[str] = None
    district_clean: Optional[str] = None
    rooms: Optional[int] = None
    area: Optional[float] = None
    floor: Optional[int] = None
    total_floors: Optional[int] = None
    renovation: Optional[str] = None
    is_luxury: bool = False
    is_new_building: bool = False
    price: Optional[float] = None
    quality_score: int = 0
    smart_badges: List[str] = field(default_factory=list)
    fingerprint: str = ""
    raw_text_clean: str = ""


class ListingAnalyzer:
    """
    100% Local, Deterministic Real Estate Listing Analyzer for Tashkent / Uzbekistan.
    Extracts structured real estate metadata (district, rooms, area, floor, renovation),
    computes listing quality score (0-100), generates smart visual badge tags for story cards,
    and produces deterministic fingerprints for multi-channel deduplication.
    Zero external AI dependencies, sub-millisecond execution.
    """

    # Comprehensive Tashkent districts and popular sub-areas / landmarks
    DISTRICTS_MAP = {
        "Mirobod": [
            r'mirobod[a-z]*', r'миробод[а-я]*', r'мирабад[а-я]*', r'госпитал[а-я]*',
            r'саперн[а-я]*', r'шахрисабз[а-я]*', r'шевченко', r'чехов[а-я]*',
            r'ойбек[а-я]*', r'oybek', r'северный вокзал', r'вэб'
        ],
        "Mirzo Ulug'bek": [
            r"mirzo\s*ulug'?bek[a-z]*", r"ulug'?bek\s*tuman[a-z]*", r'мирзо\s*улу[гғ]бек[а-я]*', r'горьк[а-я]*',
            r'буюк\s*ипак\s*й[уў]ли', r"buyuk\s*ipak\s*yo'?li", r'\bбий\b', r'ц-1\b', r'ц-2\b', r'новомосковск[а-я]*',
            r'дархан[а-я]*', r'аккурган[а-я]*', r'шастри', r'художников', r'карасу', r'кара-су'
        ],
        "Yakkasaroy": [
            r'yakkasaroy[a-z]*', r'yakkasaray[a-z]*', r'яккасарай[а-я]*', r'яккасарой[а-я]*', r'ракат[а-я]*', r'башлык',
            r'кушбеги', r'шота\s*руставели', r'педагогическ[а-я]*', r'бабур[а-я]*', r'вднх', r'текстил[а-я]*'
        ],
        "Chilonzor": [
            r'chilonzor[a-z]*', r'chilanzar[a-z]*', r'чилонзор[а-я]*', r'чиланзар[а-я]*',
            r'гагарин[а-я]*', r'новза[а-я]*', r'дружба\s*народов', r'халк\s*парки',
            r'катартал[а-я]*', r'шухрат'
        ],
        "Shayxontohur": [
            r'shayxontohur[a-z]*', r'shayxontoxur[a-z]*', r'shayhontohur[a-z]*', r'шайхантахур[а-я]*', r'шайхонтохур[а-я]*', r'шайхонтоҳур[а-я]*',
            r'ц-13\b', r'ц-14\b', r'ц-15\b', r'лабзак[а-я]*', r'самарканд\s*дарвоза',
            r'хадра', r'чорсу', r'малика', r'жар\b'
        ],
        "Yunusobod": [
            r'yunusobod[a-z]*', r'yunusabad[a-z]*', r'юнусобод[а-я]*', r'юнусабад[а-я]*',
            r'алайск[а-я]*', r'минор', r'шахристан[а-я]*', r'бодомзор[а-я]*',
            r'мегапланет', r'бахт\b', r'зенит', r'ц-4\b', r'ц-5\b', r'ц-6\b'
        ],
        "Yashnobod": [
            r'yashnobod[a-z]*', r'yashnabad[a-z]*', r'яшнобод[а-я]*', r'яшнабад[а-я]*',
            r'паркентск[а-я]*', r'д[уў]стлик', r"do'stlik", r'кадышев[а-я]*', r'авиасозлар[а-я]*',
            r'тузел[а-я]*', r'рисор'
        ],
        "Olmazor": [
            r'olmazor[a-z]*', r'almazar[a-z]*', r'олмазор[а-я]*', r'алмазар[а-я]*',
            r'себзар[а-я]*', r'каракамыш[а-я]*', r'беруни', r'тинчлик', r'гузар'
        ],
        "Sergeli": [
            r'sergeli[a-z]*', r'сергели[а-я]*', r'сергили[а-я]*', r'янги\s*сергели',
            r'спутник', r'ярмарка'
        ],
        "Uchtepa": [
            r'uchtepa[a-z]*', r'учтепа[а-я]*', r'урикзор[а-я]*', r'[уў]рикзор[а-я]*',
            r'ватан\b', r'фарход'
        ],
        "Bektemir": [
            r'bektemir[a-z]*', r'бектемир[а-я]*', r'куйлюк[а-я]*', r'қўйлиқ[а-я]*',
            r'водник', r'компас'
        ],
        "Yangihayot": [
            r'yangihayot[a-z]*', r'yangi\s*hayot', r'янгиха[её]т[а-я]*', r'янги\s*[ҳх]а[её]т[а-я]*', r'бинокор'
        ]
    }

    # Precompiled once: (canonical district, pattern)
    _DISTRICT_PATTERNS: List[Tuple[str, Pattern]] = [
        (canonical, re.compile(r'\b' + pat, re.IGNORECASE))
        for canonical, patterns in DISTRICTS_MAP.items()
        for pat in patterns
    ]

    # Number words to digits
    WORD_TO_ROOMS = {
        "bir": 1, "один": 1, "одно": 1, "1-": 1, "1": 1,
        "ikki": 2, "два": 2, "двух": 2, "двушка": 2, "2-": 2, "2": 2,
        "uch": 3, "три": 3, "трех": 3, "трёх": 3, "трешка": 3, "трёшка": 3, "3-": 3, "3": 3,
        "to'rt": 4, "tort": 4, "четыре": 4, "четырех": 4, "четырёх": 4, "4-": 4, "4": 4,
        "besh": 5, "пять": 5, "пяти": 5, "5-": 5, "5": 5
    }

    # Words that carry no listing identity (contacts, channel promotion, agency templates)
    _FINGERPRINT_STOPWORDS = frozenset({
        "telefon", "tel", "тел", "телефон", "aloqa", "алоқа", "связь", "admin", "админ", "telegram", "телеграм",
        "murojaat", "мурожаат", "kanal", "канал", "kanalimiz", "obuna", "подписывайтесь", "batafsil", "подробнее",
        "whatsapp", "instagram", "звоните", "пишите", "yozing", "qo'ng'iroq", "контакт", "контакты", "http", "https",
        "www", "narxi", "narx", "нархи", "цена", "манзил", "manzil", "адрес", "район", "tuman", "tumani",
    })
    _FINGERPRINT_URL_RE = re.compile(r"(?:https?://|t\.me/|www\.)\S+|@\w+|#\w+", re.IGNORECASE)
    _FINGERPRINT_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{6,}\d")
    _FINGERPRINT_WORD_RE = re.compile(r"[^\W\d_]{3,}")

    @classmethod
    def clean_text(cls, text: str) -> str:
        """Normalizes unicode text (NFKC, unified apostrophes) and collapses horizontal whitespace"""
        if not text:
            return ""
        norm = normalize_listing_text(text)
        norm = re.sub(r'[ \t]+', ' ', norm)
        return norm.strip()

    @staticmethod
    def _flat(text: str) -> str:
        """Single-line lowercase view used by the regex extractors (bounded whitespace keeps them linear)"""
        return re.sub(r'\s+', ' ', normalize_listing_text(text[:ANALYZE_MAX_CHARS])).lower()

    @classmethod
    def extract_district(cls, text: str) -> Optional[str]:
        """Detects and canonicalizes Tashkent district from text"""
        if not text:
            return None
        t_lower = cls._flat(text)
        # 1. Primary pass: Check explicit district patterns and known landmarks
        for canonical, pattern in cls._DISTRICT_PATTERNS:
            if pattern.search(t_lower):
                return canonical

        # 2. Secondary fallback pass: Bare kvartal numbers in Tashkent typically refer to Chilonzor (1-26 квартал)
        if re.search(r'\b(?:чиланзар\s*)?[1-9][0-9]?-квартал\b|\b[1-9][0-9]?-kvartal\b', t_lower):
            return "Chilonzor"

        return None

    @classmethod
    def extract_rooms(cls, text: str) -> Optional[int]:
        """Extracts room count (1 to 8) from listing"""
        if not text:
            return None
        t_lower = cls._flat(text)

        # Check studio
        if re.search(r'\b(?:studiya|студия|studio)\b', t_lower):
            return 1

        # Check digits: "3 xona", "3-xona", "3 хона", "3-комн", "3 комнат"
        pat_num = re.search(r'\b([1-8]) ?[-/]? ?(?:xona|хона|комн|xonali|комнат[а-я]*)', t_lower)
        if pat_num:
            return int(pat_num.group(1))

        # Check word forms: "uch xonali", "двухкомнатная", "трёхкомнатная"
        for word, val in cls.WORD_TO_ROOMS.items():
            if re.search(r'\b' + re.escape(word) + r' ?[-/]? ?(?:xona|хона|комн|xonali|комнат[а-я]*)', t_lower):
                return val

        # Secondary check: "3-х комнатная", "3х комнатная", "3x xonali", "3-x"
        pat_ru = re.search(r'\b([1-8]) ?[-–—]? ?(?:[хx] ?комнат[а-я]*|[хx] ?xona[a-z]*|[хx]\b)', t_lower)
        if pat_ru:
            return int(pat_ru.group(1))

        return None

    @classmethod
    def extract_area(cls, text: str) -> Optional[float]:
        """Extracts total living area in square meters (kv.m / m2 / m²)"""
        if not text:
            return None
        clean = cls._flat(text).replace(',', '.')
        pat = re.search(r'\b([0-9]{2,4}(?:\.[0-9]{1,2})?) ?(?:kv\.?m|m2|м2|m²|м²|метр|кв\.?м|кв)\b', clean)
        if pat:
            try:
                val = float(pat.group(1))
                if 15.0 <= val <= 1000.0:
                    return val
            except ValueError:
                pass
        return None

    @classmethod
    def extract_floor(cls, text: str) -> Tuple[Optional[int], Optional[int]]:
        """Extracts floor and total building floors: e.g. 4/9 etaj -> (4, 9), этаж 5 -> (5, None)"""
        if not text:
            return None, None
        t_lower = cls._flat(text)

        # 1. Pattern: 4/9 etaj or 4/9 этаж or этаж: 4/9
        pat_ratio = re.search(r'(?:(?:etaj|qavat|этаж|эт) ?[:\-–—]? ?)?([0-9]{1,2}) ?/ ?([0-9]{1,2}) ?(?:etaj|qavat|этаж|эт)?', t_lower)
        if pat_ratio and ('etaj' in pat_ratio.group(0) or 'этаж' in pat_ratio.group(0) or 'qavat' in pat_ratio.group(0) or 'эт' in pat_ratio.group(0)):
            try:
                fl = int(pat_ratio.group(1))
                tot = int(pat_ratio.group(2))
                if 1 <= fl <= 60 and 1 <= tot <= 60:
                    return fl, tot
            except ValueError:
                pass

        # 2. Pattern prefix: "этаж 5", "etaj: 4", "qavat: 3"
        pat_prefix = re.search(r'\b(?:etaj|qavat|этаж|эт) ?[:\-–—]? ?([0-9]{1,2})\b', t_lower)
        if pat_prefix:
            try:
                fl = int(pat_prefix.group(1))
                if 1 <= fl <= 60:
                    return fl, None
            except ValueError:
                pass

        # 3. Pattern suffix: "5-etaj", "5 этаж", "5-qavat"
        pat_suffix = re.search(r'\b([0-9]{1,2}) ?[-–—]? ?(?:etaj|qavat|этаж|эт)\b', t_lower)
        if pat_suffix:
            try:
                fl = int(pat_suffix.group(1))
                if 1 <= fl <= 60:
                    return fl, None
            except ValueError:
                pass

        return None, None

    @classmethod
    def extract_renovation(cls, text: str) -> Tuple[Optional[str], bool, bool]:
        """
        Detects renovation level, luxury status, and new building flag.
        Returns: (renovation_label, is_luxury, is_new_building)
        """
        if not text:
            return None, False, False
        t_lower = cls._flat(text)

        is_luxury = bool(re.search(
            r'\b(?:lyuks|люкс|vip|luxury|premium|премиум|авторский|mualliflik|пентхаус|penthouse)\b',
            t_lower
        ))

        is_new_building = bool(re.search(
            r'\b(?:novostroyka|новостройка|yangi bino|yangi uy|жк|новостройке)\b',
            t_lower
        ))

        renovation = None
        if is_luxury:
            renovation = "💎 Mualliflik dizayni / Lyuks"
        elif re.search(r"\b(?:evro|евро|yangi ta'?mir|свежий ремонт|отличный ремонт)\b", t_lower):
            renovation = "✨ Evroremont"
        elif re.search(r'\b(?:karobka|коробка|черновая)\b', t_lower):
            renovation = "📦 Karobka"
        elif re.search(r"\b(?:o'?rtacha|средний|косметический)\b", t_lower):
            renovation = "🛋 O'rtacha ta'mir"

        return renovation, is_luxury, is_new_building

    @classmethod
    def calculate_quality_score(
        cls,
        photo_count: int,
        price: Optional[float],
        district: Optional[str],
        rooms: Optional[int],
        area: Optional[float],
        floor: Optional[int],
        is_luxury: bool
    ) -> int:
        """
        Calculates listing attractiveness / quality score (0 to 100).
        Higher score = higher priority in drip-feed publication queue.
        """
        score = 0

        # Photos (max 45)
        if photo_count >= 6:
            score += 45
        elif photo_count >= 3:
            score += 35
        elif photo_count >= 1:
            score += 20

        # Price clarity (max 20)
        if price is not None and price >= 100:
            score += 20

        # District verified (max 15)
        if district:
            score += 15

        # Rooms clarity (max 10)
        if rooms:
            score += 10

        # Area & Floor (max 10)
        if area:
            score += 5
        if floor:
            score += 5

        # Bonus for luxury / high tier (max 5)
        if is_luxury or (price and price >= 1500):
            score = min(100, score + 5)

        return min(100, max(0, score))

    @classmethod
    def generate_smart_badges(
        cls,
        price: Optional[float],
        district: Optional[str],
        rooms: Optional[int],
        area: Optional[float],
        is_luxury: bool
    ) -> List[str]:
        """
        Builds visual badge chips for the Telegram Story card.
        Examples: ['💎 PREMYUM', '📍 Mirobod', '🚪 3-xona', '📐 85 m²']
        """
        badges = []

        # 1. Tier / Status Badge
        if is_luxury or (price and price >= 1500):
            badges.append("💎 PREMYUM")
        elif price and price >= 1000:
            badges.append("⭐ BIZNES")
        elif price and price >= 700:
            badges.append("🔥 SARA")

        # 2. Location Badge
        if district:
            badges.append(f"📍 {district}")

        # 3. Rooms Badge
        if rooms:
            badges.append(f"🚪 {rooms}-xona")

        # 4. Area Badge
        if area:
            badges.append(f"📐 {area:g} m²")

        return badges

    @classmethod
    def _signature_tokens(cls, text: str) -> List[str]:
        """Distinct meaningful words of the listing in order of appearance (contacts, links, hashtags and
        agency template words removed)"""
        t = cls._flat(text)
        t = cls._FINGERPRINT_URL_RE.sub(" ", t)
        t = cls._FINGERPRINT_PHONE_RE.sub(" ", t)
        seen = []
        seen_set = set()
        for word in cls._FINGERPRINT_WORD_RE.findall(t):
            if word in cls._FINGERPRINT_STOPWORDS or word in seen_set:
                continue
            seen_set.add(word)
            seen.append(word)
            if len(seen) >= 60:
                break
        return seen

    @classmethod
    def compute_fingerprint(
        cls,
        text: str,
        district: Optional[str],
        rooms: Optional[int],
        area: Optional[float],
        price: Optional[float],
        floor: Optional[int] = None
    ) -> str:
        """
        Computes a deterministic SHA256 fingerprint for cross-channel deduplication of the same listing
        (reposted with different contacts or formatting).
        Returns "" (no deduplication) when the post carries too little information to identify a listing,
        e.g. an empty or emoji-only caption, so unrelated posts never share one fingerprint.
        """
        tokens = cls._signature_tokens(text or "")
        known_fields = sum(1 for v in (district, rooms, area, price, floor) if v)
        if len(tokens) < 3 and known_fields < 3:
            return ""

        d_key = (district or "unknown").lower()
        r_key = str(rooms or 0)
        f_key = str(floor or 0)
        # Bin area into 5m² buckets to catch small rounding differences (e.g. 78 m² vs 80 m²)
        a_bin = str(int(round(area / 5.0) * 5)) if area else "0"
        # Bin price into $50 buckets
        p_bin = str(int(round(price / 50.0) * 50)) if price else "0"
        # All meaningful words (not an alphabetical prefix of them, which agency templates share)
        text_signature = "-".join(sorted(tokens))

        raw_fingerprint = f"{d_key}|{r_key}|{f_key}|{a_bin}|{p_bin}|{text_signature}"
        return hashlib.sha256(raw_fingerprint.encode('utf-8')).hexdigest()[:32]

    @classmethod
    def analyze(
        cls,
        text: str,
        photo_count: int = 1,
        existing_price: Optional[float] = None
    ) -> ListingMetadata:
        """
        Master analysis pipeline:
        Extracts all real estate features, calculates score, generates badges and fingerprint.
        """
        clean = cls.clean_text((text or "")[:ANALYZE_MAX_CHARS])
        district = cls.extract_district(clean)
        rooms = cls.extract_rooms(clean)
        area = cls.extract_area(clean)
        floor, total_floors = cls.extract_floor(clean)
        renovation, is_luxury, is_new_building = cls.extract_renovation(clean)

        price = existing_price
        if price is None and clean:
            try:
                from services.story_cloner_service import StoryClonerService
                price = StoryClonerService.extract_price(clean)
            except Exception:
                logger.debug("Price extraction failed during listing analysis", exc_info=True)
                price = None

        quality_score = cls.calculate_quality_score(
            photo_count=photo_count,
            price=price,
            district=district,
            rooms=rooms,
            area=area,
            floor=floor,
            is_luxury=is_luxury
        )

        smart_badges = cls.generate_smart_badges(
            price=price,
            district=district,
            rooms=rooms,
            area=area,
            is_luxury=is_luxury
        )

        fingerprint = cls.compute_fingerprint(
            text=clean,
            district=district,
            rooms=rooms,
            area=area,
            price=price,
            floor=floor
        )

        return ListingMetadata(
            district=district,
            district_clean=district,
            rooms=rooms,
            area=area,
            floor=floor,
            total_floors=total_floors,
            renovation=renovation,
            is_luxury=is_luxury,
            is_new_building=is_new_building,
            price=price,
            quality_score=quality_score,
            smart_badges=smart_badges,
            fingerprint=fingerprint,
            raw_text_clean=clean
        )


listing_analyzer = ListingAnalyzer()
