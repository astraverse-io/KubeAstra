"""Adversarial tests for the desktop folder-access boundary (Phase 1, PR 1).

The boundary is the entire security story for local-folder access (there is no OS
sandbox — the agent runs as the user). These tests assume the model may be hostile
and try to escape a granted root. They are written FIRST (TDD) and must all pass
before any tool or endpoint that uses the boundary is allowed to land.

See internal_docs/features/DESKTOP_AGENT_PHASE1_SPEC.md §3 (PR 1) and
DESKTOP_AGENT_DESIGN.md §3.2 (the threat model).
"""

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import desktop_folders as df  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _fs_is_case_insensitive(base: Path) -> bool:
    probe = base / "CaseProbe"
    probe.mkdir()
    return (base / "caseprobe").exists()


def _fs_normalizes_unicode(base: Path) -> bool:
    """True if the FS treats NFC and NFD spellings of a name as the same file."""
    import unicodedata
    nfc = base / unicodedata.normalize("NFC", "café_probe")
    nfc.mkdir()
    nfd = base / unicodedata.normalize("NFD", "café_probe")
    return nfd.exists()


# ── containment: the single most important function ───────────────────────────

class TestContainPath:
    def test_rejects_parent_traversal(self, tmp_path):
        root = (tmp_path / "grant").resolve()
        root.mkdir()
        with pytest.raises(df.AccessDenied) as ei:
            df.contain_path(str(root / ".." / ".." / "etc" / "passwd"), root)
        assert ei.value.reason == "outside_grant"

    def test_rejects_absolute_path_outside_grant(self, tmp_path):
        root = (tmp_path / "grant").resolve()
        root.mkdir()
        with pytest.raises(df.AccessDenied):
            df.contain_path("/etc/hosts", root)

    def test_allows_file_inside_grant(self, tmp_path):
        root = (tmp_path / "grant").resolve()
        (root / "sub").mkdir(parents=True)
        target = root / "sub" / "api.yaml"
        target.write_text("kind: Deployment\n")
        resolved = df.contain_path(str(target), root)
        assert resolved == target.resolve()

    def test_rejects_symlink_escaping_grant(self, tmp_path):
        """A symlink INSIDE the grant that points OUTSIDE must resolve out and be denied."""
        root = (tmp_path / "grant").resolve()
        root.mkdir()
        secret_dir = (tmp_path / "outside").resolve()
        secret_dir.mkdir()
        (secret_dir / "id_rsa").write_text("PRIVATE KEY")
        link = root / "escape"
        link.symlink_to(secret_dir)  # grant/escape -> ../outside
        with pytest.raises(df.AccessDenied) as ei:
            df.contain_path(str(link / "id_rsa"), root)
        assert ei.value.reason == "outside_grant"

    def test_allows_symlink_pointing_inside_grant(self, tmp_path):
        root = (tmp_path / "grant").resolve()
        (root / "real").mkdir(parents=True)
        (root / "real" / "app.yaml").write_text("kind: Service\n")
        link = root / "alias"
        link.symlink_to(root / "real")
        resolved = df.contain_path(str(link / "app.yaml"), root)
        assert resolved.is_relative_to(root)

    def test_case_fold_does_not_escape(self, tmp_path):
        """On a case-insensitive FS (macOS default), a differently-cased path to a file
        inside the grant stays contained — it must not be treated as an escape."""
        root = (tmp_path / "Grant").resolve()
        (root / "Sub").mkdir(parents=True)
        (root / "Sub" / "api.yaml").write_text("kind: Deployment\n")
        if not _fs_is_case_insensitive(tmp_path):
            pytest.skip("case-sensitive FS; case-fold semantics not applicable")
        # contain_path must NOT reject a differently-cased path to a file that is
        # genuinely inside the grant. (is_relative_to is case-sensitive, so we
        # assert the file resolves to something real rather than string-prefix.)
        resolved = df.contain_path(str(tmp_path / "grant" / "sub" / "api.yaml"), root)
        assert resolved.exists()
        assert df._contained(resolved, root)

    def test_unicode_normalization_does_not_escape(self, tmp_path):
        import unicodedata
        if not _fs_normalizes_unicode(tmp_path):
            pytest.skip("FS does not normalize unicode; not applicable")
        root = (tmp_path / "grant").resolve()
        root.mkdir()
        name_nfc = unicodedata.normalize("NFC", "naïve.yaml")
        (root / name_nfc).write_text("kind: ConfigMap\n")
        name_nfd = unicodedata.normalize("NFD", "naïve.yaml")
        resolved = df.contain_path(str(root / name_nfd), root)
        assert resolved.is_relative_to(root)


