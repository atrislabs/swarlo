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
    kind: str  # message | claim | result | failed | review | question | escalation | hypothesis
    channel: str
    member_id: str
    member_name: str
    member_type: str
    task_key: Optional[str] = None
    status: Optional[str] = None  # open | done | failed | blocked
    priority: int = 0  # 0=normal, 1-5=higher priority claimed first
    metadata: Optional[dict] = None  # structured data: steps, artifacts, files
    mentions: Optional[list[str]] = None  # resolved member_ids from @mentions
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d.get("metadata") is None:
            del d["metadata"]
        if d.get("mentions") is None:
            del d["mentions"]
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

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClaimResult:
    claimed: bool
    conflict: bool
    post_id: Optional[str] = None
    channel: Optional[str] = None
    kind: Optional[str] = None
    existing_claim: Optional[Post] = None
    message: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d
