#!/usr/bin/env python3
"""
Remote deployment of a release tag to a VPS — the one supported deploy path (see DEPLOYMENT.md).

The server holds a git clone of this repository; this tool only drives it over SSH:

  1. (first time) git clone --branch <ref> <repo> <app-dir>
     (otherwise)  git fetch --tags --prune origin && git checkout --detach <ref>
  2. (--bootstrap) sudo bash deploy/oracle_master_setup.sh [--domain D --email E]
  3. docker compose --profile docker-bot up -d --build telegram-cloner
  4. waits until http://127.0.0.1:<port>/ready answers 200 (falls back to /health if /ready is missing)

Secrets never leave your machine: .env, the SQLite database, the vault key and Cloudflare credentials are
NOT copied. Create <app-dir>/.env on the server yourself (scp it once over a verified connection).

SSH host keys are verified (StrictHostKeyChecking=yes). For the very first connection either add the key to
known_hosts after checking its fingerprint (ssh-keyscan <host> | ssh-keygen -lf -) or pass
--accept-new-host-key (trust on first use; the key is pinned in known_hosts afterwards).

Example:
  python scripts/deploy_remote.py --host 203.0.113.10 --user ubuntu --key ~/.ssh/oracle_arm_key \\
      --ref v1.4.0 --repo https://github.com/<owner>/<repo>.git --bootstrap --domain app.example.com --email me@example.com
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
from typing import List, Optional

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
_EMAIL_RE = re.compile(r"^[^@\s'\"]+@[^@\s'\"]+\.[^@\s'\"]+$")


def validate_args(args) -> Optional[str]:
    """Returns an error message for unsafe or malformed arguments (they end up in a remote shell script)."""
    if not _HOST_RE.match(args.host or ""):
        return f"invalid --host: {args.host!r}"
    if not _USER_RE.match(args.user or ""):
        return f"invalid --user: {args.user!r}"
    if not _REF_RE.match(args.ref or "") or ".." in args.ref:
        return f"invalid --ref (tag, branch or commit expected): {args.ref!r}"
    if args.repo and not re.match(r"^(https://|git@|ssh://)[^\s'\"]+$", args.repo):
        return f"invalid --repo: {args.repo!r}"
    if args.domain and not _DOMAIN_RE.match(args.domain):
        return f"invalid --domain: {args.domain!r}"
    if args.domain and not (args.email and _EMAIL_RE.match(args.email)):
        return "--domain requires a valid --email (Let's Encrypt account)"
    if not re.match(r"^[~A-Za-z0-9._/-]+$", args.app_dir or ""):
        return f"invalid --app-dir: {args.app_dir!r}"
    return None


def _remote_path(path: str) -> str:
    """Quotes a remote path but keeps a leading ~/ expandable by the remote shell."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def build_remote_script(args) -> str:
    """The bash script executed on the server (stdin of `ssh ... bash -s`)."""
    app_dir = _remote_path(args.app_dir)
    ref = shlex.quote(args.ref)
    lines = [
        "set -euo pipefail",
        f"APP_DIR={app_dir}",
        f"REF={ref}",
        'if [ ! -d "$APP_DIR/.git" ]; then',
    ]
    if args.repo:
        lines += [
            f'  echo "[deploy] cloning {args.repo} ($REF) into $APP_DIR"',
            f'  git clone --branch "$REF" {shlex.quote(args.repo)} "$APP_DIR"',
        ]
    else:
        lines += [
            '  echo "[deploy] $APP_DIR is not a git clone; pass --repo for the first deployment" >&2',
            "  exit 3",
        ]
    lines += [
        "else",
        '  echo "[deploy] updating $APP_DIR to $REF"',
        '  git -C "$APP_DIR" fetch --tags --prune origin',
        '  git -C "$APP_DIR" checkout --detach "$REF"',
        "fi",
        'cd "$APP_DIR"',
        'echo "[deploy] now at $(git rev-parse --short HEAD) ($(git describe --tags --always))"',
    ]
    if args.bootstrap:
        setup_args = ""
        if args.domain:
            setup_args = f" --domain {shlex.quote(args.domain)} --email {shlex.quote(args.email)}"
        if args.with_anti_reclaim:
            setup_args += " --with-anti-reclaim"
        lines.append(f"sudo bash deploy/oracle_master_setup.sh{setup_args}")
    lines += [
        "if [ ! -f .env ]; then",
        '  echo "[deploy] $APP_DIR/.env is missing: create it from .env.example on the server (never shipped by this tool)" >&2',
        "  exit 4",
        "fi",
        "if docker info >/dev/null 2>&1; then DOCKER=docker; else DOCKER='sudo docker'; fi",
        "$DOCKER compose --profile docker-bot up -d --build telegram-cloner",
        f"PORT={int(args.port)}",
        "for _ in $(seq 1 60); do",
        "  code=$(curl -s -o /dev/null -w '%{http_code}' \"http://127.0.0.1:$PORT/ready\" || true)",
        '  if [ "$code" = "200" ]; then echo "[deploy] READY"; exit 0; fi',
        '  if [ "$code" = "404" ] && curl -fsS -o /dev/null "http://127.0.0.1:$PORT/health"; then',
        '    echo "[deploy] LIVE (/health ok; this version has no /ready endpoint)"; exit 0',
        "  fi",
        "  sleep 5",
        "done",
        'echo "[deploy] the bot did not become ready within 5 minutes:" >&2',
        "$DOCKER compose --profile docker-bot logs --tail 80 telegram-cloner >&2 || true",
        "exit 5",
    ]
    return "\n".join(lines) + "\n"


