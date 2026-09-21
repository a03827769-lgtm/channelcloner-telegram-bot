import re
import logging
from typing import Dict, Optional, List, Any
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
    def replace_affiliate_links(cls, text: str, rules_text: Any) -> str:
        """
        Scans text for URLs and rewrites links according to affiliate rules.
        """
        if not text or not rules_text:
            return text

        if isinstance(rules_text, dict):
            rules = rules_text
        else:
            rules = cls.parse_rules(str(rules_text))
        if not rules:
            return text

        url_pattern = re.compile(r'(https?://[^\s<>"\'\)]+?)([.,!?:;)]*)(?=\s|$|<|>|"|\'|\))', re.IGNORECASE)

        def replacer(match: re.Match) -> str:
            original_url = match.group(1)
            trailing_punct = match.group(2) or ""
            try:
                parsed = urlparse(original_url)
                netloc = parsed.netloc.lower()

                # 1. Exact or domain/path match
                for rule_domain, target_url in rules.items():
                    rd = rule_domain.lower().strip()
                    matched = False
                    if "/" in rd:
                        matched = rd in f"{netloc}{parsed.path.lower()}"
                    else:
                        netloc_clean = netloc[4:] if netloc.startswith("www.") else netloc
                        matched = (rd == netloc) or (rd == netloc_clean) or netloc.endswith(f".{rd}")

                    if matched:
                        if target_url.startswith("http://") or target_url.startswith("https://"):
                            return target_url + trailing_punct
                        try:
                            q_dict = parse_qs(parsed.query)
                            cleaned_target = target_url.lstrip("?")
                            if "=" in cleaned_target and not cleaned_target.startswith("http"):
                                for param in cleaned_target.split("&"):
                                    if "=" in param:
                                        p_key, p_val = param.split("=", 1)
                                        q_dict[p_key] = [p_val]
                            else:
                                q_dict['ref'] = [target_url]
                            new_query = urlencode(q_dict, doseq=True)
                            return urlunparse(parsed._replace(query=new_query)) + trailing_punct
                        except Exception:
                            return target_url + trailing_punct
            except Exception as e:
                logger.debug(f"Error parsing URL {original_url}: {e}")

            return original_url + trailing_punct

        return url_pattern.sub(replacer, text)

affiliate_replacer = AffiliateReplacer()
