"""Tests for standalone Swarlo server."""

import asyncio
import os
import tempfile
import pytest

from swarlo.types import Member, Post, ClaimResult
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


class TestChannels:
    @pytest.mark.asyncio
    async def test_default_channels(self, backend):
        channels = await backend.list_channels("hub-1")
        assert "general" in channels
        assert "experiments" in channels

    @pytest.mark.asyncio
    async def test_custom_channel_appears(self, backend, member_a):
        await backend.create_post("hub-1", member_a, "custom-channel", "test")
        channels = await backend.list_channels("hub-1")
        assert "custom-channel" in channels


class TestPosts:
    @pytest.mark.asyncio
    async def test_create_and_read(self, backend, member_a):
        post = await backend.create_post("hub-1", member_a, "general", "Hello world")
        assert post.post_id
        assert post.kind == "message"

        posts = await backend.read_channel("hub-1", "general")
        assert len(posts) == 1
        assert posts[0].content == "Hello world"
        assert posts[0].member_name == "Hugo"

    @pytest.mark.asyncio
    async def test_channel_isolation(self, backend, member_a):
        await backend.create_post("hub-1", member_a, "general", "In general")
        await backend.create_post("hub-1", member_a, "experiments", "In experiments")

        general = await backend.read_channel("hub-1", "general")
        experiments = await backend.read_channel("hub-1", "experiments")
        assert len(general) == 1
        assert len(experiments) == 1
        assert general[0].content == "In general"

    @pytest.mark.asyncio
    async def test_hub_isolation(self, backend, member_a):
        member_other = Member("agent-x", "agent", "Other", "hub-2")
        await backend.create_post("hub-1", member_a, "general", "Hub 1")
        await backend.create_post("hub-2", member_other, "general", "Hub 2")

        hub1 = await backend.read_channel("hub-1", "general")
        hub2 = await backend.read_channel("hub-2", "general")
        assert len(hub1) == 1
        assert len(hub2) == 1
        assert hub1[0].content == "Hub 1"


class TestClaims:
    @pytest.mark.asyncio
    async def test_claim_succeeds(self, backend, member_a):
        result = await backend.claim("hub-1", member_a, "experiments", "task:1", "Working on it")
        assert result.claimed
        assert not result.conflict

    @pytest.mark.asyncio
    async def test_duplicate_claim_conflicts(self, backend, member_a, member_b):
        await backend.claim("hub-1", member_a, "experiments", "task:1", "I got it")
        result = await backend.claim("hub-1", member_b, "experiments", "task:1", "I want it")
        assert not result.claimed
        assert result.conflict
        assert "Hugo" in result.message

    @pytest.mark.asyncio
    async def test_open_claims_query(self, backend, member_a):
        await backend.claim("hub-1", member_a, "experiments", "task:1", "First")
        await backend.claim("hub-1", member_a, "general", "task:2", "Second")

        all_claims = await backend.get_open_claims("hub-1")
        assert len(all_claims) == 2

        exp_claims = await backend.get_open_claims("hub-1", channel="experiments")
        assert len(exp_claims) == 1

        specific = await backend.get_open_claims("hub-1", task_key="task:2")
        assert len(specific) == 1
        assert specific[0].task_key == "task:2"


class TestReports:
    @pytest.mark.asyncio
    async def test_report_closes_claim(self, backend, member_a):
        await backend.claim("hub-1", member_a, "experiments", "task:1", "Working")

        claims_before = await backend.get_open_claims("hub-1", task_key="task:1")
        assert len(claims_before) == 1

        report = await backend.report("hub-1", member_a, "experiments", "task:1", "done", "Finished")
        assert report.kind == "result"
        assert report.status == "done"

        claims_after = await backend.get_open_claims("hub-1", task_key="task:1")
        assert len(claims_after) == 0

    @pytest.mark.asyncio
    async def test_failed_report(self, backend, member_a):
        await backend.claim("hub-1", member_a, "experiments", "task:1", "Trying")
        report = await backend.report("hub-1", member_a, "experiments", "task:1", "failed", "Didn't work")
        assert report.kind == "failed"

        claims = await backend.get_open_claims("hub-1", task_key="task:1")
        assert len(claims) == 0

    @pytest.mark.asyncio
    async def test_foreign_report_cannot_close_claim(self, backend, member_a, member_b):
        await backend.claim("hub-1", member_a, "experiments", "task:1", "Working")

        with pytest.raises(PermissionError, match="claimed by Hugo"):
            await backend.report("hub-1", member_b, "experiments", "task:1", "done", "I closed it")

        claims = await backend.get_open_claims("hub-1", task_key="task:1")
        assert len(claims) == 1
        assert claims[0].member_name == "Hugo"


