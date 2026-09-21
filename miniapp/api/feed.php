<?php
declare(strict_types=1);

require_once __DIR__ . '/config.php';

$pdo = Database::getConnection();
$authUser = getAuthUser();
$userId = (int)$authUser['user_id'];

// Get recent clones for the user
$stmt = $pdo->prepare("
    SELECT cm.*, cp.source_channel as cp_src, cp.target_channel as cp_tgt
    FROM `cloned_messages` cm
    JOIN `channel_pairs` cp ON cm.pair_id = cp.id
    WHERE cp.user_id = :uid
    ORDER BY cm.cloned_at DESC
    LIMIT 10
");
$stmt->execute([':uid' => $userId]);
$feed = $stmt->fetchAll();

jsonResponse([
    'ok' => true,
    'feed' => $feed
]);
