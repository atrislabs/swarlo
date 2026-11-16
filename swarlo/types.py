"""Swarlo protocol types. No framework dependencies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

MENTION_RE = re.compile(r"@(\w[\w.-]*)")

MAX_POST_CONTENT_LENGTH = 4000
POST_PREVIEW_LENGTH = 500

TYPED_POST_KINDS = frozenset({
    "claim",
    "release",
    "result",
    "blocked",
    "review",
    "approval",
    "note",
})
# Preserved for backward compatibility with existing callers and tests.
LEGACY_POST_KINDS = frozenset({
    "message",
    "status",
    "nudge",
    "question",
    "escalation",
    "briefing",
    "assign",
    "failed",
    "hypothesis",
})
VALID_POST_KINDS = TYPED_POST_KINDS | LEGACY_POST_KINDS

CLAIM_LIKE_NOTE_RE = re.compile(
    r"\b("
    r"claiming|"
    r"i(?:'|’)ll\s+take|"
    r"i\s+will\s+take|"
    r"working\s+on|"
    r"on\s+it|"
    r"taking\s+this|"
    r"i(?:'|’)m\s+taking|"
    r"grabbing\s+this|"
    r"picking\s+(?:this|it)\s+up"
    r")\b",
    re.IGNORECASE,
)


def validate_post_content(content: str) -> str:
    """Validate post body is non-empty and within the 4000-char limit."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Post content must not be empty or whitespace-only")
    if len(content) > MAX_POST_CONTENT_LENGTH:
        raise ValueError(
            f"Post content exceeds {MAX_POST_CONTENT_LENGTH} character limit "
            f"({len(content)} chars)"
        )
    return content


def validate_post_kind(kind: str) -> str:
    """Validate post kind against allowed typed (and legacy) post kinds."""
    if not isinstance(kind, str) or kind not in VALID_POST_KINDS:
        allowed = ", ".join(sorted(TYPED_POST_KINDS))
        raise ValueError(f"Invalid post kind {kind!r}. Must be one of: {allowed}")
    return kind


def format_post_preview(content: str, limit: int = POST_PREVIEW_LENGTH) -> tuple[str, bool]:
    """Return (preview_text, truncated) with an expand marker when content > limit."""
    if len(content) <= limit:
        return content, False
    remaining = len(content) - limit
    return f"{content[:limit]}... [truncated, {remaining} more chars]", True


def check_claim_like_note(kind: str, content: str) -> Optional[str]:
    """Return a warning if a freeform note contains claim-like language."""
    if kind != "note":
        return None
    match = CLAIM_LIKE_NOTE_RE.search(content or "")
    if not match:
        return None
    return (
        f"Post kind is 'note' but content contains claim-like language "
        f"({match.group(0)!r}); use kind='claim' or the claim endpoint instead."
    )


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
    repeat_count: int = 1  # >1 when identical posts were folded into this one
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d.get("repeat_count", 1) == 1:
            del d["repeat_count"]
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
        if d.get("warning") is None:
            del d["warning"]
        preview, truncated = format_post_preview(self.content)
        if truncated:
            d["preview"] = preview
            d["truncated"] = True
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
