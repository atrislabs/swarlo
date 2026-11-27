"""Fail-first adversarial hardening regression tests covering all 6 P0/P1 fixes."""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from swarlo import gc
from swarlo.client import SwarloClient, SwarloError
from swarlo.git_dag import GitDAG
from swarlo.server import (
    _hub_dags,
    _is_safe_webhook_url,
    _post_attempts,
    _task_post_attempts,
    app,
    get_dag,
    set_backend,
    set_dag,
)
from swarlo.sqlite_backend import SQLiteBackend
from swarlo.types import Member


@pytest.fixture(autouse=True)
def fresh_env():
    _post_attempts.clear()
    _task_post_attempts.clear()
    _hub_dags.clear()
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    backend = SQLiteBackend(db_path)
    set_backend(backend)
    git_dir = tempfile.mkdtemp(suffix=".git")
    dag = GitDAG(git_dir)
    dag.init()
    set_dag(dag)
    yield backend
    backend.close()
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass
    shutil.rmtree(git_dir, ignore_errors=True)


@pytest.fixture
def client():
    return TestClient(app)


def _auth(key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


def _register(
    client: TestClient,
    member_id: str = "agent-1",
    name: str = "Agent 1",
    hub: str = "atris",
    member_type: str = "agent",
    **extra,
) -> str:
    sponsors = getattr(client, "_test_hub_sponsors", {})
    sponsor_key = sponsors.get(hub)
    payload = {
        "member_id": member_id,
        "member_type": member_type,
        "member_name": name,
        "hub_id": hub,
        **extra,
    }
    resp = client.post(
        "/api/register",
        headers=_auth(sponsor_key) if sponsor_key else {},
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    key = resp.json()["api_key"]
    sponsors.setdefault(hub, key)
    client._test_hub_sponsors = sponsors
    return key


def _make_git_bundle(tmp_path: Path) -> tuple[bytes, str, str]:
    """Create a 2-commit repo and return (bundle_bytes, root_hash, tip_hash)."""
    repo = tmp_path / "src_repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@test.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], capture_output=True, check=True)

    (repo / "a.txt").write_text("v1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "root commit"], capture_output=True, check=True)
    h1 = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    (repo / "a.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "tip commit"], capture_output=True, check=True)
    h2 = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    bundle_file = tmp_path / "two.bundle"
    branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "bundle", "create", str(bundle_file), branch], capture_output=True, check=True)
    return bundle_file.read_bytes(), h1, h2


# ── Bug 1: Cross-hub commit metadata collision ─────────────────────────────


class TestBug1CrossHubCommitCollision:
    def test_same_commit_hash_pushed_to_two_hubs_is_visible_in_both(self, client, tmp_path):
        key_a = _register(client, "worker", "WorkerA", hub="hub-a")
        key_b = _register(client, "worker", "WorkerB", hub="hub-b")
        bundle, h1, h2 = _make_git_bundle(tmp_path)

        # Also push root commit first so lineage has both nodes indexed
        root_bundle_file = tmp_path / "root.bundle"
        repo = tmp_path / "src_repo"
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/rootonly", h1], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "bundle", "create", str(root_bundle_file), "refs/heads/rootonly"], capture_output=True, check=True)
        root_bundle = root_bundle_file.read_bytes()

        for hub, key in (("hub-a", key_a), ("hub-b", key_b)):
            r1 = client.post(f"/api/{hub}/git/push", headers=_auth(key), content=root_bundle)
            assert r1.status_code == 201
            r2 = client.post(f"/api/{hub}/git/push", headers=_auth(key), content=bundle)
            assert r2.status_code == 201

        for hub, key in (("hub-a", key_a), ("hub-b", key_b)):
            get_h2 = client.get(f"/api/{hub}/git/commits/{h2}", headers=_auth(key))
            assert get_h2.status_code == 200, f"{hub} missing commit {h2}"
            assert get_h2.json()["hub_id"] == hub

            commits = client.get(f"/api/{hub}/git/commits", headers=_auth(key)).json()
            assert {c["hash"] for c in commits} >= {h1, h2}

            leaves = client.get(f"/api/{hub}/git/leaves", headers=_auth(key)).json()
            assert [c["hash"] for c in leaves] == [h2]

            lineage = client.get(f"/api/{hub}/git/commits/{h2}/lineage", headers=_auth(key)).json()
            assert [c["hash"] for c in lineage] == [h2, h1]

    def test_legacy_commits_primary_key_migration_is_idempotent_and_preserves_rows(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE commits (
                    hash TEXT PRIMARY KEY,
                    parent_hash TEXT,
                    hub_id TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    member_name TEXT NOT NULL,
                    message TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO commits VALUES ('abc12345', NULL, 'hub-a', 'm1', 'Alice', 'init', '2026-01-01T00:00:00Z')"
            )

        be1 = SQLiteBackend(str(db_path))
        try:
            assert be1.get_commit("hub-a", "abc12345")["message"] == "init"
            be1.index_commit("hub-b", "abc12345", "", "m2", "Bob", "same-hash")
            assert be1.get_commit("hub-b", "abc12345")["message"] == "same-hash"
            assert be1.get_commit("hub-a", "abc12345")["message"] == "init"
        finally:
            be1.close()

        # Re-opening must be idempotent and keep both rows
        be2 = SQLiteBackend(str(db_path))
        try:
            assert be2.get_commit("hub-a", "abc12345")["message"] == "init"
            assert be2.get_commit("hub-b", "abc12345")["message"] == "same-hash"
        finally:
            be2.close()


# ── Bug 2: Enumeration oracle, bounds, rate limits, SSRF ───────────────────


class TestBug2AuthOracleBoundsAndSSRF:
    def test_unauthenticated_register_cannot_enumerate_existing_members(self, client):
        owner_key = _register(client, "secret-owner", "Owner", hub="private-hub")

        probe_existing = client.post("/api/register", json={
            "hub_id": "private-hub",
            "member_id": "secret-owner",
            "member_type": "agent",
        })
        probe_missing = client.post("/api/register", json={
            "hub_id": "private-hub",
            "member_id": "definitely-not-there",
            "member_type": "agent",
        })
        # Unauthorized callers must receive identical 403 for both
        assert probe_existing.status_code == 403
        assert probe_missing.status_code == 403

        # Authorized caller with valid invite still gets 409 WITHOUT consuming invite
        inv = client.post("/api/private-hub/invites", headers=_auth(owner_key), json={}).json()["invite_token"]
        dup = client.post("/api/register", json={
            "hub_id": "private-hub",
            "member_id": "secret-owner",
            "member_type": "agent",
            "invite_token": inv,
        })
        assert dup.status_code == 409

        valid_join = client.post("/api/register", json={
            "hub_id": "private-hub",
            "member_id": "new-member",
            "member_type": "agent",
            "invite_token": inv,
        })
        assert valid_join.status_code == 201

    @pytest.mark.parametrize("bad_val", [
        "",
        "   ",
        "a" * 129,
        "../escape",
        "foo/bar",
        "foo\\bar",
        "bad\x00id",
        "bad\nid",
        ".",
        "..",
        "a..b",
    ])
    def test_register_rejects_malformed_hub_id_and_member_id(self, client, bad_val):
        r1 = client.post("/api/register", json={"hub_id": bad_val, "member_id": "ok-member"})
        assert r1.status_code == 422
        r2 = client.post("/api/register", json={"hub_id": "ok-hub", "member_id": bad_val})
        assert r2.status_code == 422

    def test_register_endpoint_is_rate_limited(self, client):
        statuses = []
        for i in range(130):
            resp = client.post("/api/register", json={
                "hub_id": f"spam-hub-{i}",
                "member_id": "bot",
            })
            statuses.append(resp.status_code)
            if resp.status_code == 429:
                break
        assert 429 in statuses, "Expected /api/register to enforce rate limiting with 429"

    def test_hub_dags_and_rate_limit_caches_are_bounded(self, monkeypatch):
        from swarlo import server

        for i in range(300):
            get_dag(f"bounded-hub-{i}")
        assert len(_hub_dags) <= 256

        monkeypatch.setattr(server, "MAX_RATE_LIMIT_BUCKETS", 20)
        for i in range(50):
            server._enforce_post_rate_limit("h", f"m-{i}", f"t-{i}", "status")
            server._enforce_register_rate_limit(f"10.0.0.{i}")
        assert len(server._post_attempts) <= 20
        assert len(server._task_post_attempts) <= 20
        assert len(server._register_attempts) <= 20

    @pytest.mark.parametrize("bad_url", [
        "https://example.com:99999/hook",
        "http://localhost:99999/hook",
        "https://example.com:0/hook",
    ])
    def test_webhook_rejects_invalid_ports_at_registration(self, client, bad_url):
        assert not _is_safe_webhook_url(bad_url)
        resp = client.post("/api/register", json={
            "hub_id": "port-test",
            "member_id": "a1",
            "webhook_url": bad_url,
        })
        assert resp.status_code == 400

    def test_webhook_blocks_localhost_when_env_disabled_or_non_loopback_bind(self, client, monkeypatch):
        from swarlo import server

        monkeypatch.setenv("SWARLO_ALLOW_LOCALHOST_WEBHOOKS", "0")
        for url in (
            "http://127.0.0.1:8080/hook",
            "http://localhost:8080/hook",
            "http://[::1]:8080/hook",
            "https://127.0.0.1/hook",
            "https://localhost/hook",
        ):
            assert not _is_safe_webhook_url(url), f"Should block {url} when SWARLO_ALLOW_LOCALHOST_WEBHOOKS=0"
            resp = client.post("/api/register", json={
                "hub_id": "ssrf-hub",
                "member_id": "u1",
                "webhook_url": url,
            })
            assert resp.status_code == 400

        # Non-loopback bind blocks localhost by default when env unset
        monkeypatch.delenv("SWARLO_ALLOW_LOCALHOST_WEBHOOKS", raising=False)
        prev_host = server._server_bind_host
        server.set_bind_host("0.0.0.0")
        try:
            assert not _is_safe_webhook_url("http://127.0.0.1:8080/hook")
            assert not _is_safe_webhook_url("http://localhost:8080/hook")
        finally:
            server.set_bind_host(prev_host)

    def test_webhook_dispatch_disables_redirects_and_uses_async_dns(self, fresh_env, monkeypatch):
        from swarlo import server
        from swarlo.types import Post

        hits: list[str] = []

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/secret-internal")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), RedirectHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            monkeypatch.setenv("SWARLO_ALLOW_LOCALHOST_WEBHOOKS", "1")
            fresh_env.register_member(
                Member("target", "agent", "Target", "wh-hub", webhook_url=f"http://127.0.0.1:{port}/start"),
                api_key="k1",
            )
            post = Post(
                post_id="p1", content="hi @Target", kind="message", channel="general",
                member_id="sender", member_name="Sender", member_type="agent", mentions=["target"],
            )
            client_kwargs: list[dict] = []
            real_async_client = httpx.AsyncClient

            def spy_client(*args, **kwargs):
                client_kwargs.append(kwargs)
                return real_async_client(*args, **kwargs)

            monkeypatch.setattr(server.httpx, "AsyncClient", spy_client)
            asyncio.run(server._dispatch_webhooks("wh-hub", post))

            assert client_kwargs and client_kwargs[0].get("follow_redirects") is False
            assert "/start" in hits
            assert "/secret-internal" not in hits

            # Verify external webhook DNS resolution runs via asyncio.to_thread
            to_thread_calls: list[object] = []
            real_to_thread = asyncio.to_thread

            async def spy_to_thread(fn, *args, **kwargs):
                to_thread_calls.append(fn)
                if fn is socket.getaddrinfo:
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
                return await real_to_thread(fn, *args, **kwargs)

            monkeypatch.setattr(server.asyncio, "to_thread", spy_to_thread)
            ok = asyncio.run(server._is_safe_webhook_url_async("https://example.com/hook", at_dispatch=True))
            assert ok is True
            assert socket.getaddrinfo in to_thread_calls
        finally:
            httpd.shutdown()


# ── Bug 3: Freeform /posts spoofing & dependency graph poisoning ───────────


class TestBug3FreeformSpoofingAndDepPoisoning:
    def test_freeform_result_and_blocked_do_not_hide_unclaimed_tasks(self, client):
        key = _register(client, "worker", "Worker")
        h = _auth(key)
        client.post("/api/atris/channels/ops/posts", headers=h, json={"content": "Task 1", "task_key": "task:u1"})
        client.post("/api/atris/channels/ops/posts", headers=h, json={"content": "Task 2", "task_key": "task:u2"})

        # Spoof freeform result & blocked posts (status is NULL)
        client.post("/api/atris/channels/ops/posts", headers=h, json={"content": "fake done", "kind": "result", "task_key": "task:u1"})
        client.post("/api/atris/channels/ops/posts", headers=h, json={"content": "fake blocked", "kind": "blocked", "task_key": "task:u2"})

        unclaimed = client.get("/api/atris/unclaimed", headers=h).json()
        keys = {t["task_key"] for t in unclaimed["tasks"]}
        assert "task:u1" in keys
        assert "task:u2" in keys

    def test_freeform_posts_do_not_inflate_score_or_xp(self, client):
        key = _register(client, "farmer", "Farmer")
        h = _auth(key)
        client.post("/api/atris/channels/ops/posts", headers=h, json={"content": "fake claim", "kind": "claim", "task_key": "task:s1"})
        client.post("/api/atris/channels/ops/posts", headers=h, json={"content": "fake result", "kind": "result", "task_key": "task:s1"})
        client.post("/api/atris/channels/ops/posts", headers=h, json={"content": "fake failed", "kind": "failed", "task_key": "task:s2"})
        client.post("/api/atris/channels/ops/posts", headers=h, json={"content": "fake blocked", "kind": "blocked", "task_key": "task:s3"})

        score = client.post("/api/atris/score", headers=h).json()
        assert score["tasks_shipped"] == 0

        xp_rows = client.get("/api/atris/xp", headers=h).json()["per_agent_xp"]
        farmer_row = next((r for r in xp_rows if r["member_id"] == "farmer"), None)
        assert farmer_row is None or (farmer_row["xp"] == 0 and farmer_row["shipped"] == 0 and farmer_row["claims"] == 0)

    def test_wait_for_ignores_freeform_result_without_authoritative_status(self, monkeypatch, tmp_path):
        # Client wait_for
        c = SwarloClient("http://localhost:9999", hub="h", api_key="k")
        monkeypatch.setattr(c, "read", lambda ch, limit=50: [
            {"task_key": "T1", "kind": "result", "status": None, "content": "spoofed"},
            {"task_key": "T1", "kind": "failed", "status": None, "content": "spoofed fail"},
        ])
        with pytest.raises(SwarloError) as exc:
            c.wait_for("T1", timeout=0.05, poll_interval=0.01)
        assert exc.value.status_code == 408

        # CLI wait-for
        from swarlo import __main__ as cli
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"server": "http://localhost:9999", "hub": "h", "api_key": "k"}))
        monkeypatch.setenv("SWARLO_CONFIG", str(cfg))
        monkeypatch.setattr(cli, "_request", lambda *_a, **_kw: (200, {"posts": [
            {"task_key": "T1", "kind": "result", "status": None, "content": "spoofed"},
        ]}))
        monkeypatch.setattr(sys, "argv", ["swarlo", "wait-for", "T1", "--timeout", "0.1", "--poll-interval", "0.02"])
        with pytest.raises(SystemExit) as cli_exc:
            cli.main()
        assert "timed out" in str(cli_exc.value)

    def test_freeform_post_cannot_poison_dependency_graph_or_handoff_trail(self, client):
        worker_key = _register(client, "worker", "Worker")
        attacker_key = _register(client, "attacker", "Attacker")
        wh, ah = _auth(worker_key), _auth(attacker_key)

        # Complete task:A legitimately with handoff
        assert client.post("/api/atris/channels/ops/claim", headers=wh, json={"task_key": "task:A", "content": "work A"}).status_code == 201
        assert client.post("/api/atris/channels/ops/report", headers=wh, json={
            "task_key": "task:A", "status": "done", "content": "done A",
            "metadata": {"handoff": {"artifacts": ["a.py"], "decisions": [], "open_questions": []}},
        }).status_code == 201

        # Attacker posts freeform messages trying to inject reverse edge task:A -> task:B
        for kind in ("message", "claim", "assign"):
            client.post("/api/atris/channels/ops/posts", headers=ah, json={
                "content": f"poison {kind}", "kind": kind, "task_key": "task:A", "depends_on": ["task:B"],
            })

        # Worker claiming task:B depending on task:A MUST NOT hit a fake cycle!
        claim_b = client.post("/api/atris/channels/ops/claim", headers=wh, json={
            "task_key": "task:B", "content": "work B", "depends_on": ["task:A"],
        })
        assert claim_b.status_code == 201, claim_b.text

        # Attacker tries to overwrite task:B's depends_on with freeform post to poison handoff_trail
        client.post("/api/atris/channels/ops/posts", headers=ah, json={
            "content": "poison trail", "kind": "message", "task_key": "task:B", "depends_on": ["task:evil"],
        })
        trail = client.get("/api/atris/handoff_trail/task:B", headers=wh).json()
        assert trail["count"] == 1
        assert trail["trail"][0]["from"] == "task:A"

    def test_create_post_rejects_dependency_cycles(self, client):
        op_key = _register(client, "op", "Op", member_type="human")
        _register(client, "worker", "Worker")
        oh = _auth(op_key)

        # Self-loop rejected on /posts (including whitespace-padded task_key)
        r_self = client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "self loop", "task_key": "  task:self  ", "depends_on": ["task:self"],
        })
        assert r_self.status_code == 400

        # 2-hop cycle against authoritative assign rejected on /posts
        assert client.post("/api/atris/channels/ops/assign", headers=oh, json={
            "task_key": "task:X", "assignee_id": "worker", "content": "do X", "depends_on": ["task:Y"],
        }).status_code == 201
        r_cycle = client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "closing cycle", "task_key": "task:Y", "depends_on": ["task:X"],
        })
        assert r_cycle.status_code == 400


