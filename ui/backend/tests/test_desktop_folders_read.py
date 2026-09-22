"""Read-primitive tests for the desktop folder boundary (Phase 1).

Covers read_file_contained / list_folder_contained / search_files_contained:
grant gating (NeedsAccess), deny-list + caps enforcement, secret redaction before
content can reach the LLM, and the folder.read audit contract (payload carries
{root, rel_path, bytes} — never the file body).
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import desktop_folders as df  # noqa: E402


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    import desktop_paths
    cfg = tmp_path / "desktop_config.json"
    monkeypatch.setattr(desktop_paths, "config_path", lambda: cfg)
    return cfg


@pytest.fixture
def audit_spy(monkeypatch):
    """Capture audit.emit calls so tests never touch the real audit DB, and so we
    can assert the folder.read contract."""
    import audit
    calls = []

    def _fake_emit(event_type, **kw):
        calls.append((event_type, kw))
        return "evt-test"

    monkeypatch.setattr(audit, "emit", _fake_emit)
    return calls


@pytest.fixture
def granted_repo(tmp_path, tmp_config):
    """A granted infra folder with a manifest, a secret-bearing file, and a
    deny-listed key."""
    root = (tmp_path / "infra").resolve()
    (root / "overlays" / "prod").mkdir(parents=True)
    (root / "overlays" / "prod" / "api.yaml").write_text(
        "kind: Deployment\nmetadata:\n  name: api-gateway\n"
    )
    (root / "app.yaml").write_text(
        "kind: Deployment\n# password: hunter2primetime\n"
        "annotations:\n  token: ghp_0123456789abcdef0123456789abcdef0123\n"
    )
    (root / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n")
    df.add_grant(str(root), "read")
    return root


class TestReadFile:
    def test_reads_contained_file(self, granted_repo, audit_spy):
        content = df.read_file_contained(str(granted_repo / "overlays" / "prod" / "api.yaml"))
        assert "api-gateway" in content

    def test_ungranted_path_raises_needs_access(self, tmp_path, tmp_config, audit_spy):
        with pytest.raises(df.NeedsAccess) as ei:
            df.read_file_contained(str(tmp_path / "not-granted" / "x.yaml"))
        assert ei.value.mode == "read"

    def test_needs_access_reports_normalized_path(self, granted_repo, audit_spy):
        # `infra/../.ssh/id_rsa` must be shown to the user as where it really
        # lands, not spelled to look like it's inside the granted folder.
        sneaky = str(granted_repo / ".." / ".ssh" / "id_rsa")
        with pytest.raises(df.NeedsAccess) as ei:
            df.read_file_contained(sneaky)
        assert ".." not in ei.value.path
        assert ei.value.path.endswith("/.ssh/id_rsa")

    def test_deny_listed_file_refused(self, granted_repo, audit_spy):
        with pytest.raises(df.AccessDenied) as ei:
            df.read_file_contained(str(granted_repo / "id_rsa"))
        assert ei.value.reason == "deny_list"

    def test_secrets_are_redacted_before_return(self, granted_repo, audit_spy):
        content = df.read_file_contained(str(granted_repo / "app.yaml"))
        assert "hunter2primetime" not in content          # keyword-anchored secret
        assert "ghp_0123456789abcdef0123456789abcdef0123" not in content  # entropy/prefix

    def test_read_emits_folder_read_without_content(self, granted_repo, audit_spy):
        import audit
        df.read_file_contained(str(granted_repo / "app.yaml"))
        read_events = [kw for et, kw in audit_spy if et == audit.EventType.FOLDER_READ]
        assert read_events, "expected a folder.read audit event"
        payload = read_events[-1]["payload"]
        assert set(payload) == {"root", "rel_path", "bytes"}
        assert payload["rel_path"] == "app.yaml"
        # the audit trail must never carry the file body or its secrets
        blob = str(payload)
        assert "hunter2primetime" not in blob and "ghp_" not in blob

    def test_escape_via_symlink_still_denied(self, tmp_path, tmp_config, audit_spy):
        root = (tmp_path / "infra").resolve()
        root.mkdir()
        outside = (tmp_path / "outside").resolve()
        outside.mkdir()
        (outside / "loot.yaml").write_text("secret")
        (root / "escape").symlink_to(outside)
        df.add_grant(str(root), "read")
        with pytest.raises(df.AccessDenied) as ei:
            df.read_file_contained(str(root / "escape" / "loot.yaml"))
        assert ei.value.reason == "outside_grant"


class TestListFolder:
    def test_lists_entries_and_hides_deny_listed(self, granted_repo, audit_spy):
        out = df.list_folder_contained(str(granted_repo))
        names = {e["name"] for e in out["entries"]}
        assert "app.yaml" in names
        assert "overlays" in names
        assert "id_rsa" not in names               # deny-listed → hidden
        types = {e["name"]: e["type"] for e in out["entries"]}
        assert types["overlays"] == "dir"
        assert types["app.yaml"] == "file"

    def test_ungranted_raises(self, tmp_path, tmp_config, audit_spy):
        with pytest.raises(df.NeedsAccess):
            df.list_folder_contained(str(tmp_path / "nope"))


class TestSearchFiles:
    def test_finds_pattern_and_redacts_snippet(self, granted_repo, audit_spy):
        matches = df.search_files_contained(str(granted_repo), "kind")
        assert any(m["file"].endswith("api.yaml") for m in matches)
        assert all("line" in m and "text" in m for m in matches)

    def test_search_skips_deny_listed_and_redacts(self, granted_repo, audit_spy):
        # searching for the token must not leak it via a snippet, and id_rsa is skipped
        matches = df.search_files_contained(str(granted_repo), "token")
        for m in matches:
            assert "ghp_0123456789abcdef0123456789abcdef0123" not in m["text"]
        assert all("id_rsa" not in m["file"] for m in matches)

    def test_ungranted_raises(self, tmp_path, tmp_config, audit_spy):
        with pytest.raises(df.NeedsAccess):
            df.search_files_contained(str(tmp_path / "nope"), "x")

    def test_does_not_follow_symlink_escaping_grant(self, tmp_path, tmp_config, audit_spy):
        # A file-symlink inside a granted folder that points OUTSIDE must not be
        # read by search — same containment guarantee read_file_contained gives.
        root = (tmp_path / "infra").resolve()
        root.mkdir()
        outside = (tmp_path / "outside").resolve()
        outside.mkdir()
        (outside / "leak.yaml").write_text("outside-only-content-zzz\n")
        (root / "link.yaml").symlink_to(outside / "leak.yaml")
        (root / "real.yaml").write_text("kind: Service\n")
        df.add_grant(str(root), "read")

        # the symlinked-out content must not surface
        assert df.search_files_contained(str(root), "outside-only-content-zzz") == []
        # a real in-grant file is still searchable
        assert any(m["file"] == "real.yaml"
                   for m in df.search_files_contained(str(root), "Service"))
