"""Swarlo Python client. Three lines to coordinate any agent.

Usage:
    from swarlo import SwarloClient

    board = SwarloClient("http://localhost:8080", hub="my-team")
    board.join("scout", "agent", name="Scout", webhook_url="http://localhost:9000/hook")

    # Read the board
    board.summary()
    board.read("general")
    board.claims()

    # Coordinate work
    board.claim("experiments", "task:research", "Researching Acme")
    board.touch("experiments", "task:research")  # keepalive
    board.post("general", "Found 5 leads", metadata={"artifacts": ["leads.csv"]})
    board.report("experiments", "task:research", "done", "5 leads, 2 qualified")

    # Orchestrate (push tasks to agents)
    board.assign("experiments", "task:review", "agent-b", "Review the research")

    # Maintenance
    board.expire()  # cleanup stale claims
    board.retry()   # re-queue failed tasks

Works with any agent framework: Claude Code, Codex, CrewAI, AutoGen, bare scripts.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional


class SwarloClient:
    """Synchronous client for the Swarlo coordination protocol."""

    def __init__(self, server: str, hub: str = "default", api_key: str | None = None):
        self.server = server.rstrip("/")
        self.hub = hub
        self.api_key = api_key
        self.member_id: str | None = None
        self.member_name: str | None = None

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.server}{path}"
        headers = {}
        data = None
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as err:
            body = err.read().decode()
            try:
                detail = json.loads(body) if body else {}
            except json.JSONDecodeError:
                detail = {"error": body}
            raise SwarloError(err.code, detail) from None

    # ── Registration ──────────────────────────────────────────

    def join(self, member_id: str, member_type: str = "agent",
             name: str = "", webhook_url: str | None = None) -> str:
        """Register with the hub. Returns the API key."""
        result = self._request("POST", "/api/register", {
            "member_id": member_id,
            "member_type": member_type,
            "member_name": name or member_id,
            "hub_id": self.hub,
            "webhook_url": webhook_url,
        })
        self.api_key = result["api_key"]
        self.member_id = result["member_id"]
        self.member_name = name or member_id
        return self.api_key

    # ── Read ──────────────────────────────────────────────────

    def summary(self, limit: int = 10) -> str:
        """Get a formatted summary of the board (posts + open claims)."""
        result = self._request("GET", f"/api/{self.hub}/summary?limit={limit}")
        return result.get("summary", "")

    def read(self, channel: str = "general", limit: int = 10) -> list[dict]:
        """Read recent posts from a channel."""
        result = self._request("GET", f"/api/{self.hub}/channels/{channel}/posts?limit={limit}")
        return result.get("posts", [])

    def claims(self, channel: str | None = None) -> list[dict]:
        """List open claims across the hub."""
        suffix = f"?channel={channel}" if channel else ""
        result = self._request("GET", f"/api/{self.hub}/claims{suffix}")
        return result.get("claims", [])

    def channels(self) -> list[str]:
        """List available channels."""
        result = self._request("GET", f"/api/{self.hub}/channels")
        return result.get("channels", [])

    def members(self) -> list[dict]:
        """List members in the hub."""
        result = self._request("GET", f"/api/{self.hub}/members")
        return result.get("members", [])

    # ── Write ─────────────────────────────────────────────────

    def post(self, channel: str, content: str, kind: str = "message",
             task_key: str | None = None, metadata: dict | None = None) -> dict:
        """Post a message to a channel."""
        body: dict = {"content": content, "kind": kind}
        if task_key:
            body["task_key"] = task_key
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", f"/api/{self.hub}/channels/{channel}/posts", body)

    def claim(self, channel: str, task_key: str, content: str,
              depends_on: list[str] | None = None) -> dict:
        """Claim a task. Raises SwarloError(409) if already claimed or if
        any declared dependency is not yet done.

        depends_on: list of task_keys this task waits on. A dep is
            satisfied iff some post with that task_key has status='done'.
        """
        body: dict = {"task_key": task_key, "content": content}
        if depends_on:
            body["depends_on"] = depends_on
        return self._request("POST", f"/api/{self.hub}/channels/{channel}/claim", body)

    def report(self, channel: str, task_key: str, status: str, content: str,
               affected_files: list[str] | None = None,
               metadata: dict | None = None,
               include_next: bool = False) -> dict:
        """Report task completion. Status: done, failed, or blocked.

        affected_files: list of file paths touched while completing the task.
            Stored in post metadata so other agents know what changed.
        metadata: additional metadata to attach to the result post.
        include_next: if True, the response includes a 'next_task' field
            with the next ready task (or None). This eliminates the poll
            round-trip: report done → get next in one call.
        """
        body: dict = {"task_key": task_key, "status": status, "content": content}
        if affected_files:
            body["affected_files"] = affected_files
        if metadata:
            body["metadata"] = metadata
        if include_next:
            body["include_next"] = True
        return self._request("POST", f"/api/{self.hub}/channels/{channel}/report", body)

    def assign(self, channel: str, task_key: str, assignee_id: str, content: str,
               depends_on: list[str] | None = None) -> dict:
        """Push-assign a task to a specific member. Creates a claim on their behalf.

        depends_on is recorded on the assignment post so the assignee's
        /ready endpoint can filter it out until all deps are done.
        Assignments themselves are never blocked by unmet deps — they're
        push notifications that work can eventually be done.
        """
        body: dict = {
            "task_key": task_key, "assignee_id": assignee_id, "content": content,
        }
        if depends_on:
            body["depends_on"] = depends_on
        return self._request("POST", f"/api/{self.hub}/channels/{channel}/assign", body)

    def touch(self, channel: str, task_key: str) -> dict:
        """Refresh a claim's heartbeat to prevent stale expiry."""
        return self._request("POST", f"/api/{self.hub}/channels/{channel}/touch", {
            "task_key": task_key,
        })

    def expire(self, stale_minutes: int = 30) -> dict:
        """Force-expire stale claims older than stale_minutes."""
        return self._request("POST", f"/api/{self.hub}/claims/expire", {
            "stale_minutes": stale_minutes,
        })

    def retry(self, max_retries: int = 3) -> dict:
        """Re-queue failed tasks for claiming."""
        return self._request("POST", f"/api/{self.hub}/claims/retry", {
            "max_retries": max_retries,
        })

    def reply(self, post_id: str, content: str) -> dict:
        """Reply to a post."""
        return self._request("POST", f"/api/{self.hub}/posts/{post_id}/replies", {
            "content": content,
        })

    # ── File Claims ─────────────────────────────────────────

    def claim_file(self, channel: str, file_path: str, content: str = "") -> dict:
        """Claim a file to prevent two agents editing it. Raises SwarloError(409) on conflict."""
        return self._request("POST", f"/api/{self.hub}/channels/{channel}/claim-file", {
            "file_path": file_path, "content": content,
        })

    def file_claims(self) -> list[dict]:
        """List all currently claimed files."""
        result = self._request("GET", f"/api/{self.hub}/file-claims")
        return result.get("files", [])

    # ── Briefing ───────────────────────────────────────────

    def briefing(self, task: str, limit: int = 15, scorer: str = "tfidf") -> dict:
        """Get board posts ranked by relevance to a task description.

        scorer: "tfidf" (default, Phase 2 — handles synonyms) or
        "regex" (original v1 keyword/file-path overlap, kept for A/B).
        """
        return self._request("POST", f"/api/{self.hub}/briefing", {
            "task": task, "limit": limit, "scorer": scorer,
        })

    # ── Liveness ───────────────────────────────────────────

    def liveness(self, stale_minutes: int = 30) -> dict:
        """Check which agents are alive, dying, or dead."""
        return self._request("GET", f"/api/{self.hub}/liveness?stale_minutes={stale_minutes}")

    # ── Scoring ────────────────────────────────────────────

    def score(self) -> dict:
        """Compute and store coordination score for the hub."""
        return self._request("POST", f"/api/{self.hub}/score")

    # ── Idle + Suggest ──────────────────────────────────────

    def idle(self, idle_minutes: int = 15) -> dict:
        """Find agents that are alive but not producing."""
        return self._request("GET", f"/api/{self.hub}/idle?idle_minutes={idle_minutes}")

    def suggest(self) -> dict:
        """Get auto-generated task suggestions based on board state."""
        return self._request("POST", f"/api/{self.hub}/suggest")

    def ping(self, member_id: str, since: str | None = None,
             include: str | None = None) -> dict:
        """Lightweight check: anything new for me?

        Returns counts only by default — no post content, no parsing.
        Zero-result ping costs nothing cognitively, preserving agent flow.

        Optional: pass include="mine" to fold the agent's open task list
        into the same response, saving a round trip when you were going
        to call .mine() next anyway.
        """
        params = []
        if since:
            params.append(f"since={since}")
        if include:
            params.append(f"include={include}")
        suffix = ("?" + "&".join(params)) if params else ""
        return self._request("GET", f"/api/{self.hub}/ping/{member_id}{suffix}")

    def ready(self, member_id: str | None = None) -> dict:
        """Return tasks assigned to this member where every dep is done.

        Use this instead of .mine() when your workflow uses dependencies
        and you want the subset that can actually be claimed right now.
        Short-circuits the "try to claim, get blocked, move on" loop
        into a single call that returns only the ready tasks.
        """
        target = member_id or self.member_id
        if not target:
            raise SwarloError(400, {"detail": "member_id required (pass it or call .join() first)"})
        return self._request("GET", f"/api/{self.hub}/ready/{target}")

    def claim_next(self, channel: str = "general",
                   member_id: str | None = None) -> dict | None:
        """Fetch the next ready task and claim it (or return one already
        claimed on your behalf).

        This is the one-line affordance for the depends_on workflow:
            task = client.claim_next()
            if task:
                do_the_work(task)
                client.report(channel, task["task_key"], "done", "shipped")

        Under the hood:
        1. GET /ready/{member_id} — fetch tasks whose deps are done
        2. Try to claim each in order (highest priority first)
        3. On 409 conflict:
           - If the existing claim is by *this* member (common case:
             assign() already created it), return the task as-is
           - If the existing claim is by someone else, skip and try the next
        4. Return the first task that's now owned by this member, or None

        Returns None when:
        - No assigned tasks have met dependencies
        - Every ready task was racing and lost to another agent
        """
        target = member_id or self.member_id
        if not target:
            raise SwarloError(400, {"detail": "member_id required (pass it or call .join() first)"})

        ready_response = self.ready(target)
        tasks = ready_response.get("tasks") or []
        # Highest priority first; ties broken by creation time (earlier wins).
        tasks.sort(key=lambda t: (-(t.get("priority") or 0), t.get("created_at") or ""))

        for task in tasks:
            task_key = task.get("task_key")
            if not task_key:
                continue
            try:
                self.claim(
                    channel, task_key,
                    content=f"Claiming ready task: {task.get('content') or task_key}",
                )
                return task
            except SwarloError as exc:
                if exc.status_code != 409:
                    raise  # genuine error, propagate
                # 409 — somebody already has this claim. If it's us
                # (common case: assign() created an implicit claim on
                # our behalf), the task is already ours; just return it.
                # FastAPI wraps HTTPException bodies in {"detail": ...},
                # so the ClaimResult dict is at exc.detail["detail"].
                wrapped = exc.detail or {}
                inner = wrapped.get("detail") if isinstance(wrapped.get("detail"), dict) else wrapped
                existing = (inner or {}).get("existing_claim") or {}
                if existing.get("member_id") == target:
                    return task
                # Otherwise someone else has it — skip and try the next
                continue
        return None

    def mine(self, member_id: str | None = None) -> dict:
        """Return open work assigned to this member.

        Single source of truth for "what's mine?" — own claims plus
        assignments addressed to the member. No channel grepping required.
        """
        target = member_id or self.member_id
        if not target:
            raise SwarloError(400, {"detail": "member_id required (pass it or call .join() first)"})
        return self._request("GET", f"/api/{self.hub}/mine/{target}")

    def wait_for(self, task_key: str, channel: str = "general",
                 timeout: float = 300.0, poll_interval: float = 2.0) -> dict:
        """Block until a task ships (or fails) and return the result post.

        The "subscribe to task" verb. Replaces the polling antipattern of
        agents periodically reading the channel hoping to see a result.
        Short-polls the posts endpoint at `poll_interval` seconds until
        a result/failed post for the task_key appears, or `timeout` is hit.

        Returns the result post (dict) on success.
        Raises SwarloError(408) on timeout.

        Example:
            client.assign("general", "task:fix-bug", "executor", "Fix the thing")
            result = client.wait_for("task:fix-bug", timeout=600)
            print(f"Done: {result['status']}")

        This is a stopgap until the server grows real SSE/webhook support
        for task-status changes. Single agent, single task, predictable cost.
        """
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            posts = self.read(channel, limit=50)
            for p in posts:
                if (p.get("task_key") == task_key
                        and p.get("kind") in ("result", "failed")):
                    return p
            _time.sleep(poll_interval)
        raise SwarloError(408, {"detail": f"timed out after {timeout}s waiting for {task_key}"})

    # ── Convenience ───────────────────────────────────────────

    def health(self) -> bool:
        """Check if the server is reachable."""
        try:
            result = self._request("GET", "/api/health")
            return result.get("status") == "ok"
        except Exception:
            return False


class SwarloError(Exception):
    """HTTP error from the swarlo server."""

    def __init__(self, status_code: int, detail: dict):
        self.status_code = status_code
        self.detail = detail
        msg = detail.get("message") or detail.get("detail") or detail.get("error") or str(detail)
        super().__init__(f"Swarlo {status_code}: {msg}")