# ── Bug 4: Key hashing, 0600 permissions, GC safety ────────────────────────


class TestBug4KeyHashingPermissionsAndGC:
    def test_drop_idle_members_never_deletes_humans_or_unseen_members(self, tmp_path):
        db_path = tmp_path / "gc.db"
        be = SQLiteBackend(str(db_path))
        be.conn
        be.close()

        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at, last_seen) "
                "VALUES (?, 'h1', ?, 'name', '2026-01-01T00:00:00Z', ?)",
                [
                    ("stale-human", "human", old),
                    ("unseen-human", "human", None),
                    ("unseen-agent", "agent", None),
                    ("stale-agent", "agent", old),
                    ("fresh-agent", "agent", fresh),
                ],
            )

        dropped = gc.drop_idle_members(db_path, "h1", days=30)
        assert dropped == ["stale-agent"]
        with sqlite3.connect(db_path) as conn:
            remaining = {r[0] for r in conn.execute("SELECT member_id FROM members WHERE hub_id = 'h1'")}
        assert remaining == {"stale-human", "unseen-human", "unseen-agent", "fresh-agent"}

    def test_archive_posts_blocks_traversal_in_month_and_hub(self, tmp_path):
        db_path = tmp_path / "gc_trav.db"
        be = SQLiteBackend(str(db_path))
        be.conn
        be.close()

        arc_dir = tmp_path / "archives"
        # Bad month via corrupt created_at
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, content, kind, created_at) "
                "VALUES ('p1', 'safe-hub', 'general', 'm1', 'M1', 'agent', 'hi', 'message', '../evil')"
            )
        with pytest.raises(ValueError):
            gc.archive_posts(db_path, arc_dir, "safe-hub", days=30)
        assert not list(tmp_path.glob("*evil*"))

        # Traversal hub name must never write outside archive_dir
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, content, kind, created_at) "
                "VALUES ('p2', '../../escape', 'general', 'm1', 'M1', 'agent', 'hi', 'message', ?)",
                (old,),
            )
        result = gc.archive_posts(db_path, arc_dir, "../../escape", days=30)
        for path in result:
            path.resolve().relative_to(arc_dir.resolve())
        assert not list(tmp_path.parent.glob("escape*.db"))

    def test_save_config_and_gc_backup_enforce_0600_mode(self, tmp_path, monkeypatch):
        from swarlo import __main__ as cli

        cfg = tmp_path / "config.json"
        monkeypatch.setenv("SWARLO_CONFIG", str(cfg))
        cli._save_config({"api_key": "super-secret"})
        mode = stat.S_IMODE(cfg.stat().st_mode)
        assert mode == 0o600, f"Expected 0o600 on config.json, got {oct(mode)}"

        db_path = tmp_path / "atris.db"
        be = SQLiteBackend(str(db_path))
        be.conn
        be.close()
        bak = gc.backup(db_path)
        bak_mode = stat.S_IMODE(bak.stat().st_mode)
        assert bak_mode == 0o600, f"Expected 0o600 on backup, got {oct(bak_mode)}"

    def test_api_keys_hashed_at_rest_and_legacy_plaintext_upgrades(self, client, fresh_env):
        raw_key = _register(client, "alice", "Alice", hub="sec-hub")
        stored = fresh_env.conn.execute(
            "SELECT api_key FROM members WHERE hub_id = 'sec-hub' AND member_id = 'alice'"
        ).fetchone()[0]
        assert stored != raw_key
        assert stored.startswith("sha256:")

        # Presenting the DB hash directly as a bearer token MUST fail
        assert client.get("/api/sec-hub/channels", headers=_auth(stored)).status_code == 401
        # Real key works and rotation works
        assert client.get("/api/sec-hub/channels", headers=_auth(raw_key)).status_code == 200
        rot = client.post("/api/sec-hub/rotate-key", headers=_auth(raw_key))
        assert rot.status_code == 200
        new_key = rot.json()["api_key"]
        new_stored = fresh_env.conn.execute(
            "SELECT api_key FROM members WHERE hub_id = 'sec-hub' AND member_id = 'alice'"
        ).fetchone()[0]
        assert new_stored.startswith("sha256:") and new_stored != new_key

        # Simulate legacy plaintext key directly inserted into SQLite -> auth works and upgrades
        fresh_env.conn.execute(
            "INSERT INTO members (member_id, hub_id, member_type, member_name, api_key, created_at) "
            "VALUES ('legacy', 'sec-hub', 'agent', 'Legacy', 'legacy-plain-secret', '2026-01-01T00:00:00Z')"
        )
        fresh_env.conn.commit()
        assert client.get("/api/sec-hub/channels", headers=_auth("legacy-plain-secret")).status_code == 200
        upgraded = fresh_env.conn.execute(
            "SELECT api_key FROM members WHERE hub_id = 'sec-hub' AND member_id = 'legacy'"
        ).fetchone()[0]
        assert upgraded.startswith("sha256:") and upgraded != "legacy-plain-secret"


