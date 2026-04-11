"""Swarlo — open coordination protocol for AI agent teams."""

__version__ = "0.4.1"

from .client import SwarloClient, SwarloError
from .types import Member, Post, Reply, ClaimResult, extract_mentions

__all__ = ["SwarloClient", "SwarloError", "Member", "Post", "Reply", "ClaimResult", "extract_mentions"]
