export type ActiveTab = 'dashboard' | 'channels' | 'story' | 'store' | 'backfill' | 'billing' | 'system';

export interface User {
  id: number;
  full_name: string;
  username?: string | null;
  is_admin: boolean;
  created_at?: string | null;
}

export type Tier = 'free' | 'pro' | 'vip';

export interface Subscription {
  tier: Tier;
  is_active: boolean;
  is_trial_active: boolean;
  is_vip: boolean;
  max_channels: number;
  trial_expires_at?: string | null;
  expires_at?: string | null;
  stars_spent: number;
}

export interface PlanInfo {
  key: 'pro' | 'vip';
  tier: Tier;
  title: string;
  stars: number;
  days: number;
  max_channels: number;
}

export interface BillingCatalog {
  trial_days: number;
  plans: PlanInfo[];
}

export interface DailyCount {
  date: string;
  count: number;
}

export interface SummaryStats {
  channel_pairs_count: number;
  active_pairs_count: number;
  total_cloned_messages: number;
  today_cloned_messages: number;
  daily: DailyCount[];
}

export interface FeedItem {
  id?: number;
  media_type?: string | null;
  cloned_at: string;
  cp_src?: string | null;
  cp_tgt?: string | null;
}

export type WatermarkType = 'none' | 'text' | 'logo';
export type NightMode = 'off' | 'silent' | 'buffer';
export type AdAction = 'clean' | 'drop' | 'swap' | 'off';

export interface ChannelPair {
  id: number;
  user_id: number;
  source_channel: string;
  source_title: string;
  target_channel: string;
  target_title: string;
  is_active: boolean;
  clone_mode: 'clean' | 'forward';
  clean_links: boolean;
  custom_signature: string;
  remove_signature: boolean;
  blacklist_words: string;
  replace_words: string;
  auto_translate: boolean;
  target_lang: string;
  source_lang: string;
  image_watermark_type: WatermarkType;
  image_watermark_text: string;
  image_watermark_pos: string;
  video_watermark_type: WatermarkType;
  video_watermark_text: string;
  video_watermark_pos: string;
  drip_delay_minutes: number;
  night_mode: NightMode;
  ai_paraphrase_mode: string;
  tone_of_voice: string;
  ad_action: AdAction;
  source_topic_id?: number | null;
  target_topic_id?: number | null;
  created_at?: string | null;
}

/** Body of POST /api/pairs */
export interface NewPairRequest {
  source_channel: string;
  source_title?: string;
  target_channel: string;
  clone_mode: 'clean' | 'forward';
  clean_links: boolean;
  auto_translate: boolean;
}

export type StoryBackground = 'telegram_green' | 'listing_blur' | 'luxury_dark' | 'emerald';

export interface StorySettings {
  user_id: number;
  source_channel: string;
  source_title: string;
  target_type: 'self' | 'channel';
  target_channel: string;
  min_price: number;
  max_price: number;
  require_photos: boolean;
  require_price: boolean;
  background_style: StoryBackground;
  is_active: boolean;
  prime_hours_enabled: boolean;
  prime_hours_start: number;
  prime_hours_end: number;
  drip_delay_minutes: number;
  max_stories_per_day: number;
  enable_smart_badges: boolean;
  pin_to_profile: boolean;
  video_duration: number;
  enable_ai_voice: boolean;
}

export interface StoryQueueItem {
  id: number;
  district: string;
  price: number;
  rooms: number;
  area: number;
  score: number;
  status: string;
  scheduled_at: string | null;
}

export interface StoryPostedItem {
  id: number;
  price: number;
  caption: string;
  status: string;
  posted_at: string | null;
}

export interface AudioTrack {
  filename: string;
  title: string;
  genre: string;
  duration: string;
}

/** Host details (port, database, memory, runtime) are only returned to administrators. */
export interface SystemTelemetry {
  mtproto_connected: boolean;
  mtproto_status: string;
  server_time: string;
  keep_alive_port?: number;
  db_type?: string;
  db_size_mb?: number;
  ram_mb?: number | null;
  runtime_version?: string;
}

export interface StoreProduct {
  id: number;
  name: string;
  category: string;
  type: string;
  /** Per-unit price, display only (fractional for per-1000 services) */
  price_stars: number;
  /** Quantity the catalogue price is shown for (1 or 1000) */
  display_quantity: number;
  /** Exact price charged for display_quantity units */
  display_price_stars: number;
  min_quantity: number;
  max_quantity: number;
  is_available: boolean;
  stock_status: 'in_stock' | 'out_of_stock';
  description?: string | null;
}

export interface StoreQuote {
  product_id: number;
  quantity: number;
  price_stars: number;
  min_quantity: number;
  max_quantity: number;
}

export interface StoreOrder {
  id: number;
  product_id: number;
  product_name: string;
  quantity: number;
  price_stars: number;
  target_link: string;
  status: 'awaiting_payment' | 'paid' | 'completed' | 'pending_admin' | 'failed';
  created_at?: string | null;
}
