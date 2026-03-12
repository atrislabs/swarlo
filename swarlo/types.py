"""Swarlo protocol types. No framework dependencies."""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Member:
    member_id: str
    member_type: str  # "human" | "agent" | "system"
    member_name: str
    hub_id: str


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
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


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
