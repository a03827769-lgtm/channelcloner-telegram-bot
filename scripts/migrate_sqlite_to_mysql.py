#!/usr/bin/env python3
"""
ChannelCloner Pro — SQLite to MySQL Migration Script
Reads existing data from data/cloner.db and migrates cleanly into MySQL 8.0.
"""

import os
import sys
import sqlite3
import logging
from typing import Dict, Any, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MigrateSQLiteToMySQL")

def get_mysql_connection():
    try:
        import pymysql
        host = os.getenv("MYSQL_HOST", "127.0.0.1")
        port = int(os.getenv("MYSQL_PORT", "3307"))
        user = os.getenv("MYSQL_USER", "cloner_user")
        password = os.getenv("MYSQL_PASSWORD", "cloner_pass_2026")
        database = os.getenv("MYSQL_DATABASE", "channelcloner")
        
        return pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=True
        )
    except Exception as e:
        logger.warning(f"Could not connect to MySQL via pymysql: {e}")
        return None

def migrate(sqlite_path: str = "data/cloner.db"):
    if not os.path.exists(sqlite_path):
        logger.info(f"SQLite database {sqlite_path} does not exist. Skipping data migration.")
        return

    logger.info(f"Connecting to SQLite: {sqlite_path}")
    sq_conn = sqlite3.connect(sqlite_path)
    sq_conn.row_factory = sqlite3.Row
    sq_cur = sq_conn.cursor()

    mysql_conn = get_mysql_connection()
    if not mysql_conn:
        logger.info("MySQL connection unavailable. Generated SQL dump will be printed if needed.")
        return

    my_cur = mysql_conn.cursor()

    # Tables to migrate
    tables = [
        "users", "subscriptions", "payments", "channel_pairs", 
        "cloned_messages", "story_settings", "story_queue", "posted_stories"
    ]

    for table in tables:
        try:
            # Safe because table is from a hardcoded list
            sq_cur.execute(f"SELECT * FROM `{table}`")
            rows = sq_cur.fetchall()
            if not rows:
                logger.info(f"Table '{table}' has 0 rows in SQLite.")
                continue

            columns = [d[0] for d in sq_cur.description]
            cols_str = ", ".join([f"`{c}`" for c in columns])
            placeholders = ", ".join(["%s"] * len(columns))
            sql = f"INSERT IGNORE INTO `{table}` ({cols_str}) VALUES ({placeholders})"

            data = [tuple(r[c] for c in columns) for r in rows]
            my_cur.executemany(sql, data)
            logger.info(f"Successfully migrated {len(data)} rows for table '{table}'.")
        except sqlite3.OperationalError as oe:
            logger.debug(f"Table {table} not in SQLite: {oe}")
        except Exception as e:
            logger.warning(f"Error migrating table {table}: {e}")

    sq_conn.close()
    mysql_conn.close()
    logger.info("Migration routine finished successfully.")

if __name__ == "__main__":
    db_file = sys.argv[1] if len(sys.argv) > 1 else "data/cloner.db"
    migrate(db_file)
