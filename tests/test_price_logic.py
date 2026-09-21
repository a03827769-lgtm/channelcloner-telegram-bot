import re
import unicodedata

def parse_num_string(raw: str):
    if not raw:
        return None
    s = raw.strip().replace(' ', '')
    # Handle dot or comma as thousands separator when followed by exactly 3 digits
    s = re.sub(r'[.,](\d{3})(?!\d)', r'\1', s)
    s = s.replace(',', '.')
    s = re.sub(r'\.+', '.', s).strip('.')
    try:
        return float(s)
    except ValueError:
        return None

def extract_price(text: str):
    if not text:
        return None
    clean_text = unicodedata.normalize('NFKD', text)
    distractor_unit = r'(?:kv\.?m|m2|м2|метр|кв\.?м|qavat|қават|этаж|etaj|xona|хона|sotix|соток|kishi|odam)'
    curr_usd = r'(?:\$|usd|у\.?е\.?|y\.?e\.?|dollar[a-z]*|доллар[а-я]*)'
    curr_uzs = r'(?:so[\'ʼ`]?m|som|сум|сўм|uzs)'

    # 1. Numbers explicitly accompanied by USD currency: "750$", "1.500$", "1,500$", "1200 у.е.", "800 USD"
    pat_num_usd = re.compile(r'([0-9\s.,]+)\s*' + curr_usd + r'(?!\s*' + distractor_unit + r')', re.IGNORECASE)
    for match in pat_num_usd.finditer(clean_text):
        val = parse_num_string(match.group(1))
        if val is not None and 50 <= val <= 5000000:
            return val

    # USD currency preceding number: "$750", "$ 1,500", "USD 1200"
    pat_usd_num = re.compile(curr_usd + r'\s*([0-9\s.,]+)(?!\s*' + distractor_unit + r')', re.IGNORECASE)
    for match in pat_usd_num.finditer(clean_text):
        val = parse_num_string(match.group(1))
        if val is not None and 50 <= val <= 5000000:
            return val

    # 2. Label preceded: "narxi: 850", "цена - 1200", "ijara 800"
    pat_label = re.compile(
        r'(?:narxi|narx|цена|стоимость|qiymati|ijara|arenda|аренда|to[\'ʼ`]?lov)\s*[:\-–—]?\s*([0-9\s.,]+)',
        re.IGNORECASE
    )
    for match in pat_label.finditer(clean_text):
        tail = clean_text[match.end():match.end() + 25].lower()
        if re.match(r'^\s*(?:' + curr_uzs + r'|' + distractor_unit + r')', tail):
            continue
        val = parse_num_string(match.group(1))
        if val is not None and 50 <= val <= 5000000:
            return val

    # 3. Dan pattern: "700$ dan", "700 dan boshlanadi"
    pat_dan = re.compile(r'([0-9\s.,]+)\s*(?:' + curr_usd + r')?\s*dan\b(?!\s*' + distractor_unit + r')', re.IGNORECASE)
    for match in pat_dan.finditer(clean_text):
        val = parse_num_string(match.group(1))
        if val is not None and 50 <= val <= 5000000:
            return val

    # 4. Uzbek So'm (UZS) converted to USD: "12 000 000 so'm"
    pat_uzs = re.compile(r'([0-9\s.,]+)\s*' + curr_uzs, re.IGNORECASE)
    for match in pat_uzs.finditer(clean_text):
        val = parse_num_string(match.group(1))
        if val is not None and val >= 100000:
            usd_val = round(val / 12800.0, 1)
            if 50 <= usd_val <= 5000000:
                return usd_val

    return None

def is_demand_post(text: str) -> bool:
    if not text:
        return False
    t_lower = text.lower()

    # 1. Strong listing indicators (landlords/agents offering properties for rent/sale)
    offer_patterns = [
        r'\b(?:arendaga|ijaraga)?\s*(?:beriladi|берилади|topshiriladi|топширилади)\b',
        r'\b(?:сдается|сдаётся|сдам|сдаю|сдаем)\b',
        r'\b(?:sotiladi|сотилади|sotuvda|продается|продаётся|продам)\b'
    ]
    if any(re.search(pat, t_lower) for pat in offer_patterns):
        # Explicit offer: even if mentioning tenant preferences ('oila kerak', 'kvartirant kerak'), it is an offer
        return False

    # 2. Demand indicators (clients/tenants seeking properties)
    demand_patterns = [
        r'\b(?:kvartira|uy|joy|arenda|ijara|xona|хона|квартира)\s+(?:kerak|керак)\b',
        r'\b(?:kerak|керак)\s+(?:kvartira|uy|joy|arenda|ijara|xona)\b',
        r'\b(?:menga|bizga|oilaga|klientga|klientimizga)\s+(?:kerak|керак)\b',
        r'\b(?:qidiryapman|qidirilmoqda|qidirayapmiz|qidirilyapti)\b',
        r'\b(?:olmoqchiman|olmoqchimiz|olaman)\b',
        r'\b(?:ищу|ищем|сниму|снимем|нужна|нужно|ищет)\b',
        r'\b(?:клиент\s*бор|klient\s*bor|клиент\s*есть|клиентларга)\b',
        r'\b(?:запрос\s*на\s*аренду|запрос)\b',
        r'\barenda\s*kerak\b',
        r'\bkvartira\s*kerak\b'
    ]
    return any(re.search(pat, t_lower) for pat in demand_patterns)

def test_price_logic():
    test_cases = [
        ("750$", 750.0),
        ("$800", 800.0),
        ("1.500$", 1500.0),
        ("1,500$", 1500.0),
        ("65.000$", 65000.0),
        ("65,000 $", 65000.0),
        ("Narxi: 700 $", 700.0),
        ("Цена: 1200 у.е.", 1200.0),
        ("1 000 USD / oy", 1000.0),
        ("700$ dan boshlanadi", 700.0),
        ("Arenda: 77 kv.m kvartira, narxi 800$", 800.0),
        ("Narxi: 4 000 000 so'm", 312.5),
        ("Yunusobod 4-mavze, 2 xona, 4-qavat, tel: +998901234567. Narxi: 750$", 750.0),
        ("2 xona evroremont, barcha qulayliklar bor", None)
    ]
    for t, exp in test_cases:
        res = extract_price(t)
        assert res == exp, f"Failed for '{t}': got {res}, expected {exp}"

def test_demand_detection():
    demands = [
        "Menga 2 xonali kvartira kerak, budjet 800$",
        "Ищу квартиру в Юнусабаде до 1000$",
        "Клиент бор, 3 хона керак, 1200$ гача",
        "Arenda kerak zudlik bilan",
        "Oilaga kvartira kerak",
        "Kvartira qidiryapman Yunusoboddan",
        "Сниму квартиру для семьи, 900$",
        "Ищем 2-комнатную возле метро"
    ]
    for d in demands:
        assert is_demand_post(d) is True, f"Demand failed: {d}"

    offers = [
        "Yunusobod 2 xona arendaga beriladi. Faqat oila kerak. Narxi: 800$",
        "Kvartira topshiriladi. Kvartirant kerak. Narxi: 750$",
        "Сдается 2-комнатная квартира. Нужна порядочная семья. 900$",
        "Sotiladi: 3 xona novostroyka, 70000$",
        "Yunusobod 6 mavze, 2 xona arendaga beriladi, 750$",
        "Yangi remont qilingan uy ijaraga beriladi"
    ]
    for o in offers:
        assert is_demand_post(o) is False, f"Offer failed: {o}"
