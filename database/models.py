import re
from dataclasses import dataclass
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta
from config.plans import TIER_MAX_CHANNELS, TRIAL_DAYS

@dataclass
class User:
    user_id: int
    full_name: str
    username: Optional[str] = None
    created_at: Optional[str] = None
    is_admin: bool = False
    is_blocked: bool = False

    @property
    def subscription_tier(self) -> str:
        return getattr(self, "_subscription_tier", "free")

@dataclass
class Subscription:
    user_id: int
    tier: str = "free"  # "free", "pro", "vip"
    expires_at: Optional[str] = None
    trial_expires_at: Optional[str] = None
    trial_notified: bool = False
    paid_notified: bool = False
    stars_spent: int = 0
    created_at: Optional[str] = None

    @staticmethod
    def _parse_iso_to_utc_naive(dt_str: Optional[str]) -> Optional[datetime]:
        if not dt_str:
            return None
        try:
            text = dt_str.strip().replace(" ", "T")
            if text[-1:] in ("Z", "z"):
                text = text[:-1] + "+00:00"  # fromisoformat() rejects the "Z" suffix before Python 3.11
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            return None

    @property
    def is_active(self) -> bool:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if self.tier in ["pro", "vip"]:
            exp = self._parse_iso_to_utc_naive(self.expires_at)
            return bool(exp and exp > now)
        
        # Free Tier: active only during the trial window
        if not self.trial_expires_at:
            if self.created_at:
                c_date = self._parse_iso_to_utc_naive(self.created_at)
                return bool(c_date and (c_date + timedelta(days=TRIAL_DAYS)) > now)
            return False
        trial_exp = self._parse_iso_to_utc_naive(self.trial_expires_at)
        return bool(trial_exp and trial_exp > now)

    @property
    def is_trial_active(self) -> bool:
        if self.tier != "free":
            return False
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if not self.trial_expires_at:
            if self.created_at:
                c_date = self._parse_iso_to_utc_naive(self.created_at)
                return bool(c_date and (c_date + timedelta(days=TRIAL_DAYS)) > now)
            return False
        trial_exp = self._parse_iso_to_utc_naive(self.trial_expires_at)
        return bool(trial_exp and trial_exp > now)

    @property
    def is_vip(self) -> bool:
        return self.tier == "vip" and self.is_active

    @property
    def max_channels(self) -> int:
        if not self.is_active:
            return 0
        return TIER_MAX_CHANNELS.get(self.tier, TIER_MAX_CHANNELS["free"])

@dataclass
class Payment:
    id: Optional[int] = None
    user_id: int = 0
    telegram_payment_charge_id: str = ""
    amount: int = 0
    tier: str = "pro"
    created_at: Optional[str] = None

@dataclass
class ChannelPair:
    id: Optional[int] = None
    user_id: int = 0
    source_channel: str = ""
    source_title: str = ""
    source_id: Optional[int] = None
    target_channel: str = ""
    target_title: str = ""
    target_id: Optional[int] = None
    is_active: bool = True
    clean_links: bool = True
    custom_signature: str = ""
    remove_signature: bool = False
    blacklist_words: str = ""
    replace_words: str = ""
    clone_mode: str = "clean"  # "clean" or "forward"
    
    # 5 Killer-Features Settings + VIP Auto Emojis
    auto_translate: bool = False
    target_lang: str = "uz"
    source_lang: str = "auto"
    image_watermark_type: str = "none"  # "none", "text", "logo"
    image_watermark_text: str = ""
    image_watermark_pos: str = "bottom_right"
    is_protected_source: bool = False
    affiliate_rules: str = ""
    auto_premium_emojis: bool = False
    
    # Next-Gen Tier-1 Features
    video_watermark_type: str = "none"  # "none", "text", "logo"
    video_watermark_text: str = ""
    video_watermark_pos: str = "bottom_right"
    drip_delay_minutes: int = 0  # 0, 5, 15, 30
    night_mode: str = "off"  # "off", "silent", "buffer"
    ai_paraphrase_mode: str = "off"  # "off", "short", "hype", "formal"
    tone_of_voice: str = "standard"  # "standard", "luxury", "urgency", "conversational"
    enable_invisible_watermark: bool = True
    auto_cta_buttons: bool = False
    backup_enabled: bool = True
    last_seen_msg_id: Optional[int] = None
    auto_catchup: bool = True
    
    # 2026 SOTA Upgrades: Forum Topics, AI Ad Shield & Bot API 8.x
    source_topic_id: Optional[int] = None
    target_topic_id: Optional[int] = None
    ad_action: str = "clean"  # "clean", "drop", "swap", "off"
    show_caption_above: bool = False
    
    created_at: Optional[str] = None

    def __post_init__(self):
        if self.drip_delay_minutes < 0:
            self.drip_delay_minutes = 0
        if self.night_mode not in ("off", "silent", "buffer"):
            self.night_mode = "off"
        if self.clone_mode not in ("clean", "forward"):
            self.clone_mode = "clean"

    @property
    def blacklist_list(self) -> List[str]:
        if not self.blacklist_words:
            return []
        return [w.strip().lower() for w in re.split(r'[\r\n,]+', self.blacklist_words) if w.strip()]

    @property
    def replace_dict(self) -> Dict[str, str]:
        if not self.replace_words:
            return {}
        result = {}
        for line in re.split(r'[\r\n]+', self.replace_words):
            line = line.strip()
            if not line:
                continue
            # Check if line contains mapping delimiters
            delims = ["=>", "->", "="]
            delim_count = 0
            for d in delims:
                if d in line:
                    delim_count = line.count(d)
                    break

            if delim_count > 1:
                items = re.split(r',\s*(?=[^,]+(?:=>|->|=))', line)
            else:
                items = [line]

            for item in items:
                item = item.strip()
                if not item:
                    continue
                if "=>" in item:
                    k, v = item.split("=>", 1)
                elif "->" in item:
                    k, v = item.split("->", 1)
                elif "=" in item:
                    k, v = item.split("=", 1)
                else:
                    continue
                clean_k = k.strip()
                if clean_k:
                    result[clean_k] = v.strip()
        return result

