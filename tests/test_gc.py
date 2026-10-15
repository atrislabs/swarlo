import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from swarlo.gc import (
    archive_posts,
    drop_idle_members,
    keep_backups,
    record_run,
    rotate_logs,
    start_schedule,
    trim_scores,
    vacuum,
)
from swarlo.sqlite_backend import SQLiteBackend

MB = 1024 * 1024


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


def test_trim_scores_drops_old_rows_for_one_hub(live):
    rows = [("hub-1", _ago(10)), ("hub-1", _ago(8)), ("hub-1", _ago(1)), ("hub-2", _ago(10))]
    with sqlite3.connect(live) as c:
        c.executemany(
            "INSERT INTO scores (hub_id, agents_active, tasks_claimed, tasks_shipped, "
            "avg_time_to_claim, file_conflicts, files_with_multi_editors, coord_score, computed_at) "
            "VALUES (?, 0, 0, 0, 0, 0, 0, 0, ?)",
            rows,
        )

    assert trim_scores(live, "hub-1", days=7) == 2
    with sqlite3.connect(live) as c:
        left = sorted(c.execute("SELECT hub_id, computed_at FROM scores").fetchall())
    assert left == sorted([rows[2], rows[3]])
    assert trim_scores(live, "hub-1", days=7) == 0


def test_drop_idle_members_removes_unseen_and_stale_for_one_hub(live):
    rows = [
        ("never", "hub-1", None),
        ("stale", "hub-1", _ago(31)),
        ("fresh", "hub-1", _ago(1)),
        ("other", "hub-2", _ago(31)),
    ]
    with sqlite3.connect(live) as c:
        c.executemany(
            "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at, last_seen) "
            "VALUES (?, ?, 'agent', 'x', '2026-01-01T00:00:00', ?)",
            rows,
        )

    assert sorted(drop_idle_members(live, "hub-1", days=30)) == ["never", "stale"]
    with sqlite3.connect(live) as c:
        left = sorted(r[0] for r in c.execute("SELECT member_id FROM members"))
    assert left == ["fresh", "other"]
    assert drop_idle_members(live, "hub-1", days=30) == []


def test_rotate_logs_copy_truncates_big_log(tmp_path):
    log = tmp_path / "monitor.log"
    log.write_bytes(b"a" * MB + b"b" * MB)
    inode = log.stat().st_ino

    rotated = rotate_logs(tmp_path, max_mb=1)

    assert rotated == [log]
    assert log.stat().st_ino == inode
    assert log.stat().st_size == 0
    # the .1 copy is capped to the newest max_mb bytes
    assert (tmp_path / "monitor.log.1").read_bytes() == b"b" * MB


def test_rotate_logs_open_writer_keeps_writing(tmp_path):
    log = tmp_path / "swarlo.log"
    with open(log, "ab") as writer:
        writer.write(b"x" * (2 * MB))
        writer.flush()
        rotate_logs(tmp_path, max_mb=1)
        writer.write(b"after\n")
        writer.flush()
    assert log.read_bytes() == b"after\n"


def test_rotate_logs_leaves_small_logs_and_replaces_old_copy(tmp_path):
    small = tmp_path / "small.log"
    small.write_bytes(b"tiny")
    big = tmp_path / "big.log"
    big.write_bytes(b"new" * MB)
    (tmp_path / "big.log.1").write_bytes(b"old")
    (tmp_path / "notes.txt").write_bytes(b"z" * (2 * MB))

    rotated = rotate_logs(tmp_path, max_mb=1)

    assert rotated == [big]
    assert small.read_bytes() == b"tiny"
    assert (tmp_path / "big.log.1").stat().st_size == MB
    assert not (tmp_path / "small.log.1").exists()
    assert (tmp_path / "notes.txt").stat().st_size == 2 * MB
    assert not list(tmp_path.glob("*.tmp"))


def test_keep_backups_keeps_newest_by_mtime(tmp_path):
    names = [
        "atris.db.backup-doctor-1778184753",
        "atris.db.backup-pre-prune-20261015-154101",
        "atris.db.backup-cleanup-1778183849",
        "atris.db.backup-pre-restart-20261015-045135",
        "atris.db.backup-doctor-1778989651",
    ]
    for age, name in enumerate(names):
        p = tmp_path / name
        p.write_bytes(b"db")
        t = 1_000_000 - age * 100
        os.utime(p, (t, t))
    (tmp_path / "atris.db").write_bytes(b"live")
    (tmp_path / "atris.db-wal").write_bytes(b"wal")

    removed = keep_backups(tmp_path, keep=3)

    assert sorted(p.name for p in removed) == sorted(names[3:])
    left = sorted(p.name for p in tmp_path.glob("atris.db.backup*"))
    assert left == sorted(names[:3])
    assert (tmp_path / "atris.db").read_bytes() == b"live"
    assert (tmp_path / "atris.db-wal").exists()