# ── Bug 5: Concurrent git_fetch race & streaming git_push cap ──────────────


class TestBug5GitConcurrencyAndPushCap:
    def test_concurrent_create_bundle_no_ref_collision(self, tmp_path):
        dag = GitDAG(str(tmp_path / "dag.git"))
        dag.init()
        bundle, _, h2 = _make_git_bundle(tmp_path)
        asyncio.run(dag.unbundle(bundle))

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: dag.create_bundle(h2), range(24)))
        assert all(len(b) > 0 for b in results)

        # Ensure no leftover refs/tmp/bundle-* refs remain
        refs = dag._git("for-each-ref", "refs/tmp/").stdout.strip()
        assert refs == ""

    def test_create_bundle_error_before_tempfile_does_not_raise_unbound_local(self, tmp_path, monkeypatch):
        dag = GitDAG(str(tmp_path / "dag2.git"))
        dag.init()
        bundle, _, h2 = _make_git_bundle(tmp_path)
        asyncio.run(dag.unbundle(bundle))

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", boom)
        with pytest.raises(OSError, match="disk full"):
            dag.create_bundle(h2)

    def test_git_push_rejects_oversized_content_length_and_streams_with_cap(self, client, monkeypatch):
        from swarlo import server

        key = _register(client, "pusher", "Pusher")
        # 1. Content-Length header over 50MB rejected immediately with 413
        resp = client.post(
            "/api/atris/git/push",
            headers={**_auth(key), "Content-Length": str(50 * 1024 * 1024 + 1)},
            content=b"tiny",
        )
        assert resp.status_code == 413

        # 2. Streaming body exceeding cap rejected with 413 without calling request.body()
        monkeypatch.setattr(server, "MAX_GIT_BUNDLE_BYTES", 1024)
        resp2 = client.post(
            "/api/atris/git/push",
            headers=_auth(key),
            content=b"x" * 2048,
        )
        assert resp2.status_code == 413


