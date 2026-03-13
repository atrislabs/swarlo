"""CLI entry point for Swarlo."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


CONFIG_ENV = "SWARLO_CONFIG"


def _config_path() -> Path:
    override = os.getenv(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".swarlo" / "config.json"


def _load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_config(config: dict) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")


def _request(method: str, url: str, payload: dict | None = None, api_key: str | None = None) -> tuple[int, dict]:
    headers = {}
    data = None
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
        body = err.read().decode()
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"error": body}
        return err.code, payload


def _require_runtime(args, *, auth: bool = True, hub: bool = True) -> dict:
    config = _load_config()
    runtime = {
        "server": getattr(args, "server", None) or os.getenv("SWARLO_SERVER") or config.get("server"),
        "api_key": getattr(args, "api_key", None) or os.getenv("SWARLO_API_KEY") or config.get("api_key"),
        "hub": getattr(args, "hub", None) or os.getenv("SWARLO_HUB") or config.get("hub"),
    }

    if not runtime["server"]:
        raise SystemExit("Missing server. Run `swarlo join --server ...` or pass `--server`.")
    if auth and not runtime["api_key"]:
        raise SystemExit("Missing api key. Run `swarlo join ...` first or pass `--api-key`.")
    if hub and not runtime["hub"]:
        raise SystemExit("Missing hub. Run `swarlo join ...` first or pass `--hub`.")
    return runtime


def _print_posts(posts: list[dict]) -> None:
    if not posts:
        print("No posts.")
        return
    for post in posts:
        prefix = f"[{post['kind']}]"
        if post.get("task_key"):
            prefix += f" {post['task_key']}"
        print(f"{prefix} {post['member_name']}: {post['content']}")


def _print_claims(claims: list[dict]) -> None:
    if not claims:
        print("No open claims.")
        return
    for claim in claims:
        print(f"[claim] {claim['task_key']} {claim['member_name']}: {claim['content']}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Swarlo — agent coordination protocol")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the Swarlo server")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--db", default="swarlo.db", help="SQLite database path")
    serve.add_argument("--git-dir", default="swarlo.git", help="Bare git repo path for DAG layer")

    join = sub.add_parser("join", help="Register a member and save local config")
    join.add_argument("--server", required=True)
    join.add_argument("--hub", required=True)
    join.add_argument("--member-id", required=True)
    join.add_argument("--member-name")
    join.add_argument("--member-type", default="agent")

    read = sub.add_parser("read", help="Read a channel")
    read.add_argument("channel")
    read.add_argument("--limit", type=int, default=10)
    read.add_argument("--server")
    read.add_argument("--hub")
    read.add_argument("--api-key")

    claims = sub.add_parser("claims", help="List open claims")
    claims.add_argument("--channel")
    claims.add_argument("--server")
    claims.add_argument("--hub")
    claims.add_argument("--api-key")

    post = sub.add_parser("post", help="Post to a channel")
    post.add_argument("channel")
    post.add_argument("content")
    post.add_argument("--kind", default="message")
    post.add_argument("--task-key")
    post.add_argument("--server")
    post.add_argument("--hub")
    post.add_argument("--api-key")

    claim = sub.add_parser("claim", help="Claim a task")
    claim.add_argument("channel")
    claim.add_argument("task_key")
    claim.add_argument("content")
    claim.add_argument("--server")
    claim.add_argument("--hub")
    claim.add_argument("--api-key")

    report = sub.add_parser("report", help="Report task status")
    report.add_argument("channel")
    report.add_argument("task_key")
    report.add_argument("status", choices=["done", "failed", "blocked"])
    report.add_argument("content")
    report.add_argument("--server")
    report.add_argument("--hub")
    report.add_argument("--api-key")

    init = sub.add_parser("init", help="Enable Swarlo for this repo")

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "init":
        import subprocess
        # Find project root
        try:
            root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            root = os.getcwd()

        # Write opt-in marker
        marker_dir = os.path.join(root, ".swarlo")
        os.makedirs(marker_dir, exist_ok=True)
        marker = os.path.join(marker_dir, "enabled.json")
        if not os.path.exists(marker):
            with open(marker, "w") as f:
                f.write('{"enabled": true}\n')
            print(f"Created {marker}")
        else:
            print(f"Already enabled: {marker}")

        # Write session-start hook
        hook_dir = os.path.join(root, ".claude", "hooks")
        os.makedirs(hook_dir, exist_ok=True)
        hook = os.path.join(hook_dir, "session-start.sh")
        if not os.path.exists(hook):
            hook_content = '''#!/bin/bash
# Swarlo activation — checks repo opt-in + user config + server health
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -z "$ROOT" ] && exit 0
[ ! -f "$ROOT/.swarlo/enabled.json" ] && exit 0
CONFIG="$HOME/.swarlo/config.json"
[ ! -f "$CONFIG" ] && exit 0
SERVER=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('server',''))" 2>/dev/null)
[ -z "$SERVER" ] && exit 0
HEALTH=$(curl -s --max-time 2 "$SERVER/api/health" 2>/dev/null)
if echo "$HEALTH" | grep -q "ok"; then
  MEMBER=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('member_name',''))" 2>/dev/null)
  HUB=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('hub',''))" 2>/dev/null)
  echo "[swarlo] active. member: $MEMBER, hub: $HUB. run: swarlo read general"
fi
'''
            with open(hook, "w") as f:
                f.write(hook_content)
            os.chmod(hook, 0o755)
            print(f"Created {hook}")
        else:
            print(f"Hook exists: {hook}")

        print("Swarlo enabled for this repo.")
        if not os.path.exists(os.path.expanduser("~/.swarlo/config.json")):
            print("Next: run `swarlo join --server <url> --hub <hub> --member-id <id>` to connect.")
        return

    if args.command == "serve":
        import uvicorn
        from .sqlite_backend import SQLiteBackend
        from .git_dag import GitDAG
        from .server import app, set_backend, set_dag

        set_backend(SQLiteBackend(args.db))
        dag = GitDAG(args.git_dir)
        dag.init()
        set_dag(dag)
        print(f"Swarlo server starting on {args.host}:{args.port}")
        print(f"Database: {args.db}")
        print(f"Git repo: {args.git_dir}")
        uvicorn.run(app, host=args.host, port=args.port)
        return

    if args.command == "join":
        status, body = _request(
            "POST",
            f"{args.server.rstrip('/')}/api/register",
            {
                "member_id": args.member_id,
                "member_type": args.member_type,
                "member_name": args.member_name or args.member_id,
                "hub_id": args.hub,
            },
        )
        if status not in (200, 201):
            raise SystemExit(f"Join failed ({status}): {body}")
        _save_config(
            {
                "server": args.server.rstrip("/"),
                "hub": args.hub,
                "api_key": body["api_key"],
                "member_id": body["member_id"],
                "member_name": args.member_name or args.member_id,
                "member_type": args.member_type,
            }
        )
        print(f"Joined hub `{args.hub}` as `{body['member_id']}`")
        print(f"Saved config to {_config_path()}")
        return

    if args.command == "read":
        runtime = _require_runtime(args)
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{args.channel}/posts?limit={args.limit}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Read failed ({status}): {body}")
        _print_posts(body.get("posts", []))
        return

    if args.command == "claims":
        runtime = _require_runtime(args)
        suffix = f"?channel={args.channel}" if args.channel else ""
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/claims{suffix}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Claims failed ({status}): {body}")
        _print_claims(body.get("claims", []))
        return

    if args.command == "post":
        runtime = _require_runtime(args)
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{args.channel}/posts",
            {"content": args.content, "kind": args.kind, "task_key": args.task_key},
            api_key=runtime["api_key"],
        )
        if status not in (200, 201):
            raise SystemExit(f"Post failed ({status}): {body}")
        print(f"Posted [{body['kind']}] to #{body['channel']}")
        return

    if args.command == "claim":
        runtime = _require_runtime(args)
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{args.channel}/claim",
            {"task_key": args.task_key, "content": args.content},
            api_key=runtime["api_key"],
        )
        if status == 409:
            raise SystemExit(f"Claim conflict: {body}")
        if status not in (200, 201):
            raise SystemExit(f"Claim failed ({status}): {body}")
        print(f"Claimed {args.task_key} on #{args.channel}")
        return

    if args.command == "report":
        runtime = _require_runtime(args)
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{args.channel}/report",
            {"task_key": args.task_key, "status": args.status, "content": args.content},
            api_key=runtime["api_key"],
        )
        if status not in (200, 201):
            raise SystemExit(f"Report failed ({status}): {body}")
        print(f"Reported {args.status} for {args.task_key} on #{args.channel}")
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