def test_keep_backups_does_not_count_sidecars(tmp_path):
    def make(name, t, data=b"db"):
        p = tmp_path / name
        p.write_bytes(data)
        os.utime(p, (t, t))

    make("atris.db.backup-a", 400)
    make("atris.db.backup-a-shm", 401)
    make("atris.db.backup-a-wal", 401, b"")
    make("atris.db.backup-b", 300)
    make("atris.db.backup-b-wal", 300, b"frames")
    make("atris.db.backup-b-shm", 300)
    make("atris.db.backup-c", 200)
    make("atris.db.backup-d", 100)
    make("atris.db.backup-d-wal", 100, b"frames")

    removed = keep_backups(tmp_path, keep=3)

    assert sorted(p.name for p in removed) == [
        "atris.db.backup-a-shm",
        "atris.db.backup-a-wal",
        "atris.db.backup-d",
        "atris.db.backup-d-wal",
    ]
    left = sorted(p.name for p in tmp_path.glob("atris.db.backup*"))
    assert left == [
        "atris.db.backup-a",
        "atris.db.backup-b",
        "atris.db.backup-b-shm",
        "atris.db.backup-b-wal",
        "atris.db.backup-c",
    ]


def test_keep_backups_noop_when_few(tmp_path):
    (tmp_path / "atris.db.backup-1").write_bytes(b"db")
    assert keep_backups(tmp_path) == []
    assert (tmp_path / "atris.db.backup-1").exists()


def _lines(home):
    return [json.loads(l) for l in (home / "prune.jsonl").read_text().splitlines()]


def test_record_run_appends_one_line_per_call(tmp_path):
    (tmp_path / "atris.db").write_bytes(b"x" * MB)

    rec = record_run(tmp_path, "manual", True, archived=4, logs=["swarlo.log"])
    record_run(tmp_path, "schedule", False, error="boom")

    lines = _lines(tmp_path)
    assert lines[0] == rec
    assert len(lines) == 2
    assert rec["trigger"] == "manual" and rec["ok"] is True
    assert rec["archived"] == 4 and rec["logs"] == ["swarlo.log"]
    assert rec["mb_before"] == rec["mb_after"] == 1.0
    at = datetime.fromisoformat(rec["at"])
    assert at.utcoffset() == timedelta(0)
    assert abs((datetime.now(timezone.utc) - at).total_seconds()) < 60
    assert lines[1]["ok"] is False and lines[1]["error"] == "boom"


def test_record_run_keeps_caller_mb_before(tmp_path):
    rec = record_run(tmp_path, "manual", True, mb_before=155.0)
    assert rec["mb_before"] == 155.0
    assert rec["mb_after"] == 0.0


def test_schedule_runs_and_records_with_schedule_trigger(tmp_path):
    big = tmp_path / "swarlo.log"
    big.write_bytes(b"x" * (2 * MB))
    calls = []

    def run_fn():
        calls.append(1)
        big.write_bytes(b"")
        return {"archived": 7, "scores": 3}

    async def main():
        task = start_schedule(run_fn, tmp_path, interval_hours=0.05 / 3600)
        await asyncio.sleep(0.12)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return task

    task = asyncio.run(main())
    assert task.cancelled()
    lines = _lines(tmp_path)
    assert len(calls) >= 2 and len(lines) == len(calls)
    first = lines[0]
    assert first["trigger"] == "schedule" and first["ok"] is True
    assert first["archived"] == 7 and first["scores"] == 3
    assert first["mb_before"] == 2.0 and first["mb_after"] == 0.0


def test_schedule_records_failure_and_keeps_going(tmp_path):
    async def run_fn():
        raise RuntimeError("db locked")

    async def main():
        task = start_schedule(run_fn, tmp_path, interval_hours=0.03 / 3600)
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    lines = _lines(tmp_path)
    assert len(lines) >= 2
    assert all(l["ok"] is False and l["trigger"] == "schedule" for l in lines)
    assert lines[0]["error"] == "RuntimeError: db locked"


def test_schedule_waits_out_a_recent_scheduled_run(tmp_path):
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (tmp_path / "prune.jsonl").write_text(json.dumps({"at": recent, "trigger": "schedule", "ok": True}) + "\n")
    calls = []

    async def main():
        task = start_schedule(lambda: calls.append(1), tmp_path, interval_hours=24)
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert calls == []
    assert len(_lines(tmp_path)) == 1


def _row_counts(db):
    with sqlite3.connect(db) as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def test_vacuum_frees_pages_and_keeps_rows(live):
    for i in range(400):
        _post(live, f"p{i}", _ago(1))
    with sqlite3.connect(live) as c:
        c.execute("UPDATE posts SET content = ? ", ("x" * 2000,))
        c.execute("DELETE FROM posts WHERE post_id != 'p0'")
        free_before = c.execute("PRAGMA freelist_count").fetchone()[0]
    counts = _row_counts(live)
    assert free_before > 100

    freed = vacuum(live)

    assert freed >= free_before
    assert _row_counts(live) == counts
    with sqlite3.connect(live) as c:
        assert c.execute("PRAGMA freelist_count").fetchone()[0] == 0
    wal = live.with_name(live.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
