#!/usr/bin/env python3
"""
GitHub webhook deployer (optional) for the documented VPS deployment.

Listens on WEBHOOK_BIND:WEBHOOK_PORT (default 127.0.0.1:9000, published by nginx over HTTPS as
/deploy-webhook) and deploys when a release tag is pushed:

    git fetch --tags --prune origin
    git checkout --detach refs/tags/<tag>
    docker compose --profile docker-bot up -d --build telegram-cloner

Security:
  * WEBHOOK_SECRET is mandatory; every POST needs a valid X-Hub-Signature-256 (HMAC-SHA256).
  * The request body is capped (MAX_BODY_BYTES) before it is read and hashed.
  * Deliveries are de-duplicated by X-GitHub-Delivery (GitHub retries) and one deploy runs at a time.
  * Only tags matching DEPLOY_TAG_PATTERN are deployed; commands run without a shell.

Environment: WEBHOOK_SECRET (required), WEBHOOK_BIND, WEBHOOK_PORT, PROJECT_DIR (the git clone),
DEPLOY_TAG_PATTERN (regex, default v<major>.<minor>.<patch>[-suffix]).
"""

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple

logger = logging.getLogger("DeployWebhook")

WEBHOOK_BIND = os.getenv("WEBHOOK_BIND", "127.0.0.1")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "9000"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PROJECT_DIR = os.getenv("PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPLOY_TAG_PATTERN = os.getenv("DEPLOY_TAG_PATTERN", r"^v\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?$")
MAX_BODY_BYTES = 1024 * 1024
DEPLOY_STEP_TIMEOUT_SECONDS = 1800

_deploy_lock = threading.Lock()


class DeliveryCache:
    """Remembers the X-GitHub-Delivery IDs that triggered a deployment, so a redelivered event is not
    deployed twice (a delivery answered with 409/busy is not recorded and may be redelivered)."""

    def __init__(self, max_entries: int = 1000):
        self.max_entries = max_entries
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.Lock()

    def contains(self, delivery_id: str) -> bool:
        with self._lock:
            return bool(delivery_id) and delivery_id in self._seen

    def add(self, delivery_id: str) -> None:
        if not delivery_id:
            return
        with self._lock:
            self._seen[delivery_id] = None
            while len(self._seen) > self.max_entries:
                self._seen.popitem(last=False)


_deliveries = DeliveryCache()


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    if not secret or not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)


def deploy_tag_from_push(payload: dict, pattern: str = DEPLOY_TAG_PATTERN) -> Optional[str]:
    """The release tag of a push event, or None (branch pushes, deleted tags, non-matching tags)."""
    ref = payload.get("ref") or ""
    if not ref.startswith("refs/tags/") or payload.get("deleted"):
        return None
    tag = ref[len("refs/tags/"):]
    return tag if re.match(pattern, tag) else None


def evaluate_request(headers: Dict[str, str], body: bytes, secret: str, deliveries: DeliveryCache) -> Tuple[int, dict, Optional[str]]:
    """Validates a webhook call. Returns (HTTP status, response JSON, tag to deploy or None)."""
    if not secret:
        return 503, {"error": "WEBHOOK_SECRET is not configured"}, None
    if not verify_signature(secret, body, headers.get("X-Hub-Signature-256", "")):
        return 403, {"error": "invalid signature"}, None
    event = headers.get("X-GitHub-Event", "")
    if event == "ping":
        return 200, {"status": "pong"}, None
    if event != "push":
        return 200, {"status": "ignored", "reason": f"event {event!r}"}, None
    if deliveries.contains(headers.get("X-GitHub-Delivery", "")):
        return 200, {"status": "duplicate delivery ignored"}, None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"error": "invalid JSON"}, None
    tag = deploy_tag_from_push(payload)
    if not tag:
        return 200, {"status": "ignored", "reason": "not a release tag push"}, None
    return 202, {"status": "deployment_triggered", "tag": tag}, tag


def deploy(tag: str) -> bool:
    steps = [
        ["git", "fetch", "--tags", "--prune", "origin"],
        ["git", "checkout", "--detach", f"refs/tags/{tag}"],
        ["docker", "compose", "--profile", "docker-bot", "up", "-d", "--build", "telegram-cloner"],
    ]
    for step in steps:
        try:
            proc = subprocess.run(step, cwd=PROJECT_DIR, capture_output=True, text=True,
                                  timeout=DEPLOY_STEP_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"Deploy of {tag} failed at {' '.join(step)}: {e}")
            return False
        if proc.returncode != 0:
            logger.error(f"Deploy of {tag} failed at {' '.join(step)} (exit {proc.returncode}): "
                         f"{(proc.stderr or proc.stdout or '').strip()[-2000:]}")
            return False
    logger.info(f"Deploy of {tag} finished successfully")
    return True


def _run_deploy_in_background(tag: str) -> None:
    try:
        deploy(tag)
    finally:
        _deploy_lock.release()


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "ChannelClonerDeployWebhook/2.0"

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, {"status": "active", "service": "github-webhook-deployer"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(411, {"error": "Content-Length required"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._reply(413, {"error": "payload too large"})
            return
        body = self.rfile.read(length)
        headers = {key: self.headers.get(key, "") for key in ("X-Hub-Signature-256", "X-GitHub-Event", "X-GitHub-Delivery")}
        status, payload, tag = evaluate_request(headers, body, WEBHOOK_SECRET, _deliveries)
        if tag:
            if not _deploy_lock.acquire(blocking=False):
                self._reply(409, {"status": "busy", "message": "a deployment is already running"})
                return
            _deliveries.add(headers.get("X-GitHub-Delivery", ""))
            logger.info(f"Release tag {tag} pushed; deploying")
            threading.Thread(target=_run_deploy_in_background, args=(tag,), name=f"deploy-{tag}", daemon=True).start()
        self._reply(status, payload)

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET is not set: every deployment request will be rejected")
    server = ThreadingHTTPServer((WEBHOOK_BIND, WEBHOOK_PORT), WebhookHandler)
    logger.info(f"GitHub webhook deployer listening on {WEBHOOK_BIND}:{WEBHOOK_PORT} (project {PROJECT_DIR})")
    server.serve_forever()


if __name__ == "__main__":
    run()
