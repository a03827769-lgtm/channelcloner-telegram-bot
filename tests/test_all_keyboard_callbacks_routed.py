import re
import glob
import os

def test_all_keyboard_callbacks_have_matching_handlers():
    """
    Automated regression test verifying that every single button callback_data
    defined across all inline keyboards in the project has a corresponding handler.
    """
    files_kb = glob.glob('bot/keyboards/*.py') + glob.glob('admin_bot/keyboards/*.py')
    kb_callbacks = []
    for f in files_kb:
        with open(f, 'r', encoding='utf-8') as fh:
            text = fh.read()
            matches = re.findall(r'callback_data\s*=\s*(?:f?[\'"]([^\'"]+)[\'"])', text)
            for m in matches:
                kb_callbacks.append((f, m))

    files_h = glob.glob('bot/handlers/*.py') + glob.glob('admin_bot/handlers/*.py') + ['admin_bot/bot_instance.py']
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

    unmatched = []
    for f_kb, raw_cb in kb_callbacks:
        pattern_prefix = raw_cb.split('{')[0] if '{' in raw_cb else raw_cb
        
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
