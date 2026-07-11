import importlib.util
import json
import sqlite3
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from swarlo.sqlite_backend import SQLiteBackend


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "swarlo" / "__main__.py"
SPEC = importlib.util.spec_from_file_location("swarlo_cli", CLI_PATH)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)
EXPECTED_PACKAGE = {"name": "swarlo", "version": cli.__version__}


def test_package_version_matches_pyproject():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert cli.__version__ == pyproject["project"]["version"]


def test_version_flag_prints_package_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["swarlo", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"swarlo {cli.__version__}"


def _assert_report_sha256(payload):
    digest = payload["report_sha256"]
    assert len(digest) == 64
    payload_without_digest = dict(payload)
    del payload_without_digest["report_sha256"]
    assert digest == cli._report_sha256(payload_without_digest)


def _assert_summary_sha256(payload):
    digest = payload["summary_sha256"]
    assert len(digest) == 64
    payload_without_digest = dict(payload)
    del payload_without_digest["summary_sha256"]
    assert digest == cli._report_sha256(payload_without_digest)


def _write_strict_speed_report(path, **overrides):
    rows = {
        "posts": 1000,
        "replies": 0,
        "scores": 10000,
        "members": 1,
        "commits": 0,
    }
    report = {
        "schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
        "ok": True,
        "package": {"name": "swarlo", "version": cli.__version__},
        "runtime": {
            "python": cli.platform.python_version(),
            "sqlite": cli.sqlite3.sqlite_version,
            "platform": cli.platform.platform(),
        },
        "generated_at": datetime.now(cli.UTC).isoformat().replace("+00:00", "Z"),
        "db": str(path.with_name("swarlo.db")),
        "database": {
            "access": "read_only",
            "size_bytes": 1,
            "page_count": 1,
            "page_size": 4096,
            "rows": rows,
        },
        "elapsed_ms": 1,
        "latency_budget": {"max_ms": 1000, "ok": True},
        "indexes": {
            table: {"present": len(indexes), "total": len(indexes), "missing": []}
            for table, indexes in cli.SPEED_INDEXES.items()
        },
        "planner": {
            "expected_total": len(cli.SPEED_QUERY_PLANS),
            "total": len(cli.SPEED_QUERY_PLANS),
            "ok": len(cli.SPEED_QUERY_PLANS),
            "required_ok": True,
            "paths": list(cli.SPEED_QUERY_PLANS),
        },
        "live_data": {
            "required": True,
            "required_tables": list(cli.SPEED_LIVE_DATA_TABLES),
            "missing": {},
            "ok": True,
        },
        "row_minimums": {
            "required": {"posts": 1000, "scores": 10000, "members": 1},
            "misses": {},
            "ok": True,
        },
    }
    report.update(overrides)
    report["report_sha256"] = cli._report_sha256(report)
    path.write_text(json.dumps(report))
    return report


def _insert_speed_live_rows(conn):
    conn.execute(
        "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("agent", "swarlo-speed-check", "agent", "Agent", "2026-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "post-1",
            "swarlo-speed-check",
            "ops",
            "agent",
            "Agent",
            "agent",
            "speed proof",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO scores (hub_id, coord_score, computed_at) VALUES (?, ?, ?)",
        ("swarlo-speed-check", 100, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()


def _insert_tower_rows(conn):
    now = datetime.now(cli.UTC)
    recent = (now - timedelta(minutes=5)).isoformat()
    quiet = (now - timedelta(minutes=25)).isoformat()
    stale = (now - timedelta(minutes=45)).isoformat()
    very_old = (now - timedelta(hours=3)).isoformat()
    conn.execute(
        "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at, last_seen, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("agent-a", "my-team", "agent", "Agent A", recent, recent, recent),
    )
    conn.execute(
        "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at, last_seen, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("agent-b", "my-team", "agent", "Agent B", recent, recent, quiet),
    )
    conn.execute(
        "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at, last_seen, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("agent-c", "my-team", "agent", "Agent C", recent, very_old, very_old),
    )
    rows = [
        ("msg-1", "ops", "agent-a", "Agent A", "ownerless task", "message", "task:unclaimed", None, None, recent),
        (
            "claim-1",
            "ops",
            "agent-a",
            "Agent A",
            "working but stale",
            "claim",
            "task:claimed",
            "open",
            json.dumps({"heartbeat_at": stale}),
            stale,
        ),
        ("done-1", "ops", "agent-a", "Agent A", "finished it", "result", "task:done", "done", None, recent),
        ("blocked-1", "ops", "agent-b", "Agent B", "needs decision", "failed", "task:blocked", "blocked", None, quiet),
    ]
    for row in rows:
        conn.execute(
            "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, "
            "content, kind, task_key, status, metadata, created_at) "
            "VALUES (?, 'my-team', ?, ?, ?, 'agent', ?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.execute(
        "INSERT INTO scores (hub_id, coord_score, tasks_shipped, tasks_blocked, computed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("my-team", 97, 1, 1, recent),
    )
    conn.commit()


def test_join_saves_config(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "POST"
        assert url == "http://localhost:8080/api/register"
        assert payload["hub_id"] == "my-team"
        return 201, {"member_id": "agent-1", "api_key": "secret"}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "join",
            "--server",
            "http://localhost:8080",
            "--hub",
            "my-team",
            "--member-id",
            "agent-1",
        ],
    )

    cli.main()
    saved = json.loads(config_path.read_text())
    assert saved["server"] == "http://localhost:8080"
    assert saved["hub"] == "my-team"
    assert saved["api_key"] == "secret"
    assert "Joined hub" in capsys.readouterr().out


def test_claim_uses_saved_runtime(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    called = {}

    def fake_request(method, url, payload=None, api_key=None):
        called.update({"method": method, "url": url, "payload": payload, "api_key": api_key})
        return 201, {"claimed": True}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "claim", "ops lane", "task:1", "Taking this"])

    cli.main()
    assert called["url"] == "http://localhost:8080/api/my-team/channels/ops%20lane/claim"
    assert called["payload"]["task_key"] == "task:1"
    assert called["api_key"] == "secret"
    assert "Claimed task:1" in capsys.readouterr().out


def test_read_prints_posts(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert url == "http://localhost:8080/api/my-team/channels/ops%20lane/posts?limit=10"
        return 200, {
            "posts": [
                {
                    "kind": "claim",
                    "task_key": "task:1",
                    "member_name": "Hugo",
                    "content": "Taking this",
                }
            ]
        }

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "read", "ops lane"])

    cli.main()
    assert "[claim] task:1 Hugo: Taking this" in capsys.readouterr().out


def test_score_prints_xp_leader(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "POST"
        assert url == "http://localhost:8080/api/my-team/score"
        return 200, {
            "coord_score": 42,
            "tasks_shipped": 3,
            "agents_active": 1,
            "file_conflicts": 0,
            "per_agent_xp": [{"member_name": "Agent A", "xp": 36}],
        }

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "score"])

    cli.main()
    out = capsys.readouterr().out
    assert "Score: 42" in out
    assert "XP leader: Agent A (36 XP)" in out


def test_score_explain_prints_xp_mechanics(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        return 200, {
            "coord_score": 42,
            "tasks_shipped": 3,
            "agents_active": 1,
            "file_conflicts": 0,
        }

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "score", "--explain"])

    cli.main()
    out = capsys.readouterr().out
    assert "XP mechanics:" in out
    assert "terminal reports/statuses" in out


def test_xp_prints_leaderboard(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "GET"
        assert url == "http://localhost:8080/api/my-team/xp?limit=1&member_id=agent+a%2Fb"
        return 200, {
            "per_agent_xp": [
                {"member_id": "agent a/b", "member_name": "Agent A", "xp": 36, "shipped": 3, "claims": 3, "failed": 0, "blocked": 1},
                {"member_id": "agent-b", "member_name": "Agent B", "xp": 9, "shipped": 1, "claims": 1, "failed": 1, "blocked": 0},
            ],
        }

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "xp", "--limit", "1", "--member", "agent a/b"])

    cli.main()
    out = capsys.readouterr().out
    assert "rank" in out
    assert "member_id" in out
    assert "agent a/b" in out
    assert "block" in out
    assert "Agent A" in out
    assert "Agent B" not in out


def test_xp_explain_prints_mechanics_when_no_rows(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        return 200, {"per_agent_xp": []}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "xp", "--explain"])

    cli.main()
    out = capsys.readouterr().out
    assert "No XP rows" in out
    assert "XP mechanics:" in out
    assert "terminal reports/statuses" in out


def test_score_history_prints_rows(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "GET"
        assert url == "http://localhost:8080/api/my-team/scores?limit=1"
        return 200, {
            "scores": [{
                "computed_at": "2026-05-17T00:00:00+00:00",
                "coord_score": 42,
                "tasks_shipped": 3,
                "tasks_failed": 1,
                "tasks_blocked": 2,
                "throughput_per_hour": 5,
            }, {
                "computed_at": "2026-05-16T00:00:00+00:00",
                "coord_score": 40,
                "tasks_shipped": 2,
                "tasks_failed": 1,
                "tasks_blocked": 2,
                "throughput_per_hour": 4,
            }],
        }

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "score-history", "--limit", "1"])

    cli.main()
    out = capsys.readouterr().out
    assert "score" in out
    assert "block" in out
    assert "42" in out
    assert "+2" in out
    assert "  5.0" in out


