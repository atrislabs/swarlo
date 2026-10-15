#!/usr/bin/env python3
"""Judge for Real Coordination (cmp-real-coordination): do the agents running on this machine actually coordinate through Swarlo?

Reads the live hub database (~/.swarlo/atris.db, hub "atris") over the last 24 hours.
1. Noise: at most 10% of posts repeat a post the same member already made (same first 80 chars).
2. Use: 3+ members other than the orchestrator posted a claim, result, failed, or blocked.
3. Closure: 1+ claim by one of those members reached status done.
Last line: "repeats X, members Y, closed Z" so the campaign trace can grade it.
"""
import os
import sqlite3
import sys

DB = os.environ.get("SWARLO_DB", os.path.expanduser("~/.swarlo/atris.db"))
HUB = os.environ.get("SWARLO_HUB", "atris")
BOTS = ("orchestrator",)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute(
    "SELECT member_id, kind, status, substr(content, 1, 80) FROM posts "
    "WHERE hub_id = ? AND created_at > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day') ORDER BY created_at",
    (HUB,),
).fetchall()

seen, repeats = set(), 0
for member, _, _, head in rows:
    key = (member, head)
    repeats += key in seen
    seen.add(key)
repeat_rate = repeats / len(rows) if rows else 0.0

working = {m for m, kind, _, _ in rows if m not in BOTS and kind in ("claim", "result", "failed", "blocked")}
closed = sum(1 for m, kind, status, _ in rows if m not in BOTS and kind == "claim" and status == "done")

top = con.execute(
    "SELECT member_id, kind, count(*) FROM posts WHERE hub_id = ? AND created_at > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day') "
    "GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 5",
    (HUB,),
).fetchall()
print(f"last 24h: {len(rows)} posts · loudest: " + ", ".join(f"{m}/{k} {c}" for m, k, c in top))

fails = []
if repeat_rate > 0.10:
    fails.append("cut repeat posts")
if len(working) < 3:
    fails.append(f"get {3 - len(working)} more live agents claiming and reporting")
if closed < 1:
    fails.append("close one claim end to end")
score = f"repeats {repeat_rate:.2f}, members {len(working)}, closed {closed}"
if fails:
    print(f"not yet · {score} · next: {'; '.join(fails)}"[:200])
    sys.exit(1)
print(f"pass · {score}")
