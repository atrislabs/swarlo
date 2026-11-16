"""Noise quarantine on POST /channels/{channel}/posts.

Three server-side guards, applied in order:

- test bots (a standalone "test" token in member_id or member_name) may
  post only to #sandbox -> 403 on every other channel
- at most 10 new posts per member per hub per minute -> 429 beyond that
- an identical body from the same member within 60 seconds returns the
  first post with 200 instead of writing a second row
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarlo.server import app, set_backend, set_dag, _post_attempts, _task_post_attempts
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


def _register(client, member_id="agent-1", name="Hugo", hub="atris", member_type="agent"):
    sponsors = getattr(client, "_test_hub_sponsors", {})
    sponsor_key = sponsors.get(hub)
    resp = client.post("/api/register", headers=_auth(sponsor_key) if sponsor_key else {}, json={
        "member_id": member_id, "member_type": member_type,
        "member_name": name, "hub_id": hub,
    })
    assert resp.status_code == 201
    key = resp.json()["api_key"]
    sponsors.setdefault(hub, key)
    client._test_hub_sponsors = sponsors
    return key


def _auth(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def _post(client, key, channel="general", content="hello", **extra):
    body = {"content": content}
    body.update(extra)
    return client.post(f"/api/atris/channels/{channel}/posts",
                       headers=_auth(key), json=body)


def _row_count(backend, hub_id="atris"):
    return backend.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ?", (hub_id,)
    ).fetchone()[0]


class TestTestBotChannelQuarantine:
    def test_bot_blocked_from_general(self, client):
        key = _register(client, "op-test", "Op Test")
        resp = _post(client, key, "general", "flood")
        assert resp.status_code == 403
        assert "sandbox" in resp.json()["detail"]

    @pytest.mark.parametrize("channel", ["general", "ops", "experiments", "team-loop"])
    def test_bot_blocked_from_every_channel_but_sandbox(self, client, channel):
        key = _register(client, "wake-test", "Wake Test")
        resp = _post(client, key, channel, "flood")
        assert resp.status_code == 403

    def test_bot_allowed_in_sandbox(self, client):
        key = _register(client, "op-test", "Op Test")
        resp = _post(client, key, "sandbox", "safe to land here")
        assert resp.status_code == 201
        assert resp.json()["channel"] == "sandbox"

    def test_marker_in_member_name_alone(self, client):
        key = _register(client, "worker-1", "Lane Test")
        assert _post(client, key, "general", "hi").status_code == 403

    def test_marker_in_member_id_alone(self, client):
        key = _register(client, "test-1", "Regular Name")
        assert _post(client, key, "general", "hi").status_code == 403

    @pytest.mark.parametrize("member_id,name", [
        ("latest", "Latest"),
        ("contest", "Contest"),
        ("testing", "Testing"),
        ("protest", "Protest"),
        ("attest", "Attest"),
    ])
    def test_test_substring_without_token_boundary_is_not_a_bot(self, client, member_id, name):
        """'latest'/'contest' contain 'test' but not as a standalone token."""
        key = _register(client, member_id, name)
        assert _post(client, key, "general", "hi").status_code == 201

    def test_bot_restriction_beats_dedupe(self, client):
        """A bot repeating itself off-sandbox still gets 403, not a 200."""
        key = _register(client, "op-test", "Op Test")
        assert _post(client, key, "general", "same").status_code == 403
        assert _post(client, key, "general", "same").status_code == 403


class TestPostRateLimit:
    @pytest.mark.parametrize("kind", ["status", "result", "failed"])
    def test_task_kind_flood_is_throttled_after_dedupe(self, client, fresh_backend, kind):
        key = _register(client, "atris", "Atris")
        args = {"channel": "team-loop", "kind": kind, "task_key": "member:op-test"}
        first = _post(client, key, content="first", **args)
        assert first.status_code == 201
        assert _post(client, key, content="second", **args).status_code == 201
        assert _post(client, key, content="third", **args).status_code == 429
        retry = _post(client, key, content="first", **args)
        assert retry.status_code == 200
        assert retry.json()["post_id"] == first.json()["post_id"]
        assert _row_count(fresh_backend) == 2
        attempts = _task_post_attempts[("atris", "atris", "member:op-test", kind)]
        for i in range(len(attempts)):
            attempts[i] -= 61
        assert _post(client, key, content="after window", **args).status_code == 201

    def test_task_limit_is_scoped_and_rejections_do_not_spend_global_slots(self, client):
        key = _register(client, "atris", "Atris")
        other = _register(client, "other", "Other")
        args = {"kind": "status", "task_key": "member:browser-dev"}
        for i in range(2):
            assert _post(client, key, content=f"status {i}", **args).status_code == 201
        for i in range(3):
            assert _post(client, key, content=f"rejected {i}", **args).status_code == 429
        assert _post(client, key, content="other task", kind="status", task_key="other").status_code == 201
        assert _post(client, key, content="other kind", kind="result", task_key=args["task_key"]).status_code == 201
        assert _post(client, other, content="other member", **args).status_code == 201
        for i in range(6):
            assert _post(client, key, content=f"ordinary {i}").status_code == 201
        assert _post(client, key, content="global cap").status_code == 429

    def test_eleventh_post_in_a_minute_gets_429(self, client):
        key = _register(client)
        for i in range(10):
            resp = _post(client, key, content=f"distinct {i}")
            assert resp.status_code == 201, f"post {i + 1} should pass"
        resp = _post(client, key, content="one too many")
        assert resp.status_code == 429

    def test_limit_is_per_member(self, client):
        key_a = _register(client, "agent-a", "Hugo")
        key_b = _register(client, "agent-b", "Gideon")
        for i in range(10):
            _post(client, key_a, content=f"a {i}")
        assert _post(client, key_a, content="a 11").status_code == 429
        assert _post(client, key_b, content="b still fine").status_code == 201

    def test_limit_is_per_hub(self, client):
        key_atris = _register(client, "agent-1", "Hugo", hub="atris")
        for i in range(10):
            client.post("/api/atris/channels/general/posts", headers=_auth(key_atris),
                        json={"content": f"hub1 {i}"})
        over = client.post("/api/atris/channels/general/posts", headers=_auth(key_atris),
                           json={"content": "hub1 11"})
        assert over.status_code == 429
        # Same member id on a different hub starts a fresh window.
        key_beta = _register(client, "agent-1", "Hugo", hub="beta")
        resp = client.post("/api/beta/channels/general/posts", headers=_auth(key_beta),
                           json={"content": "hub2 fine"})
        assert resp.status_code == 201

    def test_window_slides_after_a_minute(self, client):
        key = _register(client)
        for i in range(10):
            _post(client, key, content=f"burst {i}")
        assert _post(client, key, content="burst 11").status_code == 429
        # Age every recorded attempt past the 60s window.
        attempts = _post_attempts[("atris", "agent-1")]
        for i in range(len(attempts)):
            attempts[i] -= 61
        assert _post(client, key, content="after the window").status_code == 201

    def test_dedupe_hit_does_not_spend_a_rate_slot(self, client):
        """At the cap, an idempotent retry of an existing post still gets 200."""
        key = _register(client)
        first = _post(client, key, content="the original")
        assert first.status_code == 201
        for i in range(9):
            assert _post(client, key, content=f"distinct {i}").status_code == 201
        assert _post(client, key, content="one too many").status_code == 429
        retry = _post(client, key, content="the original")
        assert retry.status_code == 200
        assert retry.json()["post_id"] == first.json()["post_id"]


class TestPostDedupe:
    def test_identical_body_returns_existing_post_with_200(self, client, fresh_backend):
        key = _register(client)
        first = _post(client, key, content="same words")
        assert first.status_code == 201
        again = _post(client, key, content="same words")
        assert again.status_code == 200
        assert again.json()["post_id"] == first.json()["post_id"]
        assert _row_count(fresh_backend) == 1
        # A dedupe hit is a retry, not a repeat: repeat_count stays at 1.
        assert "repeat_count" not in again.json()

    def test_near_duplicate_creates_a_new_post(self, client, fresh_backend):
        key = _register(client)
        assert _post(client, key, content="ship it").status_code == 201
        resp = _post(client, key, content="ship it!")
        assert resp.status_code == 201
        assert _row_count(fresh_backend) == 2

    def test_different_member_is_not_a_duplicate(self, client, fresh_backend):
        key_a = _register(client, "agent-a", "Hugo")
        key_b = _register(client, "agent-b", "Gideon")
        assert _post(client, key_a, content="same words").status_code == 201
        resp = _post(client, key_b, content="same words")
        assert resp.status_code == 201
        assert _row_count(fresh_backend) == 2

    def test_different_channel_is_not_a_duplicate(self, client, fresh_backend):
        """Uses a task-bound kind so the 24h repeat-fold cannot mask the check."""
        key = _register(client)
        body = {"content": "same words", "kind": "result", "task_key": "t-1"}
        first = client.post("/api/atris/channels/general/posts", headers=_auth(key), json=body)
        assert first.status_code == 201
        second = client.post("/api/atris/channels/ops/posts", headers=_auth(key), json=body)
        assert second.status_code == 201
        assert second.json()["post_id"] != first.json()["post_id"]
        assert _row_count(fresh_backend) == 2

    def test_different_kind_is_not_a_duplicate(self, client, fresh_backend):
        key = _register(client)
        assert _post(client, key, content="same words", kind="message").status_code == 201
        resp = _post(client, key, content="same words", kind="status")
        assert resp.status_code == 201
        assert _row_count(fresh_backend) == 2

    def test_different_task_key_is_not_a_duplicate(self, client, fresh_backend):
        key = _register(client)
        body = {"content": "same words", "kind": "result"}
        first = client.post("/api/atris/channels/general/posts",
                            headers=_auth(key), json={**body, "task_key": "t-1"})
        second = client.post("/api/atris/channels/general/posts",
                             headers=_auth(key), json={**body, "task_key": "t-2"})
        third = client.post("/api/atris/channels/general/posts",
                            headers=_auth(key), json=body)  # no task_key at all
        assert first.status_code == second.status_code == third.status_code == 201
        assert _row_count(fresh_backend) == 3

    def test_different_priority_or_metadata_is_not_a_duplicate(self, client, fresh_backend):
        key = _register(client)
        body = {"content": "same words", "kind": "message", "task_key": "t-1"}
        first = client.post("/api/atris/channels/general/posts",
                            headers=_auth(key), json=body)
        by_priority = client.post("/api/atris/channels/general/posts",
                                  headers=_auth(key), json={**body, "priority": 3})
        by_metadata = client.post("/api/atris/channels/general/posts",
                                  headers=_auth(key),
                                  json={**body, "metadata": {"step": 1}})
        assert first.status_code == by_priority.status_code == by_metadata.status_code == 201
        assert _row_count(fresh_backend) == 3

    def test_duplicate_window_expires_after_60_seconds(self, client, fresh_backend):
        """Past 60s the same body is a new post again (task-bound kind dodges fold)."""
        key = _register(client)
        body = {"content": "same words", "kind": "result", "task_key": "t-1"}
        first = client.post("/api/atris/channels/general/posts", headers=_auth(key), json=body)
        assert first.status_code == 201
        # 120s ago: outside the 60s dedupe window.
        old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        fresh_backend.conn.execute(
            "UPDATE posts SET created_at = ? WHERE post_id = ?",
            (old, first.json()["post_id"]),
        )
        fresh_backend.conn.commit()
        second = client.post("/api/atris/channels/general/posts", headers=_auth(key), json=body)
        assert second.status_code == 201
        assert second.json()["post_id"] != first.json()["post_id"]

    def test_message_repeat_older_than_60s_still_folds(self, client, fresh_backend):
        """Dedupe releases at 60s; the 24h repeat-fold then merges the repost."""
        key = _register(client)
        first = _post(client, key, content="same words")
        assert first.status_code == 201
        # 61s ago: outside the 60s dedupe window, inside the 24h fold window.
        stale = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        fresh_backend.conn.execute(
            "UPDATE posts SET created_at = ? WHERE post_id = ?",
            (stale, first.json()["post_id"]),
        )
        fresh_backend.conn.commit()
        second = _post(client, key, content="same words")
        assert second.status_code == 201
        assert second.json()["post_id"] == first.json()["post_id"]
        assert second.json()["repeat_count"] == 2
        assert _row_count(fresh_backend) == 1
