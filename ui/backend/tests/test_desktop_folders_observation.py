"""What the model actually sees from local-folder tools (Phase 1 fix).

Through the react surface, every tool result was wrapped in a generic envelope
whose JSON excerpt is cut to 2 KB and then to MAX_OBSERVATION_CHARS (3 KB). A
~5 KB manifest showed the model only its first ~57%, as JSON-escaped text — so
the agent silently missed the rest of normal files, and (Phase 2) could never
write an exact edit anchor for text it hadn't seen.

These tests drive the real path — dispatch on the react surface, then the loop's
own _truncate_observation — and assert on the string the model receives.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import desktop_folders as df  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import audit
    import desktop_paths
    monkeypatch.setattr(desktop_paths, "config_path", lambda: tmp_path / "cfg.json")
    monkeypatch.setattr(audit, "emit", lambda *a, **k: "evt")
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    df.add_grant(str(root), "read")
    return root


@pytest.fixture(scope="module", autouse=True)
def registered():
    # Desktop tools exist only in desktop mode; restore the shared registry so
    # server-mode assertions elsewhere (e.g. the documented tool count) hold.
    import tool_registry as tr
    saved = dict(tr.TOOLS)
    df.register_desktop_tools()
    yield
    tr.TOOLS.clear()
    tr.TOOLS.update(saved)


def _observe(tool: str, params: dict) -> str:
    import react
    import tool_registry as tr
    res = tr.dispatch(tool, params, tr.DispatchContext(surface="react"))
    payload = getattr(res, "payload", None)
    d = res.model_dump(by_alias=True) if hasattr(res, "model_dump") else res
    if payload is not None:
        d["payload"] = payload          # exactly what react.py does
    return react._truncate_observation(d, tool)


def _configmap(n: int) -> str:
    body = "".join(f"  key{i:04d}: value-{i}\n" for i in range(n))
    return "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: big\ndata:\n" + body


class TestReadFileObservation:
    def test_whole_normal_sized_manifest_is_visible(self, repo):
        (repo / "cm.yaml").write_text(_configmap(250))          # ~5 KB
        obs = _observe("read_file", {"path": str(repo / "cm.yaml")})
        assert "key0000: value-0" in obs and "key0249: value-249" in obs

    def test_content_is_not_json_escaped(self, repo):
        (repo / "cm.yaml").write_text(_configmap(3) + '  quoted: "x"\n')
        obs = _observe("read_file", {"path": str(repo / "cm.yaml")})
        # Real newlines and quotes, so an edit anchor can be copied verbatim.
        assert "key0001: value-1\n  key0002: value-2" in obs
        assert 'quoted: "x"' in obs

    def test_large_file_is_paged_and_every_line_reachable(self, repo):
        (repo / "huge.yaml").write_text(_configmap(3000))        # ~65 KB
        first = _observe("read_file", {"path": str(repo / "huge.yaml")})
        assert "key0000" in first and "key2999" not in first
        assert "start_line=" in first                            # tells the model how to continue
        seen = set()
        start = 1
        for _ in range(20):
            out = df._handle_read_file({"path": str(repo / "huge.yaml"), "start_line": start})
            assert out["success"] is True
            seen |= {line.strip().split(":")[0] for line in out["content"].splitlines()}
            if out["end_line"] >= out["total_lines"]:
                break
            start = out["end_line"] + 1
        assert all(f"key{i:04d}" in seen for i in range(3000))

    def test_start_line_past_end(self, repo):
        (repo / "cm.yaml").write_text(_configmap(3))
        out = df._handle_read_file({"path": str(repo / "cm.yaml"), "start_line": 999})
        assert out["success"] is True and out["content"] == ""

    def test_secrets_still_redacted(self, repo):
        (repo / "app.yaml").write_text(
            "kind: Deployment\nannotations:\n  token: ghp_0123456789abcdef0123456789abcdef0123\n"
        )
        obs = _observe("read_file", {"path": str(repo / "app.yaml")})
        assert "ghp_0123456789abcdef0123456789abcdef0123" not in obs


class TestOtherToolObservations:
    def test_list_folder_shows_all_entries(self, repo):
        for i in range(300):
            (repo / f"svc-{i:03d}.yaml").write_text("a: 1\n")
        obs = _observe("list_folder", {"path": str(repo)})
        assert "svc-000.yaml" in obs and "svc-299.yaml" in obs

    def test_search_files_shows_matches_plainly(self, repo):
        (repo / "a.yaml").write_text("image: acme/api:1.0\n")
        obs = _observe("search_files", {"root": str(repo), "pattern": "acme/api"})
        assert "a.yaml:1: image: acme/api:1.0" in obs

    def test_non_folder_tools_unchanged(self):
        import react
        out = react._truncate_observation({"error": "boom", "message": "x"}, "get_pods")
        assert '"error": "boom"' in out
