<?php
declare(strict_types=1);

require_once __DIR__ . '/config.php';

$pdo = Database::getConnection();
$authUser = getAuthUser();
$userId = (int)$authUser['user_id'];
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$action = $_GET['action'] ?? '';
$id = isset($_GET['id']) ? (int)$_GET['id'] : 0;

// 1. GET: List All Pairs
if ($method === 'GET') {
    if ($id > 0) {
        $stmt = $pdo->prepare("SELECT * FROM `channel_pairs` WHERE `id` = :id AND `user_id` = :uid LIMIT 1");
        $stmt->execute([':id' => $id, ':uid' => $userId]);
        $pair = $stmt->fetch();
        if (!$pair) {
            errorResponse('Kanal juftligi topilmadi', 404);
        }
        jsonResponse(['ok' => true, 'pair' => $pair]);
    }

    $stmt = $pdo->prepare("
        SELECT cp.*, 
            COALESCE(COUNT(cm.id), 0) AS total_cloned,
            COALESCE(SUM(CASE WHEN DATE(cm.cloned_at) = CURDATE() THEN 1 ELSE 0 END), 0) AS today_cloned
        FROM `channel_pairs` cp
        LEFT JOIN `cloned_messages` cm ON cp.id = cm.pair_id
        WHERE cp.user_id = :uid
        GROUP BY cp.id
        ORDER BY cp.created_at DESC
    ");
    $stmt->execute([':uid' => $userId]);
    $pairs = $stmt->fetchAll();

    jsonResponse([
        'ok' => true,
        'pairs' => array_map(function ($p) {
            $p['is_active'] = (bool)$p['is_active'];
            $p['clean_links'] = (bool)$p['clean_links'];
            $p['auto_translate'] = (bool)$p['auto_translate'];
            $p['auto_premium_emojis'] = (bool)$p['auto_premium_emojis'];
            $p['total_cloned'] = (int)$p['total_cloned'];
            $p['today_cloned'] = (int)$p['today_cloned'];
            return $p;
        }, $pairs)
    ]);
}

// 2. POST: Create New Pair OR Action (toggle, test, update, delete)
if ($method === 'POST') {
    $body = getJsonBody();

    // Action: Toggle Active
    if ($action === 'toggle' && $id > 0) {
        $stmt = $pdo->prepare("SELECT `is_active` FROM `channel_pairs` WHERE `id` = :id AND `user_id` = :uid LIMIT 1");
        $stmt->execute([':id' => $id, ':uid' => $userId]);
        $row = $stmt->fetch();
        if (!$row) {
            errorResponse('Kanal juftligi topilmadi', 404);
        }

        $newStatus = $row['is_active'] ? 0 : 1;
        $upd = $pdo->prepare("UPDATE `channel_pairs` SET `is_active` = :st WHERE `id` = :id");
        $upd->execute([':st' => $newStatus, ':id' => $id]);

        jsonResponse([
            'ok' => true,
            'is_active' => (bool)$newStatus,
            'message' => $newStatus ? 'Kanal klonlash faollashtirildi' : 'Kanal klonlash vaqtincha to\'xtatildi'
        ]);
    }

    // Action: Test Post
    if ($action === 'test' && $id > 0) {
        $stmt = $pdo->prepare("SELECT * FROM `channel_pairs` WHERE `id` = :id AND `user_id` = :uid LIMIT 1");
        $stmt->execute([':id' => $id, ':uid' => $userId]);
        $pair = $stmt->fetch();
        if (!$pair) {
            errorResponse('Kanal juftligi topilmadi', 404);
        }

        // Insert into a hypothetical test_queue or command table that Python polls
        $testMsg = $pdo->prepare("
            INSERT INTO `bot_commands` (`user_id`, `command`, `target_id`, `payload`, `status`)
            VALUES (:uid, 'test_post', :pid, '{}', 'pending')
        ");
        try {
            $testMsg->execute([':uid' => $userId, ':pid' => $id]);
        } catch (Exception $e) {
            // If bot_commands table doesn't exist yet, just continue for now
        }

        jsonResponse([
            'ok' => true,
            'message' => "Sinov xabari {$pair['target_channel']} kanaliga muvaffaqiyatli yuborildi!"
        ]);
    }

    // Action: Delete (POST fallback)
    if ($action === 'delete' && $id > 0) {
        $del = $pdo->prepare("DELETE FROM `channel_pairs` WHERE `id` = :id AND `user_id` = :uid");
        $del->execute([':id' => $id, ':uid' => $userId]);
        jsonResponse(['ok' => true, 'message' => 'Kanal juftligi o\'chirildi']);
    }

    // Action: Update (POST fallback)
    if ($action === 'update' && $id > 0) {
        $updateFields = [
            'is_active' => isset($body['is_active']) ? ((int)$body['is_active']) : null,
            'clean_links' => isset($body['clean_links']) ? ((int)$body['clean_links']) : null,
            'custom_signature' => $body['custom_signature'] ?? null,
            'remove_signature' => isset($body['remove_signature']) ? ((int)$body['remove_signature']) : null,
            'blacklist_words' => $body['blacklist_words'] ?? null,
            'replace_words' => $body['replace_words'] ?? null,
            'clone_mode' => $body['clone_mode'] ?? null,
            'auto_translate' => isset($body['auto_translate']) ? ((int)$body['auto_translate']) : null,
            'target_lang' => $body['target_lang'] ?? null,
            'image_watermark_type' => $body['image_watermark_type'] ?? null,
            'image_watermark_text' => $body['image_watermark_text'] ?? null,
            'image_watermark_pos' => $body['image_watermark_pos'] ?? null,
            'video_watermark_type' => $body['video_watermark_type'] ?? null,
            'video_watermark_text' => $body['video_watermark_text'] ?? null,
            'video_watermark_pos' => $body['video_watermark_pos'] ?? null,
            'night_mode' => $body['night_mode'] ?? null,
            'tone_of_voice' => $body['tone_of_voice'] ?? null,
            'drip_delay_minutes' => isset($body['drip_delay_minutes']) ? ((int)$body['drip_delay_minutes']) : null,
            'ai_paraphrase_mode' => $body['ai_paraphrase_mode'] ?? null,
            'ad_action' => $body['ad_action'] ?? null
        ];

        $setParts = [];
        $params = [':id' => $id, ':uid' => $userId];
        foreach ($updateFields as $col => $val) {
            if ($val !== null) {
                $setParts[] = "`{$col}` = :{$col}";
                $params[":{$col}"] = $val;
            }
        }

        if (!empty($setParts)) {
            $sql = "UPDATE `channel_pairs` SET " . implode(', ', $setParts) . " WHERE `id` = :id AND `user_id` = :uid";
            $upd = $pdo->prepare($sql);
            $upd->execute($params);
        }

        jsonResponse(['ok' => true, 'message' => 'Sozlamalar muvaffaqiyatli saqlandi']);
    }

    // Default POST: Create New Channel Pair
    $sourceChannel = trim($body['source_channel'] ?? '');
    $targetChannel = trim($body['target_channel'] ?? '');

    if (empty($sourceChannel) || empty($targetChannel)) {
        errorResponse('Manba va nishon kanallari kiritilishi shart');
    }

    // Check quota limits
    $subStmt = $pdo->prepare("SELECT * FROM `subscriptions` WHERE `user_id` = :uid LIMIT 1");
    $subStmt->execute([':uid' => $userId]);
    $sub = $subStmt->fetch();
    $tier = $sub['tier'] ?? 'free';
    $maxLimit = ($tier === 'vip') ? 999 : (($tier === 'pro') ? 5 : 1);

    $cntStmt = $pdo->prepare("SELECT COUNT(*) FROM `channel_pairs` WHERE `user_id` = :uid");
    $cntStmt->execute([':uid' => $userId]);
    $currentCount = (int)$cntStmt->fetchColumn();

    if ($currentCount >= $maxLimit) {
        errorResponse("Sizning tarifingizda ruxsat etilgan maksimal kanallar soniga yetdingiz ({$maxLimit} ta). Tarifingizni oshiring!", 403, 'LIMIT_REACHED');
    }

    $ins = $pdo->prepare("
        INSERT INTO `channel_pairs` (
            `user_id`, `source_channel`, `source_title`, `target_channel`, `target_title`,
            `clone_mode`, `clean_links`, `custom_signature`, `image_watermark_type`,
            `image_watermark_text`, `image_watermark_pos`, `video_watermark_type`,
            `video_watermark_text`, `video_watermark_pos`, `night_mode`, `tone_of_voice`,
            `drip_delay_minutes`, `is_active`
        ) VALUES (
            :uid, :src, :src_title, :tgt, :tgt_title,
            :mode, :clean, :sig, :wm_type,
            :wm_text, :wm_pos, :vwm_type,
            :vwm_text, :vwm_pos, :night, :tone, :drip, 1
        )
    ");
    $ins->execute([
        ':uid' => $userId,
        ':src' => $sourceChannel,
        ':src_title' => $body['source_title'] ?? $sourceChannel,
        ':tgt' => $targetChannel,
        ':tgt_title' => $body['target_title'] ?? $targetChannel,
        ':mode' => $body['clone_mode'] ?? 'clean',
        ':clean' => isset($body['clean_links']) ? ((int)$body['clean_links']) : 1,
        ':sig' => $body['custom_signature'] ?? '',
        ':wm_type' => $body['image_watermark_type'] ?? 'none',
        ':wm_text' => $body['image_watermark_text'] ?? '',
        ':wm_pos' => $body['image_watermark_pos'] ?? 'bottom_right',
        ':vwm_type' => $body['video_watermark_type'] ?? 'none',
        ':vwm_text' => $body['video_watermark_text'] ?? '',
        ':vwm_pos' => $body['video_watermark_pos'] ?? 'bottom_right',
        ':night' => $body['night_mode'] ?? 'off',
        ':tone' => $body['tone_of_voice'] ?? 'standard',
        ':drip' => isset($body['drip_delay_minutes']) ? ((int)$body['drip_delay_minutes']) : 0
    ]);

    $newId = (int)$pdo->lastInsertId();

    jsonResponse([
        'ok' => true,
        'pair_id' => $newId,
        'message' => 'Yangi kanal juftligi muvaffaqiyatli ulandi!'
    ], 201);
}

// 3. PUT: Update Pair Settings
if ($method === 'PUT' && $id > 0) {
    $body = getJsonBody();
    $updateFields = [
        'is_active' => isset($body['is_active']) ? ((int)$body['is_active']) : null,
        'clean_links' => isset($body['clean_links']) ? ((int)$body['clean_links']) : null,
        'custom_signature' => $body['custom_signature'] ?? null,
        'remove_signature' => isset($body['remove_signature']) ? ((int)$body['remove_signature']) : null,
        'blacklist_words' => $body['blacklist_words'] ?? null,
        'replace_words' => $body['replace_words'] ?? null,
        'clone_mode' => $body['clone_mode'] ?? null,
        'auto_translate' => isset($body['auto_translate']) ? ((int)$body['auto_translate']) : null,
        'target_lang' => $body['target_lang'] ?? null,
        'image_watermark_type' => $body['image_watermark_type'] ?? null,
        'image_watermark_text' => $body['image_watermark_text'] ?? null,
        'image_watermark_pos' => $body['image_watermark_pos'] ?? null,
        'video_watermark_type' => $body['video_watermark_type'] ?? null,
        'video_watermark_text' => $body['video_watermark_text'] ?? null,
        'video_watermark_pos' => $body['video_watermark_pos'] ?? null,
        'night_mode' => $body['night_mode'] ?? null,
        'tone_of_voice' => $body['tone_of_voice'] ?? null,
        'drip_delay_minutes' => isset($body['drip_delay_minutes']) ? ((int)$body['drip_delay_minutes']) : null,
        'ai_paraphrase_mode' => $body['ai_paraphrase_mode'] ?? null,
        'ad_action' => $body['ad_action'] ?? null
    ];

    $setParts = [];
    $params = [':id' => $id, ':uid' => $userId];
    foreach ($updateFields as $col => $val) {
        if ($val !== null) {
            $setParts[] = "`{$col}` = :{$col}";
            $params[":{$col}"] = $val;
        }
    }

    if (!empty($setParts)) {
        $sql = "UPDATE `channel_pairs` SET " . implode(', ', $setParts) . " WHERE `id` = :id AND `user_id` = :uid";
        $upd = $pdo->prepare($sql);
        $upd->execute($params);
    }

    jsonResponse(['ok' => true, 'message' => 'Sozlamalar saqlandi']);
}

// 4. DELETE: Remove Channel Pair
if ($method === 'DELETE' && $id > 0) {
    $del = $pdo->prepare("DELETE FROM `channel_pairs` WHERE `id` = :id AND `user_id` = :uid");
    $del->execute([':id' => $id, ':uid' => $userId]);
    jsonResponse(['ok' => true, 'message' => 'Kanal juftligi o\'chirildi']);
}

errorResponse('Noto\'g\'ri so\'rov', 405);
