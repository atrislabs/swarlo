"""Repeat posts from the same member fold into the earlier post."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from swarlo.types import Member
from swarlo.sqlite_backend import SQLiteBackend


@pytest.fixture
def backend():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    b = SQLiteBackend(path)
    yield b
    b.close()
    os.unlink(path)


@pytest.fixture
def member_a():
    return Member("agent-a", "agent", "Hugo", "hub-1")


@pytest.fixture
def member_b():
    return Member("agent-b", "agent", "Gideon", "hub-1")


def _row_count(backend, hub_id="hub-1"):
    return backend.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE hub_id = ?", (hub_id,)
    ).fetchone()[0]


class TestRepeatFold:
    @pytest.mark.asyncio
    async def test_same_post_folds(self, backend, member_a):
        first = await backend.create_post("hub-1", member_a, "general", "build is green")
        second = await backend.create_post("hub-1", member_a, "general", "build is green")
        assert second.post_id == first.post_id
        assert second.repeat_count == 2
        assert _row_count(backend) == 1

    @pytest.mark.asyncio
    async def test_count_keeps_climbing_and_persists(self, backend, member_a):
        first = await backend.create_post("hub-1", member_a, "general", "still waiting")
        for _ in range(3):
            last = await backend.create_post("hub-1", member_a, "general", "still waiting")
        assert last.post_id == first.post_id
        assert last.repeat_count == 4
        [post] = await backend.read_channel("hub-1", "general")
        assert post.repeat_count == 4
        assert post.to_dict()["repeat_count"] == 4

    @pytest.mark.asyncio
    async def test_matches_on_first_80_chars(self, backend, member_a):
        head = "x" * 80
        first = await backend.create_post("hub-1", member_a, "general", head + " tail one")
        second = await backend.create_post("hub-1", member_a, "general", head + " tail two")
        assert second.post_id == first.post_id
        assert second.content == first.content  # earlier post is returned unchanged

    @pytest.mark.asyncio
    async def test_different_prefix_inserts(self, backend, member_a):
        first = await backend.create_post("hub-1", member_a, "general", "a" * 79 + "b")
        second = await backend.create_post("hub-1", member_a, "general", "a" * 79 + "c")
        assert second.post_id != first.post_id
        assert _row_count(backend) == 2

    @pytest.mark.asyncio
    async def test_other_member_does_not_fold(self, backend, member_a, member_b):
        first = await backend.create_post("hub-1", member_a, "general", "same words")
        second = await backend.create_post("hub-1", member_b, "general", "same words")
        assert second.post_id != first.post_id
        assert second.repeat_count == 1

    @pytest.mark.asyncio
    async def test_other_hub_does_not_fold(self, backend, member_a):
        other = Member("agent-a", "agent", "Hugo", "hub-2")
        first = await backend.create_post("hub-1", member_a, "general", "same words")
        second = await backend.create_post("hub-2", other, "general", "same words")
        assert second.post_id != first.post_id

    @pytest.mark.asyncio
    async def test_outside_window_inserts(self, backend, member_a):
        first = await backend.create_post("hub-1", member_a, "general", "old news")
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        backend.conn.execute(
            "UPDATE posts SET created_at = ? WHERE post_id = ?", (stale, first.post_id)
        )
        backend.conn.commit()
        second = await backend.create_post("hub-1", member_a, "general", "old news")
        assert second.post_id != first.post_id
        assert _row_count(backend) == 2

    @pytest.mark.asyncio
    async def test_custom_window(self, member_a):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        b = SQLiteBackend(path, repeat_window_hours=1)
        try:
            first = await b.create_post("hub-1", member_a, "general", "ping")
            stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            b.conn.execute(
                "UPDATE posts SET created_at = ? WHERE post_id = ?", (stale, first.post_id)
            )
            b.conn.commit()
            second = await b.create_post("hub-1", member_a, "general", "ping")
            assert second.post_id != first.post_id
        finally:
            b.close()
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_window_zero_disables(self, member_a):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        b = SQLiteBackend(path, repeat_window_hours=0)
        try:
            first = await b.create_post("hub-1", member_a, "general", "ping")
            second = await b.create_post("hub-1", member_a, "general", "ping")
            assert second.post_id != first.post_id
        finally:
            b.close()
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_task_posts_never_fold(self, backend, member_a):
        first = await backend.create_post(
            "hub-1", member_a, "general", "done", kind="result", task_key="t-1", status="done"
        )
        second = await backend.create_post(
            "hub-1", member_a, "general", "done", kind="result", task_key="t-1", status="done"
        )
        assert second.post_id != first.post_id

    @pytest.mark.asyncio
    async def test_single_post_hides_repeat_count(self, backend, member_a):
        post = await backend.create_post("hub-1", member_a, "general", "once")
        assert post.repeat_count == 1
        assert "repeat_count" not in post.to_dict()

    @pytest.mark.asyncio
    async def test_messages_with_different_numbers_stay_apart(self, backend, member_a):
        first = await backend.create_post("hub-1", member_a, "general", "Shipped as atris #1333: campaign cadence")
        second = await backend.create_post("hub-1", member_a, "general", "Shipped as atris #1334: campaign cadence")
        assert second.post_id != first.post_id
        assert _row_count(backend) == 2

    @pytest.mark.asyncio
    async def test_numbers_ignored_only_in_first_80_chars(self, backend, member_a):
        head = "y" * 78
        first = await backend.create_post("hub-1", member_a, "general", head + "7 left", kind="status")
        second = await backend.create_post("hub-1", member_a, "general", head + "x left", kind="status")
        assert second.post_id != first.post_id

    @pytest.mark.asyncio
    async def test_status_fold_shows_newest_text(self, backend, member_a):
        first = await backend.create_post("hub-1", member_a, "general", "branch is 1 commit(s) ahead", kind="status")
        second = await backend.create_post("hub-1", member_a, "general", "branch is 6 commit(s) ahead", kind="status")
        assert second.post_id == first.post_id and second.repeat_count == 2
        assert second.content == "branch is 6 commit(s) ahead"
        stored = backend.conn.execute("SELECT content FROM posts WHERE post_id = ?", (first.post_id,)).fetchone()[0]
        assert stored == "branch is 6 commit(s) ahead"

    @pytest.mark.asyncio
    async def test_nudge_with_changing_numbers_folds(self, backend, member_a):
        first = await backend.create_post(
            "hub-1", member_a, "general", "3 agent(s) idle. Post new tasks or ping them.", kind="nudge"
        )
        for n in (5, 2):
            last = await backend.create_post(
                "hub-1", member_a, "general", f"{n} agent(s) idle. Post new tasks or ping them.", kind="nudge"
            )
        assert last.post_id == first.post_id
        assert last.kind == "nudge"
        assert last.repeat_count == 3
        assert _row_count(backend) == 1

    @pytest.mark.asyncio
    async def test_nudge_does_not_fold_into_message(self, backend, member_a):
        first = await backend.create_post("hub-1", member_a, "general", "4 agents idle")
        second = await backend.create_post("hub-1", member_a, "general", "4 agents idle", kind="nudge")
        assert second.post_id != first.post_id
        assert second.kind == "nudge"

    @pytest.mark.asyncio
    async def test_nudge_with_task_key_never_folds(self, backend, member_a):
        first = await backend.create_post("hub-1", member_a, "general", "ping", kind="nudge", task_key="t-1")
        second = await backend.create_post("hub-1", member_a, "general", "ping", kind="nudge", task_key="t-1")
        assert second.post_id != first.post_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["claim", "result"])
    async def test_claims_and_results_without_task_key_never_fold(self, backend, member_a, kind):
        first = await backend.create_post("hub-1", member_a, "general", "done 1", kind=kind)
        second = await backend.create_post("hub-1", member_a, "general", "done 2", kind=kind)
        assert second.post_id != first.post_id
        assert _row_count(backend) == 2
