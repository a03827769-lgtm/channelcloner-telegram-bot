import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Lint helper: lists raw emoji literals in handler/service code (premium custom emojis are preferred).
# Paths are relative to the project root, whatever the current directory is.
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

emoji_regex = re.compile(r'[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50\u2b55\u200d\ufe0f]')
dirs_to_check = ['bot/handlers', 'admin_bot/handlers', 'services']

found = []
for d in dirs_to_check:
    for root, _, files in os.walk(d):
        norm_root = root.replace('\\', '/')
        for f in files:
            if f.endswith('.py') and f not in ['emoji_converter.py', 'custom_emojis.py']:
                rel = os.path.join(norm_root, f).replace('\\', '/')
                with open(rel, 'r', encoding='utf-8', errors='ignore') as fp:
                    for i, line in enumerate(fp, 1):
                        clean_line = line.split('#')[0]
                        matches = emoji_regex.findall(clean_line)
                        if matches:
                            found.append((rel, i, ''.join(matches), clean_line.strip()))


print(f"Total raw emoji occurrences: {len(found)}")
for r, i, m, l in found:
    print(f"{r}:{i} [{m}] -> {l[:100]}")
