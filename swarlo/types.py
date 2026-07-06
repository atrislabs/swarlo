"""Swarlo protocol types. No framework dependencies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

MENTION_RE = re.compile(r"@(\w[\w.-]*)")


def extract_mentions(content: str) -> list[str]:
    """Extract @mentions from post content. Returns list of names (without @)."""
    return MENTION_RE.findall(content)


@dataclass
class Handoff:
    """Structured state passed from one agent to the next on task completion.

    Stored serialized inside Post.metadata["handoff"] — no schema migration.
    Server bundles upstream handoffs into claim_next responses so downstream
    agents receive predecessor state in one round-trip.
    """
    artifacts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Handoff":
        return cls(
            artifacts=list(data.get("artifacts") or []),
            decisions=list(data.get("decisions") or []),
            open_questions=list(data.get("open_questions") or []),
            notes=data.get("notes"),
        )


@dataclass
class Member:
    member_id: str
    member_type: str  # "human" | "agent" | "system"
    member_name: str
    hub_id: str
    webhook_url: Optional[str] = None  # callback URL for notifications


@dataclass
class Post:
    post_id: str
    content: str
    kind: str  # message | claim | assign | result | failed | review | question | escalation | hypothesis
    channel: str
    member_id: str
    member_name: str
    member_type: str
    task_key: Optional[str] = None
    status: Optional[str] = None  # open | done | failed | blocked
    priority: int = 0  # 0=normal, 1-5=higher priority claimed first
    metadata: Optional[dict] = None  # structured data: steps, artifacts, files
    mentions: Optional[list[str]] = None  # resolved member_ids from @mentions
    depends_on: Optional[list[str]] = None  # task_keys this post depends on
    created_at: Optional[str] = None
    replies: Optional[list[dict]] = None  # eager-loaded replies — fixes thread fragmentation
    display_id: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d.get("metadata") is None:
            del d["metadata"]
        if d.get("mentions") is None:
            del d["mentions"]
        if d.get("depends_on") is None:
            del d["depends_on"]
        if d.get("replies") is None:
            del d["replies"]
        if d.get("display_id") is None:
            del d["display_id"]
        return d


@dataclass
class Reply:
    reply_id: str
    post_id: str
    content: str
    member_id: str
    member_name: str
    member_type: str
    created_at: Optional[str] = None
    display_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClaimResult:
    claimed: bool
    conflict: bool
    post_id: Optional[str] = None
    display_id: Optional[str] = None
    channel: Optional[str] = None
    kind: Optional[str] = None
    existing_claim: Optional[Post] = None
    message: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d
