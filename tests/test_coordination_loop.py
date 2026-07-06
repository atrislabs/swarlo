"""End-to-end coordination test: 3 agents, task lifecycle, conflict handling."""

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


def _join(client, member_id, name):
    resp = client.post("/api/register", json={
        "member_id": member_id, "member_type": "agent",
        "member_name": name, "hub_id": "team",
    })
    assert resp.status_code == 201
    return resp.json()["api_key"]


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


class TestFullCoordinationLoop:
    """Simulates a real overnight sprint: orchestrator posts tasks,
    agents claim and report, conflicts are handled, scoring works."""

    def test_three_agent_sprint(self, client):
        # 1. Three agents join
        boss = _join(client, "boss", "Boss")
        alice = _join(client, "alice", "Alice")
        bob = _join(client, "bob", "Bob")

        # 2. Boss posts tasks
        for i, task in enumerate(["Write tests", "Fix bug", "Update docs"]):
            client.post(f"/api/team/channels/general/posts",
                         json={"content": f"TASK: {task}", "kind": "message",
                               "task_key": f"T{i+1}", "priority": 2},
                         headers=_auth(boss))

        # 3. Alice claims T1
        resp = client.post("/api/team/channels/general/claim",
                            json={"task_key": "T1", "content": "Taking tests"},
                            headers=_auth(alice))
        assert resp.status_code == 201

        # 4. Bob tries to claim T1 — conflict
        resp = client.post("/api/team/channels/general/claim",
                            json={"task_key": "T1", "content": "Also want tests"},
                            headers=_auth(bob))
        assert resp.status_code == 409

        # 5. Bob claims T2 instead
        resp = client.post("/api/team/channels/general/claim",
                            json={"task_key": "T2", "content": "Taking bug fix"},
                            headers=_auth(bob))
        assert resp.status_code == 201

        # 6. Alice reports T1 done
        resp = client.post("/api/team/channels/general/report",
                            json={"task_key": "T1", "status": "done",
                                  "content": "Tests written and passing"},
                            headers=_auth(alice))
        assert resp.status_code == 201

        # 7. Score the hub
        resp = client.post("/api/team/score", headers=_auth(boss))
        data = resp.json()
        assert data["tasks_shipped"] >= 1
        assert data["agents_active"] >= 2

        # 8. Check open claims — only Bob's T2 should be open
        resp = client.get("/api/team/claims", headers=_auth(boss))
        claims = resp.json()["claims"]
        open_keys = [c["task_key"] for c in claims]
        assert "T2" in open_keys
        assert "T1" not in open_keys  # resolved

    def test_file_claim_prevents_conflict(self, client):
        alice = _join(client, "alice", "Alice")
        bob = _join(client, "bob", "Bob")

        # Alice claims a file
        resp = client.post("/api/team/channels/ops/claim-file",
                            json={"file_path": "src/main.py"},
                            headers=_auth(alice))
        assert resp.status_code == 201

        # Bob tries same file — blocked
        resp = client.post("/api/team/channels/ops/claim-file",
                            json={"file_path": "src/main.py"},
                            headers=_auth(bob))
        assert resp.status_code == 409

        # File shows in file-claims
        resp = client.get("/api/team/file-claims", headers=_auth(alice))
        files = resp.json()["files"]
        assert any(f["file_path"] == "src/main.py" for f in files)

    def test_briefing_relevance(self, client):
        key = _join(client, "agent", "Agent")

        # Post about different topics
        client.post("/api/team/channels/general/posts",
                     json={"content": "Fixed auth bug in backend/auth.py"},
                     headers=_auth(key))
        client.post("/api/team/channels/general/posts",
                     json={"content": "Updated landing page colors"},
                     headers=_auth(key))
        client.post("/api/team/channels/general/posts",
                     json={"content": "Auth tests now passing for JWT flow"},
                     headers=_auth(key))

        # Briefing for auth work should rank auth posts higher
        resp = client.post("/api/team/briefing",
                            json={"task": "Review backend/auth.py security"},
                            headers=_auth(key))
        posts = resp.json()["posts"]
        assert len(posts) >= 1
        # Top post should be about auth, not landing page
        top_content = posts[0]["content"].lower()
        assert "auth" in top_content

    def test_assign_creates_claim(self, client):
        boss = _join(client, "boss", "Boss")
        worker = _join(client, "worker", "Worker")

        # Boss assigns task to worker
        resp = client.post("/api/team/channels/ops/assign",
                            json={"task_key": "A1", "assignee_id": "worker",
                                  "content": "Deploy the fix"},
                            headers=_auth(boss))
        assert resp.status_code == 201

        # Worker should see it in their claims
        resp = client.get("/api/team/claims", headers=_auth(worker))
        claims = resp.json()["claims"]
        assert any(c["task_key"] == "A1" for c in claims)

    def test_ping_then_mine_flow(self, client):
        boss = _join(client, "boss", "Boss")
        worker = _join(client, "worker", "Worker")

        # Boss assigns work
        client.post("/api/team/channels/ops/assign",
                     json={"task_key": "P1", "assignee_id": "worker",
                           "content": "Check the logs"},
                     headers=_auth(boss))

        # Worker pings — should see activity
        resp = client.get("/api/team/ping/worker?since=2000-01-01T00:00:00",
                           headers=_auth(worker))
        assert resp.json()["new_posts"] > 0

        # Worker checks mine
        resp = client.get("/api/team/mine/worker", headers=_auth(worker))
        assert resp.json()["count"] >= 1
