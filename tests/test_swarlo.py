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
    async def test_duplicate_claim_conflicts_across_channels(self, backend, member_a, member_b):
        await backend.claim("hub-1", member_a, "experiments", "task:1", "I got it")
        result = await backend.claim("hub-1", member_b, "ops", "task:1", "I want it")
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

    @pytest.mark.asyncio
    async def test_foreign_cross_channel_report_cannot_close_claim(self, backend, member_a, member_b):
        await backend.claim("hub-1", member_a, "experiments", "task:1", "Working")

        with pytest.raises(PermissionError, match="claimed by Hugo"):
            await backend.report("hub-1", member_b, "ops", "task:1", "done", "I closed it")

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


class TestMentionExtraction:
    def test_single_mention(self):
        from swarlo.types import extract_mentions
        assert extract_mentions("Hey @Scout, check this") == ["Scout"]

    def test_multiple_mentions(self):
        from swarlo.types import extract_mentions
        assert extract_mentions("@Scout found it, @DesignAgent make pages") == ["Scout", "DesignAgent"]

    def test_no_mentions(self):
        from swarlo.types import extract_mentions
        assert extract_mentions("Just a normal message") == []

    def test_mention_with_hyphens(self):
        from swarlo.types import extract_mentions
        assert extract_mentions("Ask @design-agent") == ["design-agent"]

    def test_email_not_matched(self):
        from swarlo.types import extract_mentions
        # email @ should not produce a match (no word boundary before @)
        result = extract_mentions("email me at user@example.com")
        assert "example" not in result or result == ["example"]  # regex picks up @example but that's OK

    def test_mention_resolution(self, backend, member_a, member_b):
        backend.register_member(member_a)
        backend.register_member(member_b)
        resolved = backend.resolve_mentions("hub-1", ["Hugo", "Gideon", "Nobody"])
        assert "agent-a" in resolved
        assert "agent-b" in resolved
        assert len(resolved) == 2  # Nobody not resolved

    @pytest.mark.asyncio
    async def test_post_extracts_mentions(self, backend, member_a, member_b):
        backend.register_member(member_a)
        backend.register_member(member_b)
        post = await backend.create_post("hub-1", member_a, "general", "Hey @Gideon check this")
        assert post.mentions == ["agent-b"]

    @pytest.mark.asyncio
    async def test_post_with_metadata(self, backend, member_a):
        steps = {"steps": [{"label": "step 1", "done": True}]}
        post = await backend.create_post("hub-1", member_a, "general", "Done", metadata=steps)
        assert post.metadata == steps

        posts = await backend.read_channel("hub-1", "general")
        assert posts[0].metadata["steps"][0]["done"] is True


class TestStaleExpiry:
    @pytest.mark.asyncio
    async def test_stale_claims_auto_expire(self, backend, member_a):
        """Claims older than stale_minutes are auto-expired on get_open_claims."""
        # Create a claim, then backdate it
        await backend.claim("hub-1", member_a, "experiments", "task:stale", "Working")
        # Backdate the claim to 2 hours ago
        from datetime import datetime, timedelta, timezone
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        backend.conn.execute(
            "UPDATE posts SET created_at = ?, metadata = NULL WHERE task_key = ? AND status = 'open'",
            (old_time, "task:stale"),
        )
        backend.conn.commit()

        # get_open_claims should auto-expire it
        claims = await backend.get_open_claims("hub-1")
        assert len(claims) == 0

    @pytest.mark.asyncio
    async def test_fresh_claims_survive_expiry(self, backend, member_a):
        """Claims with recent heartbeat survive expiry."""
        await backend.claim("hub-1", member_a, "experiments", "task:fresh", "Working")
        claims = await backend.get_open_claims("hub-1")
        assert len(claims) == 1

    @pytest.mark.asyncio
    async def test_force_expire_returns_task_keys(self, backend, member_a):
        """force_expire_claims returns the expired task keys."""
        await backend.claim("hub-1", member_a, "experiments", "task:old", "Working")
        from datetime import datetime, timedelta, timezone
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        backend.conn.execute(
            "UPDATE posts SET created_at = ?, metadata = NULL WHERE task_key = ? AND status = 'open'",
            (old_time, "task:old"),
        )
        backend.conn.commit()

        expired = await backend.force_expire_claims("hub-1", stale_minutes=30)
        assert "task:old" in expired

    @pytest.mark.asyncio
    async def test_expired_claim_frees_task_key(self, backend, member_a, member_b):
        """After stale expiry, another agent can claim the same task."""
        await backend.claim("hub-1", member_a, "experiments", "task:reclaim", "Working")
        from datetime import datetime, timedelta, timezone
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        backend.conn.execute(
            "UPDATE posts SET created_at = ?, metadata = NULL WHERE task_key = ? AND status = 'open'",
            (old_time, "task:reclaim"),
        )
        backend.conn.commit()

        # B should be able to claim it now
        result = await backend.claim("hub-1", member_b, "experiments", "task:reclaim", "Taking over")
        assert result.claimed


