import re
import logging
from typing import Dict, Optional, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

logger = logging.getLogger(__name__)

class AffiliateReplacer:
    @staticmethod
    def parse_rules(rules_text: str) -> Dict[str, str]:
        """
        Parses multi-line or comma-separated rules:
        Format: domain_or_url=affiliate_replacement_url
        Example:
        aliexpress.com=https://s.click.aliexpress.com/e/_Dk1234
        uzum.uz=https://uzum.uz/?ref=my_aff_id
        """
        if not rules_text:
            return {}
        
        rules = {}
        # Support newline or comma separation
        lines = re.split(r'[\r\n,]+', rules_text)
        for line in lines:
            line = line.strip()
            if "=" in line:
                key, val = line.split("=", 1)
                k = key.strip().lower()
                v = val.strip()
                if k and v:
                    rules[k] = v
        return rules

    @classmethod
    def replace_affiliate_links(cls, text: str, rules_text: str) -> str:
        """
        Scans text for URLs and rewrites links according to affiliate rules.
        """
        if not text or not rules_text:
            return text

        rules = cls.parse_rules(rules_text)
        if not rules:
            return text

        url_pattern = re.compile(r'https?://[^\s<>"\'\)]+', re.IGNORECASE)

        def replacer(match: re.Match) -> str:
            original_url = match.group(0)
            try:
                parsed = urlparse(original_url)
                netloc = parsed.netloc.lower()

                # 1. Exact or partial domain match
                for rule_domain, target_url in rules.items():
                    if rule_domain in netloc or rule_domain in original_url.lower():
                        # If target_url has query parameter placeholder e.g. target_url?original=...
                        return target_url
            except Exception as e:
                logger.debug(f"Error parsing URL {original_url}: {e}")

            return original_url

        return url_pattern.sub(replacer, text)

affiliate_replacer = AffiliateReplacer()
