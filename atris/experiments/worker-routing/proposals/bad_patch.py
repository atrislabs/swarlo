"""Proposal that makes routing worse by over-claiming."""

from pathlib import Path
import os


TARGET = Path(os.environ["EXPERIMENT_TARGET"])

TARGET.write_text(
    '''"""Bounded mutation target for worker routing."""

from __future__ import annotations


def should_claim(member_role: str, task_text: str) -> bool:
    """Over-broad router that claims almost everything."""
    return bool(task_text.strip())
''',
    encoding="utf-8",
)

print("bad patch: claim every non-empty task")
