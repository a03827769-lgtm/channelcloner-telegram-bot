"""
Read-only diagnostic: lists the keys stored in the app_settings table with a short preview.
Values whose key looks secret (session, key, token, secret, password) are never shown.

    python scripts/check_session.py [path/to/cloner.db]     (default: DB_PATH from the configuration)
"""
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SECRET_MARKERS = ("session", "key", "token", "secret", "password")


def resolve_db_path(argv) -> str:
    if len(argv) > 1:
        return os.path.abspath(argv[1])
    sys.path.insert(0, PROJECT_ROOT)
    from config.settings import settings
    path = settings.DB_PATH or "database/cloner.db"
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    db_path = resolve_db_path(argv)
    if not os.path.isfile(db_path):
        print(f"Database not found: {db_path}")
        return 1
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    finally:
        conn.close()
    for key, value in rows:
        value = value or ""
        if any(marker in key.lower() for marker in _SECRET_MARKERS):
            preview = "****** [REDACTED] ******"
        else:
            preview = value[:25] + "..." if len(value) > 25 else value
        print(f"Key: {key}, Value preview: {preview}, Length: {len(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
