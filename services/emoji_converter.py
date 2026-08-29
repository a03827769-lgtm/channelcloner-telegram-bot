import re
import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Complete dictionary of standard Unicode emojis mapped to Telegram Premium animated emoji IDs
# Sourced directly from the official verified Telegram Premium catalog
EMOJI_TO_PREMIUM_ID: Dict[str, str] = {
    # --- 1. Status & Alerts ---
    "✅": "5456432998092133477",
    "✔️": "5350722806281676158",
    "❌": "5161208387957950108",
    "✖️": "5271934564699226262",
    "⚠️": "5447644880824181073",
    "⏳": "5308017534140685461",
    "⌛": "5307646968657356266",
    "🔄": "5258440967161137445",
    "🔁": "5346167340629252154",
    "ℹ️": "5332679880599418983",
    "🔔": "6203791465471022369",
    "💡": "5215437353706863297",

    # --- 2. Finance & Crypto ---
    "⭐": "6005661956931850799",
    "🌟": "5463289097336405244",
    "✨": "5463289097336405244",
    "💫": "5438496463044752972",
    "💎": "5078343973303485905",
    "💵": "5188605164000395914",
    "💰": "5201873447554145566",
    "🪙": "5339381833467436990",
    "👛": "5276137490846075469",
    "💳": "4967518033061872209",
    "🧾": "5444856076954520455",
    "🛒": "5226656353744862682",
    "🛍️": "5366328015900914335",
    "🏷️": "5293996355704865196",

    # --- 3. UI, Navigation & Controls ---
    "🏠": "5278702045883292456",
    "⚙️": "5350396951407895212",
    "◀️": "5253997076169115797",
    "▶️": "5877536313623711363",
    "⬅️": "5352759161945867747",
    "➡️": "5332819376842226496",
    "🔼": "5332348837405145999",
    "🔍": "5258274739041883702",
    "📊": "5231200819986047254",
    "📈": "5301166722001168584",
    "📉": "5931472654660800739",
    "📑": "5233237686751355290",
    "📂": "5766994197705921104",
    "📁": "5877332341331857066",
    "🗂️": "5974580618640493852",
    "📄": "5875206779196935950",
    "🗄️": "5877485980901971030",
    "📎": "5877495434124988415",
    "✏️": "5879841310902324730",
    "✍️": "5877597667231534929",
    "📦": "5319204558147188648",
    "🧹": "5330548013452509511",
    "📚": "5992157823838984339",
    "🌍": "5778184941154078090",
    "🌐": "5778184941154078090",
    "🖼️": "5796214303329620386",
    "🎯": "5330088116944380969",
    "👉": "5332819376842226496",
    "👇": "5332348837405145999",

    # --- 4. VIP, Achievements & Badges ---
    "👑": "5229011542011299168",
    "🔥": "5402406965252989103",
    "🏆": "5188344996356448758",
    "🥇": "5440539497383087970",
    "🥈": "5447203607294265305",
    "🥉": "5453902265922376865",
    "🎁": "5359664288241829619",
    "🚀": "5372917041193828849",
    "⚡": "5285063442204481914",

    # --- 5. Security & User Management ---
    "🛡️": "5301096984617166561",
    "🔒": "5465443379917629504",
    "🔓": "6034962180875490251",
    "🔑": "5291873529464122510",
    "👮": "6269458311381258421",
    "👤": "6032693626394382504",
    "👥": "5330277804175013746",

    # --- 6. Support & Communication ---
    "🎧": "5332553157589352684",
    "❓": "5456623351042694411",
    "💬": "5891169510483823323",
    "✉️": "5332811182044627428",
    "📝": "5443038326535759644",
    "📢": "5864125355551364392",
    "📣": "5309984423003823246",
    "🔗": "5260450573768990626",
    "📤": "5472209190359407741",
    "📥": "5877307202888273539",
    "📞": "5201990176175299013",
    "📱": "5226772700113935347",
    "☎️": "5332531536723984111",

    # --- 7. Celebrations & Reactions ---
    "🎉": "5330425138733139875",
    "🎊": "5373005951311812951",
    "🎆": "5458806031947671561",
    "❤️": "5343726841427405712",
    "💖": "5465465194056525619",
    "👍": "5348067670384191767",
    "👎": "5994368422031397063",
    "📍": "5206246162348136684",
    "📌": "5796440171364749940",
    "📅": "5413879192267805083",
    "📆": "5251537301154062376",
    "⏰": "5373236586760651455",
    "⏱️": "5366043289633962337",
    "🕒": "5843799474362652262",
    "🗑️": "5372825386591732174",
    "🧹": "5879896690210639947",
    "☕": "5416076480356559241",
    "🤖": "5310259124817134249",
    "💻": "5319084384962248505",
    "🇺🇿": "5330254555517045320",

    # --- 8. 3D Numbers ---
    "0️⃣": "5211007082056138654",
    "1️⃣": "5208519140645551525",
    "2️⃣": "5208431510427810832",
    "3️⃣": "5210941321811871048",
    "4️⃣": "5210884761387547411",
    "5️⃣": "5208436462525103708",
    "6️⃣": "5208504357368117762",
    "7️⃣": "5210735270755845060",
    "8️⃣": "5210672727442080493",
    "9️⃣": "5208480125162634368"
}

# Add normalized variants without variation selectors (e.g. without \ufe0f)
_EXTRA_MAP = {}
for emoji_char, emoji_id in list(EMOJI_TO_PREMIUM_ID.items()):
    clean_char = emoji_char.replace("\ufe0f", "")
    if clean_char != emoji_char and clean_char not in EMOJI_TO_PREMIUM_ID:
        _EXTRA_MAP[clean_char] = emoji_id
EMOJI_TO_PREMIUM_ID.update(_EXTRA_MAP)

# Build regex pattern sorted by length descending to match composite emojis first
_SORTED_EMOJIS = sorted(EMOJI_TO_PREMIUM_ID.keys(), key=len, reverse=True)
_EMOJI_REGEX = re.compile("|".join(re.escape(e) for e in _SORTED_EMOJIS))

# Regex to find existing HTML tags (like <tg-emoji ...>...</tg-emoji>, <a ...>...</a>, <code>...</code>)
_HTML_TAG_REGEX = re.compile(r'(<tg-emoji[^>]*>.*?</tg-emoji>|<code[^>]*>.*?</code>|<pre[^>]*>.*?</pre>|<[^>]+>)', re.DOTALL)

class EmojiConverter:
    @staticmethod
    def convert_to_premium_emojis(text: str) -> str:
        """
        Replaces standard Unicode emojis with animated Telegram Premium custom emoji tags:
        <tg-emoji emoji-id="...">emoji</tg-emoji>
        Skips text that is already inside HTML tags or existing tg-emoji tags.
        """
        if not text:
            return ""

        # Split text by HTML tags to only process plain text segments
        parts = _HTML_TAG_REGEX.split(text)
        result_parts = []

        for part in parts:
            if not part:
                continue
            if part.startswith("<") and part.endswith(">"):
                # Preserve existing HTML tags untouched
                result_parts.append(part)
            else:
                # Replace unicode emojis in plain text chunks
                converted = _EMOJI_REGEX.sub(
                    lambda m: f'<tg-emoji emoji-id="{EMOJI_TO_PREMIUM_ID[m.group(0)]}">{m.group(0)}</tg-emoji>',
                    part
                )
                result_parts.append(converted)

        return "".join(result_parts)

emoji_converter = EmojiConverter()
