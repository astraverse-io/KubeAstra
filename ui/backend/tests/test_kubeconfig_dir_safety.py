"""The directory pasted kubeconfigs are written into must be ours alone.

Uploaded kubeconfigs are cluster credentials. They were written to
``/tmp/kubeastra-kubeconfigs`` — a predictable path in a world-writable
directory — created with ``mkdir(exist_ok=True)`` and then ``chmod(0o700)``
inside ``except OSError: pass``.

Every step of that fails open on a shared host. A local user creates the path
first, pointing at a directory they own; ``exist_ok=True`` accepts it; the
``chmod`` fails because the directory is not ours and the bare ``except``
throws the evidence away. The guard in ``_write_temp_kubeconfig`` —
``path.resolve().parent != _TEMP_DIR.resolve()`` — does not catch it either,
because both sides resolve *through* the same symlink and compare equal.

The first test below is the proof-of-concept that demonstrated it, kept as a
regression test rather than thrown away: it is the only thing here that would
have passed against the old code.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for p in (str(BACKEND_DIR), str(MCP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

pytestmark = pytest.mark.skipif(
    not hasattr(os, "geteuid"), reason="POSIX ownership semantics only"
)


def test_a_pre_created_symlink_is_refused(tmp_path):
    """The original attack. This is what put credentials in an attacker's directory."""
    from routers.cluster import UnsafeKubeconfigDir, _ensure_private_dir

    attacker_owned = tmp_path / "attacker-collects-here"
    attacker_owned.mkdir()
    victim = tmp_path / "kubeastra-kubeconfigs"
    victim.symlink_to(attacker_owned)

    with pytest.raises(UnsafeKubeconfigDir, match="not a directory"):
        _ensure_private_dir(victim)


def test_a_symlink_to_a_file_is_refused(tmp_path):
    from routers.cluster import UnsafeKubeconfigDir, _ensure_private_dir

    decoy = tmp_path / "decoy"
    decoy.write_text("", encoding="utf-8")
    victim = tmp_path / "dir"
    victim.symlink_to(decoy)

    with pytest.raises(UnsafeKubeconfigDir):
        _ensure_private_dir(victim)


def test_a_plain_file_in_the_way_is_refused(tmp_path):
    from routers.cluster import UnsafeKubeconfigDir, _ensure_private_dir

    victim = tmp_path / "dir"
    victim.write_text("", encoding="utf-8")

    with pytest.raises(UnsafeKubeconfigDir):
        _ensure_private_dir(victim)


def test_a_fresh_directory_is_created_private(tmp_path):
    from routers.cluster import _ensure_private_dir

    target = tmp_path / "fresh"
    _ensure_private_dir(target)

    assert target.is_dir()
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o700


def test_our_own_loose_directory_is_tightened_not_rejected(tmp_path):
    """The upgrade path. A 0755 directory from the old code is ours — fix it."""
    from routers.cluster import _ensure_private_dir

    target = tmp_path / "loose"
    target.mkdir(mode=0o755)

    _ensure_private_dir(target)

    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o700


def test_a_directory_we_already_own_is_reused(tmp_path):
    from routers.cluster import _ensure_private_dir

    target = tmp_path / "ours"
    target.mkdir(mode=0o700)
    (target / "marker").write_text("keep me", encoding="utf-8")

    _ensure_private_dir(target)

    assert (target / "marker").read_text(encoding="utf-8") == "keep me"


def test_the_default_path_is_per_user(monkeypatch):
    """Two accounts on one host must not contend for a single directory.

    Without the uid, whoever starts first owns the path and every other user
    fails the ownership check — trading a vulnerability for a denial of
    service.
    """
    from routers.cluster import _kubeconfig_dir_path

    monkeypatch.delenv("KUBEASTRA_KUBECONFIG_DIR", raising=False)
    assert str(os.geteuid()) in _kubeconfig_dir_path().name


def test_an_explicit_directory_overrides_the_default(monkeypatch, tmp_path):
    from routers.cluster import _kubeconfig_dir_path

    monkeypatch.setenv("KUBEASTRA_KUBECONFIG_DIR", str(tmp_path / "chosen"))
    assert _kubeconfig_dir_path() == tmp_path / "chosen"


def test_the_kubeconfig_is_private_at_the_instant_it_is_created(tmp_path, monkeypatch):
    """The window, not the end state.

    Asserting the *final* mode is 0600 proves nothing: ``write_text`` followed
    by ``chmod`` ends at 0600 too. It just spends the moment in between at the
    process umask, which on a 022 default is world-readable — and the file it
    is exposing is a cluster credential.

    So this observes the mode through the descriptor the moment the file comes
    into existence. ``write_text`` goes through the C-level open and never
    touches ``os.open``, so on the old implementation nothing is recorded here
    and the assertion says so.
    """
    from routers import cluster

    monkeypatch.setattr(cluster, "_TEMP_DIR", tmp_path)
    observed: dict[str, object] = {}
    real_open = os.open

    # `os.open` itself defaults this to 0o777. Mirroring that here would put a
    # world-writable literal in a test whose whole subject is file modes — and
    # CodeQL flagged it, correctly. The real call always passes a mode
    # explicitly, so the default is never used; 0600 keeps it that way if a
    # future caller forgets.
    def watched(path, flags, mode=0o600, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        if str(path).endswith("kubeastra-abc123.yaml"):
            observed["mode"] = stat.S_IMODE(os.fstat(fd).st_mode)
            observed["nofollow"] = bool(flags & os.O_NOFOLLOW)
        return fd

    monkeypatch.setattr(os, "open", watched)

    saved = os.umask(0o000)  # worst case: nothing masked off
    try:
        written = Path(cluster._write_temp_kubeconfig("abc123", "apiVersion: v1\n"))
    finally:
        os.umask(saved)

    assert observed, (
        "the kubeconfig was not created through os.open with an explicit mode, "
        "so it existed at the process umask before any chmod narrowed it"
    )
    assert observed["mode"] == 0o600, f"created as {observed['mode']:o}, not 0600"
    assert observed["nofollow"], "O_NOFOLLOW missing — a symlink here is writable through"
    assert written.read_text(encoding="utf-8") == "apiVersion: v1\n"


def test_writing_through_a_symlink_is_refused(tmp_path, monkeypatch):
    """O_NOFOLLOW. Even inside a trusted directory, the file must be a file."""
    from routers import cluster

    monkeypatch.setattr(cluster, "_TEMP_DIR", tmp_path)
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text("original\n", encoding="utf-8")
    (tmp_path / "kubeastra-abc123.yaml").symlink_to(elsewhere)

    with pytest.raises(OSError):
        cluster._write_temp_kubeconfig("abc123", "apiVersion: v1\n")

    assert elsewhere.read_text(encoding="utf-8") == "original\n"


def test_no_bare_except_swallows_a_permission_failure():
    """The guard. The bug was not the mkdir — it was discarding the error."""
    source = (BACKEND_DIR / "routers" / "cluster.py").read_text(encoding="utf-8")
    marker = "def _ensure_private_dir"
    body = source[source.index(marker):]
    body = body[: body.index("\n_TEMP_DIR")]

    assert "except OSError:\n        pass" not in body, (
        "a swallowed OSError here is exactly how the original hole stayed "
        "invisible — raise UnsafeKubeconfigDir instead"
    )
