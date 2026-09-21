import os
import sys
import ast
import re
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIRS = ["bot", "admin_bot", "services", "database", "config", "scripts"]
SINGLE_FILES = ["run.py", "setup_wizard.py"]

findings = []

def add_finding(category, severity, file_path, line_no, title, description, recommendation):
    rel_path = os.path.relpath(file_path, BASE_DIR) if os.path.isabs(file_path) else file_path
    findings.append({
        "id": f"AUDIT-{len(findings) + 1:03d}",
        "category": category,
        "severity": severity,  # CRITICAL, HIGH, MEDIUM, LOW
        "file": rel_path.replace("\\", "/"),
        "line": line_no,
        "title": title,
        "description": description,
        "recommendation": recommendation
    })

def scan_file_ast(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception as e:
        add_finding("SYNTAX", "CRITICAL", file_path, 1, "File Read Error", str(e), "Fix file encoding or permissions.")
        return

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        add_finding("SYNTAX", "CRITICAL", file_path, e.lineno or 1, "Syntax Error", str(e), "Fix syntax.")
        return

    lines = source.splitlines()

    # 1. Check for duplicate method definitions in classes
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            method_names = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name in method_names:
                        prev_line = method_names[item.name]
                        add_finding(
                            "ARCHITECTURE", "HIGH", file_path, item.lineno,
                            f"Duplicate Method Definition '{item.name}' in class '{node.name}'",
                            f"Method '{item.name}' was first defined at line {prev_line} and re-defined at line {item.lineno}, silently shadowing the first implementation.",
                            "Consolidate or remove the duplicate method definition."
                        )
                    else:
                        method_names[item.name] = item.lineno

    # 2. Check for blocking calls inside async def
    class AsyncBlockingVisitor(ast.NodeVisitor):
        def __init__(self):
            self.in_async = False
            self.async_func_name = ""

        def visit_AsyncFunctionDef(self, node):
            old_in_async = self.in_async
            old_name = self.async_func_name
            self.in_async = True
            self.async_func_name = node.name
            self.generic_visit(node)
            self.in_async = old_in_async
            self.async_func_name = old_name

        def visit_Call(self, node):
            if self.in_async:
                # time.sleep
                if isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        if node.func.value.id == "time" and node.func.attr == "sleep":
                            add_finding(
                                "PERFORMANCE", "HIGH", file_path, node.lineno,
                                f"Synchronous time.sleep in async function '{self.async_func_name}'",
                                "Calling synchronous time.sleep blocks the entire asyncio event loop and freezes all concurrent bot operations.",
                                "Replace with 'await asyncio.sleep(...)'.",
                            )
                        elif node.func.value.id == "subprocess" and node.func.attr in ("run", "call", "check_output", "check_call"):
                            add_finding(
                                "PERFORMANCE", "HIGH", file_path, node.lineno,
                                f"Synchronous subprocess.{node.func.attr} in async function '{self.async_func_name}'",
                                f"Running blocking subprocess.{node.func.attr} directly inside an async coroutine stalls the event loop.",
                                "Replace with 'asyncio.create_subprocess_exec' or run via 'asyncio.to_thread / loop.run_in_executor'.",
                            )
                        elif node.func.value.id == "requests" and node.func.attr in ("get", "post", "put", "delete", "request"):
                            add_finding(
                                "PERFORMANCE", "HIGH", file_path, node.lineno,
                                f"Synchronous requests.{node.func.attr} in async function '{self.async_func_name}'",
                                f"Using synchronous requests.{node.func.attr} halts all concurrent tasks while waiting for HTTP response.",
                                "Migrate to async aiohttp ClientSession.",
                            )
                elif isinstance(node.func, ast.Name):
                    pass
            self.generic_visit(node)

    AsyncBlockingVisitor().visit(tree)

    # 3. Check for CallbackQuery without callback.answer() in handlers
    if "handlers" in file_path:
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                # Check if it has a CallbackQuery argument
                has_cb_arg = any(
                    arg.arg in ("callback", "cb", "callback_query", "query") or
                    (isinstance(arg.annotation, ast.Name) and arg.annotation.id == "CallbackQuery")
                    for arg in node.args.args
                )
                if has_cb_arg:
                    # Look for answer() call in the body
                    calls_answer = False
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            if isinstance(sub.func, ast.Attribute) and sub.func.attr == "answer":
                                calls_answer = True
                                break
                            if isinstance(sub.func, ast.Name) and sub.func.id in ("safe_answer", "answer"):
                                calls_answer = True
                                break
                    if not calls_answer:
                        add_finding(
                            "UX_DEFECT", "MEDIUM", file_path, node.lineno,
                            f"CallbackQuery handler '{node.name}' does not call 'await callback.answer()'",
                            "Without answering the callback query, the Telegram client keeps showing a loading spinner on the button until timeout (up to 30s).",
                            "Add 'await callback.answer()' at the beginning or end of the handler.",
                        )

    # 4. Check for bare except or silent except pass
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            # Check if body is just pass
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                if node.type is None:
                    add_finding(
                        "RELIABILITY", "CRITICAL", file_path, node.lineno,
                        "Bare 'except:' catching BaseException with silent 'pass'",
                        "Bare except catches KeyboardInterrupt, SystemExit, and asyncio.CancelledError, breaking graceful shutdown and task cancellation.",
                        "Specify 'except Exception:' and log the error or re-raise CancelledError."
                    )
                elif isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
                    # Check if CancelledError is caught
                    add_finding(
                        "RELIABILITY", "MEDIUM", file_path, node.lineno,
                        f"Broad 'except {node.type.id}: pass' without logging or cancellation handling",
                        "Swallowing all exceptions silently hides operational failures, making debugging impossible and potentially suppressing cancellation.",
                        "Log the caught exception with logger.warning/debug or re-raise."
                    )

    # 5. Check for hardcoded secrets, tokens, or personal IDs
    for idx, line in enumerate(lines, 1):
        if re.search(r'(?i)(?:api_key|bot_token|token|secret|password)\s*=\s*["\'][A-Za-z0-9_:]{20,}["\']', line):
            if "example" not in file_path.lower() and "test" not in file_path.lower():
                add_finding(
                    "SECURITY", "HIGH", file_path, idx,
                    "Potentially Hardcoded Secret/Token String",
                    f"Found hardcoded credential pattern in code: {line.strip()[:60]}...",
                    "Extract credential into environment variable / settings."
                )
        if re.search(r'(?<![A-Za-z0-9_])(ADMIN_PHONE_NUMBER_HERE|SOME_SECRET_ID_HERE)(?![A-Za-z0-9_])', line):
            if "test" not in file_path.lower():
                add_finding(
                    "SECURITY", "MEDIUM", file_path, idx,
                    "Hardcoded Personal Telegram ID / Phone Number",
                    f"Found hardcoded user ID or phone number in source code: {line.strip()[:60]}...",
                    "Make configurable via .env / database settings instead of hardcoding."
                )

    # 6. Check for datetime naive vs aware comparisons or deprecated methods
    for idx, line in enumerate(lines, 1):
        if "datetime.utcnow" + "()" in line:
            add_finding(
                "DEPRECATION", "LOW", file_path, idx,
                "Deprecated datetime.utcnow usage",
                "datetime.utcnow is deprecated in Python 3.12+ and produces naive UTC datetimes that cause subtle timezone bugs.",
                "Replace with datetime.now(timezone.utc)."
            )

    # 7. Check for SQL injection in execute calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executemany"):
                if node.args and isinstance(node.args[0], (ast.JoinedStr, ast.BinOp)):
                    # Check if it's not ALTER TABLE ADD COLUMN
                    call_str = ast.unparse(node.args[0]) if hasattr(ast, "unparse") else ""
                    if "ALTER TABLE" not in call_str:
                        add_finding(
                            "SECURITY", "HIGH", file_path, node.lineno,
                            "Dynamic SQL Construction via String Interpolation in db.execute",
                            f"SQL statement constructed using f-string or % operator: {call_str[:80]}",
                            "Use parameterized queries with ? placeholders to prevent SQL injection."
                        )

def run_audit():
    all_files = []
    for d in TARGET_DIRS:
        dp = os.path.join(BASE_DIR, d)
        if os.path.exists(dp):
            for root, _, files in os.walk(dp):
                for f in files:
                    if f.endswith(".py") and "__pycache__" not in root:
                        all_files.append(os.path.join(root, f))
    for f in SINGLE_FILES:
        fp = os.path.join(BASE_DIR, f)
        if os.path.exists(fp):
            all_files.append(fp)

    print(f"Scanning {len(all_files)} Python source files across codebase...")
    for fp in all_files:
        scan_file_ast(fp)

    out_file = os.path.join(BASE_DIR, "AUDIT_FINDINGS.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2, ensure_ascii=False)

    print(f"Audit completed! Discovered {len(findings)} findings across {len(all_files)} files.")
    print(f"Findings saved to: {out_file}")

if __name__ == "__main__":
    run_audit()
