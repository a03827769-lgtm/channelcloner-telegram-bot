"""
Input and workload limits shared by the bot UI and the Mini App API, so both entry points enforce the
same rules (text sizes, backfill sizes, story settings ranges).
"""

# Per-pair text settings (characters)
SIGNATURE_MAX_CHARS = 1024
BLACKLIST_MAX_CHARS = 4000
REPLACE_WORDS_MAX_CHARS = 4000
AFFILIATE_RULES_MAX_CHARS = 4000
WATERMARK_TEXT_MAX_CHARS = 64

# History backfill (messages per run)
BACKFILL_MAX_MESSAGES = 200            # regular plans
BACKFILL_MAX_MESSAGES_PRIVILEGED = 1000  # VIP plan and admins

# Drip feed delay between queued posts (minutes)
DRIP_DELAY_MAX_MINUTES = 1440

# Story automation settings
STORY_VIDEO_DURATION_MIN = 15
STORY_VIDEO_DURATION_MAX = 40
STORY_MAX_PER_DAY_MIN = 1
# Telegram Premium accounts may post up to 100 stories a day; accounts without Premium are capped at
# 3 by the story service (NON_PREMIUM_DAILY_STORY_LIMIT)
STORY_MAX_PER_DAY_MAX = 100
STORY_DRIP_DELAY_MAX_MINUTES = 1440
