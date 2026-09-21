-- ============================================================
-- ChannelCloner Pro — MySQL 8.0 Enterprise Relational Schema
-- High-Load, ACID-Compliant, UTF8MB4 Full Unicode Support
-- ============================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- 1. Users Table
CREATE TABLE IF NOT EXISTS `users` (
    `user_id` BIGINT NOT NULL PRIMARY KEY,
    `full_name` VARCHAR(255) NOT NULL,
    `username` VARCHAR(255) DEFAULT NULL,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `is_admin` TINYINT(1) NOT NULL DEFAULT 0,
    `is_blocked` TINYINT(1) NOT NULL DEFAULT 0,
    INDEX `idx_users_admin` (`is_admin`),
    INDEX `idx_users_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. Subscriptions Table
CREATE TABLE IF NOT EXISTS `subscriptions` (
    `user_id` BIGINT NOT NULL PRIMARY KEY,
    `tier` VARCHAR(32) NOT NULL DEFAULT 'free',
    `expires_at` DATETIME DEFAULT NULL,
    `trial_expires_at` DATETIME DEFAULT NULL,
    `trial_notified` TINYINT(1) NOT NULL DEFAULT 0,
    `paid_notified` TINYINT(1) NOT NULL DEFAULT 0,
    `stars_spent` INT NOT NULL DEFAULT 0,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_sub_tier` (`tier`),
    INDEX `idx_sub_exp` (`expires_at`),
    CONSTRAINT `fk_sub_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. Payments Table (Telegram Stars & Billing)
CREATE TABLE IF NOT EXISTS `payments` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `user_id` BIGINT NOT NULL,
    `telegram_payment_charge_id` VARCHAR(255) NOT NULL DEFAULT '',
    `amount` INT NOT NULL DEFAULT 0,
    `tier` VARCHAR(32) NOT NULL DEFAULT 'pro',
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_pay_user` (`user_id`),
    INDEX `idx_pay_charge` (`telegram_payment_charge_id`),
    CONSTRAINT `fk_pay_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 4. Channel Pairs Table
CREATE TABLE IF NOT EXISTS `channel_pairs` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `user_id` BIGINT NOT NULL,
    `source_channel` VARCHAR(255) NOT NULL,
    `source_title` VARCHAR(255) NOT NULL DEFAULT '',
    `source_id` BIGINT DEFAULT NULL,
    `target_channel` VARCHAR(255) NOT NULL,
    `target_title` VARCHAR(255) NOT NULL DEFAULT '',
    `target_id` BIGINT DEFAULT NULL,
    `is_active` TINYINT(1) NOT NULL DEFAULT 1,
    `clean_links` TINYINT(1) NOT NULL DEFAULT 1,
    `custom_signature` TEXT DEFAULT NULL,
    `remove_signature` TINYINT(1) NOT NULL DEFAULT 0,
    `blacklist_words` TEXT DEFAULT NULL,
    `replace_words` TEXT DEFAULT NULL,
    `clone_mode` VARCHAR(32) NOT NULL DEFAULT 'clean',
    
    -- Advanced 5 Killer-Features Settings
    `auto_translate` TINYINT(1) NOT NULL DEFAULT 0,
    `target_lang` VARCHAR(10) NOT NULL DEFAULT 'uz',
    `source_lang` VARCHAR(10) NOT NULL DEFAULT 'auto',
    `image_watermark_type` VARCHAR(32) NOT NULL DEFAULT 'none',
    `image_watermark_text` VARCHAR(255) NOT NULL DEFAULT '',
    `image_watermark_pos` VARCHAR(32) NOT NULL DEFAULT 'bottom_right',
    `is_protected_source` TINYINT(1) NOT NULL DEFAULT 0,
    `affiliate_rules` TEXT DEFAULT NULL,
    `auto_premium_emojis` TINYINT(1) NOT NULL DEFAULT 0,
    
    -- Next-Gen Tier-1 Features
    `video_watermark_type` VARCHAR(32) NOT NULL DEFAULT 'none',
    `video_watermark_text` VARCHAR(255) NOT NULL DEFAULT '',
    `video_watermark_pos` VARCHAR(32) NOT NULL DEFAULT 'bottom_right',
    `drip_delay_minutes` INT NOT NULL DEFAULT 0,
    `night_mode` VARCHAR(32) NOT NULL DEFAULT 'off',
    `ai_paraphrase_mode` VARCHAR(32) NOT NULL DEFAULT 'off',
    `tone_of_voice` VARCHAR(32) NOT NULL DEFAULT 'standard',
    `enable_invisible_watermark` TINYINT(1) NOT NULL DEFAULT 1,
    `auto_cta_buttons` TINYINT(1) NOT NULL DEFAULT 0,
    `backup_enabled` TINYINT(1) NOT NULL DEFAULT 1,
    `last_seen_msg_id` BIGINT DEFAULT NULL,
    `auto_catchup` TINYINT(1) NOT NULL DEFAULT 1,
    
    -- Forum Topics & AI Ad Shield
    `source_topic_id` INT DEFAULT NULL,
    `target_topic_id` INT DEFAULT NULL,
    `ad_action` VARCHAR(32) NOT NULL DEFAULT 'clean',
    `show_caption_above` TINYINT(1) NOT NULL DEFAULT 0,
    
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_cp_user` (`user_id`),
    INDEX `idx_cp_active` (`is_active`),
    INDEX `idx_cp_source_id` (`source_id`),
    CONSTRAINT `fk_cp_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5. Cloned Messages Ledger
CREATE TABLE IF NOT EXISTS `cloned_messages` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `pair_id` INT NOT NULL,
    `source_msg_id` BIGINT NOT NULL,
    `target_msg_id` BIGINT DEFAULT NULL,
    `media_group_id` VARCHAR(128) DEFAULT NULL,
    `media_type` VARCHAR(64) NOT NULL DEFAULT 'text',
    `source_channel` VARCHAR(255) DEFAULT NULL,
    `target_channel` VARCHAR(255) DEFAULT NULL,
    `story_id` INT DEFAULT NULL,
    `status` VARCHAR(32) NOT NULL DEFAULT 'active',
    `price` DECIMAL(12, 2) NOT NULL DEFAULT 0.00,
    `last_caption` MEDIUMTEXT DEFAULT NULL,
    `cloned_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_cm_pair` (`pair_id`),
    INDEX `idx_cm_src_lookup` (`source_channel`, `source_msg_id`),
    INDEX `idx_cm_media_group` (`media_group_id`),
    INDEX `idx_cm_cloned_at` (`cloned_at`),
    CONSTRAINT `fk_cm_pair` FOREIGN KEY (`pair_id`) REFERENCES `channel_pairs` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 6. Drip Feed Queue
CREATE TABLE IF NOT EXISTS `drip_queue` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `pair_id` INT NOT NULL,
    `msg_data_json` LONGTEXT NOT NULL,
    `scheduled_at` DATETIME NOT NULL,
    `status` VARCHAR(32) NOT NULL DEFAULT 'pending',
    `error_message` TEXT DEFAULT NULL,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_dq_status_sched` (`status`, `scheduled_at`),
    CONSTRAINT `fk_dq_pair` FOREIGN KEY (`pair_id`) REFERENCES `channel_pairs` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 7. Story Settings (VIP Real Estate Cloner)
CREATE TABLE IF NOT EXISTS `story_settings` (
    `user_id` BIGINT NOT NULL PRIMARY KEY,
    `source_channel` VARCHAR(255) NOT NULL DEFAULT '',
    `source_title` VARCHAR(255) NOT NULL DEFAULT '',
    `source_id` BIGINT DEFAULT NULL,
    `target_type` VARCHAR(32) NOT NULL DEFAULT 'self',
    `target_channel` VARCHAR(255) NOT NULL DEFAULT '',
    `target_id` BIGINT DEFAULT NULL,
    `min_price` DECIMAL(10, 2) NOT NULL DEFAULT 700.00,
    `max_price` DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
    `require_photos` TINYINT(1) NOT NULL DEFAULT 1,
    `require_price` TINYINT(1) NOT NULL DEFAULT 1,
    `filter_demands` TINYINT(1) NOT NULL DEFAULT 1,
    `background_style` VARCHAR(64) NOT NULL DEFAULT 'telegram_green',
    `is_active` TINYINT(1) NOT NULL DEFAULT 1,
    `prime_hours_enabled` TINYINT(1) NOT NULL DEFAULT 1,
    `prime_hours_start` INT NOT NULL DEFAULT 9,
    `prime_hours_end` INT NOT NULL DEFAULT 22,
    `drip_delay_minutes` INT NOT NULL DEFAULT 45,
    `max_stories_per_day` INT NOT NULL DEFAULT 5,
    `enable_smart_badges` TINYINT(1) NOT NULL DEFAULT 1,
    `pin_to_profile` TINYINT(1) NOT NULL DEFAULT 1,
    `video_duration` INT NOT NULL DEFAULT 25,
    `enable_ai_voice` TINYINT(1) NOT NULL DEFAULT 1,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT `fk_ss_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 8. Story Source Channels
CREATE TABLE IF NOT EXISTS `story_source_channels` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `user_id` BIGINT NOT NULL,
    `channel_username` VARCHAR(255) NOT NULL,
    `channel_title` VARCHAR(255) NOT NULL DEFAULT '',
    `channel_id` BIGINT DEFAULT NULL,
    `is_active` TINYINT(1) NOT NULL DEFAULT 1,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_ssc_user` (`user_id`),
    CONSTRAINT `fk_ssc_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 9. Story Queue
CREATE TABLE IF NOT EXISTS `story_queue` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `user_id` BIGINT NOT NULL,
    `source_channel` VARCHAR(255) NOT NULL,
    `source_msg_id` BIGINT NOT NULL,
    `price` DECIMAL(10, 2) DEFAULT NULL,
    `district` VARCHAR(255) NOT NULL DEFAULT '',
    `rooms` INT DEFAULT NULL,
    `area` DECIMAL(10, 2) DEFAULT NULL,
    `score` INT NOT NULL DEFAULT 0,
    `payload_json` LONGTEXT,
    `status` VARCHAR(32) NOT NULL DEFAULT 'pending',
    `scheduled_at` DATETIME DEFAULT NULL,
    `created_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `error_message` TEXT DEFAULT NULL,
    INDEX `idx_sq_sched` (`scheduled_at`, `status`),
    INDEX `idx_sq_user` (`user_id`),
    CONSTRAINT `fk_sq_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 10. Posted Stories History
CREATE TABLE IF NOT EXISTS `posted_stories` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `user_id` BIGINT NOT NULL,
    `source_channel` VARCHAR(255) NOT NULL DEFAULT '',
    `source_id` BIGINT DEFAULT NULL,
    `source_msg_id` BIGINT NOT NULL,
    `grouped_id` BIGINT DEFAULT NULL,
    `story_id` BIGINT DEFAULT NULL,
    `price` DECIMAL(10, 2) DEFAULT NULL,
    `caption_snippet` TEXT,
    `target_type` VARCHAR(32) NOT NULL DEFAULT 'self',
    `posted_at` DATETIME DEFAULT CURRENT_TIMESTAMP,
    `status` VARCHAR(32) NOT NULL DEFAULT 'success',
    INDEX `idx_ps_user` (`user_id`),
    INDEX `idx_ps_posted_at` (`posted_at`),
    CONSTRAINT `fk_ps_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 11. Bot Settings & Key-Value Configuration
CREATE TABLE IF NOT EXISTS `bot_settings` (
    `key_name` VARCHAR(128) NOT NULL PRIMARY KEY,
    `value_text` LONGTEXT,
    `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;