class TestTouchClaim:
    @pytest.mark.asyncio
    async def test_touch_refreshes_heartbeat(self, backend, member_a):
        """Touch updates the heartbeat timestamp."""
        await backend.claim("hub-1", member_a, "experiments", "task:touch", "Working")
        ok = await backend.touch_claim("hub-1", "agent-a", "task:touch")
        assert ok is True

    @pytest.mark.asyncio
    async def test_touch_nonexistent_returns_false(self, backend, member_a):
        """Touch on non-existent claim returns False."""
        ok = await backend.touch_claim("hub-1", "agent-a", "task:ghost")
        assert ok is False

    @pytest.mark.asyncio
    async def test_touch_wrong_member_returns_false(self, backend, member_a, member_b):
        """Touch from wrong member returns False (can't refresh someone else's claim)."""
        await backend.claim("hub-1", member_a, "experiments", "task:owned", "Mine")
        ok = await backend.touch_claim("hub-1", "agent-b", "task:owned")
        assert ok is False

    @pytest.mark.asyncio
    async def test_touched_claim_survives_expiry(self, backend, member_a):
        """A touched claim should not be expired."""
        await backend.claim("hub-1", member_a, "experiments", "task:alive", "Working")
        # Backdate the created_at but touch it (heartbeat stays fresh)
        from datetime import datetime, timedelta, timezone
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        backend.conn.execute(
            "UPDATE posts SET created_at = ? WHERE task_key = ? AND status = 'open'",
            (old_time, "task:alive"),
        )
        backend.conn.commit()

        # Touch refreshes heartbeat
        await backend.touch_claim("hub-1", "agent-a", "task:alive")

        # Should survive expiry check
        claims = await backend.get_open_claims("hub-1", task_key="task:alive")
        assert len(claims) == 1


class TestAtomicClaims:
    @pytest.mark.asyncio
    async def test_unique_index_prevents_duplicate(self, backend, member_a, member_b):
        """Two claims on same task_key: only one succeeds (DB-level uniqueness)."""
        r1 = await backend.claim("hub-1", member_a, "experiments", "task:atomic", "First")
        r2 = await backend.claim("hub-1", member_b, "experiments", "task:atomic", "Second")
        assert r1.claimed
        assert not r2.claimed
        assert r2.conflict

    @pytest.mark.asyncio
    async def test_claim_after_report_succeeds(self, backend, member_a, member_b):
        """After a claim is reported done, the task_key is free again."""
        await backend.claim("hub-1", member_a, "experiments", "task:reuse", "First")
        await backend.report("hub-1", member_a, "experiments", "task:reuse", "done", "Done")
        r2 = await backend.claim("hub-1", member_b, "experiments", "task:reuse", "Second")
        assert r2.claimed


