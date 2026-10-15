import asyncio
import json
from datetime import datetime, timedelta, timezone

from swarlo.gc_schedule import record_run, start_schedule

MB = 1024 * 1024


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
