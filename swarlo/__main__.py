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
    """Get the config file path, respecting SWARLO_CONFIG env override."""
    override = os.getenv(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".swarlo" / "config.json"


def _load_config() -> dict:
    """Load config from disk, returning empty dict if missing."""
    path = _config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_config(config: dict) -> None:
    """Save config to disk, creating parent dirs if needed."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")


def _request(method: str, url: str, payload: dict | None = None, api_key: str | None = None) -> tuple[int, dict]:
    """Make an HTTP request to the swarlo server. Returns (status_code, response_dict)."""
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

    ping = sub.add_parser("ping", help="Lightweight check: anything new?")
    ping.add_argument("--member-id", help="Override member ID")
    ping.add_argument("--server")
    ping.add_argument("--hub")
    ping.add_argument("--api-key")

    mine = sub.add_parser("mine", help="What should I be working on?")
    mine.add_argument("--member-id", help="Override member ID")
    mine.add_argument("--server")
    mine.add_argument("--hub")
    mine.add_argument("--api-key")

    sub.add_parser("score", help="Coordination score").add_argument("--server")
    sub.add_parser("idle", help="Find idle agents").add_argument("--server")
    sub.add_parser("suggest", help="Auto-generate task suggestions").add_argument("--server")

    init = sub.add_parser("init", help="Enable Swarlo for this repo")

    install_hook = sub.add_parser(
        "install-hook",
        help="Install the swarlo pre-commit hook in this git repo",
    )
    install_hook.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing .git/hooks/pre-commit",
    )
    install_hook.add_argument(
        "--path", default=None,
        help="Target path for the hook (default: <repo>/.git/hooks/pre-commit)",
    )

    sub.add_parser(
        "doctor",
        help="Diagnose swarlo setup — config, server reachability, git hook, member registration",
    )

    return parser


# ── Doctor ──────────────────────────────────────────────────

# ANSI colors for doctor output. Fall back to empty strings when stdout
# is not a terminal (e.g. when captured in tests or piped).
def _colors() -> dict:
    if sys.stdout.isatty():
        return {
            "ok": "\033[32m",    # green
            "warn": "\033[33m",  # yellow
            "fail": "\033[31m",  # red
            "dim": "\033[2m",
            "reset": "\033[0m",
        }
    return {k: "" for k in ("ok", "warn", "fail", "dim", "reset")}


def _check(label: str, status: str, detail: str = "", colors: dict | None = None) -> None:
    """Print one doctor check line.

    status: 'ok' | 'warn' | 'fail'
    """
    c = colors or _colors()
    marks = {"ok": "✓", "warn": "!", "fail": "✗"}
    tag = f"{c[status]}{marks.get(status, '?')} {status.upper():<4}{c['reset']}"
    if detail:
        print(f"  {tag}  {label} {c['dim']}— {detail}{c['reset']}")
    else:
        print(f"  {tag}  {label}")


def _install_precommit_hook(repo_root: str, force: bool = False,
                            target_path: str | None = None,
                            quiet: bool = False) -> bool:
    """Install the swarlo pre-commit hook. Returns True if installed."""
    from swarlo._precommit_hook_source import SOURCE

    if target_path:
        target = Path(target_path).expanduser().resolve()
    else:
        target = Path(repo_root) / ".git" / "hooks" / "pre-commit"

    if target.exists() and not force:
        if not quiet:
            raise SystemExit(
                f"install-hook: {target} already exists. "
                "Pass --force to overwrite."
            )
        # quiet mode: skip silently (used by init)
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SOURCE)
    target.chmod(0o755)
    print(f"Installed swarlo pre-commit hook at {target}")
    return True


def _run_doctor() -> int:
    """Diagnose swarlo setup and print a per-check report.

    Returns 0 if every check is OK or WARN, 1 if any check is FAIL.
    Designed to be run by a human at any time — no side effects, no
    config writes, just reads and HTTP HEADs.

    Checks, in order:
      1. ~/.swarlo/config.json exists and parses as JSON
      2. Config has the required fields (server, hub, member_id, api_key)
      3. Server responds to /api/health
      4. Our member_id is registered on the server (listed in /members)
      5. Running inside a git repo (optional — only a WARN if not)
      6. Pre-commit hook is installed at .git/hooks/pre-commit
      7. The installed hook matches the canonical SOURCE
    """
    import subprocess
    from pathlib import Path

    colors = _colors()
    any_fail = False

    print()
    print(f"  {colors['dim']}swarlo doctor — diagnostics{colors['reset']}")
    print()

    # Check 1: config file exists and parses
    cfg_path = _config_path()
    config: dict = {}
    if not cfg_path.exists():
        _check(f"config file at {cfg_path}", "fail",
               "missing — run `swarlo join ...` first", colors)
        any_fail = True
    else:
        try:
            config = json.loads(cfg_path.read_text())
            _check(f"config file at {cfg_path}", "ok", "parsed OK", colors)
        except json.JSONDecodeError as exc:
            _check(f"config file at {cfg_path}", "fail",
                   f"invalid JSON: {exc}", colors)
            any_fail = True

    # Check 2: required fields present
    required = ["server", "hub", "member_id", "api_key"]
    missing = [k for k in required if not config.get(k)]
    if config and missing:
        _check("required config fields",
               "fail" if "server" in missing else "warn",
               f"missing: {', '.join(missing)}", colors)
        if "server" in missing:
            any_fail = True
    elif config:
        _check("required config fields", "ok",
               "server, hub, member_id, api_key present", colors)

    # Check 3: server is reachable
    server = config.get("server")
    server_ok = False
    if server:
        try:
            with urllib.request.urlopen(
                f"{server.rstrip('/')}/api/health", timeout=3
            ) as resp:
                body = json.loads(resp.read().decode())
                if body.get("status") == "ok":
                    _check(f"server health at {server}", "ok",
                           "responded with status=ok", colors)
                    server_ok = True
                else:
                    _check(f"server health at {server}", "warn",
                           f"unexpected body: {body}", colors)
        except Exception as exc:
            _check(f"server health at {server}", "fail",
                   f"unreachable: {exc}", colors)
            any_fail = True

    # Check 4: our member_id is registered
    hub = config.get("hub")
    member_id = config.get("member_id")
    api_key = config.get("api_key")
    if server_ok and hub and member_id and api_key:
        try:
            req = urllib.request.Request(
                f"{server.rstrip('/')}/api/{hub}/members",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                members = data.get("members") or []
                me = next((m for m in members if m.get("member_id") == member_id), None)
                if me:
                    _check(f"member_id '{member_id}' registered in hub '{hub}'",
                           "ok", f"member_name={me.get('member_name')}", colors)
                else:
                    _check(f"member_id '{member_id}' registered in hub '{hub}'",
                           "fail", "not found — run `swarlo join ...` again", colors)
                    any_fail = True
        except Exception as exc:
            _check(f"member lookup in hub '{hub}'", "warn",
                   f"could not verify: {exc}", colors)

    # Check 5: inside a git repo
    in_git = False
    repo_root: Path | None = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        repo_root = Path(result.stdout.strip())
        in_git = True
        _check(f"git repo at {repo_root}", "ok", "", colors)
    except (subprocess.CalledProcessError, FileNotFoundError):
        _check("git repo", "warn",
               "not inside a git repo — hook checks skipped", colors)

    # Check 6 + 7: pre-commit hook installed and matches canonical source
    if in_git and repo_root is not None:
        hook_path = repo_root / ".git" / "hooks" / "pre-commit"
        if not hook_path.exists():
            _check("pre-commit hook", "warn",
                   "not installed — run `swarlo install-hook`", colors)
        else:
            try:
                installed = hook_path.read_text()
            except Exception as exc:
                _check("pre-commit hook", "warn",
                       f"cannot read: {exc}", colors)
                installed = None

            if installed is not None:
                try:
                    from swarlo._precommit_hook_source import SOURCE
                    if installed == SOURCE:
                        _check("pre-commit hook matches canonical source",
                               "ok", str(hook_path), colors)
                    else:
                        _check("pre-commit hook matches canonical source",
                               "warn",
                               "drift detected — run `swarlo install-hook --force` to update",
                               colors)
                except ImportError:
                    _check("pre-commit hook canonical source",
                           "warn",
                           "could not import swarlo._precommit_hook_source",
                           colors)

    print()
    if any_fail:
        print(f"  {colors['fail']}At least one check failed.{colors['reset']} "
              f"Fix the issues above and re-run.")
        return 1
    print(f"  {colors['ok']}All checks passed.{colors['reset']}")
    return 0


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

        # Install pre-commit hook (skip if already present)
        _install_precommit_hook(root, force=False, quiet=True)

        # Run doctor to show setup status
        print()
        _run_doctor()

        if not os.path.exists(os.path.expanduser("~/.swarlo/config.json")):
            print()
            print("Next: run `swarlo join --server <url> --hub <hub> --member-id <id>` to connect.")
        return

    if args.command == "doctor":
        return _run_doctor()

    if args.command == "install-hook":
        import subprocess

        if not args.path:
            try:
                root = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
            except (subprocess.CalledProcessError, FileNotFoundError):
                raise SystemExit(
                    "install-hook: not inside a git repo. "
                    "cd into your repo, or pass --path explicitly."
                )
        else:
            root = os.getcwd()

        _install_precommit_hook(root, force=args.force, target_path=args.path)
        print()
        print("The hook blocks commits to files claimed by other agents.")
        print("It fail-opens if the swarlo server is unreachable or if")
        print("~/.swarlo/config.json has no api_key — so it's safe to")
        print("leave installed while you set things up.")
        print()
        print("Test it: `git commit --allow-empty -m test` should run the hook.")
        print("Bypass it: `git commit --no-verify`.")
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

    if args.command == "mine":
        runtime = _require_runtime(args)
        member_id = args.member_id or runtime.get("member_id", "unknown")
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/mine/{member_id}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Mine failed ({status}): {body}")
        if body.get("has_work"):
            for c in body["claims"]:
                print(f"  CLAIMED: {c['task_key']} — {c['content'][:60]}")
        else:
            print("No open work. Find something to do.")
        for a in body.get("assignments", []):
            print(f"  ASSIGNED: {a['task_key']} by {a.get('assigned_by','?')} — {a['content'][:60]}")
        return

    if args.command == "ping":
        runtime = _require_runtime(args)
        member_id = args.member_id or runtime.get("member_id", "unknown")
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/ping/{member_id}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Ping failed ({status}): {body}")
        if body.get("action_needed"):
            print(f"ACTION: {body['new_mentions']} mentions, {body['new_assigns']} assigns, {body['new_posts']} posts")
        else:
            print("Clear.")
        return

    if args.command == "score":
        runtime = _require_runtime(args)
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/score",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Score failed ({status}): {body}")
        print(f"Score: {body['coord_score']} | Shipped: {body['tasks_shipped']} | Active: {body['agents_active']} | Conflicts: {body['file_conflicts']}")
        return

    if args.command == "idle":
        runtime = _require_runtime(args)
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/idle",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Idle failed ({status}): {body}")
        if body["idle"]:
            for a in body["idle"]:
                print(f"  IDLE: {a['member_name']}")
        else:
            print("All agents producing.")
        return

    if args.command == "suggest":
        runtime = _require_runtime(args)
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/suggest",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Suggest failed ({status}): {body}")
        for s in body.get("suggestions", []):
            print(f"  {s['reason']}")
            print(f"    → {s['suggestion']}")
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
