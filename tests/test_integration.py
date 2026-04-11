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


def test_mine_returns_own_claims_and_assignments(live_server):
    """The /mine endpoint is the single source of truth for 'what's mine'.

    Validates the assignee_id-as-first-class-column refactor: claimer is
    auto-set as assignee on claim, target is set on assign, and /mine
    surfaces both without channel-grepping.
    """
    alice = SwarloClient(live_server, hub="mine-test")
    bob = SwarloClient(live_server, hub="mine-test")

    alice.join("alice", name="Alice")
    bob.join("bob", name="Bob")

    # Alice claims her own task
    alice.claim("general", "task:alice-own", "Alice working")
    # Alice assigns one to Bob
    alice.assign("general", "task:bob-assigned", "bob", "Please do this")

    # Alice's mine = her own open claim
    alice_mine = alice.mine()
    alice_keys = {t["task_key"] for t in alice_mine["tasks"]}
    assert "task:alice-own" in alice_keys
    # Alice should not see Bob's assignment as her work
    assert "task:bob-assigned" not in alice_keys or alice_mine["count"] >= 1

    # Bob's mine = both the claim made on his behalf AND the assign post
    bob_mine = bob.mine()
    bob_keys = [t["task_key"] for t in bob_mine["tasks"]]
    assert "task:bob-assigned" in bob_keys


def test_ping_finds_assigns_via_first_class_column(live_server):
    """After the assignee_id column was added, /ping should detect assigns
    without depending on json_extract on metadata.
    """
    orchestrator = SwarloClient(live_server, hub="ping-test")
    worker = SwarloClient(live_server, hub="ping-test")

    orchestrator.join("orch", name="Orch")
    worker.join("worker", name="Worker")

    # Worker pings — should be quiet
    initial = worker.ping("worker")
    assert initial["new_assigns"] == 0

    # Orchestrator assigns a task
    orchestrator.assign("general", "task:ping-detect", "worker", "Detect me")

    # Worker pings again — should now show 1 assignment
    after = worker.ping("worker")
    assert after["new_assigns"] >= 1
    assert after["action_needed"] is True


def test_idle_uses_last_active_not_last_seen(live_server):
    """Pinging /ping should NOT make an agent look 'working' to /idle.

    Before the last_active split, calling /ping would bump last_seen and
    /idle would treat the agent as working. After the split, only actual
    production (post/claim/report) bumps last_active, so a polling-only
    agent correctly shows as idle.
    """
    poller = SwarloClient(live_server, hub="idle-split")
    worker = SwarloClient(live_server, hub="idle-split")
    observer = SwarloClient(live_server, hub="idle-split")

    poller.join("poller", name="Poller")
    worker.join("worker", name="Worker")
    observer.join("observer", name="Observer")

    # Worker actually produces work
    worker.post("general", "doing things")

    # Poller only pings — no posts, no claims
    poller.ping("poller")
    poller.ping("poller")
    poller.ping("poller")

    # Observer queries idle. Poller should be idle, worker should be working.
    result = observer._request("GET", "/api/idle-split/idle?idle_minutes=15")

    idle_ids = {a["member_id"] for a in result["idle"]}
    working_ids = {a["member_id"] for a in result["working"]}

    assert "poller" in idle_ids, f"poller should be idle but got working={working_ids}"
    assert "worker" in working_ids, f"worker should be working but got idle={idle_ids}"


