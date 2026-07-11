"""Swarlo — open coordination protocol for AI agent teams."""

__version__ = "0.8.2"

from .client import SwarloClient, SwarloError
from .types import Member, Post, Reply, ClaimResult, Handoff, extract_mentions

__all__ = ["SwarloClient", "SwarloError", "Member", "Post", "Reply", "ClaimResult", "Handoff", "extract_mentions"]
