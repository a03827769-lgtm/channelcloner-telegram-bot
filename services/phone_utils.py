import re
from typing import Tuple, Optional

def normalize_phone_number(raw_phone: str) -> Tuple[bool, str, Optional[str]]:
    """
    Intelligently normalizes phone numbers from various user inputs:
    - 9-digit Uzbek local format (e.g. 955494960, 901234567) -> +998955494960
    - 12-digit format without + (e.g. 998955494960) -> +998955494960
    - Russian/CIS 11-digit format starting with 8 (e.g. 89261234567) -> +79261234567
    - 11-digit starting with 7 (e.g. 79261234567) -> +79261234567
    - Full international with + (e.g. +998901234567, +14155552671)
    - Strips spaces, dashes, brackets, dots, etc.

    Returns: (is_valid, formatted_e164, formatted_display)
    """
    if not raw_phone:
        return False, "", None

    # Strip all non-digit and non-plus characters
    cleaned = re.sub(r'[^\d+]', '', raw_phone.strip())
    digits_only = re.sub(r'\D', '', cleaned)

    if not digits_only:
        return False, "", None

    # Case 1: Already has plus
    if cleaned.startswith("+"):
        if len(digits_only) >= 10:
            formatted = f"+{digits_only}"
            return True, formatted, format_display(formatted)
        elif len(digits_only) == 9:
            # User typed +901234567 or +955494960 (thought it was +998)
            formatted = f"+998{digits_only}"
            return True, formatted, format_display(formatted)
        return False, "", None

    # Case 2: 9-digit Uzbek local number (e.g. 955494960, 90 123 45 67)
    if len(digits_only) == 9:
        formatted = f"+998{digits_only}"
        return True, formatted, format_display(formatted)

    # Case 3: 12-digit number starting with 998 (e.g. 998955494960)
    if len(digits_only) == 12 and digits_only.startswith("998"):
        formatted = f"+{digits_only}"
        return True, formatted, format_display(formatted)

    # Case 4: 11-digit CIS number starting with 8 (e.g. 89261234567)
    if len(digits_only) == 11 and digits_only.startswith("8"):
        formatted = f"+7{digits_only[1:]}"
        return True, formatted, format_display(formatted)

    # Case 5: 11-digit CIS number starting with 7 (e.g. 79261234567)
    if len(digits_only) == 11 and digits_only.startswith("7"):
        formatted = f"+{digits_only}"
        return True, formatted, format_display(formatted)

    # Case 6: Generic international number (10 to 15 digits)
    if 10 <= len(digits_only) <= 15:
        formatted = f"+{digits_only}"
        return True, formatted, format_display(formatted)

    return False, "", None

def format_display(phone_e164: str) -> str:
    """Formats E.164 phone into a readable string like +998 95 549-49-60"""
    digits = phone_e164.lstrip("+")
    if len(digits) == 12 and digits.startswith("998"):
        # +998 XX XXX-XX-XX
        return f"+998 ({digits[3:5]}) {digits[5:8]}-{digits[8:10]}-{digits[10:12]}"
    elif len(digits) == 11 and digits.startswith("7"):
        # +7 XXX XXX-XX-XX
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return phone_e164

def mask_phone_number(phone_str: str) -> str:
    """Masks middle digits of a phone number for privacy, e.g. +998 90 ***-**-67"""
    if not phone_str:
        return "Mavjud emas"
    digits = re.sub(r'\D', '', str(phone_str))
    if len(digits) >= 9:
        prefix = digits[:5]
        suffix = digits[-2:]
        return f"+{prefix}****{suffix}"
    return str(phone_str)
