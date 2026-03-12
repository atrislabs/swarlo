import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "swarlo" / "__main__.py"
SPEC = importlib.util.spec_from_file_location("swarlo_cli", CLI_PATH)
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)


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
    monkeypatch.setattr(sys, "argv", ["swarlo", "claim", "general", "task:1", "Taking this"])

    cli.main()
    assert called["url"] == "http://localhost:8080/api/my-team/channels/general/claim"
    assert called["payload"]["task_key"] == "task:1"
    assert called["api_key"] == "secret"
    assert "Claimed task:1" in capsys.readouterr().out


def test_read_prints_posts(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"server": "http://localhost:8080", "hub": "my-team", "api_key": "secret"}))
    monkeypatch.setenv("SWARLO_CONFIG", str(config_path))

    def fake_request(method, url, payload=None, api_key=None):
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
    monkeypatch.setattr(sys, "argv", ["swarlo", "read", "general"])

    cli.main()
    assert "[claim] task:1 Hugo: Taking this" in capsys.readouterr().out


def test_missing_runtime_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARLO_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit, match="Missing server"):
        cli._require_runtime(type("Args", (), {"server": None, "hub": None, "api_key": None})())
