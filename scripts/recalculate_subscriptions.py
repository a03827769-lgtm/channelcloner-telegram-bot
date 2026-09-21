import asyncio
import os
import sys
import argparse
from datetime import datetime, timezone

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db_manager import db_manager
from config.settings import settings

async def audit_and_recalculate(apply_fixes: bool = False):
    print(f"[*] Connecting to database at: {db_manager.db_path}")
    await db_manager.init_db()

    async with db_manager.get_connection() as db:
        cursor = await db.execute('''
            SELECT s.user_id, s.tier, s.expires_at, s.stars_spent,
                   COUNT(p.id) as payment_count,
                   COALESCE(SUM(p.amount), 0) as total_payments_sum
            FROM subscriptions s
            LEFT JOIN payments p ON s.user_id = p.user_id
            GROUP BY s.user_id
        ''')
        rows = await cursor.fetchall()

    print(f"[*] Found {len(rows)} total subscription records to inspect.")
    anomalies = []

    for r in rows:
        uid = r[0]
        tier = r[1]
        exp = r[2]
        stars_spent = r[3]
        pay_count = r[4]
        pay_sum = r[5]

        # Check if user has VIP tier without corresponding VIP payments or admin grant
        if tier == "vip":
            # Check payments table for pro purchases while tier ended up vip
            async with db_manager.get_connection() as db:
                p_cur = await db.execute(
                    "SELECT tier, amount, created_at FROM payments WHERE user_id = ? ORDER BY id DESC",
                    (uid,)
                )
                payments = await p_cur.fetchall()

            # If the most recent payment was 'pro' but current tier is 'vip'
            if payments and payments[0][0] == "pro":
                anomalies.append({
                    "user_id": uid,
                    "issue": "Recent payment is 'pro' but current tier is 'vip'",
                    "current_tier": tier,
                    "target_tier": "pro",
                    "expires_at": exp
                })

    print(f"[*] Audit complete. Total anomalies detected: {len(anomalies)}")

    for a in anomalies:
        print(f"    - User {a['user_id']}: {a['issue']} (expires: {a['expires_at']})")

    if anomalies and apply_fixes:
        print("[+] Applying fixes to database...")
        async with db_manager.write_transaction() as db:
            for a in anomalies:
                await db.execute(
                    "UPDATE subscriptions SET tier = ? WHERE user_id = ?",
                    (a["target_tier"], a["user_id"])
                )
            await db.commit()
        print(f"[SUCCESS] Fixed {len(anomalies)} subscription anomalies.")
    elif anomalies:
        print("[INFO] Run with --apply to commit these repairs.")
    else:
        print("[SUCCESS] Zero subscription anomalies detected. Database integrity is verified!")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Audit and recalculate subscriptions to eliminate pricing exploits.")
    parser.add_argument("--apply", action="store_true", help="Apply fixes to database")
    args = parser.parse_args()
    asyncio.run(audit_and_recalculate(apply_fixes=args.apply))
