"""Pruning for a swarlo hub: old posts, home-dir files, and the daily schedule.

Move old board posts out of a live swarlo database into monthly archives.

archive_posts(db_path, archive_dir, hub, days=30)
    Every post in ``hub`` created more than ``days`` ago, except open claims,
    is copied together with all of its replies into
    ``archive_dir/<hub>-<YYYY-MM>.db``, where YYYY-MM is the post's own
    creation month. Archive tables are created from the live database's own
    CREATE statements, so the schema matches exactly. Once the copy is
    committed, the archive is re-counted; only if every selected post is
    there are the posts and their replies deleted from the live database.

    Copies use INSERT OR IGNORE, so a run interrupted between copy and delete
    is safe to repeat. Returns ``{archive_path: posts_archived}``.

drop_idle_members(db_path, hub, days=30)
    Deletes every member of ``hub`` whose ``last_seen`` is NULL or older than
    ``days``, the same rule the self-pruning judge counts. Returns the ids
    removed.

Disk hygiene for a swarlo home directory (normally ``~/.swarlo``).

rotate_logs(home, max_mb=10)
    Every ``*.log`` directly in ``home`` that is bigger than ``max_mb`` is
    copied to ``<name>.log.1`` (keeping only its newest ``max_mb`` bytes, and
    replacing any older ``.1``) and then truncated in place. Truncating rather
    than renaming keeps the inode, so daemons that hold the log open in append
    mode keep writing to the live file.

keep_backups(home, keep=3)
    Deletes all but the ``keep`` newest ``atris.db.backup*`` files directly in
    ``home``, newest by mtime (backup suffixes mix epoch and date stamps, so
    names do not sort). ``atris.db`` itself never matches the pattern.

rotate_logs and keep_backups return the paths they acted on.

Run log and daily schedule for the swarlo pruner.

record_run(home, trigger, ok, mb_before=None, **counts)
    Appends one JSON line to ``home/prune.jsonl``::

        {"at": ISO UTC, "trigger": ..., "ok": ..., **counts,
         "mb_before": x, "mb_after": y}

    ``mb_after`` is the size of ``home`` measured now; ``mb_before`` defaults
    to the same value when the caller did not measure it. Returns the record.

start_schedule(run_fn, home, interval_hours=24)
    Starts an asyncio task that calls ``run_fn()`` every ``interval_hours`` and
    records each call with trigger ``"schedule"``. ``run_fn`` may be sync or
    async and may return a dict of counts to include in the record. A run that
    raises is recorded with ``ok: false`` and the error, and the loop keeps
    going. The first wait picks up from the last scheduled run in
    ``prune.jsonl``, so restarting the hub does not prune again early.
    Returns the task; cancel it to stop.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TABLES = ("posts", "replies")


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _ensure_archive_schema(conn: sqlite3.Connection) -> None:
    """Create posts/replies (and their indexes) in ``arc`` from main's DDL.

    sqlite_master stores CREATE text with IF NOT EXISTS stripped, so a
    prefix rewrite is enough to retarget it at the attached archive.
    """
    rows = conn.execute(
        "SELECT type, tbl_name, sql FROM main.sqlite_master "
        "WHERE tbl_name IN (?, ?) AND sql IS NOT NULL "
        "ORDER BY type = 'index'",
        _TABLES,
    ).fetchall()
    for kind, table, sql in rows:
        if kind == "table":
            if not _columns(conn, "arc", table):
                conn.execute(re.sub(r"^CREATE TABLE ", "CREATE TABLE arc.", sql))
            # An archive written before a live migration lacks the new columns.
            have = set(_columns(conn, "arc", table))
            for row in conn.execute(f"PRAGMA main.table_info({table})"):
                if row[1] not in have:
                    conn.execute(f"ALTER TABLE arc.{table} ADD COLUMN {row[1]} {row[2]}")
        else:
            conn.execute(re.sub(r"^CREATE (UNIQUE )?INDEX ",
                                r"CREATE \1INDEX IF NOT EXISTS arc.", sql))


def archive_posts(db_path, archive_dir, hub: str, days: int = 30) -> dict[Path, int]:
    archive_dir = Path(archive_dir)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(
            "SELECT post_id, substr(created_at, 1, 7) FROM posts "
            "WHERE hub_id = ? AND created_at < ? "
            "AND NOT (kind = 'claim' AND status = 'open')",
            (hub, cutoff),
        ).fetchall()
        by_month: dict[str, list[str]] = {}
        for post_id, month in rows:
            by_month.setdefault(month, []).append(post_id)

        archived: dict[Path, int] = {}
        for month, ids in sorted(by_month.items()):
            archive_dir.mkdir(parents=True, exist_ok=True)
            path = archive_dir / f"{hub}-{month}.db"
            conn.execute("ATTACH DATABASE ? AS arc", (str(path),))
            try:
                conn.execute("DROP TABLE IF EXISTS temp.gc_ids")
                conn.execute("CREATE TEMP TABLE gc_ids (post_id TEXT PRIMARY KEY)")
                conn.executemany("INSERT INTO temp.gc_ids VALUES (?)", [(i,) for i in ids])

                conn.execute("BEGIN")
                _ensure_archive_schema(conn)
                for table in _TABLES:
                    cols = ", ".join(_columns(conn, "main", table))
                    conn.execute(
                        f"INSERT OR IGNORE INTO arc.{table} ({cols}) "
                        f"SELECT {cols} FROM main.{table} "
                        f"WHERE post_id IN (SELECT post_id FROM temp.gc_ids)"
                    )
                conn.execute("COMMIT")

                (copied,) = conn.execute(
                    "SELECT COUNT(*) FROM arc.posts "
                    "WHERE post_id IN (SELECT post_id FROM temp.gc_ids)"
                ).fetchone()
                if copied != len(ids):
                    raise RuntimeError(
                        f"{path}: archived {copied} of {len(ids)} posts; live db left untouched"
                    )

                conn.execute("BEGIN")
                for table in ("replies", "posts"):
                    conn.execute(
                        f"DELETE FROM main.{table} "
                        f"WHERE post_id IN (SELECT post_id FROM temp.gc_ids)"
                    )
                conn.execute("COMMIT")
                archived[path] = copied
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.execute("DROP TABLE IF EXISTS temp.gc_ids")
                conn.execute("DETACH DATABASE arc")
        return archived
    finally:
        conn.close()


def trim_scores(db_path, hub: str, days: int = 7) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        with conn:
            cur = conn.execute(
                "DELETE FROM scores WHERE hub_id = ? AND computed_at < ?", (hub, cutoff)
            )
        return cur.rowcount
    finally:
        conn.close()


def drop_idle_members(db_path, hub: str, days: int = 30) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    where = "hub_id = ? AND (last_seen IS NULL OR last_seen < ?)"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        with conn:
            ids = [r[0] for r in conn.execute(f"SELECT member_id FROM members WHERE {where}", (hub, cutoff))]
            conn.execute(f"DELETE FROM members WHERE {where}", (hub, cutoff))
        return ids
    finally:
        conn.close()


_CHUNK = 1 << 20


def rotate_logs(home, max_mb: float = 10) -> list[Path]:
    home = Path(home)
    cap = int(max_mb * 1024 * 1024)
    rotated = []
    for log in sorted(home.glob("*.log")):
        if not log.is_file() or log.is_symlink() or log.stat().st_size <= cap:
            continue
        backup = log.with_name(log.name + ".1")
        tmp = log.with_name(log.name + ".1.tmp")
        with open(log, "r+b") as src:
            size = os.fstat(src.fileno()).st_size
            src.seek(max(0, size - cap))
            with open(tmp, "wb") as dst:
                while chunk := src.read(_CHUNK):
                    dst.write(chunk)
            src.truncate(0)
        os.replace(tmp, backup)
        rotated.append(log)
    return rotated


def keep_backups(home, keep: int = 3) -> list[Path]:
    home = Path(home)
    backups = [p for p in home.glob("atris.db.backup*") if p.is_file() and not p.is_symlink()]
    backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = backups[max(0, keep):]
    for p in removed:
        p.unlink()
    return removed


LOG_NAME = "prune.jsonl"


def home_mb(home) -> float:
    total = 0
    for root, _dirs, files in os.walk(home):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return round(total / (1024 * 1024), 2)


def record_run(home, trigger: str, ok: bool, mb_before: float | None = None, **counts) -> dict:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    mb_after = home_mb(home)
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trigger": trigger,
        "ok": bool(ok),
        **counts,
        "mb_before": mb_after if mb_before is None else mb_before,
        "mb_after": mb_after,
    }
    with open(home / LOG_NAME, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def last_scheduled_at(home) -> datetime | None:
    last = None
    try:
        with open(Path(home) / LOG_NAME) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get("trigger") != "schedule":
                        continue
                    at = datetime.fromisoformat(r["at"])
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                if last is None or at > last:
                    last = at
    except FileNotFoundError:
        pass
    return last


async def _run_once(run_fn, home) -> dict:
    mb_before = home_mb(home)
    try:
        result = run_fn()
        if inspect.isawaitable(result):
            result = await result
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return record_run(home, "schedule", False, mb_before=mb_before, error=f"{type(e).__name__}: {e}")
    counts = result if isinstance(result, dict) else {}
    counts = {k: v for k, v in counts.items() if k not in ("at", "trigger", "ok", "mb_before", "mb_after")}
    return record_run(home, "schedule", True, mb_before=mb_before, **counts)


async def _loop(run_fn, home, interval: float) -> None:
    last = last_scheduled_at(home)
    wait = 0.0
    if last is not None:
        since = (datetime.now(timezone.utc) - last).total_seconds()
        wait = min(interval, max(0.0, interval - since))
    while True:
        if wait:
            await asyncio.sleep(wait)
        await _run_once(run_fn, home)
        wait = interval


def start_schedule(run_fn, home, interval_hours: float = 24) -> asyncio.Task:
    return asyncio.ensure_future(_loop(run_fn, Path(home), interval_hours * 3600))
