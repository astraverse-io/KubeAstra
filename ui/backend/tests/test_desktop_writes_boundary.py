"""Adversarial tests for the desktop write boundary (Phase 2, PR 1).

A write is the dangerous direction: the agent runs as the user with no OS
sandbox, and the model is assumed hostile mid-run. These tests pin every refusal
in desktop_writes.py — grant mode, containment, symlinks, deny-list, file type,
redaction round-trip, exact-edit semantics, TOCTOU, token lifecycle and the
atomic write itself — before any tool or endpoint is built on top.
"""

import os
import stat
import sys
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import desktop_folders as df  # noqa: E402
import desktop_writes as dw  # noqa: E402


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    import desktop_paths
    cfg = tmp_path / "desktop_config.json"
    monkeypatch.setattr(desktop_paths, "config_path", lambda: cfg)
    return cfg


@pytest.fixture
def repo(tmp_path, tmp_config):
    root = (tmp_path / "infra").resolve()
    (root / "overlays" / "prod").mkdir(parents=True)
    (root / "overlays" / "prod" / "api.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  replicas: 2\n"
    )
    return root


@pytest.fixture
def write_repo(repo):
    df.add_grant(str(repo), "write")
    return repo


# ── grant mode ─────────────────────────────────────────────────────────────────

class TestWriteGrant:
    def test_ungranted_path_needs_write_access(self, repo):
        with pytest.raises(df.NeedsAccess) as ei:
            dw.require_write_grant(str(repo / "overlays" / "prod" / "api.yaml"))
        assert ei.value.mode == "write"

    def test_read_grant_does_not_satisfy_write(self, repo):
        df.add_grant(str(repo), "read")
        with pytest.raises(df.NeedsAccess) as ei:
            dw.require_write_grant(str(repo / "overlays" / "prod" / "api.yaml"))
        assert ei.value.mode == "write"

    def test_write_grant_returned(self, write_repo):
        g = dw.require_write_grant(str(write_repo / "overlays" / "prod" / "api.yaml"))
        assert g["mode"] == "write"

    def test_dotdot_path_prompts_for_its_real_destination(self, write_repo, tmp_path):
        # `infra/../outside.yaml` is simply a path outside the grant. It must not
        # satisfy the grant, and the consent prompt must show where it really
        # lands — not the misleading "infra/..." spelling the model used.
        (tmp_path / "outside.yaml").write_text("a: 1\n")
        sneaky = str(write_repo / ".." / "outside.yaml")
        with pytest.raises(df.NeedsAccess) as ei:
            dw.require_write_grant(sneaky)
        assert ei.value.path == os.path.abspath(sneaky)
        assert ".." not in ei.value.path

    def test_symlink_escape_from_write_grant_is_denied_not_prompted(self, write_repo, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (write_repo / "hop").symlink_to(outside, target_is_directory=True)
        with pytest.raises(df.AccessDenied):
            dw.require_write_grant(str(write_repo / "hop" / "x.yaml"))


# ── target resolution: containment, symlinks, deny-list, type ────────────────

class TestResolveTarget:
    def test_existing_file_resolves(self, write_repo):
        g = dw.require_write_grant(str(write_repo / "overlays" / "prod" / "api.yaml"))
        resolved, exists = dw.resolve_write_target(str(write_repo / "overlays" / "prod" / "api.yaml"), g)
        assert exists is True
        assert resolved == (write_repo / "overlays" / "prod" / "api.yaml")

    def test_new_file_in_existing_dir_resolves(self, write_repo):
        target = write_repo / "overlays" / "prod" / "patch.yaml"
        g = dw.require_write_grant(str(target))
        resolved, exists = dw.resolve_write_target(str(target), g)
        assert exists is False and resolved == target

    def test_new_file_parent_missing_refused(self, write_repo):
        target = write_repo / "nope" / "deeper" / "x.yaml"
        g = dw.require_write_grant(str(target))
        with pytest.raises(dw.WriteRefused) as ei:
            dw.resolve_write_target(str(target), g)
        assert ei.value.reason == "parent_missing"

    def test_symlink_pointing_outside_refused(self, write_repo, tmp_path):
        victim = tmp_path / "zshrc.yaml"
        victim.write_text("x: 1\n")
        link = write_repo / "evil.yaml"
        link.symlink_to(victim)
        g = df.grant_for(str(write_repo), "write")
        with pytest.raises((dw.WriteRefused, df.AccessDenied)):
            dw.resolve_write_target(str(link), g)
        assert victim.read_text() == "x: 1\n"

    def test_symlink_pointing_inside_still_refused(self, write_repo):
        real = write_repo / "overlays" / "prod" / "api.yaml"
        link = write_repo / "alias.yaml"
        link.symlink_to(real)
        g = df.grant_for(str(write_repo), "write")
        with pytest.raises(dw.WriteRefused) as ei:
            dw.resolve_write_target(str(link), g)
        assert ei.value.reason == "symlink_target"

    def test_symlinked_parent_dir_escaping_refused(self, write_repo, tmp_path):
        outside_dir = tmp_path / "elsewhere"
        outside_dir.mkdir()
        (write_repo / "linkdir").symlink_to(outside_dir, target_is_directory=True)
        target = write_repo / "linkdir" / "new.yaml"
        g = df.grant_for(str(write_repo), "write")
        with pytest.raises((dw.WriteRefused, df.AccessDenied)):
            dw.resolve_write_target(str(target), g)

    @pytest.mark.parametrize("name", [".env", "prod.env", "tls.pem", "server.key", "my-secret.yaml"])
    def test_deny_listed_names_refused(self, write_repo, name):
        target = write_repo / name
        g = df.grant_for(str(write_repo), "write")
        with pytest.raises(dw.WriteRefused) as ei:
            dw.resolve_write_target(str(target), g)
        assert ei.value.reason == "deny_list"

    def test_git_hook_refused(self, write_repo):
        hooks = write_repo / ".git" / "hooks"
        hooks.mkdir(parents=True)
        g = df.grant_for(str(write_repo), "write")
        with pytest.raises(dw.WriteRefused) as ei:
            dw.resolve_write_target(str(hooks / "pre-commit.yaml"), g)
        assert ei.value.reason == "deny_list"

    @pytest.mark.parametrize("name", ["Dockerfile", "run.sh", "main.tf", "notes.md", "x.yaml.sh"])
    def test_non_yaml_refused(self, write_repo, name):
        g = df.grant_for(str(write_repo), "write")
        with pytest.raises(dw.WriteRefused) as ei:
            dw.resolve_write_target(str(write_repo / name), g)
        assert ei.value.reason == "unsupported_type"

    def test_directory_target_refused(self, write_repo):
        d = write_repo / "dir.yaml"
        d.mkdir()
        g = df.grant_for(str(write_repo), "write")
        with pytest.raises(dw.WriteRefused) as ei:
            dw.resolve_write_target(str(d), g)
        assert ei.value.reason == "not_a_file"


# ── edits: exact, unique, marker-free ─────────────────────────────────────────

class TestApplyEdits:
    BEFORE = "a: 1\nb: 2\nc: 3\n"

    def test_single_edit(self):
        assert dw.apply_edits(self.BEFORE, [{"old": "b: 2", "new": "b: 5"}]) == "a: 1\nb: 5\nc: 3\n"

    def test_sequential_edits(self):
        out = dw.apply_edits(self.BEFORE, [{"old": "a: 1", "new": "a: 9"}, {"old": "c: 3", "new": "c: 7"}])
        assert out == "a: 9\nb: 2\nc: 7\n"

    def test_not_found(self):
        with pytest.raises(dw.WriteRefused) as ei:
            dw.apply_edits(self.BEFORE, [{"old": "z: 0", "new": "z: 1"}])
        assert ei.value.reason == "edit_not_found"

    def test_ambiguous(self):
        with pytest.raises(dw.WriteRefused) as ei:
            dw.apply_edits("x: 1\nx: 1\n", [{"old": "x: 1", "new": "x: 2"}])
        assert ei.value.reason == "edit_ambiguous"

    def test_empty_old_refused(self):
        with pytest.raises(dw.WriteRefused) as ei:
            dw.apply_edits(self.BEFORE, [{"old": "", "new": "q: 1"}])
        assert ei.value.reason == "edit_not_found"

    def test_no_change_refused(self):
        with pytest.raises(dw.WriteRefused) as ei:
            dw.apply_edits(self.BEFORE, [{"old": "a: 1", "new": "a: 1"}])
        assert ei.value.reason == "no_change"

    def test_no_edits_refused(self):
        with pytest.raises(dw.WriteRefused) as ei:
            dw.apply_edits(self.BEFORE, [])
        assert ei.value.reason == "no_change"

    @pytest.mark.parametrize("marker", [
        "***redacted***", "***redacted (private key)***", "<REDACTED:aws_access_key>",
        "… [truncated, 99 bytes total]",
    ])
    def test_redaction_marker_in_new_refused(self, marker):
        with pytest.raises(dw.WriteRefused) as ei:
            dw.apply_edits(self.BEFORE, [{"old": "b: 2", "new": f"b: {marker}"}])
        assert ei.value.reason == "redaction_marker"

    def test_redaction_marker_in_old_refused(self):
        # The model copying a redacted line as its anchor would never match the
        # real bytes — refuse it explicitly with a clear reason.
        with pytest.raises(dw.WriteRefused) as ei:
            dw.apply_edits("password: hunter2\n", [{"old": "password: ***redacted***", "new": "x: 1"}])
        assert ei.value.reason == "redaction_marker"

    def test_marker_helper(self):
        assert dw.contains_redaction_marker("token: <REDACTED:github_pat>")
        assert not dw.contains_redaction_marker("replicas: 3")

    def test_untouched_secret_bytes_survive(self):
        # The whole point of D1: bytes the model didn't target keep their real value.
        before = "password: hunter2primetime\nreplicas: 2\n"
        after = dw.apply_edits(before, [{"old": "replicas: 2", "new": "replicas: 4"}])
        assert "hunter2primetime" in after and "replicas: 4" in after


# ── pending-write store ───────────────────────────────────────────────────────

def _pw(**kw):
    base = dict(root="/r", rel_path="a.yaml", abs_path="/r/a.yaml", before="a: 1\n",
                after="a: 2\n", created=False, diff="d", validation={}, session_id=None)
    base.update(kw)
    return dw.new_pending_write(**base)


class TestPendingWriteStore:
    def test_token_shape_and_sha(self):
        pw = _pw()
        assert pw.token.startswith("pwr_") and len(pw.token) > 16
        assert pw.before_sha == dw.sha256_text("a: 1\n")

    def test_new_file_has_no_before_sha(self):
        pw = _pw(before=None, created=True)
        assert pw.before_sha is None

    def test_single_use(self):
        store = dw.PendingWriteStore()
        pw = _pw()
        store.put(pw)
        assert store.get(pw.token) is pw
        assert store.pop(pw.token) is pw
        assert store.pop(pw.token) is None

    def test_expiry(self, monkeypatch):
        store = dw.PendingWriteStore()
        pw = _pw()
        store.put(pw)
        monkeypatch.setattr(dw.time, "time", lambda: pw.expires_at + 1)
        assert store.get(pw.token) is None

    def test_tokens_unique(self):
        assert _pw().token != _pw().token


# ── exact byte round-trip ─────────────────────────────────────────────────────

class TestExactRead:
    def test_crlf_preserved_through_edit_and_write(self, tmp_path):
        f = tmp_path / "win.yaml"
        f.write_bytes(b"a: 1\r\nb: 2\r\n")
        before = dw.read_text_exact(f)
        assert before == "a: 1\r\nb: 2\r\n"
        after = dw.apply_edits(before, [{"old": "b: 2", "new": "b: 3"}])
        dw.atomic_write(f, after, expected_sha=dw.sha256_text(before))
        assert f.read_bytes() == b"a: 1\r\nb: 3\r\n"

    def test_non_utf8_refused(self, tmp_path):
        f = tmp_path / "latin.yaml"
        f.write_bytes(b"name: caf\xe9\n")
        with pytest.raises(dw.WriteRefused) as ei:
            dw.read_text_exact(f)
        assert ei.value.reason == "not_utf8"


# ── atomic write ──────────────────────────────────────────────────────────────

class TestAtomicWrite:
    def test_writes_and_leaves_no_temp(self, tmp_path):
        f = tmp_path / "a.yaml"
        f.write_text("a: 1\n")
        dw.atomic_write(f, "a: 2\n", expected_sha=dw.sha256_text("a: 1\n"))
        assert f.read_text() == "a: 2\n"
        assert [p.name for p in tmp_path.iterdir()] == ["a.yaml"]

    def test_preserves_mode(self, tmp_path):
        f = tmp_path / "a.yaml"
        f.write_text("a: 1\n")
        os.chmod(f, 0o640)
        dw.atomic_write(f, "a: 2\n", expected_sha=dw.sha256_text("a: 1\n"))
        assert stat.S_IMODE(f.stat().st_mode) == 0o640

    def test_file_changed_since_preview_refused(self, tmp_path):
        f = tmp_path / "a.yaml"
        f.write_text("a: 1\n")
        sha = dw.sha256_text("a: 1\n")
        f.write_text("a: 1\n# user edited\n")
        with pytest.raises(dw.WriteRefused) as ei:
            dw.atomic_write(f, "a: 2\n", expected_sha=sha)
        assert ei.value.reason == "file_changed"
        assert f.read_text() == "a: 1\n# user edited\n"

    def test_new_file_created(self, tmp_path):
        f = tmp_path / "new.yaml"
        dw.atomic_write(f, "n: 1\n", expected_sha=None)
        assert f.read_text() == "n: 1\n"

    def test_new_file_raced_into_existence_refused(self, tmp_path):
        f = tmp_path / "new.yaml"
        f.write_text("someone else: 1\n")
        with pytest.raises(dw.WriteRefused) as ei:
            dw.atomic_write(f, "n: 1\n", expected_sha=None)
        assert ei.value.reason == "file_exists"
        assert f.read_text() == "someone else: 1\n"

    def test_oversize_refused(self, tmp_path):
        f = tmp_path / "big.yaml"
        with pytest.raises(dw.WriteRefused) as ei:
            dw.atomic_write(f, "x" * (df.MAX_FILE_BYTES + 1), expected_sha=None)
        assert ei.value.reason == "too_large"
        assert not f.exists()

    def test_symlink_swapped_in_before_apply_refused(self, tmp_path):
        # TOCTOU on the link itself: the target became a symlink after preview.
        victim = tmp_path / "victim.yaml"
        victim.write_text("a: 1\n")
        f = tmp_path / "a.yaml"
        f.symlink_to(victim)
        with pytest.raises(dw.WriteRefused) as ei:
            dw.atomic_write(f, "a: 2\n", expected_sha=dw.sha256_text("a: 1\n"))
        assert ei.value.reason == "symlink_target"
        assert victim.read_text() == "a: 1\n"
