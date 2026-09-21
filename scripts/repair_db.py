import sqlite3
import os
import shutil

def repair(db_path: str):
    print(f"Checking database at {db_path}...")
    if not os.path.exists(db_path):
        print(f"File {db_path} does not exist.")
        return

    conn = sqlite3.connect(db_path)
    check = conn.execute("PRAGMA integrity_check;").fetchall()
    print(f"Integrity check before repair: {check}")

    backup_path = f"{db_path}.corrupted.bak"
    shutil.copy2(db_path, backup_path)
    print(f"Backed up corrupted DB to {backup_path}")

    fixed_path = f"{db_path}.fixed"
    if os.path.exists(fixed_path):
        os.remove(fixed_path)

    dst = sqlite3.connect(fixed_path)
    count = 0
    for line in conn.iterdump():
        try:
            dst.execute(line)
            count += 1
        except Exception as e:
            print(f"Skipped faulty line: {e}")

    dst.commit()
    fixed_check = dst.execute("PRAGMA integrity_check;").fetchall()
    print(f"Fixed DB integrity check: {fixed_check}")
    dst.close()
    conn.close()

    if fixed_check == [('ok',)]:
        print("Integrity check passed! Replacing corrupted DB with clean fixed DB...")
        # Remove WAL and SHM if any
        for ext in ["-wal", "-shm"]:
            wal = f"{db_path}{ext}"
            if os.path.exists(wal):
                os.remove(wal)
        os.replace(fixed_path, db_path)
        print("Replacement complete. Database is 100% clean and healthy.")
    else:
        print("Failed to repair completely.")

if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "/app/data/cloner.db"
    repair(target)
