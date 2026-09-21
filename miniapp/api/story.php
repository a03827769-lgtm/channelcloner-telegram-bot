<?php
declare(strict_types=1);

require_once __DIR__ . '/config.php';

$pdo = Database::getConnection();
$authUser = getAuthUser();
$userId = (int)$authUser['user_id'];
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$action = $_GET['action'] ?? 'settings';

// Check VIP status
$subStmt = $pdo->prepare("SELECT * FROM `subscriptions` WHERE `user_id` = :uid LIMIT 1");
$subStmt->execute([':uid' => $userId]);
$sub = $subStmt->fetch();
$isVip = ($sub && $sub['tier'] === 'vip');

// 1. Story Settings
if ($action === 'settings') {
    if ($method === 'GET') {
        $stmt = $pdo->prepare("SELECT * FROM `story_settings` WHERE `user_id` = :uid LIMIT 1");
        $stmt->execute([':uid' => $userId]);
        $settings = $stmt->fetch();

        if (!$settings) {
            // Default VIP Story Settings
            $settings = [
                'user_id' => $userId,
                'source_channel' => '@tashkent_realestate_lux',
                'source_title' => 'Toshkent Hashamatli Ko\'chmas Mulk',
                'target_type' => 'self',
                'target_channel' => '',
                'min_price' => 700.0,
                'max_price' => 0.0,
                'require_photos' => 1,
                'require_price' => 1,
                'filter_demands' => 1,
                'background_style' => 'telegram_green',
                'is_active' => 1,
                'prime_hours_enabled' => 1,
                'prime_hours_start' => 9,
                'prime_hours_end' => 22,
                'drip_delay_minutes' => 45,
                'max_stories_per_day' => 5,
                'enable_smart_badges' => 1,
                'pin_to_profile' => 1,
                'video_duration' => 25,
                'enable_ai_voice' => 1
            ];
        }

        jsonResponse([
            'ok' => true,
            'is_vip' => $isVip,
            'settings' => [
                'user_id' => (int)$settings['user_id'],
                'source_channel' => $settings['source_channel'],
                'source_title' => $settings['source_title'],
                'target_type' => $settings['target_type'],
                'target_channel' => $settings['target_channel'],
                'min_price' => (float)$settings['min_price'],
                'max_price' => (float)$settings['max_price'],
                'require_photos' => (bool)$settings['require_photos'],
                'require_price' => (bool)$settings['require_price'],
                'filter_demands' => (bool)$settings['filter_demands'],
                'background_style' => $settings['background_style'],
                'is_active' => (bool)$settings['is_active'],
                'prime_hours_enabled' => (bool)$settings['prime_hours_enabled'],
                'prime_hours_start' => (int)$settings['prime_hours_start'],
                'prime_hours_end' => (int)$settings['prime_hours_end'],
                'drip_delay_minutes' => (int)$settings['drip_delay_minutes'],
                'max_stories_per_day' => (int)$settings['max_stories_per_day'],
                'enable_smart_badges' => (bool)$settings['enable_smart_badges'],
                'pin_to_profile' => (bool)$settings['pin_to_profile'],
                'video_duration' => (int)$settings['video_duration'],
                'enable_ai_voice' => (bool)$settings['enable_ai_voice']
            ]
        ]);
    }

    if ($method === 'POST') {
        $body = getJsonBody();
        $checkStmt = $pdo->prepare("SELECT `id` FROM `story_settings` WHERE `user_id` = :uid LIMIT 1");
        $checkStmt->execute([':uid' => $userId]);
        $exists = $checkStmt->fetch();

        $params = [
            ':uid' => $userId,
            ':src' => $body['source_channel'] ?? '@tashkent_realestate_lux',
            ':src_title' => $body['source_title'] ?? 'Toshkent Hashamatli Ko\'chmas Mulk',
            ':tgt_type' => $body['target_type'] ?? 'self',
            ':tgt' => $body['target_channel'] ?? '',
            ':min_p' => (float)($body['min_price'] ?? 700.0),
            ':max_p' => (float)($body['max_price'] ?? 0.0),
            ':req_photo' => isset($body['require_photos']) ? (int)$body['require_photos'] : 1,
            ':req_price' => isset($body['require_price']) ? (int)$body['require_price'] : 1,
            ':demands' => isset($body['filter_demands']) ? (int)$body['filter_demands'] : 1,
            ':bg' => $body['background_style'] ?? 'telegram_green',
            ':act' => isset($body['is_active']) ? (int)$body['is_active'] : 1,
            ':prime_act' => isset($body['prime_hours_enabled']) ? (int)$body['prime_hours_enabled'] : 1,
            ':prime_s' => (int)($body['prime_hours_start'] ?? 9),
            ':prime_e' => (int)($body['prime_hours_end'] ?? 22),
            ':delay' => (int)($body['drip_delay_minutes'] ?? 45),
            ':max_day' => (int)($body['max_stories_per_day'] ?? 5),
            ':badges' => isset($body['enable_smart_badges']) ? (int)$body['enable_smart_badges'] : 1,
            ':pin' => isset($body['pin_to_profile']) ? (int)$body['pin_to_profile'] : 1,
            ':dur' => (int)($body['video_duration'] ?? 25),
            ':voice' => isset($body['enable_ai_voice']) ? (int)$body['enable_ai_voice'] : 1
        ];

        if ($exists) {
            $upd = $pdo->prepare("
                UPDATE `story_settings` SET
                    `source_channel` = :src,
                    `source_title` = :src_title,
                    `target_type` = :tgt_type,
                    `target_channel` = :tgt,
                    `min_price` = :min_p,
                    `max_price` = :max_p,
                    `require_photos` = :req_photo,
                    `require_price` = :req_price,
                    `filter_demands` = :demands,
                    `background_style` = :bg,
                    `is_active` = :act,
                    `prime_hours_enabled` = :prime_act,
                    `prime_hours_start` = :prime_s,
                    `prime_hours_end` = :prime_e,
                    `drip_delay_minutes` = :delay,
                    `max_stories_per_day` = :max_day,
                    `enable_smart_badges` = :badges,
                    `pin_to_profile` = :pin,
                    `video_duration` = :dur,
                    `enable_ai_voice` = :voice
                WHERE `user_id` = :uid
            ");
            $upd->execute($params);
        } else {
            $ins = $pdo->prepare("
                INSERT INTO `story_settings` (
                    `user_id`, `source_channel`, `source_title`, `target_type`, `target_channel`,
                    `min_price`, `max_price`, `require_photos`, `require_price`, `filter_demands`,
                    `background_style`, `is_active`, `prime_hours_enabled`, `prime_hours_start`,
                    `prime_hours_end`, `drip_delay_minutes`, `max_stories_per_day`,
                    `enable_smart_badges`, `pin_to_profile`, `video_duration`, `enable_ai_voice`
                ) VALUES (
                    :uid, :src, :src_title, :tgt_type, :tgt,
                    :min_p, :max_p, :req_photo, :req_price, :demands,
                    :bg, :act, :prime_act, :prime_s,
                    :prime_e, :delay, :max_day,
                    :badges, :pin, :dur, :voice
                )
            ");
            $ins->execute($params);
        }

        // Execute completed above

        jsonResponse(['ok' => true, 'message' => 'VIP Story sozlamalari muvaffaqiyatli saqlandi!']);
    }
}

// 2. Story Queue
if ($action === 'queue') {
    $stmt = $pdo->prepare("
        SELECT * FROM `story_queue` 
        WHERE `user_id` = :uid 
        ORDER BY `created_at` DESC 
        LIMIT 10
    ");
    $stmt->execute([':uid' => $userId]);
    $items = $stmt->fetchAll();

    // Removed mock data injection so empty queues return truly empty

    jsonResponse(['ok' => true, 'queue' => $items]);
}

errorResponse('Noto\'g\'ri parametrlar', 400);
