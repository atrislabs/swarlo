"""API-level tests — exercises FastAPI routes, auth, status codes, and request validation."""

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


def _register(client, member_id="agent-1", name="Hugo", hub="atris"):
    resp = client.post("/api/register", json={
        "member_id": member_id, "member_type": "agent",
        "member_name": name, "hub_id": hub,
    })
    assert resp.status_code == 201
    return resp.json()["api_key"]


def _auth(api_key):
    return {"Authorization": f"Bearer {api_key}"}


class TestRegistration:
    def test_register_returns_api_key(self, client):
        resp = client.post("/api/register", json={
            "member_id": "test-1", "member_type": "agent",
            "member_name": "Test", "hub_id": "hub-1",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "api_key" in data
        assert data["member_id"] == "test-1"

    def test_health(self, client):
        assert client.get("/api/health").json() == {"status": "ok"}


class TestAuth:
    def test_no_auth_rejected(self, client):
        resp = client.get("/api/atris/channels")
        assert resp.status_code == 401

    def test_bad_key_rejected(self, client):
        resp = client.get("/api/atris/channels", headers={"Authorization": "Bearer bad"})
        assert resp.status_code == 401

    def test_valid_key_accepted(self, client):
        key = _register(client)
        resp = client.get("/api/atris/channels", headers=_auth(key))
        assert resp.status_code == 200


class TestPosts:
    def test_create_and_read(self, client):
        key = _register(client)
        h = _auth(key)

        resp = client.post("/api/atris/channels/general/posts", headers=h,
                          json={"content": "Hello fleet", "kind": "message"})
        assert resp.status_code == 201
        assert resp.json()["content"] == "Hello fleet"

        resp = client.get("/api/atris/channels/general/posts", headers=h)
        posts = resp.json()["posts"]
        assert len(posts) == 1
        assert posts[0]["member_name"] == "Hugo"

    def test_limit_capped_at_50(self, client):
        key = _register(client)
        resp = client.get("/api/atris/channels/general/posts?limit=999", headers=_auth(key))
        assert resp.status_code == 200


class TestClaims:
    def test_claim_succeeds(self, client):
        key = _register(client)
        resp = client.post("/api/atris/channels/experiments/claim", headers=_auth(key),
                          json={"task_key": "task:research", "content": "On it"})
        assert resp.status_code == 201
        assert resp.json()["claimed"] is True

    def test_duplicate_claim_409(self, client):
        key_a = _register(client, "agent-a", "Hugo")
        key_b = _register(client, "agent-b", "Gideon")

        client.post("/api/atris/channels/experiments/claim", headers=_auth(key_a),
                    json={"task_key": "task:1", "content": "I got it"})

        resp = client.post("/api/atris/channels/experiments/claim", headers=_auth(key_b),
                          json={"task_key": "task:1", "content": "I want it"})
        assert resp.status_code == 409

    def test_list_open_claims(self, client):
        key = _register(client)
        h = _auth(key)
        client.post("/api/atris/channels/experiments/claim", headers=h,
                    json={"task_key": "task:1", "content": "Working"})

        resp = client.get("/api/atris/claims", headers=h)
        assert resp.json()["count"] == 1


class TestReports:
    def test_report_closes_claim(self, client):
        key = _register(client)
        h = _auth(key)

        client.post("/api/atris/channels/experiments/claim", headers=h,
                    json={"task_key": "task:1", "content": "Working"})

        resp = client.post("/api/atris/channels/experiments/report", headers=h,
                          json={"task_key": "task:1", "status": "done", "content": "Finished"})
        assert resp.status_code == 201
        assert resp.json()["kind"] == "result"

        claims = client.get("/api/atris/claims", headers=h).json()
        assert claims["count"] == 0

    def test_blocked_report_preserves_kind(self, client):
        key = _register(client)
        h = _auth(key)
        client.post("/api/atris/channels/experiments/claim", headers=h,
                    json={"task_key": "task:1", "content": "Working"})

        resp = client.post("/api/atris/channels/experiments/report", headers=h,
                          json={"task_key": "task:1", "status": "blocked", "content": "Need access"})
        assert resp.status_code == 201
        assert resp.json()["kind"] == "blocked"
        assert resp.json()["status"] == "blocked"

    def test_foreign_report_rejected(self, client):
        key_a = _register(client, "agent-a", "Hugo")
        key_b = _register(client, "agent-b", "Gideon")

        client.post("/api/atris/channels/experiments/claim", headers=_auth(key_a),
                    json={"task_key": "task:1", "content": "Working"})

        resp = client.post("/api/atris/channels/experiments/report", headers=_auth(key_b),
                          json={"task_key": "task:1", "status": "done", "content": "I closed it"})
        assert resp.status_code == 409

    def test_invalid_status_rejected(self, client):
        key = _register(client)
        resp = client.post("/api/atris/channels/experiments/report", headers=_auth(key),
                          json={"task_key": "task:1", "status": "yolo", "content": "Bad"})
        assert resp.status_code == 422


class TestReplies:
    def test_reply_roundtrip(self, client):
        key_a = _register(client, "agent-a", "Hugo")
        key_b = _register(client, "agent-b", "Gideon")

        post = client.post("/api/atris/channels/general/posts", headers=_auth(key_a),
                          json={"content": "Question?"}).json()

        resp = client.post(f"/api/atris/posts/{post['post_id']}/replies", headers=_auth(key_b),
                          json={"content": "Answer."})
        assert resp.status_code == 201
        assert resp.json()["member_name"] == "Gideon"

        replies = client.get(f"/api/atris/posts/{post['post_id']}/replies", headers=_auth(key_a)).json()
        assert replies["count"] == 1
        assert replies["replies"][0]["content"] == "Answer."


class TestSummary:
    def test_summary_with_posts_and_claims(self, client):
        key = _register(client)
        h = _auth(key)

        client.post("/api/atris/channels/general/posts", headers=h,
                    json={"content": "Status update"})
        client.post("/api/atris/channels/experiments/claim", headers=h,
                    json={"task_key": "task:1", "content": "Working on X"})

        resp = client.get("/api/atris/summary", headers=h)
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert "FLEET BOARD" in summary
        assert "OPEN CLAIMS" in summary

    def test_empty_hub_returns_empty(self, client):
        key = _register(client)
        resp = client.get("/api/atris/summary", headers=_auth(key))
        assert resp.json()["summary"] == ""

    def test_summary_requires_auth(self, client):
        assert client.get("/api/atris/summary").status_code == 401


class TestMentions:
    def test_mentions_resolved_in_post(self, client):
        key_a = _register(client, "scout", "Scout")
        key_b = _register(client, "design-agent", "DesignAgent")

        resp = client.post("/api/atris/channels/general/posts", headers=_auth(key_a),
                          json={"content": "Found 10 shops. @DesignAgent please make landing pages"})
        assert resp.status_code == 201
        post = resp.json()
        assert "mentions" in post
        assert "design-agent" in post["mentions"]

    def test_unresolved_mention_ignored(self, client):
        key = _register(client)
        resp = client.post("/api/atris/channels/general/posts", headers=_auth(key),
                          json={"content": "Hey @Nobody, you there?"})
        post = resp.json()
        # No mentions resolved — @Nobody doesn't exist
        assert post.get("mentions") is None or len(post.get("mentions", [])) == 0

    def test_multiple_mentions(self, client):
        _register(client, "scout", "Scout")
        key_b = _register(client, "design", "DesignAgent")
        _register(client, "ops", "OpsAgent")

        resp = client.post("/api/atris/channels/general/posts", headers=_auth(key_b),
                          json={"content": "@Scout found shops, @OpsAgent set up CRM"})
        post = resp.json()
        assert "scout" in post["mentions"]
        assert "ops" in post["mentions"]


class TestMetadata:
    def test_metadata_stored_and_returned(self, client):
        key = _register(client)
        steps = {"steps": [
            {"label": "Read calendar", "done": True},
            {"label": "Search Google Maps", "done": True},
            {"label": "Filter within 10 min", "done": False},
        ]}
        resp = client.post("/api/atris/channels/general/posts", headers=_auth(key),
                          json={"content": "Working on coffee shops", "metadata": steps})
        assert resp.status_code == 201
        post = resp.json()
        assert post["metadata"]["steps"][0]["label"] == "Read calendar"
        assert post["metadata"]["steps"][2]["done"] is False

    def test_metadata_survives_read(self, client):
        key = _register(client)
        h = _auth(key)
        client.post("/api/atris/channels/general/posts", headers=h,
                    json={"content": "Result", "metadata": {"artifacts": ["report.pdf"]}})

        posts = client.get("/api/atris/channels/general/posts", headers=h).json()["posts"]
        assert posts[0]["metadata"]["artifacts"] == ["report.pdf"]

    def test_no_metadata_omitted(self, client):
        key = _register(client)
        resp = client.post("/api/atris/channels/general/posts", headers=_auth(key),
                          json={"content": "Plain message"})
        assert "metadata" not in resp.json()


class TestMembers:
    def test_list_members(self, client):
        _register(client, "agent-a", "Hugo")
        key_b = _register(client, "agent-b", "Gideon")

        resp = client.get("/api/atris/members", headers=_auth(key_b))
        assert resp.status_code == 200
        members = resp.json()["members"]
        names = [m["member_name"] for m in members]
        assert "Hugo" in names
        assert "Gideon" in names

    def test_members_requires_auth(self, client):
        assert client.get("/api/atris/members").status_code == 401


class TestPrune:
    def test_prune_returns_empty_when_all_active(self, client):
        """Prune with very long stale window should not remove active members."""
        key = _register(client, "active-agent", "Active")
        resp = client.post("/api/atris/prune", headers=_auth(key),
                          json={"stale_minutes": 9999})
        assert resp.status_code == 200
        assert resp.json()["count"] == 0
        assert resp.json()["pruned"] == []

    def test_prune_requires_auth(self, client):
        assert client.post("/api/atris/prune").status_code == 401


class TestDeleteMember:
    def test_delete_member_succeeds(self, client):
        """Delete an existing member."""
        key = _register(client, "to-delete", "DeleteMe")
        resp = client.delete("/api/atris/members/to-delete", headers=_auth(key))
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "to-delete"

    def test_delete_member_not_found(self, client):
        """Deleting non-existent member returns 404."""
        key = _register(client, "keeper", "Keeper")
        resp = client.delete("/api/atris/members/ghost", headers=_auth(key))
        assert resp.status_code == 404

    def test_delete_member_requires_auth(self, client):
        assert client.delete("/api/atris/members/anyone").status_code == 401


class TestReplay:
    def test_replay_returns_posts_since_timestamp(self, client):
        """Replay returns posts created after the given timestamp."""
        key = _register(client, "replayer", "Replayer")
        headers = _auth(key)

        # Post a message
        client.post("/api/atris/channels/general/posts", headers=headers,
                   json={"content": "Test message for replay"})

        # Replay from epoch should include it
        resp = client.get("/api/atris/replay?since=1970-01-01T00:00:00Z", headers=headers)
        assert resp.status_code == 200
        assert "posts" in resp.json()

    def test_replay_requires_since_param(self, client):
        """Replay without since parameter returns 422 (validation error)."""
        key = _register(client, "replayer2", "Replayer2")
        resp = client.get("/api/atris/replay", headers=_auth(key))
        assert resp.status_code == 422

    def test_replay_requires_auth(self, client):
        assert client.get("/api/atris/replay?since=2020-01-01T00:00:00Z").status_code == 401


class TestFullFlow:
    def test_claim_work_report_reclaim(self, client):
        key_a = _register(client, "agent-a", "Hugo")
        key_b = _register(client, "agent-b", "Gideon")
        ha, hb = _auth(key_a), _auth(key_b)

        # Hugo claims
        resp = client.post("/api/atris/channels/experiments/claim", headers=ha,
                          json={"task_key": "research:acme", "content": "Researching Acme"})
        assert resp.json()["claimed"]

        # Gideon blocked
        resp = client.post("/api/atris/channels/experiments/claim", headers=hb,
                          json={"task_key": "research:acme", "content": "I want it"})
        assert resp.status_code == 409

        # Hugo posts progress
        client.post("/api/atris/channels/experiments/posts", headers=ha,
                    json={"content": "Found 3 leads so far"})

        # Hugo reports done
        resp = client.post("/api/atris/channels/experiments/report", headers=ha,
                          json={"task_key": "research:acme", "status": "done",
                                "content": "Found 5 leads, 2 qualified"})
        assert resp.status_code == 201

        # Claim closed
        assert client.get("/api/atris/claims", headers=ha).json()["count"] == 0

        # Gideon can now claim same key
        resp = client.post("/api/atris/channels/experiments/claim", headers=hb,
                          json={"task_key": "research:acme", "content": "Follow-up"})
        assert resp.json()["claimed"]

        # Full history
        posts = client.get("/api/atris/channels/experiments/posts?limit=50", headers=ha).json()
        assert posts["count"] == 4
