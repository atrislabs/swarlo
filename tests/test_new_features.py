"""Tests for new Swarlo features: file claims, briefing, liveness, scoring."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from swarlo.server import app, set_backend, set_dag
from swarlo.sqlite_backend import SQLiteBackend
from swarlo.git_dag import GitDAG


@pytest.fixture(autouse=True)
def fresh_backend():
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
    os.unlink(db_path)


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, member_id="agent-1", name="Agent 1", hub="atris"):
    resp = client.post("/api/register", json={
        "member_id": member_id, "member_type": "agent",
        "member_name": name, "hub_id": hub,
    })
    assert resp.status_code == 201
    return resp.json()["api_key"]


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


# ── File Claims ────────────────────────────────────────────


class TestFileClaims:
    def test_claim_file_succeeds(self, client):
        key = _register(client)
        resp = client.post("/api/atris/channels/ops/claim-file",
                           json={"file_path": "backend/services/foo.py"},
                           headers=_auth(key))
        assert resp.status_code == 201
        assert resp.json()["claimed"] is True

    def test_claim_file_conflict(self, client):
        k1 = _register(client, "agent-1", "Agent 1")
        k2 = _register(client, "agent-2", "Agent 2")
        # First claim
        resp = client.post("/api/atris/channels/ops/claim-file",
                           json={"file_path": "backend/services/foo.py"},
                           headers=_auth(k1))
        assert resp.status_code == 201
        # Second claim — should 409
        resp = client.post("/api/atris/channels/ops/claim-file",
                           json={"file_path": "backend/services/foo.py"},
                           headers=_auth(k2))
        assert resp.status_code == 409

    def test_list_file_claims(self, client):
        key = _register(client)
        client.post("/api/atris/channels/ops/claim-file",
                     json={"file_path": "a.py"}, headers=_auth(key))
        client.post("/api/atris/channels/ops/claim-file",
                     json={"file_path": "b.py"}, headers=_auth(key))
        # Also a regular claim — should NOT appear in file claims
        client.post("/api/atris/channels/ops/claim",
                     json={"task_key": "T1", "content": "regular task"},
                     headers=_auth(key))
        resp = client.get("/api/atris/file-claims", headers=_auth(key))
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        paths = {f["file_path"] for f in data["files"]}
        assert paths == {"a.py", "b.py"}

    def test_file_claim_default_content(self, client):
        key = _register(client)
        resp = client.post("/api/atris/channels/ops/claim-file",
                           json={"file_path": "src/main.py"},
                           headers=_auth(key))
        assert resp.status_code == 201
        # Check the claim post was created with default content
        claims = client.get("/api/atris/claims", headers=_auth(key))
        found = [c for c in claims.json()["claims"] if "src/main.py" in c.get("task_key", "")]
        assert len(found) == 1


# ── Latent Briefing ────────────────────────────────────────


class TestBriefing:
    def test_briefing_returns_relevant_posts(self, client):
        key = _register(client)
        # Post about improve.py
        client.post("/api/atris/channels/general/posts",
                     json={"content": "Fixed bug in backend/routers/improve.py parse_tasks function"},
                     headers=_auth(key))
        # Post about unrelated topic
        client.post("/api/atris/channels/general/posts",
                     json={"content": "Updated the landing page CSS colors"},
                     headers=_auth(key))
        # Briefing for improve.py work
        resp = client.post("/api/atris/briefing",
                           json={"task": "Write tests for backend/routers/improve.py"},
                           headers=_auth(key))
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        # The improve.py post should score higher
        assert "improve" in data["posts"][0]["content"].lower()

    def test_briefing_extracts_paths(self, client):
        key = _register(client)
        resp = client.post("/api/atris/briefing",
                           json={"task": "Fix backend/services/auth_service.py and tests/test_auth.py"},
                           headers=_auth(key))
        data = resp.json()
        assert "backend/services/auth_service.py" in data["extracted_paths"]
        assert "tests/test_auth.py" in data["extracted_paths"]

    def test_briefing_extracts_keywords(self, client):
        key = _register(client)
        resp = client.post("/api/atris/briefing",
                           json={"task": "Implement orchestrator scoring endpoint"},
                           headers=_auth(key))
        data = resp.json()
        assert "orchestrator" in data["extracted_keywords"]
        assert "scoring" in data["extracted_keywords"]

    def test_briefing_empty_board(self, client):
        key = _register(client)
        resp = client.post("/api/atris/briefing",
                           json={"task": "Do something", "limit": 5},
                           headers=_auth(key))
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_briefing_respects_limit(self, client):
        key = _register(client)
        for i in range(10):
            client.post("/api/atris/channels/general/posts",
                         json={"content": f"Post about backend service number {i}"},
                         headers=_auth(key))
        resp = client.post("/api/atris/briefing",
                           json={"task": "backend service work", "limit": 3},
                           headers=_auth(key))
        assert resp.json()["count"] <= 3


# ── Liveness ───────────────────────────────────────────────


class TestLiveness:
    def test_all_agents_alive(self, client):
        k1 = _register(client, "a1", "Agent 1")
        k2 = _register(client, "a2", "Agent 2")
        # Make a request so last_seen is set
        client.get("/api/atris/channels", headers=_auth(k1))
        client.get("/api/atris/channels", headers=_auth(k2))
        resp = client.get("/api/atris/liveness", headers=_auth(k1))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["alive"]) == 2
        assert len(data["dead"]) == 0
        assert data["recommendation"] == "All agents healthy."

    def test_never_seen_is_dead(self, client):
        key = _register(client, "a1", "Agent 1")
        _register(client, "ghost", "Ghost")  # registered but never made an authed call
        client.get("/api/atris/channels", headers=_auth(key))
        resp = client.get("/api/atris/liveness", headers=_auth(key))
        data = resp.json()
        dead_ids = {d["member_id"] for d in data["dead"]}
        assert "ghost" in dead_ids

    def test_orphaned_claims_detected(self, client):
        key = _register(client, "a1", "Agent 1")
        ghost_key = _register(client, "ghost", "Ghost")
        # Ghost claims a task
        client.post("/api/atris/channels/ops/claim",
                     json={"task_key": "T1", "content": "Working on T1"},
                     headers=_auth(ghost_key))
        # Ghost never seen again (no authed requests to bump last_seen)
        # But a1 checks liveness. Pass auto_expire=false because we want to
        # OBSERVE the orphan here — a separate test covers auto-expire behavior.
        client.get("/api/atris/channels", headers=_auth(key))
        resp = client.get(
            "/api/atris/liveness?stale_minutes=0&auto_expire=false",
            headers=_auth(key),
        )
        data = resp.json()
        # Ghost should be in dying/dead with orphaned claims
        orphan_keys = {o["task_key"] for o in data["orphaned_claims"]}
        assert "T1" in orphan_keys


# ── Scoring ────────────────────────────────────────────────


class TestScoring:
    def test_score_empty_hub(self, client):
        key = _register(client)
        client.get("/api/atris/channels", headers=_auth(key))
        resp = client.post("/api/atris/score", headers=_auth(key))
        assert resp.status_code == 200
        data = resp.json()
        assert "agents_active" in data
        assert "tasks_shipped" in data
        assert "coord_score" in data
        assert "file_conflicts" in data

    def test_score_reflects_shipped_tasks(self, client):
        key = _register(client)
        # Claim and report done
        client.post("/api/atris/channels/ops/claim",
                     json={"task_key": "T1", "content": "task"},
                     headers=_auth(key))
        client.post("/api/atris/channels/ops/report",
                     json={"task_key": "T1", "status": "done", "content": "shipped"},
                     headers=_auth(key))
        resp = client.post("/api/atris/score", headers=_auth(key))
        data = resp.json()
        assert data["tasks_shipped"] >= 1

    def test_score_tracks_file_conflicts(self, client):
        key = _register(client)
        # Claim a file
        client.post("/api/atris/channels/ops/claim-file",
                     json={"file_path": "foo.py"}, headers=_auth(key))
        resp = client.post("/api/atris/score", headers=_auth(key))
        data = resp.json()
        assert data["file_conflicts"] >= 1

    def test_score_persists_history(self, client, fresh_backend):
        key = _register(client)
        client.get("/api/atris/channels", headers=_auth(key))
        client.post("/api/atris/score", headers=_auth(key))
        client.post("/api/atris/score", headers=_auth(key))
        # Check scores table has 2 entries
        rows = fresh_backend.conn.execute(
            "SELECT COUNT(*) FROM scores WHERE hub_id = 'atris'"
        ).fetchone()[0]
        assert rows == 2


# ── Mine ───────────────────────────────────────────────────


class TestMine:
    def test_mine_empty(self, client):
        key = _register(client)
        resp = client.get("/api/atris/mine/agent-1", headers=_auth(key))
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_mine_shows_claims(self, client):
        key = _register(client)
        client.post("/api/atris/channels/ops/claim",
                     json={"task_key": "T1", "content": "my task"},
                     headers=_auth(key))
        resp = client.get("/api/atris/mine/agent-1", headers=_auth(key))
        data = resp.json()
        assert data["count"] >= 1
        task_keys = [t["task_key"] for t in data["tasks"]]
        assert "T1" in task_keys


# ── Ping ───────────────────────────────────────────────────


class TestPing:
    def test_ping_no_activity(self, client):
        key = _register(client)
        resp = client.get("/api/atris/ping/agent-1", headers=_auth(key))
        assert resp.status_code == 200
        assert resp.json()["action_needed"] is False

    def test_ping_detects_mention(self, client):
        k1 = _register(client, "a1", "Agent 1")
        k2 = _register(client, "a2", "Agent 2")
        # a2 mentions a1
        client.post("/api/atris/channels/general/posts",
                     json={"content": "@a1 check this out"},
                     headers=_auth(k2))
        resp = client.get("/api/atris/ping/a1?since=2000-01-01T00:00:00",
                           headers=_auth(k1))
        data = resp.json()
        assert data["new_mentions"] >= 1
        assert data["action_needed"] is True

    def test_ping_bumps_last_seen(self, client):
        key = _register(client)
        client.get("/api/atris/ping/agent-1", headers=_auth(key))
        members = client.get("/api/atris/members", headers=_auth(key)).json()
        agent = [m for m in members["members"] if m["member_id"] == "agent-1"][0]
        assert agent["last_seen"] is not None


# ── Idle ───────────────────────────────────────────────────


class TestIdle:
    def test_idle_endpoint_returns(self, client):
        key = _register(client)
        # Make authed request to bump last_seen
        client.get("/api/atris/channels", headers=_auth(key))
        # Post something so not idle
        client.post("/api/atris/channels/general/posts",
                     json={"content": "working"}, headers=_auth(key))
        resp = client.get("/api/atris/idle", headers=_auth(key))
        assert resp.status_code == 200
        data = resp.json()
        assert "idle" in data
        assert "working" in data


# ── Suggest ────────────────────────────────────────────────


class TestSuggest:
    def test_suggest_returns_suggestions(self, client):
        key = _register(client)
        client.get("/api/atris/channels", headers=_auth(key))
        resp = client.post("/api/atris/suggest", headers=_auth(key))
        assert resp.status_code == 200
        data = resp.json()
        assert "suggestions" in data
        assert "suggestion_count" in data
