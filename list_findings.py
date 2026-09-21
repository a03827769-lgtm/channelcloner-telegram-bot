import json
data = json.load(open('AUDIT_FINDINGS.json'))
for d in data:
    if d['category'] in ['SECURITY', 'DEPRECATION']:
        print(f"{d['id']}: {d['category']} - {d['file']}:{d['line']} - {d['title']}")
