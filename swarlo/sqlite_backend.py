"""SQLite-backed Swarlo implementation. Zero external dependencies beyond stdlib + aiosqlite."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
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
    created_at TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_posts_hub_channel ON posts(hub_id, channel);
CREATE INDEX IF NOT EXISTS idx_posts_hub_task_key ON posts(hub_id, task_key);
CREATE INDEX IF NOT EXISTS idx_posts_kind_status ON posts(kind, status);
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

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._get_conn()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Members ─────────────────────────────────────────────

    def register_member(self, member: Member, api_key: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO members (member_id, hub_id, member_type, member_name, api_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (member.member_id, member.hub_id, member.member_type, member.member_name, api_key, _utcnow()),
        )
        self.conn.commit()

    def get_member(self, hub_id: str, member_id: str) -> Member | None:
        row = self.conn.execute(
            "SELECT * FROM members WHERE member_id = ? AND hub_id = ?",
            (member_id, hub_id),
        ).fetchone()
        if not row:
            return None
        return Member(
            member_id=row["member_id"],
            member_type=row["member_type"],
            member_name=row["member_name"],
            hub_id=row["hub_id"],
        )

    def authenticate(self, api_key: str) -> Member | None:
        row = self.conn.execute(
            "SELECT * FROM members WHERE api_key = ?", (api_key,)
        ).fetchone()
        if not row:
            return None
        return Member(
            member_id=row["member_id"],
            member_type=row["member_type"],
            member_name=row["member_name"],
            hub_id=row["hub_id"],
        )

    # ── SwarloBackend ───────────────────────────────────────

    async def list_channels(self, hub_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT channel FROM posts WHERE hub_id = ?", (hub_id,)
        ).fetchall()
        active = {row["channel"] for row in rows}
        return sorted(set(DEFAULT_CHANNELS) | active)

    async def read_channel(self, hub_id: str, channel: str, limit: int = 10) -> list[Post]:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE hub_id = ? AND channel = ? ORDER BY created_at DESC LIMIT ?",
            (hub_id, channel, limit),
        ).fetchall()
        return [self._row_to_post(r) for r in rows]

    async def create_post(self, hub_id: str, member: Member, channel: str,
                          content: str, kind: str = "message",
                          task_key: str | None = None, status: str | None = None) -> Post:
        post_id = _uid()
        now = _utcnow()
        self.conn.execute(
            "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, content, kind, task_key, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (post_id, hub_id, channel, member.member_id, member.member_name, member.member_type, content, kind, task_key, status, now),
        )
        self.conn.commit()
        return Post(
            post_id=post_id, content=content, kind=kind, channel=channel,
            member_id=member.member_id, member_name=member.member_name,
            member_type=member.member_type, task_key=task_key, status=status, created_at=now,
        )

    async def reply(self, hub_id: str, member: Member, post_id: str, content: str) -> Reply:
        reply_id = _uid()
        now = _utcnow()
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

    async def claim(self, hub_id: str, member: Member, channel: str,
                    task_key: str, content: str) -> ClaimResult:
        existing = await self.get_open_claims(hub_id, channel=channel, task_key=task_key)
        if existing:
            return ClaimResult(
                claimed=False, conflict=True,
                existing_claim=existing[0],
                message=f"Already claimed by {existing[0].member_name}",
            )
        post = await self.create_post(hub_id, member, channel, content,
                                      kind="claim", task_key=task_key, status="open")
        return ClaimResult(
            claimed=True, conflict=False,
            post_id=post.post_id, channel=channel, kind="claim",
        )

    async def report(self, hub_id: str, member: Member, channel: str,
                     task_key: str, status: str, content: str,
                     parent_id: str | None = None) -> Post:
        kind = "result" if status == "done" else "failed"
        post = await self.create_post(hub_id, member, channel, content,
                                      kind=kind, task_key=task_key, status=status)
        # Close matching open claims
        self.conn.execute(
            "UPDATE posts SET status = ? WHERE hub_id = ? AND task_key = ? AND kind = 'claim' AND status = 'open'",
            (status, hub_id, task_key),
        )
        self.conn.commit()

        if parent_id:
            await self.reply(hub_id, member, parent_id, content)

        return post

    async def get_open_claims(self, hub_id: str, channel: str | None = None,
                              task_key: str | None = None) -> list[Post]:
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
            created_at=row["created_at"],
        )
