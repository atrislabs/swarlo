"""Swarlo backend interface. Any storage can implement this."""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional

from .types import Member, Post, Reply, ClaimResult


class SwarloBackend(ABC):

    @abstractmethod
    async def list_channels(self, hub_id: str) -> list[str]: ...

    @abstractmethod
    async def read_channel(self, hub_id: str, channel: str, limit: int = 10) -> list[Post]: ...

    @abstractmethod
    async def create_post(self, hub_id: str, member: Member, channel: str,
                          content: str, kind: str = "message",
                          task_key: Optional[str] = None, status: Optional[str] = None,
                          metadata: Optional[dict] = None, priority: int = 0) -> Post: ...

    @abstractmethod
    async def reply(self, hub_id: str, member: Member, post_id: str, content: str) -> Reply: ...

    @abstractmethod
    async def claim(self, hub_id: str, member: Member, channel: str,
                    task_key: str, content: str) -> ClaimResult: ...

    @abstractmethod
    async def assign(self, hub_id: str, assigner: Member, channel: str,
                     task_key: str, assignee_id: str, content: str) -> ClaimResult:
        """Push-assign a task to a specific member. Creates a claim on their behalf."""
        ...

    @abstractmethod
    async def report(self, hub_id: str, member: Member, channel: str,
                     task_key: str, status: str, content: str,
                     parent_id: Optional[str] = None) -> Post: ...

    @abstractmethod
    async def get_open_claims(self, hub_id: str, channel: Optional[str] = None,
                              task_key: Optional[str] = None) -> list[Post]: ...

    @abstractmethod
    async def get_replies(self, hub_id: str, post_id: str) -> list[Reply]: ...

    @abstractmethod
    async def summarize_for_member(self, hub_id: str, member_id: str, limit: int = 10) -> str: ...
