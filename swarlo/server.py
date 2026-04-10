"""Swarlo standalone server. FastAPI + SQLite. No external dependencies beyond fastapi + uvicorn.

Usage:
    pip install swarlo
    swarlo serve --port 8080
    swarlo serve --port 8080 --db /path/to/swarlo.db
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .sqlite_backend import SQLiteBackend
from .git_dag import GitDAG
from .types import Member

logger = logging.getLogger("swarlo.server")

app = FastAPI(title="Swarlo", description="Open coordination protocol for AI agent swarms")

_backend: SQLiteBackend | None = None
_git_dag: GitDAG | None = None


def get_backend() -> SQLiteBackend:
    global _backend
    if _backend is None:
        _backend = SQLiteBackend("swarlo.db")
    return _backend


def get_dag() -> GitDAG:
    global _git_dag
    if _git_dag is None:
        _git_dag = GitDAG("swarlo.git")
        _git_dag.init()
    return _git_dag


def set_backend(backend: SQLiteBackend):
    global _backend
    _backend = backend


def set_dag(dag: GitDAG):
    global _git_dag
    _git_dag = dag


# ── Auth ────────────────────────────────────────────────────

def _get_member(request: Request) -> Member:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization: Bearer <api_key>")
    api_key = auth[7:]
    be = get_backend()
    member = be.authenticate(api_key)
    if not member:
        raise HTTPException(401, "Invalid API key")
    # Bump last_seen on every authenticated call
    try:
        from datetime import datetime, timezone
        be.conn.execute(
            "UPDATE members SET last_seen = ? WHERE member_id = ? AND hub_id = ?",
            (datetime.now(timezone.utc).isoformat(), member.member_id, member.hub_id),
        )
        be.conn.commit()
    except Exception:
        pass  # best-effort, don't break auth
    return member


# ── Request models ──────────────────────────────────────────

class RegisterRequest(BaseModel):
    member_id: str
    member_type: str = "agent"
    member_name: str = ""
    hub_id: str = "default"
    webhook_url: Optional[str] = None


class PostRequest(BaseModel):
    content: str
    kind: str = "message"
    task_key: Optional[str] = None
    priority: int = 0
    metadata: Optional[dict] = None


class ClaimRequest(BaseModel):
    task_key: str
    content: str


class ReportRequest(BaseModel):
    task_key: str
    status: str = Field(..., pattern="^(done|failed|blocked)$")
    content: str


class AssignRequest(BaseModel):
    task_key: str
    assignee_id: str
    content: str


class ReplyRequest(BaseModel):
    content: str


# ── Registration (no auth) ──────────────────────────────────

@app.post("/api/register", status_code=201)
async def register(body: RegisterRequest):
    if body.webhook_url and not _is_safe_webhook_url(body.webhook_url):
        raise HTTPException(400, "Invalid webhook URL: must be HTTPS and not target private networks")
    api_key = secrets.token_hex(32)
    member = Member(
        member_id=body.member_id,
        member_type=body.member_type,
        member_name=body.member_name or body.member_id,
        hub_id=body.hub_id,
        webhook_url=body.webhook_url,
    )
    get_backend().register_member(member, api_key=api_key)
    return {"member_id": member.member_id, "api_key": api_key, "hub_id": member.hub_id}


# ── Health (no auth) ────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── Channels ────────────────────────────────────────────────

@app.get("/api/{hub_id}/channels")
async def list_channels(hub_id: str, request: Request):
    _get_member(request)
    channels = await get_backend().list_channels(hub_id)
    return {"channels": channels}


# ── Read ────────────────────────────────────────────────────

@app.get("/api/{hub_id}/channels/{channel}/posts")
async def list_posts(hub_id: str, channel: str, request: Request, limit: int = 10):
    _get_member(request)
    posts = await get_backend().read_channel(hub_id, channel, limit=min(limit, 50))
    return {"channel": channel, "count": len(posts), "posts": [p.to_dict() for p in posts]}


# ── Post ────────────────────────────────────────────────────

@app.post("/api/{hub_id}/channels/{channel}/posts", status_code=201)
async def create_post(hub_id: str, channel: str, body: PostRequest, request: Request, background_tasks: BackgroundTasks):
    member = _get_member(request)
    post = await get_backend().create_post(hub_id, member, channel, body.content, body.kind, body.task_key, metadata=body.metadata, priority=body.priority)
    if post.mentions:
        background_tasks.add_task(_dispatch_webhooks, hub_id, post)
    return post.to_dict()


# ── Claim ───────────────────────────────────────────────────

@app.post("/api/{hub_id}/channels/{channel}/claim", status_code=201)
async def claim_task(hub_id: str, channel: str, body: ClaimRequest, request: Request):
    member = _get_member(request)
    result = await get_backend().claim(hub_id, member, channel, body.task_key, body.content)
    if result.conflict:
        raise HTTPException(409, result.to_dict())
    return result.to_dict()


# ── File Claims ────────────────────────────────────────────

class FileClaimRequest(BaseModel):
    file_path: str
    content: str = ""


@app.post("/api/{hub_id}/channels/{channel}/claim-file", status_code=201)
async def claim_file(hub_id: str, channel: str, body: FileClaimRequest, request: Request):
    """Claim a file to prevent two agents editing it simultaneously.
    Uses task_key = 'file:<path>' convention on the existing claim system."""
    member = _get_member(request)
    task_key = f"file:{body.file_path}"
    desc = body.content or f"Editing {body.file_path}"
    result = await get_backend().claim(hub_id, member, channel, task_key, desc)
    if result.conflict:
        raise HTTPException(409, result.to_dict())
    return result.to_dict()


@app.get("/api/{hub_id}/file-claims")
async def list_file_claims(hub_id: str, request: Request):
    """List all currently claimed files across all channels."""
    _get_member(request)
    claims = await get_backend().get_open_claims(hub_id)
    file_claims = [c for c in claims if c.task_key and c.task_key.startswith("file:")]
    return {
        "count": len(file_claims),
        "files": [
            {
                "file_path": c.task_key[5:],  # strip "file:" prefix
                "claimed_by": c.member_name,
                "member_id": c.member_id,
                "channel": c.channel,
                "claimed_at": c.created_at,
            }
            for c in file_claims
        ],
    }


# ── Assign ─────────────────────────────────────────────────

@app.post("/api/{hub_id}/channels/{channel}/assign", status_code=201)
async def assign_task(hub_id: str, channel: str, body: AssignRequest, request: Request, background_tasks: BackgroundTasks):
    """Push-assign a task to a specific member. Creates a claim on their behalf and notifies via webhook."""
    assigner = _get_member(request)
    be = get_backend()
    result = await be.assign(hub_id, assigner, channel, body.task_key, body.assignee_id, body.content)
    if not result.claimed:
        if result.conflict:
            raise HTTPException(409, result.to_dict())
        raise HTTPException(400, result.message or "Assignment failed")

    # Fire webhook to assignee — post_id is the claim (what assignee acts on)
    assignee = be.get_member(hub_id, body.assignee_id)
    if assignee and assignee.webhook_url:
        from .types import Post
        notify_post = Post(
            post_id=result.post_id or "", content=body.content, kind="claim",
            channel=channel, member_id=body.assignee_id, member_name=assignee.member_name,
            member_type=assignee.member_type, task_key=body.task_key, status="open",
            metadata={"assigned_by": assigner.member_id},
            mentions=[body.assignee_id],
        )
        background_tasks.add_task(_dispatch_webhooks, hub_id, notify_post)

    return result.to_dict()


# ── Report ──────────────────────────────────────────────────

@app.post("/api/{hub_id}/channels/{channel}/report", status_code=201)
async def report_result(hub_id: str, channel: str, body: ReportRequest, request: Request):
    member = _get_member(request)
    try:
        post = await get_backend().report(hub_id, member, channel, body.task_key, body.status, body.content)
    except PermissionError as exc:
        raise HTTPException(409, str(exc))
    return post.to_dict()


# ── Touch (keepalive) ──────────────────────────────────────

class TouchRequest(BaseModel):
    task_key: str


@app.post("/api/{hub_id}/channels/{channel}/touch")
async def touch_claim(hub_id: str, channel: str, body: TouchRequest, request: Request):
    member = _get_member(request)
    task_key = body.task_key
    ok = await get_backend().touch_claim(hub_id, member.member_id, task_key)
    if not ok:
        raise HTTPException(404, f"No open claim for {task_key}")
    return {"touched": True, "task_key": task_key}


# ── Force-expire stale claims ─────────────────────────────

@app.post("/api/{hub_id}/claims/expire")
async def expire_stale_claims(hub_id: str, request: Request):
    _get_member(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    stale_minutes = body.get("stale_minutes", 30) if isinstance(body, dict) else 30
    expired = await get_backend().force_expire_claims(hub_id, stale_minutes=stale_minutes)
    return {"expired": expired, "count": len(expired)}


# ── Retry failed tasks ─────────────────────────────────────

@app.post("/api/{hub_id}/claims/retry")
async def retry_failed_tasks(hub_id: str, request: Request):
    _get_member(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    max_retries = body.get("max_retries", 3) if isinstance(body, dict) else 3
    retried = await get_backend().retry_failed(hub_id, max_retries=max_retries)
    return {"retried": retried, "count": len(retried)}


# ── Claims ──────────────────────────────────────────────────

@app.get("/api/{hub_id}/claims")
async def list_claims(hub_id: str, request: Request, channel: Optional[str] = None):
    _get_member(request)
    claims = await get_backend().get_open_claims(hub_id, channel=channel)
    return {"count": len(claims), "claims": [c.to_dict() for c in claims]}


# ── Summary ────────────────────────────────────────────────

@app.get("/api/{hub_id}/summary")
async def get_summary(hub_id: str, request: Request, limit: int = 10):
    member = _get_member(request)
    text = await get_backend().summarize_for_member(hub_id, member.member_id, limit=min(limit, 50))
    return {"summary": text}


# ── Replies ─────────────────────────────────────────────────

@app.get("/api/{hub_id}/posts/{post_id}/replies")
async def list_replies(hub_id: str, post_id: str, request: Request):
    _get_member(request)
    replies = await get_backend().get_replies(hub_id, post_id)
    return {
        "post_id": post_id,
        "count": len(replies),
        "replies": [r.to_dict() for r in replies],
    }


@app.post("/api/{hub_id}/posts/{post_id}/replies", status_code=201)
async def create_reply(hub_id: str, post_id: str, body: ReplyRequest, request: Request):
    member = _get_member(request)
    reply = await get_backend().reply(hub_id, member, post_id, body.content)
    return reply.to_dict()


# ── Webhooks ───────────────────────────────────────────────

import ipaddress
from urllib.parse import urlparse


_LOCALHOST = ("localhost", "127.0.0.1", "::1")


def _is_safe_webhook_url(url: str, at_dispatch: bool = False) -> bool:
    """Validate webhook URL against SSRF.

    Rules:
    - localhost: always allowed (server itself is local)
    - External: must be HTTPS, must resolve to a globally-routable IP
    - at_dispatch=True: DNS must resolve (blocks rebinding attacks)
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if not parsed.scheme or not parsed.hostname:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    # Localhost is always allowed (the server runs locally)
    if parsed.hostname in _LOCALHOST:
        return True

    # External: require HTTPS
    if parsed.scheme != "https":
        return False

    # Resolve DNS and require globally-routable IPs
    try:
        import socket
        resolved = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        for _, _, _, _, addr in resolved:
            ip = ipaddress.ip_address(addr[0])
            if not ip.is_global:
                return False
    except (socket.gaierror, ValueError):
        if at_dispatch:
            return False  # Must resolve at dispatch time
        # Registration: allow — will re-check at dispatch

    return True


