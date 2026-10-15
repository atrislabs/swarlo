#!/usr/bin/env python3
"""Judge for Self-Pruning (cmp-self-pruning): the Swarlo hub keeps itself lean, on its own, without losing history.

Read only. Passes when all hold on hub `atris` (~/.swarlo):
  1. no posts older than 30 days in the live board, except open claims
  2. nothing lost: live posts + archived posts >= 5155 (hub atris on 2026-10-15)
  3. no score rows older than 7 days
  4. no members unseen for 30+ days
  5. every *.log in ~/.swarlo is 10 MB or less, and at most 3 atris.db backups remain
  6. ~/.swarlo is 60 MB or less in total
  7. it is automatic: a scheduled prune (trigger "schedule" in ~/.swarlo/prune.jsonl) ran in the last 26 hours
The last line carries every number and the next move.
"""
import datetime as dt
import glob
import json
import os
import sqlite3
import sys

HOME = os.path.expanduser("~/.swarlo")
DB = os.path.join(HOME, "atris.db")
HUB = "atris"
FLOOR = 5155
LOG_MB, BACKUPS, TOTAL_MB = 10, 3, 60


def ago(days):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def q(con, sql, *args):
    return con.execute(sql, args).fetchone()[0] or 0


def size_mb(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1e6


con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
old_posts = q(con, "SELECT COUNT(*) FROM posts WHERE hub_id = ? AND created_at < ? AND NOT (kind = 'claim' AND status = 'open')", HUB, ago(30))
live = q(con, "SELECT COUNT(*) FROM posts WHERE hub_id = ?", HUB)
old_scores = q(con, "SELECT COUNT(*) FROM scores WHERE hub_id = ? AND computed_at < ?", HUB, ago(7))
idle = q(con, "SELECT COUNT(*) FROM members WHERE hub_id = ? AND (last_seen IS NULL OR last_seen < ?)", HUB, ago(30))
con.close()

archived = 0
for path in glob.glob(os.path.join(HOME, "archive", "*.db")):
    try:
        a = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        archived += q(a, "SELECT COUNT(*) FROM posts WHERE hub_id = ?", HUB)
        a.close()
    except sqlite3.Error:
        pass

big_logs = [os.path.basename(p) for p in glob.glob(os.path.join(HOME, "*.log")) if os.path.getsize(p) > LOG_MB * 1e6]
backups = len(glob.glob(os.path.join(HOME, "atris.db.backup*")))
total = size_mb(HOME)

last_scheduled = None
try:
    for line in open(os.path.join(HOME, "prune.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("trigger") == "schedule" and r.get("ok"):
            last_scheduled = max(last_scheduled or "", r.get("at", ""))
except OSError:
    pass
recent = bool(last_scheduled) and last_scheduled >= ago(26 / 24)

fails = []
if old_posts:
    fails.append("archive posts older than 30 days")
if live + archived < FLOOR:
    fails.append(f"history lost: live {live} + archived {archived} < {FLOOR}")
if old_scores:
    fails.append("drop score rows older than 7 days")
if idle:
    fails.append("remove members unseen for 30 days")
if big_logs:
    fails.append("rotate logs over 10 MB")
if backups > BACKUPS:
    fails.append("keep only the 3 newest backups")
if total > TOTAL_MB:
    fails.append("shrink ~/.swarlo")
if not recent:
    fails.append("make the hub prune itself on a schedule (prune.jsonl, trigger schedule)")

nums = (f"posts>30d {old_posts}, kept {live}+{archived}, scores>7d {old_scores}, idle members {idle}, "
        f"big logs {len(big_logs)}, backups {backups}, size {total:.0f}MB, last scheduled prune {last_scheduled or 'never'}")
if fails:
    print(f"not yet · {nums} · next: {fails[0]}")
    sys.exit(1)
print(f"pass · {nums}")
