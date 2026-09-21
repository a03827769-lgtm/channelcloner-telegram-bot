#!/usr/bin/env python3
"""
ChannelCloner Pro — Cloudflare Watchdog Startup Script
Runs the 24/7 Cloudflare self-healing tunnel watchdog daemon.
"""

import os
import sys
from pathlib import Path

# Ensure UTF-8 stdout on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import logging
from services.cloudflare_tunnel_watchdog import CloudflareTunnelWatchdog

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [CloudflareWatchdog] %(message)s"
    )

    port = os.getenv("MINIAPP_PORT", os.getenv("PORT", "8089"))
    local_url = f"http://127.0.0.1:{port}"

    print("============================================================")
    print(" 🚀 ChannelCloner Pro — 24/7 Cloudflare Tunnel Watchdog")
    print(f" 🎯 Local Target: {local_url}")
    print(" 🛡️ Auto-Recovery: Enabled (Recycles on edge disconnect)")
    print(" 🔄 Real-time Bot URL Synchronization: Enabled")
    print("============================================================", flush=True)

    watchdog = CloudflareTunnelWatchdog(
        local_target=local_url,
        probe_interval=15,
        max_probe_failures=3
    )

    try:
        watchdog.run_forever()
    except KeyboardInterrupt:
        print("\n🛑 Watchdog stopped by user.")
        watchdog.stop()

if __name__ == "__main__":
    main()
