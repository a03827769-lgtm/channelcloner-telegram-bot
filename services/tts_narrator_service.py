import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TTSNarratorService:
    """
    Uzbek Neural AI Voiceover Generator for Telegram Video Stories.
    Extracts key property highlights (rooms, location, price, key benefits)
    from listing captions and synthesizes a punchy, professional 8-12s audio teaser
    using Microsoft Neural Edge-TTS ('uz-UZ-MadinaNeural' / 'uz-UZ-SardorNeural').
    """

    def __init__(self, default_voice: str = "uz-UZ-MadinaNeural"):
        self.default_voice = default_voice

    def generate_teaser_script(self, caption: str, price: Optional[float] = None) -> str:
        """
        Parses real estate post caption to create a clean, natural-sounding
        spoken Uzbek script for the story teaser (approx 15-25 words, ~8-10 seconds).
        """
        if not caption or not caption.strip():
            return "Kanalimizda yangi ajoyib ko'chmas mulk taklifi. Batafsil ma'lumot olish uchun postni ko'ring!"

        # Clean HTML tags and markdown
        clean_text = re.sub(r"<[^>]+>", " ", caption)
        clean_text = re.sub(r"[*_`#~]", "", clean_text)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

        # Extract Room Count (e.g., 2 xona, 3-xonali, 4 xonali)
        rooms_match = re.search(r"(\d+)\s*(?:[-–]?\s*xona(?:li)?)", clean_text, re.IGNORECASE)
        rooms_phrase = ""
        if rooms_match:
            rooms_count = rooms_match.group(1)
            rooms_phrase = f"{rooms_count} xonali shinam xonadon."

        # Extract District / Location
        districts = [
            "Chilonzor", "Yunusobod", "Mirzo Ulug'bek", "Yakkasaroy", "Mirobod",
            "Shayxontohur", "Olmazor", "Uchtepa", "Sergeli", "Yangi Hayot",
            "Bektemir", "Yashnobod", "Novomoskovskaya", "Oybek", "Markaz"
        ]
        location_phrase = ""
        for d in districts:
            if re.search(rf"\b{re.escape(d)}\b", clean_text, re.IGNORECASE):
                location_phrase = f"{d} tumanida joylashgan."
                break

        # Extract Price or format provided price
        price_phrase = ""
        if price and price > 0:
            p_val = int(price)
            if p_val >= 1000:
                price_phrase = f"Narxi {p_val // 1000} ming dollar."
            else:
                price_phrase = f"Narxi {p_val} dollar."
        else:
            p_match = re.search(r"(\d+[\s.,]?\d*)\s*(?:\$|dollar|usd|y\.e)", clean_text, re.IGNORECASE)
            if p_match:
                raw_num = re.sub(r"[\s.,]", "", p_match.group(1))
                if raw_num.isdigit():
                    val = int(raw_num)
                    if val >= 1000:
                        price_phrase = f"Narxi {val // 1000} ming dollar."
                    else:
                        price_phrase = f"Narxi {val} dollar."

        # Assemble teaser components
        parts = []
        if rooms_phrase:
            parts.append(rooms_phrase)
        if location_phrase:
            parts.append(location_phrase)
        if price_phrase:
            parts.append(price_phrase)

        if not parts:
            # Fallback: extract first sentence or clean 100 chars
            first_sent = clean_text.split(".")[0].strip()
            if len(first_sent) > 120:
                first_sent = first_sent[:117] + "..."
            return f"Diqqat, yangi taklif: {first_sent}. To'liq ma'lumot kanalimizda!"

        # Combine with a warm call to action
        body = " ".join(parts)
        return f"Ajoyib taklif! {body} To'liq ma'lumot kanalimizda mavjud."

    async def generate_voiceover_mp3(
        self,
        script_text: str,
        output_path: Optional[str] = None,
        voice: Optional[str] = None,
        rate: str = "+0%",
        pitch: str = "+0Hz"
    ) -> Optional[str]:
        """
        Speech synthesis / voice narration is completely disabled per architecture requirements
        ('gapiradigan narsa olib tashlangan'). Telegram Stories rely exclusively on clean luxury background
        music tracks and 1080x1920 Full HD visuals. Returns None immediately without external network calls.
        """
        logger.debug("Voiceover generation skipped: speech narration is disabled per system architecture.")
        return None

    @property
    def is_enabled(self) -> bool:
        return False


tts_narrator_service = TTSNarratorService()
