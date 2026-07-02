"""Unit tests for GitRepoSource — URL rewriting and parsing.

Cloning is not tested here (would need network); the rewriting logic is
the part most likely to regress and the part the plan §11.1 specifically
called out as fixing a real bug.
"""
from __future__ import annotations

from services.rag.sources.git_repo import (
    GitRepoSource,
    _looks_safe,
    _parse_github_owner_repo,
)


def test_parse_github_https_with_dot_git():
    assert _parse_github_owner_repo(
        "https://github.com/kubeastra/deployment-provisioning.git"
    ) == ("kubeastra", "deployment-provisioning")


def test_parse_github_https_without_dot_git():
    assert _parse_github_owner_repo(
        "https://github.com/kubeastra/deployment-provisioning"
    ) == ("kubeastra", "deployment-provisioning")


def test_parse_github_https_trailing_slash():
    assert _parse_github_owner_repo(
        "https://github.com/kubeastra/deployment-provisioning/"
    ) == ("kubeastra", "deployment-provisioning")


def test_parse_github_ssh():
    assert _parse_github_owner_repo(
        "git@github.com:kubeastra/deployment-provisioning.git"
    ) == ("kubeastra", "deployment-provisioning")


def test_parse_non_github_returns_none():
    assert _parse_github_owner_repo("https://gitea.internal/group/repo.git") is None
    assert _parse_github_owner_repo("https://gitlab.com/group/repo.git") is None


def test_blob_base_for_github_repo():
    src = GitRepoSource("https://github.com/foo/bar.git", branch="main")
    assert src._blob_base() == "https://github.com/foo/bar/blob/main"


def test_blob_base_none_for_non_github():
    src = GitRepoSource("https://gitea.internal/group/repo.git", branch="main")
    assert src._blob_base() is None


def test_looks_safe_accepts_https_and_ssh():
    assert _looks_safe("https://github.com/foo/bar.git")
    assert _looks_safe("git@github.com:foo/bar.git")
    assert _looks_safe("ssh://git@host:2222/foo/bar.git")


def test_looks_safe_rejects_shell_metachars():
    assert not _looks_safe("https://github.com/foo/bar.git; rm -rf /")
    assert not _looks_safe("https://github.com/foo/bar.git`whoami`")
    assert not _looks_safe("https://github.com/foo/bar.git$(echo evil)")


def test_unsafe_url_raises_at_construction():
    import pytest
    with pytest.raises(ValueError):
        GitRepoSource("https://github.com/foo/bar.git; rm -rf /")
