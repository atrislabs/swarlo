"""Tests for the read-only campaign bridge (campaign event log -> Swarlo hub)."""

import hashlib
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from swarlo import SwarloClient, SwarloError
from swarlo.campaign_bridge import CampaignBridge, map_event, read_events
from swarlo.server import app, set_backend
from swarlo.sqlite_backend import SQLiteBackend

SCHEMA = """
CREATE TABLE events (
  pos INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  campaign_id TEXT NOT NULL,
  origin TEXT NOT NULL,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  data TEXT NOT NULL,
  UNIQUE (origin, seq)
);
"""

M = "m-demo"
EVENTS = [
    ("mission_proposed", "builder", {"mission_id": M, "objective": "ship it"}),
    ("mission_claimed", "builder", {"mission_id": M, "claim_key": "ship the demo"}),
    ("mission_started", "builder", {"mission_id": M, "member": "builder", "objective": "ship it"}),
    ("step", "builder", {"mission_id": M, "kind": "plan", "engine": "claude"}),
    ("step", "builder", {"mission_id": M, "cost_usd": 0.25, "tokens": 1000}),
    ("mission_ended", "builder", {"mission_id": M, "outcome": "kept", "cost_usd": 0.25,
                                  "receipt_path": "atris/runs/demo.json"}),
    ("judge_ran", "judge", {"passed": False, "summary": "repeats 0.72, members 1, closed 0"}),
    ("ask_opened", "builder", {"ask_id": "a-1", "question": "ship to prod?"}),
    ("judge_set", "agent", {"judge": "python3 judge.py"}),
]


def make_db(path: Path, events=EVENTS, campaign="cmp-demo", start=0) -> None:
    con = sqlite3.connect(path)
    if start == 0:
        con.executescript(SCHEMA)
    for i, (kind, actor, data) in enumerate(events, start=start + 1):
        con.execute(
            "INSERT INTO events (id, campaign_id, origin, seq, type, at, actor, data) VALUES (?,?,?,?,?,?,?,?)",
            (f"evt-{i}", campaign, "test", i, kind, "2026-10-15T00:00:00Z", actor, json.dumps(data)),
        )
    con.commit()
    con.close()


def ev(kind, data, actor="builder", campaign="cmp-demo"):
    return {"pos": 1, "id": "evt-x", "campaign_id": campaign, "type": kind,
            "at": "", "actor": actor, "data": data}


# ── Mapping ────────────────────────────────────────────────

class TestMapEvent:
    def test_claimed_is_claim_by_actor(self):
        a = map_event(ev("mission_claimed", {"mission_id": M, "claim_key": "ship"}))
        assert (a.verb, a.member, a.task_key, a.content) == ("claim", "builder", "cmp-demo/m-demo", "ship")

    def test_started_is_briefing_with_objective(self):
        a = map_event(ev("mission_started", {"mission_id": M, "objective": "ship it"}))
        assert a.verb == "post" and a.kind == "briefing" and "ship it" in a.content

    def test_step_with_cost_is_touch(self):
        a = map_event(ev("step", {"mission_id": M, "cost_usd": 0.5}))
        assert a.verb == "touch" and a.metadata["cost_usd"] == 0.5

    def test_step_without_cost_ignored(self):
        assert map_event(ev("step", {"mission_id": M, "kind": "plan"})) is None

    @pytest.mark.parametrize("outcome,status", [("kept", "done"), ("failed", "failed"), ("reverted", "failed")])
    def test_ended_reports_status(self, outcome, status):
        a = map_event(ev("mission_ended", {"mission_id": M, "outcome": outcome, "cost_usd": 1.5,
                                           "receipt_path": "r.json"}))
        assert a.verb == "report" and a.status == status
        assert a.metadata["cost_usd"] == 1.5 and a.metadata["receipt_path"] == "r.json"

    def test_ask_is_escalation_with_answer_command(self):
        a = map_event(ev("ask_opened", {"ask_id": "a-1", "question": "ok?"}))
        assert a.channel == "escalations" and a.kind == "escalation"
        assert 'atris campaign answer cmp-demo a-1 "..."' in a.content

    def test_judge_ran_is_result_in_experiments(self):
        a = map_event(ev("judge_ran", {"passed": True, "summary": "all good"}, actor="judge"))
        assert a.channel == "experiments" and a.kind == "result" and "all good" in a.content

    def test_judge_set_is_message(self):
        a = map_event(ev("judge_set", {"judge": "x.py"}))
        assert a.kind == "message" and "judge changed" in a.content

    def test_unknown_ignored(self):
        assert map_event(ev("budget_reserved", {"mission_id": M, "amount_usd": 2})) is None