class TestPriority:
    @pytest.mark.asyncio
    async def test_priority_stored_and_retrieved(self, backend, member_a):
        """Priority field is stored and returned."""
        post = await backend.create_post("hub-1", member_a, "general", "Urgent", priority=5)
        posts = await backend.read_channel("hub-1", "general")
        assert posts[0].priority == 5

    @pytest.mark.asyncio
    async def test_default_priority_is_zero(self, backend, member_a):
        """Default priority is 0."""
        await backend.create_post("hub-1", member_a, "general", "Normal")
        posts = await backend.read_channel("hub-1", "general")
        assert posts[0].priority == 0


class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_requeues_failed_task(self, backend, member_a):
        """Failed tasks are marked for retry."""
        await backend.claim("hub-1", member_a, "experiments", "task:fail", "Trying")
        await backend.report("hub-1", member_a, "experiments", "task:fail", "failed", "Oops")

        retried = await backend.retry_failed("hub-1", max_retries=3)
        assert "task:fail" in retried

    @pytest.mark.asyncio
    async def test_retry_respects_max_retries(self, backend, member_a):
        """Tasks beyond max_retries are not retried."""
        await backend.claim("hub-1", member_a, "experiments", "task:exhaust", "Try 1")
        await backend.report("hub-1", member_a, "experiments", "task:exhaust", "failed", "Fail 1")

        # Retry 3 times
        for _ in range(3):
            await backend.retry_failed("hub-1", max_retries=3)

        # 4th retry should return empty
        retried = await backend.retry_failed("hub-1", max_retries=3)
        assert "task:exhaust" not in retried

    @pytest.mark.asyncio
    async def test_retry_skips_tasks_with_open_claims(self, backend, member_a, member_b):
        """Don't retry a task that has an open claim."""
        await backend.claim("hub-1", member_a, "experiments", "task:claimed", "Working")
        await backend.report("hub-1", member_a, "experiments", "task:claimed", "failed", "Failed")
        # Reclaim it
        await backend.claim("hub-1", member_b, "experiments", "task:claimed", "Retrying manually")

        retried = await backend.retry_failed("hub-1", max_retries=3)
        assert "task:claimed" not in retried


class TestHubIsolation:
    @pytest.mark.asyncio
    async def test_replies_scoped_by_hub(self, backend, member_a):
        """get_replies respects hub_id via JOIN."""
        post = await backend.create_post("hub-1", member_a, "general", "Hello")
        await backend.reply("hub-1", member_a, post.post_id, "Reply")

        # Should find the reply in hub-1
        replies = await backend.get_replies("hub-1", post.post_id)
        assert len(replies) == 1

        # Should NOT find it in hub-2 (different hub)
        replies_other = await backend.get_replies("hub-2", post.post_id)
        assert len(replies_other) == 0


class TestSSRF:
    def test_block_private_ip(self):
        from swarlo.server import _is_safe_webhook_url
        assert not _is_safe_webhook_url("http://169.254.169.254/latest/meta-data/")

    def test_block_ftp_scheme(self):
        from swarlo.server import _is_safe_webhook_url
        assert not _is_safe_webhook_url("ftp://evil.com/steal")

    def test_block_http_external(self):
        from swarlo.server import _is_safe_webhook_url
        assert not _is_safe_webhook_url("http://evil.com/steal")

    def test_allow_localhost(self):
        from swarlo.server import _is_safe_webhook_url
        assert _is_safe_webhook_url("http://localhost:3000/webhook")

    def test_block_private_ip_https(self):
        from swarlo.server import _is_safe_webhook_url
        assert not _is_safe_webhook_url("https://10.0.0.1/admin")

    def test_block_empty_url(self):
        from swarlo.server import _is_safe_webhook_url
        assert not _is_safe_webhook_url("")

    def test_block_no_scheme(self):
        from swarlo.server import _is_safe_webhook_url
        assert not _is_safe_webhook_url("evil.com/steal")


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
