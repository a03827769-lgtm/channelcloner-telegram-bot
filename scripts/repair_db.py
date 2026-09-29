"""
Offline SQLite check / repair for the Channel Cloner database.

    python scripts/repair_db.py                          check the configured database (settings.DB_PATH)
    python scripts/repair_db.py path/to/cloner.db        check another database file
    python scripts/repair_db.py [path] --rebuild         rebuild a corrupted database
    python scripts/repair_db.py [path] --rebuild --accept-loss
                                                         ... even if some rows cannot be recovered

Safety rules:
  * it refuses to work while the bot is running (app.pid names a live run.py, or /health answers):
    stop the bot first (python scripts/windows_keepalive_watchdog.py --stop, or docker compose stop);
  * before anything is changed, a full copy is taken with the SQLite online backup API (includes the
    content of the -wal file);
  * the rebuilt database replaces the original only when its integrity check passes and every table has
    the same number of rows as the original (unless --accept-loss);
  * -wal/-shm files are removed only after the original was checkpointed and closed, never while in use.
"""

import argparse
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_db_path() -> str:
    sys.path.insert(0, PROJECT_ROOT)
    from config.settings import settings
    path = settings.DB_PATH or "database/cloner.db"
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def _health_port() -> int:
    try:
        with open(os.path.join(PROJECT_ROOT, "logs", "health_port.txt"), "r", encoding="utf-8") as f:
            value = f.read().strip()
            if value.isdigit():
                return int(value)
    except OSError:
        pass
    raw = os.getenv("PORT", "")
    return int(raw) if raw.isdigit() else 8080


def bot_is_running() -> Optional[str]:
    """A human-readable reason when the bot appears to be running, else None."""
    try:
        with open(os.path.join(PROJECT_ROOT, "app.pid"), "r", encoding="utf-8") as f:
            match = re.search(r"\d+", f.read())
        if match:
            import psutil
            pid = int(match.group(0))
            if psutil.pid_exists(pid):
                try:
                    cmdline = " ".join(psutil.Process(pid).cmdline())
                except psutil.Error:
                    cmdline = ""
                if "run.py" in cmdline:
                    return f"app.pid names a running bot process (PID {pid})"
    except (OSError, ImportError):
        pass
    try:
        url = f"http://127.0.0.1:{_health_port()}/health"
        with urllib.request.urlopen(url, timeout=3) as response:
            if response.status == 200:
                return f"the bot answers on {url}"
    except Exception:
        pass
    return None


def integrity_check(db_path: str) -> List[str]:
    # A normal connection: read-only opens of a WAL database fail when its -shm file is missing
    conn = sqlite3.connect(db_path)
    try:
        return [row[0] for row in conn.execute("PRAGMA integrity_check;").fetchall()]
    finally:
        conn.close()


def backup_database(db_path: str, backup_path: str) -> None:
    """Consistent full copy (main file + WAL content) through the SQLite online backup API."""
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def table_row_counts(conn: sqlite3.Connection) -> Dict[str, Optional[int]]:
    """Rows per table; None when the table cannot be read (corruption)."""
    counts: Dict[str, Optional[int]] = {}
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    for table in tables:
        try:
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except sqlite3.DatabaseError:
            counts[table] = None
    return counts


def rebuild_database(src_path: str, dst_path: str) -> Tuple[int, List[str]]:
    """Dumps the source statement by statement into a fresh database. Returns (statements, errors)."""
    if os.path.exists(dst_path):
        os.remove(dst_path)
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    executed, errors = 0, []
    try:
        dump = src.iterdump()
        while True:
            try:
                statement = next(dump)
            except StopIteration:
                break
            except sqlite3.DatabaseError as e:
                errors.append(f"dump aborted: {e}")
                break
            try:
                dst.execute(statement)
                executed += 1
            except sqlite3.DatabaseError as e:
                errors.append(f"{e}: {statement[:120]}")
        dst.commit()
    finally:
        dst.close()
        src.close()
    return executed, errors