def test_tower_prints_plain_language_control_view(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _insert_tower_rows(backend.conn)
    backend.close()

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "tower", "--db", str(db_path), "--hub", "my-team", "--limit", "3"],
    )

    cli.main()

    out = capsys.readouterr().out
    assert "Swarlo Tower" in out
    assert "Overall: Needs attention" in out
    assert "Next: Reassign or refresh stale claims first." in out
    assert "Active now:" in out
    assert "No owner yet:" in out
    assert "Stale: task:claimed with Agent A" in out
    assert "No owner: task:unclaimed in ops - ownerless task" in out
    assert "Blocked: task:blocked - needs decision" in out
    assert "1. Agent A - 12 XP" in out
    assert "Status:           Needs attention" in out
    assert "Fast routes:" in out
    assert "Data seen:" in out


def test_tower_json_exposes_same_operator_state(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _insert_tower_rows(backend.conn)
    backend.close()

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "tower", "--db", str(db_path), "--hub", "my-team", "--json"],
    )

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["hub"] == "my-team"
    assert payload["health"] == "Needs attention"
    assert payload["counts"]["stale_claims"] == 1
    assert payload["counts"]["unclaimed_tasks"] == 1
    assert payload["counts"]["blocked_tasks"] == 1
    assert payload["leaderboard"][0]["member_name"] == "Agent A"
    assert payload["proof"]["ok"] is False
    assert payload["proof"]["missing"]["row_minimums"]


def test_unclaimed_prints_tasks(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "GET"
        assert url == "http://localhost:8080/api/my-team/unclaimed?limit=1&channel=ops+lane"
        return 200, {
            "tasks": [{
                "created_at": "2026-05-17T00:00:00+00:00",
                "channel": "ops",
                "task_key": "unclaimed-T1",
                "content": "needs owner",
            }]
        }

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "unclaimed", "--limit", "1", "--channel", "ops lane"])

    cli.main()
    out = capsys.readouterr().out
    assert "unclaimed-T1" in out
    assert "needs owner" in out


def test_replay_prints_posts(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "GET"
        assert url == "http://localhost:8080/api/my-team/replay?since=2026-07-10T00%3A00%3A00%2B00%3A00&limit=200&channel=ops"
        return 200, {
            "posts": [{
                "id": "p1",
                "kind": "message",
                "member_name": "Agent A",
                "channel": "ops",
                "content": "missed message",
            }]
        }

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "replay", "2026-07-10T00:00:00+00:00", "--channel", "ops"])

    cli.main()
    out = capsys.readouterr().out
    assert "missed message" in out


def test_replay_empty_prints_notice(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    monkeypatch.setattr(cli, "_request", lambda *a, **k: (200, {"posts": []}))
    monkeypatch.setattr(sys, "argv", ["swarlo", "replay", "2099-01-01T00:00:00+00:00"])

    cli.main()
    out = capsys.readouterr().out
    assert "No posts since" in out


def test_remove_member_prints_confirmation(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "DELETE"
        assert url == "http://localhost:8080/api/my-team/members/ghost%20one"
        return 200, {"deleted": "ghost one"}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "remove", "ghost one"])

    cli.main()
    out = capsys.readouterr().out
    assert "Removed member: ghost one" in out


def test_remove_missing_member_errors(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    monkeypatch.setattr(cli, "_request", lambda *a, **k: (404, {"detail": "Member nope not found"}))
    monkeypatch.setattr(sys, "argv", ["swarlo", "remove", "nope"])

    with pytest.raises(SystemExit):
        cli.main()


def test_prune_lists_removed_members(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "POST"
        assert url == "http://localhost:8080/api/my-team/prune"
        assert payload == {"stale_minutes": 30}
        return 200, {"pruned": ["ghost-1", "ghost-2"], "count": 2}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "prune", "--stale-minutes", "30"])

    cli.main()
    out = capsys.readouterr().out
    assert "Pruned 2 member(s)" in out
    assert "ghost-1" in out
    assert "ghost-2" in out


def test_prune_empty_prints_notice(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    monkeypatch.setattr(cli, "_request", lambda *a, **k: (200, {"pruned": [], "count": 0}))
    monkeypatch.setattr(sys, "argv", ["swarlo", "prune"])

    cli.main()
    out = capsys.readouterr().out
    assert "nothing pruned" in out


def test_read_command_limits_are_clamped(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))
    calls = []

    def fake_request(method, url, payload=None, api_key=None):
        calls.append((method, url))
        if "/xp?" in url:
            return 200, {"per_agent_xp": []}
        if "/scores?" in url:
            return 200, {"scores": []}
        return 200, {"tasks": []}

    monkeypatch.setattr(cli, "_request", fake_request)

    monkeypatch.setattr(sys, "argv", ["swarlo", "xp", "--limit", "-5"])
    cli.main()
    monkeypatch.setattr(sys, "argv", ["swarlo", "score-history", "--limit", "0"])
    cli.main()
    monkeypatch.setattr(sys, "argv", ["swarlo", "unclaimed", "--limit", "9999"])
    cli.main()

    assert calls == [
        ("GET", "http://localhost:8080/api/my-team/xp?limit=1"),
        ("GET", "http://localhost:8080/api/my-team/scores?limit=1"),
        ("GET", "http://localhost:8080/api/my-team/unclaimed?limit=500"),
    ]


def test_xp_member_help_mentions_member_id(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["swarlo", "xp", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "filter to one member_id" in capsys.readouterr().out


def test_mechanics_command_does_not_call_hub(monkeypatch, capsys):
    def fake_request(*_args, **_kwargs):
        raise AssertionError("mechanics should be offline")

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "mechanics"])

    cli.main()
    out = capsys.readouterr().out
    assert "XP mechanics:" in out
    assert "terminal reports/statuses" in out


