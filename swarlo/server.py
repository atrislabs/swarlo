"""Swarlo standalone server. FastAPI + SQLite. No external dependencies beyond fastapi + uvicorn.

Usage:
    pip install swarlo
    swarlo serve --port 8080
    swarlo serve --port 8080 --db /path/to/swarlo.db
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import posixpath
import re
import secrets
import socket
import sqlite3
import subprocess
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field, StringConstraints, field_validator

# A task identifier that must be non-empty. Leading/trailing whitespace is
# stripped so "task:foo " and "task:foo" name the same lock, and a
# whitespace-only key is rejected (422) instead of creating a phantom
# shared claim that every empty-key claimant collides on.
TaskKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

from . import gc
from .sqlite_backend import SQLiteBackend
from .git_dag import GitDAG
from .types import Member

logger = logging.getLogger("swarlo.server")

app = FastAPI(title="Swarlo", description="Open coordination protocol for AI agent swarms")

_backend: SQLiteBackend | None = None
_git_dag: GitDAG | None = None
BRIEFING_LOOKBACK_DAYS = 14
REPLAY_MAX_LOOKBACK_DAYS = 7
OPEN_CLAIM_STALE_MINUTES = 30

# Noise quarantine on POST /channels/{channel}/posts.
# Members carry no bot flag, so a test bot is marked by a standalone "test"
# token in member_id or member_name ("op-test", "wake-test", "test-1",
# "Test Bot"). "latest" and "contest" do not match: the token must stand
# alone. Test bots may post only to #sandbox.
TEST_BOT_CHANNEL = "sandbox"
TEST_BOT_NAME_RE = re.compile(r"(?:^|[^a-z0-9])test(?:[^a-z0-9]|$)", re.IGNORECASE)
# Sliding-window cap on new posts, per member per hub. In-memory: a restart
# resets it, which is fine for a noise guard.
POST_RATE_LIMIT_PER_MINUTE = 10
POST_RATE_WINDOW_SECONDS = 60.0
TASK_POST_RATE_LIMIT_PER_MINUTE = 2
TASK_POST_RATE_KINDS = {"status", "result", "failed"}
# Registration rate limit per client IP per minute.
REGISTER_RATE_LIMIT_PER_MINUTE = 100
REGISTER_RATE_WINDOW_SECONDS = 60.0
# In-memory cache caps to prevent unbounded RAM growth under churn.
MAX_HUB_DAGS = 256
MAX_RATE_LIMIT_BUCKETS = 10_000
MAX_GIT_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_WINDOW_MINUTES = 525600
_ID_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
# An identical body retried within this window gets the first post back
# (200, idempotent) instead of a second row.
POST_DEDUPE_WINDOW_SECONDS = 60.0

# (hub_id, member_id) -> monotonic timestamps of accepted post attempts.
_post_attempts: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
_task_post_attempts: OrderedDict[tuple[str, str, str, str], deque[float]] = OrderedDict()
_register_attempts: OrderedDict[str, deque[float]] = OrderedDict()


def _utc_cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _open_claim_cutoff(now: datetime | None = None) -> str:
    reference = now or datetime.now(timezone.utc)
    return (reference - timedelta(minutes=OPEN_CLAIM_STALE_MINUTES)).isoformat()


def _open_claim_predicate(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}kind = 'claim' AND {prefix}status = 'open' "
        f"AND COALESCE(json_extract({prefix}metadata, '$.heartbeat_at'), "
        f"{prefix}created_at) >= ?"
    )


def _bounded_replay_since(since: str) -> str:
    oldest_allowed = datetime.now(timezone.utc) - timedelta(days=REPLAY_MAX_LOOKBACK_DAYS)
    try:
        parsed = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
    except ValueError:
        return oldest_allowed.isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed < oldest_allowed:
        return oldest_allowed.isoformat()
    return parsed.isoformat()


def get_backend() -> SQLiteBackend:
    """Get or create the global SQLite backend instance."""
    global _backend
    if _backend is None:
        _backend = SQLiteBackend("swarlo.db")
    return _backend


_hub_dags: OrderedDict[str, GitDAG] = OrderedDict()


def get_dag(hub_id: str | None = None) -> GitDAG:
    """Get the git store for commit tracking.

    Each hub gets its own store under the root one, so a hub can only ever
    read objects it pushed. A shared store let any member fetch another hub's
    code by hash, or forge a bundle head that named it. The directory is a
    hash of the hub id, so a hub id never becomes a path.
    """
    global _git_dag
    if _git_dag is None:
        _git_dag = GitDAG("swarlo.git")
        _git_dag.init()
    if hub_id is None:
        return _git_dag
    key = f"{_git_dag.path}\0{hub_id}"
    dag = _hub_dags.get(key)
    if dag is not None:
        _hub_dags.move_to_end(key)
        return dag
    name = hashlib.sha256(hub_id.encode("utf-8")).hexdigest()[:32]
    dag = GitDAG(str(_git_dag.path / "hubs" / f"{name}.git"))
    dag.init()
    _hub_dags[key] = dag
    while len(_hub_dags) > MAX_HUB_DAGS:
        _hub_dags.popitem(last=False)
    return dag


def set_backend(backend: SQLiteBackend):
    """Set the global SQLite backend instance for server requests."""
    global _backend
    _backend = backend
    # Rate-limit state is per backend lifetime: swapping the backend (tests,
    # reconfigure) starts a fresh window.
    _post_attempts.clear()
    _task_post_attempts.clear()
    _register_attempts.clear()


def set_dag(dag: GitDAG):
    """Set the global GitDAG instance for commit tracking."""
    global _git_dag
    _git_dag = dag
    _hub_dags.clear()


# Daily self-prune of the live hub (~/.swarlo). Only runs when this server
# serves HOME/atris.db, so tests and side servers never prune the real hub.
HOME = Path.home() / ".swarlo"
_prune_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_prune_schedule():
    global _prune_task
    if _backend is None or Path(_backend.db_path).resolve() != (HOME / "atris.db").resolve():
        return
    _prune_task = gc.start_schedule(lambda: asyncio.to_thread(gc.prune_hub, HOME), HOME)


@app.on_event("shutdown")
async def _stop_prune_schedule():
    global _prune_task
    if _prune_task is not None:
        _prune_task.cancel()
        await asyncio.gather(_prune_task, return_exceptions=True)
        _prune_task = None


# ── Auth ────────────────────────────────────────────────────

def _get_member(request: Request, hub_id: str) -> Member:
    """Authenticate the member (401) and require access to the requested hub (403)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization: Bearer <api_key>")
    api_key = auth[7:]
    be = get_backend()
    member = be.authenticate(api_key)
    if not member:
        raise HTTPException(401, "Invalid API key")
    if member.hub_id != hub_id:
        raise HTTPException(403, "Member does not belong to this hub")
    # Bump last_seen on every authenticated call
    try:
        with be._write_lock:
            be.conn.execute(
                "UPDATE members SET last_seen = ? WHERE member_id = ? AND hub_id = ?",
                (datetime.now(timezone.utc).isoformat(), member.member_id, member.hub_id),
            )
            be.conn.commit()
    except Exception:
        pass  # best-effort, don't break auth
    return member


def _require_human(member: Member) -> None:
    if member.member_type != "human":
        raise HTTPException(403, "Human operator required")


def _enforce_claimed_member(member: Member, claimed_id: str | None,
                            claimed_name: str | None) -> None:
    """Reject a write that claims a different member than the API key's owner.

    Attribution always comes from the authenticated member, so these fields
    are a self-declared cross-check: a client holding a stale or shared
    credential fails loudly (403) instead of silently posting as someone
    else. Absent fields keep older clients working unchanged.
    """
    if claimed_id and claimed_id != member.member_id:
        raise HTTPException(
            403, "Claimed member_id does not match the authenticated member")
    if claimed_name and claimed_name != member.member_name:
        raise HTTPException(
            403, "Claimed member_name does not match the authenticated member")


def _is_test_bot(member: Member) -> bool:
    """True when the member's id or name carries a standalone "test" token.

    Members have no bot flag, so the name is the marker: "op-test",
    "wake-test", "lane-test", "test-1", and "Test Bot" match, while
    "latest", "contest", and "testing" do not (the token must stand alone).
    """
    return bool(TEST_BOT_NAME_RE.search(member.member_id)
                or TEST_BOT_NAME_RE.search(member.member_name))


def _enforce_test_bot_channel(member: Member, channel: str) -> None:
    """Quarantine test bots to #sandbox; every other channel is a 403."""
    if _is_test_bot(member) and channel != TEST_BOT_CHANNEL:
        raise HTTPException(
            403, f"Test bots may only post to #{TEST_BOT_CHANNEL}")


