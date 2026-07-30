"""Session -> cluster targeting must never guess.

get_runner() returns `runner_ctx.get() or kubectl`. That fallback is right for
a session that never connected, and dangerous for one that did: kubectl still
succeeds, against whatever cluster the machine points at. On a laptop that is
the operator's day-job cluster.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import cluster_session  # noqa: E402


@pytest.fixture
def rows(monkeypatch):
    store = {}
    monkeypatch.setattr(cluster_session.db, "get_cluster_connection", lambda sid: store.get(sid))
    monkeypatch.setattr(cluster_session.db, "get_ssh_target", lambda sid: None)
    return store


def conn(tmp_path, *, exists=True, name="prod"):
    path = tmp_path / f"{name}.yaml"
    if exists:
        path.write_text("apiVersion: v1\n")
    return {
        "mode": "kubeconfig", "context_name": f"{name}-ctx", "cluster_name": name,
        "server_url": "https://example:6443", "namespace": "default",
        "kubeconfig_path": str(path),
    }


def test_no_connection_returns_none(rows):
    """A session that never connected should use the local kubeconfig."""
    assert cluster_session.resolve("s1") is None


def test_no_session_id_returns_none(rows):
    assert cluster_session.resolve(None) is None


def test_live_connection_is_returned(rows, tmp_path):
    rows["s1"] = conn(tmp_path)
    assert cluster_session.resolve("s1")["context_name"] == "prod-ctx"


def test_missing_kubeconfig_raises_instead_of_falling_back(rows, tmp_path):
    """The whole point. Returning None here would silently retarget."""
    rows["s1"] = conn(tmp_path, exists=False)
    with pytest.raises(cluster_session.ClusterConnectionUnavailable) as excinfo:
        cluster_session.resolve("s1")
    assert "NOT run against a different cluster" in str(excinfo.value)


def test_stale_row_is_kept_so_it_keeps_failing_closed(rows, tmp_path):
    """Deleting the row would make the next message look like 'never
    connected' and quietly resume using local credentials."""
    rows["s1"] = conn(tmp_path, exists=False)
    for _ in range(3):
        with pytest.raises(cluster_session.ClusterConnectionUnavailable):
            cluster_session.resolve("s1")
    assert "s1" in rows


def test_db_failure_does_not_silently_retarget(rows, monkeypatch):
    def boom(_sid):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(cluster_session.db, "get_cluster_connection", boom)
    with pytest.raises(cluster_session.ClusterConnectionUnavailable):
        cluster_session.resolve("s1")


def test_connection_without_a_file_is_allowed(rows):
    """In-cluster / context-only connections carry no kubeconfig path."""
    rows["s1"] = {
        "mode": "context", "context_name": "ctx", "cluster_name": "c",
        "server_url": "", "namespace": "", "kubeconfig_path": None,
    }
    assert cluster_session.resolve("s1") is not None


def test_status_reports_stale_rather_than_plain_disconnected(rows, tmp_path):
    """'connected: false' alone would read as a bug while the backend still
    refuses to run anything. The UI needs to be able to say why."""
    rows["s1"] = conn(tmp_path, exists=False)
    status = cluster_session.status_for("s1")
    assert status["connected"] is False
    assert status["stale"] is True
    assert "reconnect" in status["reason"].lower()


def test_status_when_never_connected_is_not_stale(rows):
    assert cluster_session.status_for("s1") == {"connected": False, "stale": False}


# ── the exit-time wipe ────────────────────────────────────────────────────


def test_prune_keeps_referenced_kubeconfigs(tmp_path, monkeypatch):
    """Regression: an atexit handler deleted every kubeastra-*.yaml on exit.

    Harmless while they lived in /tmp; desktop mode points
    KUBEASTRA_KUBECONFIG_DIR at the durable app-data directory, so quitting
    destroyed every kubeconfig the operator had uploaded while leaving the
    rows that reference them.
    """
    live = tmp_path / "kubeastra-live.yaml"
    orphan = tmp_path / "kubeastra-orphan.yaml"
    for f in (live, orphan):
        f.write_text("apiVersion: v1\n")

    monkeypatch.setattr(
        cluster_session.db, "list_cluster_connections",
        lambda: [{"kubeconfig_path": str(live)}],
    )
    removed = cluster_session.prune_orphan_kubeconfigs(str(tmp_path))

    assert removed == 1
    assert live.exists(), "a referenced kubeconfig must never be pruned"
    assert not orphan.exists()


def test_prune_deletes_nothing_when_references_are_unreadable(tmp_path, monkeypatch):
    """Losing a live kubeconfig is far worse than leaving a stale file."""
    kept = tmp_path / "kubeastra-x.yaml"
    kept.write_text("apiVersion: v1\n")

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cluster_session.db, "list_cluster_connections", boom)
    assert cluster_session.prune_orphan_kubeconfigs(str(tmp_path)) == 0
    assert kept.exists()


def test_prune_ignores_unrelated_files(tmp_path, monkeypatch):
    other = tmp_path / "my-own-config.yaml"
    other.write_text("apiVersion: v1\n")
    monkeypatch.setattr(cluster_session.db, "list_cluster_connections", lambda: [])
    cluster_session.prune_orphan_kubeconfigs(str(tmp_path))
    assert other.exists(), "only kubeastra-*.yaml uploads are ours to delete"
