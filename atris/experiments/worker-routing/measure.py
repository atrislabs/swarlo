"""Objective replay metric for worker routing."""

from __future__ import annotations

import json
from pathlib import Path
import sys


EXPERIMENT_DIR = Path(__file__).resolve().parent
if str(EXPERIMENT_DIR) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_DIR))

from candidate import should_claim


CASES = [
    ("builder", "Build the CLI flag parser for worker-routing", True),
    ("builder", "Implement the git DAG bundle endpoint", True),
    ("builder", "Review the claim conflict regression test", False),
    ("builder", "Validate the release flow before publish", False),
    ("validator", "Review the claim conflict regression test", True),
    ("validator", "Validate the release flow before publish", True),
    ("validator", "Build the CLI flag parser for worker-routing", False),
    ("validator", "Implement the git DAG bundle endpoint", False),
]


def main() -> int:
    passed = 0

    for role, task_text, expected in CASES:
        actual = should_claim(role, task_text)
        if actual == expected:
            passed += 1

    total = len(CASES)
    score = passed / total if total else 0.0
    payload = {
        "score": round(score, 4),
        "passed": passed,
        "total": total,
        "status": "pass" if passed == total else "fail",
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
