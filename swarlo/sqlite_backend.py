"""SQLite-backed Swarlo implementation. Zero external dependencies beyond stdlib + aiosqlite."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .backend import SwarloBackend
from .types import Member, Post, Reply, ClaimResult

DEFAULT_CHANNELS = ["general", "experiments", "outreach", "ops", "policies", "escalations"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    member_id TEXT NOT NULL,
    hub_id TEXT NOT NULL,
    member_type TEXT NOT NULL,
    member_name TEXT NOT NULL,
    api_key TEXT,
    webhook_url TEXT,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    PRIMARY KEY (member_id, hub_id)
);

CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY,
    hub_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    member_id TEXT NOT NULL,
    member_name TEXT NOT NULL,
    member_type TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'message',
    task_key TEXT,
    status TEXT,
    priority INTEGER DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    metadata TEXT,
    mentions TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replies (
    reply_id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES posts(post_id),
    hub_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    member_name TEXT NOT NULL,
    member_type TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
    hash TEXT PRIMARY KEY,
    parent_hash TEXT,
    hub_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    member_name TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hub_id TEXT NOT NULL,
    agents_active INTEGER,
    tasks_claimed INTEGER,
    tasks_shipped INTEGER,
    avg_time_to_claim REAL,
    file_conflicts INTEGER DEFAULT 0,
    files_with_multi_editors INTEGER DEFAULT 0,
    coord_score INTEGER DEFAULT 0,
    computed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_hub_channel ON posts(hub_id, channel);
CREATE INDEX IF NOT EXISTS idx_posts_hub_task_key ON posts(hub_id, task_key);
CREATE INDEX IF NOT EXISTS idx_posts_kind_status ON posts(kind, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_open_claim ON posts(hub_id, task_key) WHERE kind = 'claim' AND status = 'open';
CREATE INDEX IF NOT EXISTS idx_replies_post ON replies(post_id);
CREATE INDEX IF NOT EXISTS idx_commits_hub ON commits(hub_id);
CREATE INDEX IF NOT EXISTS idx_commits_parent ON commits(parent_hash);
CREATE INDEX IF NOT EXISTS idx_commits_member ON commits(member_id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


class SQLiteBackend(SwarloBackend):
    """Swarlo backed by a local SQLite database."""

    def __init__(self, db_path: str = "swarlo.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            # Migrations for existing DBs
            try:
                self._conn.execute("ALTER TABLE posts ADD COLUMN priority INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                self._conn.execute("ALTER TABLE posts ADD COLUMN retry_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                # First-class assignee column. Set on claim (= claimer) and
                # assign (= target). Lets agents query "what's mine?" without
                # grepping channels.
                self._conn.execute("ALTER TABLE posts ADD COLUMN assignee_id TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                # Distinguish "alive" (last_seen, bumped on any auth touch)
                # from "working" (last_active, bumped only when producing
                # a post/claim/report). Fixes the idle/liveness lying problem.
                self._conn.execute("ALTER TABLE members ADD COLUMN last_active TEXT")
            except sqlite3.OperationalError:
                pass
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._get_conn()

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass  # cross-thread close in test teardown
            self._conn = None

    # ── Members ─────────────────────────────────────────────

    def register_member(self, member: Member, api_key: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO members (member_id, hub_id, member_type, member_name, api_key, webhook_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (member.member_id, member.hub_id, member.member_type, member.member_name, api_key, member.webhook_url, _utcnow()),
            )
            self.conn.commit()

    def get_member(self, hub_id: str, member_id: str) -> Member | None:
        row = self.conn.execute(
            "SELECT * FROM members WHERE member_id = ? AND hub_id = ?",
            (member_id, hub_id),
        ).fetchone()
        if not row:
            return None
        return self._row_to_member(row)

    def authenticate(self, api_key: str) -> Member | None:
        row = self.conn.execute(
            "SELECT * FROM members WHERE api_key = ?", (api_key,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_member(row)

    def resolve_mentions(self, hub_id: str, names: list[str]) -> list[str]:
        """Resolve @mention names to member_ids. Case-insensitive match on member_name or member_id."""
        if not names:
            return []
        resolved = []
        for name in names:
            row = self.conn.execute(
                "SELECT member_id FROM members WHERE hub_id = ? AND (LOWER(member_name) = LOWER(?) OR LOWER(member_id) = LOWER(?))",
                (hub_id, name, name),
            ).fetchone()
            if row:
                resolved.append(row["member_id"])
        return resolved

    def get_members_by_ids(self, hub_id: str, member_ids: list[str]) -> list[Member]:
        """Get members by IDs. Used for webhook dispatch."""
        if not member_ids:
            return []
        placeholders = ",".join("?" for _ in member_ids)
        rows = self.conn.execute(
            f"SELECT * FROM members WHERE hub_id = ? AND member_id IN ({placeholders})",
            [hub_id, *member_ids],
        ).fetchall()
        return [self._row_to_member(r) for r in rows]

    def _row_to_member(self, row) -> Member:
        return Member(
            member_id=row["member_id"],
            member_type=row["member_type"],
            member_name=row["member_name"],
            hub_id=row["hub_id"],
            webhook_url=row["webhook_url"] if "webhook_url" in row.keys() else None,
        )

    # ── SwarloBackend ───────────────────────────────────────

    async def list_channels(self, hub_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT channel FROM posts WHERE hub_id = ?", (hub_id,)
        ).fetchall()
        active = {row["channel"] for row in rows}
        return sorted(set(DEFAULT_CHANNELS) | active)

    async def read_channel(self, hub_id: str, channel: str, limit: int = 10,
                           include_replies: bool = True) -> list[Post]:
        """Read recent posts on a channel.

        When include_replies is True (default), eagerly batch-loads replies
        for the returned posts in a single extra query so threads don't die
        on arrival. Replies are attached to each Post via the .replies field
        as a list of dicts (chronological order).
        """
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE hub_id = ? AND channel = ? ORDER BY created_at DESC LIMIT ?",
            (hub_id, channel, limit),
        ).fetchall()
        posts = [self._row_to_post(r) for r in rows]
        if include_replies and posts:
            self._attach_replies(hub_id, posts)
        return posts

    def _attach_replies(self, hub_id: str, posts: list[Post]) -> None:
        """Batch-fetch replies for a list of posts and attach via Post.replies.

        Single SQL query with IN clause — O(1) extra round trip regardless
        of how many posts are in the list. Replies are grouped by post_id
        and sorted chronologically (oldest first, like a normal thread).
        """
        if not posts:
            return
        post_ids = [p.post_id for p in posts]
        placeholders = ",".join("?" * len(post_ids))
        rows = self.conn.execute(
            f"SELECT * FROM replies WHERE hub_id = ? AND post_id IN ({placeholders}) "
            f"ORDER BY post_id, created_at ASC",
            (hub_id, *post_ids),
        ).fetchall()

        from collections import defaultdict
        grouped: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            grouped[r["post_id"]].append({
                "reply_id": r["reply_id"],
                "post_id": r["post_id"],
                "content": r["content"],
                "member_id": r["member_id"],
                "member_name": r["member_name"],
                "member_type": r["member_type"],
                "created_at": r["created_at"],
            })
        for p in posts:
            replies = grouped.get(p.post_id)
            if replies:
                p.replies = replies

    async def create_post(self, hub_id: str, member: Member, channel: str,
                          content: str, kind: str = "message",
                          task_key: str | None = None, status: str | None = None,
                          metadata: dict | None = None, priority: int = 0,
                          assignee_id: str | None = None) -> Post:
        from .types import extract_mentions
        post_id = _uid()
        now = _utcnow()

        # Extract and resolve @mentions
        mention_names = extract_mentions(content)
        mention_ids = self.resolve_mentions(hub_id, mention_names) if mention_names else []

        metadata_json = json.dumps(metadata) if metadata is not None else None
        mentions_json = json.dumps(mention_ids) if mention_ids else None

        with self._lock:
            self.conn.execute(
                "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, content, kind, task_key, status, priority, metadata, mentions, created_at, assignee_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, hub_id, channel, member.member_id, member.member_name, member.member_type, content, kind, task_key, status, priority, metadata_json, mentions_json, now, assignee_id),
            )
            # Bump last_active — this is the "actually working" signal,
            # distinct from last_seen which fires on any auth touch.
            self.conn.execute(
                "UPDATE members SET last_active = ? WHERE hub_id = ? AND member_id = ?",
                (now, hub_id, member.member_id),
            )
            self.conn.commit()
        return Post(
            post_id=post_id, content=content, kind=kind, channel=channel,
            member_id=member.member_id, member_name=member.member_name,
            member_type=member.member_type, task_key=task_key, status=status,
            metadata=metadata, mentions=mention_ids or None, created_at=now,
        )

    async def retry_failed(self, hub_id: str, max_retries: int = 3) -> list[str]:
        """Re-queue failed tasks that haven't exceeded max_retries. Returns retried task_keys."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT post_id, task_key, channel, member_id, member_name, member_type, content, priority, retry_count "
                "FROM posts WHERE hub_id = ? AND kind IN ('failed', 'result') AND status = 'failed' "
                "AND retry_count < ? ORDER BY priority DESC, created_at ASC",
                (hub_id, max_retries),
            ).fetchall()

        # Check claims outside lock (involves await)
        retried = []
        update_ids = []
        for r in rows:
            task_key = r["task_key"]
            if not task_key:
                continue
            existing = await self.get_open_claims(hub_id, task_key=task_key)
            if existing:
                continue
            update_ids.append(r["post_id"])
            retried.append(task_key)

        if update_ids:
            with self._lock:
                for pid in update_ids:
                    self.conn.execute(
                        "UPDATE posts SET retry_count = retry_count + 1 WHERE post_id = ?",
                        (pid,),
                    )
                self.conn.commit()
        return retried

    async def reply(self, hub_id: str, member: Member, post_id: str, content: str) -> Reply:
        reply_id = _uid()
        now = _utcnow()
        with self._lock:
            self.conn.execute(
                "INSERT INTO replies (reply_id, post_id, hub_id, member_id, member_name, member_type, content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (reply_id, post_id, hub_id, member.member_id, member.member_name, member.member_type, content, now),
            )
            self.conn.commit()
        return Reply(
            reply_id=reply_id, post_id=post_id, content=content,
            member_id=member.member_id, member_name=member.member_name,
            member_type=member.member_type, created_at=now,
        )

    async def assign(self, hub_id: str, assigner: Member, channel: str,
                     task_key: str, assignee_id: str, content: str) -> ClaimResult:
        """Push-assign a task to a specific member. Claims first, then posts assignment message."""
        # Look up assignee
        assignee = self.get_member(hub_id, assignee_id)
        if not assignee:
            return ClaimResult(
                claimed=False, conflict=False,
                message=f"Member {assignee_id} not found in hub",
            )

        # Claim first — no orphan assign posts on conflict
        result = await self.claim(hub_id, assignee, channel, task_key, content)
        if not result.claimed:
            return result

        # Claim succeeded — post the visible assignment message.
        # assignee_id is passed as a first-class column so /ping can find it
        # without json_extract gymnastics.
        assign_post = await self.create_post(
            hub_id, assigner, channel, f"Assigned {task_key} to @{assignee.member_name}: {content}",
            kind="assign", task_key=task_key,
            metadata={"assignee_id": assignee_id, "claim_post_id": result.post_id},
            assignee_id=assignee_id,
        )

        return result

    async def claim(self, hub_id: str, member: Member, channel: str,
                    task_key: str, content: str) -> ClaimResult:
        # Auto-expire stale claims first
        await self.force_expire_claims(hub_id, stale_minutes=30)

        # Atomic claim via unique index — no TOCTOU race condition
        from .types import extract_mentions
        post_id = _uid()
        now = _utcnow()
        mention_names = extract_mentions(content)
        mention_ids = self.resolve_mentions(hub_id, mention_names) if mention_names else []
        metadata_json = json.dumps({"heartbeat_at": now})
        mentions_json = json.dumps(mention_ids) if mention_ids else None

        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, "
                    "content, kind, task_key, status, metadata, mentions, created_at, assignee_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'claim', ?, 'open', ?, ?, ?, ?)",
                    (post_id, hub_id, channel, member.member_id, member.member_name,
                     member.member_type, content, task_key, metadata_json, mentions_json, now,
                     member.member_id),  # claimer is the assignee
                )
                # Bump last_active — claimer is producing work
                self.conn.execute(
                    "UPDATE members SET last_active = ? WHERE hub_id = ? AND member_id = ?",
                    (now, hub_id, member.member_id),
                )
                self.conn.commit()
            return ClaimResult(
                claimed=True, conflict=False,
                post_id=post_id, channel=channel, kind="claim",
            )
        except sqlite3.IntegrityError:
            # Unique index violation — someone else claimed first
            existing = await self.get_open_claims(hub_id, task_key=task_key)
            return ClaimResult(
                claimed=False, conflict=True,
                existing_claim=existing[0] if existing else None,
                message=f"Already claimed by {existing[0].member_name}" if existing else "Claim conflict",
            )

    async def report(self, hub_id: str, member: Member, channel: str,
                     task_key: str, status: str, content: str,
                     parent_id: str | None = None,
                     affected_files: list[str] | None = None,
                     metadata: dict | None = None) -> Post:
        existing = await self.get_open_claims(hub_id, task_key=task_key)
        if existing and existing[0].member_id != member.member_id:
            raise PermissionError(
                f"Task {task_key} is claimed by {existing[0].member_name}"
            )

        kind = "result" if status == "done" else "failed" if status == "failed" else status
        # Merge affected_files into metadata for downstream consumption
        report_meta = dict(metadata or {})
        if affected_files:
            # Sanitize: keep only string entries, cap to 100 files
            clean = [str(f) for f in affected_files if f][:100]
            if clean:
                report_meta["affected_files"] = clean
        post = await self.create_post(hub_id, member, channel, content,
                                      kind=kind, task_key=task_key, status=status,
                                      metadata=report_meta or None)
        with self._lock:
            self.conn.execute(
                "UPDATE posts SET status = ? WHERE hub_id = ? AND task_key = ? AND kind = 'claim' AND status = 'open' AND member_id = ?",
                (status, hub_id, task_key, member.member_id),
            )
            self.conn.commit()

        if parent_id:
            await self.reply(hub_id, member, parent_id, content)

        return post

    async def touch_claim(self, hub_id: str, member_id: str, task_key: str) -> bool:
        """Refresh a claim's heartbeat to prevent stale expiry."""
        now = _utcnow()
        meta_patch = json.dumps({"heartbeat_at": now})
        with self._lock:
            cur = self.conn.execute(
                "UPDATE posts SET metadata = json_patch(COALESCE(metadata, '{}'), ?) "
                "WHERE hub_id = ? AND task_key = ? AND kind = 'claim' AND status = 'open' AND member_id = ?",
                (meta_patch, hub_id, task_key, member_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    async def force_expire_claims(self, hub_id: str, stale_minutes: int = 30) -> list[str]:
        """Expire all claims older than stale_minutes with no heartbeat. Returns expired task_keys."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).isoformat()
        with self._lock:
            rows = self.conn.execute(
                "SELECT post_id, task_key FROM posts WHERE hub_id = ? AND kind = 'claim' AND status = 'open' "
                "AND (COALESCE(json_extract(metadata, '$.heartbeat_at'), created_at) < ?)",
                (hub_id, cutoff),
            ).fetchall()
            expired_keys = [r["task_key"] for r in rows]
            if rows:
                post_ids = [r["post_id"] for r in rows]
                placeholders = ",".join("?" for _ in post_ids)
                self.conn.execute(
                    f"UPDATE posts SET status = 'stale' WHERE post_id IN ({placeholders})",
                    post_ids,
                )
                self.conn.commit()
        return expired_keys

    async def get_my_open_tasks(self, hub_id: str, member_id: str) -> list[Post]:
        """Return open work assigned to a member.

        Includes:
        - Their own open claims (assignee_id = member_id, kind = claim, status = open)
        - Open assignments addressed to them (assignee_id = member_id, kind = assign)

        Replaces the "grep channels for your name" antipattern. This is the
        single source of truth for "what's mine?".
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM posts WHERE hub_id = ? AND assignee_id = ? "
                "AND ((kind = 'claim' AND status = 'open') OR kind = 'assign') "
                "ORDER BY created_at DESC",
                (hub_id, member_id),
            ).fetchall()
        return [self._row_to_post(r) for r in rows]

    async def get_open_claims(self, hub_id: str, channel: str | None = None,
                              task_key: str | None = None) -> list[Post]:
        # Auto-expire stale claims (30 min without heartbeat)
        await self.force_expire_claims(hub_id, stale_minutes=30)

        query = "SELECT * FROM posts WHERE hub_id = ? AND kind = 'claim' AND status = 'open'"
        params: list = [hub_id]
        if channel:
            query += " AND channel = ?"
            params.append(channel)
        if task_key:
            query += " AND task_key = ?"
            params.append(task_key)
        query += " ORDER BY created_at DESC"

        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_post(r) for r in rows]

    async def get_replies(self, hub_id: str, post_id: str) -> list[Reply]:
        rows = self.conn.execute(
            "SELECT r.* FROM replies r JOIN posts p ON r.post_id = p.post_id "
            "WHERE r.post_id = ? AND p.hub_id = ? ORDER BY r.created_at ASC",
            (post_id, hub_id),
        ).fetchall()
        return [
            Reply(
                reply_id=r["reply_id"], post_id=r["post_id"], content=r["content"],
                member_id=r["member_id"], member_name=r["member_name"],
                member_type=r["member_type"], created_at=r["created_at"],
            )
            for r in rows
        ]

    async def summarize_for_member(self, hub_id: str, member_id: str, limit: int = 10) -> str:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE hub_id = ? ORDER BY created_at DESC LIMIT ?",
            (hub_id, limit * 3),
        ).fetchall()

        lines = []
        open_claims = []
        count = 0
        for r in rows:
            kind = r["kind"]
            name = r["member_name"]
            ch = r["channel"]
            content = r["content"][:150].replace("\n", " ")

            if kind == "claim" and r["status"] == "open":
                open_claims.append(f"  - {name}: {content}")
                continue

            kind_tag = kind.upper() if kind in ("claim", "result", "failed", "escalation") else ""
            lines.append(f"  #{ch} {name}: {kind_tag + ' ' if kind_tag else ''}{content}")
            count += 1
            if count >= limit:
                break

        if not lines and not open_claims:
            return ""

        parts = ["\nFLEET BOARD (Swarlo):"]
        parts.extend(lines)
        if open_claims:
            parts.append("\nOPEN CLAIMS (do not duplicate):")
            parts.extend(open_claims)

        return "\n".join(parts)

    # ── DAG ─────────────────────────────────────────────────

    def index_commit(self, hub_id: str, hash: str, parent_hash: str,
                     member_id: str, member_name: str, message: str) -> None:
        """Index a commit in the metadata store."""
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO commits (hash, parent_hash, hub_id, member_id, member_name, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (hash, parent_hash or None, hub_id, member_id, member_name, message, _utcnow()),
            )
            self.conn.commit()

    def get_commit(self, hub_id: str, hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM commits WHERE hash = ? AND hub_id = ?", (hash, hub_id)
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def list_commits(self, hub_id: str, member_id: str | None = None, limit: int = 50) -> list[dict]:
        if member_id:
            rows = self.conn.execute(
                "SELECT * FROM commits WHERE hub_id = ? AND member_id = ? ORDER BY created_at DESC LIMIT ?",
                (hub_id, member_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM commits WHERE hub_id = ? ORDER BY created_at DESC LIMIT ?",
                (hub_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_children(self, hub_id: str, hash: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM commits WHERE hub_id = ? AND parent_hash = ? ORDER BY created_at DESC",
            (hub_id, hash),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_leaves(self, hub_id: str) -> list[dict]:
        rows = self.conn.execute("""
            SELECT c.* FROM commits c
            LEFT JOIN commits child ON child.parent_hash = c.hash AND child.hub_id = c.hub_id
            WHERE c.hub_id = ? AND child.hash IS NULL
            ORDER BY c.created_at DESC
        """, (hub_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_lineage(self, hub_id: str, hash: str) -> list[dict]:
        lineage = []
        current = hash
        while current:
            row = self.conn.execute(
                "SELECT * FROM commits WHERE hash = ? AND hub_id = ?", (current, hub_id)
            ).fetchone()
            if not row:
                break
            lineage.append(dict(row))
            current = row["parent_hash"]
        return lineage

    # ── Helpers ─────────────────────────────────────────────

    def _row_to_post(self, row: sqlite3.Row) -> Post:
        metadata_raw = row["metadata"] if "metadata" in row.keys() else None
        mentions_raw = row["mentions"] if "mentions" in row.keys() else None
        keys = row.keys()
        return Post(
            post_id=row["post_id"],
            content=row["content"],
            kind=row["kind"],
            channel=row["channel"],
            member_id=row["member_id"],
            member_name=row["member_name"],
            member_type=row["member_type"],
            task_key=row["task_key"],
            status=row["status"],
            priority=row["priority"] if "priority" in keys else 0,
            metadata=json.loads(metadata_raw) if metadata_raw else None,
            mentions=json.loads(mentions_raw) if mentions_raw else None,
            created_at=row["created_at"],
        )