def compare_counts(original: Dict[str, Optional[int]], rebuilt: Dict[str, Optional[int]]) -> List[str]:
    problems = []
    for table, count in original.items():
        if count is None:
            problems.append(f"{table}: unreadable in the original")
        elif rebuilt.get(table) != count:
            problems.append(f"{table}: {count} rows in the original, {rebuilt.get(table)} after rebuild")
    return problems


def repair(db_path: str, rebuild: bool = False, accept_loss: bool = False,
           running_check: Callable[[], Optional[str]] = bot_is_running, report: Callable[[str], None] = print) -> int:
    if not os.path.isfile(db_path):
        report(f"[ERROR] Database not found: {db_path}")
        return 1
    reason = running_check()
    if reason:
        report(f"[ERROR] Refusing to touch the database while the bot is running: {reason}.")
        report("        Stop the bot first (python scripts/windows_keepalive_watchdog.py --stop).")
        return 3

    result = integrity_check(db_path)
    report(f"Integrity check: {result[:5]}")
    if result == ["ok"] and not rebuild:
        report("[OK] Database is healthy; nothing to do.")
        return 0
    if not rebuild:
        report("[WARN] Integrity problems found. Re-run with --rebuild to rebuild the database.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.pre-repair-{stamp}.bak"
    backup_database(db_path, backup_path)
    report(f"[OK] Full backup (SQLite backup API): {backup_path}")

    fixed_path = f"{db_path}.rebuilt-{stamp}"
    executed, errors = rebuild_database(backup_path, fixed_path)
    report(f"Rebuild: {executed} statements executed, {len(errors)} failed")
    for error in errors[:20]:
        report(f"  - {error}")

    backup_conn = sqlite3.connect(backup_path)
    fixed_conn = sqlite3.connect(fixed_path)
    try:
        problems = compare_counts(table_row_counts(backup_conn), table_row_counts(fixed_conn))
        fixed_ok = [row[0] for row in fixed_conn.execute("PRAGMA integrity_check;").fetchall()] == ["ok"]
    finally:
        fixed_conn.close()
        backup_conn.close()
    for problem in problems:
        report(f"  ! {problem}")
    if not fixed_ok:
        report(f"[ERROR] The rebuilt database is not healthy; original left untouched ({fixed_path} kept).")
        return 2
    if (problems or errors) and not accept_loss:
        report(f"[ERROR] Rows would be lost; original left untouched. Inspect {fixed_path} and re-run with "
               "--accept-loss to replace the database anyway.")
        return 2

    reason = running_check()
    if reason:
        report(f"[ERROR] The bot was started meanwhile ({reason}); original left untouched.")
        return 3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except sqlite3.DatabaseError as e:
        report(f"[WARN] WAL checkpoint failed ({e}); the backup contains the WAL content.")
    finally:
        conn.close()
    wal_path = f"{db_path}-wal"
    if os.path.exists(wal_path) and os.path.getsize(wal_path) > 0:
        report("[ERROR] The -wal file still holds data (another process uses the database); original left untouched.")
        return 3
    os.replace(fixed_path, db_path)
    for suffix in ("-wal", "-shm"):
        leftover = f"{db_path}{suffix}"
        if os.path.exists(leftover):
            os.remove(leftover)
    report(f"[OK] {db_path} replaced by the rebuilt database. Backup: {backup_path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check or rebuild the Channel Cloner SQLite database (bot must be stopped)")
    parser.add_argument("db_path", nargs="?", help="Database file (default: DB_PATH from the configuration)")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the database from a dump")
    parser.add_argument("--accept-loss", action="store_true", help="Replace even if some rows cannot be recovered")
    args = parser.parse_args(argv)
    db_path = os.path.abspath(args.db_path) if args.db_path else default_db_path()
    return repair(db_path, rebuild=args.rebuild, accept_loss=args.accept_loss)


if __name__ == "__main__":
    sys.exit(main())