def test_replies_eager_loaded_in_read_channel(live_server):
    """Threads no longer die on arrival — read_channel returns replies inline.

    Before this fix, replies were only visible by individually GETting
    /api/{hub}/posts/{post_id}/replies. Now read_channel batch-fetches
    them in one extra query and attaches them to each post.
    """
    alice = SwarloClient(live_server, hub="thread-test")
    bob = SwarloClient(live_server, hub="thread-test")

    alice.join("alice", name="Alice")
    bob.join("bob", name="Bob")

    # Alice starts a thread
    parent = alice._request("POST", "/api/thread-test/channels/general/posts", {
        "content": "anyone seen the build break?",
        "kind": "question",
    })
    parent_id = parent["post_id"]

    # Bob and Alice both reply
    bob._request("POST", f"/api/thread-test/posts/{parent_id}/replies", {
        "content": "looking at it now"
    })
    alice._request("POST", f"/api/thread-test/posts/{parent_id}/replies", {
        "content": "thanks bob"
    })

    # read_channel should return the parent post with replies attached
    posts = alice.read("general", limit=10)
    parent_post = next((p for p in posts if p["post_id"] == parent_id), None)
    assert parent_post is not None, "parent post missing from read_channel"
    assert "replies" in parent_post, "replies field not eagerly loaded"
    assert len(parent_post["replies"]) == 2, f"expected 2 replies, got {len(parent_post['replies'])}"
    # Chronological order
    assert parent_post["replies"][0]["content"] == "looking at it now"
    assert parent_post["replies"][1]["content"] == "thanks bob"


def test_wait_for_task_returns_when_done(live_server):
    """The subscribe-to-task verb. wait_for blocks until the task ships
    and returns the result post — no manual polling required.
    """
    requester = SwarloClient(live_server, hub="wait-test")
    worker = SwarloClient(live_server, hub="wait-test")
    requester.join("requester", name="Requester")
    worker.join("worker", name="Worker")

    # Worker claims and immediately ships (synthetic — same process)
    worker.claim("general", "task:quick", "Doing it")
    worker.report("general", "task:quick", "done", "Finished fast")

    # Requester subscribes — should return the result post immediately
    result = requester.wait_for("task:quick", timeout=5, poll_interval=0.2)
    assert result["task_key"] == "task:quick"
    assert result["status"] == "done"
    assert result["kind"] == "result"
    assert "Finished fast" in result["content"]


def test_wait_for_times_out_when_task_never_ships(live_server):
    """If nobody completes the task, wait_for raises SwarloError(408)."""
    requester = SwarloClient(live_server, hub="wait-timeout")
    requester.join("requester", name="Requester")

    with pytest.raises(SwarloError) as exc_info:
        requester.wait_for("task:never-happens", timeout=1, poll_interval=0.2)
    assert exc_info.value.status_code == 408


def test_precommit_hook_blocks_conflicting_files(live_server, tmp_path):
    """The swarlo-precommit-hook should block a commit when a staged file
    is claimed by another agent, and pass when files are unclaimed.
    """
    import json
    import subprocess
    from pathlib import Path

    # Two agents share the hub
    alice = SwarloClient(live_server, hub="hook-test")
    bob = SwarloClient(live_server, hub="hook-test")
    alice_key = alice.join("alice", name="Alice")
    bob_key = bob.join("bob", name="Bob")

    # Alice claims one file
    alice.claim_file("general", "src/contested.py")

    # Set up Bob's swarlo config in a temp HOME so the hook reads it
    fake_home = tmp_path / "home"
    (fake_home / ".swarlo").mkdir(parents=True)
    config = {
        "server": live_server,
        "hub": "hook-test",
        "member_id": "bob",
        "api_key": bob_key,
    }
    (fake_home / ".swarlo" / "config.json").write_text(json.dumps(config))

    # Set up a tiny git repo where Bob will try to commit
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "bob@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Bob"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "contested.py").write_text("# bob's edit\n")
    (repo / "src" / "safe.py").write_text("# nobody owns this\n")

    hook = Path(__file__).resolve().parent.parent / "scripts" / "swarlo-precommit-hook"
    assert hook.exists(), f"hook missing at {hook}"

    env = {**os.environ, "HOME": str(fake_home)}

    # Case 1: stage only the SAFE file → hook should pass
    subprocess.run(["git", "add", "src/safe.py"], cwd=repo, check=True)
    result = subprocess.run(
        [str(hook)], cwd=repo, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"hook should pass for unclaimed file, got {result.returncode}\n"
        f"stderr: {result.stderr}"
    )
    subprocess.run(["git", "reset"], cwd=repo, check=True)

    # Case 2: stage the CONTESTED file → hook should block
    subprocess.run(["git", "add", "src/contested.py"], cwd=repo, check=True)
    result = subprocess.run(
        [str(hook)], cwd=repo, env=env, capture_output=True, text=True
    )
    assert result.returncode == 1, (
        f"hook should block claimed file, got {result.returncode}\n"
        f"stderr: {result.stderr}"
    )
    assert "contested.py" in result.stderr
    assert "Alice" in result.stderr or "alice" in result.stderr