def _prune_rate_buckets(store: OrderedDict, cutoff: float) -> None:
    if len(store) <= MAX_RATE_LIMIT_BUCKETS:
        return
    stale_keys = [k for k, dq in store.items() if not dq or dq[-1] <= cutoff]
    for k in stale_keys:
        store.pop(k, None)
    while len(store) > MAX_RATE_LIMIT_BUCKETS:
        store.popitem(last=False)


def _enforce_register_rate_limit(client_key: str) -> None:
    """Cap registration attempts per client IP per minute."""
    now = time.monotonic()
    cutoff = now - REGISTER_RATE_WINDOW_SECONDS
    attempts = _register_attempts.get(client_key)
    if attempts is None:
        attempts = _register_attempts[client_key] = deque()
    else:
        _register_attempts.move_to_end(client_key)
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()
    if len(attempts) >= REGISTER_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            429, f"Rate limit: at most {REGISTER_RATE_LIMIT_PER_MINUTE} registrations per minute",
        )
    attempts.append(now)
    _prune_rate_buckets(_register_attempts, cutoff)


def _enforce_post_rate_limit(hub_id: str, member_id: str,
                             task_key: str | None, kind: str) -> None:
    """Cap new posts at POST_RATE_LIMIT_PER_MINUTE per member per hub per
    minute, plus two task-bound status/result/failed posts per task and kind.
    Only reached after dedupe, so an idempotent retry never spends a slot."""
    now = time.monotonic()
    key = (hub_id, member_id)
    attempts = _post_attempts.get(key)
    if attempts is None:
        attempts = _post_attempts[key] = deque()
    else:
        _post_attempts.move_to_end(key)
    cutoff = now - POST_RATE_WINDOW_SECONDS
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()
    task_attempts = None
    if task_key and kind in TASK_POST_RATE_KINDS:
        task_key_bucket = (hub_id, member_id, task_key, kind)
        task_attempts = _task_post_attempts.get(task_key_bucket)
        if task_attempts is None:
            task_attempts = _task_post_attempts[task_key_bucket] = deque()
        else:
            _task_post_attempts.move_to_end(task_key_bucket)
        while task_attempts and task_attempts[0] <= cutoff:
            task_attempts.popleft()
        if len(task_attempts) >= TASK_POST_RATE_LIMIT_PER_MINUTE:
            raise HTTPException(429, "Rate limit: too many posts for this task and kind")
    if len(attempts) >= POST_RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            429, f"Rate limit: at most {POST_RATE_LIMIT_PER_MINUTE} posts per minute")
    attempts.append(now)
    if task_attempts is not None:
        task_attempts.append(now)
    _prune_rate_buckets(_post_attempts, cutoff)
    _prune_rate_buckets(_task_post_attempts, cutoff)


# ── Request models ──────────────────────────────────────────

class _ClaimedIdentity(BaseModel):
    """Optional self-declared identity on member-attributed writes.

    Each field present must match the authenticated member exactly; a
    mismatch is rejected with 403 (see _enforce_claimed_member).
    """

    member_id: Optional[str] = None
    member_name: Optional[str] = None


