import os

from swarlo.gc_files import keep_backups, rotate_logs

MB = 1024 * 1024


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


def test_keep_backups_noop_when_few(tmp_path):
    (tmp_path / "atris.db.backup-1").write_bytes(b"db")
    assert keep_backups(tmp_path) == []
    assert (tmp_path / "atris.db.backup-1").exists()
