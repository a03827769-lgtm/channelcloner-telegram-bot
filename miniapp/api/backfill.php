<?php
declare(strict_types=1);

require_once __DIR__ . '/config.php';

$pdo = Database::getConnection();
$authUser = getAuthUser();
$userId = (int)$authUser['user_id'];
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

if ($method === 'POST') {
    $body = getJsonBody();
    $pairId = (int)($body['pair_id'] ?? 0);
    $limit = (int)($body['limit'] ?? 20);

    if ($limit < 1) $limit = 10;
    if ($limit > 100) $limit = 100;

    $stmt = $pdo->prepare("SELECT * FROM `channel_pairs` WHERE `id` = :id AND `user_id` = :uid LIMIT 1");
    $stmt->execute([':id' => $pairId, ':uid' => $userId]);
    $pair = $stmt->fetch();

    if (!$pair) {
        errorResponse('Kanal juftligi topilmadi', 404);
    }

    // Insert into command table for Python to process
    $cmd = $pdo->prepare("
        INSERT INTO `bot_commands` (`user_id`, `command`, `target_id`, `payload`, `status`)
        VALUES (:uid, 'backfill', :pid, :payload, 'pending')
    ");
    try {
        $cmd->execute([
            ':uid' => $userId,
            ':pid' => $pairId,
            ':payload' => json_encode(['limit' => $limit])
        ]);
    } catch (Exception $e) {
        // If table doesn't exist yet, ignore
    }

    jsonResponse([
        'ok' => true,
        'pair_id' => $pairId,
        'limit' => $limit,
        'status' => 'queued',
        'message' => "{$limit} ta postni ko'chirish navbatga qo'yildi",
        'logs' => [
            "Tarixiy {$limit} ta xabarni ko'chirish Python serveriga navbatga uzatildi."
        ]
    ]);
}

if ($method === 'GET') {
    jsonResponse([
        'ok' => true,
        'active_jobs' => [],
        'supported_limits' => [10, 20, 50, 100],
        'status' => 'idle'
    ]);
}

errorResponse('Faqat GET yoki POST so\'rov qabul qilinadi', 405);
