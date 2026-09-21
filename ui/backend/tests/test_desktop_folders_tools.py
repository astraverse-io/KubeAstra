"""Tool-layer tests for the desktop folder boundary (Phase 1, PR 3).

Exercises the four tool handlers directly (needs_access / success / denied
contracts and the find_source_for_workload bridge), plus desktop-only
registration into the shared tool registry.
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
    import audit
    monkeypatch.setattr(audit, "emit", lambda event_type, **kw: "evt-test")


@pytest.fixture
def granted_repo(tmp_path, tmp_config):
    root = (tmp_path / "infra").resolve()
    (root / "overlays" / "prod").mkdir(parents=True)
    (root / "overlays" / "prod" / "api.yaml").write_text(
        "kind: Deployment\nmetadata:\n  name: api-gateway\n  namespace: prod\n"
    )
    (root / "app.yaml").write_text("kind: Service\nmetadata:\n  name: web\n")
    (root / "id_rsa").write_text("PRIVATE KEY")
    df.add_grant(str(root), "read")
    return root


class TestReadFileHandler:
    def test_needs_access_when_ungranted(self, tmp_path, tmp_config, audit_spy):
        out = df._handle_read_file({"path": str(tmp_path / "nope" / "x.yaml"), "reason": "inspect"})
        assert "needs_access" in out
        assert out["needs_access"]["mode"] == "read"
        assert out["needs_access"]["reason"] == "inspect"

    def test_success(self, granted_repo, audit_spy):
        out = df._handle_read_file({"path": str(granted_repo / "app.yaml")})
        assert out["success"] is True
        assert "web" in out["content"]

    def test_denied_deny_list(self, granted_repo, audit_spy):
        out = df._handle_read_file({"path": str(granted_repo / "id_rsa")})
        assert out["success"] is False
        assert "deny_list" in out["error"]


class TestListAndSearchHandlers:
    def test_list_hides_deny_listed(self, granted_repo, audit_spy):
        out = df._handle_list_folder({"path": str(granted_repo)})
        assert out["success"] is True
        names = {e["name"] for e in out["entries"]}
        assert "app.yaml" in names and "overlays" in names
        assert "id_rsa" not in names

    def test_search_returns_matches(self, granted_repo, audit_spy):
        out = df._handle_search_files({"root": str(granted_repo), "pattern": "kind"})
        assert out["success"] is True
        assert any(m["file"].endswith("api.yaml") for m in out["matches"])


class TestFindSourceForWorkload:
    def test_found_single(self, granted_repo, audit_spy):
        out = df._handle_find_source_for_workload({"kind": "Deployment", "name": "api-gateway"})
        assert out["success"] is True
        assert len(out["candidates"]) == 1
        assert out["candidates"][0]["file"].endswith("api.yaml")
        assert out["candidates"][0]["namespace"] == "prod"

    def test_ambiguous_returns_list(self, granted_repo, audit_spy):
        # a second file also defines Deployment/api-gateway → candidate LIST, no guess
        (granted_repo / "overlays" / "staging").mkdir(parents=True)
        (granted_repo / "overlays" / "staging" / "api.yaml").write_text(
            "kind: Deployment\nmetadata:\n  name: api-gateway\n  namespace: staging\n"
        )
        out = df._handle_find_source_for_workload({"kind": "Deployment", "name": "api-gateway"})
        assert len(out["candidates"]) == 2

    def test_unknown_workload_empty(self, granted_repo, audit_spy):
        out = df._handle_find_source_for_workload({"kind": "Deployment", "name": "does-not-exist"})
        assert out["success"] is True
        assert out["candidates"] == []

    def test_needs_access_without_grant(self, tmp_path, tmp_config, audit_spy):
        out = df._handle_find_source_for_workload({"kind": "Deployment", "name": "api-gateway"})
        assert "needs_access" in out
        assert out["needs_access"]["mode"] == "read"

    def test_namespace_filter(self, granted_repo, audit_spy):
        out = df._handle_find_source_for_workload(
            {"kind": "Deployment", "name": "api-gateway", "namespace": "staging"})
        assert out["success"] is True
        assert out["candidates"] == []  # only prod defines it

    def test_ignores_symlink_escaping_grant(self, tmp_path, tmp_config, audit_spy):
        # A yaml-named symlink inside the grant pointing OUTSIDE must not be
        # indexed — the bridge must honour the same containment as read_file.
        root = (tmp_path / "infra").resolve()
        root.mkdir()
        outside = (tmp_path / "outside").resolve()
        outside.mkdir()
        (outside / "sneaky.yaml").write_text("kind: Deployment\nmetadata:\n  name: exfil\n")
        (root / "sneaky.yaml").symlink_to(outside / "sneaky.yaml")
        df.add_grant(str(root), "read")

        out = df._handle_find_source_for_workload({"kind": "Deployment", "name": "exfil"})
        assert out["success"] is True
        assert out["candidates"] == []


class TestDesktopToolRegistration:
    def test_registers_four_desktop_tools(self):
        mcp_dir = BACKEND_DIR.parent.parent / "mcp"
        if str(mcp_dir) not in sys.path:
            sys.path.insert(0, str(mcp_dir))
        import tool_registry as tr

        names = ("read_file", "list_folder", "search_files", "find_source_for_workload")
        saved = {n: tr.TOOLS.get(n) for n in names}
        try:
            df.register_desktop_tools()
            for n in names:
                assert n in tr.TOOLS, f"{n} not registered"
                assert tr.TOOLS[n].surfaces == frozenset({"react", "chat"})
                assert tr.TOOLS[n].write_op is False
                assert tr.resolve_tool(n) is not None
        finally:
            for n, tool in saved.items():
                if tool is None:
                    tr.TOOLS.pop(n, None)
                else:
                    tr.TOOLS[n] = tool
