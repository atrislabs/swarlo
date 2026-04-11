#!/usr/bin/env python3
"""Swarlo board monitor — polls the board, logs activity, flags stale claims.

Runs as a daemon. Writes to ~/.swarlo/monitor.log.
Check it anytime: tail -50 ~/.swarlo/monitor.log
"""

import json
import time
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swarlo import SwarloClient

SERVER = os.environ.get("SWARLO_URL", "http://localhost:8090")
HUB = os.environ.get("SWARLO_HUB", "atris")
POLL_INTERVAL = int(os.environ.get("SWARLO_POLL_SECONDS", "120"))  # 2 min default
LOG_PATH = Path.home() / ".swarlo" / "monitor.log"

seen_post_ids: set[str] = set()
last_member_count = 0


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def check_board(board: SwarloClient):
    global last_member_count

    # Health
    if not board.health():
        log("WARN: swarlo server unreachable")
        return

    # Members
    members = board.members()
    if len(members) != last_member_count:
        if last_member_count > 0:
            new_names = [m["member_name"] for m in members]
            log(f"MEMBERS: {len(members)} registered — {', '.join(new_names)}")
        last_member_count = len(members)

    # New posts
    posts = board.read("general", limit=20)
    new_count = 0
    for p in posts:
        pid = p["post_id"]
        if pid not in seen_post_ids:
            seen_post_ids.add(pid)
            tag = f"[{p['kind']}]" if p["kind"] != "message" else ""
            mentions = ""
            if p.get("mentions"):
                mentions = f" → @{', @'.join(p['mentions'])}"
            log(f"NEW {tag} {p['member_name']}: {p['content'][:120]}{mentions}")
            new_count += 1
    if new_count == 0:
        log("quiet — no new posts")

    # Open claims
    claims = board.claims()
    if claims:
        for c in claims:
            log(f"OPEN CLAIM: {c['member_name']} → {c['task_key']}: {c['content'][:80]}")

    # Check experiments channel too
    exp_posts = board.read("experiments", limit=5)
    for p in exp_posts:
        pid = p["post_id"]
        if pid not in seen_post_ids:
            seen_post_ids.add(pid)
            tag = f"[{p['kind']}]" if p["kind"] != "message" else ""
            log(f"NEW #experiments {tag} {p['member_name']}: {p['content'][:120]}")


def main():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log(f"Swarlo monitor started — server={SERVER} hub={HUB} poll={POLL_INTERVAL}s")

    board = SwarloClient(SERVER, hub=HUB)
    try:
        board.join("monitor", member_type="system", name="Monitor")
        log("Registered as system/Monitor")
    except Exception as e:
        log(f"Registration: {e}")

    # Seed seen posts so we don't replay history
    try:
        for ch in ["general", "experiments", "ops"]:
            for p in board.read(ch, limit=50):
                seen_post_ids.add(p["post_id"])
        log(f"Seeded {len(seen_post_ids)} existing posts")
    except Exception:
        pass

    while True:
        try:
            check_board(board)
        except KeyboardInterrupt:
            log("Monitor stopped")
            break
        except Exception as e:
            log(f"ERROR: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
