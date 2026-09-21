-- ============================================================
-- ChannelCloner Pro — Seed & Initial Production Data
-- ============================================================

SET NAMES utf8mb4;

-- Initial Demo / Admin User (1110001)
INSERT INTO `users` (`user_id`, `full_name`, `username`, `is_admin`, `is_blocked`)
VALUES (1110001, 'ChannelCloner Administrator', 'klonlaadmin', 1, 0)
ON DUPLICATE KEY UPDATE `full_name` = VALUES(`full_name`), `is_admin` = 1;

-- VIP Subscription for Admin / Demo
INSERT INTO `subscriptions` (`user_id`, `tier`, `expires_at`, `trial_expires_at`, `trial_notified`, `paid_notified`, `stars_spent`)
VALUES (1110001, 'vip', DATE_ADD(NOW(), INTERVAL 365 DAY), NULL, 0, 0, 300)
ON DUPLICATE KEY UPDATE `tier` = 'vip', `expires_at` = VALUES(`expires_at`);

-- Demo Channel Pairs
INSERT INTO `channel_pairs` (
    `id`, `user_id`, `source_channel`, `source_title`, `source_id`, 
    `target_channel`, `target_title`, `target_id`, `is_active`, `clean_links`, 
    `custom_signature`, `clone_mode`, `auto_translate`, `target_lang`, 
    `image_watermark_type`, `image_watermark_text`, `image_watermark_pos`, 
    `drip_delay_minutes`, `ai_paraphrase_mode`, `ad_action`
) VALUES 
(1, 1110001, '@tashkent_news_source', 'Toshkent Yangiliklari', -1001234567890, 
 '@my_new_uz_channel', 'Mening Yangi Kanalim', -1009876543210, 1, 1, 
 '👉 Bizning kanal: @my_new_uz_channel', 'clean', 0, 'uz', 
 'text', '@my_new_uz_channel', 'bottom_right', 0, 'off', 'clean'),
(2, 1110001, '@auto_elonlar_tashkent', 'Toshkent Avto E\'lonlar', -1001122334455, 
 '@toshkent_premium_avto', 'Toshkent Premium Avto', -1005544332211, 1, 1, 
 '🚘 Rasmiy hamkor: @toshkent_premium_avto', 'clean', 0, 'uz', 
 'text', 'PREMIUM AVTO', 'bottom_right', 5, 'short', 'drop')
ON DUPLICATE KEY UPDATE `id` = `id`;

-- Initial Story Settings
INSERT INTO `story_settings` (
    `user_id`, `source_channel`, `source_title`, `target_type`, `target_channel`, 
    `min_price`, `max_price`, `require_photos`, `require_price`, `filter_demands`, 
    `background_style`, `is_active`, `prime_hours_enabled`, `prime_hours_start`, 
    `prime_hours_end`, `drip_delay_minutes`, `max_stories_per_day`, 
    `enable_smart_badges`, `pin_to_profile`, `video_duration`, `enable_ai_voice`
) VALUES (
    1110001, '@tashkent_realestate_lux', 'Toshkent Hashamatli Ko\'chmas Mulk', 'self', '',
    700.00, 0.00, 1, 1, 1,
    'telegram_green', 1, 1, 9,
    22, 45, 5,
    1, 1, 25, 1
) ON DUPLICATE KEY UPDATE `min_price` = 700.00;

-- Initial Key-Value Settings
INSERT INTO `bot_settings` (`key_name`, `value_text`) VALUES
('miniapp_version', '2.0.0-pro'),
('maintenance_mode', 'false'),
('default_trial_days', '14'),
('stars_pro_monthly', '100'),
('stars_vip_monthly', '300')
ON DUPLICATE KEY UPDATE `value_text` = VALUES(`value_text`);