class RegisterRequest(BaseModel):
    """Request to register a new member with the coordination hub."""

    member_id: str
    member_type: Literal["agent", "human"] = "agent"
    member_name: str = ""
    hub_id: str = "default"
    webhook_url: Optional[str] = None
    invite_token: Optional[str] = None

    @field_validator("member_id", "hub_id")
    @classmethod
    def _validate_identifier(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or len(stripped) > 128 or ".." in stripped or not _ID_SLUG_RE.fullmatch(stripped):
            raise ValueError(
                "Identifier must be 1-128 chars matching [A-Za-z0-9._:@-], start with alphanumeric, and contain no '..' or path separators"
            )
        return stripped

    @field_validator("member_name")
    @classmethod
    def _validate_member_name(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) > 128 or any(ord(c) < 32 for c in stripped):
            raise ValueError("member_name must be at most 128 chars with no control characters")
        return stripped


class InviteRequest(BaseModel):
    member_type: Literal["agent", "human"] = "agent"
    expires_in_minutes: int = Field(default=60, ge=1, le=10080)


class PostRequest(_ClaimedIdentity):
    """Request to create a new post in a channel."""

    content: str
    kind: str = "message"
    task_key: Optional[str] = None
    priority: int = 0
    depends_on: Optional[list[str]] = None
    metadata: Optional[dict] = None


class ClaimRequest(_ClaimedIdentity):
    """Request to claim a task for exclusive work."""

    task_key: TaskKey
    content: str
    depends_on: Optional[list[str]] = None


class ReportRequest(_ClaimedIdentity):
    """Request to report task completion status."""

    task_key: TaskKey
    status: str = Field(..., pattern="^(done|failed|blocked)$")
    content: str
    affected_files: Optional[list[str]] = None
    metadata: Optional[dict] = None
    include_next: bool = False  # if True, atomically return next ready task
    suggest_if_empty: bool = False  # if True AND include_next returns null, auto-suggest


class AssignRequest(_ClaimedIdentity):
    """Request to push-assign a task to a specific member."""

    task_key: TaskKey
    assignee_id: str
    content: str
    depends_on: Optional[list[str]] = None
    priority: int = 0  # 0-5, higher = claimed first by claim_next


class ReplyRequest(_ClaimedIdentity):
    """Request to reply to an existing post."""

    content: str


# ── Registration and invites ────────────────────────────────

@app.post("/api/register", status_code=201)
async def register(body: RegisterRequest, request: Request):
    """Create a hub openly; require an invite or current member key to join one."""
    client_host = request.client.host if request.client else "unknown"
    _enforce_register_rate_limit(client_host)
    if body.webhook_url and not await _is_safe_webhook_url_async(body.webhook_url):
        raise HTTPException(400, "Invalid webhook URL: must be HTTPS and not target private networks")
    be = get_backend()
    api_key = secrets.token_hex(32)
    member = Member(
        member_id=body.member_id,
        member_type=body.member_type,
        member_name=body.member_name or body.member_id,
        hub_id=body.hub_id,
        webhook_url=body.webhook_url,
    )
    try:
        auth = request.headers.get("Authorization", "")
        be.register_member(
            member, api_key=api_key, require_authority=True,
            sponsor_key=auth[7:] if auth.startswith("Bearer ") else None,
            invite_token=body.invite_token,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Member already registered; use the current key to rotate credentials")
    return {"member_id": member.member_id, "api_key": api_key, "hub_id": member.hub_id}


@app.post("/api/{hub_id}/invites", status_code=201)
async def create_invite(hub_id: str, body: InviteRequest, request: Request):
    """Invite one new member with a bounded lifetime and explicit member type."""
    _get_member(request, hub_id)
    try:
        return get_backend().create_invite(
            hub_id, request.headers["Authorization"][7:], body.member_type, body.expires_in_minutes,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


@app.post("/api/{hub_id}/rotate-key")
async def rotate_key(hub_id: str, request: Request):
    """Rotate the authenticated member's key without changing their profile or work."""
    member = _get_member(request, hub_id)
    api_key = secrets.token_hex(32)
    if not get_backend().rotate_member_key(member, request.headers["Authorization"][7:], api_key):
        raise HTTPException(401, "Invalid API key")
    return {"member_id": member.member_id, "api_key": api_key, "hub_id": member.hub_id}


# ── Health (no auth) ────────────────────────────────────────

@app.get("/api/health")
async def health():
    """Health check endpoint — returns ok if the server is running."""
    return {"status": "ok"}


# ── Channels ────────────────────────────────────────────────

@app.get("/api/{hub_id}/channels")
async def list_channels(hub_id: str, request: Request):
    """List all channels in the hub including defaults and custom."""
    _get_member(request, hub_id)
    channels = await get_backend().list_channels(hub_id)
    return {"channels": channels}


# ── Read ────────────────────────────────────────────────────

@app.get("/api/{hub_id}/channels/{channel}/posts")
async def list_posts(hub_id: str, channel: str, request: Request, limit: int = 10):
    """List recent posts in a channel, newest first."""
    _get_member(request, hub_id)
    # Floor at 1: a negative limit reaches SQLite as `LIMIT -n`, which SQLite
    # treats as unbounded — returning the whole channel and bypassing the cap.
    posts = await get_backend().read_channel(hub_id, channel, limit=max(1, min(limit, 50)))
    return {"channel": channel, "count": len(posts), "posts": [p.to_dict() for p in posts]}


# ── Post ────────────────────────────────────────────────────

@app.post("/api/{hub_id}/channels/{channel}/posts", status_code=201)
async def create_post(hub_id: str, channel: str, body: PostRequest, request: Request,
                      background_tasks: BackgroundTasks, response: Response):
    """Create a new post in a channel. Resolves @mentions and fires webhooks.

    Noise quarantine, in order: test bots may only post to #sandbox (403),
    an identical body retried within POST_DEDUPE_WINDOW_SECONDS returns the
    first post with 200, and new posts are capped at
    POST_RATE_LIMIT_PER_MINUTE per member per hub per minute (429). Task-bound
    status, result, and failed posts have a tighter per-task and kind cap.
    """
    member = _get_member(request, hub_id)
    _enforce_claimed_member(member, body.member_id, body.member_name)
    _enforce_test_bot_channel(member, channel)
    from .types import (
        check_claim_like_note,
        validate_depends_on,
        validate_metadata,
        validate_post_content,
        validate_post_kind,
    )
    try:
        validate_post_content(body.content)
        validate_post_kind(body.kind)
        validate_metadata(body.metadata)
        validate_depends_on(body.depends_on)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    be = get_backend()
    existing = be.find_duplicate_post(
        hub_id, member.member_id, channel, body.content, body.kind, body.task_key,
        priority=body.priority, metadata=body.metadata, depends_on=body.depends_on,
        within_seconds=POST_DEDUPE_WINDOW_SECONDS,
    )
    if existing is not None:
        warning = check_claim_like_note(body.kind, body.content)
        if warning:
            existing.warning = warning
        response.status_code = 200
        return existing.to_dict()
    _enforce_post_rate_limit(hub_id, member.member_id, body.task_key, body.kind)
    try:
        post = await be.create_post(
            hub_id,
            member,
            channel,
            body.content,
            body.kind,
            body.task_key,
            metadata=body.metadata,
            priority=body.priority,
            depends_on=body.depends_on,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if post.mentions:
        background_tasks.add_task(_dispatch_webhooks, hub_id, post)
    return post.to_dict()


# ── Claim ───────────────────────────────────────────────────

@app.post("/api/{hub_id}/channels/{channel}/claim", status_code=201)
async def claim_task(hub_id: str, channel: str, body: ClaimRequest, request: Request):
    """Claim a task to prevent duplicate work. Returns 409 if already claimed."""
    member = _get_member(request, hub_id)
    _enforce_claimed_member(member, body.member_id, body.member_name)
    _enforce_test_bot_channel(member, channel)
    from .types import validate_depends_on, validate_post_content
    try:
        validate_post_content(body.content)
        validate_depends_on(body.depends_on)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    _enforce_post_rate_limit(hub_id, member.member_id, body.task_key, "claim")
    try:
        result = await get_backend().claim(
            hub_id, member, channel, body.task_key, body.content,
            depends_on=body.depends_on,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if result.conflict:
        raise HTTPException(409, result.to_dict())
    return result.to_dict()


# ── File Claims ────────────────────────────────────────────

class FileClaimRequest(_ClaimedIdentity):
    """Request to claim exclusive access to a file."""

    file_path: str
    content: str = ""

    @field_validator("file_path")
    @classmethod
    def _normalize_file_path(cls, value: str) -> str:
        raw = value.strip()
        if not raw or len(raw) > 512 or any(ord(c) < 32 for c in raw):
            raise ValueError("file_path must be a non-empty relative path without control characters")
        raw = raw.replace("\\", "/")
        if raw.startswith("/"):
            raise ValueError("file_path must be relative, not absolute")
        norm = posixpath.normpath(raw)
        if norm in (".", "..", "/") or norm.startswith("../") or norm.startswith("/"):
            raise ValueError("file_path must not escape the repository root or be '.'")
        return norm


@app.post("/api/{hub_id}/channels/{channel}/claim-file", status_code=201)
async def claim_file(hub_id: str, channel: str, body: FileClaimRequest, request: Request):
    """Claim a file to prevent two agents editing it simultaneously.
    Uses task_key = 'file:<path>' convention on the existing claim system."""
    member = _get_member(request, hub_id)
    _enforce_claimed_member(member, body.member_id, body.member_name)
    _enforce_test_bot_channel(member, channel)
    from .types import validate_post_content
    desc = body.content if body.content != "" else f"Editing {body.file_path}"
    try:
        validate_post_content(desc)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    task_key = f"file:{body.file_path}"
    _enforce_post_rate_limit(hub_id, member.member_id, task_key, "claim")
    try:
        result = await get_backend().claim(hub_id, member, channel, task_key, desc)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if result.conflict:
        raise HTTPException(409, result.to_dict())
    return result.to_dict()


@app.get("/api/{hub_id}/file-claims")
async def list_file_claims(hub_id: str, request: Request):
    """List all currently claimed files across all channels."""
    _get_member(request, hub_id)
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
    assigner = _get_member(request, hub_id)
    _enforce_claimed_member(assigner, body.member_id, body.member_name)
    _require_human(assigner)
    _enforce_test_bot_channel(assigner, channel)
    from .types import validate_depends_on, validate_post_content
    try:
        validate_post_content(body.content)
        validate_depends_on(body.depends_on)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    _enforce_post_rate_limit(hub_id, assigner.member_id, body.task_key, "assign")
    be = get_backend()
    try:
        result = await be.assign(hub_id, assigner, channel, body.task_key, body.assignee_id, body.content,
                                 depends_on=body.depends_on, priority=body.priority)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not result.claimed:
        if result.conflict:
            raise HTTPException(409, result.to_dict())
        raise HTTPException(400, result.message or "Assignment failed")

    # Fire webhook to assignee — post_id is the claim (what assignee acts on)
    assignee = be.get_member(hub_id, body.assignee_id)
    if assignee and assignee.webhook_url:
        from .types import Post
        notify_post = Post(
            post_id=result.post_id or "", content=body.content, kind="assign",
            channel=channel, member_id=assigner.member_id, member_name=assigner.member_name,
            member_type=assigner.member_type, task_key=body.task_key, status="open",
            metadata={"assigned_by": assigner.member_id, "assignee_id": body.assignee_id},
            mentions=[body.assignee_id],
        )
        background_tasks.add_task(_dispatch_webhooks, hub_id, notify_post)

    return result.to_dict()


# ── Report ──────────────────────────────────────────────────

@app.post("/api/{hub_id}/channels/{channel}/report", status_code=201)
async def report_result(hub_id: str, channel: str, body: ReportRequest, request: Request):
    """Report a task result and optionally get the next ready task.

    When `include_next: true`, the response includes a `next_task` field
    with the next assigned task whose dependencies are all met (or null
    if none). This eliminates the poll round-trip: report done → get next
    in one call. The agent loop becomes event-driven instead of timer-driven.
    """
    member = _get_member(request, hub_id)
    _enforce_claimed_member(member, body.member_id, body.member_name)
    _enforce_test_bot_channel(member, channel)
    from .types import validate_metadata, validate_post_content
    try:
        validate_post_content(body.content)
        validate_metadata(body.metadata)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    report_kind = "result" if body.status == "done" else "failed" if body.status == "failed" else body.status
    _enforce_post_rate_limit(hub_id, member.member_id, body.task_key, report_kind)
    try:
        post = await get_backend().report(
            hub_id, member, channel, body.task_key, body.status, body.content,
            affected_files=body.affected_files,
            metadata=body.metadata,
        )
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

    result = post.to_dict()

    if body.include_next:
        ready = await get_backend().get_ready_tasks(hub_id, member.member_id)
        result["next_task"] = None
        for task in ready:
            claimed = await get_backend().claim(
                hub_id, member, task.channel, task.task_key, "Accepting next assigned task",
                depends_on=task.depends_on,
            )
            if claimed.claimed or (claimed.existing_claim and claimed.existing_claim.member_id == member.member_id):
                result["next_task"] = task.to_dict()
                break

        # When the queue is empty and the caller asked for suggestions,
        # auto-generate task ideas so the agent doesn't go idle. This
        # addresses the "empty queue = orchestrator failure" feedback.
        if result["next_task"] is None and body.suggest_if_empty:
            suggest_resp = await suggest_tasks(hub_id, request)
            result["suggestions"] = suggest_resp.get("suggestions", [])

    return result


# ── Touch (keepalive) ──────────────────────────────────────

class TouchRequest(BaseModel):
    """Request to send a keepalive for an active claim."""

    task_key: str


@app.post("/api/{hub_id}/channels/{channel}/touch")
async def touch_claim(hub_id: str, channel: str, body: TouchRequest, request: Request):
    """Touch a claim to refresh its timestamp and prevent expiration."""
    member = _get_member(request, hub_id)
    task_key = body.task_key
    ok = await get_backend().touch_claim(hub_id, member.member_id, task_key)
    if not ok:
        raise HTTPException(404, f"No open claim for {task_key}")
    return {"touched": True, "task_key": task_key}


# ── Ping (lightweight notification badge) ──────────────────

@app.get("/api/{hub_id}/ping/{member_id}")
async def ping(hub_id: str, member_id: str, request: Request,
               since: Optional[str] = None, include: Optional[str] = None):
    """Cheapest possible check: anything new for me?

    Returns counts only — no post content, no parsing, no context switch.
    Agent glances at the badge. If all zeros, keep working. If non-zero,
    decide whether to interrupt.

    This replaces full board reads on poll ticks. The agent's flow is
    preserved because a zero-result ping costs nothing cognitively.

    Optional: pass `include=mine` to fold the agent's open task list
    into the same response. This collapses the common two-call pattern
    (ping → if action_needed then /mine) into a single round trip.
    The extra work is skipped entirely when include is None, so this
    does not regress the cheapest path.
    """
    _get_member(request, hub_id)
    be = get_backend()

    # Default: since last 15 minutes
    if not since:
        from datetime import datetime, timezone, timedelta
        since = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()

    # Count new posts since last check
    new_posts = be.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ? AND created_at > ?",
        (hub_id, since),
    ).fetchone()[0]

    # Count @mentions of this member (escape LIKE wildcards % and _)
    escaped_member_id = member_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    new_mentions = be.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ? AND created_at > ? AND mentions LIKE ? ESCAPE '\\'",
        (hub_id, since, f'%"{escaped_member_id}"%'),
    ).fetchone()[0]

    # Count assignments to this member. Uses the first-class assignee_id
    # column (added 2026-04-11) instead of json_extract on metadata, which
    # silently failed for any post that didn't store assignee_id in metadata.
    new_assigns = be.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ? AND created_at > ? AND kind = 'assign' "
        "AND assignee_id = ?",
        (hub_id, since, member_id),
    ).fetchone()[0]

    # _get_member already refreshed the caller's own last_seen. Writing it for
    # the URL's member_id would let anyone keep a dead member looking alive.

    response = {
        "new_posts": new_posts,
        "new_mentions": new_mentions,
        "new_assigns": new_assigns,
        "since": since,
        "action_needed": new_mentions > 0 or new_assigns > 0,
    }

    # Optional: fold the task list into the same response. Saves a
    # round trip when the agent is going to call /mine anyway.
    includes = {s.strip() for s in (include or "").split(",") if s.strip()}
    if "mine" in includes:
        tasks = await be.get_my_open_tasks(hub_id, member_id)
        response["mine"] = [p.to_dict() for p in tasks]
        response["mine_count"] = len(tasks)

    return response


@app.get("/api/{hub_id}/ready/{member_id}")
async def my_ready_tasks(hub_id: str, member_id: str, request: Request):
    """Return tasks assigned to this member where every dependency is done.

    This is /mine filtered by dependency readiness. Use it as the
    "what can I claim right now?" query instead of /mine when your
    workflow uses task dependencies. Short-circuits the "try to claim,
    get blocked, move on" loop into one call.
    """
    _get_member(request, hub_id)
    be = get_backend()
    posts = await be.get_ready_tasks(hub_id, member_id)
    return {
        "member_id": member_id,
        "count": len(posts),
        "tasks": [p.to_dict() for p in posts],
    }


@app.get("/api/{hub_id}/mine/{member_id}")
async def my_open_tasks(hub_id: str, member_id: str, request: Request):
    """Return open work assigned to this member.

    The single source of truth for "what's mine?" — replaces the antipattern
    of grepping channel posts to discover assignments. Includes:
    - Open claims the member created themselves
    - Assignment posts addressed to them via the assignee_id column

    Combine with /ping for the full notification badge → work-list flow.
    Use /ready instead if you want only the subset whose deps are done.
    """
    _get_member(request, hub_id)
    be = get_backend()
    posts = await be.get_my_open_tasks(hub_id, member_id)
    return {
        "member_id": member_id,
        "count": len(posts),
        "tasks": [p.to_dict() for p in posts],
    }


@app.get("/api/{hub_id}/handoff_trail/{task_key:path}")
async def handoff_trail(hub_id: str, task_key: str, request: Request, depth: int = 3):
    """Walk depends_on backward from task_key (BFS, capped).

    Powers `swarlo handoff`. Backend already implements the walk;
    this route was documented and CLI-wired but never mounted.
    """
    _get_member(request, hub_id)
    depth = max(1, min(int(depth), 10))
    trail = get_backend().walk_handoff_trail(hub_id, task_key, depth=depth)
    return {
        "task_key": task_key,
        "depth": depth,
        "count": len(trail),
        "trail": trail,
    }


# ── Idle Detection ─────────────────────────────────────────

@app.get("/api/{hub_id}/idle")
async def detect_idle(hub_id: str, request: Request, idle_minutes: int = 15):
    """Find agents that are alive but not producing.

    Uses the last_active column (added 2026-04-11) which is bumped only
    when an agent actually produces work (post/claim/report) — not on
    every auth touch like last_seen. This makes "idle" a server-native
    concept instead of an N+1 query that infers it from posts.

    An agent is:
    - alive   : last_seen   within 30 minutes (connected)
    - working : last_active within idle_minutes (producing) OR has open claim
    - idle    : alive but neither of the above
    """
    _get_member(request, hub_id)
    be = get_backend()
    idle_minutes = max(1, min(int(idle_minutes), MAX_WINDOW_MINUTES))

    now = datetime.now(timezone.utc)
    alive_cutoff = (now - timedelta(minutes=30)).isoformat()
    idle_cutoff = (now - timedelta(minutes=idle_minutes)).isoformat()
    open_claim_cutoff = _open_claim_cutoff(now)

    # Single query: alive agents with their last_active and any open claim.
    # LEFT JOIN folds the open-claim lookup into one round trip.
    rows = be.conn.execute(
        f"""
        SELECT m.member_id, m.member_name, m.last_active,
               (SELECT p.task_key FROM posts p
                WHERE p.hub_id = m.hub_id AND p.member_id = m.member_id
                  AND {_open_claim_predicate('p')}
                LIMIT 1) AS open_claim
        FROM members m
        WHERE m.hub_id = ? AND m.last_seen > ? AND m.member_type = 'agent'
        """,
        (open_claim_cutoff, hub_id, alive_cutoff),
    ).fetchall()

    idle, working = [], []
    for r in rows:
        entry = {
            "member_id": r["member_id"],
            "member_name": r["member_name"],
            "last_active": r["last_active"],
            "has_open_claim": r["open_claim"],
        }
        # Working = produced work recently OR holding an open claim
        if r["last_active"] and r["last_active"] > idle_cutoff:
            working.append(entry)
        elif r["open_claim"]:
            working.append(entry)
        else:
            idle.append(entry)

    return {
        "idle": idle,
        "working": working,
        "idle_count": len(idle),
        "working_count": len(working),
        "nudge": f"{len(idle)} agent(s) idle. Post new tasks or ping them." if idle else "All agents producing.",
    }


# ── Latent Briefing (task-guided context filtering) ────────

class BriefingRequest(BaseModel):
    """Request for task-guided context filtering from the board."""

    task: str  # what the agent is about to work on
    limit: int = 15
    scorer: str = "tfidf"  # "tfidf" (default, Phase 2) or "regex" (v1 baseline)


@app.post("/api/{hub_id}/briefing")
async def get_briefing(hub_id: str, body: BriefingRequest, request: Request):
    """Return board posts ranked by relevance to a task description.

    The task prompt determines what's relevant from shared context — a
    text-level analog of KV-cache compaction from Latent Briefing
    (Geist 2026). Two scorers share the endpoint:

    - `tfidf` (default) — TF-IDF cosine similarity with light stemming.
      Handles synonyms and suffix variation ("dropping" ≈ "dropped").
    - `regex` — original keyword/file-path overlap. Kept for A/B and
      for tasks that name exact file paths.

    Phase 3 will add `attention` (compaction against a local model)
    once Sovereign is back online.
    """
    from . import _briefing
    import re as _re
    member = _get_member(request, hub_id)
    be = get_backend()

    # Reject unknown scorers instead of silently falling back to tfidf.
    # _briefing.score() defaults any unrecognized name to v2_tfidf, but the
    # response echoes body.scorer verbatim — so a typo ("tfdif") or a not-yet-
    # implemented scorer ("attention") would return tfidf results while the
    # response claims a scorer that never ran. Fail fast with the valid set.
    if body.scorer not in _briefing.SCORERS:
        raise HTTPException(
            400,
            f"Unknown scorer '{body.scorer}'. Valid scorers: "
            f"{', '.join(sorted(_briefing.SCORERS))}",
        )

    # Diagnostic extracts — kept in response for debugging/observability.
    file_pats = _re.findall(r'[\w./]+\.(?:py|ts|js|md|json|yaml|sql)\b', body.task)
    dir_pats = _re.findall(r'(?:backend|atris|swarlo|scripts|tests)/[\w/]+', body.task)
    extracted_paths = sorted(set(file_pats + dir_pats))
    raw_words = {w.lower() for w in _re.findall(r'\b\w{3,}\b', body.task)}
    extracted_keywords = sorted(raw_words - _briefing._STOPWORDS)[:20]

    rows = be.conn.execute(
        "SELECT * FROM posts WHERE hub_id = ? AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT 200",
        (hub_id, _utc_cutoff(BRIEFING_LOOKBACK_DAYS)),
    ).fetchall()
    if not rows:
        return {
            "task": body.task,
            "scorer": body.scorer,
            "extracted_paths": extracted_paths,
            "extracted_keywords": extracted_keywords,
            "count": 0,
            "posts": [],
        }

    candidates = [(r["content"] or "") for r in rows]
    scores = _briefing.score(body.task, candidates, scorer=body.scorer)

    # Metadata file-overlap bonus is scorer-agnostic — files explicitly
    # tracked on a post are still a strong positive signal.
    task_paths = set(extracted_paths)
    if task_paths:
        for i, r in enumerate(rows):
            if r["metadata"]:
                try:
                    meta = json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"]
                    meta_files = meta.get("affected_files", []) or []
                    hits = sum(1 for fp in task_paths if any(fp in mf for mf in meta_files))
                    if hits:
                        scores[i] = scores[i] + 0.3 * hits  # small additive bump
                except Exception:
                    pass

    # Keep non-zero scores, sort descending, take top N.
    # Floor the slice at 1: body.limit is unconstrained, and a negative value
    # makes scored[:body.limit] a from-the-end slice (e.g. -5 drops the 5
    # LOWEST-scored and returns everything else) — the opposite of "top N".
    scored = [(s, r) for s, r in zip(scores, rows) if s > 0]
    scored.sort(key=lambda x: -x[0])
    top = scored[: max(1, body.limit)]

    return {
        "task": body.task,
        "scorer": body.scorer,
        "extracted_paths": extracted_paths,
        "extracted_keywords": extracted_keywords,
        "count": len(top),
        "posts": [
            {
                "score": round(s, 4),
                "member_name": r["member_name"],
                "kind": r["kind"],
                "task_key": r["task_key"],
                "content": (r["content"] or "")[:300],
                "channel": r["channel"],
                "created_at": r["created_at"],
            }
            for s, r in top
        ],
    }


# ── Liveness Check ─────────────────────────────────────────

@app.get("/api/{hub_id}/liveness")
async def check_liveness(hub_id: str, request: Request, stale_minutes: int = 30,
                          auto_expire: bool = True):
    """Check which agents are alive, dying, or dead.

    Returns actionable lists so the orchestrator can ping or reassign.
    This is the RLHF signal — agents that go dark get detected, not ignored.

    Side effect: when auto_expire is True (default), expires any claim
    whose heartbeat is older than stale_minutes. This makes /liveness a
    passive cleanup sweep — the orchestrator's health check doubles as
    garbage collection for dead-agent claims. Pass auto_expire=false if
    you only want to observe without cleaning up.
    """
    _get_member(request, hub_id)
    be = get_backend()
    stale_minutes = int(stale_minutes)
    if stale_minutes < 0:
        stale_minutes = 1
    elif stale_minutes > MAX_WINDOW_MINUTES:
        stale_minutes = MAX_WINDOW_MINUTES

    # Passive cleanup: expire stale claims before computing orphans. This
    # way the orphaned_claims list reflects what's *still* stuck after
    # the expiry ran, not what was stale a second ago.
    expired_keys: list[str] = []
    if auto_expire:
        expired_keys = await be.force_expire_claims(
            hub_id, stale_minutes=min(MAX_WINDOW_MINUTES, max(OPEN_CLAIM_STALE_MINUTES, stale_minutes)),
        )

    now = datetime.now(timezone.utc)
    cutoff_dying = (now - timedelta(minutes=stale_minutes)).isoformat()
    cutoff_dead = (now - timedelta(minutes=stale_minutes * 3)).isoformat()

    rows = be.conn.execute(
        "SELECT member_id, member_name, member_type, last_seen, last_active FROM members WHERE hub_id = ?",
        (hub_id,),
    ).fetchall()

    alive, dying, dead = [], [], []
    for r in rows:
        ls = r["last_seen"]
        # last_seen = connected, last_active = producing work. Surface both
        # so consumers can tell "alive but silent" from "alive and shipping".
        entry = {"member_id": r["member_id"], "member_name": r["member_name"],
                 "member_type": r["member_type"], "last_seen": ls,
                 "last_active": r["last_active"]}
        if not ls:
            dead.append(entry)
        elif ls < cutoff_dead:
            dead.append(entry)
        elif ls < cutoff_dying:
            dying.append(entry)
        else:
            alive.append(entry)

    # Find orphaned claims from dead/dying agents (after auto-expire cleanup)
    orphaned_claims = []
    dead_ids = {d["member_id"] for d in dead + dying}
    if dead_ids:
        claims = await be.get_open_claims(hub_id)
        orphaned_claims = [
            {"task_key": c.task_key, "member_id": c.member_id, "member_name": c.member_name}
            for c in claims if c.member_id in dead_ids
        ]

    return {
        "alive": alive,
        "dying": dying,
        "dead": dead,
        "orphaned_claims": orphaned_claims,
        "expired_on_sweep": expired_keys,
        "recommendation": (
            f"Ping {len(dying)} dying agent(s). Reassign {len(orphaned_claims)} orphaned claim(s)."
            if dying or orphaned_claims
            else "All agents healthy."
        ),
    }


# ── Force-expire stale claims ─────────────────────────────

@app.post("/api/{hub_id}/claims/expire")
async def expire_stale_claims(hub_id: str, request: Request):
    """Force-expire claims that have been stale beyond the threshold."""
    _get_member(request, hub_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_stale = body.get("stale_minutes", 30) if isinstance(body, dict) else 30
    # Cleanup cannot shorten the normal claim heartbeat budget.
    try:
        stale_minutes = max(OPEN_CLAIM_STALE_MINUTES, min(int(raw_stale), MAX_WINDOW_MINUTES))
    except (TypeError, ValueError):
        raise HTTPException(400, "stale_minutes must be an integer")
    expired = await get_backend().force_expire_claims(hub_id, stale_minutes=stale_minutes)
    return {"expired": expired, "count": len(expired)}


# ── Retry failed tasks ─────────────────────────────────────

@app.post("/api/{hub_id}/claims/retry")
async def retry_failed_tasks(hub_id: str, request: Request):
    """Re-open failed tasks for retry up to max_retries limit."""
    _get_member(request, hub_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_max = body.get("max_retries", 3) if isinstance(body, dict) else 3
    # Coerce to a non-negative int. A non-numeric value is especially
    # dangerous here: the backend filters `retry_count < max_retries`, and
    # SQLite orders every INTEGER before any TEXT, so a string max_retries
    # makes that predicate true for *every* failed task — silently bypassing
    # the retry cap and re-queuing poison tasks that already exhausted it.
    try:
        max_retries = max(0, int(raw_max))
    except (TypeError, ValueError):
        raise HTTPException(400, "max_retries must be a non-negative integer")
    retried = await get_backend().retry_failed(hub_id, max_retries=max_retries)
    return {"retried": retried, "count": len(retried)}


# ── Claims ──────────────────────────────────────────────────

@app.get("/api/{hub_id}/claims")
async def list_claims(hub_id: str, request: Request, channel: Optional[str] = None):
    """List all open claims in the hub, optionally filtered by channel."""
    _get_member(request, hub_id)
    claims = await get_backend().get_open_claims(hub_id, channel=channel)
    return {"count": len(claims), "claims": [c.to_dict() for c in claims]}


# ── Replay (catch-up for late-joining agents) ──────────────

@app.get("/api/{hub_id}/replay")
async def replay_posts(
    hub_id: str,
    request: Request,
    since: str,
    channel: Optional[str] = None,
    limit: int = 200,
):
    """Return posts created after `since` timestamp.

    Lets a late-joining agent catch up on what it missed without
    fetching the full board history. `since` is an ISO8601 timestamp
    (e.g. 2026-04-10T22:00:00+00:00).

    Returns posts in chronological order (oldest first), capped at `limit`.
    """
    _get_member(request, hub_id)
    if not since or not since.strip():
        raise HTTPException(400, "since query param is required (ISO8601 timestamp)")
    try:
        datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            400,
            f"since is not a valid ISO8601 timestamp: {since!r} "
            "(e.g. 2026-07-10T00:00:00+00:00)",
        )

    safe_limit = max(1, min(int(limit), 500))
    query_since = _bounded_replay_since(since)
    be = get_backend()
    if channel:
        rows = be.conn.execute(
            "SELECT * FROM posts WHERE hub_id = ? AND channel = ? AND created_at > ? "
            "ORDER BY created_at ASC LIMIT ?",
            (hub_id, channel, query_since, safe_limit),
        ).fetchall()
    else:
        rows = be.conn.execute(
            "SELECT * FROM posts WHERE hub_id = ? AND created_at > ? "
            "ORDER BY created_at ASC LIMIT ?",
            (hub_id, query_since, safe_limit),
        ).fetchall()

    posts = [be._row_to_post(r).to_dict() for r in rows]
    return {
        "since": since,
        "channel": channel,
        "count": len(posts),
        "limit": safe_limit,
        "posts": posts,
    }


# ── Summary ────────────────────────────────────────────────

@app.get("/api/{hub_id}/summary")
async def get_summary(hub_id: str, request: Request, limit: int = 10):
    """Get a personalized summary of recent activity for the authenticated member."""
    member = _get_member(request, hub_id)
    text = await get_backend().summarize_for_member(hub_id, member.member_id, limit=max(1, min(limit, 50)))
    return {"summary": text}


# ── Replies ─────────────────────────────────────────────────

@app.get("/api/{hub_id}/posts/{post_id}/replies")
async def list_replies(hub_id: str, post_id: str, request: Request):
    """List all replies to a specific post."""
    _get_member(request, hub_id)
    replies = await get_backend().get_replies(hub_id, post_id)
    return {
        "post_id": post_id,
        "count": len(replies),
        "replies": [r.to_dict() for r in replies],
    }


@app.post("/api/{hub_id}/posts/{post_id}/replies", status_code=201)
async def create_reply(hub_id: str, post_id: str, body: ReplyRequest, request: Request):
    """Create a reply to an existing post."""
    member = _get_member(request, hub_id)
    _enforce_claimed_member(member, body.member_id, body.member_name)
    from .types import validate_post_content
    try:
        validate_post_content(body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    be = get_backend()
    parent = be.conn.execute(
        "SELECT channel FROM posts WHERE hub_id = ? AND post_id = ?",
        (hub_id, post_id),
    ).fetchone()
    if not parent:
        raise HTTPException(404, f"Post {post_id} not found in hub {hub_id}")
    _enforce_test_bot_channel(member, parent["channel"])
    _enforce_post_rate_limit(hub_id, member.member_id, None, "reply")
    try:
        reply = await be.reply(hub_id, member, post_id, body.content)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return reply.to_dict()


# ── Webhooks ───────────────────────────────────────────────

_LOCALHOST = frozenset({"localhost", "127.0.0.1", "::1"})
_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_server_bind_host: str = "127.0.0.1"


def set_bind_host(host: str) -> None:
    """Record the server bind host to gate default localhost webhook allowance."""
    global _server_bind_host
    _server_bind_host = (host or "").strip().lower()


def _allow_localhost_webhooks() -> bool:
    env = os.environ.get("SWARLO_ALLOW_LOCALHOST_WEBHOOKS")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return _server_bind_host in _LOOPBACK_BIND_HOSTS


def _is_global_ip(raw_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _classify_webhook_url(url: str) -> tuple[str, str | None, int | None]:
    """Validate URL syntax, port, scheme, and literal IPs.

    Returns ("allow", None, None), ("deny", None, None), or ("resolve", hostname, port).
    """
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        return ("deny", None, None)
    try:
        parsed = urlparse(url)
    except Exception:
        return ("deny", None, None)

    if not parsed.scheme or not parsed.hostname:
        return ("deny", None, None)
    if parsed.scheme not in ("http", "https"):
        return ("deny", None, None)

    try:
        port = parsed.port
    except ValueError:
        return ("deny", None, None)
    if port is not None and not (1 <= port <= 65535):
        return ("deny", None, None)

    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname:
        return ("deny", None, None)

    if hostname in _LOCALHOST:
        return ("allow", None, None) if _allow_localhost_webhooks() else ("deny", None, None)

    # External webhooks must use HTTPS.
    if parsed.scheme != "https":
        return ("deny", None, None)

    # If hostname is an IP literal, check directly without DNS.
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return ("resolve", hostname, port or 443)
    return ("allow", None, None) if _is_global_ip(hostname) else ("deny", None, None)


def _resolved_addrs_are_global(resolved) -> bool:
    if not resolved:
        return False
    for _, _, _, _, addr in resolved:
        if not addr or not _is_global_ip(addr[0]):
            return False
    return True


def _is_safe_webhook_url(url: str, at_dispatch: bool = False) -> bool:
    """Validate webhook URL against SSRF.

    Rules:
    - Invalid ports/schemes are always rejected (at registration and dispatch).
    - Localhost is allowed only when SWARLO_ALLOW_LOCALHOST_WEBHOOKS is enabled
      (defaults to enabled only for loopback/testclient binds, disabled otherwise).
    - External: must be HTTPS and resolve exclusively to globally-routable IPs.
    - at_dispatch=True: DNS must resolve (blocks rebinding attacks).
    """
    action, hostname, port = _classify_webhook_url(url)
    if action == "allow":
        return True
    if action == "deny":
        return False
    try:
        resolved = socket.getaddrinfo(hostname, port)
        return _resolved_addrs_are_global(resolved)
    except socket.gaierror:
        return not at_dispatch
    except (ValueError, OSError):
        return False


async def _is_safe_webhook_url_async(url: str, at_dispatch: bool = False) -> bool:
    """Async SSRF check that offloads DNS resolution via asyncio.to_thread."""
    action, hostname, port = _classify_webhook_url(url)
    if action == "allow":
        return True
    if action == "deny":
        return False
    try:
        resolved = await asyncio.to_thread(socket.getaddrinfo, hostname, port)
        return _resolved_addrs_are_global(resolved)
    except socket.gaierror:
        return not at_dispatch
    except (ValueError, OSError):
        return False


async def _dispatch_webhooks(hub_id: str, post):
    """Fire webhook callbacks for @mentioned members. Best-effort, no retries."""
    backend = get_backend()
    members = backend.get_members_by_ids(hub_id, post.mentions)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for m in members:
            if not m.webhook_url:
                continue
            if not await _is_safe_webhook_url_async(m.webhook_url, at_dispatch=True):
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


# ── Auto-suggest (self-feeding task queue) ─────────────────

@app.post("/api/{hub_id}/suggest")
async def suggest_tasks(hub_id: str, request: Request):
    """Analyze board state and suggest next tasks.

    Looks at: what shipped recently, what channels are quiet, what agents
    are idle, what task patterns recur. Returns suggested task descriptions
    the orchestrator can post directly.

    This is the RL self-feeding loop: when the queue is empty, the system
    generates its own work based on what it observes.
    """
    _get_member(request, hub_id)
    be = get_backend()
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    suggestions = []

    # 1. Find idle agents
    alive_cutoff = (now - timedelta(minutes=30)).isoformat()
    idle_cutoff = (now - timedelta(minutes=15)).isoformat()
    open_claim_cutoff = _open_claim_cutoff(now)
    idle_rows = be.conn.execute(
        f"""
        WITH alive AS (
            SELECT member_id, member_name
            FROM members
            WHERE hub_id = ? AND last_seen > ? AND member_type = 'agent'
        ),
        last_posts AS (
            SELECT p.member_id, MAX(p.created_at) AS last_post_at
            FROM posts p
            JOIN alive a ON a.member_id = p.member_id
            WHERE p.hub_id = ?
            GROUP BY p.member_id
        ),
        open_claims AS (
            SELECT DISTINCT p.member_id
            FROM posts p
            JOIN alive a ON a.member_id = p.member_id
            WHERE p.hub_id = ? AND {_open_claim_predicate('p')}
        )
        SELECT a.member_name
        FROM alive a
        LEFT JOIN last_posts lp ON lp.member_id = a.member_id
        LEFT JOIN open_claims oc ON oc.member_id = a.member_id
        WHERE oc.member_id IS NULL
          AND (lp.last_post_at IS NULL OR lp.last_post_at < ?)
        ORDER BY a.member_name
        """,
        (hub_id, alive_cutoff, hub_id, hub_id, open_claim_cutoff, idle_cutoff),
    ).fetchall()
    idle_agents = [row["member_name"] for row in idle_rows]

    # 2. Find channels with no recent activity
    quiet_channels = []
    for ch in ["general", "experiments", "ops", "outreach"]:
        latest = be.conn.execute(
            "SELECT created_at FROM posts WHERE hub_id = ? AND channel = ? ORDER BY created_at DESC LIMIT 1",
            (hub_id, ch),
        ).fetchone()
        if not latest or latest["created_at"] < (now - timedelta(hours=1)).isoformat():
            quiet_channels.append(ch)

    # 3. Find patterns in completed work — what kind of tasks ship successfully?
    recent_results = be.conn.execute(
        "SELECT content, task_key FROM posts WHERE hub_id = ? AND kind = 'result' AND status = 'done' ORDER BY created_at DESC LIMIT 20",
        (hub_id,),
    ).fetchall()
    shipped_keywords = {}
    for r in recent_results:
        for word in (r["content"] or "").lower().split():
            if len(word) > 4 and word.isalpha():
                shipped_keywords[word] = shipped_keywords.get(word, 0) + 1
    top_themes = sorted(shipped_keywords.items(), key=lambda x: -x[1])[:5]

    # 4. Find failed tasks that could be retried with different approach
    recent_failures = be.conn.execute(
        "SELECT task_key, content FROM posts WHERE hub_id = ? AND kind = 'failed' AND status = 'failed' ORDER BY created_at DESC LIMIT 5",
        (hub_id,),
    ).fetchall()

    # Build suggestions
    if idle_agents:
        suggestions.append({
            "reason": f"{len(idle_agents)} agent(s) idle: {', '.join(idle_agents)}",
            "suggestion": "Post tasks for idle agents or ping them with specific assignments",
        })

    if quiet_channels:
        for ch in quiet_channels:
            suggestions.append({
                "reason": f"#{ch} has been quiet for 1+ hour",
                "suggestion": f"Seed a task in #{ch} to reactivate that lane",
            })

    if recent_failures:
        for f in recent_failures[:2]:
            suggestions.append({
                "reason": f"Task {f['task_key']} failed",
                "suggestion": f"Retry with different approach: {f['content'][:100]}",
            })

    if top_themes:
        theme_str = ", ".join(t[0] for t in top_themes[:3])
        suggestions.append({
            "reason": f"Recent shipped work clusters around: {theme_str}",
            "suggestion": f"Look for more work in these areas — momentum is here",
        })

    if not suggestions:
        suggestions.append({
            "reason": "Board is healthy — all agents working, all channels active",
            "suggestion": "No action needed. Check again next tick.",
        })

    return {
        "idle_agents": idle_agents,
        "quiet_channels": quiet_channels,
        "top_themes": [{"word": t[0], "count": t[1]} for t in top_themes],
        "recent_failures": [{"task_key": f["task_key"], "content": f["content"][:100]} for f in recent_failures],
        "suggestions": suggestions,
        "suggestion_count": len(suggestions),
    }


# ── Members ────────────────────────────────────────────────

@app.get("/api/{hub_id}/members")
async def list_members(hub_id: str, request: Request):
    """List all registered members in the hub."""
    _get_member(request, hub_id)
    rows = get_backend().conn.execute(
        "SELECT member_id, member_name, member_type, created_at, last_seen FROM members WHERE hub_id = ?",
        (hub_id,),
    ).fetchall()
    return {"count": len(rows), "members": [dict(r) for r in rows]}


@app.delete("/api/{hub_id}/members/{member_id}")
async def delete_member(hub_id: str, member_id: str, request: Request):
    """Remove a member from the hub."""
    caller = _get_member(request, hub_id)
    if caller.member_id != member_id:
        _require_human(caller)
    if not get_backend().delete_member(hub_id, member_id):
        raise HTTPException(404, f"Member {member_id} not found")
    return {"deleted": member_id}


@app.post("/api/{hub_id}/prune")
async def prune_members(hub_id: str, request: Request):
    """Remove members not seen in `stale_minutes` (default 60)."""
    caller = _get_member(request, hub_id)
    _require_human(caller)
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_stale = body.get("stale_minutes", 60) if isinstance(body, dict) else 60
    # A non-numeric value would otherwise 500. The backend clamps to >= 1.
    try:
        stale_minutes = max(1, min(int(raw_stale), MAX_WINDOW_MINUTES))
    except (TypeError, ValueError):
        raise HTTPException(400, "stale_minutes must be an integer >= 1")
    pruned = get_backend().prune_members(hub_id, stale_minutes)
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
    _get_member(request, hub_id)  # Auth check
    be = get_backend()
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    open_claim_cutoff = _open_claim_cutoff(now)

    # Active agents (seen in last 60 minutes)
    cutoff = (now - timedelta(minutes=60)).isoformat()
    agents_active = be.conn.execute(
        "SELECT COUNT(*) FROM members WHERE hub_id = ? AND last_seen > ? AND member_type = 'agent'",
        (hub_id, cutoff),
    ).fetchone()[0]

    # Open claims
    tasks_claimed = be.conn.execute(
        f"SELECT COUNT(*) FROM posts p WHERE p.hub_id = ? AND {_open_claim_predicate('p')}",
        (hub_id, open_claim_cutoff),
    ).fetchone()[0]

    # Completed results (authoritative status required; freeform kind='result' with status=NULL ignored)
    tasks_shipped = be.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ? AND kind = 'result' AND status = 'done'",
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
          AND c.status IS NOT NULL
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

    # Conflict detection: find file claims that were contested (409'd)
    # and edits to same file by different agents within 30 min
    file_conflicts = be.conn.execute(
        f"""
        SELECT COUNT(DISTINCT task_key) FROM posts p
        WHERE p.hub_id = ? AND p.task_key LIKE 'file:%'
        AND {_open_claim_predicate('p')}
        """,
        (hub_id, open_claim_cutoff),
    ).fetchone()[0]

    # Detect revert patterns: same file edited by 2+ different agents
    revert_risk = be.conn.execute(
        """
        SELECT task_key, COUNT(DISTINCT member_id) as editors
        FROM posts
        WHERE hub_id = ? AND task_key LIKE 'file:%' AND kind = 'claim' AND status IS NOT NULL
        GROUP BY task_key
        HAVING editors > 1
        """,
        (hub_id,),
    ).fetchall()
    files_with_multi_editors = len(revert_risk)

    # Coordination score: higher is better
    # +10 per shipped task, +5 per active agent, -20 per multi-editor file, -5 per unclaimed task
    unclaimed_tasks = be.conn.execute(
        "SELECT COUNT(DISTINCT task_key) FROM posts WHERE hub_id = ? AND kind = 'message' AND task_key IS NOT NULL "
        "AND task_key NOT IN (SELECT task_key FROM posts WHERE hub_id = ? AND kind = 'claim' AND status IS NOT NULL AND task_key IS NOT NULL)",
        (hub_id, hub_id),
    ).fetchone()[0]

    coord_score = (tasks_shipped * 10) + (agents_active * 5) - (files_with_multi_editors * 20) - (unclaimed_tasks * 5)

    now = datetime.now(timezone.utc).isoformat()

    # Store score in SQLite for history. The scores table is created in
    # the SCHEMA block at startup (sqlite_backend.py) — no need to
    # CREATE TABLE IF NOT EXISTS on every request.
    try:
        with be._write_lock:
            be.conn.execute(
                "INSERT INTO scores (hub_id, agents_active, tasks_claimed, tasks_shipped, avg_time_to_claim, file_conflicts, files_with_multi_editors, coord_score, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (hub_id, agents_active, tasks_claimed, tasks_shipped, avg_time_to_claim, file_conflicts, files_with_multi_editors, coord_score, now),
            )
            be.conn.commit()
    except Exception:
        pass  # Non-critical

    per_agent_xp = _compute_per_agent_xp(be, hub_id)

    return {
        "hub_id": hub_id,
        "agents_active": agents_active,
        "tasks_claimed": tasks_claimed,
        "tasks_shipped": tasks_shipped,
        "avg_time_to_claim": avg_time_to_claim,
        "file_conflicts": file_conflicts,
        "files_with_multi_editors": files_with_multi_editors,
        "unclaimed_tasks": unclaimed_tasks,
        "coord_score": coord_score,
        "computed_at": now,
        "per_agent_xp": per_agent_xp[:20],
    }


def _compute_per_agent_xp(be: SQLiteBackend, hub_id: str) -> list[dict]:
    """XP ledger matching CLI mechanics: claim +2, done +10, failed -3, blocked -1.

    file: task keys are excluded. Order is XP desc, then member_id.
    Only authoritative claims/reports (status IS NOT NULL) award or deduct XP.
    """
    events = be.conn.execute(
        "SELECT member_id, member_name, kind, status, task_key FROM posts WHERE hub_id = ?",
        (hub_id,),
    ).fetchall()
    by_member: dict[str, dict] = {}
    for event in events:
        task_key = event["task_key"]
        if task_key and str(task_key).startswith("file:"):
            continue
        kind = event["kind"]
        status = event["status"]
        if status is None:
            continue
        member_id = event["member_id"] or "unknown"
        row = by_member.setdefault(
            member_id,
            {
                "member_id": member_id,
                "member_name": event["member_name"] or member_id,
                "xp": 0,
                "shipped": 0,
                "claims": 0,
                "failed": 0,
                "blocked": 0,
            },
        )
        if kind == "claim" and status != "retracted":
            row["xp"] += 2
            row["claims"] += 1
        elif kind != "claim" and status == "blocked":
            row["xp"] -= 1
            row["blocked"] += 1
        elif kind != "claim" and status == "failed":
            row["xp"] -= 3
            row["failed"] += 1
        elif kind != "claim" and status == "done":
            row["xp"] += 10
            row["shipped"] += 1
    return sorted(
        by_member.values(),
        key=lambda r: (-int(r["xp"]), str(r["member_id"])),
    )


def _list_unclaimed_tasks(
    be: SQLiteBackend,
    hub_id: str,
    *,
    limit: int = 20,
    channel: str | None = None,
) -> list[dict]:
    """Message tasks with no open claim and no terminal report/status."""
    open_claim_keys = {
        row["task_key"]
        for row in be.conn.execute(
            "SELECT task_key FROM posts WHERE hub_id = ? AND kind = 'claim' "
            "AND status = 'open' AND task_key IS NOT NULL AND task_key NOT LIKE 'file:%'",
            (hub_id,),
        ).fetchall()
        if row["task_key"]
    }
    terminal_keys = {
        row["task_key"]
        for row in be.conn.execute(
            "SELECT DISTINCT task_key FROM posts WHERE hub_id = ? AND task_key IS NOT NULL "
            "AND task_key NOT LIKE 'file:%' AND status IN ('done', 'failed', 'blocked')",
            (hub_id,),
        ).fetchall()
        if row["task_key"]
    }
    params: list = [hub_id]
    channel_sql = ""
    if channel:
        channel_sql = "AND channel = ? "
        params.append(channel)
    rows = be.conn.execute(
        "SELECT channel, task_key, content, created_at, member_name FROM posts "
        f"WHERE hub_id = ? AND kind = 'message' AND task_key IS NOT NULL "
        f"AND task_key NOT LIKE 'file:%' {channel_sql}"
        "ORDER BY created_at DESC",
        params,
    ).fetchall()
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        task_key = row["task_key"]
        if not task_key or task_key in seen:
            continue
        if task_key in open_claim_keys or task_key in terminal_keys:
            continue
        seen.add(task_key)
        out.append(
            {
                "channel": row["channel"],
                "task_key": task_key,
                "content": row["content"],
                "created_at": row["created_at"],
                "member_name": row["member_name"],
            }
        )
        if len(out) >= limit:
            break
    return out


@app.get("/api/{hub_id}/unclaimed")
async def list_unclaimed(
    hub_id: str,
    request: Request,
    limit: int = 20,
    channel: Optional[str] = None,
):
    """List message tasks that still need an owner."""
    _get_member(request, hub_id)
    limit = max(1, min(int(limit), 500))
    tasks = _list_unclaimed_tasks(
        get_backend(), hub_id, limit=limit, channel=channel or None
    )
    return {"count": len(tasks), "tasks": tasks}


@app.get("/api/{hub_id}/xp")
async def list_xp(
    hub_id: str,
    request: Request,
    limit: int = 20,
    member_id: Optional[str] = None,
):
    """Per-agent XP leaderboard (same mechanics as CLI `swarlo xp`)."""
    _get_member(request, hub_id)
    limit = max(1, min(int(limit), 500))
    rows = _compute_per_agent_xp(get_backend(), hub_id)
    if member_id:
        rows = [r for r in rows if r["member_id"] == member_id]
    rows = rows[:limit]
    return {"count": len(rows), "per_agent_xp": rows}


@app.get("/api/{hub_id}/scores")
async def list_scores(hub_id: str, request: Request, limit: int = 10):
    """Persisted coordination score history for `swarlo score-history`."""
    _get_member(request, hub_id)
    limit = max(1, min(int(limit), 500))
    be = get_backend()
    rows = be.conn.execute(
        "SELECT hub_id, agents_active, tasks_claimed, tasks_shipped, "
        "avg_time_to_claim, file_conflicts, files_with_multi_editors, "
        "coord_score, throughput_per_hour, mttr_seconds, rework_rate, "
        "idle_ratio, tasks_failed, tasks_blocked, computed_at "
        "FROM scores WHERE hub_id = ? ORDER BY computed_at DESC, id DESC LIMIT ?",
        (hub_id, limit),
    ).fetchall()
    scores = [dict(row) for row in rows]
    return {"count": len(scores), "scores": scores}


# ── Git DAG ─────────────────────────────────────────────────

@app.post("/api/{hub_id}/git/push", status_code=201)
async def git_push(hub_id: str, request: Request):
    """Push a git bundle to the hub's DAG store."""
    member = _get_member(request, hub_id)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_len = int(content_length)
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length header")
        if parsed_len < 0:
            raise HTTPException(400, "Invalid Content-Length header")
        if parsed_len > MAX_GIT_BUNDLE_BYTES:
            raise HTTPException(413, "Bundle too large (max 50MB)")

    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_GIT_BUNDLE_BYTES:
            raise HTTPException(413, "Bundle too large (max 50MB)")
        chunks.extend(chunk)
    body = bytes(chunks)

    dag = await asyncio.to_thread(get_dag, hub_id)
    try:
        hashes = await dag.unbundle(body)
    except (ValueError, subprocess.CalledProcessError):
        raise HTTPException(400, "Bundle could not be imported into this hub")
    # A forged header can name a commit the bundle never carried.
    for h in hashes:
        if not await asyncio.to_thread(dag.commit_exists, h):
            raise HTTPException(400, "Bundle names a commit it does not contain")

    # Index each commit in SQLite
    backend = get_backend()
    for h in hashes:
        existing = backend.get_commit(hub_id, h)
        if existing:
            continue
        parent_hash, message = await asyncio.to_thread(dag.get_commit_info, h)
        backend.index_commit(hub_id, h, parent_hash, member.member_id, member.member_name, message)

    return {"hashes": hashes}


@app.get("/api/{hub_id}/git/fetch/{hash}")
async def git_fetch(hub_id: str, hash: str, request: Request):
    """Fetch a commit bundle by hash from the DAG store."""
    _get_member(request, hub_id)
    dag = await asyncio.to_thread(get_dag, hub_id)
    if not await asyncio.to_thread(dag.commit_exists, hash):
        raise HTTPException(404, "Commit not found")
    from fastapi.responses import Response
    bundle_bytes = await asyncio.to_thread(dag.create_bundle, hash)
    return Response(content=bundle_bytes, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename={hash[:12]}.bundle"})


@app.get("/api/{hub_id}/git/commits")
async def git_list_commits(hub_id: str, request: Request, member_filter: Optional[str] = None, limit: int = 50):
    """List commits in the hub, optionally filtered by member."""
    _get_member(request, hub_id)
    return get_backend().list_commits(hub_id, member_id=member_filter, limit=max(1, min(limit, 200)))


@app.get("/api/{hub_id}/git/commits/{hash}")
async def git_get_commit(hub_id: str, hash: str, request: Request):
    """Get metadata for a specific commit by hash."""
    _get_member(request, hub_id)
    commit = get_backend().get_commit(hub_id, hash)
    if not commit:
        raise HTTPException(404, "Commit not found")
    return commit


@app.get("/api/{hub_id}/git/commits/{hash}/children")
async def git_children(hub_id: str, hash: str, request: Request):
    """Get all child commits of a given commit hash."""
    _get_member(request, hub_id)
    return get_backend().get_children(hub_id, hash)


@app.get("/api/{hub_id}/git/leaves")
async def git_leaves(hub_id: str, request: Request):
    """Get all leaf commits (commits with no children) in the DAG."""
    _get_member(request, hub_id)
    return get_backend().get_leaves(hub_id)


@app.get("/api/{hub_id}/git/commits/{hash}/lineage")
async def git_lineage(hub_id: str, hash: str, request: Request):
    """Get the full ancestor chain from a commit back to root."""
    _get_member(request, hub_id)
    return get_backend().get_lineage(hub_id, hash)


@app.get("/api/{hub_id}/git/diff/{hash_a}/{hash_b}")
async def git_diff(hub_id: str, hash_a: str, hash_b: str, request: Request):
    """Get the diff between two commits as plain text."""
    _get_member(request, hub_id)
    dag = await asyncio.to_thread(get_dag, hub_id)
    exists_a = await asyncio.to_thread(dag.commit_exists, hash_a)
    exists_b = await asyncio.to_thread(dag.commit_exists, hash_b)
    if not exists_a or not exists_b:
        raise HTTPException(404, "Commit not found")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(await asyncio.to_thread(dag.diff, hash_a, hash_b))