class TestReplies:
    @pytest.mark.asyncio
    async def test_reply_to_post(self, backend, member_a, member_b):
        post = await backend.create_post("hub-1", member_a, "general", "Question")
        reply = await backend.reply("hub-1", member_b, post.post_id, "Answer")
        assert reply.post_id == post.post_id
        assert reply.member_name == "Gideon"


class TestSummary:
    @pytest.mark.asyncio
    async def test_summary_includes_posts(self, backend, member_a):
        await backend.create_post("hub-1", member_a, "general", "Status update")
        summary = await backend.summarize_for_member("hub-1", "agent-a")
        assert "FLEET BOARD" in summary
        assert "Hugo" in summary

    @pytest.mark.asyncio
    async def test_summary_shows_open_claims(self, backend, member_a):
        await backend.claim("hub-1", member_a, "experiments", "task:1", "Working on X")
        summary = await backend.summarize_for_member("hub-1", "agent-a")
        assert "OPEN CLAIMS" in summary

    @pytest.mark.asyncio
    async def test_summary_dedupes_open_claim_lines(self, backend, member_a):
        await backend.create_post("hub-1", member_a, "general", "Status update")
        await backend.claim("hub-1", member_a, "experiments", "task:1", "Working on X")
        summary = await backend.summarize_for_member("hub-1", "agent-a")
        assert summary.count("Working on X") == 1

    @pytest.mark.asyncio
    async def test_empty_summary(self, backend):
        summary = await backend.summarize_for_member("hub-1", "nobody")
        assert summary == ""


class TestMembers:
    def test_register_and_authenticate(self, backend):
        member = Member("agent-1", "agent", "Hugo", "hub-1")
        backend.register_member(member, api_key="secret-key")

        found = backend.authenticate("secret-key")
        assert found is not None
        assert found.member_name == "Hugo"

    def test_invalid_key_returns_none(self, backend):
        assert backend.authenticate("bad-key") is None

    def test_get_member(self, backend):
        member = Member("agent-1", "agent", "Hugo", "hub-1")
        backend.register_member(member)

        found = backend.get_member("hub-1", "agent-1")
        assert found is not None
        assert found.member_type == "agent"


class TestFullFlow:
    @pytest.mark.asyncio
    async def test_claim_progress_report_flow(self, backend, member_a, member_b):
        """Full protocol flow: claim → conflict → progress → report → closure."""
        # A claims
        claim = await backend.claim("hub-1", member_a, "experiments", "task:dogfood", "Running dogfood")
        assert claim.claimed

        # B gets blocked
        conflict = await backend.claim("hub-1", member_b, "experiments", "task:dogfood", "I want it")
        assert conflict.conflict

        # A posts progress
        progress = await backend.create_post("hub-1", member_a, "experiments", "Halfway done", kind="message")
        assert progress.post_id

        # A reports done
        report = await backend.report("hub-1", member_a, "experiments", "task:dogfood", "done", "All good")
        assert report.status == "done"

        # Claim is closed
        open_claims = await backend.get_open_claims("hub-1", task_key="task:dogfood")
        assert len(open_claims) == 0

        # Channel has full history
        posts = await backend.read_channel("hub-1", "experiments")
        assert len(posts) == 3  # claim + progress + result
        kinds = [p.kind for p in posts]
        assert "claim" in kinds
        assert "message" in kinds
        assert "result" in kinds
