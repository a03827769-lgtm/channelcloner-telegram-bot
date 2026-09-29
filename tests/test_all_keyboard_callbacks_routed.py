import re
import glob
import os

# Paths are resolved from the project root, so the test means the same from any working directory
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _project_glob(pattern):
    return glob.glob(os.path.join(PROJECT_ROOT, pattern))


def test_all_keyboard_callbacks_have_matching_handlers():
    """
    Static check: every literal callback_data prefix written in the keyboard modules has a handler filter
    (== / in_ / startswith / regexp) in the handler modules. Callback data built from variables is
    covered by the dispatcher-level test in test_fix_bot.py.
    """
    files_kb = _project_glob('bot/keyboards/*.py') + _project_glob('admin_bot/keyboards/*.py')
    kb_callbacks = []
    for f in files_kb:
        with open(f, 'r', encoding='utf-8') as fh:
            text = fh.read()
            matches = re.findall(r'callback_data\s*=\s*(?:f?[\'"]([^\'"]+)[\'"])', text)
            for m in matches:
                kb_callbacks.append((f, m))

    files_h = (_project_glob('bot/handlers/*.py') + _project_glob('admin_bot/handlers/*.py')
               + [os.path.join(PROJECT_ROOT, 'admin_bot', 'bot_instance.py')])
    assert len(files_kb) >= 4 and len(files_h) >= 10, "keyboard / handler modules not found"
    handler_filters = []
    for f in files_h:
        with open(f, 'r', encoding='utf-8') as fh:
            text = fh.read()
            # Capture startswith prefixes directly
            sws = re.findall(r'startswith\([\'"]([^\'"]+)[\'"]\)', text)
            for sw in sws:
                handler_filters.append((f, f'startswith("{sw}")'))
            
            # Capture direct equality
            eqs = re.findall(r'F\.data\s*==\s*[\'"]([^\'"]+)[\'"]', text)
            for eq in eqs:
                handler_filters.append((f, f'== "{eq}"'))

            # Capture in_ lists
            ins = re.findall(r'in_\(\[([^\]]+)\]\)', text)
            for in_list in ins:
                items = re.findall(r'[\'"]([^\'"]+)[\'"]', in_list)
                for it in items:
                    handler_filters.append((f, f'== "{it}"'))

            # Capture regexp patterns
            regexps = re.findall(r'regexp\([\'"r]+([^\'"]+)[\'"]\)', text)
            for rg in regexps:
                handler_filters.append((f, f'regexp("{rg}")'))

    assert len(kb_callbacks) >= 50, "no callback_data literals were found"
    unmatched = []
    for f_kb, raw_cb in kb_callbacks:
        pattern_prefix = raw_cb.split('{')[0] if '{' in raw_cb else raw_cb
        if not pattern_prefix:
            continue  # fully dynamic value: checked by the dispatcher-level test
        
        matched = False
        for f_h, h_filter in handler_filters:
            if f'F.data == "{raw_cb}"' in h_filter or f"F.data == '{raw_cb}'" in h_filter:
                matched = True
                break
            if f'"{raw_cb}"' in h_filter or f"'{raw_cb}'" in h_filter:
                matched = True
                break
            sw_matches = re.findall(r'startswith\([\'"]([^\'"]+)[\'"]\)', h_filter)
            for sw in sw_matches:
                if raw_cb.startswith(sw) or pattern_prefix.startswith(sw):
                    matched = True
                    break
            if matched:
                break
            if 'regexp(' in h_filter:
                rg_pat = re.search(r'regexp\("([^"]+)"\)', h_filter)
                if rg_pat:
                    prefix_from_rg = rg_pat.group(1).lstrip('^').split('(')[0]
                    if pattern_prefix.startswith(prefix_from_rg) or prefix_from_rg.startswith(pattern_prefix):
                        matched = True
                        break
            if pattern_prefix and pattern_prefix in h_filter:
                matched = True
                break

        if not matched:
            unmatched.append((f_kb, raw_cb))

    assert len(unmatched) == 0, f"Found {len(unmatched)} unhandled button callbacks: {unmatched}"
