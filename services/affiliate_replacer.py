import re
import html
import logging
from typing import Dict, Any, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

logger = logging.getLogger(__name__)

# Merchant-specific affiliate parameter used when a rule value is a bare id ("uzum.uz=myref" -> ?p=myref)
AFFILIATE_PARAM_KEYS = (
    ("amazon.", "tag"),
    ("aliexpress.", "aff_id"),
    ("uzum.", "p"),
)

# URLs are matched on the (HTML-escaped) post text, so '&amp;' / '&#39;' entities are part of the URL token
_URL_PATTERN = re.compile(r'(https?://[^\s<>"\'\)]+?)([.,!?:;)]*)(?=\s|$|<|>|"|\'|\))', re.IGNORECASE)


def affiliate_param_key(netloc: str) -> str:
    """Query parameter carrying a bare affiliate id for the given host."""
    host = (netloc or "").lower()
    for marker, key in AFFILIATE_PARAM_KEYS:
        if marker in host:
            return key
    return "ref"


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
            if "=>" in line:
                key, val = line.split("=>", 1)
            elif "->" in line:
                key, val = line.split("->", 1)
            elif "=" in line:
                key, val = line.split("=", 1)
            else:
                continue
            k = key.strip().lower()
            v = val.strip()
            if k and v:
                rules[k] = v
        return rules

    @staticmethod
    def _rewrite_url(original_url: str, rules: Dict[str, str]) -> str:
        """Returns the affiliate version of one plain (unescaped) URL, or the URL unchanged."""
        parsed = urlparse(original_url)
        netloc = parsed.netloc.lower()
        netloc_clean = netloc[4:] if netloc.startswith("www.") else netloc

        for rule_domain, target_url in rules.items():
            rd = rule_domain.lower().strip()
            if "/" in rd:
                matched = rd in f"{netloc}{parsed.path.lower()}"
            else:
                matched = (rd == netloc) or (rd == netloc_clean) or netloc.endswith(f".{rd}")
            if not matched:
                continue

            target = html.unescape(target_url)
            if target.startswith("http://") or target.startswith("https://"):
                return target
            try:
                params = parse_qsl(parsed.query, keep_blank_values=True)
                cleaned_target = target.lstrip("?")
                if "=" in cleaned_target:
                    new_pairs = [p.split("=", 1) for p in cleaned_target.split("&") if "=" in p]
                else:
                    new_pairs = [[affiliate_param_key(netloc), cleaned_target]]
                new_keys = {k for k, _ in new_pairs}
                params = [(k, v) for k, v in params if k not in new_keys] + [(k, v) for k, v in new_pairs]
                return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
            except Exception:
                return target
        return original_url

    @classmethod
    def replace_and_protect(cls, text: str, rules_text: Any) -> Tuple[str, Dict[str, str]]:
        """Rewrites affiliate links and replaces every rewritten URL with a ___AFF_PROT_n___ placeholder.

        Returns (text_with_placeholders, {placeholder: html_escaped_url}). Later cleaning steps (link cleaner, ad
        shield, word replacements, translation) cannot touch the placeholders; the caller restores them last."""
        if not text or not rules_text:
            return text, {}
        rules = rules_text if isinstance(rules_text, dict) else cls.parse_rules(str(rules_text))
        if not rules:
            return text, {}

        protected: Dict[str, str] = {}
        by_url: Dict[str, str] = {}

        def replacer(match: re.Match) -> str:
            escaped_url = match.group(1)
            trailing_punct = match.group(2) or ""
            plain_url = html.unescape(escaped_url)
            try:
                new_url = cls._rewrite_url(plain_url, rules)
            except Exception as e:
                logger.debug(f"Error parsing URL {plain_url}: {e}")
                return match.group(0)
            if new_url == plain_url:
                return match.group(0)
            safe_url = html.escape(new_url, quote=True)
            placeholder = by_url.get(safe_url)
            if placeholder is None:
                placeholder = f"___AFF_PROT_{len(protected)}___"
                protected[placeholder] = safe_url
                by_url[safe_url] = placeholder
            return placeholder + trailing_punct

        return _URL_PATTERN.sub(replacer, text), protected

    @staticmethod
    def restore_placeholders(text: str, protected: Dict[str, str]) -> str:
        if not text or not protected:
            return text
        for placeholder, url in protected.items():
            text = text.replace(placeholder, url)
        return text

    @classmethod
    def replace_affiliate_links(cls, text: str, rules_text: Any) -> str:
        """
        Scans text for URLs and rewrites links according to affiliate rules.
        URLs are unescaped before parsing and the rewritten URL is HTML-escaped on insert, so query strings
        with '&amp;' are neither corrupted nor double-escaped.
        """
        new_text, protected = cls.replace_and_protect(text, rules_text)
        return cls.restore_placeholders(new_text, protected)

affiliate_replacer = AffiliateReplacer()
