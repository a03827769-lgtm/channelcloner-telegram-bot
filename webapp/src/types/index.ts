export type ActiveTab = 'dashboard' | 'channels' | 'story' | 'backfill' | 'billing' | 'system';

export interface User {
  id: number;
  full_name: string;
  username?: string;
  is_admin: boolean;
  created_at?: string;
}

export interface Subscription {
  tier: 'free' | 'pro' | 'vip';
  is_active: boolean;
  is_vip: boolean;
  max_channels: number;
  trial_expires_at?: string;
  expires_at?: string;
  stars_spent: number;
}

export interface SummaryStats {
  channel_pairs_count: number;
  total_cloned_messages: number;
  success_rate: number;
}

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
  image_watermark_type: 'none' | 'text' | 'logo';
  image_watermark_text: string;
  image_watermark_pos: string;
  video_watermark_type: 'none' | 'text' | 'logo';
  video_watermark_text: string;
  video_watermark_pos: string;
  drip_delay_minutes: number;
  night_mode: string;
  ai_paraphrase_mode: string;
  tone_of_voice: string;
  ad_action: string;
  source_topic_id?: number | null;
  target_topic_id?: number | null;
  created_at?: string;
}

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
  background_style: string;
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
  status: 'pending' | 'ready' | 'sent' | 'skipped';
  scheduled_at: string;
}

export interface AudioTrack {
  filename: string;
  title: string;
  genre: string;
  duration: string;
}

export interface SystemTelemetry {
  mtproto_connected: boolean;
  mtproto_status: string;
  keep_alive_port: number;
  db_type: string;
  db_size_mb: number;
  ram_mb: number;
  php_version: string;
  server_time: string;
}
