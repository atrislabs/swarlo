"""Tests for the SwarloClient — exercises the full client → server → backend path."""

import os
import tempfile
import threading
import time

import pytest
import uvicorn

from swarlo import SwarloClient, SwarloError
from swarlo.server import app, set_backend, set_dag
from swarlo.sqlite_backend import SQLiteBackend
from swarlo.git_dag import GitDAG


@pytest.fixture(scope="module")
def server():
    """Start a real swarlo server on a random port for integration tests."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    git_dir = tempfile.mkdtemp(suffix=".git")

    backend = SQLiteBackend(db_path)
    set_backend(backend)
    dag = GitDAG(git_dir)
    dag.init()
    set_dag(dag)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)

    # Find a free port
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to start
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            import urllib.request
            urllib.request.urlopen(f"{url}/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)

    yield url

    server.should_exit = True
    backend.close()


class TestClientBasics:
    def test_health(self, server):
        client = SwarloClient(server, hub="test")
        assert client.health() is True

    def test_health_bad_server(self):
        client = SwarloClient("http://127.0.0.1:1", hub="test")
        assert client.health() is False

    def test_join_and_read(self, server):
        client = SwarloClient(server, hub="test")
        key = client.join("reader-1", name="Reader")
        assert key
        assert client.api_key == key

        posts = client.read("general")
        assert isinstance(posts, list)

    def test_summary_empty(self, server):
        client = SwarloClient(server, hub="empty-hub")
        client.join("lonely", name="Lonely")
        assert client.summary() == ""


class TestClientCoordination:
    def test_post_and_read(self, server):
        client = SwarloClient(server, hub="coord")
        client.join("poster-1", name="Hugo")

        client.post("general", "Hello from the client")
        posts = client.read("general")
        assert any(p["content"] == "Hello from the client" for p in posts)

    def test_claim_and_report(self, server):
        client = SwarloClient(server, hub="coord2")
        client.join("worker-1", name="Scout")

        result = client.claim("experiments", "task:client-test", "Testing the client")
        assert result["claimed"] is True

        claims = client.claims()
        assert len(claims) >= 1

        report = client.report("experiments", "task:client-test", "done", "Client works")
        assert report["status"] == "done"

        claims_after = client.claims()
        task_claims = [c for c in claims_after if c["task_key"] == "task:client-test"]
        assert len(task_claims) == 0

    def test_duplicate_claim_raises(self, server):
        a = SwarloClient(server, hub="conflict")
        b = SwarloClient(server, hub="conflict")
        a.join("agent-a", name="Hugo")
        b.join("agent-b", name="Gideon")

        a.claim("general", "task:contested", "I got it")

        with pytest.raises(SwarloError) as exc_info:
            b.claim("general", "task:contested", "I want it")
        assert exc_info.value.status_code == 409


class TestClientMentions:
    def test_mentions_resolved(self, server):
        a = SwarloClient(server, hub="mentions")
        b = SwarloClient(server, hub="mentions")
        a.join("scout", name="Scout")
        b.join("designer", name="DesignAgent")

        post = a.post("general", "Found shops. @DesignAgent make pages")
        assert "designer" in post.get("mentions", [])

    def test_metadata_roundtrip(self, server):
        client = SwarloClient(server, hub="meta")
        client.join("worker", name="Worker")

        steps = {"steps": [{"label": "step 1", "done": True}]}
        post = client.post("general", "Done", metadata=steps)
        assert post["metadata"]["steps"][0]["done"] is True

        posts = client.read("general")
        assert posts[0]["metadata"]["steps"][0]["label"] == "step 1"


class TestClientConvenience:
    def test_channels(self, server):
        client = SwarloClient(server, hub="channels-test")
        client.join("lister", name="Lister")
        channels = client.channels()
        assert "general" in channels
        assert "experiments" in channels

    def test_members(self, server):
        client = SwarloClient(server, hub="members-test")
        client.join("mem-1", name="Hugo")
        client.join("mem-2", name="Gideon")
        members = client.members()
        names = [m["member_name"] for m in members]
        assert "Hugo" in names
        assert "Gideon" in names

    def test_reply(self, server):
        client = SwarloClient(server, hub="replies")
        client.join("replier", name="Replier")

        post = client.post("general", "Question?")
        reply = client.reply(post["post_id"], "Answer.")
        assert reply["content"] == "Answer."


class TestFullFlow:
    def test_multi_agent_handoff(self, server):
        """The coffee shop flow: Scout finds, DesignAgent builds, OpsAgent tracks."""
        scout = SwarloClient(server, hub="coffee")
        design = SwarloClient(server, hub="coffee")
        ops = SwarloClient(server, hub="coffee")

        scout.join("scout", name="Scout")
        design.join("design", name="DesignAgent")
        ops.join("ops", name="OpsAgent")

        # Scout claims research
        scout.claim("general", "task:find-shops", "Finding coffee shops in MV")
        scout.post("general", "Found 10 shops. @DesignAgent make landing pages",
                   metadata={"artifacts": ["mv-coffee-shops.csv"]})
        scout.report("general", "task:find-shops", "done", "10 shops found")

        # DesignAgent claims design work
        design.claim("general", "task:landing-pages", "Making 10 landing pages")
        design.post("general", "Generated 10 personalized variants. @OpsAgent set up CRM",
                    metadata={"artifacts": ["red-rock-preview.tsx", "dana-st-preview.tsx"],
                              "steps": [
                                  {"label": "Clone templates", "done": True},
                                  {"label": "Generate variants", "done": True},
                                  {"label": "Deploy previews", "done": True},
                              ]})
        design.report("general", "task:landing-pages", "done", "10 previews deployed")

        # OpsAgent claims CRM setup
        ops.claim("general", "task:crm-setup", "Setting up CRM table")
        ops.report("general", "task:crm-setup", "done", "Notion database created with 10 shops")

        # Verify the full flow is visible
        summary = scout.summary(limit=20)
        assert "FLEET BOARD" in summary

        posts = scout.read("general", limit=20)
        assert len(posts) >= 6  # 3 claims resolved + 2 progress posts + 3 reports... some merged

        # No open claims left
        assert len(scout.claims()) == 0


class TestClientAffectedFiles:
    """Tests for affected_files in report metadata (W4)."""

    def test_report_with_affected_files(self, server):
        client = SwarloClient(server, hub="affected1")
        client.join("editor", name="Editor")

        client.claim("general", "task:fix-stuff", "Fixing")
        result = client.report(
            "general", "task:fix-stuff", "done", "Fixed it",
            affected_files=["backend/services/foo.py", "backend/tests/test_foo.py"],
        )
        assert result["status"] == "done"
        meta = result.get("metadata") or {}
        assert "affected_files" in meta
        assert "backend/services/foo.py" in meta["affected_files"]

    def test_report_without_affected_files(self, server):
        client = SwarloClient(server, hub="affected2")
        client.join("editor2", name="Editor2")

        client.claim("general", "task:plain", "Plain")
        result = client.report("general", "task:plain", "done", "Done")
        assert result["status"] == "done"
        meta = result.get("metadata") or {}
        assert "affected_files" not in meta


class TestClientFileClaiming:
    """Tests for claim_file + file_claims (W1)."""

    def test_claim_file_succeeds(self, server):
        client = SwarloClient(server, hub="files1")
        client.join("editor-1", name="Navigator")

        result = client.claim_file("general", "backend/services/foo.py")
        assert result["claimed"] is True

    def test_file_claims_lists_open(self, server):
        client = SwarloClient(server, hub="files2")
        client.join("editor-2", name="Navigator")

        client.claim_file("general", "backend/services/bar.py")
        claims = client.file_claims()
        assert isinstance(claims, list)
        # claim_file uses file: prefix; check that some claim references bar.py
        all_text = " ".join(str(c) for c in claims)
        assert "bar.py" in all_text

    def test_file_claim_conflict(self, server):
        a = SwarloClient(server, hub="files3")
        b = SwarloClient(server, hub="files3")
        a.join("agent-a", name="Alice")
        b.join("agent-b", name="Bob")

        a.claim_file("general", "backend/services/contested.py")
        # Second agent should hit conflict
        with pytest.raises(SwarloError):
            b.claim_file("general", "backend/services/contested.py")


class TestClientBriefing:
    """Tests for briefing — task-guided context filtering (W1)."""

    def test_briefing_returns_dict(self, server):
        client = SwarloClient(server, hub="brief1")
        client.join("worker-1", name="Worker")

        # Seed some context
        client.post("general", "Working on auth refactor")
        client.post("general", "Found bug in JWT validation")

        result = client.briefing("Fix JWT validation")
        assert isinstance(result, dict)
        # Should contain some kind of context payload
        assert result is not None

    def test_briefing_with_limit(self, server):
        client = SwarloClient(server, hub="brief2")
        client.join("worker-2", name="Worker2")

        client.post("general", "Context post 1")
        client.post("general", "Context post 2")

        result = client.briefing("Test task", limit=5)
        assert isinstance(result, dict)


class TestClientLiveness:
    """Tests for liveness — fleet health check (W1)."""

    def test_liveness_returns_dict(self, server):
        client = SwarloClient(server, hub="live1")
        client.join("agent-live", name="LiveOne")

        result = client.liveness()
        assert isinstance(result, dict)
        # Should have alive/dead/dying buckets
        assert "alive" in result or "dead" in result

    def test_liveness_custom_stale(self, server):
        client = SwarloClient(server, hub="live2")
        client.join("agent-live2", name="LiveTwo")

        result = client.liveness(stale_minutes=60)
        assert isinstance(result, dict)


class TestClientScore:
    """Tests for score — orchestrator metrics endpoint (W1)."""

    def test_score_returns_metrics(self, server):
        client = SwarloClient(server, hub="score1")
        client.join("scorer", name="Scorer")

        # Generate some activity
        client.post("general", "task:test scoring")
        client.claim("general", "task:scoring", "Testing")
        client.report("general", "task:scoring", "done", "Done")

        result = client.score()
        assert isinstance(result, dict)
        assert "agents_active" in result
        assert "tasks_claimed" in result
        assert "tasks_shipped" in result

    def test_score_counts_shipped_tasks(self, server):
        client = SwarloClient(server, hub="score2")
        client.join("counter", name="Counter")

        # Ship a task
        client.claim("general", "task:counted", "Counting")
        client.report("general", "task:counted", "done", "Counted")

        result = client.score()
        assert result.get("tasks_shipped", 0) >= 1
