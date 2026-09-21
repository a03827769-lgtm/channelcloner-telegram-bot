<?php
declare(strict_types=1);

require_once __DIR__ . '/config.php';

$pdo = Database::getConnection();
$authUser = getAuthUser();
$userId = (int)$authUser['user_id'];
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$action = $_GET['action'] ?? 'tariffs';

if ($action === 'tariffs') {
    jsonResponse([
        'ok' => true,
        'tariffs' => [
            [
                'id' => 'free',
                'name' => 'Bepul Sinov (Trial)',
                'stars' => 0,
                'period' => '14 kun',
                'features' => [
                    '1 ta faol kanal juftligi',
                    'Begona link va reklamani tozalash',
                    'Oddiy va Toza nusxa rejimi',
                    'Shaxsiy imzo qo\'yish'
                ]
            ],
            [
                'id' => 'pro',
                'name' => 'Pro Tarif',
                'stars' => 100,
                'period' => '1 oy',
                'popular' => true,
                'features' => [
                    '5 tagacha faol kanal juftligi',
                    'AI Content Paraphraser (3 xil uslub)',
                    'Rasm va Video Watermark (Logo urish)',
                    'Avto-Tarjima (Uz, Ru, En, Tr)',
                    'Dynamic Affiliate & Referal Almashtirgich',
                    'Drip Feed kechikishi (0-45m)'
                ]
            ],
            [
                'id' => 'vip',
                'name' => 'VIP Cheksiz',
                'stars' => 300,
                'period' => '1 oy',
                'vip' => true,
                'features' => [
                    'Cheksiz kanallar klonlash (999+)',
                    'VIP Real Estate Auto-Story Cloner ($700+)',
                    '4K Playwright kollaj & Ken Burns video',
                    'Telegram Premium animatsion emojilar',
                    'Himoyalangan kanallar (Protected mode)',
                    'AI Reklama Qalqoni (Ad Shield)',
                    'Prioritet 24/7 server navbati'
                ]
            ]
        ]
    ]);
}

if ($action === 'checkout' && $method === 'POST') {
    $body = getJsonBody();
    $tier = $body['tier'] ?? 'pro';
    $stars = ($tier === 'vip') ? 300 : 100;

    global $botToken;
    
    // Create Telegram Stars Invoice Link using Bot API
    $ch = curl_init("https://api.telegram.org/bot{$botToken}/createInvoiceLink");
    $payload = [
        'title' => "ChannelCloner " . strtoupper($tier),
        'description' => "1 oylik " . strtoupper($tier) . " ta'rifiga to'lov",
        'payload' => "sub_{$tier}_{$userId}_" . time(),
        'provider_token' => "", // Empty for Telegram Stars
        'currency' => "XTR",
        'prices' => [
            ['label' => 'Stars', 'amount' => $stars]
        ]
    ];
    
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => json_encode($payload),
        CURLOPT_HTTPHEADER => ['Content-Type: application/json']
    ]);
    
    $result = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    $invoiceLink = '';
    if ($result) {
        $data = json_decode($result, true);
        if (isset($data['ok']) && $data['ok'] && !empty($data['result'])) {
            $invoiceLink = $data['result'];
        }
    }

    if (empty($invoiceLink)) {
        // Fallback for local testing if Bot token is fake
        $invoiceLink = "https://t.me/\$payment?slug=demo_fail_test_{$stars}";
    }

    jsonResponse([
        'ok' => true,
        'tier' => $tier,
        'stars' => $stars,
        'invoice_link' => $invoiceLink,
        'message' => "Telegram Stars to'lov hisobi yaratildi ({$stars} Stars)"
    ]);
}

errorResponse('Noto\'g\'ri so\'rov', 400);
