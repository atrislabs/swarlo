"""Typed posts tests: 4000-char limit, 500-char preview, allowed kinds, claim-like note warning."""

import os
import shutil
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

from swarlo.__main__ import _print_posts, main
from swarlo.git_dag import GitDAG
from swarlo.server import _post_attempts, _task_post_attempts, app, set_backend, set_dag
from swarlo.sqlite_backend import SQLiteBackend
from swarlo.types import (
    MAX_POST_CONTENT_LENGTH,
    POST_PREVIEW_LENGTH,
    TYPED_POST_KINDS,
    Member,
)


@pytest.fixture(autouse=True)
def fresh_backend():
    _post_attempts.clear()
    _task_post_attempts.clear()
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
    shutil.rmtree(git_dir, ignore_errors=True)


@pytest.fixture
def client():
    return TestClient(app)


def _auth(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _register(client: TestClient, member_id: str = "agent-1", name: str = "Hugo", hub: str = "atris") -> str:
    sponsors = getattr(client, "_test_hub_sponsors", {})
    sponsor_key = sponsors.get(hub)
    resp = client.post(
        "/api/register",
        headers=_auth(sponsor_key),
        json={
            "member_id": member_id,
            "member_type": "agent",
            "member_name": name,
            "hub_id": hub,
        },
    )
    assert resp.status_code == 201
    key = resp.json()["api_key"]
    sponsors.setdefault(hub, key)
    client._test_hub_sponsors = sponsors
    return key


def _post(client: TestClient, key: str, content: str, kind: str = "note", channel: str = "general"):
    return client.post(
        f"/api/atris/channels/{channel}/posts",
        headers=_auth(key),
        json={"content": content, "kind": kind},
    )


class TestPostBodyLimit:
    def test_4000_char_post_accepted_and_preserved(self, client):
        key = _register(client)
        body = "x" * MAX_POST_CONTENT_LENGTH
        resp = _post(client, key, body, kind="note")
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["content"]) == MAX_POST_CONTENT_LENGTH

        read_resp = client.get("/api/atris/channels/general/posts", headers=_auth(key))
        assert read_resp.status_code == 200
        assert len(read_resp.json()["posts"][0]["content"]) == MAX_POST_CONTENT_LENGTH

    def test_over_4000_char_post_rejected_with_400(self, client):
        key = _register(client)
        body = "x" * (MAX_POST_CONTENT_LENGTH + 1)
        resp = _post(client, key, body, kind="note")
        assert resp.status_code == 400
        assert "4000" in resp.json()["detail"]

    @pytest.mark.parametrize("empty_body", ["", "   ", "\n\t  \n"])
    def test_empty_or_whitespace_body_rejected_with_400(self, client, empty_body):
        key = _register(client)
        resp = _post(client, key, empty_body, kind="note")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_backend_rejects_empty_and_over_limit(self, fresh_backend):
        member = Member(member_id="m1", member_type="agent", member_name="M1", hub_id="h1")
        with pytest.raises(ValueError):
            await fresh_backend.create_post("h1", member, "general", "   ", kind="note")
        with pytest.raises(ValueError):
            await fresh_backend.create_post("h1", member, "general", "a" * 4001, kind="note")


