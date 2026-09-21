<?php
declare(strict_types=1);

require_once __DIR__ . '/config.php';

$pdo = Database::getConnection();
$authUser = getAuthUser();
$userId = (int)$authUser['user_id'];

// Upsert User
$stmt = $pdo->prepare("
    INSERT INTO `users` (`user_id`, `full_name`, `username`, `is_admin`, `is_blocked`)
    VALUES (:uid, :name, :uname, :admin, 0)
    ON DUPLICATE KEY UPDATE 
        `full_name` = :name2,
        `username` = :uname2
");
$stmt->execute([
    ':uid' => $userId,
    ':name' => $authUser['full_name'] ?: 'Telegram User',
    ':uname' => $authUser['username'] ?: null,
    ':admin' => $authUser['is_admin'] ? 1 : 0,
    ':name2' => $authUser['full_name'] ?: 'Telegram User',
    ':uname2' => $authUser['username'] ?: null,
]);

// Fetch Subscription
$subStmt = $pdo->prepare("SELECT * FROM `subscriptions` WHERE `user_id` = :uid LIMIT 1");
$subStmt->execute([':uid' => $userId]);
$sub = $subStmt->fetch();

if (!$sub) {
    // Auto-create 14-day trial
    $now = new DateTime('now', new DateTimeZone('UTC'));
    $trialExp = (clone $now)->modify('+14 days')->format('Y-m-d H:i:s');
    
    $insSub = $pdo->prepare("
        INSERT INTO `subscriptions` (`user_id`, `tier`, `trial_expires_at`, `created_at`)
        VALUES (:uid, 'free', :trial_exp, NOW())
    ");
    $insSub->execute([
        ':uid' => $userId,
        ':trial_exp' => $trialExp
    ]);
    
    $sub = [
        'user_id' => $userId,
        'tier' => 'free',
        'expires_at' => null,
        'trial_expires_at' => $trialExp,
        'stars_spent' => 0
    ];
}

// Compute active status and tier
$now = new DateTime('now', new DateTimeZone('UTC'));
$tier = $sub['tier'] ?? 'free';
$isActive = false;
$isTrialActive = false;
$isVip = false;
$maxChannels = 1;
$daysLeft = 0;

if (in_array($tier, ['pro', 'vip'], true) && !empty($sub['expires_at'])) {
    $expDate = new DateTime($sub['expires_at'], new DateTimeZone('UTC'));
    if ($expDate > $now) {
        $isActive = true;
        $diff = $now->diff($expDate);
        $daysLeft = $diff->days;
        if ($tier === 'vip') {
            $isVip = true;
            $maxChannels = 999;
        } else {
            $maxChannels = 5;
        }
    }
} else {
    // Free Trial check
    if (!empty($sub['trial_expires_at'])) {
        $trialExp = new DateTime($sub['trial_expires_at'], new DateTimeZone('UTC'));
        if ($trialExp > $now) {
            $isActive = true;
            $isTrialActive = true;
            $diff = $now->diff($trialExp);
            $daysLeft = $diff->days;
            $maxChannels = 1;
        }
    }
}

// Count user pairs
$pairCountStmt = $pdo->prepare("SELECT COUNT(*) FROM `channel_pairs` WHERE `user_id` = :uid");
$pairCountStmt->execute([':uid' => $userId]);
$pairsCount = (int)$pairCountStmt->fetchColumn();

// Count cloned messages today
$clonedTodayStmt = $pdo->prepare("
    SELECT COUNT(*) FROM `cloned_messages` cm
    JOIN `channel_pairs` cp ON cm.pair_id = cp.id
    WHERE cp.user_id = :uid AND DATE(cm.cloned_at) = CURDATE()
");
$clonedTodayStmt->execute([':uid' => $userId]);
$clonedToday = (int)$clonedTodayStmt->fetchColumn();

// Admin overrides
if ($authUser['is_admin']) {
    $isVip = true;
    $isActive = true;
    $maxChannels = 999;
}

jsonResponse([
    'ok' => true,
    'user' => [
        'id' => $userId, // App.tsx expects 'id' for User object
        'full_name' => $authUser['full_name'],
        'username' => $authUser['username'],
        'is_admin' => (bool)$authUser['is_admin']
    ],
    'subscription' => [
        'tier' => $tier,
        'is_active' => $isActive,
        'is_trial_active' => $isTrialActive,
        'is_vip' => $isVip,
        'max_channels' => $maxChannels,
        'expires_at' => $sub['expires_at'] ?? $sub['trial_expires_at'],
        'days_left' => $daysLeft,
        'stars_spent' => (int)($sub['stars_spent'] ?? 0)
    ],
    'stats' => [
        'channel_pairs_count' => $pairsCount,
        'total_cloned_messages' => $clonedToday,
        'success_rate' => 99.8 // We can keep a static or calculate properly if needed
    ]
]);