def ssh_base_command(args) -> List[str]:
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", f"StrictHostKeyChecking={'accept-new' if args.accept_new_host_key else 'yes'}",
        "-o", "ServerAliveInterval=30",
    ]
    if args.key:
        cmd += ["-i", os.path.expanduser(args.key)]
    if args.port_ssh:
        cmd += ["-p", str(args.port_ssh)]
    return cmd + [f"{args.user}@{args.host}"]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy a release tag to the VPS over SSH (git-based, no secrets shipped)")
    parser.add_argument("--host", required=True, help="Server IP address or DNS name")
    parser.add_argument("--user", default="ubuntu", help="SSH user (ubuntu / opc)")
    parser.add_argument("--key", default=None, help="SSH private key file")
    parser.add_argument("--port-ssh", type=int, default=None, help="SSH port (default 22)")
    parser.add_argument("--ref", required=True, help="Release tag to deploy (e.g. v1.4.0)")
    parser.add_argument("--repo", default=None, help="Repository URL (needed for the first deployment only)")
    parser.add_argument("--app-dir", default="~/channelcloner", help="Clone directory on the server")
    parser.add_argument("--port", type=int, default=8080, help="Bot HTTP port on the server loopback (BOT_HTTP_PORT)")
    parser.add_argument("--bootstrap", action="store_true", help="Run deploy/oracle_master_setup.sh (first deployment)")
    parser.add_argument("--domain", default=None, help="With --bootstrap: domain for nginx + Let's Encrypt")
    parser.add_argument("--email", default=None, help="With --domain: Let's Encrypt account e-mail")
    parser.add_argument("--with-anti-reclaim", action="store_true", help="With --bootstrap: Oracle Always Free helper")
    parser.add_argument("--accept-new-host-key", action="store_true",
                        help="Trust the server's host key on first connection (pinned in known_hosts afterwards)")
    parser.add_argument("--dry-run", action="store_true", help="Print the remote script instead of running it")
    args = parser.parse_args(argv)

    error = validate_args(args)
    if error:
        print(f"[deploy] {error}", file=sys.stderr)
        return 2

    script = build_remote_script(args)
    if args.dry_run:
        print(script)
        return 0

    ssh_cmd = ssh_base_command(args)
    print(f"[deploy] {args.user}@{args.host}: deploying {args.ref}")
    try:
        result = subprocess.run(ssh_cmd + ["bash", "-s"], input=script, text=True)
    except FileNotFoundError:
        print("[deploy] the OpenSSH client (ssh) is not installed or not on PATH", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print(f"[deploy] FAILED (exit code {result.returncode})", file=sys.stderr)
        return result.returncode
    print("[deploy] SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
