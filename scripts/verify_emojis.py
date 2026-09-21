import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
pattern = re.compile(r'[\U00010000-\U0010ffff\u2600-\u26ff\u2700-\u27bf\ufe0f]')

paths = [
    'webapp/public/index.html',
    'webapp/public/index.php',
    'webapp/public/assets/js/app.js',
    'webapp/public/assets/css/liquid.css',
    'webapp/public/admin.html',
    'webapp/public/assets/js/admin.js'
]

for path in paths:
    print(f"=== Checking {path} ===")
    count = 0
    with open(path, 'r', encoding='utf-8') as f:
        for idx, line in enumerate(f, 1):
            s = line.strip()
            if s.startswith('/*') or s.startswith('*') or s.startswith('//'):
                continue
            matches = pattern.findall(line)
            if matches:
                # In app.js, dictionary alias keys are allowed: '✨': 'sparkle',
                if "': '" in s:
                    continue
                print(f"  Line {idx}: {matches} -> {s[:100]}")
                count += 1
    print(f"Total unaliased matches in {path}: {count}")
