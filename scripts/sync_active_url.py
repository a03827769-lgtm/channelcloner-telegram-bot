#!/usr/bin/env python3
"""
ChannelCloner Pro — Active URL Synchronizer
Syncs the live Cloudflare HTTPS URL to active_tunnel_url.txt and MySQL bot_settings.
"""

import os
import sys
from pathlib import Path
import pymysql

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

def sync(url: str):
    url = url.strip()
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    
    url_file = data_dir / "active_tunnel_url.txt"
    url_file.write_text(url, encoding="utf-8")
    print(f"✅ Saved to {url_file}: {url}")

    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = int(os.getenv("MYSQL_PORT", "3307"))
    user = os.getenv("MYSQL_USER", "cloner_user")
    password = os.getenv("MYSQL_PASSWORD", "cloner_pass_2026")
    database = os.getenv("MYSQL_DATABASE", "channelcloner")

    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=True
        )
        with conn.cursor() as cur:
            sql = "INSERT INTO bot_settings (key_name, value_text) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value_text = VALUES(value_text)"
            cur.execute(sql, ("webapp_url", url))
        conn.close()
        print(f"✅ Synchronized to MySQL database {database} on port {port}!")
    except Exception as e:
        print(f"⚠️ Could not sync to MySQL: {e}")

if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://deemed-pressure-tales-contracts.trycloudflare.com"
    sync(target_url)
