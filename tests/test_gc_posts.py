import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from swarlo.gc_posts import archive_posts
from swarlo.sqlite_backend import SQLiteBackend


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "live.db"
    backend = SQLiteBackend(str(path))
    backend.conn  # first connection builds the full schema + migrations
    backend.close()
    return path


def _post(db, post_id, created_at, hub="hub-1", kind="message", status=None, task_key=None):
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, "
            "content, kind, task_key, status, created_at) VALUES (?, ?, 'general', 'm1', 'Hugo', "
            "'agent', ?, ?, ?, ?, ?)",
            (post_id, hub, f"body {post_id}", kind, task_key, status, created_at),
        )


def _reply(db, reply_id, post_id, created_at, hub="hub-1"):
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO replies (reply_id, post_id, hub_id, member_id, member_name, member_type, "
            "content, created_at) VALUES (?, ?, ?, 'm1', 'Hugo', 'agent', 'ok', ?)",
            (reply_id, post_id, hub, created_at),
        )


def _ids(db, table, col):
    with sqlite3.connect(db) as c:
        return {r[0] for r in c.execute(f"SELECT {col} FROM {table}")}


def _month(created_at):
    return created_at[:7]


def test_old_posts_and_replies_move_to_month_archive(live, tmp_path):
    old, new = _ago(45), _ago(2)
    _post(live, "p-old", old)
    _reply(live, "r-old", "p-old", old)
    _reply(live, "r-late", "p-old", new)  # a fresh reply still follows its post
    _post(live, "p-new", new)
    _reply(live, "r-new", "p-new", new)

    result = archive_posts(live, tmp_path / "arc", "hub-1", days=30)

    arc = tmp_path / "arc" / f"hub-1-{_month(old)}.db"
    assert result == {arc: 1}
    assert _ids(arc, "posts", "post_id") == {"p-old"}
    assert _ids(arc, "replies", "reply_id") == {"r-old", "r-late"}
    assert _ids(live, "posts", "post_id") == {"p-new"}
    assert _ids(live, "replies", "reply_id") == {"r-new"}

    with sqlite3.connect(live) as a, sqlite3.connect(arc) as b:
        for table in ("posts", "replies"):
            assert a.execute(f"PRAGMA table_info({table})").fetchall() == \
                b.execute(f"PRAGMA table_info({table})").fetchall()
        row = b.execute("SELECT content, member_name FROM posts").fetchone()
    assert row == ("body p-old", "Hugo")


def test_open_claims_are_never_touched(live, tmp_path):
    old = _ago(90)
    _post(live, "c-open", old, kind="claim", status="open", task_key="t1")
    _reply(live, "r-open", "c-open", old)
    _post(live, "c-done", old, kind="claim", status="done", task_key="t2")

    result = archive_posts(live, tmp_path / "arc", "hub-1", days=30)

    assert sum(result.values()) == 1
    assert _ids(live, "posts", "post_id") == {"c-open"}
    assert _ids(live, "replies", "reply_id") == {"r-open"}
    arc = tmp_path / "arc" / f"hub-1-{_month(old)}.db"
    assert _ids(arc, "posts", "post_id") == {"c-done"}


def test_other_hubs_stay_and_months_split(live, tmp_path):
    a, b = _ago(40), _ago(80)
    assert _month(a) != _month(b)
    _post(live, "p-a", a)
    _post(live, "p-b", b)
    _post(live, "p-other", b, hub="hub-2")

    result = archive_posts(live, tmp_path / "arc", "hub-1", days=30)

    arc_a = tmp_path / "arc" / f"hub-1-{_month(a)}.db"
    arc_b = tmp_path / "arc" / f"hub-1-{_month(b)}.db"
    assert result == {arc_a: 1, arc_b: 1}
    assert _ids(arc_a, "posts", "post_id") == {"p-a"}
    assert _ids(arc_b, "posts", "post_id") == {"p-b"}
    assert _ids(live, "posts", "post_id") == {"p-other"}


def test_rerun_appends_to_existing_archive(live, tmp_path):
    old = _ago(45)
    _post(live, "p1", old)
    archive_posts(live, tmp_path / "arc", "hub-1", days=30)
    assert archive_posts(live, tmp_path / "arc", "hub-1", days=30) == {}

    _post(live, "p2", old)
    archive_posts(live, tmp_path / "arc", "hub-1", days=30)

    arc = tmp_path / "arc" / f"hub-1-{_month(old)}.db"
    assert _ids(arc, "posts", "post_id") == {"p1", "p2"}
    assert _ids(live, "posts", "post_id") == set()


def test_count_mismatch_leaves_live_db_untouched(live, tmp_path):
    old = _ago(45)
    _post(live, "p1", old)
    _reply(live, "r1", "p1", old)
    arc_dir = tmp_path / "arc"
    arc_dir.mkdir()
    arc = arc_dir / f"hub-1-{_month(old)}.db"
    # A broken archive that silently rejects every copied post.
    with sqlite3.connect(arc) as c:
        c.execute("CREATE TABLE posts (post_id TEXT PRIMARY KEY, guard TEXT NOT NULL)")

    with pytest.raises(RuntimeError, match="archived 0 of 1"):
        archive_posts(live, arc_dir, "hub-1", days=30)

    assert _ids(live, "posts", "post_id") == {"p1"}
    assert _ids(live, "replies", "reply_id") == {"r1"}