# ── End to end against a live hub ──────────────────────────

@pytest.fixture(scope="module")
def server():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    backend = SQLiteBackend(db_path)
    set_backend(backend)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{url}/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield url
    srv.should_exit = True
    backend.close()


@pytest.fixture
def env(tmp_path, server, request):
    hub = f"hub-{request.node.name}"[:40]
    db = tmp_path / "atris.db"
    make_db(db)
    return {"server": server, "hub": hub, "db": db, "home": tmp_path / "swarlo"}


def bridge(env, **kw):
    return CampaignBridge(env["server"], env["hub"], dbs=[env["db"]], home=env["home"], **kw)


def board(env, channel):
    reader = SwarloClient(env["server"], hub=env["hub"])
    reader.join("reader")
    return reader.read(channel, limit=50)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestBridgeLive:
    def test_events_land_on_board(self, env):
        counts = bridge(env).run_once()
        assert counts == {"seen": 9, "posted": 7, "skipped": 0, "ignored": 2}

        general = board(env, "general")
        claim = next(p for p in general if p["kind"] == "claim")
        assert claim["member_id"] == "builder" and claim["task_key"] == "cmp-demo/m-demo"
        assert claim["status"] == "done"
        result = next(p for p in general if p["kind"] == "result")
        assert "kept" in result["content"] and "atris/runs/demo.json" in result["content"]
        assert any(p["kind"] == "briefing" and "ship it" in p["content"] for p in general)
        assert any("judge changed" in p["content"] for p in general)

        assert board(env, "escalations")[0]["kind"] == "escalation"
        exp = board(env, "experiments")[0]
        assert exp["member_id"] == "judge" and "repeats 0.72" in exp["content"]

    def test_retail_is_harmless(self, env):
        b = bridge(env)
        b.run_once()
        before = len(board(env, "general"))
        assert bridge(env).run_once()["seen"] == 0  # pos remembered across instances
        b.state["pos"] = {}  # lose pos: dedupe by event id still holds
        assert b.run_once()["posted"] == 0
        assert len(board(env, "general")) == before

    def test_tails_new_events_only(self, env):
        b = bridge(env)
        b.run_once()
        make_db(env["db"], [("judge_ran", "judge", {"passed": True, "summary": "green"})], start=len(EVENTS))
        assert b.run_once() == {"seen": 1, "posted": 1, "skipped": 0, "ignored": 0}

    def test_registers_each_actor_once(self, env):
        joins = []

        class Counting(SwarloClient):
            def join(self, member_id, *a, **k):
                joins.append(member_id)
                return super().join(member_id, *a, **k)

        bridge(env, client_factory=Counting).run_once()
        assert sorted(joins) == ["agent", "builder", "judge"]
        key = env["home"] / f"{env['hub']}-builder.key"
        assert key.exists() and oct(key.stat().st_mode & 0o777) == "0o600"

        make_db(env["db"], [("judge_set", "builder", {"judge": "y.py"})], start=len(EVENTS))
        bridge(env, client_factory=Counting).run_once()  # fresh instance reuses key file
        assert len(joins) == 3

    def test_stale_key_re_registers(self, env):
        env["home"].mkdir(parents=True)
        stale = env["home"] / f"{env['hub']}-builder.key"
        stale.write_text("key-from-an-older-hub")
        counts = bridge(env).run_once()
        assert counts["posted"] == 7
        assert stale.read_text() != "key-from-an-older-hub"
        assert any(p["kind"] == "claim" for p in board(env, "general"))

    def test_never_writes_campaign_db(self, env):
        before = digest(env["db"])
        bridge(env).run_once()
        assert digest(env["db"]) == before
        assert not Path(str(env["db"]) + "-wal").exists()

    def test_hub_failure_does_not_advance(self, env):
        class Down(SwarloClient):
            def post(self, *a, **k):
                raise SwarloError(503, {"detail": "down"})

        with pytest.raises(SwarloError):
            bridge(env, client_factory=Down).run_once()
        # claim (pos 2) landed; the briefing post at pos 3 failed and is retried
        assert bridge(env).run_once()["seen"] == len(EVENTS) - 2

    def test_missing_db_is_quiet(self, env, tmp_path):
        assert read_events(tmp_path / "nope.db") == []
        b = CampaignBridge(env["server"], env["hub"], dbs=[tmp_path / "nope.db"], home=env["home"])
        assert b.run_once()["seen"] == 0
