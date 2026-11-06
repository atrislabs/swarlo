"""Git DAG layer for Swarlo. Bare repo + bundles. Same pattern as karpathy/agenthub.

Git is the blob store. SQLite tracks metadata (hash, parent, member, message).
All git operations are subprocess calls to the git binary.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

HASH_RE = re.compile(r"^[0-9a-f]{4,64}$")


def _valid_hash(h: str) -> bool:
    """Check if string is a valid git commit hash (4-64 hex chars)."""
    return bool(HASH_RE.match(h))


class GitDAG:
    """Wraps a bare git repository for bundle-based code exchange."""

    def __init__(self, repo_path: str):
        self.path = Path(repo_path).resolve()
        self._lock = asyncio.Lock()
        self._initialized = False

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command in the bare repo context."""
        env = {**os.environ, "GIT_DIR": str(self.path)}
        return subprocess.run(
            ["git", *args],
            cwd=str(self.path),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=check,
        )

    def init(self) -> None:
        """Create or open a bare git repository."""
        if (self.path / "HEAD").exists():
            self._initialized = True
            return
        subprocess.run(
            ["git", "init", "--bare", str(self.path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        self._initialized = True

    def _ensure_init(self) -> None:
        """Lazy init: create bare repo if not already initialized."""
        if not self._initialized:
            self.init()

    def commit_exists(self, hash: str) -> bool:
        """Check if a commit exists in the repository."""
        if not _valid_hash(hash):
            return False
        result = self._git("cat-file", "-t", hash, check=False)
        return result.returncode == 0

    def get_commit_info(self, hash: str) -> tuple[str, str]:
        """Return (parent_hash, message) for a commit."""
        if not _valid_hash(hash):
            raise ValueError(f"Invalid hash: {hash}")
        result = self._git("log", "-1", "--format=%P%x00%s", hash)
        output = result.stdout.strip()
        parts = output.split("\x00", 1)
        parent_hash = ""
        message = ""
        if parts[0]:
            parents = parts[0].split()
            if parents:
                parent_hash = parents[0]
        if len(parts) > 1:
            message = parts[1]
        return parent_hash, message

    async def unbundle(self, bundle_bytes: bytes) -> list[str]:
        """Import a git bundle into the bare repo. Returns commit hashes."""
        self._ensure_init()
        async with self._lock:
            with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as f:
                f.write(bundle_bytes)
                bundle_path = f.name

            try:
                # List heads in the bundle
                result = self._git("bundle", "list-heads", bundle_path)
                hashes = _parse_head_hashes(result.stdout)
                if not hashes:
                    raise ValueError("Bundle contains no refs")

                # Unbundle into bare repo
                self._git("bundle", "unbundle", bundle_path)
                return hashes
            finally:
                os.unlink(bundle_path)

    def create_bundle(self, commit_hash: str) -> bytes:
        """Create a bundle containing a commit and its ancestors. Returns bundle bytes."""
        self._ensure_init()
        if not _valid_hash(commit_hash):
            raise ValueError(f"Invalid hash: {commit_hash}")

        # Create a temporary ref for bundling
        tmp_ref = f"refs/tmp/bundle-{commit_hash[:8]}"
        self._git("update-ref", tmp_ref, commit_hash)

        try:
            with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as f:
                bundle_path = f.name

            self._git("bundle", "create", bundle_path, tmp_ref)
            with open(bundle_path, "rb") as f:
                return f.read()
        finally:
            self._git("update-ref", "-d", tmp_ref, check=False)
            if os.path.exists(bundle_path):
                os.unlink(bundle_path)

    def diff(self, hash_a: str, hash_b: str) -> str:
        """Return diff between two commits."""
        if not _valid_hash(hash_a) or not _valid_hash(hash_b):
            raise ValueError("Invalid hash")
        result = self._git("diff", hash_a, hash_b)
        return result.stdout

    def show_file(self, commit_hash: str, file_path: str) -> str:
        """Return contents of a file at a specific commit."""
        if not _valid_hash(commit_hash):
            raise ValueError(f"Invalid hash: {commit_hash}")
        result = self._git("show", f"{commit_hash}:{file_path}")
        return result.stdout


def _parse_head_hashes(output: str) -> list[str]:
    """Extract commit hashes from git bundle list-heads output."""
    hashes = []
    for line in output.strip().split("\n"):
        fields = line.split()
        if fields and _valid_hash(fields[0]):
            hashes.append(fields[0])
    return hashes
