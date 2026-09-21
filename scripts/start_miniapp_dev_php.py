#!/usr/bin/env python3
"""
ChannelCloner Pro — Local PHP Mini App Development Server
Runs PHP built-in server with full router, MySQL connection and SPA fallback.
"""

import os
import sys
import subprocess
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

def main():
    port = os.getenv("MINIAPP_PORT", "8089")
    repo_root = Path(__file__).parent.parent.resolve()
    
    cmd = [
        "php",
        "-S", f"127.0.0.1:{port}",
        str(repo_root / "miniapp" / "router.php"),
        "-t", str(repo_root / "miniapp")
    ]

    env = os.environ.copy()
    env["MYSQL_HOST"] = env.get("MYSQL_HOST", "127.0.0.1")
    env["MYSQL_PORT"] = env.get("MYSQL_PORT", "3306")
    env["MYSQL_DATABASE"] = env.get("MYSQL_DATABASE", "channelcloner")
    env["MYSQL_USER"] = env.get("MYSQL_USER", "cloner_user")
    env["MYSQL_PASSWORD"] = env.get("MYSQL_PASSWORD", "cloner_pass_2026")

    print(f"🚀 ChannelCloner PHP Mini App running at http://127.0.0.1:{port}", flush=True)
    print(f"📊 Connected to MySQL: {env['MYSQL_HOST']}:{env['MYSQL_PORT']}/{env['MYSQL_DATABASE']}", flush=True)

    try:
        subprocess.run(cmd, env=env, cwd=str(repo_root))
    except KeyboardInterrupt:
        print("\n🛑 PHP server stopped.")

if __name__ == "__main__":
    main()
