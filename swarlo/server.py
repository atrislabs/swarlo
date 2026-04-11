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
    depends_on: Optional[list[str]] = None


class ReportRequest(BaseModel):
    task_key: str
    status: str = Field(..., pattern="^(done|failed|blocked)$")
    content: str
    affected_files: Optional[list[str]] = None
    metadata: Optional[dict] = None


class AssignRequest(BaseModel):
    task_key: str
    assignee_id: str
    content: str
    depends_on: Optional[list[str]] = None


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
    result = await get_backend().claim(
        hub_id, member, channel, body.task_key, body.content,
        depends_on=body.depends_on,
    )
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
    result = await be.assign(hub_id, assigner, channel, body.task_key, body.assignee_id, body.content,
                             depends_on=body.depends_on)
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
        post = await get_backend().report(
            hub_id, member, channel, body.task_key, body.status, body.content,
            affected_files=body.affected_files,
            metadata=body.metadata,
        )
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
    _get_member(request)
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

    # Count @mentions of this member
    new_mentions = be.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ? AND created_at > ? AND mentions LIKE ?",
        (hub_id, since, f'%"{member_id}"%'),
    ).fetchone()[0]

    # Count assignments to this member. Uses the first-class assignee_id
    # column (added 2026-04-11) instead of json_extract on metadata, which
    # silently failed for any post that didn't store assignee_id in metadata.
    new_assigns = be.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ? AND created_at > ? AND kind = 'assign' "
        "AND assignee_id = ?",
        (hub_id, since, member_id),
    ).fetchone()[0]

    # Bump last_seen
    from datetime import datetime, timezone
    try:
        be.conn.execute(
            "UPDATE members SET last_seen = ? WHERE member_id = ? AND hub_id = ?",
            (datetime.now(timezone.utc).isoformat(), member_id, hub_id),
        )
        be.conn.commit()
    except Exception:
        pass

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
    _get_member(request)
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
    _get_member(request)
    be = get_backend()
    posts = await be.get_my_open_tasks(hub_id, member_id)
    return {
        "member_id": member_id,
        "count": len(posts),
        "tasks": [p.to_dict() for p in posts],
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
    _get_member(request)
    be = get_backend()
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    alive_cutoff = (now - timedelta(minutes=30)).isoformat()
    idle_cutoff = (now - timedelta(minutes=idle_minutes)).isoformat()

    # Single query: alive agents with their last_active and any open claim.
    # LEFT JOIN folds the open-claim lookup into one round trip.
    rows = be.conn.execute(
        """
        SELECT m.member_id, m.member_name, m.last_active,
               (SELECT task_key FROM posts
                WHERE hub_id = m.hub_id AND member_id = m.member_id
                  AND kind = 'claim' AND status = 'open'
                LIMIT 1) AS open_claim
        FROM members m
        WHERE m.hub_id = ? AND m.last_seen > ? AND m.member_type = 'agent'
        """,
        (hub_id, alive_cutoff),
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
    task: str  # what the agent is about to work on
    limit: int = 15


@app.post("/api/{hub_id}/briefing")
async def get_briefing(hub_id: str, body: BriefingRequest, request: Request):
    """Return board posts ranked by relevance to a task description.

    Extracts file paths and keywords from the task, scores all posts
    by overlap, returns the most relevant ones. This is the text-level
    analog of KV-cache compaction from Latent Briefing (Geist 2026):
    the task prompt determines what's relevant from shared context.

    Same API shape upgrades to attention-based filtering when running
    on a local model (Sovereign/Qwen).
    """
    import re
    member = _get_member(request)
    be = get_backend()

    # --- Extract signals from task description ---
    # File paths: anything that looks like a/b.py or a/b/c
    file_pats = re.findall(r'[\w./]+\.(?:py|ts|js|md|json|yaml|sql)\b', body.task)
    # Also match directory-style paths
    dir_pats = re.findall(r'(?:backend|atris|swarlo|scripts|tests)/[\w/]+', body.task)
    all_paths = list(set(file_pats + dir_pats))

    # Keywords: split, lowercase, drop short/common words
    stopwords = {'the','a','an','is','are','was','were','be','been','and','or','but',
                 'in','on','at','to','for','of','with','by','from','this','that','it',
                 'not','no','do','does','did','will','would','should','can','could',
                 'has','have','had','all','each','every','any','some','into','about',
                 'up','out','if','then','than','so','as','just','also','how','what',
                 'when','where','which','who','why','may','must','shall','very','too',
                 'only','own','same','few','more','most','other','such','test','tests',
                 'file','files','code','add','fix','bug','new','run','check','make',
                 'write','read','use','get','set','put','update','create','delete'}
    words = set(w.lower() for w in re.findall(r'\b\w{3,}\b', body.task)) - stopwords

    # --- Score all recent posts ---
    rows = be.conn.execute(
        "SELECT * FROM posts WHERE hub_id = ? ORDER BY created_at DESC LIMIT 200",
        (hub_id,),
    ).fetchall()

    scored = []
    for r in rows:
        score = 0.0
        content_lower = (r["content"] or "").lower()
        task_key = r["task_key"] or ""

        # File path matches (strongest signal — like attention to specific keys)
        for fp in all_paths:
            if fp.lower() in content_lower or fp.lower() in task_key.lower():
                score += 3.0

        # Keyword matches (weaker signal — like distributed attention)
        matches = sum(1 for w in words if w in content_lower)
        if words:
            score += min(matches / len(words) * 2.0, 2.0)  # cap at 2.0

        # Metadata file overlap
        if r["metadata"]:
            try:
                meta = json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"]
                meta_files = meta.get("affected_files", [])
                for fp in all_paths:
                    if any(fp in mf for mf in meta_files):
                        score += 3.0
            except Exception:
                pass

        # Result/claim posts get slight boost (more actionable than messages)
        if r["kind"] in ("result", "claim", "assign"):
            score += 0.5

        if score > 0.5:  # threshold — like MAD normalization in the paper
            scored.append((score, r))

    # Sort by score descending, take top N
    scored.sort(key=lambda x: -x[0])
    top = scored[:body.limit]

    return {
        "task": body.task,
        "extracted_paths": all_paths,
        "extracted_keywords": sorted(words)[:20],
        "count": len(top),
        "posts": [
            {
                "score": round(s, 2),
                "member_name": r["member_name"],
                "kind": r["kind"],
                "task_key": r["task_key"],
                "content": r["content"][:300],
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
    _get_member(request)
    be = get_backend()
    from datetime import datetime, timezone, timedelta

    # Passive cleanup: expire stale claims before computing orphans. This
    # way the orphaned_claims list reflects what's *still* stuck after
    # the expiry ran, not what was stale a second ago.
    expired_keys: list[str] = []
    if auto_expire:
        expired_keys = await be.force_expire_claims(hub_id, stale_minutes=stale_minutes)

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
    _get_member(request)
    if not since or not since.strip():
        raise HTTPException(400, "since query param is required (ISO8601 timestamp)")

    safe_limit = max(1, min(int(limit), 500))
    be = get_backend()
    if channel:
        rows = be.conn.execute(
            "SELECT * FROM posts WHERE hub_id = ? AND channel = ? AND created_at > ? "
            "ORDER BY created_at ASC LIMIT ?",
            (hub_id, channel, since, safe_limit),
        ).fetchall()
    else:
        rows = be.conn.execute(
            "SELECT * FROM posts WHERE hub_id = ? AND created_at > ? "
            "ORDER BY created_at ASC LIMIT ?",
            (hub_id, since, safe_limit),
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
    _get_member(request)
    be = get_backend()
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    suggestions = []

    # 1. Find idle agents
    alive_cutoff = (now - timedelta(minutes=30)).isoformat()
    idle_cutoff = (now - timedelta(minutes=15)).isoformat()
    alive = be.conn.execute(
        "SELECT member_id, member_name FROM members WHERE hub_id = ? AND last_seen > ? AND member_type = 'agent'",
        (hub_id, alive_cutoff),
    ).fetchall()

    idle_agents = []
    for a in alive:
        last_post = be.conn.execute(
            "SELECT created_at FROM posts WHERE hub_id = ? AND member_id = ? ORDER BY created_at DESC LIMIT 1",
            (hub_id, a["member_id"]),
        ).fetchone()
        has_claim = be.conn.execute(
            "SELECT 1 FROM posts WHERE hub_id = ? AND member_id = ? AND kind = 'claim' AND status = 'open'",
            (hub_id, a["member_id"]),
        ).fetchone()
        if not has_claim and (not last_post or last_post["created_at"] < idle_cutoff):
            idle_agents.append(a["member_name"])

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
        "SELECT content, task_key FROM posts WHERE hub_id = ? AND kind = 'result' ORDER BY created_at DESC LIMIT 20",
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
        "SELECT task_key, content FROM posts WHERE hub_id = ? AND kind = 'failed' ORDER BY created_at DESC LIMIT 5",
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

    # Conflict detection: find file claims that were contested (409'd)
    # and edits to same file by different agents within 30 min
    file_conflicts = be.conn.execute(
        """
        SELECT COUNT(DISTINCT task_key) FROM posts
        WHERE hub_id = ? AND task_key LIKE 'file:%'
        AND kind = 'claim' AND status = 'open'
        """,
        (hub_id,),
    ).fetchone()[0]

    # Detect revert patterns: same file edited by 2+ different agents
    revert_risk = be.conn.execute(
        """
        SELECT task_key, COUNT(DISTINCT member_id) as editors
        FROM posts
        WHERE hub_id = ? AND task_key LIKE 'file:%' AND kind = 'claim'
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
        "AND task_key NOT IN (SELECT task_key FROM posts WHERE hub_id = ? AND kind = 'claim' AND task_key IS NOT NULL)",
        (hub_id, hub_id),
    ).fetchone()[0]

    coord_score = (tasks_shipped * 10) + (agents_active * 5) - (files_with_multi_editors * 20) - (unclaimed_tasks * 5)

    now = datetime.now(timezone.utc).isoformat()

    # Store score in SQLite for history. The scores table is created in
    # the SCHEMA block at startup (sqlite_backend.py) — no need to
    # CREATE TABLE IF NOT EXISTS on every request.
    try:
        be.conn.execute(
            "INSERT INTO scores (hub_id, agents_active, tasks_claimed, tasks_shipped, avg_time_to_claim, file_conflicts, files_with_multi_editors, coord_score, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (hub_id, agents_active, tasks_claimed, tasks_shipped, avg_time_to_claim, file_conflicts, files_with_multi_editors, coord_score, now),
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
        "file_conflicts": file_conflicts,
        "files_with_multi_editors": files_with_multi_editors,
        "unclaimed_tasks": unclaimed_tasks,
        "coord_score": coord_score,
        "computed_at": now,
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
