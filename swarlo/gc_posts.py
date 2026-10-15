"""Move old board posts out of a live swarlo database into monthly archives.

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
"""

from __future__ import annotations

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
