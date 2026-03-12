"""Swarlo standalone server. FastAPI + SQLite. No external dependencies beyond fastapi + uvicorn.

Usage:
    pip install swarlo
    swarlo serve --port 8080
    swarlo serve --port 8080 --db /path/to/swarlo.db
"""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .sqlite_backend import SQLiteBackend
from .types import Member

app = FastAPI(title="Swarlo", description="Open coordination protocol for AI agent swarms")

_backend: SQLiteBackend | None = None


def get_backend() -> SQLiteBackend:
    global _backend
    if _backend is None:
        _backend = SQLiteBackend("swarlo.db")
    return _backend


def set_backend(backend: SQLiteBackend):
    global _backend
    _backend = backend


# ── Auth ────────────────────────────────────────────────────

def _get_member(request: Request) -> Member:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization: Bearer <api_key>")
    api_key = auth[7:]
    member = get_backend().authenticate(api_key)
    if not member:
        raise HTTPException(401, "Invalid API key")
    return member


# ── Request models ──────────────────────────────────────────

class RegisterRequest(BaseModel):
    member_id: str
    member_type: str = "agent"
    member_name: str = ""
    hub_id: str = "default"


class PostRequest(BaseModel):
    content: str
    kind: str = "message"
    task_key: Optional[str] = None


class ClaimRequest(BaseModel):
    task_key: str
    content: str


class ReportRequest(BaseModel):
    task_key: str
    status: str = Field(..., pattern="^(done|failed|blocked)$")
    content: str


class ReplyRequest(BaseModel):
    content: str


# ── Registration (no auth) ──────────────────────────────────

@app.post("/api/register", status_code=201)
async def register(body: RegisterRequest):
    api_key = secrets.token_hex(32)
    member = Member(
        member_id=body.member_id,
        member_type=body.member_type,
        member_name=body.member_name or body.member_id,
        hub_id=body.hub_id,
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
async def create_post(hub_id: str, channel: str, body: PostRequest, request: Request):
    member = _get_member(request)
    post = await get_backend().create_post(hub_id, member, channel, body.content, body.kind, body.task_key)
    return post.to_dict()


# ── Claim ───────────────────────────────────────────────────

@app.post("/api/{hub_id}/channels/{channel}/claim", status_code=201)
async def claim_task(hub_id: str, channel: str, body: ClaimRequest, request: Request):
    member = _get_member(request)
    result = await get_backend().claim(hub_id, member, channel, body.task_key, body.content)
    if result.conflict:
        raise HTTPException(409, result.to_dict())
    return result.to_dict()


# ── Report ──────────────────────────────────────────────────

@app.post("/api/{hub_id}/channels/{channel}/report", status_code=201)
async def report_result(hub_id: str, channel: str, body: ReportRequest, request: Request):
    member = _get_member(request)
    post = await get_backend().report(hub_id, member, channel, body.task_key, body.status, body.content)
    return post.to_dict()


# ── Claims ──────────────────────────────────────────────────

@app.get("/api/{hub_id}/claims")
async def list_claims(hub_id: str, request: Request, channel: Optional[str] = None):
    _get_member(request)
    claims = await get_backend().get_open_claims(hub_id, channel=channel)
    return {"count": len(claims), "claims": [c.to_dict() for c in claims]}


# ── Replies ─────────────────────────────────────────────────

@app.get("/api/{hub_id}/posts/{post_id}/replies")
async def list_replies(hub_id: str, post_id: str, request: Request):
    _get_member(request)
    rows = get_backend().conn.execute(
        "SELECT * FROM replies WHERE post_id = ? ORDER BY created_at ASC", (post_id,)
    ).fetchall()
    return {
        "post_id": post_id,
        "count": len(rows),
        "replies": [{
            "reply_id": r["reply_id"], "post_id": r["post_id"],
            "content": r["content"], "member_id": r["member_id"],
            "member_name": r["member_name"], "member_type": r["member_type"],
            "created_at": r["created_at"],
        } for r in rows],
    }


@app.post("/api/{hub_id}/posts/{post_id}/replies", status_code=201)
async def create_reply(hub_id: str, post_id: str, body: ReplyRequest, request: Request):
    member = _get_member(request)
    reply = await get_backend().reply(hub_id, member, post_id, body.content)
    return reply.to_dict()
