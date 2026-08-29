from dataclasses import dataclass
from typing import Optional, List, Dict
from datetime import datetime

@dataclass
class User:
    user_id: int
    full_name: str
    username: Optional[str] = None
    created_at: Optional[str] = None
    is_admin: bool = False

@dataclass
class Subscription:
    user_id: int
    tier: str = "free"  # "free", "pro", "vip"
    expires_at: Optional[str] = None
    trial_expires_at: Optional[str] = None
    trial_notified: bool = False
    stars_spent: int = 0
    created_at: Optional[str] = None

    @property
    def is_active(self) -> bool:
        now = datetime.utcnow()
        if self.tier in ["pro", "vip"]:
            if not self.expires_at:
                return False
            try:
                exp = datetime.fromisoformat(self.expires_at)
                return exp > now
            except Exception:
                return False
        
        # Free Tier: active only during 14-day trial
        if not self.trial_expires_at:
            return True
        try:
            trial_exp = datetime.fromisoformat(self.trial_expires_at)
            return trial_exp > now
        except Exception:
            return False

    @property
    def is_trial_active(self) -> bool:
        if self.tier != "free":
            return False
        if not self.trial_expires_at:
            return True
        try:
            return datetime.fromisoformat(self.trial_expires_at) > datetime.utcnow()
        except Exception:
            return False

    @property
    def max_channels(self) -> int:
        if not self.is_active:
            return 0
        if self.tier == "vip":
            return 999
        elif self.tier == "pro":
            return 5
        return 1

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
    auto_cta_buttons: bool = False
    backup_enabled: bool = True
    
    created_at: Optional[str] = None

    @property
    def blacklist_list(self) -> List[str]:
        if not self.blacklist_words:
            return []
        return [w.strip().lower() for w in self.blacklist_words.split(",") if w.strip()]

    @property
    def replace_dict(self) -> Dict[str, str]:
        if not self.replace_words:
            return {}
        result = {}
        for item in self.replace_words.split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                result[k.strip()] = v.strip()
        return result

@dataclass
class ClonedMessage:
    id: Optional[int] = None
    pair_id: int = 0
    source_msg_id: int = 0
    target_msg_id: Optional[int] = None
    media_group_id: Optional[str] = None
    cloned_at: Optional[str] = None

@dataclass
class DripQueueItem:
    id: Optional[int] = None
    pair_id: int = 0
    msg_data_json: str = ""
    scheduled_at: str = ""
    status: str = "pending"  # "pending", "sent", "failed"
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
    created_at: Optional[str] = None
