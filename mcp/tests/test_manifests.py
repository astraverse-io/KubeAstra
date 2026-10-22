"""The shared manifest reader (``services.manifests``) used by the Upgrade and
Resilience cores."""
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.manifests import iter_manifest_docs, manifest_files  # noqa: E402


def test_docs_carry_file_index_and_start_line(tmp_path):
    (tmp_path / "a.yaml").write_text("# header\nkind: A\n---\nkind: B\nx: 1\n---\n\nkind: C\n")
    docs = list(iter_manifest_docs(tmp_path))
    assert [(d.index, d.line, d.doc["kind"]) for d in docs] == [(0, 2, "A"), (1, 4, "B"), (2, 8, "C")]
    assert all(d.path.name == "a.yaml" for d in docs)


def test_a_malformed_file_is_skipped_whole_not_partially(tmp_path):
    (tmp_path / "bad.yaml").write_text("kind: Good\n---\nkind: [unclosed\n")
    (tmp_path / "ok.yml").write_text("kind: Fine\n")
    assert [d.doc["kind"] for d in iter_manifest_docs(tmp_path)] == ["Fine"]


def test_walks_recursively_in_sorted_order_and_ignores_other_files(tmp_path):
    (tmp_path / "z.yaml").write_text("kind: Z\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.json").write_text('{"kind": "J"}')
    (tmp_path / "README.md").write_text("kind: NotAManifest\n")
    (tmp_path / "dir.yaml").mkdir()  # a directory with a manifest suffix is not a file
    assert [p.name for p in manifest_files(tmp_path)] == ["a.json", "z.yaml"]
    assert [d.doc["kind"] for d in iter_manifest_docs(tmp_path)] == ["J", "Z"]


def test_single_file_path_and_undecodable_file(tmp_path):
    f = tmp_path / "one.yaml"
    f.write_text("kind: One\n")
    assert [d.doc["kind"] for d in iter_manifest_docs(f)] == ["One"]
    (tmp_path / "bin.yaml").write_bytes(b"\xff\xfe\x00garbage")
    assert [d.doc["kind"] for d in iter_manifest_docs(tmp_path)] == ["One"]
