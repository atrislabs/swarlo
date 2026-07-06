"""Canonical source for the swarlo pre-commit hook.

This constant is the source of truth for the hook content. The sibling
`scripts/swarlo-precommit-hook` file is a verbatim copy kept for people
who want to `cp` the hook into their repo without needing swarlo
installed. A pytest regression verifies the two stay byte-identical.

The CLI subcommand `swarlo install-hook` reads SOURCE from here and
writes it to the caller's `.git/hooks/pre-commit`, which removes the
need to locate `scripts/` at runtime (it may not be packaged in wheel
installs).
"""

SOURCE = '''#!/usr/bin/env python3
"""Swarlo pre-commit hook — refuse to commit files claimed by other agents.

Install in a consumer repo:

    cp scripts/swarlo-precommit-hook /path/to/repo/.git/hooks/pre-commit
    chmod +x /path/to/repo/.git/hooks/pre-commit

What it does on every `git commit`:

1. Reads ~/.swarlo/config.json for the member_id, server URL, and api_key.
2. Asks the swarlo server which files are currently claimed.
3. Compares against the staged files in the current commit.
4. If a staged file is claimed by ANOTHER member, blocks the commit and
   prints which agent owns it. The user can then either coordinate with
   them, wait, or override with `git commit --no-verify`.

Files claimed by *this* member are allowed (you can't conflict with
yourself). Files with no claim are allowed (the honor system is gone).

Why this exists: agents editing the same file in parallel caused multiple
revert wars during the 2026-04-10 RLEF sprint. The chat-based "EDITING
<filepath>" convention was an honor system on top of a chat board. This
hook makes the OS enforce it instead.

Fail-open behavior: if the swarlo server is unreachable, the api key is
missing, or the network call times out, the hook prints a warning and
ALLOWS the commit. We never want to block productive work because the
coordination layer is down.

Exit codes:
    0 — no conflicts, commit allowed
    1 — at least one staged file is claimed by another member, blocked
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path.home() / ".swarlo" / "config.json"
TIMEOUT_SECONDS = 3


def _load_config() -> dict | None:
    """Load swarlo config. Returns None if missing or unreadable."""
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return None


def _staged_files() -> list[str]:
    """Files in the current commit (added/modified, not deleted)."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            text=True,
            timeout=5,
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def _fetch_file_claims(server: str, hub: str, api_key: str) -> list[dict] | None:
    """GET /api/{hub}/file-claims. Returns list of claim dicts, or None on failure."""
    url = f"{server.rstrip('/')}/api/{hub}/file-claims"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode())
            return data.get("files") or []
    except Exception:
        return None


def _claim_file(server: str, hub: str, api_key: str, file_path: str) -> bool:
    """POST a file claim so other agents see we are touching this file.

    Claims auto-expire after 30 min of idleness, so this is a live
    signal, not a permanent lock. Fail-silent on any error: publishing
    is best-effort and must never block a commit.
    """
    url = f"{server.rstrip('/')}/api/{hub}/channels/general/claim-file"
    body = json.dumps({
        "file_path": file_path,
        "content": "auto-claimed by pre-commit hook",
    }).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False


def main() -> int:
    config = _load_config()
    if not config:
        print("[swarlo-hook] no ~/.swarlo/config.json — allowing commit", file=sys.stderr)
        return 0

    # Env vars override config so the same hook works for multiple identities
    # on one machine (human + agents) without editing ~/.swarlo/config.json.
    member_id = os.environ.get("SWARLO_MEMBER_ID") or config.get("member_id")
    server = os.environ.get("SWARLO_SERVER") or config.get("server")
    hub = os.environ.get("SWARLO_HUB") or config.get("hub", "atris")
    api_key = os.environ.get("SWARLO_API_KEY") or config.get("api_key")

    if not member_id or not server or not api_key:
        print(
            "[swarlo-hook] config missing member_id/server/api_key — allowing commit",
            file=sys.stderr,
        )
        return 0

    staged = _staged_files()
    if not staged:
        return 0  # nothing to check

    claims = _fetch_file_claims(server, hub, api_key)
    if claims is None:
        print(
            "[swarlo-hook] swarlo server unreachable — allowing commit (fail-open)",
            file=sys.stderr,
        )
        return 0

    # Files claimed by anyone (so we know what NOT to re-claim) and the
    # subset claimed by OTHER members (so we know what to block on).
    all_claimed_files: set[str] = set()
    others_claims: dict[str, dict] = {}
    for c in claims:
        fp = c.get("file_path")
        if not fp:
            continue
        all_claimed_files.add(fp)
        if c.get("member_id") != member_id:
            others_claims[fp] = c

    conflicts = [(f, others_claims[f]) for f in staged if f in others_claims]

    if not conflicts:
        # No conflicts. Publish my intent to work on the staged files
        # so the next agent's hook sees them as held. Opt-out with
        # SWARLO_HOOK_AUTO_CLAIM=0. Fail-silent — never block a commit.
        if os.environ.get("SWARLO_HOOK_AUTO_CLAIM", "1") != "0":
            unclaimed = [f for f in staged if f not in all_claimed_files]
            for f in unclaimed:
                _claim_file(server, hub, api_key, f)
        return 0

    # Block the commit and print who owns each file
    print()
    print("\\033[1;31m✗ swarlo: commit blocked — files are claimed by other agents\\033[0m", file=sys.stderr)
    print(file=sys.stderr)
    for f, c in conflicts:
        owner = c.get("claimed_by") or c.get("member_id") or "unknown"
        print(f"  \\033[33m{f}\\033[0m  →  claimed by \\033[36m{owner}\\033[0m", file=sys.stderr)
    print(file=sys.stderr)
    print("Options:", file=sys.stderr)
    print("  1. Coordinate on the swarlo board and have them release the claim", file=sys.stderr)
    print("  2. Wait for their work to ship (claims auto-expire after 30 min idle)", file=sys.stderr)
    print("  3. Override with: git commit --no-verify", file=sys.stderr)
    print(file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''
