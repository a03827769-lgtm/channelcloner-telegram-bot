import os
import re
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


keyboard_dirs = ['bot', 'admin_bot', 'services']
emoji_pattern = re.compile(r'[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50\u2b55\u200d\ufe0f]')

import ast

def inspect_file(filepath):
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as fp:
        content = fp.read()
    try:
        tree = ast.parse(content, filename=filepath)
    except Exception as e:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = ''
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            if func_name in ('InlineKeyboardButton', 'KeyboardButton'):
                kw = {k.arg: k.value for k in node.keywords}
                text_val = ''
                if 'text' in kw and isinstance(kw['text'], ast.Constant):
                    text_val = str(kw['text'].value)
                elif 'text' in kw and isinstance(kw['text'], ast.JoinedStr):
                    text_val = '<f-string>'
                
                raw_emojis = emoji_pattern.findall(text_val)
                has_icon = 'icon_custom_emoji_id' in kw
                has_style = 'style' in kw
                has_url = 'url' in kw

                issues = []
                if raw_emojis:
                    issues.append(f"RAW_EMOJI: {raw_emojis}")
                if not has_icon and not has_url:
                    issues.append("NO_ICON")
                if not has_style and not has_url:
                    issues.append("NO_STYLE")
                if issues:
                    print(f"{filepath}:{node.lineno} [{func_name}] -> {', '.join(issues)} | text='{text_val}'")

for d in keyboard_dirs:
    for root, dirs, files in os.walk(d):
        for f in files:
            if f.endswith('.py'):
                inspect_file(os.path.join(root, f))

