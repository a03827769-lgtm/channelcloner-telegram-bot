import os
import pymysql
from pathlib import Path

def init_mysql():
    root_conn = pymysql.connect(
        host=os.environ.get('MYSQL_HOST', '127.0.0.1'),
        port=int(os.environ.get('MYSQL_PORT', 3306)),
        user=os.environ.get('MYSQL_USER', 'root'),
        password=os.environ.get('MYSQL_PASSWORD', ''),
        charset='utf8mb4',
        autocommit=True
    )
    with root_conn.cursor() as cur:
        print("Creating database channelcloner...")
        cur.execute("CREATE DATABASE IF NOT EXISTS `channelcloner` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        print("Granting user permissions...")
        cur.execute("CREATE USER IF NOT EXISTS 'cloner_user'@'%' IDENTIFIED BY 'cloner_pass_2026';")
        cur.execute("GRANT ALL PRIVILEGES ON `channelcloner`.* TO 'cloner_user'@'%';")
        cur.execute("FLUSH PRIVILEGES;")
    root_conn.close()

    # Now connect to channelcloner
    db_conn = pymysql.connect(
        host='127.0.0.1',
        port=3306,
        user='cloner_user',
        password='cloner_pass_2026',
        database='channelcloner',
        charset='utf8mb4',
        autocommit=True
    )

    schema_sql = Path("miniapp/sql/schema.sql").read_text(encoding="utf-8")
    seed_sql = Path("miniapp/sql/seed_data.sql").read_text(encoding="utf-8")

    with db_conn.cursor() as cur:
        print("Executing schema.sql...")
        for statement in schema_sql.split(";"):
            stmt = statement.strip()
            if stmt:
                cur.execute(stmt)

        print("Executing seed_data.sql...")
        for statement in seed_sql.split(";"):
            stmt = statement.strip()
            if stmt:
                cur.execute(stmt)

        cur.execute("SHOW TABLES;")
        tables = [t[0] for t in cur.fetchall()]
        print("Tables in channelcloner database:", tables)

    db_conn.close()
    print("MySQL initialization complete!")

if __name__ == "__main__":
    init_mysql()