class TestPostPreviewAndExpandMarker:
    def test_short_post_not_truncated(self, client):
        key = _register(client)
        body = "a" * POST_PREVIEW_LENGTH
        resp = _post(client, key, body, kind="note")
        assert resp.status_code == 201
        data = resp.json()
        assert "preview" not in data
        assert "truncated" not in data

    def test_long_post_has_500_char_preview_and_expand_marker(self, client):
        key = _register(client)
        body = ("a" * POST_PREVIEW_LENGTH) + ("b" * 150)
        resp = _post(client, key, body, kind="result")
        assert resp.status_code == 201
        created = resp.json()
        assert created["content"] == body
        assert created["truncated"] is True
        assert created["preview"] == f"{'a' * POST_PREVIEW_LENGTH}... [truncated, 150 more chars]"

        read_resp = client.get("/api/atris/channels/general/posts", headers=_auth(key))
        assert read_resp.status_code == 200
        post = read_resp.json()["posts"][0]
        assert post["content"] == body
        assert post["truncated"] is True
        assert post["preview"] == f"{'a' * POST_PREVIEW_LENGTH}... [truncated, 150 more chars]"

    def test_cli_print_posts_truncates_by_default_and_expands_with_full(self, capsys):
        body = ("p" * POST_PREVIEW_LENGTH) + ("q" * 42)
        posts = [{"kind": "note", "member_name": "Hugo", "content": body}]

        _print_posts(posts)
        out = capsys.readouterr().out
        assert "... [truncated, 42 more chars]" in out
        assert "q" * 42 not in out

        _print_posts(posts, full=True)
        out_full = capsys.readouterr().out
        assert "q" * 42 in out_full
        assert "[truncated," not in out_full

    def test_cli_read_command_respects_full_flag(self, monkeypatch, capsys):
        body = ("m" * POST_PREVIEW_LENGTH) + ("z" * 25)
        monkeypatch.setattr(
            "swarlo.__main__._require_runtime",
            lambda _args: {"server": "http://localhost:8080", "hub": "atris", "api_key": "k"},
        )
        monkeypatch.setattr(
            "swarlo.__main__._request",
            lambda *_a, **_kw: (200, {"posts": [{"kind": "note", "member_name": "Hugo", "content": body}]}),
        )

        monkeypatch.setattr(sys, "argv", ["swarlo", "read", "general"])
        main()
        out = capsys.readouterr().out
        assert "... [truncated, 25 more chars]" in out

        monkeypatch.setattr(sys, "argv", ["swarlo", "read", "general", "--full"])
        main()
        out_full = capsys.readouterr().out
        assert "z" * 25 in out_full
        assert "[truncated," not in out_full


class TestTypedPostKinds:
    @pytest.mark.parametrize("kind", sorted(TYPED_POST_KINDS))
    def test_all_required_typed_kinds_accepted(self, client, kind):
        key = _register(client)
        resp = _post(client, key, f"post of kind {kind}", kind=kind)
        assert resp.status_code == 201
        assert resp.json()["kind"] == kind

    @pytest.mark.parametrize("legacy_kind", ["message", "status", "nudge", "question", "escalation", "briefing"])
    def test_legacy_kinds_still_accepted_for_compatibility(self, client, legacy_kind):
        key = _register(client)
        resp = _post(client, key, f"legacy {legacy_kind}", kind=legacy_kind)
        assert resp.status_code == 201
        assert resp.json()["kind"] == legacy_kind

    @pytest.mark.parametrize("bad_kind", ["yolo", "bogus", "", "chat"])
    def test_disallowed_kind_rejected_with_400(self, client, bad_kind):
        key = _register(client)
        resp = _post(client, key, "valid content", kind=bad_kind)
        assert resp.status_code == 400
        assert "Invalid post kind" in resp.json()["detail"]


class TestClaimLikeNoteWarning:
    @pytest.mark.parametrize(
        "phrase",
        [
            "claiming this task now",
            "I'll take the auth refactor",
            "working on the migration",
            "on it right now",
            "taking this one",
        ],
    )
    def test_claim_like_note_surfaces_warning_without_failing(self, client, phrase):
        key = _register(client)
        resp = _post(client, key, phrase, kind="note")
        assert resp.status_code == 201
        data = resp.json()
        assert data["kind"] == "note"
        assert "warning" in data
        assert "claim" in data["warning"].lower()

    def test_normal_note_has_no_warning(self, client):
        key = _register(client)
        resp = _post(client, key, "Benchmark finished in 1.2s.", kind="note")
        assert resp.status_code == 201
        assert "warning" not in resp.json()

    def test_claim_kind_with_claim_language_has_no_warning(self, client):
        key = _register(client)
        resp = _post(client, key, "claiming this task now", kind="claim")
        assert resp.status_code == 201
        assert "warning" not in resp.json()

    def test_cli_post_prints_warning_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "swarlo.__main__._require_runtime",
            lambda _args: {"server": "http://localhost:8080", "hub": "atris", "api_key": "k"},
        )
        monkeypatch.setattr(
            "swarlo.__main__._request",
            lambda *_a, **_kw: (
                201,
                {
                    "kind": "note",
                    "channel": "general",
                    "warning": "Post kind is 'note' but content contains claim-like language",
                },
            ),
        )
        monkeypatch.setattr(sys, "argv", ["swarlo", "post", "general", "working on it", "--kind", "note"])
        main()
        captured = capsys.readouterr()
        assert "warning:" in captured.err
        assert "Posted [note] to #general" in captured.out
