"""Run log and daily schedule for the swarlo pruner.

record_run(home, trigger, ok, mb_before=None, **counts)
    Appends one JSON line to ``home/prune.jsonl``::

        {"at": ISO UTC, "trigger": ..., "ok": ..., **counts,
         "mb_before": x, "mb_after": y}

    ``mb_after`` is the size of ``home`` measured now; ``mb_before`` defaults
    to the same value when the caller did not measure it. Returns the record.

start_schedule(run_fn, home, interval_hours=24)
    Starts an asyncio task that calls ``run_fn()`` every ``interval_hours`` and
    records each call with trigger ``"schedule"``. ``run_fn`` may be sync or
    async and may return a dict of counts to include in the record. A run that
    raises is recorded with ``ok: false`` and the error, and the loop keeps
    going. The first wait picks up from the last scheduled run in
    ``prune.jsonl``, so restarting the hub does not prune again early.
    Returns the task; cancel it to stop.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_NAME = "prune.jsonl"


def home_mb(home) -> float:
    total = 0
    for root, _dirs, files in os.walk(home):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return round(total / (1024 * 1024), 2)


def record_run(home, trigger: str, ok: bool, mb_before: float | None = None, **counts) -> dict:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    mb_after = home_mb(home)
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trigger": trigger,
        "ok": bool(ok),
        **counts,
        "mb_before": mb_after if mb_before is None else mb_before,
        "mb_after": mb_after,
    }
    with open(home / LOG_NAME, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def last_scheduled_at(home) -> datetime | None:
    last = None
    try:
        with open(Path(home) / LOG_NAME) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get("trigger") != "schedule":
                        continue
                    at = datetime.fromisoformat(r["at"])
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                if last is None or at > last:
                    last = at
    except FileNotFoundError:
        pass
    return last


async def _run_once(run_fn, home) -> dict:
    mb_before = home_mb(home)
    try:
        result = run_fn()
        if inspect.isawaitable(result):
            result = await result
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return record_run(home, "schedule", False, mb_before=mb_before, error=f"{type(e).__name__}: {e}")
    counts = result if isinstance(result, dict) else {}
    counts = {k: v for k, v in counts.items() if k not in ("at", "trigger", "ok", "mb_before", "mb_after")}
    return record_run(home, "schedule", True, mb_before=mb_before, **counts)


async def _loop(run_fn, home, interval: float) -> None:
    last = last_scheduled_at(home)
    wait = 0.0
    if last is not None:
        since = (datetime.now(timezone.utc) - last).total_seconds()
        wait = min(interval, max(0.0, interval - since))
    while True:
        if wait:
            await asyncio.sleep(wait)
        await _run_once(run_fn, home)
        wait = interval


def start_schedule(run_fn, home, interval_hours: float = 24) -> asyncio.Task:
    return asyncio.ensure_future(_loop(run_fn, Path(home), interval_hours * 3600))
