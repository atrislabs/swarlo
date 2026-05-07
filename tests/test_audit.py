"""Tests for swarlo audit (hub data health check + cleanup)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from swarlo.audit import _detect_findings, run_audit


def _make_schema(con: sqlite3.Connection) -> None:
    """Build the minimal posts/members schema audit reads from."""
    con.executescript("""
        CREATE TABLE members (
            member_id TEXT NOT NULL,
            hub_id TEXT NOT NULL,
            member_type TEXT NOT NULL,
            member_name TEXT NOT NULL,
            api_key TEXT,
            webhook_url TEXT,
            created_at TEXT NOT NULL,
            last_seen TEXT,
            last_active TEXT,
            PRIMARY KEY (member_id, hub_id)
        );
        CREATE TABLE posts (
            post_id TEXT PRIMARY KEY,
            hub_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            member_id TEXT NOT NULL,
            member_name TEXT NOT NULL,
            member_type TEXT NOT NULL,
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'message',
            task_key TEXT,
            status TEXT,
            metadata TEXT,
            mentions TEXT,
            created_at TEXT NOT NULL
        );
    """)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """Create a fresh SQLite DB with the audit-relevant schema."""
    p = tmp_path / "audit.db"
    con = sqlite3.connect(str(p))
    _make_schema(con)
    con.commit()
    con.close()
    return p


def test_clean_db_returns_no_findings(db: Path, tmp_path: Path) -> None:
    """A pristine DB with no rows should yield zero findings."""
    findings, fixable = _detect_findings(db, "atris", tmp_path / "keys", tmp_path / "lock")
    assert findings == []
    assert fixable == []


def test_cross_hub_data_is_high_severity(db: Path, tmp_path: Path) -> None:
    """A member or post in a non-canonical hub_id should fire 'single-hub' high."""
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        ("legacy-agent", "old-uuid-hub", "agent", "Legacy"),
    )
    con.commit()
    con.close()
    findings, fixable = _detect_findings(db, "atris", tmp_path / "keys", tmp_path / "lock")
    codes = [f.code for f in findings]
    assert "single-hub" in codes
    assert any(f.severity == "high" and f.code == "single-hub" for f in findings)
    assert any(a.label == "single-hub" for a in fixable)


def test_repeated_nudges_flagged_as_false_alarm(db: Path, tmp_path: Path) -> None:
    """6 identical nudges from one member in 7 days should fire 'false-alarm-nudges'."""
    con = sqlite3.connect(str(db))
    for i in range(6):
        con.execute(
            "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, "
            "member_type, content, kind, created_at) "
            "VALUES (?, 'atris', 'general', 'orch', 'Orchestrator', 'agent', "
            "'[orch] 246 unpushed commit(s) on master', 'nudge', datetime('now'))",
            (f"p{i}",),
        )
    con.commit()
    con.close()
    findings, fixable = _detect_findings(db, "atris", tmp_path / "keys", tmp_path / "lock")
    codes = [f.code for f in findings]
    assert "false-alarm-nudges" in codes


def test_fix_prunes_cross_hub_and_backs_up(db: Path, tmp_path: Path, capsys) -> None:
    """run_audit(fix=True) should back up the DB and delete cross-hub rows."""
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at) "
        "VALUES ('legacy', 'old-uuid', 'agent', 'Legacy', datetime('now'))"
    )
    con.commit()
    con.close()

    rc = run_audit(db_path=db, hub_id="atris", key_dir=tmp_path / "keys",
                   lock_path=tmp_path / "lock", fix=True)
    assert rc == 0

    # Backup file should exist alongside the DB.
    backups = list(db.parent.glob("audit.db.backup-audit-*"))
    assert len(backups) == 1, f"expected one backup, got {backups}"

    # Cross-hub row should be gone.
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM members WHERE hub_id != 'atris'").fetchone()[0]
    con.close()
    assert n == 0


def test_clean_db_exits_zero(db: Path, tmp_path: Path) -> None:
    """run_audit on a clean DB should exit 0 and print 'clean'."""
    rc = run_audit(db_path=db, hub_id="atris", key_dir=tmp_path / "keys",
                   lock_path=tmp_path / "lock", fix=False)
    assert rc == 0