@dataclass
class ClonedMessage:
    id: Optional[int] = None
    pair_id: int = 0
    source_msg_id: int = 0
    target_msg_id: Optional[int] = None
    media_group_id: Optional[str] = None
    media_type: str = "text"
    source_channel: Optional[str] = None
    target_channel: Optional[str] = None
    story_id: Optional[int] = None
    status: str = "active"  # "active", "sold", "edited"
    price: float = 0.0
    last_caption: Optional[str] = None
    cloned_at: Optional[str] = None

@dataclass
class DripQueueItem:
    id: Optional[int] = None
    pair_id: int = 0
    msg_data_json: str = ""
    scheduled_at: str = ""
    status: str = "pending"  # "pending", "sent", "failed"
    error_message: Optional[str] = None
    created_at: Optional[str] = None

@dataclass
class ChannelBackup:
    id: Optional[int] = None
    pair_id: int = 0
    source_id: Optional[int] = None
    message_id: int = 0
    text: str = ""
    media_type: str = "none"
    media_file_id: Optional[str] = None
    entities_json: str = ""
    media_group_id: Optional[str] = None
    created_at: Optional[str] = None

@dataclass
class StorySettings:
    user_id: int
    source_channel: str = ""
    source_title: str = ""
    source_id: Optional[int] = None
    target_type: str = "self"  # "self" or "channel"
    target_channel: str = ""
    target_id: Optional[int] = None
    min_price: float = 700.0
    max_price: float = 0.0  # 0 = no limit
    require_photos: bool = True
    require_price: bool = True
    filter_demands: bool = True
    background_style: str = "telegram_green"
    # Story automation starts only after the user explicitly enables it (account + source configured)
    is_active: bool = False
    prime_hours_enabled: bool = True
    prime_hours_start: int = 9
    prime_hours_end: int = 22
    drip_delay_minutes: int = 45
    max_stories_per_day: int = 5
    enable_smart_badges: bool = True
    pin_to_profile: bool = True
    video_duration: int = 25
    enable_ai_voice: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def __post_init__(self):
        if self.max_stories_per_day <= 0:
            self.max_stories_per_day = 5
        if self.drip_delay_minutes < 0:
            self.drip_delay_minutes = 0

@dataclass
class StorySourceChannel:
    id: Optional[int] = None
    user_id: int = 0
    channel_username: str = ""
    channel_title: str = ""
    channel_id: Optional[int] = None
    is_active: bool = True
    created_at: Optional[str] = None

@dataclass
class StoryQueueItem:
    id: Optional[int] = None
    user_id: int = 0
    source_channel: str = ""
    source_msg_id: int = 0
    price: Optional[float] = None
    district: str = ""
    rooms: Optional[int] = None
    area: Optional[float] = None
    score: int = 0
    payload_json: str = ""
    status: str = "pending"  # "pending", "sent", "skipped", "failed"
    scheduled_at: str = ""
    created_at: Optional[str] = None
    error_message: Optional[str] = None

@dataclass
class PostedStory:
    id: Optional[int] = None
    user_id: int = 0
    source_channel: str = ""
    source_id: Optional[int] = None
    source_msg_id: int = 0
    grouped_id: Optional[int] = None
    story_id: Optional[int] = None
    price: Optional[float] = None
    caption_snippet: str = ""
    target_type: str = "self"
    posted_at: Optional[str] = None
    status: str = "success"

@dataclass
class SupplierConfig:
    id: Optional[int] = None
    provider_name: str = "Standard SMM/Reseller API"
    api_url: str = "https://justanotherpanel.com/api/v2"
    api_key: str = ""
    margin_percent: float = 25.0  # Automatic profit margin
    balance: float = 0.0
    currency: str = "USD"
    last_synced_at: Optional[str] = None
    is_active: bool = True

@dataclass
class StoreProduct:
    id: Optional[int] = None
    supplier_id: int = 1
    supplier_service_id: int = 0
    name: str = ""
    category: str = "Telegram"
    type: str = "Default"
    supplier_rate: float = 0.0
    selling_price_stars: int = 50
    min_quantity: int = 10
    max_quantity: int = 10000
    is_available: bool = True
    stock_status: str = "in_stock"  # "in_stock", "out_of_stock"
    description: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

@dataclass
class StoreOrder:
    id: Optional[int] = None
    user_id: int = 0
    product_id: int = 0
    product_name: str = ""
    quantity: int = 1
    price_stars: int = 0
    target_link: str = ""
    supplier_order_id: Optional[int] = None
    status: str = "completed"  # "completed", "pending_admin", "failed"
    admin_notified: bool = False
    note: Optional[str] = None
    created_at: Optional[str] = None


