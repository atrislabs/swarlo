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
            try:
                # Dependency edge for the coordination graph. JSON-encoded
                # list of task_keys this task waits on. Enables recursive
                # CTE reachability queries — "what can I claim right now?"
                # — in O(log D) iterations where D is the dep-chain depth.
                self._conn.execute("ALTER TABLE posts ADD COLUMN depends_on TEXT")
            except sqlite3.OperationalError:
                pass
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._get_conn()

    def close(self):
        """Close the database connection and release resources."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass  # cross-thread close in test teardown
            self._conn = None

    # ── Members ─────────────────────────────────────────────

    def register_member(self, member: Member, api_key: str | None = None) -> None:
        """Register or update a member in the hub.

        Args:
            member: Member object with id, hub_id, type, name, webhook_url
            api_key: Optional API key for authentication
        """
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO members (member_id, hub_id, member_type, member_name, api_key, webhook_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (member.member_id, member.hub_id, member.member_type, member.member_name, api_key, member.webhook_url, _utcnow()),
            )
            self.conn.commit()

    def get_member(self, hub_id: str, member_id: str) -> Member | None:
        """Retrieve a member by hub and member ID.

        Returns:
            Member object if found, None otherwise
        """
        row = self.conn.execute(
            "SELECT * FROM members WHERE member_id = ? AND hub_id = ?",
            (member_id, hub_id),
        ).fetchone()
        if not row:
            return None
        return self._row_to_member(row)

    def authenticate(self, api_key: str) -> Member | None:
        """Authenticate a member by API key. Returns Member if valid, None otherwise."""
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
                          assignee_id: str | None = None,
                          depends_on: list[str] | None = None) -> Post:
        from .types import extract_mentions
        post_id = _uid()
        now = _utcnow()

        # Extract and resolve @mentions
        mention_names = extract_mentions(content)
        mention_ids = self.resolve_mentions(hub_id, mention_names) if mention_names else []

        metadata_json = json.dumps(metadata) if metadata is not None else None
        mentions_json = json.dumps(mention_ids) if mention_ids else None
        # Dependencies stored as JSON array of task_keys. None = no deps.
        depends_on_json = json.dumps(depends_on) if depends_on else None

        with self._lock:
            self.conn.execute(
                "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, content, kind, task_key, status, priority, metadata, mentions, created_at, assignee_id, depends_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, hub_id, channel, member.member_id, member.member_name, member.member_type, content, kind, task_key, status, priority, metadata_json, mentions_json, now, assignee_id, depends_on_json),
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
                     task_key: str, assignee_id: str, content: str,
                     depends_on: list[str] | None = None,
                     priority: int = 0) -> ClaimResult:
        """Push-assign a task to a specific member. Claims first, then posts assignment message.

        depends_on is recorded on both the implicit claim AND the visible
        assign post so /ready can filter correctly regardless of which
        row it walks.
        """
        # Look up assignee
        assignee = self.get_member(hub_id, assignee_id)
        if not assignee:
            return ClaimResult(
                claimed=False, conflict=False,
                message=f"Member {assignee_id} not found in hub",
            )

        # NOTE: we don't check dep readiness on assign — assignments are
        # push notifications that work can eventually be done. Whether
        # the assignee should *claim* right now is handled by /ready.
        # This lets the orchestrator assign the whole dependency graph
        # up front without having to wait for each level to finish first.
        # So we DON'T pass depends_on to the inner claim (which would
        # block on unmet deps); deps go only on the visible assign post.
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
            depends_on=depends_on,
            priority=priority,
        )

        return result

    async def claim(self, hub_id: str, member: Member, channel: str,
                    task_key: str, content: str,
                    depends_on: list[str] | None = None) -> ClaimResult:
        # Auto-expire stale claims first
        await self.force_expire_claims(hub_id, stale_minutes=30)

        # Cycle detection — reject a claim whose declared deps would
        # transitively depend back on this task_key. Without this, a
        # cycle like task:A depends on task:B and task:B depends on
        # task:A silently causes /ready to return nothing forever and
        # nobody knows why. Catch it at declaration time.
        if depends_on:
            cycle_path = self._find_cycle(hub_id, task_key, depends_on)
            if cycle_path:
                return ClaimResult(
                    claimed=False, conflict=True,
                    message=f"Dependency cycle detected: {' → '.join(cycle_path)}",
                )

        # Check that every declared dependency is already done. A dep is
        # satisfied when there's a post with that task_key whose status is
        # 'done'. Missing deps (no post at all) block — this prevents
        # claiming work that depends on tasks that haven't even been
        # posted yet, which is a silent-failure pattern I want to catch.
        if depends_on:
            blocked = await self._unmet_deps(hub_id, depends_on)
            if blocked:
                # Enrich the error with the current state of each blocked
                # dep so the user knows whether to wait, reassign, or
                # escalate. Without this, "Blocked by unmet deps: task:B"
                # gives no actionable signal.
                explained = self._explain_blocked_deps(hub_id, blocked)
                return ClaimResult(
                    claimed=False, conflict=True,
                    message=f"Blocked by unmet dependencies: {explained}",
                )

        # Atomic claim via unique index — no TOCTOU race condition
        from .types import extract_mentions
        post_id = _uid()
        now = _utcnow()
        mention_names = extract_mentions(content)
        mention_ids = self.resolve_mentions(hub_id, mention_names) if mention_names else []
        metadata_json = json.dumps({"heartbeat_at": now})
        mentions_json = json.dumps(mention_ids) if mention_ids else None
        depends_on_json = json.dumps(depends_on) if depends_on else None

        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, "
                    "content, kind, task_key, status, metadata, mentions, created_at, assignee_id, depends_on) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'claim', ?, 'open', ?, ?, ?, ?, ?)",
                    (post_id, hub_id, channel, member.member_id, member.member_name,
                     member.member_type, content, task_key, metadata_json, mentions_json, now,
                     member.member_id, depends_on_json),  # claimer is the assignee
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

    async def _unmet_deps(self, hub_id: str, deps: list[str]) -> list[str]:
        """Return the subset of deps whose task_keys have no done post yet.

        A dep is "met" iff some post with that task_key has status='done'.
        Anything else (no post, open claim, failed, etc.) counts as unmet.
        Single query — no per-dep round trip.
        """
        if not deps:
            return []
        placeholders = ",".join("?" * len(deps))
        rows = self.conn.execute(
            f"SELECT DISTINCT task_key FROM posts "
            f"WHERE hub_id = ? AND task_key IN ({placeholders}) AND status = 'done'",
            (hub_id, *deps),
        ).fetchall()
        done_set = {r["task_key"] for r in rows}
        return [d for d in deps if d not in done_set]

    def _explain_blocked_deps(self, hub_id: str, blocked: list[str]) -> str:
        """Render a human-readable explanation of why blocked deps aren't met.

        For each blocked dep, find the most-recent post with that task_key
        and annotate the dep with what state it's in:
          - task:X (not yet posted)
          - task:X (claimed by @alice, in progress)
          - task:X (failed)
          - task:X (stale claim)

        This is the error-path-only read path — one batch query for the
        whole blocked set, no per-dep round trip.
        """
        if not blocked:
            return ""
        placeholders = ",".join("?" * len(blocked))
        # For each blocked task_key, find its most recent post by kind/status.
        # A task_key may have multiple posts (assign, claim, result) — we want
        # the one that tells us the current state.
        rows = self.conn.execute(
            f"SELECT task_key, kind, status, member_name "
            f"FROM posts "
            f"WHERE hub_id = ? AND task_key IN ({placeholders}) "
            f"ORDER BY created_at DESC",
            (hub_id, *blocked),
        ).fetchall()

        # Walk rows in most-recent-first order and take the first row per task_key
        latest: dict[str, dict] = {}
        for r in rows:
            if r["task_key"] not in latest:
                latest[r["task_key"]] = {
                    "kind": r["kind"],
                    "status": r["status"],
                    "member_name": r["member_name"],
                }

        parts: list[str] = []
        for task_key in blocked:
            info = latest.get(task_key)
            if info is None:
                parts.append(f"{task_key} (not yet posted)")
                continue
            kind = info["kind"]
            status = info["status"]
            name = info["member_name"]
            if kind == "claim" and status == "open":
                parts.append(f"{task_key} (claimed by @{name}, in progress)")
            elif kind == "claim" and status == "stale":
                parts.append(f"{task_key} (stale claim by @{name}, heartbeat expired)")
            elif kind == "failed" or status == "failed":
                parts.append(f"{task_key} (failed by @{name})")
            elif kind == "assign":
                parts.append(f"{task_key} (assigned to @{name}, not yet started)")
            else:
                parts.append(f"{task_key} ({kind}/{status or 'no-status'})")
        return ", ".join(parts)

    def _find_cycle(self, hub_id: str, new_task_key: str,
                    declared_deps: list[str]) -> list[str] | None:
        """Return a cycle path if the new claim would create one, else None.

        Walks the depends_on graph forward from each declared dep (BFS).
        When we find a dep that points back to new_task_key, we've closed
        the cycle and can return the path:

            [new_task_key, declared_dep, ..., closing_node, new_task_key]

        BFS batches each frontier level into one query, so the cost is
        O(levels) queries bounded by the graph's D (3-5 in practice).
        """
        if not declared_deps:
            return None

        # Degenerate self-loop: new_task_key is in its own declared deps
        if new_task_key in declared_deps:
            return [new_task_key, new_task_key]

        # parent[t] = the node that discovered t during BFS; None = root (declared dep)
        parent: dict[str, str | None] = {d: None for d in declared_deps}
        frontier: set[str] = set(declared_deps)
        visited: set[str] = set()

        while frontier:
            placeholders = ",".join("?" * len(frontier))
            rows = self.conn.execute(
                f"SELECT task_key, depends_on FROM posts "
                f"WHERE hub_id = ? AND task_key IN ({placeholders}) "
                f"AND depends_on IS NOT NULL",
                (hub_id, *frontier),
            ).fetchall()

            visited |= frontier
            next_frontier: set[str] = set()
            for r in rows:
                src = r["task_key"]
                try:
                    deps = json.loads(r["depends_on"])
                except Exception:
                    deps = []
                for d in deps:
                    if d == new_task_key:
                        # Cycle: src transitively depends on new_task_key,
                        # and now src also depends directly on new_task_key.
                        # Path: new_task_key → declared_dep → ... → src → new_task_key
                        return self._reconstruct_cycle_path(new_task_key, parent, src)
                    if d not in visited and d not in next_frontier:
                        parent[d] = src
                        next_frontier.add(d)
            frontier = next_frontier

        return None

    @staticmethod
    def _reconstruct_cycle_path(new_task_key: str,
                                parent: dict[str, str | None],
                                closing_node: str) -> list[str]:
        """Build the cycle path from a parent-map walk.

        closing_node is the node whose depends_on points back to
        new_task_key — i.e. the last node in the cycle before the loop
        closes. We walk backward from there through parent pointers
        until we hit a root (None), then reverse to get forward order.
        """
        chain: list[str] = [closing_node]
        cur: str | None = parent.get(closing_node)
        guard = 0
        while cur is not None and guard < 64:
            chain.append(cur)
            cur = parent.get(cur)
            guard += 1
        chain.reverse()
        # chain is now: declared_dep → ... → closing_node
        # The full cycle is: new → chain → new
        return [new_task_key, *chain, new_task_key]

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

    async def get_ready_tasks(self, hub_id: str, member_id: str) -> list[Post]:
        """Return tasks assigned to a member where every direct dep is done.

        Two-query implementation: one query fetches the set of all done
        task_keys for the hub, a second query fetches this member's
        candidate assignments. Readiness is then a pure Python set
        membership check per candidate — no per-candidate round trip,
        no recursive self-reference, no extra work.

        A task is "ready" iff:
          - It's assigned to this member (assignee_id = member_id)
          - It's kind='assign'
          - It has NOT yet been reported as done/failed
          - Every task_key in its depends_on array is directly done

        Transitive readiness across a dep chain resolves naturally via
        the workflow: an agent calls claim_next, ships, reports done,
        calls claim_next again. The recursive SQL version was attempted
        (see atris/research/papers/ouro-looped-lm.md, Ouro tick 6) but
        SQLite forbids recursive CTE self-reference inside subqueries,
        making the json_each-based formulation impossible without an
        auxiliary fixpoint. The two-query approach is the same order of
        complexity (O(candidates + deps)) without the round-trip explosion
        the previous N+1 implementation had.
        """
        with self._lock:
            # Query 1: every task_key that's currently done in this hub
            done_rows = self.conn.execute(
                "SELECT DISTINCT task_key FROM posts "
                "WHERE hub_id = ? AND task_key IS NOT NULL AND status = 'done'",
                (hub_id,),
            ).fetchall()
            done_set = {r["task_key"] for r in done_rows}

            # Query 2: my candidate assignments (not yet done/failed)
            rows = self.conn.execute(
                """
                SELECT * FROM posts
                WHERE hub_id = ? AND assignee_id = ? AND kind = 'assign'
                  AND task_key NOT IN (
                      SELECT task_key FROM posts
                      WHERE hub_id = ? AND task_key IS NOT NULL
                        AND status IN ('done', 'failed')
                  )
                """,
                (hub_id, member_id, hub_id),
            ).fetchall()

        ready: list[Post] = []
        for row in rows:
            depends_on_raw = row["depends_on"]
            if not depends_on_raw:
                ready.append(self._row_to_post(row))
                continue
            try:
                deps = json.loads(depends_on_raw)
            except Exception:
                deps = []
            # Set-membership check — O(deps) per candidate, no extra queries
            if all(d in done_set for d in deps):
                ready.append(self._row_to_post(row))
        # Higher priority first, then oldest first (FIFO within same priority)
        ready.sort(key=lambda p: (-p.priority, p.created_at))
        return ready

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
        """Retrieve a single commit by its hash."""
        row = self.conn.execute(
            "SELECT * FROM commits WHERE hash = ? AND hub_id = ?", (hash, hub_id)
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def list_commits(self, hub_id: str, member_id: str | None = None, limit: int = 50) -> list[dict]:
        """List recent commits, optionally filtered by member."""
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
        """Get all commits that have this hash as their parent."""
        rows = self.conn.execute(
            "SELECT * FROM commits WHERE hub_id = ? AND parent_hash = ? ORDER BY created_at DESC",
            (hub_id, hash),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_leaves(self, hub_id: str) -> list[dict]:
        """Get commits with no children (branch tips)."""
        rows = self.conn.execute("""
            SELECT c.* FROM commits c
            LEFT JOIN commits child ON child.parent_hash = c.hash AND child.hub_id = c.hub_id
            WHERE c.hub_id = ? AND child.hash IS NULL
            ORDER BY c.created_at DESC
        """, (hub_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_lineage(self, hub_id: str, hash: str) -> list[dict]:
        """Walk the parent chain from a commit back to root."""
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
