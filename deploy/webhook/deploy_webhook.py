#!/usr/bin/env python3
"""
Lightweight Zero-Dependency GitHub Webhook Auto-Deploy Listener.
Listens on port 9000 (or internal) and triggers repository pull & rebuild when a push to main branch occurs.
"""

import os
import hmac
import hashlib
import subprocess
import shutil
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("DeployWebhook")

WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "9000"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # Optional GitHub webhook secret
PROJECT_DIR = os.getenv("PROJECT_DIR") or (os.path.expanduser("~/app") if os.path.exists(os.path.expanduser("~/app")) else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BRANCH_TARGET = "refs/heads/main"

_deploy_lock = threading.Lock()

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "active", "service": "github-webhook-deployer"}')

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        payload = self.rfile.read(content_length)

        # Strict signature verification: require secret to prevent unauthorized RCE
        if not WEBHOOK_SECRET:
            logger.warning("WEBHOOK_SECRET is not configured. Rejecting POST request for security.")
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "WEBHOOK_SECRET is required to trigger deployment"}')
            return

        signature = self.headers.get('X-Hub-Signature-256', '')
        expected = 'sha256=' + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid signature"}')
            return

        event = self.headers.get('X-GitHub-Event', '')
        if event == 'ping':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "pong"}')
            return

        if event == 'push':
            try:
                import json
                data = json.loads(payload.decode('utf-8'))
                if data.get('ref') == BRANCH_TARGET:
                    logger.info(f"Push event received for {BRANCH_TARGET}. Starting auto-deploy...")
                    
                    if not _deploy_lock.acquire(blocking=False):
                        logger.warning("Deployment already in progress. Rejecting concurrent webhook request.")
                        self.send_response(429)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"status": "busy", "message": "Deployment already in progress"}')
                        return

                    def _run_deploy():
                        try:
                            shell_bin = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
                            deploy_cmd = (
                                "git fetch origin main && "
                                "git merge --ff-only origin/main && "
                                "docker compose up -d --build"
                            )
                            proc = subprocess.run([shell_bin, "-c", deploy_cmd], cwd=PROJECT_DIR, capture_output=True, text=True)
                            logger.info(f"Deploy finished with returncode {proc.returncode}")
                        except Exception as de:
                            logger.error(f"Deployment error: {de}")
                        finally:
                            _deploy_lock.release()

                    import threading
                    threading.Thread(target=_run_deploy, daemon=True).start()
                    
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status": "deployment_triggered"}')
                    return
            except Exception as e:
                logger.error(f"Webhook error: {e}")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ignored"}')

def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)
    logger.info(f"GitHub Webhook Server listening on port {WEBHOOK_PORT}...")
    server.serve_forever()

if __name__ == "__main__":
    run()
