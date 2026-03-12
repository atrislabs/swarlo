"""Restore worker-routing to baseline."""

from pathlib import Path


TARGET = Path(__file__).resolve().parent / "candidate.py"

TARGET.write_text(
    '''"""Bounded mutation target for worker routing."""

from __future__ import annotations


def should_claim(member_role: str, task_text: str) -> bool:
    """Return True when a worker should claim a task."""
    text = task_text.lower()
    role = member_role.lower()

    shared_keywords = (
        "task",
        "issue",
        "claim",
        "work",
        "routing",
        "build",
        "fix",
        "review",
        "validate",
        "test",
    )

    if role not in {"builder", "validator"}:
        return False

    return any(keyword in text for keyword in shared_keywords)
''',
    encoding="utf-8",
)

print("reset worker-routing to baseline")
