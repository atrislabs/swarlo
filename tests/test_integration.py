"""End-to-end integration test (W5).

Spins up a real swarlo server in-process, registers 3 distinct agents,
runs the full claim/report/conflict cycle, and verifies the board state.
"""

import os
import socket
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
def live_server():
    """Spin up a real swarlo server on a random port."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    git_dir = tempfile.mkdtemp(suffix=".git")

    backend = SQLiteBackend(db_path)
    set_backend(backend)
    dag = GitDAG(git_dir)
    dag.init()
    set_dag(dag)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

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


def test_three_agent_claim_report_conflict_cycle(live_server):
    """Full E2E: 3 agents register, contend for tasks, ship work, no leaks."""

    alice = SwarloClient(live_server, hub="e2e")
    bob = SwarloClient(live_server, hub="e2e")
    carol = SwarloClient(live_server, hub="e2e")

    # === Phase 1: Registration ===
    alice_key = alice.join("alice", name="Alice")
    bob_key = bob.join("bob", name="Bob")
    carol_key = carol.join("carol", name="Carol")
    assert alice_key and bob_key and carol_key
    assert alice_key != bob_key != carol_key  # distinct API keys

    # === Phase 2: Each agent claims their own task ===
    alice.claim("general", "task:alice-work", "Alice doing alice-work")
    bob.claim("general", "task:bob-work", "Bob doing bob-work")
    carol.claim("general", "task:carol-work", "Carol doing carol-work")

    open_claims = alice.claims()
    assert len(open_claims) == 3
    claim_keys = {c["task_key"] for c in open_claims}
    assert claim_keys == {"task:alice-work", "task:bob-work", "task:carol-work"}

    # === Phase 3: Conflict — Bob tries to claim Alice's task ===
    with pytest.raises(SwarloError) as exc_info:
        bob.claim("general", "task:alice-work", "Bob trying to steal")
    # Should be 409 Conflict
    assert "409" in str(exc_info.value) or "claim" in str(exc_info.value).lower()

    # Original claim should still be Alice's
    open_claims = alice.claims()
    alice_claim = next(c for c in open_claims if c["task_key"] == "task:alice-work")
    assert alice_claim["member_id"] == "alice"

    # === Phase 4: Each agent reports their own task done ===
    alice.report("general", "task:alice-work", "done", "Alice finished",
                 affected_files=["alice/file.py"])
    bob.report("general", "task:bob-work", "done", "Bob finished")
    carol.report("general", "task:carol-work", "failed", "Carol hit a wall")

    # === Phase 5: Verify board state ===
    open_claims_after = alice.claims()
    assert len(open_claims_after) == 0  # all closed

    posts = alice.read("general", limit=50)
    contents = " ".join(p["content"] for p in posts)
    assert "Alice finished" in contents
    assert "Bob finished" in contents
    assert "Carol hit a wall" in contents

    # === Phase 6: Bob can't report Alice's task (permission check) ===
    alice.claim("general", "task:alice-only", "Alice locked it")
    with pytest.raises(SwarloError) as exc_info:
        bob.report("general", "task:alice-only", "done", "Bob trying to close")
    # Should be 409 PermissionError from backend
    assert "409" in str(exc_info.value) or "claim" in str(exc_info.value).lower()

    # Cleanup
    alice.report("general", "task:alice-only", "done", "Alice closes her own")


def test_three_agent_message_flow(live_server):
    """3 agents post messages and read each other's posts in real time."""

    alice = SwarloClient(live_server, hub="msg-flow")
    bob = SwarloClient(live_server, hub="msg-flow")
    carol = SwarloClient(live_server, hub="msg-flow")

    alice.join("a1", name="Alice")
    bob.join("b1", name="Bob")
    carol.join("c1", name="Carol")

    alice.post("general", "Status: working on auth")
    bob.post("general", "Status: refactoring credits")
    carol.post("general", "Status: cleaning up tests")

    # Carol should see all three messages
    posts = carol.read("general", limit=10)
    contents = [p["content"] for p in posts]
    assert "Status: working on auth" in contents
    assert "Status: refactoring credits" in contents
    assert "Status: cleaning up tests" in contents

    # Member listing should show all three
    members = carol.members()
    member_ids = {m["member_id"] for m in members}
    assert {"a1", "b1", "c1"}.issubset(member_ids)
