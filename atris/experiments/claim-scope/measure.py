"""Fixed replay metric for hub-wide claim scope."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from swarlo.sqlite_backend import SQLiteBackend
from swarlo.types import Member


async def run_scenario() -> dict:
    with TemporaryDirectory() as tmpdir:
        backend = SQLiteBackend(str(Path(tmpdir) / "measure.db"))
        owner = Member("agent-a", "agent", "Hugo", "hub-1")
        other = Member("agent-b", "agent", "Gideon", "hub-1")
        try:
            first = await backend.claim("hub-1", owner, "experiments", "task:same", "Owner claim")
            second = await backend.claim("hub-1", other, "ops", "task:same", "Duplicate cross-channel claim")

            foreign_blocked = False
            try:
                await backend.report("hub-1", other, "ops", "task:same", "done", "Trying to close someone else's task")
            except PermissionError:
                foreign_blocked = True

            open_claims = await backend.get_open_claims("hub-1", task_key="task:same")
            return {
                "first_claim_succeeds": first.claimed and not first.conflict,
                "cross_channel_duplicate_conflicts": (not second.claimed) and second.conflict,
                "foreign_cross_channel_report_blocked": foreign_blocked,
                "one_open_claim_remains": len(open_claims) == 1 and open_claims[0].member_name == "Hugo",
            }
        finally:
            backend.close()


def main() -> int:
    checks = asyncio.run(run_scenario())
    passed = sum(1 for ok in checks.values() if ok)
    total = len(checks)
    payload = {
        "score": round(passed / total if total else 0.0, 4),
        "passed": passed,
        "total": total,
        "status": "pass" if passed == total else "fail",
        "checks": checks,
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