def test_liveness_auto_expires_stale_claims(live_server):
    """Hitting /liveness sweeps stale claims so orphans don't accumulate.

    Before this change, a stale claim would sit in 'open' status forever
    unless someone explicitly hit /claims/expire. Now the orchestrator's
    regular liveness check doubles as cleanup.
    """
    alice = SwarloClient(live_server, hub="liveness-sweep")
    observer = SwarloClient(live_server, hub="liveness-sweep")
    alice.join("alice", name="Alice")
    observer.join("observer", name="Observer")

    # Alice claims a task
    alice.claim("general", "task:will-go-stale", "Alice working")

    # Initially the claim is open
    open_before = observer.claims()
    assert any(c["task_key"] == "task:will-go-stale" for c in open_before)

    # Liveness with stale_minutes=0 treats everything as stale and sweeps
    result = observer._request(
        "GET", "/api/liveness-sweep/liveness?stale_minutes=0"
    )
    assert "expired_on_sweep" in result
    assert "task:will-go-stale" in result["expired_on_sweep"]

    # After the sweep, the claim is no longer open
    open_after = observer.claims()
    assert not any(c["task_key"] == "task:will-go-stale" for c in open_after)


def test_liveness_auto_expire_can_be_disabled(live_server):
    """auto_expire=false lets consumers observe without cleanup."""
    alice = SwarloClient(live_server, hub="liveness-observe")
    observer = SwarloClient(live_server, hub="liveness-observe")
    alice.join("alice", name="Alice")
    observer.join("observer", name="Observer")

    alice.claim("general", "task:stays-open", "Alice working")

    result = observer._request(
        "GET", "/api/liveness-observe/liveness?stale_minutes=0&auto_expire=false"
    )
    # No sweep happened
    assert result.get("expired_on_sweep") == []
    # Claim still present
    open_after = observer.claims()
    assert any(c["task_key"] == "task:stays-open" for c in open_after)


def test_ping_with_include_mine_folds_task_list_into_response(live_server):
    """include=mine lets agents get their ping badge AND task list in one
    call, collapsing the common two-round-trip pattern to one.
    """
    orch = SwarloClient(live_server, hub="ping-include")
    worker = SwarloClient(live_server, hub="ping-include")
    orch.join("orch", name="Orch")
    worker.join("worker", name="Worker")

    # Default ping — no task list, unchanged behavior
    plain = worker.ping("worker")
    assert "mine" not in plain
    assert "mine_count" not in plain

    # Worker has nothing yet, ping with include=mine returns empty list
    baseline = worker.ping("worker", include="mine")
    assert "mine" in baseline
    assert baseline["mine"] == []
    assert baseline["mine_count"] == 0

    # Orchestrator assigns one task and worker claims another
    orch.assign("general", "task:assigned", "worker", "Do this")
    worker.claim("general", "task:own-claim", "Also doing this")

    # Now include=mine returns both. Note: assign() creates both a claim
    # post (on behalf of assignee) AND an assign post, so "task:assigned"
    # surfaces as two rows — check unique task_keys, not row count.
    result = worker.ping("worker", include="mine")
    assert result["action_needed"] is True  # the assignment fires action
    task_keys = {t["task_key"] for t in result["mine"]}
    assert "task:assigned" in task_keys
    assert "task:own-claim" in task_keys
    assert result["mine_count"] >= 2


def test_claim_blocks_on_unmet_dependency(live_server):
    """A task with unmet deps cannot be claimed directly."""
    alice = SwarloClient(live_server, hub="deps-claim")
    alice.join("alice", name="Alice")

    # Try to claim task:B which depends on task:A (which hasn't been done)
    with pytest.raises(SwarloError) as exc_info:
        alice.claim("general", "task:B", "trying to start", depends_on=["task:A"])
    assert exc_info.value.status_code == 409
    assert "task:A" in str(exc_info.value)


