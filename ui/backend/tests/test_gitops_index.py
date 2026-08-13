from __future__ import annotations
import io, sys, tarfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from gitops.index import RepoFile, build_index, detect_markers, read_tarball  # noqa: E402

API = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-gateway
spec:
  replicas: 3
"""


def test_index_keys_on_kind_and_name():
    idx = build_index([RepoFile("base/api.yaml", API)])
    assert ("Deployment", "api-gateway") in idx
    match = idx[("Deployment", "api-gateway")][0]
    assert match.file_path == "base/api.yaml"
    assert match.doc_index == 0


def test_templated_helm_file_is_skipped_not_fatal():
    bad = "kind: Deployment\nmetadata:\n  name: {{ .Values.name }}\n"
    idx = build_index([RepoFile("templates/x.yaml", bad), RepoFile("base/api.yaml", API)])
    # the good file still indexed; the templated one did not blow up the index
    assert ("Deployment", "api-gateway") in idx


def test_detect_markers_flags_helm_and_argo():
    helm = RepoFile("Chart.yaml", "apiVersion: v2\nname: app\n")
    argo = RepoFile("apps/app.yaml",
                    "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: a\n")
    assert "helm" in detect_markers([helm])
    assert "argo" in detect_markers([argo])


def test_read_tarball_strips_root_prefix_and_filters():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in [("acme-repo-abc123/base/api.yaml", API),
                           ("acme-repo-abc123/README.md", "# hi")]:
            data = body.encode()
            info = tarfile.TarInfo(name); info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    files = read_tarball(buf.getvalue())
    paths = {f.path for f in files}
    assert "base/api.yaml" in paths       # prefix stripped
    assert "README.md" not in paths       # non-yaml filtered