# ── deny-list (inside a legitimately-granted root) ─────────────────────────────

class TestDenyList:
    @pytest.mark.parametrize("name", [
        "id_rsa", "id_ed25519", "server.pem", "tls.key",
        ".env", ".env.local", "app-secret.yaml", "mysecret.txt",
    ])
    def test_denies_secretish_files(self, tmp_path, name):
        root = (tmp_path / "grant").resolve()
        root.mkdir()
        f = root / name
        f.write_text("x")
        assert df.is_denied(f.resolve(), root) is True

    @pytest.mark.parametrize("rel", [".git/config", ".ssh/known_hosts", ".aws/credentials", ".kube/config"])
    def test_denies_sensitive_dir_segments(self, tmp_path, rel):
        root = (tmp_path / "grant").resolve()
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        assert df.is_denied(p.resolve(), root) is True

    @pytest.mark.parametrize("name", ["api.yaml", "values.yaml", "kustomization.yaml", "README.md"])
    def test_allows_normal_manifests(self, tmp_path, name):
        root = (tmp_path / "grant").resolve()
        root.mkdir()
        f = root / name
        f.write_text("kind: Deployment\n")
        assert df.is_denied(f.resolve(), root) is False


# ── size / binary caps ─────────────────────────────────────────────────────────

class TestCaps:
    def test_rejects_oversized_file(self, tmp_path):
        f = tmp_path / "big.yaml"
        f.write_bytes(b"a" * (df.MAX_FILE_BYTES + 1))
        with pytest.raises(df.AccessDenied) as ei:
            df.within_caps(f.resolve())
        assert ei.value.reason == "too_large"

    def test_rejects_binary_file(self, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"MZ\x00\x00\x90text")
        with pytest.raises(df.AccessDenied) as ei:
            df.within_caps(f.resolve())
        assert ei.value.reason == "binary"

    def test_allows_small_text_file(self, tmp_path):
        f = tmp_path / "ok.yaml"
        f.write_text("kind: Service\n")
        df.within_caps(f.resolve())  # must not raise


# ── grant registry (persisted via desktop_config → tmp) ────────────────────────

@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Point desktop_config's storage at a tmp file so tests never touch the real config."""
    import desktop_paths
    cfg = tmp_path / "desktop_config.json"
    monkeypatch.setattr(desktop_paths, "config_path", lambda: cfg)
    return cfg


class TestGrantRegistry:
    def test_add_then_list(self, tmp_path, tmp_config):
        root = (tmp_path / "infra").resolve()
        root.mkdir()
        g = df.add_grant(str(root), "read")
        assert g["id"].startswith("grt_")
        assert g["mode"] == "read"
        assert Path(g["root"]) == root  # stored resolved
        assert g["granted_at"] and g["last_used"]
        assert any(x["id"] == g["id"] for x in df.list_grants())

    def test_grant_for_matches_contained_path(self, tmp_path, tmp_config):
        root = (tmp_path / "infra").resolve()
        (root / "prod").mkdir(parents=True)
        df.add_grant(str(root), "read")
        assert df.grant_for(str(root / "prod" / "api.yaml"), "read") is not None
        assert df.grant_for(str(tmp_path / "elsewhere" / "x.yaml"), "read") is None

    def test_read_grant_does_not_satisfy_write(self, tmp_path, tmp_config):
        root = (tmp_path / "infra").resolve()
        root.mkdir()
        df.add_grant(str(root), "read")
        assert df.grant_for(str(root / "api.yaml"), "write") is None

    def test_write_grant_satisfies_read(self, tmp_path, tmp_config):
        root = (tmp_path / "infra").resolve()
        root.mkdir()
        df.add_grant(str(root), "write")
        assert df.grant_for(str(root / "api.yaml"), "read") is not None

    def test_revoke(self, tmp_path, tmp_config):
        root = (tmp_path / "infra").resolve()
        root.mkdir()
        g = df.add_grant(str(root), "read")
        assert df.revoke_grant(g["id"]) is True
        assert df.list_grants() == []
        assert df.revoke_grant("grt_missing") is False

    def test_add_is_idempotent_for_nested_same_mode(self, tmp_path, tmp_config):
        root = (tmp_path / "infra").resolve()
        (root / "prod").mkdir(parents=True)
        g1 = df.add_grant(str(root), "read")
        g2 = df.add_grant(str(root / "prod"), "read")  # inside an existing read grant
        assert g1["id"] == g2["id"]
        assert len(df.list_grants()) == 1
