"""Tests for the git DAG layer."""

import os
import subprocess
import tempfile
import pytest

from swarlo.git_dag import GitDAG, _valid_hash


class TestHashValidation:
    def test_valid_hashes(self):
        assert _valid_hash("abcd1234")
        assert _valid_hash("a" * 40)
        assert _valid_hash("0123456789abcdef")

    def test_invalid_hashes(self):
        assert not _valid_hash("")
        assert not _valid_hash("xyz")
        assert not _valid_hash("ABCD")  # uppercase
        assert not _valid_hash("ab cd")  # space


class TestGitDAG:
    @pytest.fixture
    def dag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = os.path.join(tmpdir, "test.git")
            d = GitDAG(repo_path)
            d.init()
            yield d, tmpdir

    @pytest.fixture
    def source_repo(self):
        """Create a normal git repo with commits we can bundle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", tmpdir], capture_output=True, check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@test.com"], capture_output=True, check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Test"], capture_output=True, check=True)

            # First commit
            with open(os.path.join(tmpdir, "file.txt"), "w") as f:
                f.write("hello")
            subprocess.run(["git", "-C", tmpdir, "add", "."], capture_output=True, check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-m", "first commit"], capture_output=True, check=True)

            hash1 = subprocess.run(
                ["git", "-C", tmpdir, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            # Second commit
            with open(os.path.join(tmpdir, "file.txt"), "w") as f:
                f.write("world")
            subprocess.run(["git", "-C", tmpdir, "add", "."], capture_output=True, check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-m", "second commit"], capture_output=True, check=True)

            hash2 = subprocess.run(
                ["git", "-C", tmpdir, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            yield tmpdir, hash1, hash2

    def _create_bundle(self, repo_path: str) -> bytes:
        """Create a bundle from a normal repo."""
        bundle_path = os.path.join(repo_path, "test.bundle")
        # Get the default branch name
        branch = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", repo_path, "bundle", "create", bundle_path, branch],
            capture_output=True, check=True,
        )
        with open(bundle_path, "rb") as f:
            return f.read()

    def test_init_creates_bare_repo(self, dag):
        d, tmpdir = dag
        assert (d.path / "HEAD").exists()

    @pytest.mark.asyncio
    async def test_unbundle_and_commit_exists(self, dag, source_repo):
        d, tmpdir = dag
        repo_path, hash1, hash2 = source_repo

        bundle = self._create_bundle(repo_path)
        hashes = await d.unbundle(bundle)

        assert len(hashes) >= 1
        assert d.commit_exists(hash2)
        assert d.commit_exists(hash1)

    @pytest.mark.asyncio
    async def test_get_commit_info(self, dag, source_repo):
        d, tmpdir = dag
        repo_path, hash1, hash2 = source_repo

        bundle = self._create_bundle(repo_path)
        await d.unbundle(bundle)

        parent, message = d.get_commit_info(hash2)
        assert parent == hash1
        assert message == "second commit"

    @pytest.mark.asyncio
    async def test_create_bundle_roundtrip(self, dag, source_repo):
        d, tmpdir = dag
        repo_path, hash1, hash2 = source_repo

        # Push commits in
        bundle_in = self._create_bundle(repo_path)
        await d.unbundle(bundle_in)

        # Pull a commit out
        bundle_out = d.create_bundle(hash2)
        assert len(bundle_out) > 0
        assert isinstance(bundle_out, bytes)

    @pytest.mark.asyncio
    async def test_diff(self, dag, source_repo):
        d, tmpdir = dag
        repo_path, hash1, hash2 = source_repo

        bundle = self._create_bundle(repo_path)
        await d.unbundle(bundle)

        diff_text = d.diff(hash1, hash2)
        assert "hello" in diff_text or "world" in diff_text

    def test_commit_exists_false_for_missing(self, dag):
        d, tmpdir = dag
        assert not d.commit_exists("0000000000000000000000000000000000000000")

    def test_invalid_hash_rejected(self, dag):
        d, tmpdir = dag
        with pytest.raises(ValueError):
            d.get_commit_info("not-a-hash")


class TestSQLiteDAGQueries:
    @pytest.fixture
    def backend(self):
        import tempfile
        from swarlo.sqlite_backend import SQLiteBackend
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        b = SQLiteBackend(path)
        yield b
        b.close()
        os.unlink(path)

    def test_index_and_get_commit(self, backend):
        backend.index_commit("hub-1", "aaa111", None, "agent-1", "Hugo", "first")
        commit = backend.get_commit("hub-1", "aaa111")
        assert commit is not None
        assert commit["member_name"] == "Hugo"

    def test_children(self, backend):
        backend.index_commit("hub-1", "aaa111", None, "agent-1", "Hugo", "root")
        backend.index_commit("hub-1", "bbb222", "aaa111", "agent-2", "Gideon", "child")
        children = backend.get_children("hub-1", "aaa111")
        assert len(children) == 1
        assert children[0]["hash"] == "bbb222"

    def test_leaves(self, backend):
        backend.index_commit("hub-1", "aaa111", None, "agent-1", "Hugo", "root")
        backend.index_commit("hub-1", "bbb222", "aaa111", "agent-2", "Gideon", "child")
        backend.index_commit("hub-1", "ccc333", "aaa111", "agent-1", "Hugo", "branch")

        leaves = backend.get_leaves("hub-1")
        leaf_hashes = {l["hash"] for l in leaves}
        assert "bbb222" in leaf_hashes
        assert "ccc333" in leaf_hashes
        assert "aaa111" not in leaf_hashes  # has children

    def test_lineage(self, backend):
        backend.index_commit("hub-1", "aaa111", None, "agent-1", "Hugo", "root")
        backend.index_commit("hub-1", "bbb222", "aaa111", "agent-2", "Gideon", "middle")
        backend.index_commit("hub-1", "ccc333", "bbb222", "agent-1", "Hugo", "tip")

        lineage = backend.get_lineage("hub-1", "ccc333")
        assert len(lineage) == 3
        assert lineage[0]["hash"] == "ccc333"
        assert lineage[1]["hash"] == "bbb222"
        assert lineage[2]["hash"] == "aaa111"

    def test_hub_isolation(self, backend):
        backend.index_commit("hub-1", "aaa111", None, "agent-1", "Hugo", "hub1")
        backend.index_commit("hub-2", "bbb222", None, "agent-2", "Gideon", "hub2")

        assert backend.get_commit("hub-1", "aaa111") is not None
        assert backend.get_commit("hub-1", "bbb222") is None
        assert backend.get_commit("hub-2", "bbb222") is not None