def test_claim_succeeds_after_dep_is_done(live_server):
    """Once a dep has a done post, a claim with that dep works."""
    alice = SwarloClient(live_server, hub="deps-unblock")
    alice.join("alice", name="Alice")

    # Complete task:A first
    alice.claim("general", "task:A", "doing A")
    alice.report("general", "task:A", "done", "A finished")

    # Now task:B can claim with task:A as a dep
    result = alice.claim("general", "task:B", "starting B", depends_on=["task:A"])
    assert result["claimed"] is True


def test_assign_records_deps_but_does_not_block(live_server):
    """Assigning a task with unmet deps succeeds — assignments are push
    notifications, not readiness gates. /ready does the filtering.
    """
    orch = SwarloClient(live_server, hub="deps-assign")
    worker = SwarloClient(live_server, hub="deps-assign")
    orch.join("orch", name="Orch")
    worker.join("worker", name="Worker")

    # Assign task:C which depends on task:A (not done). Assignment should
    # succeed even though the dep isn't met.
    result = orch.assign("general", "task:C", "worker", "please do C",
                        depends_on=["task:A"])
    assert result["claimed"] is True


def test_ready_filters_out_unmet_dep_tasks(live_server):
    """/ready returns only the subset of /mine whose deps are all done."""
    orch = SwarloClient(live_server, hub="deps-ready")
    worker = SwarloClient(live_server, hub="deps-ready")
    orch.join("orch", name="Orch")
    worker.join("worker", name="Worker")

    # Three assignments:
    # - task:free has no deps → always ready
    # - task:blocked depends on task:A (not done) → never ready until A ships
    # - task:unblocked depends on task:B (already done) → ready now
    orch.assign("general", "task:free", "worker", "no deps")

    # Do task:B first so the unblocked path works
    orch.assign("general", "task:B", "worker", "first task")
    worker.report("general", "task:B", "done", "B finished")

    orch.assign("general", "task:unblocked", "worker", "depends on B",
                depends_on=["task:B"])
    orch.assign("general", "task:blocked", "worker", "depends on A",
                depends_on=["task:A"])

    ready = worker.ready()
    ready_keys = {t["task_key"] for t in ready["tasks"]}
    assert "task:free" in ready_keys
    assert "task:unblocked" in ready_keys
    assert "task:blocked" not in ready_keys


def test_claim_next_picks_ready_and_skips_blocked(live_server):
    """claim_next returns the ready task and ignores the one blocked by deps.

    Workflow: get a ready task, work it, report done, then ask for the next.
    When the only remaining task has unmet deps, the second call returns None.
    """
    orch = SwarloClient(live_server, hub="claim-next-a")
    worker = SwarloClient(live_server, hub="claim-next-a")
    orch.join("orch", name="Orch")
    worker.join("worker", name="Worker")

    # Assign one task with no deps (ready) and one blocked on an unposted dep
    orch.assign("general", "task:ready", "worker", "no deps")
    orch.assign("general", "task:blocked", "worker", "blocked",
                depends_on=["task:nonexistent"])

    # First call returns the ready task
    result = worker.claim_next("general")
    assert result is not None
    assert result["task_key"] == "task:ready"

    # Agent reports it done (normal workflow — finish before asking for more)
    worker.report("general", "task:ready", "done", "finished it")

    # Second call returns None — task:blocked still has an unmet dep
    second = worker.claim_next("general")
    assert second is None


def test_claim_next_returns_none_when_nothing_is_ready(live_server):
    """claim_next returns None when no assignments have met dependencies."""
    orch = SwarloClient(live_server, hub="claim-next-empty")
    worker = SwarloClient(live_server, hub="claim-next-empty")
    orch.join("orch", name="Orch")
    worker.join("worker", name="Worker")

    result = worker.claim_next("general")
    assert result is None


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
