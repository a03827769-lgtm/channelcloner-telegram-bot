<?php
declare(strict_types=1);

/**
 * ChannelCloner Pro — Telegram Mini App Core Configuration & Database
 * PHP 8.3 OOP Architecture with Strict Typing, UTF8MB4 & Security Best Practices
 */

// Standard Security & Content Headers
header('Content-Type: application/json; charset=utf-8');
header('X-Content-Type-Options: nosniff');
header('X-Frame-Options: ALLOWALL'); // Mandatory for Telegram WebApp iframe
header('X-XSS-Protection: 1; mode=block');

// CORS Handling
$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
$allowedOrigins = [
    'https://t.me',
    'https://web.telegram.org',
    'https://app.abdulloh.me',
    'http://localhost:8080',
    'http://127.0.0.1:8080',
    'http://localhost:5173',
    'http://127.0.0.1:5173',
];

if (in_array($origin, $allowedOrigins, true) || empty($origin) || getenv('APP_ENV') === 'development' || empty(getenv('APP_ENV'))) {
    header('Access-Control-Allow-Origin: ' . ($origin ?: '*'));
} else {
    header('Access-Control-Allow-Origin: https://app.abdulloh.me');
}
header('Access-Control-Allow-Methods: GET, POST, PUT, DELETE, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization, X-Requested-With, X-Telegram-Init-Data');

// Preflight CORS response
if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') {
    http_response_code(200);
    exit;
}

// Database Credentials from Environment
$dbHost = getenv('MYSQL_HOST') ?: (getenv('DB_HOST') ?: '127.0.0.1');
$dbPort = (int)(getenv('MYSQL_PORT') ?: (getenv('DB_PORT') ?: 3307));
$dbName = getenv('MYSQL_DATABASE') ?: (getenv('DB_NAME') ?: 'channelcloner');
$dbUser = getenv('MYSQL_USER') ?: (getenv('DB_USER') ?: 'cloner_user');
$dbPass = getenv('MYSQL_PASSWORD') ?: (getenv('DB_PASSWORD') ?: 'cloner_pass_2026');
$botToken = getenv('BOT_TOKEN') ?: '7812984712:AAH9_mock_cloner_token';

class Database {
    private static ?PDO $instance = null;

    public static function getConnection(): PDO {
        if (self::$instance === null) {
            global $dbHost, $dbPort, $dbName, $dbUser, $dbPass;
            
            $dsn = "mysql:host={$dbHost};port={$dbPort};dbname={$dbName};charset=utf8mb4";
            $options = [
                PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES   => false,
                PDO::MYSQL_ATTR_INIT_COMMAND => "SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci"
            ];

            try {
                self::$instance = new PDO($dsn, $dbUser, $dbPass, $options);
            } catch (PDOException $e) {
                // If container connection failed on 3307, try localhost:3306 or internal docker 'mysql'
                $fallbackHosts = ['mysql', '127.0.0.1'];
                $connected = false;
                foreach ($fallbackHosts as $fHost) {
                    try {
                        $fPort = ($fHost === 'mysql') ? 3306 : 3306;
                        $fDsn = "mysql:host={$fHost};port={$fPort};dbname={$dbName};charset=utf8mb4";
                        self::$instance = new PDO($fDsn, $dbUser, $dbPass, $options);
                        $connected = true;
                        break;
                    } catch (PDOException $fe) {
                        continue;
                    }
                }
                if (!$connected) {
                    error_log("Database connection error: " . $e->getMessage());
                    http_response_code(500);
                    echo json_encode([
                        'ok' => false,
                        'error' => 'Database connection failed. Please try again later.'
                    ], JSON_UNESCAPED_UNICODE);
                    exit;
                }
            }
        }
        return self::$instance;
    }
}

/**
 * Standard JSON Response Helper
 */
function jsonResponse(array $data, int $status = 200): void {
    http_response_code($status);
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    exit;
}

/**
 * Standard Error Response Helper
 */
function errorResponse(string $message, int $status = 400, ?string $code = null): void {
    http_response_code($status);
    $payload = [
        'ok' => false,
        'error' => $message,
    ];
    if ($code !== null) {
        $payload['code'] = $code;
    }
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

/**
 * Validates Telegram WebApp initData HMAC-SHA256
 */
function validateTelegramInitData(string $initData, string $botToken): ?array {
    if (empty($initData)) {
        return null;
    }

    parse_str($initData, $params);
    if (!isset($params['hash'])) {
        return null;
    }

    $hash = $params['hash'];
    unset($params['hash']);

    // Sort parameters alphabetically
    ksort($params);
    $dataCheckString = [];
    foreach ($params as $key => $val) {
        $dataCheckString[] = "{$key}={$val}";
    }
    $checkString = implode("\n", $dataCheckString);

    $secretKey = hash_hmac('sha256', $botToken, 'WebAppData', true);
    $calculatedHash = hash_hmac('sha256', $checkString, $secretKey);

    if (hash_equals($calculatedHash, $hash)) {
        $userJson = $params['user'] ?? null;
        if ($userJson) {
            return json_decode($userJson, true);
        }
    }
    return null;
}

/**
 * Extract authenticated user or fallback for development/demo
 */
function getAuthUser(): array {
    global $botToken;

    $initData = $_SERVER['HTTP_X_TELEGRAM_INIT_DATA'] ?? ($_GET['initData'] ?? '');
    error_log("[AUTH_DEBUG] Host: " . ($_SERVER['HTTP_HOST'] ?? '') . " | InitData len: " . strlen($initData) . " | Raw: " . substr($initData, 0, 100));
    if (!empty($initData)) {
        $tgUser = validateTelegramInitData($initData, $botToken);
        if (!$tgUser) {
            error_log("[AUTH_DEBUG] validateTelegramInitData FAILED for token: " . substr($botToken, 0, 10) . "...");
        }
        if ($tgUser && isset($tgUser['id'])) {
            $adminIds = array_filter(array_map('trim', explode(',', (string)(getenv('ADMIN_IDS') ?: '8881989487'))));
            $isAdmin = in_array((string)$tgUser['id'], $adminIds, true);
            return [
                'user_id' => (int)$tgUser['id'],
                'full_name' => trim(($tgUser['first_name'] ?? '') . ' ' . ($tgUser['last_name'] ?? '')),
                'username' => $tgUser['username'] ?? null,
                'is_admin' => $isAdmin
            ];
        }
    }

    // Direct browser visits or invalid initData are strictly forbidden!
    errorResponse('Ushbu Mini App faqat rasmiy @klonlabot Telegram boti orqali ochiladi. Brauzer orqali kirish taqiqlangan.', 403, 'TELEGRAM_REQUIRED');
    return [];
}

/**
 * Helper to read JSON request body
 */
function getJsonBody(): array {
    $raw = file_get_contents('php://input');
    if (empty($raw)) {
        return [];
    }
    $decoded = json_decode($raw, true);
    return is_array($decoded) ? $decoded : [];
}
