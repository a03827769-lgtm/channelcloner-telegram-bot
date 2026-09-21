import os
import re
import json

def fix_except_pass(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Regex to find:
    # except Exception:
    #     pass
    # and replace with logger
    
    pattern = r'(except Exception:\s+)(pass)'
    
    if not re.search(pattern, content):
        return False
    
    new_except_block = r'\g<1>logger.debug("Ignored exception", exc_info=True)'
    content = re.sub(pattern, new_except_block, content)
    
    # ensure logging is imported and logger is defined
    has_import = re.search(r'import logging', content)
    has_logger = re.search(r'logger\s*=\s*logging\.getLogger', content)
    
    lines = content.split('\n')
    
    if not has_logger:
        # find the last import to insert below it
        last_import_idx = -1
        for i, line in enumerate(lines):
            if line.startswith('import ') or line.startswith('from '):
                last_import_idx = i
                
        if last_import_idx != -1:
            if not has_import:
                lines.insert(last_import_idx + 1, 'import logging')
                lines.insert(last_import_idx + 2, 'logger = logging.getLogger(__name__)')
            else:
                lines.insert(last_import_idx + 1, 'logger = logging.getLogger(__name__)')
                
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
        
    return True

data = json.load(open('AUDIT_FINDINGS.json'))
files_to_fix = set(d['file'] for d in data if d['category'] == 'RELIABILITY')

fixed_count = 0
for rel_file in files_to_fix:
    abs_path = os.path.join(os.getcwd(), rel_file)
    if os.path.exists(abs_path):
        if fix_except_pass(abs_path):
            fixed_count += 1
            print(f"Fixed {rel_file}")

print(f"Successfully patched {fixed_count} files for reliability issues.")
