import sqlite3
import os

db_path = "/app/data/cloner.db" if os.path.exists("/app/data/cloner.db") else "database/cloner.db"
conn = sqlite3.connect(db_path)
rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
for k, v in rows:
    lower_k = k.lower()
    if any(s in lower_k for s in ("session", "key", "token", "secret", "password")):
        preview = "****** [REDACTED FOR SECURITY] ******"
    else:
        preview = v[:25] + "..." if len(v) > 25 else v
    print(f"Key: {k}, Value preview: {preview}, Length: {len(v)}")