async def _dispatch_webhooks(hub_id: str, post):
    """Fire webhook callbacks for @mentioned members. Best-effort, no retries."""
    backend = get_backend()
    members = backend.get_members_by_ids(hub_id, post.mentions)
    async with httpx.AsyncClient(timeout=10) as client:
        for m in members:
            if not m.webhook_url:
                continue
            if not _is_safe_webhook_url(m.webhook_url, at_dispatch=True):
                logger.warning(f"Blocked unsafe webhook for member {m.member_id}")
                continue
            try:
                await client.post(m.webhook_url, json={
                    "event": "mention",
                    "hub_id": hub_id,
                    "post": post.to_dict(),
                    "mentioned_member_id": m.member_id,
                })
            except Exception as e:
                logger.debug(f"Webhook to {m.member_id} failed: {e}")


# ── Members ────────────────────────────────────────────────

@app.get("/api/{hub_id}/members")
async def list_members(hub_id: str, request: Request):
    _get_member(request)
    rows = get_backend().conn.execute(
        "SELECT member_id, member_name, member_type, created_at, last_seen FROM members WHERE hub_id = ?",
        (hub_id,),
    ).fetchall()
    return {"count": len(rows), "members": [dict(r) for r in rows]}


@app.delete("/api/{hub_id}/members/{member_id}")
async def delete_member(hub_id: str, member_id: str, request: Request):
    caller = _get_member(request)
    be = get_backend()
    row = be.conn.execute(
        "SELECT member_id FROM members WHERE hub_id = ? AND member_id = ?",
        (hub_id, member_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Member {member_id} not found")
    be.conn.execute(
        "DELETE FROM members WHERE hub_id = ? AND member_id = ?",
        (hub_id, member_id),
    )
    be.conn.commit()
    return {"deleted": member_id}


@app.post("/api/{hub_id}/prune")
async def prune_members(hub_id: str, request: Request):
    """Remove members not seen in `stale_minutes` (default 60)."""
    caller = _get_member(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    stale_minutes = body.get("stale_minutes", 60) if isinstance(body, dict) else 60
    be = get_backend()
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).isoformat()
    rows = be.conn.execute(
        "SELECT member_id FROM members WHERE hub_id = ? AND (last_seen IS NOT NULL AND last_seen < ?) AND member_type != 'human'",
        (hub_id, cutoff),
    ).fetchall()
    pruned = [r["member_id"] for r in rows]
    if pruned:
        be.conn.execute(
            f"DELETE FROM members WHERE hub_id = ? AND member_id IN ({','.join('?' * len(pruned))})",
            (hub_id, *pruned),
        )
        be.conn.commit()
    return {"pruned": pruned, "count": len(pruned)}


# ── Orchestrator Scoring ────────────────────────────────────

@app.post("/api/{hub_id}/score")
async def compute_score(hub_id: str, request: Request):
    """Compute orchestrator performance metrics.

    Returns:
        agents_active: members with activity in last hour
        tasks_claimed: open claims
        tasks_shipped: completed results
        avg_time_to_claim: average seconds from task post to claim
    """
    _get_member(request)  # Auth check
    be = get_backend()
    from datetime import datetime, timezone, timedelta

    # Active agents (seen in last 60 minutes)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    agents_active = be.conn.execute(
        "SELECT COUNT(*) FROM members WHERE hub_id = ? AND last_seen > ? AND member_type = 'agent'",
        (hub_id, cutoff),
    ).fetchone()[0]

    # Open claims
    tasks_claimed = be.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ? AND kind = 'claim' AND status = 'open'",
        (hub_id,),
    ).fetchone()[0]

    # Completed results
    tasks_shipped = be.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ? AND kind = 'result'",
        (hub_id,),
    ).fetchone()[0]

    # Avg time to claim: find task posts that have matching claims
    # A claim references a task_key; find pairs and compute time diff
    rows = be.conn.execute(
        """
        SELECT
            t.created_at as task_time,
            c.created_at as claim_time
        FROM posts t
        JOIN posts c ON t.task_key = c.task_key AND t.hub_id = c.hub_id
        WHERE t.hub_id = ?
          AND t.kind = 'message'
          AND t.task_key IS NOT NULL
          AND c.kind = 'claim'
        ORDER BY c.created_at DESC
        LIMIT 100
        """,
        (hub_id,),
    ).fetchall()

    avg_time_to_claim = None
    if rows:
        total_seconds = 0
        count = 0
        for row in rows:
            try:
                task_dt = datetime.fromisoformat(row["task_time"].replace("Z", "+00:00"))
                claim_dt = datetime.fromisoformat(row["claim_time"].replace("Z", "+00:00"))
                diff = (claim_dt - task_dt).total_seconds()
                if diff >= 0:  # Sanity check
                    total_seconds += diff
                    count += 1
            except Exception:
                continue
        if count > 0:
            avg_time_to_claim = round(total_seconds / count, 1)

    # Store score in SQLite for history (create table if needed)
    try:
        be.conn.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hub_id TEXT NOT NULL,
                agents_active INTEGER,
                tasks_claimed INTEGER,
                tasks_shipped INTEGER,
                avg_time_to_claim REAL,
                computed_at TEXT NOT NULL
            )
        """)
        be.conn.execute(
            "INSERT INTO scores (hub_id, agents_active, tasks_claimed, tasks_shipped, avg_time_to_claim, computed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (hub_id, agents_active, tasks_claimed, tasks_shipped, avg_time_to_claim, datetime.now(timezone.utc).isoformat()),
        )
        be.conn.commit()
    except Exception:
        pass  # Non-critical

    return {
        "hub_id": hub_id,
        "agents_active": agents_active,
        "tasks_claimed": tasks_claimed,
        "tasks_shipped": tasks_shipped,
        "avg_time_to_claim": avg_time_to_claim,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Git DAG ─────────────────────────────────────────────────

@app.post("/api/{hub_id}/git/push", status_code=201)
async def git_push(hub_id: str, request: Request):
    member = _get_member(request)
    body = await request.body()
    if len(body) > 50 * 1024 * 1024:
        raise HTTPException(413, "Bundle too large (max 50MB)")

    dag = get_dag()
    hashes = await dag.unbundle(body)

    # Index each commit in SQLite
    backend = get_backend()
    for h in hashes:
        existing = backend.get_commit(hub_id, h)
        if existing:
            continue
        parent_hash, message = dag.get_commit_info(h)
        backend.index_commit(hub_id, h, parent_hash, member.member_id, member.member_name, message)

    return {"hashes": hashes}


@app.get("/api/{hub_id}/git/fetch/{hash}")
async def git_fetch(hub_id: str, hash: str, request: Request):
    _get_member(request)
    dag = get_dag()
    if not dag.commit_exists(hash):
        raise HTTPException(404, "Commit not found")
    from fastapi.responses import Response
    bundle_bytes = dag.create_bundle(hash)
    return Response(content=bundle_bytes, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename={hash[:12]}.bundle"})


@app.get("/api/{hub_id}/git/commits")
async def git_list_commits(hub_id: str, request: Request, member_filter: Optional[str] = None, limit: int = 50):
    _get_member(request)
    return get_backend().list_commits(hub_id, member_id=member_filter, limit=min(limit, 200))


@app.get("/api/{hub_id}/git/commits/{hash}")
async def git_get_commit(hub_id: str, hash: str, request: Request):
    _get_member(request)
    commit = get_backend().get_commit(hub_id, hash)
    if not commit:
        raise HTTPException(404, "Commit not found")
    return commit


@app.get("/api/{hub_id}/git/commits/{hash}/children")
async def git_children(hub_id: str, hash: str, request: Request):
    _get_member(request)
    return get_backend().get_children(hub_id, hash)


@app.get("/api/{hub_id}/git/leaves")
async def git_leaves(hub_id: str, request: Request):
    _get_member(request)
    return get_backend().get_leaves(hub_id)


@app.get("/api/{hub_id}/git/commits/{hash}/lineage")
async def git_lineage(hub_id: str, hash: str, request: Request):
    _get_member(request)
    return get_backend().get_lineage(hub_id, hash)


@app.get("/api/{hub_id}/git/diff/{hash_a}/{hash_b}")
async def git_diff(hub_id: str, hash_a: str, hash_b: str, request: Request):
    _get_member(request)
    dag = get_dag()
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(dag.diff(hash_a, hash_b))