# ── Bug 6: Atomicity, import json, 500s, bounds, file-lock normalization ───


class TestBug6AtomicityJsonBoundsAndFileLocks:
    def test_briefing_applies_metadata_affected_files_boost(self, client):
        key = _register(client, "briefer", "Briefer")
        h = _auth(key)
        # Post whose body has ZERO word overlap with task, only metadata.affected_files matches!
        client.post("/api/atris/channels/general/posts", headers=h, json={
            "content": "completely unrelated prose about bananas",
            "metadata": {"affected_files": ["swarlo/server.py"]},
        })
        resp = client.post("/api/atris/briefing", headers=h, json={
            "task": "harden swarlo/server.py",
            "scorer": "regex",
        })
        assert resp.status_code == 200
        posts = resp.json()["posts"]
        assert len(posts) == 1
        assert posts[0]["score"] >= 0.3

    def test_reply_missing_post_returns_404_not_500(self, client):
        key = _register(client, "replier", "Replier")
        resp = client.post("/api/atris/posts/does-not-exist/replies", headers=_auth(key), json={"content": "hello"})
        assert resp.status_code == 404

    def test_report_and_assign_overlong_content_return_400_and_assign_is_atomic(self, client):
        op_key = _register(client, "op", "Op", member_type="human")
        w_key = _register(client, "worker", "Worker")
        oh, wh = _auth(op_key), _auth(w_key)

        # Report with >4000 chars returns 400 and leaves claim open
        assert client.post("/api/atris/channels/ops/claim", headers=wh, json={"task_key": "T1", "content": "working"}).status_code == 201
        r_rep = client.post("/api/atris/channels/ops/report", headers=wh, json={
            "task_key": "T1", "status": "done", "content": "x" * 4001,
        })
        assert r_rep.status_code == 400
        claims = client.get("/api/atris/claims", headers=wh).json()["claims"]
        assert any(c["task_key"] == "T1" for c in claims)

        # Assign where "Assigned T2 to @Worker: " + content > 4000 returns 400 AND leaves NO dangling open claim!
        r_assign = client.post("/api/atris/channels/ops/assign", headers=oh, json={
            "task_key": "T2", "assignee_id": "worker", "content": "y" * 3995,
        })
        assert r_assign.status_code == 400
        claims_after = client.get("/api/atris/claims", headers=wh).json()["claims"]
        assert not any(c["task_key"] == "T2" for c in claims_after)

    def test_content_validation_bot_quarantine_and_rate_limit_on_write_routes(self, client):
        human_key = _register(client, "human", "Human")
        bot_key = _register(client, "op-test", "Op Test")
        hh, bh = _auth(human_key), _auth(bot_key)

        # Seed a post on #general and one on #sandbox
        p_gen = client.post("/api/atris/channels/general/posts", headers=hh, json={"content": "parent"}).json()["post_id"]
        p_sb = client.post("/api/atris/channels/sandbox/posts", headers=bh, json={"content": "sandbox parent"}).json()["post_id"]

        # Test bot quarantined on claim, claim-file, report, reply off #sandbox
        assert client.post("/api/atris/channels/general/claim", headers=bh, json={"task_key": "BT1", "content": "mine"}).status_code == 403
        assert client.post("/api/atris/channels/general/claim-file", headers=bh, json={"file_path": "a.py"}).status_code == 403
        assert client.post("/api/atris/channels/general/report", headers=bh, json={"task_key": "BT1", "status": "done", "content": "ok"}).status_code == 403
        assert client.post(f"/api/atris/posts/{p_gen}/replies", headers=bh, json={"content": "hi"}).status_code == 403

        # Allowed on #sandbox
        assert client.post("/api/atris/channels/sandbox/claim", headers=bh, json={"task_key": "BT2", "content": "ok"}).status_code == 201
        assert client.post(f"/api/atris/posts/{p_sb}/replies", headers=bh, json={"content": "ok reply"}).status_code == 201

        # Empty / overlong content rejected with 400 on claim, claim-file, reply
        assert client.post("/api/atris/channels/general/claim", headers=hh, json={"task_key": "T-empty", "content": "   "}).status_code == 400
        assert client.post("/api/atris/channels/general/claim", headers=hh, json={"task_key": "T-big", "content": "z" * 4001}).status_code == 400
        assert client.post("/api/atris/channels/general/claim-file", headers=hh, json={"file_path": "x.py", "content": "z" * 4001}).status_code == 400
        assert client.post(f"/api/atris/posts/{p_gen}/replies", headers=hh, json={"content": "   "}).status_code == 400
        assert client.post(f"/api/atris/posts/{p_gen}/replies", headers=hh, json={"content": "z" * 4001}).status_code == 400

        # Rate limit applies across claim/reply
        rl_key = _register(client, "spammer", "Spammer")
        rlh = _auth(rl_key)
        statuses = [
            client.post(f"/api/atris/posts/{p_gen}/replies", headers=rlh, json={"content": f"r {i}"}).status_code
            for i in range(12)
        ]
        assert 429 in statuses

    def test_file_claim_path_normalization_and_traversal_rejection(self, client):
        k1 = _register(client, "ed1", "Ed1")
        k2 = _register(client, "ed2", "Ed2")

        r1 = client.post("/api/atris/channels/ops/claim-file", headers=_auth(k1), json={"file_path": "  ./src//a.py  "})
        assert r1.status_code == 201

        # Equivalent normalized paths collide with 409
        for alias in ("src/a.py", "./src/a.py", "src/sub/../a.py"):
            r = client.post("/api/atris/channels/ops/claim-file", headers=_auth(k2), json={"file_path": alias})
            assert r.status_code == 409, f"Expected 409 for equivalent path {alias!r}, got {r.status_code}"

        files = client.get("/api/atris/file-claims", headers=_auth(k1)).json()["files"]
        assert files[0]["file_path"] == "src/a.py"

        # Invalid/traversal paths rejected with 422
        for bad in ("", "   ", ".", "./", "..", "../secret.py", "a/../../b.py", "/etc/passwd"):
            r = client.post("/api/atris/channels/ops/claim-file", headers=_auth(k2), json={"file_path": bad})
            assert r.status_code == 422, f"Expected 422 for bad path {bad!r}, got {r.status_code}"

    def test_metadata_size_and_depends_on_length_are_bounded(self, client):
        op_key = _register(client, "op", "Op", member_type="human")
        _register(client, "w", "W")
        oh = _auth(op_key)

        giant_meta = {"blob": "x" * 20_000}
        assert client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "hi", "metadata": giant_meta,
        }).status_code in (400, 422)

        assert client.post("/api/atris/channels/ops/claim", headers=oh, json={"task_key": "TM", "content": "go"}).status_code == 201
        assert client.post("/api/atris/channels/ops/report", headers=oh, json={
            "task_key": "TM", "status": "done", "content": "done", "metadata": giant_meta,
        }).status_code in (400, 422)

        too_many_deps = [f"dep:{i}" for i in range(1000)]
        assert client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "hi", "task_key": "TD1", "depends_on": too_many_deps,
        }).status_code in (400, 422)
        assert client.post("/api/atris/channels/ops/claim", headers=oh, json={
            "task_key": "TD2", "content": "go", "depends_on": too_many_deps,
        }).status_code in (400, 422)
        assert client.post("/api/atris/channels/ops/assign", headers=oh, json={
            "task_key": "TD3", "assignee_id": "w", "content": "go", "depends_on": too_many_deps,
        }).status_code in (400, 422)

    def test_window_minutes_clamped_against_overflow_and_negatives(self, client):
        op_key = _register(client, "op", "Op", member_type="human")
        ag_key = _register(client, "ag", "Ag", member_type="agent")
        oh, ah = _auth(op_key), _auth(ag_key)
        client.post("/api/atris/channels/general/posts", headers=ah, json={"content": "active now"})

        huge = 999_999_999_999
        assert client.get(f"/api/atris/idle?idle_minutes={huge}", headers=oh).status_code == 200
        assert client.get(f"/api/atris/liveness?stale_minutes={huge}", headers=oh).status_code == 200
        assert client.post("/api/atris/claims/expire", headers=oh, json={"stale_minutes": huge}).status_code == 200
        assert client.post("/api/atris/prune", headers=oh, json={"stale_minutes": huge}).status_code == 200

        # Negative stale_minutes must not put cutoff in the future and mark active agent dead
        live_neg = client.get("/api/atris/liveness?stale_minutes=-5", headers=oh).json()
        alive_ids = {m["member_id"] for m in live_neg["alive"]}
        assert "ag" in alive_ids

        # Negative idle_minutes must not put cutoff in the future and mark active agent idle
        idle_neg = client.get("/api/atris/idle?idle_minutes=-5", headers=oh).json()
        working_ids = {m["member_id"] for m in idle_neg["working"]}
        assert "ag" in working_ids

    def test_ping_escapes_like_wildcards(self, client):
        k1 = _register(client, "agent_1", "Agent_1")
        k2 = _register(client, "agentX1", "AgentX1")
        k3 = _register(client, "sender", "Sender")

        client.post("/api/atris/channels/general/posts", headers=_auth(k3), json={"content": "@agentX1 hello"})
        ping_underscore = client.get("/api/atris/ping/agent_1?since=2000-01-01T00:00:00Z", headers=_auth(k1)).json()
        assert ping_underscore["new_mentions"] == 0

        client.post("/api/atris/channels/general/posts", headers=_auth(k3), json={"content": "@agent_1 hello"})
        ping_exact = client.get("/api/atris/ping/agent_1?since=2000-01-01T00:00:00Z", headers=_auth(k1)).json()
        assert ping_exact["new_mentions"] == 1

    @pytest.mark.asyncio
    async def test_report_is_atomic_when_parent_reply_fails(self, fresh_env):
        m = Member("a1", "agent", "A1", "h1")
        fresh_env.register_member(m, api_key="k1")
        claim = await fresh_env.claim("h1", m, "general", "task:atomic-rep", "working")
        assert claim.claimed

        with pytest.raises(KeyError):
            await fresh_env.report("h1", m, "general", "task:atomic-rep", "done", "shipped", parent_id="missing-post")

        # Claim must still be open and no result post should exist!
        open_claims = await fresh_env.get_open_claims("h1", task_key="task:atomic-rep")
        assert len(open_claims) == 1
        results = fresh_env.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE hub_id = 'h1' AND task_key = 'task:atomic-rep' AND kind = 'result'"
        ).fetchone()[0]
        assert results == 0


