"""
Subscription plans — the single source of truth for prices, durations and tier ordering.
Used by the Stars billing handlers, the Mini App checkout and the subscription arithmetic in the database layer.
"""
from typing import Dict, Any

# Higher rank = more features. "free" covers the trial and the paid private-mode unlock.
TIER_RANK: Dict[str, int] = {"free": 0, "pro": 1, "vip": 2}

# Channel pair limits per active tier
TIER_MAX_CHANNELS: Dict[str, int] = {"free": 1, "pro": 5, "vip": 999}

TRIAL_DAYS = 14

PLANS: Dict[str, Dict[str, Any]] = {
    "pro": {
        "tier": "pro",
        "title": "Pro Tarif (30 kun)",
        "description": "5 ta kanal, AI Content Paraphraser, Video Watermark, Dynamic CTA, Avto-tarjima, Referal almashtirgich.",
        "stars": 100,
        "days": 30,
    },
    "vip": {
        "tier": "vip",
        "title": "VIP Cheksiz Tarif (30 kun)",
        "description": "Ko'chmas Mulk Auto-Story Cloner, cheksiz kanallar, Telegram Premium animatsion emojilar, himoyalangan kanallar, AI Paraphraser, Video Watermark.",
        "stars": 300,
        "days": 30,
    },
    "private_50": {
        "tier": "free",
        "title": "14 Kunlik Kirish (Private Unlock)",
        "description": "Yopiq botdan 14 kun davomida to'liq foydalanish va sinov muddati huquqi.",
        "stars": 50,
        "days": 14,
    },
}


def daily_price(tier: str) -> float:
    """Stars per day of a paid tier (used to convert remaining time when switching tiers)."""
    plan = PLANS.get(tier)
    if not plan or plan["tier"] not in ("pro", "vip"):
        return 0.0
    return plan["stars"] / float(plan["days"])
