<?php
declare(strict_types=1);

/**
 * ChannelCloner Pro — Local PHP Built-in Server Router
 * Emulates Nginx rewrites, static caching, FastCGI routing and SPA fallback.
 */

$uri = urldecode((string)parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH));

// 1. Serve static files from miniapp/public
$publicFile = __DIR__ . '/public' . $uri;
if ($uri !== '/' && file_exists($publicFile) && !is_dir($publicFile)) {
    $ext = strtolower(pathinfo($publicFile, PATHINFO_EXTENSION));
    $mimes = [
        'js' => 'application/javascript; charset=utf-8',
        'css' => 'text/css; charset=utf-8',
        'svg' => 'image/svg+xml',
        'png' => 'image/png',
        'jpg' => 'image/jpeg',
        'jpeg' => 'image/jpeg',
        'json' => 'application/json',
        'ico' => 'image/x-icon',
        'woff2' => 'font/woff2',
        'mp3' => 'audio/mpeg'
    ];
    $mime = $mimes[$ext] ?? 'application/octet-stream';
    header("Content-Type: {$mime}");
    header('Cache-Control: public, max-age=86400');
    readfile($publicFile);
    exit;
}

// 2. Direct Static File in assets/
$assetFile = dirname(__DIR__) . $uri;
if ($uri !== '/' && file_exists($assetFile) && !is_dir($assetFile)) {
    $ext = strtolower(pathinfo($assetFile, PATHINFO_EXTENSION));
    $mime = ($ext === 'mp3') ? 'audio/mpeg' : 'application/octet-stream';
    header("Content-Type: {$mime}");
    readfile($assetFile);
    exit;
}

// 3. API Routing
if (str_starts_with($uri, '/api/')) {
    $apiFile = __DIR__ . $uri;
    if (file_exists($apiFile) && str_ends_with($apiFile, '.php')) {
        require $apiFile;
        exit;
    }

    // Clean REST URL rewrites
    if ($uri === '/api/me') {
        require __DIR__ . '/api/auth.php';
        exit;
    }
    if ($uri === '/api/pairs') {
        require __DIR__ . '/api/pairs.php';
        exit;
    }
    if ($uri === '/api/story/settings') {
        $_GET['action'] = 'settings';
        require __DIR__ . '/api/story.php';
        exit;
    }
    if ($uri === '/api/story/queue') {
        $_GET['action'] = 'queue';
        require __DIR__ . '/api/story.php';
        exit;
    }
    if ($uri === '/api/audio-tracks') {
        require __DIR__ . '/api/audio.php';
        exit;
    }
    if ($uri === '/api/system') {
        require __DIR__ . '/api/system.php';
        exit;
    }
    if ($uri === '/api/billing') {
        require __DIR__ . '/api/billing.php';
        exit;
    }
}

// 4. Default: SPA fallback to public/index.html
if (file_exists(__DIR__ . '/public/index.html')) {
    header('Content-Type: text/html; charset=utf-8');
    readfile(__DIR__ . '/public/index.html');
    exit;
}

http_response_code(404);
echo "404 Not Found";
