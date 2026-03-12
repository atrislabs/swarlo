"""Proposal that makes builder vs validator routing role-aware."""

from pathlib import Path
import os


TARGET = Path(os.environ["EXPERIMENT_TARGET"])

TARGET.write_text(
    '''"""Bounded mutation target for worker routing."""

from __future__ import annotations


def should_claim(member_role: str, task_text: str) -> bool:
    """Route claims using small role-specific keyword sets."""
    text = task_text.lower()
    role = member_role.lower()

    builder = ("build", "implement", "ship", "feature", "cli", "server", "backend", "code")
    validator = ("validate", "review", "audit", "verify", "test", "regression", "bug")

    if role == "builder":
        return any(keyword in text for keyword in builder)
    if role == "validator":
        return any(keyword in text for keyword in validator)
    return False
''',
    encoding="utf-8",
)

print("fix patch: route claims by worker role")