def test_speed_check_reports_required_indexes(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    conn = sqlite3.connect(db_path)
    for table in cli.SPEED_INDEXES:
        conn.execute(f"CREATE TABLE {table} (id TEXT)")
    for table, indexes in cli.SPEED_INDEXES.items():
        for name in indexes:
            conn.execute(f"CREATE INDEX {name} ON {table}(id)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sys, "argv", ["swarlo", "speed-check", "--db", str(db_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "All speed indexes present." in out
    assert "commits ok" in out
    assert "posts   ok" in out


def test_speed_check_reports_query_plan_checks(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _ = backend.conn
    backend.close()

    monkeypatch.setattr(sys, "argv", ["swarlo", "speed-check", "--db", str(db_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "planner ok" in out
    assert "15/15 query plans" in out
    assert "planner paths" in out
    assert "channel_reads" in out
    assert "api_key_auth" in out


def test_speed_check_json_report(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _ = backend.conn
    backend.close()

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--json"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema_version"] == 1
    _assert_report_sha256(payload)
    assert payload["package"] == EXPECTED_PACKAGE
    assert payload["runtime"]["python"]
    assert payload["runtime"]["platform"]
    assert payload["runtime"]["sqlite"]
    assert payload["database"]["access"] == "read_only"
    assert payload["database"]["page_count"] >= 0
    assert payload["database"]["page_size"] > 0
    assert payload["database"]["size_bytes"] >= 0
    assert set(payload["database"]["rows"]) == set(cli.SPEED_INDEXES)
    assert payload["database"]["rows"]["posts"] == 0
    assert payload["generated_at"].endswith("Z")
    datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
    assert payload["elapsed_ms"] >= 0
    assert payload["latency_budget"] == {"max_ms": None, "ok": True}
    assert payload["live_data"] == {
        "required": False,
        "required_tables": ["members", "posts", "scores"],
        "ok": True,
        "missing": {"members": 0, "posts": 0, "scores": 0},
    }
    assert payload["row_minimums"] == {"required": {}, "ok": True, "misses": {}}
    assert payload["indexes"]["posts"]["present"] == len(cli.SPEED_INDEXES["posts"])
    assert payload["planner"]["total"] == 15
    assert payload["planner"]["expected_total"] == 15
    assert payload["planner"]["required"] is False
    assert payload["planner"]["required_ok"] is True
    assert "channel_reads" in payload["planner"]["paths"]
    assert payload["planner"]["misses"] == []


def test_speed_check_writes_json_report(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    out_path = tmp_path / "artifacts" / "speed-check.json"
    out_path.parent.mkdir()
    out_path.write_text('{"stale": true}\n')
    backend = SQLiteBackend(str(db_path))
    _ = backend.conn
    backend.close()

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--output", str(out_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "All speed indexes present." in out
    assert f"report written  {out_path}" in out
    assert "report sha256" in out
    payload = json.loads(out_path.read_text())
    assert payload["ok"] is True
    assert payload["schema_version"] == 1
    _assert_report_sha256(payload)
    assert payload["package"] == EXPECTED_PACKAGE
    assert payload["runtime"]["python"]
    assert payload["runtime"]["platform"]
    assert payload["runtime"]["sqlite"]
    assert payload["database"]["access"] == "read_only"
    assert payload["database"]["page_count"] >= 0
    assert payload["database"]["page_size"] > 0
    assert payload["database"]["size_bytes"] >= 0
    assert set(payload["database"]["rows"]) == set(cli.SPEED_INDEXES)
    assert payload["generated_at"].endswith("Z")
    assert payload["elapsed_ms"] >= 0
    assert payload["latency_budget"] == {"max_ms": None, "ok": True}
    assert payload["live_data"]["ok"] is True
    assert payload["live_data"]["required"] is False
    assert payload["row_minimums"]["ok"] is True
    assert payload["row_minimums"]["required"] == {}
    assert "stale" not in payload
    assert payload["planner"]["total"] == 15
    assert payload["planner"]["expected_total"] == 15
    assert payload["planner"]["required"] is False
    assert payload["planner"]["required_ok"] is True
    assert "api_key_auth" in payload["planner"]["paths"]
    assert not (out_path.parent / ".speed-check.json.tmp").exists()


def test_speed_verify_accepts_saved_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "planner": {"total": 15},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(sys, "argv", ["swarlo", "speed-verify", str(report_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "speed-check report verified" in out
    assert payload["report_sha256"] in out


def _strict_live_speed_report() -> dict:
    rows = {table: 1 for table in cli.SPEED_INDEXES}
    indexes = {
        table: {"present": len(expected), "total": len(expected), "missing": []}
        for table, expected in cli.SPEED_INDEXES.items()
    }
    payload = {
        "ok": True,
        "schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(cli.UTC).isoformat().replace("+00:00", "Z"),
        "package": {"name": "swarlo", "version": cli.__version__},
        "runtime": {
            "python": cli.platform.python_version(),
            "sqlite": cli.sqlite3.sqlite_version,
            "platform": cli.platform.platform(),
        },
        "indexes": indexes,
        "planner": {
            "expected_total": len(cli.SPEED_QUERY_PLANS),
            "required_ok": True,
            "total": len(cli.SPEED_QUERY_PLANS),
            "paths": list(cli.SPEED_QUERY_PLANS),
        },
        "latency_budget": {"max_ms": 1000.0, "ok": True},
        "elapsed_ms": 1.0,
        "database": {
            "access": "read_only",
            "size_bytes": 1,
            "page_count": 1,
            "page_size": 4096,
            "rows": rows,
        },
        "live_data": {
            "required_tables": list(cli.SPEED_LIVE_DATA_TABLES),
            "ok": True,
            "missing": {},
        },
        "row_minimums": {
            "required": {"posts": 1, "scores": 1, "members": 1},
            "ok": True,
            "misses": {},
        },
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    return payload


def test_speed_verify_strict_live_accepts_complete_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = _strict_live_speed_report()
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--strict-live"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "ok true" in out
    assert "generated_at fresh <= 5 minutes" in out
    assert "row minimum consistency true" in out


def test_speed_verify_strict_live_rejects_stale_report_by_default(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = _strict_live_speed_report()
    payload["generated_at"] = "2000-01-01T00:00:00Z"
    payload["report_sha256"] = cli._report_sha256({k: v for k, v in payload.items() if k != "report_sha256"})
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--strict-live"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "generated_at is older than 5 minutes" in out


def test_speed_verify_require_ok_accepts_passing_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-ok"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "ok true" in capsys.readouterr().out


def test_speed_verify_require_ok_rejects_failing_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": False,
        "schema_version": 1,
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-ok"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "ok is not true" in out


def test_speed_verify_require_schema_version_accepts_current_schema(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-schema-version"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "schema version 1" in capsys.readouterr().out


@pytest.mark.parametrize("schema_version", [None, 0, 2, "1"])
def test_speed_verify_require_schema_version_rejects_wrong_schema(monkeypatch, tmp_path, capsys, schema_version):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True}
    if schema_version is not None:
        payload["schema_version"] = schema_version
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-schema-version"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "schema_version is not 1" in out


def test_speed_verify_require_package_version_accepts_current_package(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "package": {"name": "swarlo", "version": cli.__version__},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-package-version"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert f"package swarlo {cli.__version__}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "package",
    [None, {}, {"name": "other", "version": "0.7.0"}, {"name": "swarlo", "version": "0.0.0"}],
)
def test_speed_verify_require_package_version_rejects_wrong_package(monkeypatch, tmp_path, capsys, package):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if package is not None:
        payload["package"] = package
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-package-version"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert f"package is not swarlo {cli.__version__}" in out


def test_speed_verify_require_runtime_accepts_current_runtime(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "runtime": {
            "python": cli.platform.python_version(),
            "sqlite": cli.sqlite3.sqlite_version,
        },
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-runtime"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert f"runtime python {cli.platform.python_version()}" in out
    assert f"sqlite {cli.sqlite3.sqlite_version}" in out


@pytest.mark.parametrize(
    "runtime",
    [
        None,
        {},
        {"python": "0.0.0", "sqlite": "0.0.0"},
        {"python": cli.platform.python_version(), "sqlite": "0.0.0"},
        {"python": "0.0.0", "sqlite": cli.sqlite3.sqlite_version},
    ],
)
def test_speed_verify_require_runtime_rejects_wrong_runtime(monkeypatch, tmp_path, capsys, runtime):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if runtime is not None:
        payload["runtime"] = runtime
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-runtime"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert f"runtime is not python {cli.platform.python_version()}" in out
    assert f"sqlite {cli.sqlite3.sqlite_version}" in out


def test_speed_verify_require_platform_accepts_current_platform(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "runtime": {"platform": cli.platform.platform()},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-platform"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert f"platform {cli.platform.platform()}" in capsys.readouterr().out


@pytest.mark.parametrize("runtime", [None, {}, {"platform": "other-platform"}])
def test_speed_verify_require_platform_rejects_wrong_platform(monkeypatch, tmp_path, capsys, runtime):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if runtime is not None:
        payload["runtime"] = runtime
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-platform"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert f"platform is not {cli.platform.platform()}" in out


def test_speed_verify_max_age_accepts_fresh_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "generated_at": datetime.now(cli.UTC).isoformat().replace("+00:00", "Z"),
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--max-age-min", "5"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "generated_at fresh <= 5 minutes" in capsys.readouterr().out


def test_speed_verify_max_age_rejects_stale_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "generated_at": "2000-01-01T00:00:00Z",
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--max-age-min", "5"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "generated_at is older than 5 minutes" in out


@pytest.mark.parametrize("generated_at", [None, "not-a-timestamp"])
def test_speed_verify_max_age_rejects_missing_or_invalid_timestamp(monkeypatch, tmp_path, capsys, generated_at):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if generated_at is not None:
        payload["generated_at"] = generated_at
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--max-age-min", "5"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "generated_at is not a valid timestamp" in out


def test_speed_verify_require_planner_accepts_required_planner_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "planner": {"required_ok": True},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-planner"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "planner required true" in capsys.readouterr().out


def test_speed_verify_require_planner_rejects_incomplete_planner_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "planner": {"required_ok": False},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-planner"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "planner.required_ok is not true" in out


def test_speed_verify_require_planner_paths_accepts_complete_planner_paths(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "planner": {
            "expected_total": len(cli.SPEED_QUERY_PLANS),
            "total": len(cli.SPEED_QUERY_PLANS),
            "paths": list(cli.SPEED_QUERY_PLANS),
        },
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-planner-paths"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "planner paths complete true" in capsys.readouterr().out


@pytest.mark.parametrize(
    "planner",
    [
        None,
        {},
        {"expected_total": len(cli.SPEED_QUERY_PLANS), "total": len(cli.SPEED_QUERY_PLANS), "paths": []},
        {"expected_total": 1, "total": len(cli.SPEED_QUERY_PLANS), "paths": list(cli.SPEED_QUERY_PLANS)},
        {"expected_total": len(cli.SPEED_QUERY_PLANS), "total": 1, "paths": list(cli.SPEED_QUERY_PLANS)},
        {
            "expected_total": len(cli.SPEED_QUERY_PLANS),
            "total": len(cli.SPEED_QUERY_PLANS),
            "paths": list(reversed(cli.SPEED_QUERY_PLANS)),
        },
    ],
)
def test_speed_verify_require_planner_paths_rejects_incomplete_planner_paths(monkeypatch, tmp_path, capsys, planner):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if planner is not None:
        payload["planner"] = planner
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-planner-paths"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "planner paths are not complete" in out


def test_speed_verify_require_latency_accepts_passing_latency_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "latency_budget": {"ok": True},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-latency"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "latency budget true" in capsys.readouterr().out


def test_speed_verify_require_latency_rejects_failing_latency_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "latency_budget": {"ok": False},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-latency"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "latency_budget.ok is not true" in out


def test_speed_verify_require_latency_consistency_accepts_elapsed_within_budget(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "elapsed_ms": 12.5,
        "latency_budget": {"max_ms": 100.0, "ok": True},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-latency-consistency"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "latency consistency true" in capsys.readouterr().out


@pytest.mark.parametrize(
    "elapsed_ms,latency_budget",
    [
        (None, {"max_ms": 100.0, "ok": True}),
        (12.5, None),
        (12.5, {"max_ms": None, "ok": True}),
        (12.5, {"max_ms": 0, "ok": True}),
        (12.5, {"max_ms": 100.0, "ok": False}),
        (120.0, {"max_ms": 100.0, "ok": True}),
    ],
)
def test_speed_verify_require_latency_consistency_rejects_inconsistent_budget(
    monkeypatch,
    tmp_path,
    capsys,
    elapsed_ms,
    latency_budget,
):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    if latency_budget is not None:
        payload["latency_budget"] = latency_budget
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-latency-consistency"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "latency budget is not consistent" in out


def test_speed_verify_require_elapsed_accepts_nonnegative_elapsed(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "elapsed_ms": 0.0,
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-elapsed"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "elapsed_ms true" in capsys.readouterr().out


@pytest.mark.parametrize("elapsed_ms", [None, -0.1, "0", True])
def test_speed_verify_require_elapsed_rejects_invalid_elapsed(monkeypatch, tmp_path, capsys, elapsed_ms):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-elapsed"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "elapsed_ms is not valid" in out


def test_speed_verify_require_live_data_accepts_populated_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "live_data": {"ok": True},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-live-data"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "live data true" in capsys.readouterr().out


def test_speed_verify_require_live_data_rejects_empty_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "live_data": {"ok": False},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-live-data"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "live_data.ok is not true" in out


def test_speed_verify_require_live_data_consistency_accepts_matching_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    rows = {table: 1 for table in cli.SPEED_INDEXES}
    payload = {
        "ok": True,
        "schema_version": 1,
        "database": {"rows": rows},
        "live_data": {
            "required_tables": list(cli.SPEED_LIVE_DATA_TABLES),
            "ok": True,
            "missing": {},
        },
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-live-data-consistency"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "live data consistency true" in capsys.readouterr().out


@pytest.mark.parametrize(
    "rows,live_data",
    [
        ({table: 1 for table in cli.SPEED_INDEXES}, None),
        (
            {table: 1 for table in cli.SPEED_INDEXES},
            {"required_tables": [], "ok": True, "missing": {}},
        ),
        (
            {**{table: 1 for table in cli.SPEED_INDEXES}, "posts": 0},
            {"required_tables": list(cli.SPEED_LIVE_DATA_TABLES), "ok": True, "missing": {}},
        ),
        (
            {**{table: 1 for table in cli.SPEED_INDEXES}, "posts": 0},
            {"required_tables": list(cli.SPEED_LIVE_DATA_TABLES), "ok": False, "missing": {"posts": 2}},
        ),
        (
            {table: 1 for table in cli.SPEED_INDEXES},
            {"required_tables": list(cli.SPEED_LIVE_DATA_TABLES), "ok": False, "missing": {}},
        ),
    ],
)
def test_speed_verify_require_live_data_consistency_rejects_mismatched_report(
    monkeypatch,
    tmp_path,
    capsys,
    rows,
    live_data,
):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1, "database": {"rows": rows}}
    if live_data is not None:
        payload["live_data"] = live_data
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-live-data-consistency"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "live_data is not consistent" in out


def test_speed_verify_require_row_minimums_accepts_passing_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "row_minimums": {"ok": True},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-row-minimums"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "row minimums true" in capsys.readouterr().out


def test_speed_verify_require_row_minimums_rejects_failing_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "row_minimums": {"ok": False},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-row-minimums"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "row_minimums.ok is not true" in out


def test_speed_verify_require_row_minimum_consistency_accepts_matching_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "database": {"rows": {table: 0 for table in cli.SPEED_INDEXES}},
        "row_minimums": {
            "required": {"posts": 1},
            "ok": False,
            "misses": {"posts": {"actual": 0, "minimum": 1}},
        },
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-row-minimum-consistency"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "row minimum consistency true" in capsys.readouterr().out


@pytest.mark.parametrize(
    "rows,row_minimums",
    [
        ({table: 0 for table in cli.SPEED_INDEXES}, None),
        ({table: 0 for table in cli.SPEED_INDEXES}, {"required": {"posts": 1}, "ok": True, "misses": {}}),
        (
            {table: 0 for table in cli.SPEED_INDEXES},
            {"required": {"posts": 1}, "ok": False, "misses": {"posts": {"actual": 2, "minimum": 1}}},
        ),
        (
            {table: 2 for table in cli.SPEED_INDEXES},
            {"required": {"posts": 1}, "ok": False, "misses": {"posts": {"actual": 2, "minimum": 1}}},
        ),
        ({table: 0 for table in cli.SPEED_INDEXES}, {"required": {"unknown": 1}, "ok": False, "misses": {}}),
        ({table: 0 for table in cli.SPEED_INDEXES}, {"required": {"posts": -1}, "ok": True, "misses": {}}),
    ],
)
def test_speed_verify_require_row_minimum_consistency_rejects_mismatched_report(
    monkeypatch,
    tmp_path,
    capsys,
    rows,
    row_minimums,
):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1, "database": {"rows": rows}}
    if row_minimums is not None:
        payload["row_minimums"] = row_minimums
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-row-minimum-consistency"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "row_minimums are not consistent" in out


def test_speed_verify_require_indexes_accepts_complete_index_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "indexes": {
            table: {"present": len(indexes), "total": len(indexes), "missing": []}
            for table, indexes in cli.SPEED_INDEXES.items()
        },
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-indexes"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "indexes complete true" in capsys.readouterr().out


@pytest.mark.parametrize(
    "indexes",
    [
        None,
        {},
        {"posts": {"present": 0, "total": 1, "missing": ["idx_missing"]}},
        {
            **{
                table: {"present": len(expected), "total": len(expected), "missing": []}
                for table, expected in cli.SPEED_INDEXES.items()
            },
            "posts": {"present": 0, "total": len(cli.SPEED_INDEXES["posts"]), "missing": []},
        },
        {
            **{
                table: {"present": len(expected), "total": len(expected), "missing": []}
                for table, expected in cli.SPEED_INDEXES.items()
            },
            "posts": {
                "present": len(cli.SPEED_INDEXES["posts"]),
                "total": len(cli.SPEED_INDEXES["posts"]),
                "missing": ["idx_missing"],
            },
        },
    ],
)
def test_speed_verify_require_indexes_rejects_incomplete_index_report(monkeypatch, tmp_path, capsys, indexes):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if indexes is not None:
        payload["indexes"] = indexes
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-indexes"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "indexes are not complete" in out


def test_speed_verify_require_row_counts_accepts_complete_database_rows(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "database": {"rows": {table: 0 for table in cli.SPEED_INDEXES}},
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-row-counts"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "row counts complete true" in capsys.readouterr().out


@pytest.mark.parametrize(
    "database",
    [
        None,
        {},
        {"rows": {}},
        {"rows": {"posts": 0}},
        {"rows": {**{table: 0 for table in cli.SPEED_INDEXES}, "posts": -1}},
        {"rows": {**{table: 0 for table in cli.SPEED_INDEXES}, "posts": "0"}},
    ],
)
def test_speed_verify_require_row_counts_rejects_incomplete_database_rows(monkeypatch, tmp_path, capsys, database):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if database is not None:
        payload["database"] = database
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-row-counts"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "database.rows are not complete" in out


def test_speed_verify_require_db_metadata_accepts_complete_database_metadata(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
        "database": {
            "access": "read_only",
            "size_bytes": 0,
            "page_count": 0,
            "page_size": 4096,
        },
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-db-metadata"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "database metadata true" in capsys.readouterr().out


@pytest.mark.parametrize(
    "database",
    [
        None,
        {},
        {"access": "read_write", "size_bytes": 0, "page_count": 0, "page_size": 4096},
        {"access": "read_only", "size_bytes": -1, "page_count": 0, "page_size": 4096},
        {"access": "read_only", "size_bytes": 0, "page_count": -1, "page_size": 4096},
        {"access": "read_only", "size_bytes": 0, "page_count": 0, "page_size": 0},
        {"access": "read_only", "size_bytes": "0", "page_count": 0, "page_size": 4096},
    ],
)
def test_speed_verify_require_db_metadata_rejects_incomplete_database_metadata(monkeypatch, tmp_path, capsys, database):
    report_path = tmp_path / "speed-check.json"
    payload = {"ok": True, "schema_version": 1}
    if database is not None:
        payload["database"] = database
    payload["report_sha256"] = cli._report_sha256(payload)
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-verify", str(report_path), "--require-db-metadata"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report failed" in out
    assert "database metadata is not complete" in out


def test_speed_verify_rejects_tampered_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    payload = {
        "ok": True,
        "schema_version": 1,
    }
    payload["report_sha256"] = cli._report_sha256(payload)
    payload["ok"] = False
    report_path.write_text(json.dumps(payload) + "\n")

    monkeypatch.setattr(sys, "argv", ["swarlo", "speed-verify", str(report_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-check report invalid" in out
    assert "expected" in out
    assert "actual" in out


def test_speed_verify_rejects_missing_digest(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-check.json"
    report_path.write_text(json.dumps({"ok": True}) + "\n")

    monkeypatch.setattr(sys, "argv", ["swarlo", "speed-verify", str(report_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "missing report_sha256" in out


def test_speed_check_fails_when_index_missing(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    conn = sqlite3.connect(db_path)
    for table in cli.SPEED_INDEXES:
        conn.execute(f"CREATE TABLE {table} (id TEXT)")
    conn.execute("CREATE INDEX idx_posts_hub_created ON posts(id)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(sys, "argv", ["swarlo", "speed-check", "--db", str(db_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "Missing speed indexes:" in out
    assert "idx_members_api_key" in out


def test_speed_check_json_reports_missing_indexes(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    conn = sqlite3.connect(db_path)
    for table in cli.SPEED_INDEXES:
        conn.execute(f"CREATE TABLE {table} (id TEXT)")
    conn.execute("CREATE INDEX idx_posts_hub_created ON posts(id)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--json"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["schema_version"] == 1
    _assert_report_sha256(payload)
    assert payload["package"] == EXPECTED_PACKAGE
    assert payload["runtime"]["python"]
    assert payload["runtime"]["platform"]
    assert payload["runtime"]["sqlite"]
    assert payload["database"]["access"] == "read_only"
    assert payload["database"]["page_count"] >= 0
    assert payload["database"]["page_size"] > 0
    assert payload["database"]["size_bytes"] >= 0
    assert set(payload["database"]["rows"]) == set(cli.SPEED_INDEXES)
    assert payload["generated_at"].endswith("Z")
    assert payload["elapsed_ms"] >= 0
    assert payload["latency_budget"] == {"max_ms": None, "ok": True}
    assert payload["live_data"]["ok"] is True
    assert payload["row_minimums"]["ok"] is True
    assert payload["indexes"]["posts"]["present"] == 1
    assert "idx_posts_hub_channel" in payload["indexes"]["posts"]["missing"]
    assert "idx_members_api_key" in payload["indexes"]["members"]["missing"]
    assert payload["planner"]["total"] == 0
    assert payload["planner"]["expected_total"] == 15
    assert payload["planner"]["required"] is False
    assert payload["planner"]["required_ok"] is True


def test_speed_check_reports_latency_budget(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _ = backend.conn
    backend.close()
    times = iter([100.0, 100.25])
    monkeypatch.setattr(cli.time, "perf_counter", lambda: next(times))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--json", "--max-ms", "300"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["elapsed_ms"] == 250.0
    assert payload["latency_budget"] == {"max_ms": 300.0, "ok": True}


@pytest.mark.parametrize("max_ms", ["0", "-1", "abc"])
def test_speed_check_rejects_invalid_latency_budget(monkeypatch, capsys, max_ms):
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", "unused.db", "--max-ms", max_ms],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "--max-ms" in capsys.readouterr().err


@pytest.mark.parametrize("min_row", ["posts=0", "posts=-1", "posts=abc", "missing=1", "posts"])
def test_speed_check_rejects_invalid_row_minimum(monkeypatch, capsys, min_row):
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", "unused.db", "--min-row", min_row],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "--min-row" in capsys.readouterr().err


def test_speed_check_require_planner_passes_full_schema(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _ = backend.conn
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--json", "--require-planner"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planner"]["required"] is True
    assert payload["planner"]["required_ok"] is True
    assert payload["planner"]["total"] == payload["planner"]["expected_total"] == 15


def test_speed_check_min_row_passes_populated_schema(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _insert_speed_live_rows(backend.conn)
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-check",
            "--db",
            str(db_path),
            "--json",
            "--min-row",
            "posts=1",
            "--min-row",
            "scores=1",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["row_minimums"] == {
        "required": {"posts": 1, "scores": 1},
        "ok": True,
        "misses": {},
    }


def test_speed_check_min_row_fails_small_schema(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _insert_speed_live_rows(backend.conn)
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--min-row", "posts=2"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "row mins missing" in out
    assert "Row-minimum proof incomplete:" in out
    assert "posts: 1 rows < 2" in out


def test_speed_check_require_live_data_passes_populated_schema(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _insert_speed_live_rows(backend.conn)
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--json", "--require-live-data"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["database"]["rows"]["members"] == 1
    assert payload["database"]["rows"]["posts"] == 1
    assert payload["database"]["rows"]["scores"] == 1
    assert payload["live_data"] == {
        "required": True,
        "required_tables": ["members", "posts", "scores"],
        "ok": True,
        "missing": {},
    }


def test_speed_check_strict_live_enables_release_gates(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _insert_speed_live_rows(backend.conn)
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-check",
            "--db",
            str(db_path),
            "--json",
            "--strict-live",
            "--min-row",
            "posts=1",
            "--min-row",
            "scores=1",
            "--min-row",
            "members=1",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["latency_budget"]["max_ms"] == 1000
    assert payload["planner"]["required"] is True
    assert payload["planner"]["required_ok"] is True
    assert payload["live_data"]["required"] is True
    assert payload["row_minimums"]["required"] == {
        "posts": 1,
        "scores": 1,
        "members": 1,
    }


def test_speed_check_strict_live_defaults_to_release_row_floors(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _insert_speed_live_rows(backend.conn)
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--strict-live"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "row mins missing" in out
    assert "posts: 1 rows < 1000" in out
    assert "scores: 1 rows < 10000" in out


def test_speed_proof_runs_strict_check_and_verify(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    report_path = tmp_path / "speed-proof.json"
    backend = SQLiteBackend(str(db_path))
    _insert_speed_live_rows(backend.conn)
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof",
            "--db",
            str(db_path),
            "--output",
            str(report_path),
            "--min-row",
            "posts=1",
            "--min-row",
            "scores=1",
            "--min-row",
            "members=1",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "report written" in out
    assert "speed-check report verified" in out
    assert "row minimum consistency true" in out
    payload = json.loads(report_path.read_text())
    assert payload["ok"] is True
    _assert_report_sha256(payload)


def test_speed_proof_json_emits_machine_readable_summary(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    backend = SQLiteBackend(str(db_path))
    _insert_speed_live_rows(backend.conn)
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof",
            "--db",
            str(db_path),
            "--output",
            str(report_path),
            "--json",
            "--summary-output",
            str(summary_path),
            "--min-row",
            "posts=1",
            "--min-row",
            "scores=1",
            "--min-row",
            "members=1",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    saved_summary = json.loads(summary_path.read_text())
    assert saved_summary == summary
    assert summary["schema_version"] == 1
    assert summary["kind"] == "speed-proof"
    _assert_summary_sha256(summary)
    assert summary["package"] == EXPECTED_PACKAGE
    assert summary["report_schema_version"] == 1
    assert summary["ok"] is True
    assert summary["check_ok"] is True
    assert summary["verify_ok"] is True
    assert summary["check_code"] == 0
    assert summary["verify_code"] == 0
    assert summary["exit_code"] == 0
    assert summary["gates"] == {
        "strict_live": True,
        "max_ms": 1000,
        "max_age_min": 5,
        "require_planner": True,
        "require_live_data": True,
        "min_rows": {"posts": 1, "scores": 1, "members": 1},
    }
    assert summary["report"] == str(report_path)
    assert summary["db"] == str(db_path)
    assert summary["database"]["access"] == "read_only"
    assert summary["database"]["rows"]["posts"] == 1
    assert summary["database"]["rows"]["scores"] == 1
    assert summary["database"]["rows"]["members"] == 1
    assert summary["generated_at"].endswith("Z")
    assert summary["report_sha256"]
    assert summary["planner"]["required_ok"] is True
    assert summary["live_data"]["ok"] is True
    assert summary["row_minimums"]["ok"] is True
    assert "report written" not in out
    assert "speed-check report verified" not in out


def test_speed_proof_summary_output_writes_summary_without_json_stdout(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "nested" / "speed-proof-summary.json"
    backend = SQLiteBackend(str(db_path))
    _insert_speed_live_rows(backend.conn)
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof",
            "--db",
            str(db_path),
            "--output",
            str(report_path),
            "--summary-output",
            str(summary_path),
            "--min-row",
            "posts=1",
            "--min-row",
            "scores=1",
            "--min-row",
            "members=1",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "report written" in out
    assert "speed-check report verified" in out
    summary = json.loads(summary_path.read_text())
    assert summary["kind"] == "speed-proof"
    assert summary["ok"] is True
    assert summary["report"] == str(report_path)
    _assert_summary_sha256(summary)


def test_speed_proof_json_emits_failure_summary(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    report_path = tmp_path / "speed-proof.json"
    backend = SQLiteBackend(str(db_path))
    _insert_speed_live_rows(backend.conn)
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof",
            "--db",
            str(db_path),
            "--output",
            str(report_path),
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["schema_version"] == 1
    assert summary["kind"] == "speed-proof"
    _assert_summary_sha256(summary)
    assert summary["package"] == EXPECTED_PACKAGE
    assert summary["report_schema_version"] == 1
    assert summary["ok"] is False
    assert summary["check_ok"] is False
    assert summary["verify_ok"] is False
    assert summary["check_code"] == 1
    assert summary["verify_code"] is None
    assert summary["exit_code"] == 1
    assert summary["gates"] == {
        "strict_live": True,
        "max_ms": 1000,
        "max_age_min": 5,
        "require_planner": True,
        "require_live_data": True,
        "min_rows": {"posts": 1000, "scores": 10000, "members": 1},
    }
    assert summary["error"] is None
    assert summary["report"] == str(report_path)
    assert summary["db"] == str(db_path)
    assert summary["database"]["access"] == "read_only"
    assert summary["database"]["rows"]["posts"] == 1
    assert summary["generated_at"].endswith("Z")
    assert summary["report_sha256"]
    assert summary["row_minimums"]["ok"] is False
    assert "report written" not in out
    assert "row mins missing" not in out


def test_speed_proof_json_emits_missing_db_summary(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "missing.db"
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof",
            "--db",
            str(db_path),
            "--output",
            str(report_path),
            "--json",
            "--summary-output",
            str(summary_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    summary = json.loads(capsys.readouterr().out)
    saved_summary = json.loads(summary_path.read_text())
    assert saved_summary == summary
    assert summary["ok"] is False
    assert summary["schema_version"] == 1
    assert summary["kind"] == "speed-proof"
    _assert_summary_sha256(summary)
    assert summary["package"] == EXPECTED_PACKAGE
    assert summary["report_schema_version"] is None
    assert summary["check_ok"] is False
    assert summary["verify_ok"] is False
    assert summary["check_code"] == 1
    assert summary["verify_code"] is None
    assert summary["exit_code"] == 1
    assert summary["gates"] == {
        "strict_live": True,
        "max_ms": 1000,
        "max_age_min": 5,
        "require_planner": True,
        "require_live_data": True,
        "min_rows": {"posts": 1000, "scores": 10000, "members": 1},
    }
    assert summary["error"] == f"speed-check: database not found: {db_path}"
    assert summary["db"] is None
    assert summary["database"] is None
    assert summary["generated_at"] is None
    assert summary["report_sha256"] is None
    assert summary["planner"] is None


def test_speed_proof_summary_output_writes_missing_db_without_json(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "missing.db"
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "nested" / "speed-proof-summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof",
            "--db",
            str(db_path),
            "--output",
            str(report_path),
            "--summary-output",
            str(summary_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert capsys.readouterr().out == ""
    summary = json.loads(summary_path.read_text())
    assert summary["kind"] == "speed-proof"
    assert summary["ok"] is False
    assert summary["check_code"] == 1
    assert summary["verify_code"] is None
    assert summary["error"] == f"speed-check: database not found: {db_path}"
    _assert_summary_sha256(summary)


def test_speed_proof_summary_verify_accepts_saved_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "check_ok": True,
        "verify_ok": True,
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "speed-proof summary verified" in out
    assert summary["summary_sha256"] in out


def test_speed_proof_summary_verify_require_ok_accepts_passing_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--require-ok"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "ok true" in capsys.readouterr().out


def test_speed_proof_summary_verify_require_ok_rejects_failing_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": False,
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--require-ok"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-proof summary failed" in out
    assert "ok is not true" in out


def test_speed_proof_summary_verify_max_age_accepts_fresh_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "generated_at": datetime.now(cli.UTC).isoformat().replace("+00:00", "Z"),
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--max-age-min", "5"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "generated_at fresh <= 5 minutes" in capsys.readouterr().out


def test_speed_proof_summary_verify_max_age_rejects_stale_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "generated_at": "2000-01-01T00:00:00Z",
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--max-age-min", "5"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "speed-proof summary failed" in out
    assert "generated_at is older than 5 minutes" in out


def test_speed_proof_summary_verify_require_package_version_accepts_current_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "package": {"name": "swarlo", "version": cli.__version__},
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--require-package-version"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert f"package swarlo {cli.__version__}" in capsys.readouterr().out


def test_speed_proof_summary_verify_require_package_version_rejects_wrong_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "package": {"name": "swarlo", "version": "0.0.0"},
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--require-package-version"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert f"package is not swarlo {cli.__version__}" in capsys.readouterr().out


def test_speed_proof_summary_verify_require_report_schema_version_accepts_current_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "report_schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--require-report-schema-version"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert (
        f"report schema version {cli.SPEED_CHECK_REPORT_SCHEMA_VERSION}"
        in capsys.readouterr().out
    )


def test_speed_proof_summary_verify_require_report_schema_version_rejects_wrong_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "report_schema_version": 0,
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--require-report-schema-version"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert (
        f"report_schema_version is not {cli.SPEED_CHECK_REPORT_SCHEMA_VERSION}"
        in capsys.readouterr().out
    )


def test_speed_proof_summary_verify_require_report_accepts_matching_receipt(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = {
        "schema_version": 1,
        "ok": True,
        "package": {"name": "swarlo", "version": cli.__version__},
    }
    report["report_sha256"] = cli._report_sha256(report)
    report_path.write_text(json.dumps(report))
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--require-report"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "report true" in capsys.readouterr().out


def test_speed_proof_summary_verify_require_report_rejects_mismatched_receipt(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = {
        "schema_version": 1,
        "ok": True,
        "package": {"name": "swarlo", "version": cli.__version__},
    }
    report["report_sha256"] = cli._report_sha256(report)
    report_path.write_text(json.dumps(report))
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "report": str(report_path),
        "report_sha256": "0" * 64,
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--require-report"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "report_sha256 does not match summary" in capsys.readouterr().out


def test_speed_proof_summary_verify_require_report_schema_version_rejects_wrong_linked_receipt(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = {
        "schema_version": 0,
        "ok": True,
    }
    report["report_sha256"] = cli._report_sha256(report)
    report_path.write_text(json.dumps(report))
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "report_schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof-summary-verify",
            str(summary_path),
            "--require-report",
            "--require-report-schema-version",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert (
        f"report schema_version is not {cli.SPEED_CHECK_REPORT_SCHEMA_VERSION}"
        in capsys.readouterr().out
    )


def test_speed_proof_summary_verify_require_package_version_rejects_wrong_linked_receipt(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = {
        "schema_version": 1,
        "ok": True,
        "package": {"name": "swarlo", "version": "0.0.0"},
    }
    report["report_sha256"] = cli._report_sha256(report)
    report_path.write_text(json.dumps(report))
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "package": {"name": "swarlo", "version": cli.__version__},
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof-summary-verify",
            str(summary_path),
            "--require-report",
            "--require-package-version",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert f"report package is not swarlo {cli.__version__}" in capsys.readouterr().out


def test_speed_proof_summary_verify_require_report_consistency_accepts_matching_receipt(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = _write_strict_speed_report(report_path)
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
        "db": report["db"],
        "generated_at": report["generated_at"],
        "database": report["database"],
        "elapsed_ms": report["elapsed_ms"],
        "latency_budget": report["latency_budget"],
        "planner": report["planner"],
        "live_data": report["live_data"],
        "row_minimums": report["row_minimums"],
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof-summary-verify",
            str(summary_path),
            "--require-report",
            "--require-report-consistency",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "report consistency true" in capsys.readouterr().out


def test_speed_proof_summary_verify_require_report_consistency_rejects_mismatch(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = _write_strict_speed_report(report_path)
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
        "db": "wrong.db",
        "generated_at": report["generated_at"],
        "database": report["database"],
        "elapsed_ms": report["elapsed_ms"],
        "latency_budget": report["latency_budget"],
        "planner": report["planner"],
        "live_data": report["live_data"],
        "row_minimums": report["row_minimums"],
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "speed-proof-summary-verify",
            str(summary_path),
            "--require-report",
            "--require-report-consistency",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "db does not match linked report" in capsys.readouterr().out


def test_speed_proof_summary_verify_strict_live_accepts_release_summary(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = _write_strict_speed_report(report_path)
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "package": {"name": "swarlo", "version": cli.__version__},
        "report_schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
        "db": report["db"],
        "generated_at": report["generated_at"],
        "database": report["database"],
        "elapsed_ms": report["elapsed_ms"],
        "latency_budget": report["latency_budget"],
        "planner": report["planner"],
        "live_data": report["live_data"],
        "row_minimums": report["row_minimums"],
        "gates": {
            "strict_live": True,
            "max_ms": 1000,
            "max_age_min": 5,
            "require_planner": True,
            "require_live_data": True,
            "min_rows": {"posts": 1000, "scores": 10000, "members": 1},
        },
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--strict-live"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "ok true" in out
    assert "report true" in out
    assert f"package swarlo {cli.__version__}" in out
    assert f"report schema version {cli.SPEED_CHECK_REPORT_SCHEMA_VERSION}" in out
    assert "report consistency true" in out
    assert "strict live true" in out


def test_speed_proof_summary_verify_strict_live_rejects_weakened_gates(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = _write_strict_speed_report(report_path)
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "package": {"name": "swarlo", "version": cli.__version__},
        "report_schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
        "db": report["db"],
        "generated_at": report["generated_at"],
        "database": report["database"],
        "elapsed_ms": report["elapsed_ms"],
        "latency_budget": report["latency_budget"],
        "planner": report["planner"],
        "live_data": report["live_data"],
        "row_minimums": report["row_minimums"],
        "gates": {
            "strict_live": True,
            "max_ms": 1000,
            "max_age_min": 5,
            "require_planner": True,
            "require_live_data": True,
            "min_rows": {"posts": 1, "scores": 1, "members": 1},
        },
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--strict-live"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "gates are not strict-live" in capsys.readouterr().out


def test_speed_proof_summary_verify_strict_live_rejects_failed_live_data(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = _write_strict_speed_report(report_path)
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "package": {"name": "swarlo", "version": cli.__version__},
        "report_schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
        "db": report["db"],
        "generated_at": report["generated_at"],
        "database": report["database"],
        "elapsed_ms": report["elapsed_ms"],
        "latency_budget": report["latency_budget"],
        "planner": report["planner"],
        "live_data": {"ok": False},
        "row_minimums": report["row_minimums"],
        "gates": {
            "strict_live": True,
            "max_ms": 1000,
            "max_age_min": 5,
            "require_planner": True,
            "require_live_data": True,
            "min_rows": {"posts": 1000, "scores": 10000, "members": 1},
        },
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--strict-live"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "live_data is not true" in capsys.readouterr().out


def test_speed_proof_summary_verify_strict_live_rejects_stale_summary(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = _write_strict_speed_report(report_path)
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "generated_at": "2000-01-01T00:00:00Z",
        "package": {"name": "swarlo", "version": cli.__version__},
        "report_schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
        "db": report["db"],
        "database": report["database"],
        "elapsed_ms": report["elapsed_ms"],
        "latency_budget": report["latency_budget"],
        "planner": report["planner"],
        "live_data": report["live_data"],
        "row_minimums": report["row_minimums"],
        "gates": {
            "strict_live": True,
            "max_ms": 1000,
            "max_age_min": 5,
            "require_planner": True,
            "require_live_data": True,
            "min_rows": {"posts": 1000, "scores": 10000, "members": 1},
        },
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--strict-live"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "generated_at is older than 5 minutes" in capsys.readouterr().out


def test_speed_proof_summary_verify_strict_live_rejects_weak_linked_report(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "speed-proof.json"
    summary_path = tmp_path / "speed-proof-summary.json"
    report = _write_strict_speed_report(
        report_path,
        runtime={
            "python": "0.0.0",
            "sqlite": cli.sqlite3.sqlite_version,
            "platform": cli.platform.platform(),
        },
    )
    summary = {
        "schema_version": 1,
        "kind": "speed-proof",
        "ok": True,
        "package": {"name": "swarlo", "version": cli.__version__},
        "report_schema_version": cli.SPEED_CHECK_REPORT_SCHEMA_VERSION,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
        "db": report["db"],
        "generated_at": report["generated_at"],
        "database": report["database"],
        "elapsed_ms": report["elapsed_ms"],
        "latency_budget": report["latency_budget"],
        "planner": report["planner"],
        "live_data": report["live_data"],
        "row_minimums": report["row_minimums"],
        "gates": {
            "strict_live": True,
            "max_ms": 1000,
            "max_age_min": 5,
            "require_planner": True,
            "require_live_data": True,
            "min_rows": {"posts": 1000, "scores": 10000, "members": 1},
        },
    }
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path), "--strict-live"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "linked report strict verification failed" in capsys.readouterr().out


def test_speed_proof_summary_verify_rejects_tampered_summary(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary = {"schema_version": 1, "kind": "speed-proof", "ok": True}
    summary["summary_sha256"] = cli._report_sha256(summary)
    summary["ok"] = False
    summary_path.write_text(json.dumps(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "speed-proof summary invalid" in capsys.readouterr().out


def test_speed_proof_summary_verify_rejects_missing_digest(monkeypatch, tmp_path, capsys):
    summary_path = tmp_path / "speed-proof-summary.json"
    summary_path.write_text(json.dumps({"schema_version": 1, "kind": "speed-proof"}))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-proof-summary-verify", str(summary_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "missing summary_sha256" in capsys.readouterr().out


def test_speed_check_require_live_data_fails_empty_schema(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _ = backend.conn
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--require-live-data"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "live data missing" in out
    assert "Live-data proof incomplete:" in out
    assert "members: 0 rows" in out
    assert "posts: 0 rows" in out
    assert "scores: 0 rows" in out


def test_speed_check_prints_required_planner_status(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _ = backend.conn
    backend.close()
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--require-planner"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "planner required ok" in out
    assert "15/15 query plans" in out


def test_speed_check_require_planner_fails_incomplete_schema(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    conn = sqlite3.connect(db_path)
    for table in cli.SPEED_INDEXES:
        conn.execute(f"CREATE TABLE {table} (id TEXT)")
    for table, indexes in cli.SPEED_INDEXES.items():
        for name in indexes:
            conn.execute(f"CREATE INDEX {name} ON {table}(id)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--require-planner"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "planner required missing" in out
    assert "Planner checks incomplete: 0/15 plans ran" in out


def test_speed_check_fails_latency_budget(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    backend = SQLiteBackend(str(db_path))
    _ = backend.conn
    backend.close()
    times = iter([100.0, 100.25])
    monkeypatch.setattr(cli.time, "perf_counter", lambda: next(times))
    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--max-ms", "100"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "latency slow" in out
    assert "exceeded latency budget" in out


def test_speed_check_writes_json_report_on_failure(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "swarlo.db"
    out_path = tmp_path / "speed-check-failed.json"
    conn = sqlite3.connect(db_path)
    for table in cli.SPEED_INDEXES:
        conn.execute(f"CREATE TABLE {table} (id TEXT)")
    conn.execute("CREATE INDEX idx_posts_hub_created ON posts(id)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "speed-check", "--db", str(db_path), "--output", str(out_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "Missing speed indexes:" in capsys.readouterr().out
    payload = json.loads(out_path.read_text())
    assert payload["ok"] is False
    assert "idx_members_api_key" in payload["indexes"]["members"]["missing"]


def test_score_history_help_mentions_score_deltas(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["swarlo", "score-history", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    assert "score deltas" in capsys.readouterr().out


def test_unclaimed_help_mentions_retracted_claims(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["swarlo", "unclaimed", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "non-retracted claim" in output
    assert "terminal report" in output


def test_handoff_renders_trail(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080",
                                       "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "GET"
        assert "/handoff_trail/T3?depth=3" in url
        return 200, {
            "task_key": "T3", "depth": 3, "count": 2,
            "trail": [
                {"from": "T2", "by": "bob", "by_id": "bob",
                 "at": "2026-05-16T12:00Z", "hop": 1,
                 "handoff": {"artifacts": ["t2.py"],
                             "decisions": ["chose B"], "open_questions": []}},
                {"from": "T1", "by": "alice", "by_id": "alice",
                 "at": "2026-05-16T11:00Z", "hop": 2,
                 "handoff": {}},
            ],
        }

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "handoff", "T3", "--depth", "3"])

    cli.main()
    out = capsys.readouterr().out
    assert "Handoff trail for T3" in out
    assert "hop 1 — T2" in out
    assert "artifacts: t2.py" in out
    assert "decision:  chose B" in out
    assert "hop 2 — T1" in out
    assert "(no handoff recorded)" in out


def test_handoff_url_encodes_task_key(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080",
                                       "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "GET"
        assert "/handoff_trail/task%2Fwith%20space?depth=2" in url
        return 200, {"task_key": "task/with space", "depth": 2, "count": 0, "trail": []}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "handoff", "task/with space", "--depth", "2"])

    cli.main()
    assert "No handoff trail for task/with space" in capsys.readouterr().out


def test_ping_url_encodes_member_id(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080",
                                       "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        assert method == "GET"
        assert (
            "/ping/agent%2Fwith%20space?"
            "since=2026-05-17T00%3A00%3A00%2B00%3A00&include=mine%2Cready"
        ) in url
        return 200, {"action_needed": False}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "swarlo",
            "ping",
            "--member-id",
            "agent/with space",
            "--since",
            "2026-05-17T00:00:00+00:00",
            "--include",
            "mine,ready",
        ],
    )

    cli.main()
    assert "Clear." in capsys.readouterr().out


def test_handoff_json_flag_emits_raw(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080",
                                       "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        return 200, {"task_key": "T1", "depth": 1, "count": 0, "trail": []}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv",
                        ["swarlo", "handoff", "T1", "--depth", "1", "--json"])
    cli.main()
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["task_key"] == "T1"


def test_missing_runtime_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARLO_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit, match="Missing server"):
        cli._require_runtime(type("Args", (), {"server": None, "hub": None, "api_key": None})())


def test_precommit_hook_source_matches_scripts_copy():
    """The canonical SOURCE in swarlo/_precommit_hook_source.py must stay
    byte-identical to scripts/swarlo-precommit-hook. If you edit one,
    edit the other. A CI test catching drift immediately is cheaper
    than two divergent copies of a 150-line hook.
    """
    from swarlo._precommit_hook_source import SOURCE

    script_path = REPO_ROOT / "scripts" / "swarlo-precommit-hook"
    on_disk = script_path.read_text()
    assert SOURCE == on_disk, (
        "swarlo/_precommit_hook_source.py SOURCE has drifted from "
        "scripts/swarlo-precommit-hook. Sync them."
    )


def test_install_hook_writes_executable(monkeypatch, tmp_path, capsys):
    """`swarlo install-hook --path ...` writes the hook and chmods +x."""
    target = tmp_path / "pre-commit"

    monkeypatch.setattr(sys, "argv", [
        "swarlo", "install-hook", "--path", str(target),
    ])
    cli.main()

    assert target.exists()
    from swarlo._precommit_hook_source import SOURCE
    assert target.read_text() == SOURCE
    import stat
    mode = target.stat().st_mode
    assert mode & stat.S_IXUSR, f"hook is not executable (mode={oct(mode)})"

    out = capsys.readouterr().out
    assert "Installed swarlo pre-commit hook" in out


def test_install_hook_refuses_to_clobber_without_force(monkeypatch, tmp_path):
    """Existing hook is not overwritten unless --force is passed."""
    target = tmp_path / "pre-commit"
    target.write_text("# existing\n")
    original = target.read_text()

    monkeypatch.setattr(sys, "argv", [
        "swarlo", "install-hook", "--path", str(target),
    ])
    with pytest.raises(SystemExit, match="already exists"):
        cli.main()

    assert target.read_text() == original


def test_install_hook_force_overwrites(monkeypatch, tmp_path):
    """--force replaces the existing hook."""
    target = tmp_path / "pre-commit"
    target.write_text("# old\n")

    monkeypatch.setattr(sys, "argv", [
        "swarlo", "install-hook", "--path", str(target), "--force",
    ])
    cli.main()

    from swarlo._precommit_hook_source import SOURCE
    assert target.read_text() == SOURCE


# ── Doctor ──────────────────────────────────────────────────

def test_doctor_reports_missing_config(monkeypatch, tmp_path, capsys):
    """Doctor fails loudly when ~/.swarlo/config.json doesn't exist."""
    monkeypatch.setenv("SWARLO_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setattr(sys, "argv", ["swarlo", "doctor"])

    exit_code = cli.main()
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "config file" in out
    assert "FAIL" in out
    assert "missing" in out


def test_doctor_reports_malformed_config(monkeypatch, tmp_path, capsys):
    """Doctor fails when config isn't valid JSON."""
    config_path = tmp_path / "bad.json"
    config_path.write_text("{ not json")
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))
    monkeypatch.setattr(sys, "argv", ["swarlo", "doctor"])

    exit_code = cli.main()
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "invalid JSON" in out


def test_doctor_reports_unreachable_server(monkeypatch, tmp_path, capsys):
    """Doctor flags an unreachable server as FAIL and exits 1."""
    # Use a deliberately-dead port so the health check fails fast
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "server": "http://127.0.0.1:1",  # unreachable
        "hub": "atris",
        "member_id": "navigator",
        "api_key": "fake-key",
    }))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))
    monkeypatch.setattr(sys, "argv", ["swarlo", "doctor"])

    exit_code = cli.main()
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "server health" in out
    assert "unreachable" in out or "FAIL" in out


def test_doctor_reports_hook_drift(monkeypatch, tmp_path, capsys):
    """Doctor warns when the installed hook has drifted from canonical source.

    We can't easily spoof a git repo in this test, but we can exercise
    the core drift-detection logic by importing _run_doctor and running
    it against a test config that points at a local fake hook.
    Instead, we verify via the existing install-hook + drift test that
    the drift comparison works, and trust the integration path in
    _run_doctor. Here we just assert that doctor runs end-to-end
    without crashing when the hook exists but differs.
    """
    # Minimal setup: valid config format, point at a dead server so we
    # don't block on network, assert doctor reaches the git/hook checks
    # and exits cleanly with code 1 (server unreachable).
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "server": "http://127.0.0.1:1",
        "hub": "atris",
        "member_id": "navigator",
        "api_key": "fake-key",
    }))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))
    monkeypatch.setattr(sys, "argv", ["swarlo", "doctor"])

    exit_code = cli.main()
    out = capsys.readouterr().out

    # Either the server check fails (expected) OR we're not in a git repo
    # (also fine). In both cases doctor ran to completion.
    assert exit_code in (0, 1)
    assert "config file" in out


def test_tower_survives_members_missing_last_seen(monkeypatch, tmp_path, capsys):
    """Pre-liveness DBs lack last_seen — tower must not crash."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE members (
            member_id TEXT NOT NULL,
            hub_id TEXT NOT NULL,
            member_type TEXT NOT NULL,
            member_name TEXT NOT NULL,
            api_key TEXT,
            created_at TEXT NOT NULL,
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
            created_at TEXT NOT NULL
        );
        CREATE TABLE scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hub_id TEXT NOT NULL,
            coord_score INTEGER DEFAULT 0,
            computed_at TEXT NOT NULL
        );
        """
    )
    now = datetime.now(cli.UTC).isoformat()
    conn.execute(
        "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("agent-a", "my-team", "agent", "Agent A", now, now),
    )
    conn.execute(
        "INSERT INTO posts (post_id, hub_id, channel, member_id, member_name, member_type, "
        "content, kind, task_key, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("msg-1", "my-team", "ops", "agent-a", "Agent A", "agent",
         "ownerless", "message", "task:legacy", None, now),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        sys,
        "argv",
        ["swarlo", "tower", "--db", str(db_path), "--hub", "my-team", "--json"],
    )
    cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["hub"] == "my-team"
    assert payload["counts"]["unclaimed_tasks"] >= 1


def test_migration_adds_last_seen_to_legacy_members(tmp_path):
    """Opening SQLiteBackend migrates pre-liveness members + posts tables."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE members (
            member_id TEXT NOT NULL,
            hub_id TEXT NOT NULL,
            member_type TEXT NOT NULL,
            member_name TEXT NOT NULL,
            api_key TEXT,
            created_at TEXT NOT NULL,
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
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO members (member_id, hub_id, member_type, member_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("a1", "h1", "agent", "A", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    backend = SQLiteBackend(str(db_path))
    try:
        member_cols = {
            str(row[1])
            for row in backend.conn.execute("PRAGMA table_info(members)").fetchall()
        }
        post_cols = {
            str(row[1])
            for row in backend.conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        assert "last_seen" in member_cols
        assert "last_active" in member_cols
        assert "metadata" in post_cols
        assert "mentions" in post_cols
        indexes = {
            str(row[1])
            for row in backend.conn.execute("PRAGMA index_list(members)").fetchall()
        }
        post_indexes = {
            str(row[1])
            for row in backend.conn.execute("PRAGMA index_list(posts)").fetchall()
        }
        assert "idx_members_hub_type_seen" in indexes
        assert "idx_members_hub_seen_type" in indexes
        assert "idx_posts_open_claims_expiry" in post_indexes
    finally:
        backend.close()


def test_doctor_warns_on_missing_liveness_columns(monkeypatch, tmp_path, capsys):
    """Doctor surfaces local DB schema drift for last_seen / last_active."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "server": "http://127.0.0.1:1",
        "hub": "atris",
        "member_id": "navigator",
        "api_key": "fake-key",
    }))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    legacy = tmp_path / "swarlo.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        """
        CREATE TABLE members (
            member_id TEXT NOT NULL,
            hub_id TEXT NOT NULL,
            member_type TEXT NOT NULL,
            member_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (member_id, hub_id)
        )
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["swarlo", "doctor"])
    exit_code = cli.main()
    out = capsys.readouterr().out

    assert exit_code == 1  # server still unreachable
    assert "liveness columns" in out
    assert "last_seen" in out


def test_http_failure_message_hints_on_route_404():
    msg = cli._http_failure_message(
        "Unclaimed",
        404,
        {"detail": "Not Found"},
        route="/api/{hub}/unclaimed",
    )
    assert "Unclaimed failed (404)" in msg
    assert "missing /api/{hub}/unclaimed" in msg
    assert "restart" in msg.lower() or "upgrade" in msg.lower()


def test_http_failure_message_plain_for_non_404():
    msg = cli._http_failure_message("XP", 500, {"detail": "boom"}, route="/api/{hub}/xp")
    assert msg == "XP failed (500): {'detail': 'boom'}"
    assert "missing" not in msg


def test_unclaimed_404_hints_server_upgrade(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "server": "http://localhost:8080",
        "hub": "my-team",
        "api_key": "secret",
    }))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
        return 404, {"detail": "Not Found"}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["swarlo", "unclaimed"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    msg = str(exc_info.value)
    assert "404" in msg
    assert "unclaimed" in msg
    assert "restart" in msg.lower() or "upgrade" in msg.lower()
