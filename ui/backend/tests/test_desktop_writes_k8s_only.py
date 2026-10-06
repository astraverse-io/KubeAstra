"""The agent may only write Kubernetes-shaped files (Phase 2 hardening).

".yaml" is not the same as "Kubernetes": CI workflows, pre-commit configs,
compose files and tool configs are YAML too, and each of them runs code. The
validators would pass them (they parse, and aren't manifests), which invites a
rubber-stamp approval. So writes are limited to:

- manifests: every document has a Kubernetes-style apiVersion and a kind;
- kustomization files (by name);
- Helm values files and chart templates (by location).

A write deny-list of known code-running config paths backs this up, because
some tools (pre-commit, for one) tolerate extra keys like apiVersion/kind.
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
import desktop_validators as dv  # noqa: E402
import desktop_writes as dw  # noqa: E402

CM = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg\ndata:\n  a: '1'\n"
DEPLOY = ("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n"
          "spec:\n  replicas: 2\n")


class Ctx:
    session_id = "s1"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import audit
    import desktop_paths
    monkeypatch.setattr(desktop_paths, "config_path", lambda: tmp_path / "cfg.json")
    monkeypatch.setattr(audit, "emit", lambda *a, **k: "evt")
    monkeypatch.setattr(dv, "_found", lambda tool: None)
    monkeypatch.setattr(dw, "pending_write_store", dw.PendingWriteStore())
    monkeypatch.setattr(dw, "_session_cluster", lambda session_id: None)   # no cluster connected
    dw._invalid_attempts.clear()


@pytest.fixture
def repo(tmp_path):
    root = (tmp_path / "infra").resolve()
    root.mkdir()
    df.add_grant(str(root), "write")
    return root


def put(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def propose(path, *, edits=None, content=None):
    params = {"path": str(path), "reason": "fix", "edits": edits or []}
    if content is not None:
        params["content"] = content
    return dw._handle_propose_file_edit(params, Ctx())


class TestApiVersionRule:
    @pytest.mark.parametrize("v", ["v1", "apps/v1", "batch/v1", "autoscaling/v2",
                                   "networking.k8s.io/v1", "cert-manager.io/v1",
                                   "argoproj.io/v1alpha1", "kustomize.config.k8s.io/v1beta1"])
    def test_kubernetes_api_versions(self, v):
        assert dw._k8s_api_version(v)

    @pytest.mark.parametrize("v", ["v2", "skaffold/v4beta6", "foo/v1", "apps/latest", "apps/",
                                   "", None, 123, "tekton/v1"])
    def test_everything_else(self, v):
        assert not dw._k8s_api_version(v)


class TestAllowed:
    def test_manifest_edit(self, repo):
        p = put(repo, "api.yaml", DEPLOY)
        assert "pending_write" in propose(p, edits=[{"old": "replicas: 2", "new": "replicas: 3"}])

    def test_crd_manifest_new_file(self, repo):
        rollout = "apiVersion: argoproj.io/v1alpha1\nkind: Rollout\nmetadata:\n  name: api\n"
        assert "pending_write" in propose(repo / "rollout.yaml", content=rollout)

    def test_multi_document_manifest(self, repo):
        assert "pending_write" in propose(repo / "both.yaml", content=CM + "---\n" + DEPLOY)

    def test_kustomization_without_apiversion(self, repo):
        assert "pending_write" in propose(repo / "kustomization.yaml", content="resources:\n  - api.yaml\n")

    def test_helm_values_and_template(self, repo):
        put(repo, "charts/api/Chart.yaml", "apiVersion: v2\nname: api\nversion: 0.1.0\n")
        values = put(repo, "charts/api/values.yaml", "replicas: 2\n")
        tpl = put(repo, "charts/api/templates/cm.yaml",
                  "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}\n")
        assert "pending_write" in propose(values, edits=[{"old": "replicas: 2", "new": "replicas: 3"}])
        assert "pending_write" in propose(tpl, edits=[{"old": "name: {{ .Release.Name }}",
                                                       "new": "name: {{ .Release.Name }}-cfg"}])


class TestNotKubernetes:
    def test_plain_config_file_is_refused_and_final(self, repo):
        p = put(repo, "config.yaml", "log_level: info\n")
        for _ in range(5):                                   # security refusal: never "stops"
            out = propose(p, edits=[{"old": "info", "new": "debug"}])
        assert out["error"] == "refused: not_k8s" and "stop" not in out

    def test_chart_yaml_is_refused(self, repo):
        p = put(repo, "charts/api/Chart.yaml", "apiVersion: v2\nname: api\nversion: 0.1.0\n")
        assert propose(p, edits=[{"old": "0.1.0", "new": "0.2.0"}])["error"] == "refused: not_k8s"

    def test_skaffold_config_is_refused(self, repo):
        sk = "apiVersion: skaffold/v4beta6\nkind: Config\nbuild: {}\n"
        assert propose(repo / "build.yaml", content=sk)["error"] == "refused: not_k8s_content"

    def test_edit_that_turns_a_manifest_into_something_else(self, repo):
        p = put(repo, "api.yaml", DEPLOY)
        out = propose(p, edits=[{"old": "kind: Deployment\n", "new": ""}])
        assert out["error"] == "refused: not_k8s_content" and out["attempts_remaining"] == 2

    def test_new_file_must_be_kubernetes(self, repo):
        assert propose(repo / "x.yaml", content="a: 1\n")["error"] == "refused: not_k8s_content"

    def test_one_foreign_document_spoils_the_file(self, repo):
        out = propose(repo / "x.yaml", content=CM + "---\nrepos: []\n")
        assert out["error"] == "refused: not_k8s_content"


class TestCodeRunningConfigPaths:
    @pytest.mark.parametrize("rel", [
        ".github/workflows/deploy.yml", ".GitHub/workflows/deploy.yaml", ".gitlab/ci.yml",
        ".circleci/config.yml", ".config/gh/config.yml",
        ".pre-commit-config.yaml", ".gitlab-ci.yml", "azure-pipelines.yml",
        "docker-compose.yml", "docker-compose.prod.yaml", "compose.yaml",
        "Taskfile.yml", "skaffold.yaml", "cloudbuild.yaml", ".goreleaser.yaml", "mkdocs.yml",
    ])
    def test_refused_even_with_kubernetes_keys(self, repo, rel):
        # A doc can carry apiVersion/kind AND be a valid config for a tool that
        # ignores unknown keys; refuse these paths outright.
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        out = propose(repo / rel, content=CM)
        assert out["error"] == "refused: deny_list", rel

    def test_apply_rechecks_the_path(self, repo):
        # resolve_write_target runs again at apply, so the path rule holds there too.
        (repo / ".github").mkdir()
        g = df.grant_for(str(repo), "write")
        with pytest.raises(dw.WriteRefused) as ei:
            dw.resolve_write_target(str(repo / ".github" / "x.yaml"), g)
        assert ei.value.reason == "deny_list"
