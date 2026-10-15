"""Read-only bridge from Atris campaign event logs to a Swarlo hub.

Each workspace keeps its campaign log in ``<checkout>/.atris/state/atris.db``
(table ``events``). The bridge opens those databases read-only, tails them by
``pos``, and turns campaign events into board posts made by the event's actor:

    mission_claimed      -> claim
    mission_started      -> briefing (data.objective)
    step with cost_usd   -> touch the claim (cost recorded in bridge state)
    mission_ended        -> report done (kept) or failed (failed / reverted)
    ask_opened           -> escalation in #escalations
    judge_ran            -> result in #experiments (skipped when it repeats the
                            campaign's last judge post with digits as '#')
    judge_set            -> message "judge changed"

task_key is ``<campaign_id>/<mission_id>``. Events are deduped by id, so
re-tailing a database is harmless. Each actor registers on the hub once and
its key is kept in ``~/.swarlo/<hub>-<actor>.key`` (registering again would
rotate the key, so an existing key file is always reused).

Run: ``python3 -m swarlo.campaign_bridge [--once] [--db PATH ...]``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .client import SwarloClient, SwarloError

SWARLO_HOME = Path.home() / ".swarlo"
KNOWN_WORKSPACES = (
    Path.home() / "arena" / "empire" / "personal_context",
    Path.home() / "arena" / "empire" / "atris-cli",
    Path(__file__).resolve().parent.parent,
)
SEEN_CAP = 5000
DIGITS = re.compile(r"\d+")  # digit runs, as the hub folds them


def default_dbs() -> list[Path]:
    """$ATRIS_CAMPAIGN_DB (os.pathsep-separated) or the known workspaces."""
    env = os.environ.get("ATRIS_CAMPAIGN_DB")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p]
    return [ws / ".atris" / "state" / "atris.db" for ws in KNOWN_WORKSPACES]


def read_events(db_path: Path, after_pos: int = 0) -> list[dict]:
    """Return events with pos > after_pos, oldest first. Never writes."""
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT pos, id, campaign_id, type, at, actor, data FROM events "
            "WHERE pos > ? ORDER BY pos",
            (after_pos,),
        ).fetchall()
    finally:
        con.close()
    events = []
    for pos, event_id, campaign_id, kind, at, actor, data in rows:
        try:
            payload = json.loads(data) if data else {}
        except json.JSONDecodeError:
            payload = {}
        events.append({
            "pos": pos, "id": event_id, "campaign_id": campaign_id, "type": kind,
            "at": at, "actor": actor, "data": payload if isinstance(payload, dict) else {},
        })
    return events


@dataclass
class Action:
    """One board write, made as `member`."""
    verb: str  # claim | post | touch | report
    member: str
    channel: str
    content: str = ""
    task_key: str | None = None
    kind: str = "message"
    status: str | None = None
    metadata: dict = field(default_factory=dict)


def map_event(event: dict, channel: str = "general") -> Action | None:
    """Translate one campaign event into a board action, or None to skip."""
    data = event["data"]
    campaign = event["campaign_id"]
    actor = event["actor"]
    mission = data.get("mission_id")
    task_key = f"{campaign}/{mission}" if mission else None
    meta = {"campaign_event": event["id"], "campaign_id": campaign}
    kind = event["type"]

    if kind == "mission_claimed" and task_key:
        return Action("claim", actor, channel, data.get("claim_key") or mission, task_key)
    if kind == "mission_started" and task_key:
        what = data.get("objective") or data.get("what") or mission
        return Action("post", actor, channel, f"started {mission}: {what}", task_key,
                      kind="briefing", metadata=meta)
    if kind == "step" and task_key and data.get("cost_usd") is not None:
        return Action("touch", actor, channel, task_key=task_key,
                      metadata={**meta, "cost_usd": data["cost_usd"]})
    if kind == "mission_ended" and task_key:
        outcome = data.get("outcome", "")
        status = "done" if outcome == "kept" else "failed"
        cost = data.get("cost_usd")
        text = f"{mission} {outcome or 'ended'}"
        if cost is not None:
            text += f" · ${cost:.2f}"
        if data.get("receipt_path"):
            text += f" · receipt {data['receipt_path']}"
        for k in ("outcome", "cost_usd", "receipt_path"):
            if data.get(k) is not None:
                meta[k] = data[k]
        return Action("report", actor, channel, text, task_key, status=status, metadata=meta)
    if kind == "ask_opened":
        ask = data.get("ask_id") or data.get("id") or event["id"]
        question = data.get("question") or data.get("ask") or data.get("text") or ""
        text = f"{campaign} asks: {question} · answer: atris campaign answer {campaign} {ask} \"...\""
        return Action("post", actor, "escalations", text, task_key, kind="escalation", metadata=meta)
    if kind == "judge_ran":
        verdict = "passed" if data.get("passed") else "not passed"
        text = f"{campaign} judge {verdict}: {data.get('summary', '')}".rstrip(": ")
        return Action("post", actor, "experiments", text, kind="result", metadata=meta)
    if kind == "judge_set":
        judge = data.get("judge", "")
        text = f"{campaign} judge changed" + (f": {judge}" if judge else "")
        return Action("post", actor, channel, text, metadata=meta)
    return None


class CampaignBridge:
    """Tails campaign dbs and replays mapped events onto a hub."""

    def __init__(self, server: str, hub: str, dbs: list[Path] | None = None,
                 home: Path = SWARLO_HOME, channel: str = "general",
                 client_factory: Callable[..., SwarloClient] = SwarloClient):
        self.server = server
        self.hub = hub
        self.dbs = [Path(p) for p in (dbs if dbs is not None else default_dbs())]
        self.home = Path(home)
        self.channel = channel
        self.client_factory = client_factory
        self.state_path = self.home / f"campaign_bridge-{hub}.json"
        self.state = self._load_state()
        self._clients: dict[str, SwarloClient] = {}

    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        state.setdefault("pos", {})
        state.setdefault("seen", [])
        state.setdefault("cost", {})
        state.setdefault("judge_last", {})
        return state

    def _save_state(self) -> None:
        self.state["seen"] = self.state["seen"][-SEEN_CAP:]
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.state_path)

    def client_for(self, member: str, fresh: bool = False) -> SwarloClient:
        """Client for `member`, registering only if no key file exists yet
        (or `fresh`, after the hub rejected the stored key)."""
        if member in self._clients and not fresh:
            return self._clients[member]
        key_path = self.home / f"{self.hub}-{member}.key"
        client = self.client_factory(self.server, hub=self.hub)
        if key_path.exists() and not fresh:
            client.api_key = key_path.read_text().strip()
            client.member_id = member
        else:
            key = client.join(member, member_type="agent", name=member)
            self.home.mkdir(parents=True, exist_ok=True)
            key_path.write_text(key)
            key_path.chmod(0o600)
        self._clients[member] = client
        return client

    def apply(self, action: Action) -> str:
        """Perform one action. Returns 'ok' or 'skipped' (hub said 404/409).
        A stale stored key (401) re-registers the member once and retries."""
        try:
            return self._apply(self.client_for(action.member), action)
        except SwarloError as exc:
            if exc.status_code != 401:
                raise
        return self._apply(self.client_for(action.member, fresh=True), action)

    def _apply(self, client: SwarloClient, action: Action) -> str:
        try:
            if action.verb == "claim":
                client.claim(action.channel, action.task_key, action.content)
            elif action.verb == "touch":
                client.touch(action.channel, action.task_key)
            elif action.verb == "report":
                client.report(action.channel, action.task_key, action.status,
                              action.content, metadata=action.metadata)
            else:
                client.post(action.channel, action.content, kind=action.kind,
                            task_key=action.task_key, metadata=action.metadata)
        except SwarloError as exc:
            if exc.status_code in (404, 409):
                return "skipped"
            raise
        return "ok"

    def run_once(self) -> dict:
        """Tail every db once. Stops a db at the first hub failure so the
        event is retried next run. Returns counts."""
        counts = {"seen": 0, "posted": 0, "skipped": 0, "ignored": 0}
        seen = set(self.state["seen"])
        try:
            for db in self.dbs:
                key = str(db)
                for event in read_events(db, self.state["pos"].get(key, 0)):
                    counts["seen"] += 1
                    if event["id"] not in seen:
                        action = map_event(event, self.channel)
                        judge = None
                        if action is not None and event["type"] == "judge_ran":
                            judge = DIGITS.sub("#", action.content)
                            if self.state["judge_last"].get(event["campaign_id"]) == judge:
                                action = None  # same judge line, only numbers moved
                        if action is None:
                            counts["ignored"] += 1
                        else:
                            outcome = self.apply(action)
                            counts["posted" if outcome == "ok" else "skipped"] += 1
                            if judge is not None and outcome == "ok":
                                self.state["judge_last"][event["campaign_id"]] = judge
                            if action.verb == "touch":
                                cost = self.state["cost"]
                                cost[action.task_key] = cost.get(action.task_key, 0) + action.metadata["cost_usd"]
                        seen.add(event["id"])
                        self.state["seen"].append(event["id"])
                    self.state["pos"][key] = event["pos"]
        finally:
            self._save_state()
        return counts


def main(argv: list[str] | None = None) -> int:
    config = {}
    try:
        config = json.loads((SWARLO_HOME / "config.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    parser = argparse.ArgumentParser(prog="swarlo.campaign_bridge", description=__doc__.splitlines()[0])
    parser.add_argument("--server", default=os.environ.get("SWARLO_SERVER", config.get("server", "http://localhost:8090")))
    parser.add_argument("--hub", default=os.environ.get("SWARLO_HUB", config.get("hub", "atris")))
    parser.add_argument("--db", action="append", help="campaign db (repeatable); default: known workspaces")
    parser.add_argument("--channel", default="general")
    parser.add_argument("--once", action="store_true", help="tail once and exit")
    parser.add_argument("--interval", type=float, default=10.0)
    args = parser.parse_args(argv)

    bridge = CampaignBridge(args.server, args.hub,
                            dbs=[Path(p).expanduser() for p in args.db] if args.db else None,
                            channel=args.channel)
    while True:
        print(json.dumps(bridge.run_once()), flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
