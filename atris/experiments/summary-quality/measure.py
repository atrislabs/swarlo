"""Fixed replay metric for Swarlo summary quality."""

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


CLAIM_TEXT = "Working on dedupe check"


async def build_summary() -> str:
    with TemporaryDirectory() as tmpdir:
        backend = SQLiteBackend(str(Path(tmpdir) / "measure.db"))
        member = Member("agent-a", "agent", "Hugo", "hub-1")
        try:
            await backend.create_post("hub-1", member, "general", "Status update")
            await backend.claim("hub-1", member, "experiments", "summary:1", CLAIM_TEXT)
            return await backend.summarize_for_member("hub-1", "agent-a")
        finally:
            backend.close()


def main() -> int:
    summary = asyncio.run(build_summary())
    checks = {
        "has_board_header": "FLEET BOARD (Swarlo):" in summary,
        "has_open_claims_header": "OPEN CLAIMS (do not duplicate):" in summary,
        "has_status_update": "Status update" in summary,
        "has_claim_text": CLAIM_TEXT in summary,
        "claim_appears_once": summary.count(CLAIM_TEXT) == 1,
    }
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