# ── Audit Follow-up: 8 MUST FIX Hardening Regressions ──────────────────────


class TestAuditHardeningFixes:
    def test_failed_dml_does_not_leak_transaction_or_brick_begin_immediate(self, client, fresh_env):
        """Item 1: Any failed DML rolls back cleanly so subsequent BEGIN IMMEDIATE never bricks."""
        op_key = _register(client, "op", "Op", hub="tx-hub", member_type="human")
        worker_key = _register(client, "worker", "Worker", hub="tx-hub")
        oh, wh = _auth(op_key), _auth(worker_key)

        # Seed a claim + a post and warm up the ASGI worker thread's SQLite connection
        assert client.post(
            "/api/tx-hub/channels/ops/claim",
            headers=wh,
            json={"task_key": "task:tx1", "content": "working"},
        ).status_code == 201
        p = client.post(
            "/api/tx-hub/channels/ops/posts",
            headers=wh,
            json={"content": "parent post"},
        ).json()
        assert client.get("/api/tx-hub/channels", headers=wh).status_code == 200

        # Inject ABORT triggers on members UPDATE, scores INSERT, and replies INSERT
        # to simulate mid-write DML failures across both HTTP and direct backend paths.
        with fresh_env._write_lock, fresh_env.conn:
            fresh_env.conn.execute(
                "CREATE TRIGGER fail_members_upd BEFORE UPDATE ON members "
                "BEGIN SELECT RAISE(ABORT, 'members update boom'); END"
            )
            fresh_env.conn.execute(
                "CREATE TRIGGER fail_scores_ins BEFORE INSERT ON scores "
                "BEGIN SELECT RAISE(ABORT, 'scores insert boom'); END"
            )
            fresh_env.conn.execute(
                "CREATE TRIGGER fail_replies_ins BEFORE INSERT ON replies "
                "BEGIN SELECT RAISE(ABORT, 'replies insert boom'); END"
            )

        try:
            # 1. _get_member last_seen update fails (swallowed) on ASGI thread
            assert client.get("/api/tx-hub/channels", headers=wh).status_code == 200

            # 2. compute_score() insert fails (swallowed) on ASGI thread
            assert client.post("/api/tx-hub/score", headers=wh).status_code == 200

            # 3. Immediate HTTP /api/register (BEGIN IMMEDIATE) on ASGI thread MUST succeed!
            reg_after_fail = client.post(
                "/api/register",
                headers=oh,
                json={"hub_id": "tx-hub", "member_id": "post-fail-http", "member_type": "agent"},
            )
            assert reg_after_fail.status_code == 201, reg_after_fail.text

            # 4. authenticate() legacy key upgrade fails (swallowed) -> must NOT leak tx
            with fresh_env._write_lock, fresh_env.conn:
                fresh_env.conn.execute(
                    "INSERT INTO members (member_id, hub_id, member_type, member_name, api_key, created_at) "
                    "VALUES ('legacy-tx', 'tx-hub', 'agent', 'LegacyTx', 'legacy-tx-secret', '2026-01-01T00:00:00Z')"
                )
            assert fresh_env.authenticate("legacy-tx-secret") is not None
            assert not fresh_env.conn.in_transaction

            # 5. create_post() inserts post then fails on UPDATE members.last_active -> MUST roll back BOTH!
            m_worker = Member("worker", "agent", "Worker", "tx-hub")
            with pytest.raises(sqlite3.IntegrityError):
                asyncio.run(fresh_env.create_post("tx-hub", m_worker, "ops", "rolled back post"))
            assert not fresh_env.conn.in_transaction
            leaked_post = fresh_env.conn.execute(
                "SELECT COUNT(*) FROM posts WHERE hub_id = 'tx-hub' AND content = 'rolled back post'"
            ).fetchone()[0]
            assert leaked_post == 0

            # 6. reply() fails on insert -> must NOT leak tx
            with pytest.raises(sqlite3.IntegrityError):
                asyncio.run(fresh_env.reply("tx-hub", m_worker, p["post_id"], "will fail"))
            assert not fresh_env.conn.in_transaction

            # 7. rotate_member_key() fails on UPDATE members -> must NOT leak tx
            with pytest.raises(sqlite3.IntegrityError):
                fresh_env.rotate_member_key(m_worker, worker_key, "new-key-value")
            assert not fresh_env.conn.in_transaction

            # 8. Direct register_member (BEGIN IMMEDIATE) on main thread MUST succeed immediately!
            fresh_env.register_member(Member("worker-2", "agent", "Worker 2", "tx-hub"), api_key="k-new-2")
        finally:
            with fresh_env._write_lock, fresh_env.conn:
                fresh_env.conn.execute("DROP TRIGGER IF EXISTS fail_members_upd")
                fresh_env.conn.execute("DROP TRIGGER IF EXISTS fail_scores_ins")
                fresh_env.conn.execute("DROP TRIGGER IF EXISTS fail_replies_ins")

        # Subsequent assign (BEGIN IMMEDIATE) MUST succeed cleanly!
        assign_resp = client.post(
            "/api/tx-hub/channels/ops/assign",
            headers=oh,
            json={"task_key": "task:tx2", "assignee_id": "worker-2", "content": "post-recovery work"},
        )
        assert assign_resp.status_code == 201, assign_resp.text

    def test_failed_migration_closes_connection(self, tmp_path, monkeypatch):
        """Item 1: A failure inside _run_migrations closes the newly opened SQLite connection."""
        from swarlo import sqlite_backend

        opened: list[sqlite3.Connection] = []
        closed_flags: list[bool] = []
        real_connect = sqlite3.connect

        class TrackingConnection(sqlite3.Connection):
            def close(self):
                closed_flags.append(True)
                super().close()

        def spy_connect(*args, **kwargs):
            kwargs["factory"] = TrackingConnection
            c = real_connect(*args, **kwargs)
            opened.append(c)
            return c

        monkeypatch.setattr(sqlite_backend.sqlite3, "connect", spy_connect)
        monkeypatch.setattr(
            SQLiteBackend,
            "_run_migrations",
            lambda _self, _conn: (_ for _ in ()).throw(RuntimeError("migration boom")),
        )

        be = SQLiteBackend(str(tmp_path / "broken_mig.db"))
        with pytest.raises(RuntimeError, match="migration boom"):
            _ = be.conn
        assert len(opened) == 1
        assert closed_flags == [True]
        assert not be._connections

    def test_webhook_dispatch_pins_validated_ip_once_and_disables_trust_env(self, fresh_env, monkeypatch):
        """Item 2: _dispatch_webhooks resolves DNS once, pins validated IP with Host/SNI, and sets trust_env=False."""
        from swarlo import server
        from swarlo.types import Post

        fresh_env.register_member(
            Member(
                "rebind-target",
                "agent",
                "RebindTarget",
                "pin-hub",
                webhook_url="https://rebind.example.com:8443/hook?token=abc",
            ),
            api_key="k-pin",
        )
        post = Post(
            post_id="p-pin",
            content="ping @RebindTarget",
            kind="message",
            channel="general",
            member_id="sender",
            member_name="Sender",
            member_type="agent",
            mentions=["rebind-target"],
        )

        # Simulate DNS rebinding: 1st lookup returns public IP, 2nd would return AWS metadata IP!
        dns_calls: list[tuple] = []

        def rebinding_getaddrinfo(host, port, *args, **kwargs):
            dns_calls.append((host, port))
            if len(dns_calls) == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

        client_kwargs: list[dict] = []
        post_calls: list[dict] = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                client_kwargs.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, *, headers=None, extensions=None, json=None):
                post_calls.append({
                    "url": url,
                    "headers": headers,
                    "extensions": extensions,
                    "json": json,
                })

        monkeypatch.setattr(server.socket, "getaddrinfo", rebinding_getaddrinfo)
        monkeypatch.setattr(server.httpx, "AsyncClient", FakeAsyncClient)

        asyncio.run(server._dispatch_webhooks("pin-hub", post))

        assert len(dns_calls) == 1, f"Expected single DNS resolution, got {len(dns_calls)}"
        assert client_kwargs and client_kwargs[0].get("trust_env") is False
        assert client_kwargs[0].get("follow_redirects") is False
        assert len(post_calls) == 1
        assert post_calls[0]["url"] == "https://93.184.216.34:8443/hook?token=abc"
        assert post_calls[0]["headers"] == {"Host": "rebind.example.com:8443"}
        assert post_calls[0]["extensions"] == {"sni_hostname": "rebind.example.com"}

        # Mixed global + private DNS answer must block delivery outright
        post_calls.clear()
        monkeypatch.setattr(
            server.socket,
            "getaddrinfo",
            lambda host, port, *a, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
            ],
        )
        asyncio.run(server._dispatch_webhooks("pin-hub", post))
        assert post_calls == []

    def test_default_asgi_bind_host_fails_closed_for_localhost_webhooks(self, client, monkeypatch):
        """Item 3: Direct ASGI import leaves _server_bind_host='' and blocks localhost webhooks by default."""
        from swarlo import server

        # Verify fresh import default in a clean subprocess without SWARLO_ALLOW_LOCALHOST_WEBHOOKS
        clean_env = {k: v for k, v in os.environ.items() if k != "SWARLO_ALLOW_LOCALHOST_WEBHOOKS"}
        check = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from swarlo import server; "
                    "assert server._server_bind_host == '', repr(server._server_bind_host); "
                    "assert not server._is_safe_webhook_url('http://127.0.0.1:8080/hook'); "
                    "assert not server._is_safe_webhook_url('http://localhost:8080/hook')"
                ),
            ],
            env=clean_env,
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stderr

        # Verify HTTP /api/register rejects localhost webhook under default fail-closed state
        monkeypatch.delenv("SWARLO_ALLOW_LOCALHOST_WEBHOOKS", raising=False)
        prev_host = server._server_bind_host
        server.set_bind_host("")
        try:
            resp = client.post("/api/register", json={
                "hub_id": "asgi-fail-closed",
                "member_id": "u1",
                "webhook_url": "http://127.0.0.1:9000/hook",
            })
            assert resp.status_code == 400
            server.set_bind_host("127.0.0.1")
            assert _is_safe_webhook_url("http://127.0.0.1:9000/hook")
        finally:
            server.set_bind_host(prev_host)

    def test_webhook_blocks_6to4_teredo_ipv6_and_userinfo(self, monkeypatch):
        """Item 4: Reject 6to4/Teredo IPv6 SSRF bypasses and URL userinfo."""
        from swarlo import server

        for blocked in (
            "https://[2002:7f00:0001::]/hook",
            "https://[2002:a9fe:a9fe::]/hook",
            "https://[2001:0000:4136:e378:8000:63bf:3fff:fdd2]/hook",
            "https://user:pass@93.184.216.34/hook",
            "https://evil@93.184.216.34/hook",
            "https://@93.184.216.34/hook",
        ):
            assert _is_safe_webhook_url(blocked) is False, f"Should reject {blocked}"

        # Legitimate global IPv4 and IPv6 literals remain allowed
        assert _is_safe_webhook_url("https://93.184.216.34/hook") is True
        assert _is_safe_webhook_url("https://[2606:2800:220:1:248:1893:25c8:1946]/hook") is True

        # DNS returning a 6to4 IPv6 address must also be rejected
        monkeypatch.setattr(
            server.socket,
            "getaddrinfo",
            lambda host, port, *a, **kw: [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2002:7f00:0001::", port, 0, 0)),
            ],
        )
        assert _is_safe_webhook_url("https://sixtofour.example.com/hook", at_dispatch=True) is False

    def test_file_claim_normalization_across_claim_assign_report_touch_and_bad_paths(self, client):
        """Item 5: Canonicalize file: paths across /claim, /assign, /report, /touch and reject drive/percent tricks."""
        op_key = _register(client, "op", "Op", member_type="human")
        k1 = _register(client, "ed1", "Ed1")
        k2 = _register(client, "ed2", "Ed2")
        oh, h1, h2 = _auth(op_key), _auth(k1), _auth(k2)

        # Claim via /claim with unnormalized file: path
        r1 = client.post(
            "/api/atris/channels/ops/claim",
            headers=h1,
            json={"task_key": "file:./src//a.py", "content": "editing a.py"},
        )
        assert r1.status_code == 201

        # Collides on /claim, /claim-file, and /assign using canonical or alternate forms
        assert client.post(
            "/api/atris/channels/ops/claim",
            headers=h2,
            json={"task_key": "file:src/a.py", "content": "dup"},
        ).status_code == 409
        assert client.post(
            "/api/atris/channels/ops/claim-file",
            headers=h2,
            json={"file_path": "src/sub/../a.py"},
        ).status_code == 409
        assert client.post(
            "/api/atris/channels/ops/assign",
            headers=oh,
            json={"task_key": "file:./src/a.py", "assignee_id": "ed2", "content": "edit"},
        ).status_code == 409

        # Touch and report using unnormalized aliases hit the canonical lock
        touch_r = client.post(
            "/api/atris/channels/ops/touch",
            headers=h1,
            json={"task_key": "file:./src/sub/../a.py"},
        )
        assert touch_r.status_code == 200
        assert touch_r.json()["task_key"] == "file:src/a.py"

        rep_r = client.post(
            "/api/atris/channels/ops/report",
            headers=h1,
            json={"task_key": "file:src//a.py", "status": "done", "content": "finished"},
        )
        assert rep_r.status_code == 201
        assert client.get("/api/atris/file-claims", headers=h1).json()["count"] == 0

        # Assign normalizes file: path too
        assign_r = client.post(
            "/api/atris/channels/ops/assign",
            headers=oh,
            json={"task_key": "file:./src//b.py", "assignee_id": "ed2", "content": "edit b"},
        )
        assert assign_r.status_code == 201
        files = client.get("/api/atris/file-claims", headers=oh).json()["files"]
        assert [f["file_path"] for f in files] == ["src/b.py"]

        # Invalid/traversal/drive-letter/percent-encoded/DEL paths return 400 across task routes and 422 on /claim-file
        bad_paths = (
            "../../etc/passwd",
            "C:\\Windows\\win.ini",
            "C:foo.py",
            "C:/secret.py",
            "%2e%2e%2fetc%2fpasswd",
            "src%2fa.py",
            "src%5ca.py",
            "bad\x7f.py",
        )
        for bad in bad_paths:
            key = f"file:{bad}"
            assert client.post(
                "/api/atris/channels/ops/claim",
                headers=h2,
                json={"task_key": key, "content": "bad"},
            ).status_code == 400, f"/claim should reject {key!r}"
            assert client.post(
                "/api/atris/channels/ops/assign",
                headers=oh,
                json={"task_key": key, "assignee_id": "ed2", "content": "bad"},
            ).status_code == 400, f"/assign should reject {key!r}"
            assert client.post(
                "/api/atris/channels/ops/report",
                headers=h2,
                json={"task_key": key, "status": "done", "content": "bad"},
            ).status_code == 400, f"/report should reject {key!r}"
            assert client.post(
                "/api/atris/channels/ops/touch",
                headers=h2,
                json={"task_key": key},
            ).status_code == 400, f"/touch should reject {key!r}"
            assert client.post(
                "/api/atris/channels/ops/claim-file",
                headers=h2,
                json={"file_path": bad},
            ).status_code == 422, f"/claim-file should reject {bad!r}"

    def test_tower_ignores_freeform_coordination_spoofing(self, tmp_path):
        """Item 6: swarlo tower ignores freeform status=NULL posts and claim rows on terminal/blocked/XP views."""
        from swarlo import __main__ as cli

        db_path = tmp_path / "tower_spoof.db"
        be = SQLiteBackend(str(db_path))
        try:
            now = datetime.now(timezone.utc).isoformat()
            with be._write_lock, be.conn:
                be.conn.execute(
                    "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at, last_seen, last_active) "
                    "VALUES ('spoofer', 'tower-hub', 'agent', 'Spoofer', ?, ?, ?)",
                    (now, now, now),
                )
                be.conn.execute(
                    "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at, last_seen, last_active) "
                    "VALUES ('real-owner', 'tower-hub', 'agent', 'RealOwner', ?, ?, ?)",
                    (now, now, now),
                )
                rows = [
                    # Genuine unclaimed task
                    ("m-open", "ops", "real-owner", "RealOwner", "needs owner", "message", "task:open-real", None, now),
                    # Freeform spoofs (status=NULL) trying to hide task:open-real, inject fake blocked, and farm XP
                    ("s-res", "ops", "spoofer", "Spoofer", "fake done", "result", "task:open-real", None, now),
                    ("s-blk1", "ops", "spoofer", "Spoofer", "fake blocked", "blocked", "task:open-real", None, now),
                    ("s-blk2", "ops", "spoofer", "Spoofer", "fake blocked 2", "blocked", "task:fake-blocked", None, now),
                    ("s-clm", "ops", "spoofer", "Spoofer", "fake claim", "claim", "task:fake-xp", None, now),
                    ("s-fail", "ops", "spoofer", "Spoofer", "fake fail", "failed", "task:fake-fail", None, now),
                    # A claim row whose status became 'blocked' must NOT appear in blocked_rows or double-deduct XP
                    ("c-blk", "ops", "real-owner", "RealOwner", "claim row blocked", "claim", "task:claim-blocked", "blocked", now),
                ]
                for row in rows:
                    be.conn.execute(
                        "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, "
                        "content, kind, task_key, status, created_at) "
                        "VALUES (?, 'tower-hub', ?, ?, ?, 'agent', ?, ?, ?, ?, ?)",
                        row,
                    )
        finally:
            be.close()

        state = cli._build_tower_state(str(db_path), hub="tower-hub", limit=10)
        unclaimed_keys = {t["task_key"] for t in state["unclaimed"]}
        blocked_keys = {b["task_key"] for b in state["blocked"]}
        leaderboard_by_id = {r["member_id"]: r for r in state["leaderboard"]}

        assert "task:open-real" in unclaimed_keys
        assert "task:fake-blocked" not in blocked_keys
        assert "task:open-real" not in blocked_keys
        assert "task:claim-blocked" not in blocked_keys
        assert "spoofer" not in leaderboard_by_id
        # real-owner has only 1 legitimate claim row (kind='claim', status='blocked' -> +2 claim XP, 0 blocked penalty)
        assert leaderboard_by_id["real-owner"]["xp"] == 2
        assert leaderboard_by_id["real-owner"]["blocked"] == 0

    def test_get_dag_and_set_dag_are_thread_safe(self, tmp_path, monkeypatch):
        """Item 7: Concurrent get_dag() / set_dag() calls under LRU eviction raise no race errors."""
        from swarlo import server

        root_dag = GitDAG(str(tmp_path / "concurrent.git"))
        root_dag.init()
        set_dag(root_dag)
        monkeypatch.setattr(server, "MAX_HUB_DAGS", 16)

        errors: list[BaseException] = []

        def worker(idx: int):
            try:
                if idx % 29 == 0:
                    set_dag(root_dag)
                dag = get_dag(f"thread-hub-{idx % 40}")
                assert dag is not None
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(worker, range(160)))

        assert errors == []
        assert len(_hub_dags) <= 16

    def test_task_key_max_length_and_whitespace_normalization_before_dedupe_and_ratelimit(self, client):
        """Item 8: Enforce MAX_TASK_KEY_LENGTH and normalize task_key + depends_on before dedupe/rate-limit."""
        op_key = _register(client, "op", "Op", member_type="human")
        _register(client, "w", "W")
        oh = _auth(op_key)

        too_long = "t" * 257
        assert client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "hi", "task_key": too_long,
        }).status_code == 422
        assert client.post("/api/atris/channels/ops/claim", headers=oh, json={
            "content": "hi", "task_key": too_long,
        }).status_code == 422
        assert client.post("/api/atris/channels/ops/assign", headers=oh, json={
            "content": "hi", "task_key": too_long, "assignee_id": "w",
        }).status_code == 422
        assert client.post("/api/atris/channels/ops/report", headers=oh, json={
            "content": "hi", "task_key": too_long, "status": "done",
        }).status_code == 422
        assert client.post("/api/atris/channels/ops/touch", headers=oh, json={
            "task_key": too_long,
        }).status_code == 422

        # Whitespace-padded task_key shares the same per-task rate limit bucket (2/min)
        r1 = client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "status one", "kind": "status", "task_key": "task:rl",
        })
        r2 = client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "status two", "kind": "status", "task_key": "  task:rl  ",
        })
        r3 = client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "status three", "kind": "status", "task_key": " task:rl",
        })
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r3.status_code == 429

        # Dedupe matches normalized task_key and normalized depends_on
        d1 = client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "dep post", "task_key": "task:dup", "depends_on": ["task:dep1"],
        })
        d2 = client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "dep post", "task_key": "  task:dup  ", "depends_on": ["  task:dep1  "],
        })
        assert d1.status_code == 201
        assert d2.status_code == 200
        assert d2.json()["post_id"] == d1.json()["post_id"]

        # Whitespace-only task_key normalizes to None before dedupe and storage
        w1 = client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "blank key", "task_key": "   ",
        })
        w2 = client.post("/api/atris/channels/ops/posts", headers=oh, json={
            "content": "blank key", "task_key": None,
        })
        assert w1.status_code == 201
        assert w1.json()["task_key"] is None
        assert w2.status_code == 200
        assert w2.json()["post_id"] == w1.json()["post_id"]
