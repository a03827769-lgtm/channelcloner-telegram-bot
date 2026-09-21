import json
import sys

with open("AUDIT_FINDINGS.json", "r", encoding="utf-8") as f:
    findings = json.load(f)

required_keys = [
    "id", "file", "line_start", "line_end", "category", "severity", 
    "likelihood", "impact", "priority_score", "title", "description", 
    "evidence", "root_cause", "recommended_fix", "status", 
    "verification_method", "confidence", "fixed_in_commit"
]

valid_severities = {"critical", "high", "medium", "low"}
valid_statuses = {"open", "in_progress", "fixed", "wont_fix", "needs_human_decision"}
valid_confidences = {"high", "medium", "low"}

errors = []
for i, item in enumerate(findings):
    fid = item.get("id", f"INDEX_{i}")
    for k in required_keys:
        if k not in item:
            errors.append(f"{fid} missing key {k}")
    if item.get("severity") not in valid_severities:
        errors.append(f"{fid} invalid severity: {item.get('severity')}")
    if item.get("status") not in valid_statuses:
        errors.append(f"{fid} invalid status: {item.get('status')}")
    if item.get("confidence") not in valid_confidences:
        errors.append(f"{fid} invalid confidence: {item.get('confidence')}")
    if not isinstance(item.get("line_start"), int) or not isinstance(item.get("line_end"), int):
        errors.append(f"{fid} line numbers must be integers")
    if not isinstance(item.get("priority_score"), (int, float)):
        errors.append(f"{fid} priority_score must be numeric")

print(f"Total findings: {len(findings)}")
print(f"Total validation errors: {len(errors)}")

if errors:
    for e in errors[:10]:
        print("  ERROR:", e)
    sys.exit(1)
else:
    print("SUCCESS: 100% schema compliance verified across all findings!")
