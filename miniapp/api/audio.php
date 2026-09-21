<?php
declare(strict_types=1);

require_once __DIR__ . '/config.php';

$audioDir = dirname(__DIR__, 2) . '/assets/audio';
$track = $_GET['track'] ?? '';

// Prevent hotlinking: require authentication
$authUser = getAuthUser();
if (!$authUser || empty($authUser['user_id'])) {
    header('Content-Type: application/json', true, 401);
    echo json_encode(['ok' => false, 'error' => 'Unauthenticated'], JSON_UNESCAPED_UNICODE);
    exit;
}

// 1. Stream single audio file if track is requested
if (!empty($track)) {
    // Sanitize filename against directory traversal
    $filename = basename($track);
    $filepath = $audioDir . '/' . $filename;

    if (!file_exists($filepath) || !is_readable($filepath)) {
        header('Content-Type: application/json', true, 404);
        echo json_encode(['ok' => false, 'error' => 'Audio trek topilmadi'], JSON_UNESCAPED_UNICODE);
        exit;
    }

    $filesize = filesize($filepath);
    $offset = 0;
    $length = $filesize;

    // Handle HTTP Range header for seeking
    if (isset($_SERVER['HTTP_RANGE'])) {
        if (preg_match('/bytes=(\d+)-(\d+)?/', $_SERVER['HTTP_RANGE'], $matches)) {
            $offset = (int)$matches[1];
            if (!empty($matches[2])) {
                $end = (int)$matches[2];
                $length = $end - $offset + 1;
            } else {
                $length = $filesize - $offset;
            }
            header('HTTP/1.1 206 Partial Content');
            header("Content-Range: bytes {$offset}-" . ($offset + $length - 1) . "/{$filesize}");
        }
    }

    header('Content-Type: audio/mpeg');
    header('Accept-Ranges: bytes');
    header("Content-Length: {$length}");
    header('Cache-Control: public, max-age=86400');

    $fp = fopen($filepath, 'rb');
    if ($offset > 0) {
        fseek($fp, $offset);
    }

    $bufferSize = 64 * 1024;
    $remaining = $length;
    while (!feof($fp) && $remaining > 0) {
        $read = min($bufferSize, $remaining);
        echo fread($fp, $read);
        $remaining -= $read;
    }
    fclose($fp);
    exit;
}

// 2. Return list of all available audio tracks dynamically
$genreMap = [
    'luxury' => 'Ambient Corporate',
    'lounge' => 'Chill / Lounge',
    'piano' => 'Cinematic Piano',
    'lofi' => 'Lo-Fi Beats',
    'jazz' => 'Jazz / Mellow',
    'harmony' => 'Meditation Ambient',
    'penthouse' => 'Minimal Tech',
    'terrace' => 'Warm Chillout',
    'estate' => 'Strings & Piano',
    'groove' => 'Future Lounge',
    'acoustic' => 'Acoustic Guitar',
    'cocktail' => 'Deep House Vibe',
    'skyline' => 'Ethereal Ambient',
    'bossa' => 'Bossa Nova Lounge',
    'horizon' => 'Cinematic Epic',
    'sanctuary' => 'Zen Meditation',
    'midnight' => 'Midnight Deep',
    'breeze' => 'Coastal Chill',
    'oasis' => 'Warm Ambient',
    'living' => 'Modern Future'
];

$tracks = [];
if (is_dir($audioDir)) {
    $files = scandir($audioDir);
    foreach ($files as $fn) {
        if (str_ends_with(strtolower($fn), '.mp3')) {
            $base = pathinfo($fn, PATHINFO_FILENAME);
            $cleanTitle = ucwords(str_replace(['_', '-'], ' ', preg_replace('/^\d+_?/', '', $base)));
            
            $genre = 'Ambient Luxury';
            foreach ($genreMap as $k => $g) {
                if (stripos($fn, $k) !== false) {
                    $genre = $g;
                    break;
                }
            }

            $tracks[] = [
                'id' => $fn,
                'filename' => $fn,
                'title' => $cleanTitle,
                'genre' => $genre,
                'duration' => '0:30',
                'url' => "/api/audio.php?track=" . urlencode($fn),
                'exists' => true
            ];
        }
    }
}

jsonResponse(['ok' => true, 'tracks' => $tracks]);
