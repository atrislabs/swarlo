"""CLI entry point for Swarlo."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

try:
    from . import __version__
except ImportError:  # pragma: no cover - direct file loading in tests
    from swarlo import __version__


CONFIG_ENV = "SWARLO_CONFIG"
SPEED_CHECK_REPORT_SCHEMA_VERSION = 1
SPEED_PROOF_SUMMARY_SCHEMA_VERSION = 1
SPEED_LIVE_DATA_TABLES = ("members", "posts", "scores")
SPEED_INDEXES = {
    "posts": {
        "idx_posts_hub_channel",
        "idx_posts_hub_channel_created",
        "idx_posts_hub_created",
        "idx_posts_hub_member_created",
        "idx_posts_hub_assignee_created",
        "idx_posts_hub_kind_created",
        "idx_posts_hub_kind_status_created",
        "idx_posts_hub_kind_status_member_created",
        "idx_posts_hub_task_status_created",
    },
    "replies": {"idx_replies_hub_post_created"},
    "scores": {"idx_scores_hub_computed"},
    "members": {"idx_members_api_key", "idx_members_hub_type_seen", "idx_members_hub_seen_type"},
    "commits": {
        "idx_commits_hub_created",
        "idx_commits_hub_member_created",
        "idx_commits_hub_parent_created",
    },
}
SPEED_QUERY_PLANS = {
    "channel_reads": (
        "idx_posts_hub_channel_created",
        "SELECT * FROM posts WHERE hub_id = ? AND channel = ? "
        "ORDER BY created_at DESC LIMIT ?",
        ("swarlo-speed-check", "ops", 10),
    ),
    "channel_listing": (
        "idx_posts_hub_channel",
        "SELECT DISTINCT channel FROM posts WHERE hub_id = ?",
        ("swarlo-speed-check",),
    ),
    "recent_board": (
        "idx_posts_hub_created",
        "SELECT * FROM posts WHERE hub_id = ? ORDER BY created_at DESC LIMIT ?",
        ("swarlo-speed-check", 10),
    ),
    "member_posts": (
        "idx_posts_hub_member_created",
        "SELECT * FROM posts WHERE hub_id = ? AND member_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        ("swarlo-speed-check", "agent", 10),
    ),
    "assignee_work": (
        "idx_posts_hub_assignee_created",
        "SELECT * FROM posts WHERE hub_id = ? AND assignee_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        ("swarlo-speed-check", "agent", 10),
    ),
    "kind_reads": (
        "idx_posts_hub_kind_created",
        "SELECT * FROM posts WHERE hub_id = ? AND kind = ? "
        "ORDER BY created_at DESC LIMIT ?",
        ("swarlo-speed-check", "message", 10),
    ),
    "task_status": (
        "idx_posts_hub_task_status_created",
        "SELECT * FROM posts WHERE hub_id = ? AND task_key = ? AND status = ? "
        "ORDER BY created_at DESC",
        ("swarlo-speed-check", "TASK-1", "done"),
    ),
    "open_claims": (
        "idx_posts_hub_kind_status_created",
        "SELECT * FROM posts WHERE hub_id = ? AND kind = ? AND status = ? "
        "ORDER BY created_at DESC LIMIT ?",
        ("swarlo-speed-check", "claim", "open", 10),
    ),
    "orphan_claims": (
        "idx_posts_hub_kind_status_member_created",
        "SELECT * FROM posts INDEXED BY idx_posts_hub_kind_status_member_created "
        "WHERE hub_id = ? AND kind = ? AND status = ? AND member_id IN (?, ?) "
        "ORDER BY created_at DESC",
        ("swarlo-speed-check", "claim", "open", "agent-a", "agent-b"),
    ),
    "reply_thread": (
        "idx_replies_hub_post_created",
        "SELECT * FROM replies WHERE hub_id = ? AND post_id = ? "
        "ORDER BY created_at ASC",
        ("swarlo-speed-check", "post-1"),
    ),
    "active_agents": (
        "idx_members_hub_type_seen",
        "SELECT member_id FROM members WHERE hub_id = ? AND member_type = ? "
        "AND last_seen > ?",
        ("swarlo-speed-check", "agent", "1970-01-01T00:00:00+00:00"),
    ),
    "api_key_auth": (
        "idx_members_api_key",
        "SELECT * FROM members WHERE api_key = ?",
        ("swarlo-speed-check-api-key",),
    ),
    "score_history": (
        "idx_scores_hub_computed",
        "SELECT * FROM scores WHERE hub_id = ? "
        "ORDER BY computed_at DESC, id DESC LIMIT ?",
        ("swarlo-speed-check", 10),
    ),
    "commit_children": (
        "idx_commits_hub_parent_created",
        "SELECT * FROM commits WHERE hub_id = ? AND parent_hash = ? "
        "ORDER BY created_at DESC",
        ("swarlo-speed-check", "root"),
    ),
    "commit_leaves": (
        "idx_commits_hub_parent_created",
        "SELECT c.* FROM commits c "
        "LEFT JOIN commits child "
        "ON child.parent_hash = c.hash AND child.hub_id = c.hub_id "
        "WHERE c.hub_id = ? AND child.hash IS NULL "
        "ORDER BY c.created_at DESC",
        ("swarlo-speed-check",),
    ),
}


def _config_path() -> Path:
    override = os.getenv(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".swarlo" / "config.json"


def _load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_config(config: dict) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")


def _request(method: str, url: str, payload: dict | None = None, api_key: str | None = None) -> tuple[int, dict]:
    headers = {}
    data = None
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
        body = err.read().decode()
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"error": body}
        return err.code, payload


# Routes the CLI needs that older hubs may not have mounted yet.
_CLI_SERVER_ROUTES = (
    ("GET", "/api/{hub}/unclaimed"),
    ("GET", "/api/{hub}/xp"),
    ("GET", "/api/{hub}/scores"),
    ("GET", "/api/{hub}/handoff_trail/{task_key}"),
)


def _http_failure_message(label: str, status: int, body: dict, *, route: str | None = None) -> str:
    """Human failure line. 404 on a known route → upgrade/restart hint."""
    base = f"{label} failed ({status}): {body}"
    if status != 404 or not route:
        return base
    return (
        f"{base}\n"
        f"  server is missing {route} — restart `swarlo serve` with a package that mounts it "
        f"(or upgrade swarlo). CLI and hub are out of sync."
    )


def _raise_http_failure(label: str, status: int, body: dict, *, route: str | None = None) -> None:
    raise SystemExit(_http_failure_message(label, status, body, route=route))


def _require_runtime(args, *, auth: bool = True, hub: bool = True) -> dict:
    config = _load_config()
    runtime = {
        "server": getattr(args, "server", None) or os.getenv("SWARLO_SERVER") or config.get("server"),
        "api_key": getattr(args, "api_key", None) or os.getenv("SWARLO_API_KEY") or config.get("api_key"),
        "hub": getattr(args, "hub", None) or os.getenv("SWARLO_HUB") or config.get("hub"),
        "member_id": (
            getattr(args, "member_id", None)
            or os.getenv("SWARLO_MEMBER_ID")
            or config.get("member_id")
        ),
    }

    if not runtime["server"]:
        raise SystemExit("Missing server. Run `swarlo join --server ...` or pass `--server`.")
    if auth and not runtime["api_key"]:
        raise SystemExit("Missing api key. Run `swarlo join ...` first or pass `--api-key`.")
    if hub and not runtime["hub"]:
        raise SystemExit("Missing hub. Run `swarlo join ...` first or pass `--hub`.")
    return runtime


def _bounded_limit(value: int, *, default: int = 20, maximum: int = 500) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, maximum))


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _row_minimum(value: str) -> tuple[str, int]:
    try:
        table, raw_minimum = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must use table=count") from exc
    if table not in SPEED_INDEXES:
        raise argparse.ArgumentTypeError(
            f"table must be one of {', '.join(sorted(SPEED_INDEXES))}"
        )
    try:
        minimum = int(raw_minimum)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("count must be an integer") from exc
    if minimum <= 0:
        raise argparse.ArgumentTypeError("count must be greater than 0")
    return table, minimum


def _report_sha256(report: dict) -> str:
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _print_xp_mechanics() -> None:
    print()
    print("XP mechanics:")
    print("  claim:                 +2 XP")
    print("  done result:           +10 XP")
    print("  failed result:         -3 XP")
    print("  blocked result:        -1 XP")
    print("  file: task keys:       excluded from XP/task metrics")
    print("  unclaimed:             excludes live claims and terminal reports/statuses")


def _print_posts(posts: list[dict]) -> None:
    if not posts:
        print("No posts.")
        return
    for post in posts:
        prefix = f"[{post['kind']}]"
        if post.get("task_key"):
            prefix += f" {post['task_key']}"
        print(f"{prefix} {post['member_name']}: {post['content']}")


def _print_claims(claims: list[dict]) -> None:
    if not claims:
        print("No open claims.")
        return
    for claim in claims:
        print(f"[claim] {claim['task_key']} {claim['member_name']}: {claim['content']}")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_minutes(value: str | None, now: datetime) -> float | None:
    parsed = _parse_timestamp(value)
    if not parsed:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60)


def _short_age(value: str | None, now: datetime) -> str:
    minutes = _age_minutes(value, now)
    if minutes is None:
        return "unknown"
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def _clip_text(value: str | None, width: int = 100) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    return text[: width - 3].rstrip() + "..."


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row[1]) for row in rows}


def _load_json_object(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_tower_hub(conn: sqlite3.Connection, requested: str | None) -> str:
    if requested:
        return requested
    config_hub = _load_config().get("hub")
    if config_hub:
        return str(config_hub)
    row = conn.execute(
        "SELECT hub_id, COUNT(*) AS n FROM posts GROUP BY hub_id ORDER BY n DESC LIMIT 1"
    ).fetchone()
    if row and row["hub_id"]:
        return str(row["hub_id"])
    row = conn.execute(
        "SELECT hub_id FROM members GROUP BY hub_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    if row and row["hub_id"]:
        return str(row["hub_id"])
    raise SystemExit("tower: no hub found in the database; pass --hub")


def _tower_speed_report(db_path: str) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = _run_speed_check(
            db_path,
            as_json=True,
            max_ms=1000,
            require_planner=True,
            require_live_data=True,
            min_rows=[("posts", 1000), ("scores", 10000), ("members", 1)],
        )
    try:
        report = json.loads(out.getvalue())
    except json.JSONDecodeError:
        report = {"ok": False, "error": "speed proof did not return JSON"}
    report["exit_code"] = code
    return report


def _build_tower_state(
    db_path: str,
    *,
    hub: str | None = None,
    limit: int = 5,
    stale_minutes: int = 30,
    idle_minutes: int = 15,
) -> dict:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise SystemExit(f"tower: database not found: {path}")

    now = datetime.now(UTC)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        hub_id = _resolve_tower_hub(conn, hub)
        post_cols = _table_columns(conn, "posts")
        member_cols = _table_columns(conn, "members")
        # Old local DBs may predate last_seen / last_active. SELECT only
        # what exists so tower stays usable instead of crashing.
        member_select = ["member_id", "member_name", "member_type"]
        for col in ("last_seen", "last_active"):
            if col in member_cols:
                member_select.append(col)
            else:
                member_select.append(f"NULL AS {col}")

        members = [
            dict(row)
            for row in conn.execute(
                f"SELECT {', '.join(member_select)} "
                "FROM members WHERE hub_id = ? ORDER BY member_name",
                (hub_id,),
            ).fetchall()
        ]
        claim_select = [
            "post_id",
            "channel",
            "task_key",
            "member_id",
            "member_name",
            "content",
            "created_at",
        ]
        if "metadata" in post_cols:
            claim_select.insert(-1, "metadata")
        else:
            claim_select.insert(-1, "NULL AS metadata")
        open_claims = [
            dict(row)
            for row in conn.execute(
                f"SELECT {', '.join(claim_select)} "
                "FROM posts WHERE hub_id = ? AND kind = 'claim' AND status = 'open' "
                "ORDER BY created_at DESC",
                (hub_id,),
            ).fetchall()
        ]
        open_claim_keys = {
            row["task_key"]
            for row in open_claims
            if row.get("task_key") and not str(row.get("task_key")).startswith("file:")
        }
        terminal_rows = conn.execute(
            "SELECT DISTINCT task_key FROM posts WHERE hub_id = ? AND task_key IS NOT NULL "
            "AND task_key NOT LIKE 'file:%' AND (kind IN ('result', 'blocked') "
            "OR status IN ('done', 'failed', 'blocked'))",
            (hub_id,),
        ).fetchall()
        terminal_keys = {row["task_key"] for row in terminal_rows}
        task_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT channel, task_key, content, created_at FROM posts "
                "WHERE hub_id = ? AND kind = 'message' AND task_key IS NOT NULL "
                "AND task_key NOT LIKE 'file:%' ORDER BY created_at DESC",
                (hub_id,),
            ).fetchall()
        ]
        unclaimed = [
            row
            for row in task_rows
            if row["task_key"] not in open_claim_keys and row["task_key"] not in terminal_keys
        ][:limit]
        blocked_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT channel, task_key, member_name, content, created_at FROM posts "
                "WHERE hub_id = ? AND task_key IS NOT NULL AND task_key NOT LIKE 'file:%' "
                "AND (kind = 'blocked' OR status = 'blocked') "
                "ORDER BY created_at DESC LIMIT ?",
                (hub_id, limit * 5),
            ).fetchall()
        ]
        blocked = []
        seen_blocked: set[str] = set()
        for row in blocked_rows:
            task_key = row.get("task_key")
            if not task_key or task_key in seen_blocked:
                continue
            seen_blocked.add(task_key)
            blocked.append(row)
            if len(blocked) >= limit:
                break
        latest_score = conn.execute(
            "SELECT * FROM scores WHERE hub_id = ? ORDER BY computed_at DESC, id DESC LIMIT 1",
            (hub_id,),
        ).fetchone()

        xp_by_member: dict[str, dict] = {}
        event_cols = "member_id, member_name, kind, status, task_key"
        if "task_key" not in post_cols:
            event_cols = "member_id, member_name, kind, status, NULL AS task_key"
        events = conn.execute(f"SELECT {event_cols} FROM posts WHERE hub_id = ?", (hub_id,)).fetchall()
        for event in events:
            task_key = event["task_key"]
            if task_key and str(task_key).startswith("file:"):
                continue
            member_id = event["member_id"] or "unknown"
            row = xp_by_member.setdefault(
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
            kind = event["kind"]
            status = event["status"]
            if kind == "claim" and status != "retracted":
                row["xp"] += 2
                row["claims"] += 1
            elif status == "blocked" or kind == "blocked":
                row["xp"] -= 1
                row["blocked"] += 1
            elif status == "failed" or kind == "failed":
                row["xp"] -= 3
                row["failed"] += 1
            elif status == "done" or kind == "result":
                row["xp"] += 10
                row["shipped"] += 1

        claim_by_member = {row["member_id"] for row in open_claims if row.get("member_id")}
        active, idle, offline = [], [], []
        for member in members:
            if member.get("member_type") != "agent":
                continue
            seen_age = _age_minutes(member.get("last_seen"), now)
            active_age = _age_minutes(member.get("last_active"), now)
            has_claim = member.get("member_id") in claim_by_member
            entry = {
                "member_id": member.get("member_id"),
                "member_name": member.get("member_name") or member.get("member_id"),
                "last_seen_age": _short_age(member.get("last_seen"), now),
                "last_active_age": _short_age(member.get("last_active"), now),
                "has_open_claim": has_claim,
            }
            if seen_age is None or seen_age > stale_minutes * 3:
                offline.append(entry)
            elif has_claim or (active_age is not None and active_age <= idle_minutes):
                active.append(entry)
            else:
                idle.append(entry)

        stale_claims = []
        for claim in open_claims:
            metadata = _load_json_object(claim.get("metadata"))
            heartbeat_at = metadata.get("heartbeat_at") or claim.get("created_at")
            if (_age_minutes(heartbeat_at, now) or 0) >= stale_minutes:
                stale_claims.append(
                    {
                        "task_key": claim.get("task_key"),
                        "member_name": claim.get("member_name"),
                        "channel": claim.get("channel"),
                        "age": _short_age(heartbeat_at, now),
                    }
                )

        proof = _tower_speed_report(str(path))
        proof_ok = bool(proof.get("ok"))
        if stale_claims:
            next_action = "Reassign or refresh stale claims first."
        elif unclaimed:
            next_action = "Give ownerless tasks a clear owner."
        elif blocked:
            next_action = "Review blocked tasks and decide unblock, retry, or close."
        elif not proof_ok:
            next_action = "Run speed-proof and fix the proof gap before a release."
        elif idle:
            next_action = "Give idle agents the next task, or let them stand down."
        else:
            next_action = "No owner action needed right now."

        health = "Calm" if next_action == "No owner action needed right now." else "Needs attention"
        rows = proof.get("database", {}).get("rows", {})
        index_count = sum(
            int(item.get("present", 0))
            for item in (proof.get("indexes") or {}).values()
            if isinstance(item, dict)
        )
        index_total = sum(
            int(item.get("total", 0))
            for item in (proof.get("indexes") or {}).values()
            if isinstance(item, dict)
        )

        return {
            "hub": hub_id,
            "db": str(path),
            "health": health,
            "next_action": next_action,
            "counts": {
                "agents_active": len(active),
                "agents_idle": len(idle),
                "agents_offline": len(offline),
                "open_claims": len(open_claims),
                "stale_claims": len(stale_claims),
                "unclaimed_tasks": len(unclaimed),
                "blocked_tasks": len(blocked),
                "coord_score": dict(latest_score).get("coord_score") if latest_score else None,
            },
            "people": {
                "active": active[:limit],
                "idle": idle[:limit],
                "offline": offline[:limit],
            },
            "leaderboard": sorted(
                xp_by_member.values(),
                key=lambda row: (row["xp"], row["shipped"], row["claims"]),
                reverse=True,
            )[:limit],
            "open_claims": [
                {
                    "task_key": row.get("task_key"),
                    "member_name": row.get("member_name"),
                    "channel": row.get("channel"),
                    "age": _short_age(row.get("created_at"), now),
                }
                for row in open_claims[:limit]
            ],
            "stale_claims": stale_claims[:limit],
            "unclaimed": unclaimed,
            "blocked": blocked,
            "proof": {
                "ok": proof_ok,
                "elapsed_ms": proof.get("elapsed_ms"),
                "fast_paths": {
                    "ok": proof.get("planner", {}).get("ok"),
                    "total": proof.get("planner", {}).get("expected_total"),
                },
                "indexes": {"ok": index_count, "total": index_total},
                "rows": rows,
                "missing": {
                    "indexes": {
                        table: result.get("missing")
                        for table, result in (proof.get("indexes") or {}).items()
                        if isinstance(result, dict) and result.get("missing")
                    },
                    "live_data": proof.get("live_data", {}).get("missing") or {},
                    "row_minimums": proof.get("row_minimums", {}).get("misses") or {},
                },
            },
        }
    finally:
        conn.close()


def _print_tower(state: dict) -> None:
    counts = state["counts"]
    proof = state["proof"]
    print("Swarlo Tower")
    print(f"Hub: {state['hub']}")
    print(f"Overall: {state['health']}")
    print(f"Next: {state['next_action']}")
    print()
    print("Work")
    print(f"  Active now:       {counts['agents_active']} agent(s)")
    print(f"  Idle but online:  {counts['agents_idle']} agent(s)")
    print(f"  Quiet history:    {counts['agents_offline']} older agent record(s)")
    print(f"  Tasks in hand:    {counts['open_claims']} open claim(s)")
    print(f"  Stale claims:     {counts['stale_claims']} older claim(s)")
    print(f"  No owner yet:     {counts['unclaimed_tasks']} task(s)")
    print(f"  Blocked:          {counts['blocked_tasks']} task(s)")
    if counts.get("coord_score") is not None:
        print(f"  Score:            {counts['coord_score']}")
    print()
    print("People")
    if state["leaderboard"]:
        for idx, row in enumerate(state["leaderboard"], start=1):
            print(
                f"  {idx}. {row['member_name']} - {row['xp']} XP "
                f"({row['shipped']} done, {row['claims']} claimed, {row['blocked']} blocked)"
            )
    else:
        print("  No work history yet.")
    print()
    print("Needs attention")
    if not state["stale_claims"] and not state["unclaimed"] and not state["blocked"]:
        print("  Nothing urgent.")
    for row in state["stale_claims"]:
        print(f"  Stale: {row['task_key']} with {row['member_name']} ({row['age']})")
    for row in state["unclaimed"]:
        print(f"  No owner: {row.get('task_key')} in {row.get('channel')} - {_clip_text(row.get('content'))}")
    for row in state["blocked"]:
        print(f"  Blocked: {row.get('task_key')} - {_clip_text(row.get('content'))}")
    print()
    proof_status = "Good" if proof["ok"] else "Needs attention"
    print("Proof")
    print(f"  Status:           {proof_status}")
    print(
        f"  Fast routes:      {proof['fast_paths']['ok']}/{proof['fast_paths']['total']} ready"
    )
    print(f"  Speed helpers:    {proof['indexes']['ok']}/{proof['indexes']['total']} ready")
    rows = proof.get("rows") or {}
    print(
        "  Data seen:        "
        f"{rows.get('posts', 0)} posts, {rows.get('scores', 0)} scores, "
        f"{rows.get('members', 0)} members"
    )
    if proof.get("elapsed_ms") is not None:
        print(f"  Checked in:       {proof['elapsed_ms']} ms")
    if not proof["ok"]:
        missing = proof.get("missing") or {}
        if missing.get("indexes"):
            print("  Fix first: missing speed indexes.")
        elif missing.get("live_data"):
            print("  Fix first: database does not have enough live data.")
        elif missing.get("row_minimums"):
            print("  Fix first: database is below release-scale row floors.")
        else:
            print("  Fix first: run speed-proof for details.")


def _run_speed_check(
    db_path: str,
    *,
    as_json: bool = False,
    output_path: str | None = None,
    max_ms: float | None = None,
    require_planner: bool = False,
    require_live_data: bool = False,
    min_rows: list[tuple[str, int]] | None = None,
) -> int:
    started = time.perf_counter()
    path = Path(db_path).expanduser()
    if not path.exists():
        raise SystemExit(f"speed-check: database not found: {path}")

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        rows = conn.execute(
            "SELECT tbl_name, name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
        row_counts: dict[str, int | None] = {}
        for table in sorted(SPEED_INDEXES):
            try:
                row_counts[table] = int(
                    conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
            except sqlite3.OperationalError:
                row_counts[table] = None

        by_table: dict[str, set[str]] = {}
        for table, name in rows:
            by_table.setdefault(table, set()).add(name)

        missing: dict[str, list[str]] = {}
        for table, expected in SPEED_INDEXES.items():
            absent = sorted(expected - by_table.get(table, set()))
            if absent:
                missing[table] = absent

        planner_checked: list[str] = []
        planner_misses: list[tuple[str, str, str]] = []
        for name, (expected_index, sql, params) in SPEED_QUERY_PLANS.items():
            try:
                plan_rows = conn.execute(
                    f"EXPLAIN QUERY PLAN {sql}", params
                ).fetchall()
            except sqlite3.OperationalError:
                # Minimal synthetic schemas used by tests may only contain
                # index names. Real Swarlo DBs exercise these plan checks.
                continue
            planner_checked.append(name)
            plan = " | ".join(str(row[3]) for row in plan_rows)
            if expected_index not in plan:
                planner_misses.append((name, expected_index, plan))
    finally:
        conn.close()

    index_results = {}
    for table in sorted(SPEED_INDEXES):
        index_results[table] = {
            "present": len(SPEED_INDEXES[table]) - len(missing.get(table, [])),
            "total": len(SPEED_INDEXES[table]),
            "missing": missing.get(table, []),
        }
    planner_details = [
        {"name": name, "expected_index": expected_index, "plan": plan}
        for name, expected_index, plan in planner_misses
    ]
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    latency_ok = max_ms is None or elapsed_ms <= max_ms
    planner_required_ok = not require_planner or len(planner_checked) == len(SPEED_QUERY_PLANS)
    live_data_missing = {
        table: row_counts.get(table)
        for table in SPEED_LIVE_DATA_TABLES
        if not isinstance(row_counts.get(table), int) or int(row_counts[table] or 0) <= 0
    }
    live_data_ok = not require_live_data or not live_data_missing
    row_minimums = {table: minimum for table, minimum in (min_rows or [])}
    row_minimum_misses = {
        table: {"actual": row_counts.get(table), "minimum": minimum}
        for table, minimum in row_minimums.items()
        if not isinstance(row_counts.get(table), int) or int(row_counts[table] or 0) < minimum
    }
    row_minimums_ok = not row_minimum_misses
    ok = (
        not missing
        and not planner_misses
        and latency_ok
        and planner_required_ok
        and live_data_ok
        and row_minimums_ok
    )
    report = {
        "ok": ok,
        "db": str(path),
        "database": {
            "access": "read_only",
            "page_count": page_count,
            "page_size": page_size,
            "rows": row_counts,
            "size_bytes": path.stat().st_size,
        },
        "elapsed_ms": elapsed_ms,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "package": {"name": "swarlo", "version": __version__},
        "runtime": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "schema_version": SPEED_CHECK_REPORT_SCHEMA_VERSION,
        "indexes": index_results,
        "latency_budget": {
            "max_ms": max_ms,
            "ok": latency_ok,
        },
        "live_data": {
            "required": require_live_data,
            "required_tables": list(SPEED_LIVE_DATA_TABLES),
            "ok": live_data_ok,
            "missing": live_data_missing,
        },
        "row_minimums": {
            "required": row_minimums,
            "ok": row_minimums_ok,
            "misses": row_minimum_misses,
        },
        "planner": {
            "expected_total": len(SPEED_QUERY_PLANS),
            "ok": len(planner_checked) - len(planner_misses),
            "required": require_planner,
            "required_ok": planner_required_ok,
            "total": len(planner_checked),
            "paths": planner_checked,
            "misses": planner_details,
        },
    }
    report["report_sha256"] = _report_sha256(report)
    report_json = json.dumps(report, indent=2, sort_keys=True) + "\n"

    output: Path | None = None
    if output_path:
        output = Path(output_path).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp_output = output.with_name(f".{output.name}.tmp")
        tmp_output.write_text(report_json)
        tmp_output.replace(output)

    if as_json:
        print(report_json, end="")
        return 0 if ok else 1

    print(f"Swarlo speed-check: {path}")
    for table in sorted(SPEED_INDEXES):
        present = index_results[table]["present"]
        total = index_results[table]["total"]
        status = "ok" if table not in missing else "missing"
        print(f"  {table:7s} {status:7s} {present}/{total} indexes")
    if planner_checked:
        planner_ok = len(planner_checked) - len(planner_misses)
        print(f"  planner ok      {planner_ok}/{len(planner_checked)} query plans")
        print(f"  planner paths   {', '.join(planner_checked)}")
    if require_planner:
        status = "ok" if planner_required_ok else "missing"
        print(
            f"  planner required {status:7s} "
            f"{len(planner_checked)}/{len(SPEED_QUERY_PLANS)} query plans"
        )
    if max_ms is not None:
        status = "ok" if latency_ok else "slow"
        print(f"  latency {status:7s} {elapsed_ms:g}/{max_ms:g} ms")
    if require_live_data:
        status = "ok" if live_data_ok else "missing"
        print(f"  live data {status:7s} {len(SPEED_LIVE_DATA_TABLES) - len(live_data_missing)}/{len(SPEED_LIVE_DATA_TABLES)} tables")
    if row_minimums:
        status = "ok" if row_minimums_ok else "missing"
        print(
            f"  row mins {status:7s} "
            f"{len(row_minimums) - len(row_minimum_misses)}/{len(row_minimums)} tables"
        )
    if output is not None:
        print(f"  report written  {output}")
        print(f"  report sha256   {report['report_sha256']}")

    if missing:
        print()
        print("Missing speed indexes:")
        for table in sorted(missing):
            for name in missing[table]:
                print(f"  {table}: {name}")
        return 1
    if planner_misses:
        print()
        print("Query plans not using expected speed indexes:")
        for name, expected_index, plan in planner_misses:
            print(f"  {name}: expected {expected_index}")
            print(f"    plan: {plan}")
        return 1
    if not planner_required_ok:
        print()
        print(
            "Planner checks incomplete: "
            f"{len(planner_checked)}/{len(SPEED_QUERY_PLANS)} plans ran"
        )
        return 1
    if not latency_ok:
        print()
        print(f"Speed-check exceeded latency budget: {elapsed_ms:g} ms > {max_ms:g} ms")
        return 1
    if not live_data_ok:
        print()
        print("Live-data proof incomplete:")
        for table in SPEED_LIVE_DATA_TABLES:
            if table in live_data_missing:
                print(f"  {table}: {live_data_missing[table]} rows")
        return 1
    if not row_minimums_ok:
        print()
        print("Row-minimum proof incomplete:")
        for table in sorted(row_minimum_misses):
            miss = row_minimum_misses[table]
            print(f"  {table}: {miss['actual']} rows < {miss['minimum']}")
        return 1

    print("All speed indexes present.")
    return 0


def _run_speed_verify(
    report_path: str,
    *,
    require_ok: bool = False,
    require_indexes: bool = False,
    require_planner: bool = False,
    require_planner_paths: bool = False,
    require_latency: bool = False,
    require_latency_consistency: bool = False,
    require_elapsed: bool = False,
    require_live_data: bool = False,
    require_live_data_consistency: bool = False,
    require_db_metadata: bool = False,
    require_row_counts: bool = False,
    require_row_minimums: bool = False,
    require_row_minimum_consistency: bool = False,
    require_schema_version: bool = False,
    require_package_version: bool = False,
    require_runtime: bool = False,
    require_platform: bool = False,
    max_age_min: float | None = None,
) -> int:
    path = Path(report_path).expanduser()
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(f"speed-verify: report not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"speed-verify: invalid JSON: {exc}")

    expected = payload.get("report_sha256")
    if not isinstance(expected, str) or not expected:
        print(f"speed-check report invalid: {path}")
        print("  missing report_sha256")
        return 1
    payload_without_digest = dict(payload)
    del payload_without_digest["report_sha256"]
    actual = _report_sha256(payload_without_digest)
    if actual != expected:
        print(f"speed-check report invalid: {path}")
        print(f"  expected {expected}")
        print(f"  actual   {actual}")
        return 1
    if require_ok and payload.get("ok") is not True:
        print(f"speed-check report failed: {path}")
        print("  ok is not true")
        return 1
    if require_schema_version and payload.get("schema_version") != SPEED_CHECK_REPORT_SCHEMA_VERSION:
        print(f"speed-check report failed: {path}")
        print(
            "  schema_version is not "
            f"{SPEED_CHECK_REPORT_SCHEMA_VERSION}"
        )
        return 1
    package = payload.get("package")
    package_ok = (
        isinstance(package, dict)
        and package.get("name") == "swarlo"
        and package.get("version") == __version__
    )
    if require_package_version and not package_ok:
        print(f"speed-check report failed: {path}")
        print(f"  package is not swarlo {__version__}")
        return 1
    indexes = payload.get("indexes")
    indexes_ok = isinstance(indexes, dict)
    if indexes_ok:
        for table in SPEED_INDEXES:
            table_indexes = indexes.get(table)
            if not isinstance(table_indexes, dict):
                indexes_ok = False
                break
            if table_indexes.get("present") != table_indexes.get("total"):
                indexes_ok = False
                break
            if table_indexes.get("missing") != []:
                indexes_ok = False
                break
    if require_indexes and not indexes_ok:
        print(f"speed-check report failed: {path}")
        print("  indexes are not complete")
        return 1
    runtime = payload.get("runtime")
    runtime_ok = (
        isinstance(runtime, dict)
        and runtime.get("python") == platform.python_version()
        and runtime.get("sqlite") == sqlite3.sqlite_version
    )
    if require_runtime and not runtime_ok:
        print(f"speed-check report failed: {path}")
        print(
            "  runtime is not "
            f"python {platform.python_version()}, sqlite {sqlite3.sqlite_version}"
        )
        return 1
    platform_ok = isinstance(runtime, dict) and runtime.get("platform") == platform.platform()
    if require_platform and not platform_ok:
        print(f"speed-check report failed: {path}")
        print(f"  platform is not {platform.platform()}")
        return 1
    generated_at = payload.get("generated_at")
    generated_dt: datetime | None = None
    if max_age_min is not None:
        if isinstance(generated_at, str):
            try:
                generated_dt = datetime.fromisoformat(
                    generated_at.replace("Z", "+00:00")
                )
            except ValueError:
                generated_dt = None
        if generated_dt is None:
            print(f"speed-check report failed: {path}")
            print("  generated_at is not a valid timestamp")
            return 1
        if generated_dt.tzinfo is None:
            generated_dt = generated_dt.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - generated_dt.astimezone(UTC)).total_seconds()
        if age_seconds > max_age_min * 60:
            print(f"speed-check report failed: {path}")
            print(f"  generated_at is older than {max_age_min:g} minutes")
            return 1
    planner = payload.get("planner")
    planner_required_ok = isinstance(planner, dict) and planner.get("required_ok") is True
    if require_planner and not planner_required_ok:
        print(f"speed-check report failed: {path}")
        print("  planner.required_ok is not true")
        return 1
    planner_paths_ok = (
        isinstance(planner, dict)
        and planner.get("expected_total") == len(SPEED_QUERY_PLANS)
        and planner.get("total") == len(SPEED_QUERY_PLANS)
        and planner.get("paths") == list(SPEED_QUERY_PLANS)
    )
    if require_planner_paths and not planner_paths_ok:
        print(f"speed-check report failed: {path}")
        print("  planner paths are not complete")
        return 1
    latency_budget = payload.get("latency_budget")
    latency_ok = isinstance(latency_budget, dict) and latency_budget.get("ok") is True
    if require_latency and not latency_ok:
        print(f"speed-check report failed: {path}")
        print("  latency_budget.ok is not true")
        return 1
    elapsed_ms = payload.get("elapsed_ms")
    elapsed_ok = isinstance(elapsed_ms, int | float) and not isinstance(elapsed_ms, bool) and elapsed_ms >= 0
    max_ms = latency_budget.get("max_ms") if isinstance(latency_budget, dict) else None
    max_ms_ok = isinstance(max_ms, int | float) and not isinstance(max_ms, bool) and max_ms > 0
    latency_consistency_ok = latency_ok and elapsed_ok and max_ms_ok and elapsed_ms <= max_ms
    if require_latency_consistency and not latency_consistency_ok:
        print(f"speed-check report failed: {path}")
        print("  latency budget is not consistent")
        return 1
    if require_elapsed and not elapsed_ok:
        print(f"speed-check report failed: {path}")
        print("  elapsed_ms is not valid")
        return 1
    live_data = payload.get("live_data")
    live_data_ok = isinstance(live_data, dict) and live_data.get("ok") is True
    if require_live_data and not live_data_ok:
        print(f"speed-check report failed: {path}")
        print("  live_data.ok is not true")
        return 1
    database = payload.get("database")
    db_metadata_ok = (
        isinstance(database, dict)
        and database.get("access") == "read_only"
        and isinstance(database.get("size_bytes"), int)
        and database.get("size_bytes") >= 0
        and isinstance(database.get("page_count"), int)
        and database.get("page_count") >= 0
        and isinstance(database.get("page_size"), int)
        and database.get("page_size") > 0
    )
    if require_db_metadata and not db_metadata_ok:
        print(f"speed-check report failed: {path}")
        print("  database metadata is not complete")
        return 1
    rows = database.get("rows") if isinstance(database, dict) else None
    row_counts_ok = isinstance(rows, dict)
    if row_counts_ok:
        for table in SPEED_INDEXES:
            count = rows.get(table)
            if not isinstance(count, int) or count < 0:
                row_counts_ok = False
                break
    if require_row_counts and not row_counts_ok:
        print(f"speed-check report failed: {path}")
        print("  database.rows are not complete")
        return 1
    live_data_consistency_ok = False
    if isinstance(live_data, dict) and isinstance(rows, dict):
        required_tables = live_data.get("required_tables")
        missing = live_data.get("missing")
        if isinstance(required_tables, list) and isinstance(missing, dict):
            expected_tables = list(SPEED_LIVE_DATA_TABLES)
            expected_missing = {
                table: rows.get(table)
                for table in SPEED_LIVE_DATA_TABLES
                if isinstance(rows.get(table), int) and rows.get(table) <= 0
            }
            live_data_consistency_ok = (
                required_tables == expected_tables
                and all(isinstance(rows.get(table), int) for table in SPEED_LIVE_DATA_TABLES)
                and missing == expected_missing
                and live_data.get("ok") is (not expected_missing)
            )
    if require_live_data_consistency and not live_data_consistency_ok:
        print(f"speed-check report failed: {path}")
        print("  live_data is not consistent")
        return 1
    row_minimums = payload.get("row_minimums")
    row_minimums_ok = isinstance(row_minimums, dict) and row_minimums.get("ok") is True
    if require_row_minimums and not row_minimums_ok:
        print(f"speed-check report failed: {path}")
        print("  row_minimums.ok is not true")
        return 1
    row_minimum_consistency_ok = False
    if isinstance(row_minimums, dict) and isinstance(rows, dict):
        required = row_minimums.get("required")
        misses = row_minimums.get("misses")
        if isinstance(required, dict) and isinstance(misses, dict):
            expected_misses = {
                table: {"actual": rows.get(table), "minimum": minimum}
                for table, minimum in required.items()
                if (
                    table in SPEED_INDEXES
                    and isinstance(minimum, int)
                    and minimum >= 0
                    and isinstance(rows.get(table), int)
                    and rows.get(table) < minimum
                )
            }
            row_minimum_consistency_ok = (
                all(
                    table in SPEED_INDEXES
                    and isinstance(minimum, int)
                    and minimum >= 0
                    and isinstance(rows.get(table), int)
                    for table, minimum in required.items()
                )
                and misses == expected_misses
                and row_minimums.get("ok") is (not expected_misses)
            )
    if require_row_minimum_consistency and not row_minimum_consistency_ok:
        print(f"speed-check report failed: {path}")
        print("  row_minimums are not consistent")
        return 1

    print(f"speed-check report verified: {path}")
    print(f"  report sha256 {expected}")
    if require_ok:
        print("  ok true")
    if require_schema_version:
        print(f"  schema version {SPEED_CHECK_REPORT_SCHEMA_VERSION}")
    if require_package_version:
        print(f"  package swarlo {__version__}")
    if require_indexes:
        print("  indexes complete true")
    if require_runtime:
        print(f"  runtime python {platform.python_version()}, sqlite {sqlite3.sqlite_version}")
    if require_platform:
        print(f"  platform {platform.platform()}")
    if max_age_min is not None:
        print(f"  generated_at fresh <= {max_age_min:g} minutes")
    if require_planner:
        print("  planner required true")
    if require_planner_paths:
        print("  planner paths complete true")
    if require_latency:
        print("  latency budget true")
    if require_latency_consistency:
        print("  latency consistency true")
    if require_elapsed:
        print("  elapsed_ms true")
    if require_live_data:
        print("  live data true")
    if require_live_data_consistency:
        print("  live data consistency true")
    if require_db_metadata:
        print("  database metadata true")
    if require_row_counts:
        print("  row counts complete true")
    if require_row_minimums:
        print("  row minimums true")
    if require_row_minimum_consistency:
        print("  row minimum consistency true")
    return 0


def _run_speed_proof_summary_verify(
    summary_path: str,
    *,
    require_ok: bool = False,
    require_report: bool = False,
    require_package_version: bool = False,
    require_report_schema_version: bool = False,
    require_report_consistency: bool = False,
    require_strict_live: bool = False,
    max_age_min: float | None = None,
) -> int:
    path = Path(summary_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"speed-proof-summary-verify: summary not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"speed-proof-summary-verify: invalid JSON: {exc}")

    expected = payload.get("summary_sha256")
    if not isinstance(expected, str) or not expected:
        print(f"speed-proof summary invalid: {path}")
        print("  missing summary_sha256")
        return 1
    payload_without_digest = dict(payload)
    del payload_without_digest["summary_sha256"]
    actual = _report_sha256(payload_without_digest)
    if actual != expected:
        print(f"speed-proof summary invalid: {path}")
        print(f"  expected {expected}")
        print(f"  actual   {actual}")
        return 1
    if payload.get("kind") != "speed-proof":
        print(f"speed-proof summary failed: {path}")
        print("  kind is not speed-proof")
        return 1
    if payload.get("schema_version") != SPEED_PROOF_SUMMARY_SCHEMA_VERSION:
        print(f"speed-proof summary failed: {path}")
        print(
            "  schema_version is not "
            f"{SPEED_PROOF_SUMMARY_SCHEMA_VERSION}"
        )
        return 1
    if require_ok and payload.get("ok") is not True:
        print(f"speed-proof summary failed: {path}")
        print("  ok is not true")
        return 1
    package = payload.get("package")
    package_ok = (
        isinstance(package, dict)
        and package.get("name") == "swarlo"
        and package.get("version") == __version__
    )
    if require_package_version and not package_ok:
        print(f"speed-proof summary failed: {path}")
        print(f"  package is not swarlo {__version__}")
        return 1
    if (
        require_report_schema_version
        and payload.get("report_schema_version") != SPEED_CHECK_REPORT_SCHEMA_VERSION
    ):
        print(f"speed-proof summary failed: {path}")
        print(
            "  report_schema_version is not "
            f"{SPEED_CHECK_REPORT_SCHEMA_VERSION}"
        )
        return 1
    generated_at = payload.get("generated_at")
    generated_dt: datetime | None = None
    if max_age_min is not None:
        if isinstance(generated_at, str):
            try:
                generated_dt = datetime.fromisoformat(
                    generated_at.replace("Z", "+00:00")
                )
            except ValueError:
                generated_dt = None
        if generated_dt is None:
            print(f"speed-proof summary failed: {path}")
            print("  generated_at is not a valid timestamp")
            return 1
        if generated_dt.tzinfo is None:
            generated_dt = generated_dt.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - generated_dt.astimezone(UTC)).total_seconds()
        if age_seconds > max_age_min * 60:
            print(f"speed-proof summary failed: {path}")
            print(f"  generated_at is older than {max_age_min:g} minutes")
            return 1
    if require_strict_live:
        gates = payload.get("gates")
        expected_gates = {
            "strict_live": True,
            "max_ms": 1000,
            "max_age_min": 5,
            "require_planner": True,
            "require_live_data": True,
            "min_rows": {"posts": 1000, "scores": 10000, "members": 1},
        }
        if gates != expected_gates:
            print(f"speed-proof summary failed: {path}")
            print("  gates are not strict-live")
            return 1
        strict_sections = {
            "latency_budget": payload.get("latency_budget"),
            "live_data": payload.get("live_data"),
            "row_minimums": payload.get("row_minimums"),
        }
        for name, section in strict_sections.items():
            if not isinstance(section, dict) or section.get("ok") is not True:
                print(f"speed-proof summary failed: {path}")
                print(f"  {name} is not true")
                return 1
        planner = payload.get("planner")
        planner_ok = (
            isinstance(planner, dict)
            and planner.get("required_ok") is True
            and planner.get("total") == planner.get("expected_total") == len(SPEED_QUERY_PLANS)
            and planner.get("ok") == len(SPEED_QUERY_PLANS)
        )
        if not planner_ok:
            print(f"speed-proof summary failed: {path}")
            print("  planner is not complete")
            return 1
    if require_report:
        report_path = payload.get("report")
        expected_report_sha = payload.get("report_sha256")
        if not isinstance(report_path, str) or not report_path:
            print(f"speed-proof summary failed: {path}")
            print("  report path is missing")
            return 1
        if not isinstance(expected_report_sha, str) or not expected_report_sha:
            print(f"speed-proof summary failed: {path}")
            print("  report_sha256 is missing")
            return 1
        report_file = Path(report_path).expanduser()
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"speed-proof summary failed: {path}")
            print(f"  report not found: {report_file}")
            return 1
        except json.JSONDecodeError as exc:
            print(f"speed-proof summary failed: {path}")
            print(f"  report invalid JSON: {exc}")
            return 1
        report_sha = report.get("report_sha256")
        if report_sha != expected_report_sha:
            print(f"speed-proof summary failed: {path}")
            print("  report_sha256 does not match summary")
            return 1
        report_without_digest = dict(report)
        report_without_digest.pop("report_sha256", None)
        if _report_sha256(report_without_digest) != expected_report_sha:
            print(f"speed-proof summary failed: {path}")
            print("  report digest is invalid")
            return 1
        report_package = report.get("package")
        report_package_ok = (
            isinstance(report_package, dict)
            and report_package.get("name") == "swarlo"
            and report_package.get("version") == __version__
        )
        if require_package_version and not report_package_ok:
            print(f"speed-proof summary failed: {path}")
            print(f"  report package is not swarlo {__version__}")
            return 1
        if require_report_consistency:
            report_consistency_fields = (
                "db",
                "generated_at",
                "database",
                "elapsed_ms",
                "latency_budget",
                "planner",
                "live_data",
                "row_minimums",
            )
            for field in report_consistency_fields:
                if payload.get(field) != report.get(field):
                    print(f"speed-proof summary failed: {path}")
                    print(f"  {field} does not match linked report")
                    return 1
        if (
            require_report_schema_version
            and report.get("schema_version") != SPEED_CHECK_REPORT_SCHEMA_VERSION
        ):
            print(f"speed-proof summary failed: {path}")
            print(
                "  report schema_version is not "
                f"{SPEED_CHECK_REPORT_SCHEMA_VERSION}"
            )
            return 1
        if require_strict_live:
            report_verify_code = _run_speed_verify(
                str(report_file),
                require_ok=True,
                require_indexes=True,
                require_schema_version=True,
                require_package_version=True,
                require_runtime=True,
                require_platform=True,
                max_age_min=max_age_min,
                require_planner=True,
                require_planner_paths=True,
                require_latency=True,
                require_latency_consistency=True,
                require_elapsed=True,
                require_live_data=True,
                require_live_data_consistency=True,
                require_db_metadata=True,
                require_row_counts=True,
                require_row_minimums=True,
                require_row_minimum_consistency=True,
            )
            if report_verify_code != 0:
                print(f"speed-proof summary failed: {path}")
                print("  linked report strict verification failed")
                return 1

    print(f"speed-proof summary verified: {path}")
    print(f"  summary sha256 {expected}")
    if require_ok:
        print("  ok true")
    if require_report:
        print("  report true")
    if require_package_version:
        print(f"  package swarlo {__version__}")
    if require_report_schema_version:
        print(f"  report schema version {SPEED_CHECK_REPORT_SCHEMA_VERSION}")
    if require_report_consistency:
        print("  report consistency true")
    if require_strict_live:
        print("  strict live true")
    if max_age_min is not None:
        print(f"  generated_at fresh <= {max_age_min:g} minutes")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Swarlo — agent coordination protocol")
    parser.add_argument("--version", action="version", version=f"swarlo {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the Swarlo server")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--db", default="swarlo.db", help="SQLite database path")
    serve.add_argument("--git-dir", default="swarlo.git", help="Bare git repo path for DAG layer")

    join = sub.add_parser("join", help="Register a member and save local config")
    join.add_argument("--server", required=True)
    join.add_argument("--hub", required=True)
    join.add_argument("--member-id", required=True)
    join.add_argument("--member-name")
    join.add_argument("--member-type", default="agent")

    read = sub.add_parser("read", help="Read a channel")
    read.add_argument("channel")
    read.add_argument("--limit", type=int, default=10)
    read.add_argument("--server")
    read.add_argument("--hub")
    read.add_argument("--api-key")

    claims = sub.add_parser("claims", help="List open claims")
    claims.add_argument("--channel")
    claims.add_argument("--server")
    claims.add_argument("--hub")
    claims.add_argument("--api-key")

    post = sub.add_parser("post", help="Post to a channel")
    post.add_argument("channel")
    post.add_argument("content")
    post.add_argument("--kind", default="message")
    post.add_argument("--task-key")
    post.add_argument("--server")
    post.add_argument("--hub")
    post.add_argument("--api-key")

    claim = sub.add_parser("claim", help="Claim a task")
    claim.add_argument("channel")
    claim.add_argument("task_key")
    claim.add_argument("content")
    claim.add_argument("--server")
    claim.add_argument("--hub")
    claim.add_argument("--api-key")

    report = sub.add_parser("report", help="Report task status")
    report.add_argument("channel")
    report.add_argument("task_key")
    report.add_argument("status", choices=["done", "failed", "blocked"])
    report.add_argument("content")
    report.add_argument("--server")
    report.add_argument("--hub")
    report.add_argument("--api-key")

    assign = sub.add_parser("assign", help="Push-assign a task to another member")
    assign.add_argument("channel")
    assign.add_argument("task_key")
    assign.add_argument("assignee_id")
    assign.add_argument("content")
    assign.add_argument("--priority", type=int, default=0,
                        help="0-5, higher is claimed first by claim-next")
    assign.add_argument("--depends-on", default=None,
                        help="comma-separated task keys this assignment waits on")
    assign.add_argument("--server")
    assign.add_argument("--hub")
    assign.add_argument("--api-key")

    touch = sub.add_parser("touch", help="Heartbeat an open claim so it isn't force-expired")
    touch.add_argument("channel")
    touch.add_argument("task_key")
    touch.add_argument("--server")
    touch.add_argument("--hub")
    touch.add_argument("--api-key")

    ping = sub.add_parser("ping", help="Lightweight check: anything new?")
    ping.add_argument("--member-id", help="Override member ID")
    ping.add_argument("--since", help="ISO timestamp watermark")
    ping.add_argument("--include", help="Comma-separated bundles, e.g. mine")
    ping.add_argument("--server")
    ping.add_argument("--hub")
    ping.add_argument("--api-key")

    mine = sub.add_parser("mine", help="What should I be working on?")
    mine.add_argument("--member-id", help="Override member ID")
    mine.add_argument("--server")
    mine.add_argument("--hub")
    mine.add_argument("--api-key")

    ready = sub.add_parser(
        "ready",
        help="Tasks assigned to me whose depends_on are all done",
        description="Subset of /mine that can be claimed right now (deps satisfied).",
    )
    ready.add_argument("--member-id", help="Override member ID")
    ready.add_argument("--server")
    ready.add_argument("--hub")
    ready.add_argument("--api-key")

    briefing = sub.add_parser(
        "briefing",
        help="Rank board posts by relevance to a task description",
        description="Task-guided board briefing (tfidf or regex scorer).",
    )
    briefing.add_argument("task", help="Task description to rank posts against")
    briefing.add_argument("--limit", type=int, default=15)
    briefing.add_argument("--scorer", choices=("tfidf", "regex"), default="tfidf")
    briefing.add_argument("--server")
    briefing.add_argument("--hub")
    briefing.add_argument("--api-key")

    summary = sub.add_parser(
        "summary",
        help="Print a plain-language board summary",
        description="Formatted hub summary (posts + open claims).",
    )
    summary.add_argument("--limit", type=int, default=10)
    summary.add_argument("--server")
    summary.add_argument("--hub")
    summary.add_argument("--api-key")

    members = sub.add_parser(
        "members",
        help="List hub members",
        description="List registered members in the hub.",
    )
    members.add_argument("--server")
    members.add_argument("--hub")
    members.add_argument("--api-key")

    channels = sub.add_parser(
        "channels",
        help="List hub channels",
        description="List available channels in the hub.",
    )
    channels.add_argument("--server")
    channels.add_argument("--hub")
    channels.add_argument("--api-key")

    handoff = sub.add_parser(
        "handoff",
        help="Show upstream handoff trail for a task (deps + their decisions/artifacts)",
    )
    handoff.add_argument("task_key")
    handoff.add_argument("--depth", type=int, default=3,
                         help="How many hops back to walk (capped at 10)")
    handoff.add_argument("--json", action="store_true",
                         help="Emit raw JSON instead of human-readable output")
    handoff.add_argument("--server")
    handoff.add_argument("--hub")
    handoff.add_argument("--api-key")

    score = sub.add_parser("score", help="Coordination score")
    score.add_argument("--explain", action="store_true", help="Print XP mechanics after the score")
    score.add_argument("--server")
    score.add_argument("--hub")
    score.add_argument("--api-key")
    tower = sub.add_parser(
        "tower",
        help="Show a calm local control tower for the hub",
        description="Show a calm local control tower: who is working, what needs an owner, what is blocked, and whether the local DB proof is healthy.",
    )
    tower.add_argument("--db", default="swarlo.db", help="SQLite database path")
    tower.add_argument("--hub", help="Hub to show; defaults to config or the busiest hub in the DB")
    tower.add_argument("--limit", type=int, default=5, help="Rows to show in each section")
    tower.add_argument("--stale-minutes", type=int, default=30, help="Claim age that counts as stale")
    tower.add_argument("--idle-minutes", type=int, default=15, help="Agent quiet time that counts as idle")
    tower.add_argument("--json", action="store_true", help="Emit the tower state as JSON")
    score_history = sub.add_parser(
        "score-history",
        help="Recent persisted coordination scores with score deltas",
        description="Recent persisted coordination scores with score deltas.",
    )
    score_history.add_argument("--limit", type=int, default=10)
    score_history.add_argument("--server")
    score_history.add_argument("--hub")
    score_history.add_argument("--api-key")
    xp = sub.add_parser("xp", help="Read-only per-agent XP leaderboard")
    xp.add_argument("--limit", type=int, default=20)
    xp.add_argument("--member", help="filter to one member_id")
    xp.add_argument("--explain", action="store_true", help="Print XP mechanics after the leaderboard")
    xp.add_argument("--server")
    xp.add_argument("--hub")
    xp.add_argument("--api-key")
    sub.add_parser("mechanics", help="Print XP mechanics without contacting the hub")
    speed_check = sub.add_parser(
        "speed-check",
        help="Verify the local SQLite DB has Swarlo read-speed indexes",
    )
    speed_check.add_argument("--db", default="swarlo.db", help="SQLite database path")
    speed_check.add_argument("--json", action="store_true", help="Emit a machine-readable report")
    speed_check.add_argument("--output", help="Write the machine-readable report to a file")
    speed_check.add_argument("--max-ms", type=_positive_float, help="Fail when the speed-check takes longer than this many milliseconds")
    speed_check.add_argument("--min-row", action="append", type=_row_minimum, default=[], metavar="TABLE=COUNT", help="Fail unless TABLE has at least COUNT rows; repeatable")
    speed_check.add_argument("--require-planner", action="store_true", help="Fail unless every representative planner check can run")
    speed_check.add_argument("--require-live-data", action="store_true", help="Fail unless members, posts, and scores have rows")
    speed_check.add_argument("--strict-live", action="store_true", help="Enable the live release speed-check gate set")
    speed_proof = sub.add_parser(
        "speed-proof",
        help="Run strict live speed-check and verify the saved receipt",
    )
    speed_proof.add_argument("--db", default="swarlo.db", help="SQLite database path")
    speed_proof.add_argument("--output", default="/tmp/swarlo-speed-proof.json", help="Write the speed proof receipt to this path")
    speed_proof.add_argument("--json", action="store_true", help="Emit a machine-readable proof summary")
    speed_proof.add_argument("--summary-output", help="Write the machine-readable proof summary to this path")
    speed_proof.add_argument("--max-ms", type=_positive_float, help="Override the strict live latency budget")
    speed_proof.add_argument("--max-age-min", type=_positive_float, default=5, help="Verifier freshness window in minutes")
    speed_proof.add_argument("--min-row", action="append", type=_row_minimum, default=[], metavar="TABLE=COUNT", help="Override or add row floors; repeatable")
    speed_verify = sub.add_parser(
        "speed-verify",
        help="Verify a saved speed-check JSON report digest",
    )
    speed_verify.add_argument("report", help="Path to a speed-check JSON report")
    speed_verify.add_argument("--require-ok", action="store_true", help="Fail unless the saved report has ok: true")
    speed_verify.add_argument("--require-schema-version", action="store_true", help=f"Fail unless the saved report uses schema_version {SPEED_CHECK_REPORT_SCHEMA_VERSION}")
    speed_verify.add_argument("--require-package-version", action="store_true", help=f"Fail unless the saved report was produced by swarlo {__version__}")
    speed_verify.add_argument("--require-indexes", action="store_true", help="Fail unless the saved report has all expected speed indexes")
    speed_verify.add_argument("--require-runtime", action="store_true", help="Fail unless the saved report uses this Python and SQLite runtime")
    speed_verify.add_argument("--require-platform", action="store_true", help="Fail unless the saved report uses this platform string")
    speed_verify.add_argument("--max-age-min", type=_positive_float, help="Fail unless generated_at is within this many minutes")
    speed_verify.add_argument("--require-planner", action="store_true", help="Fail unless the saved report has planner.required_ok: true")
    speed_verify.add_argument("--require-planner-paths", action="store_true", help="Fail unless the saved report has all expected planner path names")
    speed_verify.add_argument("--require-latency", action="store_true", help="Fail unless the saved report has latency_budget.ok: true")
    speed_verify.add_argument("--require-latency-consistency", action="store_true", help="Fail unless elapsed_ms is within a positive saved max_ms budget")
    speed_verify.add_argument("--require-elapsed", action="store_true", help="Fail unless the saved report has non-negative elapsed_ms")
    speed_verify.add_argument("--require-live-data", action="store_true", help="Fail unless the saved report has live_data.ok: true")
    speed_verify.add_argument("--require-live-data-consistency", action="store_true", help="Fail unless saved live-data results match saved row counts")
    speed_verify.add_argument("--require-db-metadata", action="store_true", help="Fail unless the saved report has read-only DB size/page metadata")
    speed_verify.add_argument("--require-row-counts", action="store_true", help="Fail unless the saved report has row counts for all speed-checked tables")
    speed_verify.add_argument("--require-row-minimums", action="store_true", help="Fail unless the saved report has row_minimums.ok: true")
    speed_verify.add_argument("--require-row-minimum-consistency", action="store_true", help="Fail unless saved row minimum results match saved row counts")
    speed_verify.add_argument("--strict-live", action="store_true", help="Enable the full live release receipt verifier gate set")
    speed_summary_verify = sub.add_parser(
        "speed-proof-summary-verify",
        help="Verify a saved speed-proof JSON summary digest",
    )
    speed_summary_verify.add_argument("summary", help="Path to a speed-proof JSON summary")
    speed_summary_verify.add_argument("--require-ok", action="store_true", help="Fail unless the saved summary has ok: true")
    speed_summary_verify.add_argument("--require-report", action="store_true", help="Fail unless the referenced receipt exists and matches report_sha256")
    speed_summary_verify.add_argument("--require-package-version", action="store_true", help=f"Fail unless the saved summary was produced by swarlo {__version__}")
    speed_summary_verify.add_argument("--require-report-schema-version", action="store_true", help=f"Fail unless the saved summary references report schema_version {SPEED_CHECK_REPORT_SCHEMA_VERSION}")
    speed_summary_verify.add_argument("--require-report-consistency", action="store_true", help="Fail unless copied summary fields match the linked receipt")
    speed_summary_verify.add_argument("--max-age-min", type=_positive_float, help="Fail unless generated_at is within this many minutes")
    speed_summary_verify.add_argument("--strict-live", action="store_true", help="Enable the live release summary verifier gate set")
    unclaimed = sub.add_parser(
        "unclaimed",
        help="List message tasks without a non-retracted claim or terminal report/status",
        description="List message tasks without a non-retracted claim or terminal report/status.",
    )
    unclaimed.add_argument("--limit", type=int, default=20)
    unclaimed.add_argument("--channel")
    unclaimed.add_argument("--server")
    unclaimed.add_argument("--hub")
    unclaimed.add_argument("--api-key")
    replay = sub.add_parser(
        "replay",
        help="Replay posts created after a timestamp (catch up on what you missed)",
        description="Replay posts created after an ISO8601 timestamp, oldest first.",
    )
    replay.add_argument("since", help="ISO8601 timestamp, e.g. 2026-07-10T00:00:00+00:00")
    replay.add_argument("--channel")
    replay.add_argument("--limit", type=int, default=200)
    replay.add_argument("--server")
    replay.add_argument("--hub")
    replay.add_argument("--api-key")

    remove = sub.add_parser(
        "remove",
        help="Remove one member from the hub by id (deletes the member)",
        description="Remove a single member from the hub by member id.",
    )
    remove.add_argument("member_id", help="Member id to remove")
    remove.add_argument("--server")
    remove.add_argument("--hub")
    remove.add_argument("--api-key")

    prune = sub.add_parser(
        "prune",
        help="Remove non-human members not seen in --stale-minutes (deletes members)",
        description="Remove non-human members whose last activity is older than --stale-minutes.",
    )
    prune.add_argument("--stale-minutes", type=int, default=60)
    prune.add_argument("--server")
    prune.add_argument("--hub")
    prune.add_argument("--api-key")

    liveness = sub.add_parser(
        "liveness",
        help="Show which agents are alive, dying, or dead (and orphaned claims)",
        description="Health view of the swarm. By default also expires stale "
                    "claims (the sweep GC); pass --no-expire to observe only.",
    )
    liveness.add_argument("--stale-minutes", type=int, default=30)
    liveness.add_argument("--no-expire", action="store_true",
                          help="observe only; do not expire stale claims")
    liveness.add_argument("--server")
    liveness.add_argument("--hub")
    liveness.add_argument("--api-key")

    expire = sub.add_parser(
        "expire",
        help="Force-expire claims stale beyond --stale-minutes (frees them to reclaim)",
        description="Expire open claims with no heartbeat older than --stale-minutes.",
    )
    expire.add_argument("--stale-minutes", type=int, default=30)
    expire.add_argument("--server")
    expire.add_argument("--hub")
    expire.add_argument("--api-key")

    retry = sub.add_parser(
        "retry",
        help="Re-queue failed tasks that haven't exhausted --max-retries",
        description="Re-open failed tasks for claiming, up to --max-retries attempts each.",
    )
    retry.add_argument("--max-retries", type=int, default=3)
    retry.add_argument("--server")
    retry.add_argument("--hub")
    retry.add_argument("--api-key")

    commits = sub.add_parser(
        "commits",
        help="List indexed commits in the shared git DAG (newest first)",
        description="List commits pushed to the hub's shared DAG, newest first.",
    )
    commits.add_argument("--member", help="only show commits from this member id")
    commits.add_argument("--limit", type=int, default=50)
    commits.add_argument("--server")
    commits.add_argument("--hub")
    commits.add_argument("--api-key")

    leaves = sub.add_parser(
        "leaves",
        help="List leaf commits (tips with no children) in the shared git DAG",
        description="Show the DAG tips — commits nothing else builds on yet.",
    )
    leaves.add_argument("--server")
    leaves.add_argument("--hub")
    leaves.add_argument("--api-key")

    lineage = sub.add_parser(
        "lineage",
        help="Walk a commit's ancestor chain back to root (newest first)",
        description="Print the ancestor chain from a commit hash back to root.",
    )
    lineage.add_argument("hash")
    lineage.add_argument("--server")
    lineage.add_argument("--hub")
    lineage.add_argument("--api-key")

    sub.add_parser("idle", help="Find idle agents").add_argument("--server")
    sub.add_parser("suggest", help="Auto-generate task suggestions").add_argument("--server")

    init = sub.add_parser("init", help="Enable Swarlo for this repo")

    install_hook = sub.add_parser(
        "install-hook",
        help="Install the swarlo pre-commit hook in this git repo",
    )
    install_hook.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing .git/hooks/pre-commit",
    )
    install_hook.add_argument(
        "--path", default=None,
        help="Target path for the hook (default: <repo>/.git/hooks/pre-commit)",
    )

    sub.add_parser(
        "doctor",
        help="Diagnose swarlo setup — config, server reachability, git hook, member registration",
    )

    return parser


# ── Doctor ──────────────────────────────────────────────────

# ANSI colors for doctor output. Fall back to empty strings when stdout
# is not a terminal (e.g. when captured in tests or piped).
def _colors() -> dict:
    if sys.stdout.isatty():
        return {
            "ok": "\033[32m",    # green
            "warn": "\033[33m",  # yellow
            "fail": "\033[31m",  # red
            "dim": "\033[2m",
            "reset": "\033[0m",
        }
    return {k: "" for k in ("ok", "warn", "fail", "dim", "reset")}


def _check(label: str, status: str, detail: str = "", colors: dict | None = None) -> None:
    """Print one doctor check line.

    status: 'ok' | 'warn' | 'fail'
    """
    c = colors or _colors()
    marks = {"ok": "✓", "warn": "!", "fail": "✗"}
    tag = f"{c[status]}{marks.get(status, '?')} {status.upper():<4}{c['reset']}"
    if detail:
        print(f"  {tag}  {label} {c['dim']}— {detail}{c['reset']}")
    else:
        print(f"  {tag}  {label}")


def _install_precommit_hook(repo_root: str, force: bool = False,
                            target_path: str | None = None,
                            quiet: bool = False) -> bool:
    """Install the swarlo pre-commit hook. Returns True if installed."""
    from swarlo._precommit_hook_source import SOURCE

    if target_path:
        target = Path(target_path).expanduser().resolve()
    else:
        target = Path(repo_root) / ".git" / "hooks" / "pre-commit"

    if target.exists() and not force:
        if not quiet:
            raise SystemExit(
                f"install-hook: {target} already exists. "
                "Pass --force to overwrite."
            )
        # quiet mode: skip silently (used by init)
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SOURCE)
    target.chmod(0o755)
    print(f"Installed swarlo pre-commit hook at {target}")
    return True


def _run_doctor() -> int:
    """Diagnose swarlo setup and print a per-check report.

    Returns 0 if every check is OK or WARN, 1 if any check is FAIL.
    Designed to be run by a human at any time — no side effects, no
    config writes, just reads and HTTP HEADs.

    Checks, in order:
      1. ~/.swarlo/config.json exists and parses as JSON
      2. Config has the required fields (server, hub, member_id, api_key)
      3. Server responds to /api/health
      4. Our member_id is registered on the server (listed in /members)
      5. Running inside a git repo (optional — only a WARN if not)
      6. Pre-commit hook is installed at .git/hooks/pre-commit
      7. The installed hook matches the canonical SOURCE
    """
    import subprocess
    from pathlib import Path

    colors = _colors()
    any_fail = False

    print()
    print(f"  {colors['dim']}swarlo doctor — diagnostics{colors['reset']}")
    print()

    # Check 1: config file exists and parses
    cfg_path = _config_path()
    config: dict = {}
    if not cfg_path.exists():
        _check(f"config file at {cfg_path}", "fail",
               "missing — run `swarlo join ...` first", colors)
        any_fail = True
    else:
        try:
            config = json.loads(cfg_path.read_text())
            _check(f"config file at {cfg_path}", "ok", "parsed OK", colors)
        except json.JSONDecodeError as exc:
            _check(f"config file at {cfg_path}", "fail",
                   f"invalid JSON: {exc}", colors)
            any_fail = True

    # Check 2: required fields present
    required = ["server", "hub", "member_id", "api_key"]
    missing = [k for k in required if not config.get(k)]
    if config and missing:
        _check("required config fields",
               "fail" if "server" in missing else "warn",
               f"missing: {', '.join(missing)}", colors)
        if "server" in missing:
            any_fail = True
    elif config:
        _check("required config fields", "ok",
               "server, hub, member_id, api_key present", colors)

    # Check 3: server is reachable
    server = config.get("server")
    server_ok = False
    if server:
        try:
            with urllib.request.urlopen(
                f"{server.rstrip('/')}/api/health", timeout=3
            ) as resp:
                body = json.loads(resp.read().decode())
                if body.get("status") == "ok":
                    _check(f"server health at {server}", "ok",
                           "responded with status=ok", colors)
                    server_ok = True
                else:
                    _check(f"server health at {server}", "warn",
                           f"unexpected body: {body}", colors)
        except Exception as exc:
            _check(f"server health at {server}", "fail",
                   f"unreachable: {exc}", colors)
            any_fail = True

    # Check 4: our member_id is registered
    hub = config.get("hub")
    member_id = config.get("member_id")
    api_key = config.get("api_key")
    if server_ok and hub and member_id and api_key:
        try:
            req = urllib.request.Request(
                f"{server.rstrip('/')}/api/{hub}/members",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                members = data.get("members") or []
                me = next((m for m in members if m.get("member_id") == member_id), None)
                if me:
                    _check(f"member_id '{member_id}' registered in hub '{hub}'",
                           "ok", f"member_name={me.get('member_name')}", colors)
                else:
                    _check(f"member_id '{member_id}' registered in hub '{hub}'",
                           "fail", "not found — run `swarlo join ...` again", colors)
                    any_fail = True
        except Exception as exc:
            _check(f"member lookup in hub '{hub}'", "warn",
                   f"could not verify: {exc}", colors)

    # Check 5: inside a git repo
    in_git = False
    repo_root: Path | None = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        repo_root = Path(result.stdout.strip())
        in_git = True
        _check(f"git repo at {repo_root}", "ok", "", colors)
    except (subprocess.CalledProcessError, FileNotFoundError):
        _check("git repo", "warn",
               "not inside a git repo — hook checks skipped", colors)

    # Check 6 + 7: pre-commit hook installed and matches canonical source
    if in_git and repo_root is not None:
        hook_path = repo_root / ".git" / "hooks" / "pre-commit"
        if not hook_path.exists():
            _check("pre-commit hook", "warn",
                   "not installed — run `swarlo install-hook`", colors)
        else:
            try:
                installed = hook_path.read_text()
            except Exception as exc:
                _check("pre-commit hook", "warn",
                       f"cannot read: {exc}", colors)
                installed = None

            if installed is not None:
                try:
                    from swarlo._precommit_hook_source import SOURCE
                    if installed == SOURCE:
                        _check("pre-commit hook matches canonical source",
                               "ok", str(hook_path), colors)
                    else:
                        _check("pre-commit hook matches canonical source",
                               "warn",
                               "drift detected — run `swarlo install-hook --force` to update",
                               colors)
                except ImportError:
                    _check("pre-commit hook canonical source",
                           "warn",
                           "could not import swarlo._precommit_hook_source",
                           colors)

    # Check 8: local control-tower DB has liveness columns (last_seen /
    # last_active). Pre-liveness DBs make tower crash and idle/liveness
    # lie. WARN only — opening the DB via serve migrates them.
    db_candidates: list[Path] = []
    if in_git and repo_root is not None:
        db_candidates.append(repo_root / "swarlo.db")
    db_candidates.append(Path.cwd() / "swarlo.db")
    seen_db: set[Path] = set()
    for db_path in db_candidates:
        try:
            resolved = db_path.resolve()
        except OSError:
            continue
        if resolved in seen_db or not resolved.is_file():
            continue
        seen_db.add(resolved)
        try:
            conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
            try:
                cols = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(members)").fetchall()
                }
            finally:
                conn.close()
        except sqlite3.Error as exc:
            _check(f"local DB schema at {resolved}", "warn",
                   f"unreadable: {exc}", colors)
            continue
        if not cols:
            _check(f"local DB schema at {resolved}", "warn",
                   "no members table — tower has nothing to show", colors)
            continue
        missing = [c for c in ("last_seen", "last_active") if c not in cols]
        if missing:
            _check(
                f"local DB liveness columns at {resolved}",
                "warn",
                f"missing {', '.join(missing)} — run `swarlo serve` once to migrate "
                "(tower stays up, but agents look offline until then)",
                colors,
            )
        else:
            _check(f"local DB liveness columns at {resolved}", "ok",
                   "last_seen, last_active present", colors)

    # Check 9: live server OpenAPI advertises CLI-critical routes.
    # A healthy /api/health is not enough if the hub is an older package.
    if server_ok and server:
        try:
            with urllib.request.urlopen(
                f"{server.rstrip('/')}/openapi.json", timeout=3
            ) as resp:
                openapi = json.loads(resp.read().decode())
            path_keys = set((openapi.get("paths") or {}).keys())
            missing_routes = []
            for needle, label in (
                ("/unclaimed", "GET /api/{hub}/unclaimed"),
                ("/xp", "GET /api/{hub}/xp"),
                ("/scores", "GET /api/{hub}/scores"),
                ("/handoff_trail/", "GET /api/{hub}/handoff_trail/{task_key}"),
            ):
                if not any(needle in p for p in path_keys):
                    missing_routes.append(label)
            if missing_routes:
                _check(
                    "server API surface for CLI",
                    "warn",
                    "missing "
                    + ", ".join(missing_routes)
                    + " — restart hub with current swarlo",
                    colors,
                )
            else:
                _check(
                    "server API surface for CLI",
                    "ok",
                    "unclaimed, xp, scores, handoff_trail present",
                    colors,
                )
        except Exception as exc:
            _check("server API surface for CLI", "warn",
                   f"could not read openapi.json: {exc}", colors)

    print()
    if any_fail:
        print(f"  {colors['fail']}At least one check failed.{colors['reset']} "
              f"Fix the issues above and re-run.")
        return 1
    print(f"  {colors['ok']}All checks passed.{colors['reset']}")
    return 0


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "init":
        import subprocess
        # Find project root
        try:
            root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            root = os.getcwd()

        # Write opt-in marker
        marker_dir = os.path.join(root, ".swarlo")
        os.makedirs(marker_dir, exist_ok=True)
        marker = os.path.join(marker_dir, "enabled.json")
        if not os.path.exists(marker):
            with open(marker, "w") as f:
                f.write('{"enabled": true}\n')
            print(f"Created {marker}")
        else:
            print(f"Already enabled: {marker}")

        # Write session-start hook
        hook_dir = os.path.join(root, ".claude", "hooks")
        os.makedirs(hook_dir, exist_ok=True)
        hook = os.path.join(hook_dir, "session-start.sh")
        if not os.path.exists(hook):
            hook_content = '''#!/bin/bash
# Swarlo activation — checks repo opt-in + user config + server health
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -z "$ROOT" ] && exit 0
[ ! -f "$ROOT/.swarlo/enabled.json" ] && exit 0
CONFIG="$HOME/.swarlo/config.json"
[ ! -f "$CONFIG" ] && exit 0
SERVER=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('server',''))" 2>/dev/null)
[ -z "$SERVER" ] && exit 0
HEALTH=$(curl -s --max-time 2 "$SERVER/api/health" 2>/dev/null)
if echo "$HEALTH" | grep -q "ok"; then
  MEMBER=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('member_name',''))" 2>/dev/null)
  HUB=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('hub',''))" 2>/dev/null)
  echo "[swarlo] active. member: $MEMBER, hub: $HUB. run: swarlo read general"
fi
'''
            with open(hook, "w") as f:
                f.write(hook_content)
            os.chmod(hook, 0o755)
            print(f"Created {hook}")
        else:
            print(f"Hook exists: {hook}")

        # Install pre-commit hook (skip if already present)
        _install_precommit_hook(root, force=False, quiet=True)

        # Run doctor to show setup status
        print()
        _run_doctor()

        if not os.path.exists(os.path.expanduser("~/.swarlo/config.json")):
            print()
            print("Next: run `swarlo join --server <url> --hub <hub> --member-id <id>` to connect.")
        return

    if args.command == "doctor":
        return _run_doctor()

    if args.command == "speed-check":
        strict_live = args.strict_live
        min_rows = (
            [("posts", 1000), ("scores", 10000), ("members", 1)] + args.min_row
            if strict_live
            else args.min_row
        )
        raise SystemExit(
            _run_speed_check(
                args.db,
                as_json=args.json,
                output_path=args.output,
                max_ms=args.max_ms if args.max_ms is not None else (1000 if strict_live else None),
                require_planner=args.require_planner or strict_live,
                require_live_data=args.require_live_data or strict_live,
                min_rows=min_rows,
            )
        )

    if args.command == "speed-proof":
        min_rows = [("posts", 1000), ("scores", 10000), ("members", 1)] + args.min_row
        max_ms = args.max_ms if args.max_ms is not None else 1000
        effective_min_rows = dict(min_rows)
        stdout_context = contextlib.redirect_stdout(io.StringIO()) if args.json else contextlib.nullcontext()
        error: str | None = None
        try:
            with stdout_context:
                check_code = _run_speed_check(
                    args.db,
                    output_path=args.output,
                    max_ms=max_ms,
                    require_planner=True,
                    require_live_data=True,
                    min_rows=min_rows,
                )
        except SystemExit as exc:
            if not (args.json or args.summary_output):
                raise
            check_code = exc.code if isinstance(exc.code, int) else 1
            error = str(exc.code) if exc.code else None
        verify_code: int | None = None
        if check_code == 0:
            try:
                with stdout_context:
                    verify_code = _run_speed_verify(
                        args.output,
                        require_ok=True,
                        require_indexes=True,
                        require_schema_version=True,
                        require_package_version=True,
                        require_runtime=True,
                        require_platform=True,
                        max_age_min=args.max_age_min,
                        require_planner=True,
                        require_planner_paths=True,
                        require_latency=True,
                        require_latency_consistency=True,
                        require_elapsed=True,
                        require_live_data=True,
                        require_live_data_consistency=True,
                        require_db_metadata=True,
                        require_row_counts=True,
                        require_row_minimums=True,
                        require_row_minimum_consistency=True,
                    )
            except SystemExit as exc:
                if not (args.json or args.summary_output):
                    raise
                verify_code = exc.code if isinstance(exc.code, int) else 1
                error = str(exc.code) if exc.code else None
        if args.json or args.summary_output:
            report_path = Path(args.output).expanduser()
            report: dict[str, object] = {}
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = {}
            exit_code = check_code if check_code != 0 else verify_code
            summary = {
                "schema_version": SPEED_PROOF_SUMMARY_SCHEMA_VERSION,
                "kind": "speed-proof",
                "ok": check_code == 0 and verify_code == 0,
                "check_ok": check_code == 0,
                "verify_ok": verify_code == 0,
                "check_code": check_code,
                "verify_code": verify_code,
                "exit_code": exit_code,
                "error": error,
                "gates": {
                    "strict_live": True,
                    "max_ms": max_ms,
                    "max_age_min": args.max_age_min,
                    "require_planner": True,
                    "require_live_data": True,
                    "min_rows": effective_min_rows,
                },
                "package": {"name": "swarlo", "version": __version__},
                "report_schema_version": report.get("schema_version"),
                "report": str(report_path),
                "db": report.get("db"),
                "database": report.get("database"),
                "generated_at": report.get("generated_at"),
                "report_sha256": report.get("report_sha256"),
                "elapsed_ms": report.get("elapsed_ms"),
                "latency_budget": report.get("latency_budget"),
                "live_data": report.get("live_data"),
                "row_minimums": report.get("row_minimums"),
                "planner": report.get("planner"),
            }
            summary["summary_sha256"] = _report_sha256(summary)
            summary_json = json.dumps(summary, indent=2, sort_keys=True) + "\n"
            if args.summary_output:
                summary_path = Path(args.summary_output).expanduser()
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_summary_path = summary_path.with_name(f".{summary_path.name}.tmp")
                tmp_summary_path.write_text(summary_json, encoding="utf-8")
                tmp_summary_path.replace(summary_path)
            if args.json:
                print(summary_json, end="")
        raise SystemExit(check_code if check_code != 0 else verify_code)

    if args.command == "speed-verify":
        strict_live = args.strict_live
        max_age_min = args.max_age_min if args.max_age_min is not None else (5 if strict_live else None)
        raise SystemExit(
            _run_speed_verify(
                args.report,
                require_ok=args.require_ok or strict_live,
                require_indexes=args.require_indexes or strict_live,
                require_schema_version=args.require_schema_version or strict_live,
                require_package_version=args.require_package_version or strict_live,
                require_runtime=args.require_runtime or strict_live,
                require_platform=args.require_platform or strict_live,
                max_age_min=max_age_min,
                require_planner=args.require_planner or strict_live,
                require_planner_paths=args.require_planner_paths or strict_live,
                require_latency=args.require_latency or strict_live,
                require_latency_consistency=args.require_latency_consistency or strict_live,
                require_elapsed=args.require_elapsed or strict_live,
                require_live_data=args.require_live_data or strict_live,
                require_live_data_consistency=args.require_live_data_consistency or strict_live,
                require_db_metadata=args.require_db_metadata or strict_live,
                require_row_counts=args.require_row_counts or strict_live,
                require_row_minimums=args.require_row_minimums or strict_live,
                require_row_minimum_consistency=args.require_row_minimum_consistency or strict_live,
            )
        )

    if args.command == "speed-proof-summary-verify":
        strict_live = args.strict_live
        max_age_min = args.max_age_min if args.max_age_min is not None else (5 if strict_live else None)
        raise SystemExit(
            _run_speed_proof_summary_verify(
                args.summary,
                require_ok=args.require_ok or strict_live,
                require_report=args.require_report or strict_live,
                require_package_version=args.require_package_version or strict_live,
                require_report_schema_version=args.require_report_schema_version or strict_live,
                require_report_consistency=args.require_report_consistency or strict_live,
                require_strict_live=strict_live,
                max_age_min=max_age_min,
            )
        )

    if args.command == "install-hook":
        import subprocess

        if not args.path:
            try:
                root = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
            except (subprocess.CalledProcessError, FileNotFoundError):
                raise SystemExit(
                    "install-hook: not inside a git repo. "
                    "cd into your repo, or pass --path explicitly."
                )
        else:
            root = os.getcwd()

        _install_precommit_hook(root, force=args.force, target_path=args.path)
        print()
        print("The hook blocks commits to files claimed by other agents.")
        print("It fail-opens if the swarlo server is unreachable or if")
        print("~/.swarlo/config.json has no api_key — so it's safe to")
        print("leave installed while you set things up.")
        print()
        print("Test it: `git commit --allow-empty -m test` should run the hook.")
        print("Bypass it: `git commit --no-verify`.")
        return

    if args.command == "serve":
        import uvicorn
        from .sqlite_backend import SQLiteBackend
        from .git_dag import GitDAG
        from .server import app, set_backend, set_dag

        set_backend(SQLiteBackend(args.db))
        dag = GitDAG(args.git_dir)
        dag.init()
        set_dag(dag)
        print(f"Swarlo server starting on {args.host}:{args.port}")
        print(f"Database: {args.db}")
        print(f"Git repo: {args.git_dir}")
        uvicorn.run(app, host=args.host, port=args.port)
        return

    if args.command == "join":
        status, body = _request(
            "POST",
            f"{args.server.rstrip('/')}/api/register",
            {
                "member_id": args.member_id,
                "member_type": args.member_type,
                "member_name": args.member_name or args.member_id,
                "hub_id": args.hub,
            },
        )
        if status not in (200, 201):
            raise SystemExit(f"Join failed ({status}): {body}")
        _save_config(
            {
                "server": args.server.rstrip("/"),
                "hub": args.hub,
                "api_key": body["api_key"],
                "member_id": body["member_id"],
                "member_name": args.member_name or args.member_id,
                "member_type": args.member_type,
            }
        )
        print(f"Joined hub `{args.hub}` as `{body['member_id']}`")
        print(f"Saved config to {_config_path()}")
        return

    if args.command == "read":
        runtime = _require_runtime(args)
        channel = urllib.parse.quote(args.channel, safe="")
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{channel}/posts?limit={args.limit}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Read failed ({status}): {body}")
        _print_posts(body.get("posts", []))
        return

    if args.command == "claims":
        runtime = _require_runtime(args)
        suffix = f"?{urllib.parse.urlencode({'channel': args.channel})}" if args.channel else ""
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/claims{suffix}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Claims failed ({status}): {body}")
        _print_claims(body.get("claims", []))
        return

    if args.command == "post":
        runtime = _require_runtime(args)
        channel = urllib.parse.quote(args.channel, safe="")
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{channel}/posts",
            {"content": args.content, "kind": args.kind, "task_key": args.task_key},
            api_key=runtime["api_key"],
        )
        if status not in (200, 201):
            raise SystemExit(f"Post failed ({status}): {body}")
        print(f"Posted [{body['kind']}] to #{body['channel']}")
        return

    if args.command == "claim":
        runtime = _require_runtime(args)
        channel = urllib.parse.quote(args.channel, safe="")
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{channel}/claim",
            {"task_key": args.task_key, "content": args.content},
            api_key=runtime["api_key"],
        )
        if status == 409:
            raise SystemExit(f"Claim conflict: {body}")
        if status not in (200, 201):
            raise SystemExit(f"Claim failed ({status}): {body}")
        print(f"Claimed {args.task_key} on #{args.channel}")
        return

    if args.command == "report":
        runtime = _require_runtime(args)
        channel = urllib.parse.quote(args.channel, safe="")
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{channel}/report",
            {"task_key": args.task_key, "status": args.status, "content": args.content},
            api_key=runtime["api_key"],
        )
        if status not in (200, 201):
            raise SystemExit(f"Report failed ({status}): {body}")
        print(f"Reported {args.status} for {args.task_key} on #{args.channel}")
        return

    if args.command == "assign":
        runtime = _require_runtime(args)
        channel = urllib.parse.quote(args.channel, safe="")
        payload = {
            "task_key": args.task_key,
            "assignee_id": args.assignee_id,
            "content": args.content,
        }
        if args.priority:
            payload["priority"] = args.priority
        if args.depends_on:
            payload["depends_on"] = [
                d.strip() for d in args.depends_on.split(",") if d.strip()
            ]
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{channel}/assign",
            payload,
            api_key=runtime["api_key"],
        )
        if status == 409:
            raise SystemExit(f"Assign conflict: {body}")
        if status not in (200, 201):
            raise SystemExit(f"Assign failed ({status}): {body}")
        print(f"Assigned {args.task_key} to {args.assignee_id} on #{args.channel}")
        return

    if args.command == "touch":
        runtime = _require_runtime(args)
        channel = urllib.parse.quote(args.channel, safe="")
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels/{channel}/touch",
            {"task_key": args.task_key},
            api_key=runtime["api_key"],
        )
        if status == 404:
            raise SystemExit(f"No open claim for {args.task_key} on #{args.channel}")
        if status not in (200, 201):
            raise SystemExit(f"Touch failed ({status}): {body}")
        print(f"Touched {args.task_key} on #{args.channel}")
        return

    if args.command == "mine":
        runtime = _require_runtime(args)
        member_id = args.member_id or runtime.get("member_id", "unknown")
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/mine/{urllib.parse.quote(member_id, safe='')}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Mine failed ({status}): {body}")
        if body.get("has_work"):
            for c in body["claims"]:
                print(f"  CLAIMED: {c['task_key']} — {c['content'][:60]}")
        else:
            print("No open work. Find something to do.")
        for a in body.get("assignments", []):
            print(f"  ASSIGNED: {a['task_key']} by {a.get('assigned_by','?')} — {a['content'][:60]}")
        return

    if args.command == "ready":
        runtime = _require_runtime(args)
        member_id = args.member_id or runtime.get("member_id")
        if not member_id:
            raise SystemExit("ready needs --member-id or a joined config member_id")
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/ready/"
            f"{urllib.parse.quote(member_id, safe='')}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Ready", status, body, route="/api/{hub}/ready/{member_id}")
        tasks = body.get("tasks") or []
        if not tasks:
            print("No ready tasks (deps not met or queue empty).")
            return
        print(f"{body.get('count', len(tasks))} ready task(s) for {member_id}:")
        for t in tasks:
            deps = t.get("depends_on") or []
            dep_s = f" deps={','.join(deps)}" if deps else ""
            print(f"  READY: {t.get('task_key')} — {(t.get('content') or '')[:60]}{dep_s}")
        return

    if args.command == "briefing":
        runtime = _require_runtime(args)
        task = (args.task or "").strip()
        if not task:
            raise SystemExit("briefing needs a task description")
        limit = _bounded_limit(args.limit, default=15)
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/briefing",
            {"task": task, "limit": limit, "scorer": args.scorer},
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Briefing", status, body, route="/api/{hub}/briefing")
        posts = body.get("posts") or body.get("results") or []
        if not posts:
            print("No ranked posts.")
            return
        print(f"Briefing for: {task[:80]}  (scorer={body.get('scorer', args.scorer)})")
        for i, post in enumerate(posts, start=1):
            score = post.get("score")
            score_s = f" score={score:.3f}" if isinstance(score, (int, float)) else ""
            key = post.get("task_key") or ""
            key_s = f" {key}" if key else ""
            print(
                f"  {i:>2}.{score_s}{key_s} "
                f"[{post.get('kind', '?')}] {(post.get('content') or '')[:70]}"
            )
        return

    if args.command == "summary":
        runtime = _require_runtime(args)
        limit = _bounded_limit(args.limit, default=10)
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/summary?limit={limit}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Summary", status, body, route="/api/{hub}/summary")
        text = body.get("summary") or ""
        if not text:
            print("Empty summary.")
            return
        print(text)
        return

    if args.command == "members":
        runtime = _require_runtime(args)
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/members",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Members", status, body, route="/api/{hub}/members")
        rows = body.get("members") or []
        if not rows:
            print("No members.")
            return
        print(f"{body.get('count', len(rows))} member(s):")
        for m in rows:
            print(
                f"  {m.get('member_id')}  {m.get('member_name')}  "
                f"type={m.get('member_type')}  last_seen={m.get('last_seen')}"
            )
        return

    if args.command == "channels":
        runtime = _require_runtime(args)
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/channels",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Channels", status, body, route="/api/{hub}/channels")
        rows = body.get("channels") or []
        if not rows:
            print("No channels.")
            return
        for ch in rows:
            print(f"  #{ch}" if not str(ch).startswith("#") else f"  {ch}")
        return

    if args.command in ("commits", "leaves", "lineage"):
        runtime = _require_runtime(args)
        base = f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}"
        if args.command == "commits":
            query = {"limit": max(1, min(int(args.limit), 200))}
            if args.member:
                query["member_filter"] = args.member
            url = f"{base}/git/commits?{urllib.parse.urlencode(query)}"
            route, label, empty = "/api/{hub}/git/commits", "Commits", "No commits."
        elif args.command == "leaves":
            url = f"{base}/git/leaves"
            route, label, empty = "/api/{hub}/git/leaves", "Leaves", "No leaf commits."
        else:  # lineage
            h = urllib.parse.quote(args.hash, safe="")
            url = f"{base}/git/commits/{h}/lineage"
            route, label, empty = "/api/{hub}/git/commits/{hash}/lineage", "Lineage", "No lineage."
        status, body = _request("GET", url, api_key=runtime["api_key"])
        if status != 200:
            _raise_http_failure(label, status, body, route=route)
        rows = body if isinstance(body, list) else (body.get("commits") or [])
        if not rows:
            print(empty)
            return
        print(f"{len(rows)} commit(s):")
        for c in rows:
            h = str(c.get("hash", ""))[:12]
            msg = (c.get("message") or "").splitlines()[0] if c.get("message") else ""
            print(f"  {h}  {c.get('member_name') or c.get('member_id') or '?'}  {msg}")
        return

    if args.command == "ping":
        runtime = _require_runtime(args)
        member_id = args.member_id or runtime.get("member_id", "unknown")
        member_path = urllib.parse.quote(member_id, safe="")
        query = {}
        if args.since:
            query["since"] = args.since
        if args.include:
            query["include"] = args.include
        suffix = ("?" + urllib.parse.urlencode(query)) if query else ""
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/ping/{member_path}{suffix}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Ping failed ({status}): {body}")
        if body.get("action_needed"):
            print(f"ACTION: {body['new_mentions']} mentions, {body['new_assigns']} assigns, {body['new_posts']} posts")
        else:
            print("Clear.")
        return

    if args.command == "handoff":
        runtime = _require_runtime(args)
        task_key = urllib.parse.quote(args.task_key, safe="")
        query = urllib.parse.urlencode({"depth": args.depth})
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/handoff_trail/"
            f"{task_key}?{query}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure(
                "Handoff trail",
                status,
                body,
                route=f"/api/{{hub}}/handoff_trail/{args.task_key}",
            )
        if args.json:
            print(json.dumps(body, indent=2))
            return
        trail = body.get("trail", [])
        if not trail:
            print(f"No handoff trail for {args.task_key} (depth={body.get('depth')}).")
            return
        print(f"Handoff trail for {args.task_key} (depth={body.get('depth')}, "
              f"{body.get('count')} hops):")
        for node in trail:
            handoff = node.get("handoff") or {}
            print()
            print(f"  hop {node['hop']} — {node['from']} (by {node['by']}, "
                  f"{node['at']})")
            arts = handoff.get("artifacts") or []
            decs = handoff.get("decisions") or []
            qs = handoff.get("open_questions") or []
            notes = handoff.get("notes")
            if arts:
                print(f"    artifacts: {', '.join(arts)}")
            if decs:
                for d in decs:
                    print(f"    decision:  {d}")
            if qs:
                for q in qs:
                    print(f"    open Q:    {q}")
            if notes:
                print(f"    notes:     {notes}")
            if not (arts or decs or qs or notes):
                print("    (no handoff recorded)")
        return

    if args.command == "tower":
        state = _build_tower_state(
            args.db,
            hub=args.hub,
            limit=_bounded_limit(args.limit, default=5, maximum=50),
            stale_minutes=max(1, int(args.stale_minutes)),
            idle_minutes=max(1, int(args.idle_minutes)),
        )
        if args.json:
            print(json.dumps(state, indent=2, sort_keys=True))
        else:
            _print_tower(state)
        return

    if args.command == "score":
        runtime = _require_runtime(args)
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/score",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Score failed ({status}): {body}")
        mttr = body.get('mttr_seconds')
        mttr_str = f"{mttr:.0f}s" if mttr else "n/a"
        rework = body.get('rework_rate', 0)
        tput = body.get('throughput_per_hour', 0)
        idle = body.get('idle_ratio', 0)
        print(f"Score: {body['coord_score']} | Shipped: {body['tasks_shipped']} | Active: {body['agents_active']} | Conflicts: {body['file_conflicts']}")
        print(f"  Throughput: {tput:.1f}/hr | MTTR: {mttr_str} | Rework: {rework:.1%} | Idle: {idle:.0%}")
        if body.get("per_agent_xp"):
            leader = body["per_agent_xp"][0]
            print(f"  XP leader: {leader['member_name']} ({leader['xp']} XP)")
        if body.get('tasks_failed'):
            print(f"  Failed: {body['tasks_failed']} | Blocked: {body.get('tasks_blocked', 0)}")
        if args.explain:
            _print_xp_mechanics()
        return

    if args.command == "score-history":
        runtime = _require_runtime(args)
        limit = _bounded_limit(args.limit, default=10)
        query = urllib.parse.urlencode({"limit": limit})
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/scores?{query}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure(
                "Score history",
                status,
                body,
                route="/api/{hub}/scores",
            )
        rows = body.get("scores") or []
        if not rows:
            print("No score history.")
            return
        print(f"{'when':19s}  {'score':>6}  {'Δ':>5}  {'ship':>5}  {'fail':>4}  {'block':>5}  {'tput':>5}")
        for idx, row in enumerate(rows):
            when = (row.get("computed_at") or "")[:19]
            tput = row.get("throughput_per_hour") or 0
            next_row = rows[idx + 1] if idx + 1 < len(rows) else None
            if next_row and row.get("coord_score") is not None and next_row.get("coord_score") is not None:
                delta = int(row.get("coord_score") or 0) - int(next_row.get("coord_score") or 0)
                delta_s = f"{delta:+d}" if delta else ""
            else:
                delta_s = ""
            print(
                f"{when:19s}  "
                f"{row.get('coord_score', 0):>6}  "
                f"{delta_s:>5}  "
                f"{row.get('tasks_shipped', 0):>5}  "
                f"{row.get('tasks_failed', 0):>4}  "
                f"{row.get('tasks_blocked', 0):>5}  "
                f"{tput:>5.1f}"
            )
        return

    if args.command == "xp":
        runtime = _require_runtime(args)
        limit = _bounded_limit(args.limit)
        query = {"limit": limit}
        if args.member:
            query["member_id"] = args.member
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/xp?{urllib.parse.urlencode(query)}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("XP", status, body, route="/api/{hub}/xp")
        rows = body.get("per_agent_xp") or []
        rows = rows[:limit]
        if not rows:
            target = f" for {args.member}" if args.member else ""
            print(f"No XP rows{target}.")
            if args.explain:
                _print_xp_mechanics()
            return
        print(f"{'rank':>4}  {'xp':>6}  {'ship':>4}  {'claim':>5}  {'fail':>4}  {'block':>5}  {'member_id':24s}  member")
        for i, row in enumerate(rows, start=1):
            print(
                f"{i:>4}  {row.get('xp', 0):>6}  "
                f"{row.get('shipped', 0):>4}  "
                f"{row.get('claims', 0):>5}  "
                f"{row.get('failed', 0):>4}  "
                f"{row.get('blocked', 0):>5}  "
                f"{row.get('member_id') or '':24s}  "
                f"{row.get('member_name') or row.get('member_id')}"
            )
        if args.explain:
            _print_xp_mechanics()
        return

    if args.command == "mechanics":
        _print_xp_mechanics()
        return

    if args.command == "unclaimed":
        runtime = _require_runtime(args)
        limit = _bounded_limit(args.limit)
        query = {"limit": limit}
        if args.channel:
            query["channel"] = args.channel
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/unclaimed?{urllib.parse.urlencode(query)}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure(
                "Unclaimed",
                status,
                body,
                route="/api/{hub}/unclaimed",
            )
        rows = (body.get("tasks") or [])[:limit]
        if not rows:
            print("No unclaimed tasks.")
            return
        print(f"{'created':19s}  {'channel':10s}  task")
        for row in rows:
            created = (row.get("created_at") or "")[:19]
            channel = (row.get("channel") or "")[:10]
            print(f"{created:19s}  {channel:10s}  {row.get('task_key')}: {row.get('content')}")
        return

    if args.command == "replay":
        runtime = _require_runtime(args)
        since = (args.since or "").strip()
        if not since:
            raise SystemExit("replay needs a since timestamp (ISO8601)")
        query = {"since": since, "limit": _bounded_limit(args.limit, default=200)}
        if args.channel:
            query["channel"] = args.channel
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/replay?{urllib.parse.urlencode(query)}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Replay", status, body, route="/api/{hub}/replay")
        posts = body.get("posts", [])
        if not posts:
            print(f"No posts since {since}.")
            return
        _print_posts(posts)
        return

    if args.command == "remove":
        runtime = _require_runtime(args)
        member_id = (args.member_id or "").strip()
        if not member_id:
            raise SystemExit("remove needs a member id")
        status, body = _request(
            "DELETE",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/members/{urllib.parse.quote(member_id, safe='')}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Remove", status, body, route="/api/{hub}/members/{member_id}")
        print(f"Removed member: {body.get('deleted', member_id)}")
        return

    if args.command == "prune":
        runtime = _require_runtime(args)
        stale_minutes = max(1, int(args.stale_minutes))
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/prune",
            payload={"stale_minutes": stale_minutes},
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Prune", status, body, route="/api/{hub}/prune")
        pruned = body.get("pruned", [])
        if not pruned:
            print(f"No members stale beyond {stale_minutes}m — nothing pruned.")
            return
        print(f"Pruned {len(pruned)} member(s) stale beyond {stale_minutes}m:")
        for member_id in pruned:
            print(f"  removed: {member_id}")
        return

    if args.command == "liveness":
        runtime = _require_runtime(args)
        stale_minutes = max(1, int(args.stale_minutes))
        auto_expire = "false" if args.no_expire else "true"
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/liveness"
            f"?stale_minutes={stale_minutes}&auto_expire={auto_expire}",
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Liveness", status, body, route="/api/{hub}/liveness")
        alive, dying, dead = body.get("alive", []), body.get("dying", []), body.get("dead", [])
        print(f"alive {len(alive)}  dying {len(dying)}  dead {len(dead)}")
        for a in dying:
            print(f"  DYING: {a['member_name']} (last seen {a.get('last_seen')})")
        for a in dead:
            print(f"  DEAD:  {a['member_name']} (last seen {a.get('last_seen')})")
        for c in body.get("orphaned_claims", []):
            print(f"  ORPHAN: {c['task_key']} held by {c['member_name']}")
        expired = body.get("expired_on_sweep", [])
        if expired:
            print(f"  expired on sweep: {', '.join(expired)}")
        print(body.get("recommendation", ""))
        return

    if args.command == "expire":
        runtime = _require_runtime(args)
        stale_minutes = max(1, int(args.stale_minutes))
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/claims/expire",
            {"stale_minutes": stale_minutes},
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Expire", status, body, route="/api/{hub}/claims/expire")
        expired = body.get("expired", [])
        if expired:
            for key in expired:
                print(f"  EXPIRED: {key}")
        print(f"Expired {body.get('count', len(expired))} stale claim(s).")
        return

    if args.command == "retry":
        runtime = _require_runtime(args)
        # Clamp to >=0, mirroring the server's max(0, ...) coercion so a
        # negative value can't be sent as a bogus retry cap.
        max_retries = max(0, int(args.max_retries))
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/claims/retry",
            {"max_retries": max_retries},
            api_key=runtime["api_key"],
        )
        if status != 200:
            _raise_http_failure("Retry", status, body, route="/api/{hub}/claims/retry")
        retried = body.get("retried", [])
        if retried:
            for key in retried:
                print(f"  RETRY: {key}")
        print(f"Re-queued {body.get('count', len(retried))} failed task(s).")
        return

    if args.command == "idle":
        runtime = _require_runtime(args)
        status, body = _request(
            "GET",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/idle",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Idle failed ({status}): {body}")
        if body["idle"]:
            for a in body["idle"]:
                print(f"  IDLE: {a['member_name']}")
        else:
            print("All agents producing.")
        return

    if args.command == "suggest":
        runtime = _require_runtime(args)
        status, body = _request(
            "POST",
            f"{runtime['server'].rstrip('/')}/api/{runtime['hub']}/suggest",
            api_key=runtime["api_key"],
        )
        if status != 200:
            raise SystemExit(f"Suggest failed ({status}): {body}")
        for s in body.get("suggestions", []):
            print(f"  {s['reason']}")
            print(f"    → {s['suggestion']}")
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
