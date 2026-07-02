"""Tests for read-only Helm tools and the HelmRunner safety/redaction behavior."""
import json
from pathlib import Path
import sys

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.helm_runner import HelmResult, HelmRunner, set_helm_runner, _validate_read_only  # noqa: E402
from k8s.helm_wrappers import (  # noqa: E402
    helm_available, list_helm_releases, get_helm_release, diff_helm_revisions,
)
from k8s import redaction  # noqa: E402


def _section_key(args):
    if args and args[0] == "get":
        return ("get", args[1])
    return (args[0],)


class FakeHelmRunner:
    """Returns canned helm output keyed by subcommand."""

    def __init__(self, responses):
        # responses: {("list",): (stdout, returncode, stderr), ...}
        self.responses = responses
        self.calls = []

    def run(self, args, namespace=None):
        self.calls.append(list(args))
        stdout, rc, stderr = self.responses.get(_section_key(args), ("", 1, "not found"))
        return HelmResult(stdout, stderr, rc, ["helm"] + list(args), 0.0)


class FakeSshHelmRunner(FakeHelmRunner):
    ssh_runner = object()


def _use(responses):
    set_helm_runner(FakeHelmRunner(responses))


def teardown_function():
    set_helm_runner(None)


RELEASE_LIST = json.dumps([
    {"name": "jenkins-legacy", "namespace": "jenkins-legacy", "revision": "7",
     "status": "deployed", "chart": "jenkins-5.8.92", "app_version": "2.452.1",
     "updated": "2026-06-01T10:00:00Z"},
])

STATUS_JSON = json.dumps({
    "name": "jenkins-legacy", "namespace": "jenkins-legacy", "version": 7,
    "info": {"status": "deployed", "last_deployed": "2026-06-01T10:00:00Z",
             "description": "Upgrade complete"},
    "chart": {"metadata": {"name": "jenkins", "version": "5.8.92", "appVersion": "2.452.1"}},
})

HISTORY_JSON = json.dumps([
    {"revision": 6, "updated": "2026-05-01", "status": "superseded", "chart": "jenkins-5.8.90",
     "app_version": "2.450.0", "description": "Upgrade"},
    {"revision": 7, "updated": "2026-06-01", "status": "deployed", "chart": "jenkins-5.8.92",
     "app_version": "2.452.1", "description": "Upgrade complete"},
])

VALUES_YAML = "controller:\n  adminPassword: hunter2\n  tag: 2.452.1\n"

SECRET_MANIFEST = (
    "---\n"
    "apiVersion: v1\n"
    "kind: Secret\n"
    "metadata:\n"
    "  name: jenkins-admin\n"
    "data:\n"
    "  jenkins-admin-password: aHVudGVyMg==\n"
    "  jenkins-admin-user: YWRtaW4=\n"
    "type: Opaque\n"
    "---\n"
    "apiVersion: v1\n"
    "kind: ConfigMap\n"
    "metadata:\n"
    "  name: jenkins-config\n"
    "data:\n"
    "  log_level: INFO\n"
)


# ── helm_available ───────────────────────────────────────────────────────────

def test_helm_available_true():
    _use({("version",): ("v3.14.0+g1234", 0, "")})
    res = helm_available()
    assert res["available"] is True
    assert "v3.14.0" in res["version"]


def test_helm_available_false_when_missing():
    _use({("version",): ("", 127, "helm: command not found")})
    res = helm_available()
    assert res["available"] is False
    assert res["reason"] == "helm_unavailable"
    assert res["target"] == "backend"
    assert "backend image" in res["remediation_hint"]


def test_helm_available_missing_over_ssh_points_to_target_host():
    set_helm_runner(FakeSshHelmRunner({
        ("version",): ("", 127, "sh: 1: helm: not found"),
    }))
    res = helm_available()
    assert res["available"] is False
    assert res["reason"] == "helm_unavailable"
    assert res["target"] == "ssh_target"
    assert "SSH target host" in res["remediation_hint"]


class _RaisingRunner:
    """Simulates a runner that raises (e.g. SSH/helm-binary failure)."""

    def run(self, args, namespace=None):
        from k8s.helm_runner import HelmError
        raise HelmError("helm over SSH failed: connection refused")


def test_helm_available_graceful_on_runner_error():
    set_helm_runner(_RaisingRunner())
    res = helm_available()
    assert res["available"] is False
    assert res["reason"] == "helm_check_failed"
    assert "install helm" not in res["remediation_hint"].lower()


def test_list_helm_releases_graceful_on_runner_error():
    set_helm_runner(_RaisingRunner())
    res = list_helm_releases("default")
    assert res["available"] is False
    assert res["reason"] == "helm_check_failed"
    assert res["releases"] == []


def test_get_helm_release_graceful_on_runner_error():
    set_helm_runner(_RaisingRunner())
    res = get_helm_release("r", "ns")
    # Every section errors, none crash the call.
    assert res["found"] is False
    assert set(res["errors"].keys()) == {"status", "history", "values"}


def test_get_helm_release_missing_helm_adds_user_facing_hint():
    _use({
        ("status",): ("", 127, "helm: command not found"),
        ("history",): ("", 127, "helm: command not found"),
        ("get", "values"): ("", 127, "helm: command not found"),
    })
    res = get_helm_release("r", "ns")
    assert res["found"] is False
    assert res["reason"] == "helm_unavailable"
    assert res["target"] == "backend"
    assert "Install Helm in the backend image/container" in res["remediation_hint"]


# ── list_helm_releases ───────────────────────────────────────────────────────

def test_list_helm_releases_parses_json():
    _use({("list",): (RELEASE_LIST, 0, "")})
    res = list_helm_releases("jenkins-legacy")
    assert res["available"] is True
    assert res["release_count"] == 1
    assert res["releases"][0]["chart"] == "jenkins-5.8.92"


def test_list_helm_releases_unavailable():
    _use({("list",): ("", 127, "helm: command not found")})
    res = list_helm_releases("default")
    assert res["available"] is False
    assert res["releases"] == []


# ── get_helm_release ─────────────────────────────────────────────────────────

HOOKS_MANIFEST = (
    "---\n# Source: chart/templates/hook.yaml\n"
    "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db-init-hook\n"
    "data:\n  password: c2VjcmV0aG9vaw==\n"
)
NOTES_TEXT = "Thank you for installing jenkins.\nGet the admin password: token=supersecrettoken\n"
METADATA_JSON = json.dumps({
    "name": "jenkins-legacy", "chart": "jenkins", "version": "5.8.92",
    "appVersion": "2.452.1", "namespace": "jenkins-legacy", "revision": 7,
})


def _full_responses(overrides=None):
    base = {
        ("status",): (STATUS_JSON, 0, ""),
        ("history",): (HISTORY_JSON, 0, ""),
        ("get", "values"): (VALUES_YAML, 0, ""),
        ("get", "manifest"): (SECRET_MANIFEST, 0, ""),
        ("get", "hooks"): (HOOKS_MANIFEST, 0, ""),
        ("get", "notes"): (NOTES_TEXT, 0, ""),
        ("get", "metadata"): (METADATA_JSON, 0, ""),
    }
    base.update(overrides or {})
    return base


def test_get_helm_release_default_sections_skip_manifest():
    runner = FakeHelmRunner(_full_responses())
    set_helm_runner(runner)
    res = get_helm_release("jenkins-legacy", "jenkins-legacy")
    assert set(res["sections"].keys()) == {"status", "history", "values"}
    assert res["sections"]["status"]["chart"] == "jenkins"
    assert res["sections"]["status"]["revision"] == 7
    assert len(res["sections"]["history"]) == 2
    # manifest must not have been fetched by default
    assert ["get", "manifest"] not in runner.calls


def test_get_helm_release_redacts_values():
    _use(_full_responses())
    res = get_helm_release("jenkins-legacy", "jenkins-legacy")
    values = res["sections"]["values"]
    assert "hunter2" not in values
    assert "tag: 2.452.1" in values  # non-secret values preserved


def test_get_helm_release_manifest_redacts_secret_data():
    _use(_full_responses())
    res = get_helm_release("jenkins-legacy", "jenkins-legacy", sections=["manifest"])
    manifest = res["sections"]["manifest"]
    assert "aHVudGVyMg==" not in manifest          # secret base64 gone
    assert "YWRtaW4=" not in manifest                # secret base64 gone
    assert "log_level: INFO" in manifest             # ConfigMap data preserved
    assert "***redacted***" in manifest


def test_get_helm_release_partial_results_on_section_failure():
    res_map = _full_responses({("get", "values"): ("", 1, "release: not found")})
    set_helm_runner(FakeHelmRunner(res_map))
    res = get_helm_release("jenkins-legacy", "jenkins-legacy")
    assert res["sections"]["status"] is not None     # other sections still work
    assert res["sections"]["values"] is None
    assert "values" in res["errors"]
    assert res["found"] is True


# ── Phase 1.5: new sections, revision, list filters ──────────────────────────

def test_get_helm_release_hooks_section_redacts_secrets():
    _use(_full_responses())
    res = get_helm_release("jenkins-legacy", "jenkins-legacy", sections=["hooks"])
    hooks = res["sections"]["hooks"]
    assert "c2VjcmV0aG9vaw==" not in hooks      # hook Secret data redacted
    assert "***redacted***" in hooks


def test_get_helm_release_notes_section_redacts_secrets():
    _use(_full_responses())
    res = get_helm_release("jenkins-legacy", "jenkins-legacy", sections=["notes"])
    assert "supersecrettoken" not in res["sections"]["notes"]


def test_get_helm_release_metadata_section_parsed():
    _use(_full_responses())
    res = get_helm_release("jenkins-legacy", "jenkins-legacy", sections=["metadata"])
    md = res["sections"]["metadata"]
    assert md["chart"] == "jenkins" and md["version"] == "5.8.92"


def test_get_helm_release_revision_passed_to_revisioned_sections_only():
    runner = FakeHelmRunner(_full_responses())
    set_helm_runner(runner)
    get_helm_release("jenkins-legacy", "jenkins-legacy",
                     sections=["status", "history", "values", "metadata"], revision=3)
    # status, values & metadata carry --revision 3; history (all-revisions) does not.
    def has_rev(section_call_match):
        return any(section_call_match(c) and "--revision" in c and "3" in c for c in runner.calls)
    assert has_rev(lambda c: c[:1] == ["status"])
    assert has_rev(lambda c: c[:2] == ["get", "values"])
    assert has_rev(lambda c: c[:2] == ["get", "metadata"])
    assert not any(c[:1] == ["history"] and "--revision" in c for c in runner.calls)


@pytest.mark.parametrize("bad", [0, -1, "3", True, 1.5])
def test_get_helm_release_rejects_invalid_revision(bad):
    _use(_full_responses())
    with pytest.raises(ValueError):
        get_helm_release("jenkins-legacy", "jenkins-legacy", revision=bad)


def test_list_helm_releases_status_filter_adds_flag():
    runner = FakeHelmRunner({("list",): (RELEASE_LIST, 0, "")})
    set_helm_runner(runner)
    res = list_helm_releases("jenkins-legacy", status_filter="failed")
    assert res["status_filter"] == "failed"
    assert any("--failed" in c for c in runner.calls)


def test_list_helm_releases_unknown_status_filter_not_applied_and_not_echoed():
    runner = FakeHelmRunner({("list",): (RELEASE_LIST, 0, "")})
    set_helm_runner(runner)
    res = list_helm_releases("ns", status_filter="bogus")
    call = runner.calls[0]
    assert not any(flag in call for flag in ("--failed", "--pending", "--deployed", "--superseded"))
    # Response must not claim an unapplied filter was applied.
    assert res["status_filter"] is None


def test_list_helm_releases_status_filter_echoes_normalized():
    runner = FakeHelmRunner({("list",): (RELEASE_LIST, 0, "")})
    set_helm_runner(runner)
    res = list_helm_releases("ns", status_filter="FAILED")
    assert res["status_filter"] == "failed"  # normalized, and it was applied
    assert any("--failed" in c for c in runner.calls)


# ── HelmRunner safety ────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    ["upgrade", "rel"], ["uninstall", "rel"], ["rollback", "rel", "1"],
    ["delete", "rel"], ["repo", "add"], ["get", "hooks-and-more"],
    ["show", "chart", "stable/x"], ["env"],  # tightened out of v1 allowlist
])
def test_validate_read_only_rejects_writes(args):
    with pytest.raises(ValueError):
        _validate_read_only(args)


@pytest.mark.parametrize("args", [
    ["version", "--short"], ["list", "-o", "json"], ["status", "rel"],
    ["history", "rel"], ["get", "values", "rel"], ["get", "manifest", "rel"],
])
def test_validate_read_only_allows_reads(args):
    _validate_read_only(args)  # must not raise


def test_helm_runner_rejects_write_before_executing():
    # Validation happens before any subprocess, so this is safe without helm installed.
    runner = HelmRunner()
    with pytest.raises(ValueError):
        runner.run(["upgrade", "rel", "chart"])


# ── redaction.redact_manifest direct ─────────────────────────────────────────

def test_redact_prose_redacts_inline_secret_but_not_plain_prose():
    txt = "Run kubectl. The api_key=abc123 is set. The secret is safe in the vault."
    out = redaction.redact_prose(txt, 100000)
    assert "abc123" not in out                     # inline secret redacted
    assert "secret is safe in the vault" in out     # plain prose (no :/= ) untouched


def test_redact_manifest_handles_unterminated_secret_block_then_doc():
    res = redaction.redact_manifest(SECRET_MANIFEST, 100000)
    assert "aHVudGVyMg==" not in res
    assert "log_level: INFO" in res


def test_redact_manifest_redacts_inline_secret_map():
    # Inline flow-map form must be redacted too, not just block style.
    manifest = (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n  name: s\n"
        "data: {password: c2VjcmV0, user: YWRtaW4=}\n"
        "type: Opaque\n"
    )
    res = redaction.redact_manifest(manifest, 100000)
    assert "c2VjcmV0" not in res
    assert "YWRtaW4=" not in res
    assert "***redacted***" in res
    assert "type: Opaque" in res


def test_redact_manifest_keeps_empty_inline_data_map():
    manifest = "kind: Secret\nmetadata:\n  name: s\ndata: {}\ntype: Opaque\n"
    res = redaction.redact_manifest(manifest, 100000)
    assert "data: {}" in res


def test_redact_manifest_redacts_block_scalar_and_survives_blank_line():
    # A Secret with a multi-line block-scalar value and a blank line inside data:
    # both the block-scalar body AND lines after the blank must stay redacted.
    manifest = (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        "  name: tls\n"
        "data:\n"
        "  tls.key: |\n"
        "    SECRETKEYBODYLINE1\n"
        "    SECRETKEYBODYLINE2\n"
        "\n"
        "  api_token: VE9LRU5WQUw=\n"
        "type: kubernetes.io/tls\n"
    )
    res = redaction.redact_manifest(manifest, 100000)
    assert "SECRETKEYBODYLINE1" not in res     # block-scalar body redacted
    assert "SECRETKEYBODYLINE2" not in res
    assert "VE9LRU5WQUw=" not in res            # line after blank still redacted
    assert "type: kubernetes.io/tls" in res     # dedented non-secret line kept


# ── diff_helm_revisions ──────────────────────────────────────────────────────

class RevHelmRunner:
    """Returns per-revision values/manifest, keyed by the --revision flag."""

    def __init__(self, by_rev):
        self.by_rev = by_rev  # {rev: {"values": str, "manifest": str}}
        self.calls = []

    def run(self, args, namespace=None):
        self.calls.append(list(args))
        rev = None
        if "--revision" in args:
            rev = int(args[args.index("--revision") + 1])
        data = self.by_rev.get(rev, {})
        if args[:2] == ["get", "values"]:
            return HelmResult(data.get("values", ""), "", 0, ["helm"] + list(args), 0.0)
        if args[:2] == ["get", "manifest"]:
            return HelmResult(data.get("manifest", ""), "", 0, ["helm"] + list(args), 0.0)
        return HelmResult("", "unexpected", 1, ["helm"] + list(args), 0.0)


def test_diff_helm_revisions_values_shows_nonsecret_change():
    set_helm_runner(RevHelmRunner({
        6: {"values": "controller:\n  tag: 2.450.0\n  adminPassword: OLDSECRET\n"},
        7: {"values": "controller:\n  tag: 2.452.1\n  adminPassword: NEWSECRET\n"},
    }))
    res = diff_helm_revisions("jenkins-legacy", "jenkins-legacy", 6, 7)
    assert res["changed"] is True
    assert "2.450.0" in res["diff"] and "2.452.1" in res["diff"]   # tag change visible
    # Secret values never appear in the diff (both sides redacted identically).
    assert "OLDSECRET" not in res["diff"]
    assert "NEWSECRET" not in res["diff"]


def test_diff_helm_revisions_identical_is_unchanged():
    same = {"values": "controller:\n  tag: 2.452.1\n"}
    set_helm_runner(RevHelmRunner({6: same, 7: dict(same)}))
    res = diff_helm_revisions("r", "ns", 6, 7)
    assert res["changed"] is False
    assert res["diff"] == ""
    # Must flag that a secret-only change would be hidden, so the agent does not
    # claim the revisions are byte-identical.
    assert res["redaction_may_hide_secret_only_changes"] is True


def test_diff_helm_revisions_secret_only_change_reports_unchanged_with_flag():
    # Only the admin password differs between revisions; nothing else.
    set_helm_runner(RevHelmRunner({
        1: {"values": "adminPassword: OLDSECRET\n"},
        2: {"values": "adminPassword: NEWSECRET\n"},
    }))
    res = diff_helm_revisions("r", "ns", 1, 2)
    assert res["changed"] is False                  # redacted diff shows nothing
    assert "OLDSECRET" not in res["diff"] and "NEWSECRET" not in res["diff"]
    assert res["redaction_may_hide_secret_only_changes"] is True


def test_diff_helm_revisions_manifest_section():
    set_helm_runner(RevHelmRunner({
        1: {"manifest": "kind: Deployment\nspec:\n  replicas: 1\n"},
        2: {"manifest": "kind: Deployment\nspec:\n  replicas: 3\n"},
    }))
    res = diff_helm_revisions("r", "ns", 1, 2, section="manifest")
    assert res["section"] == "manifest"
    assert "replicas: 1" in res["diff"] and "replicas: 3" in res["diff"]


@pytest.mark.parametrize("a,b", [(0, 1), (1, -2), ("1", 2)])
def test_diff_helm_revisions_rejects_bad_revisions(a, b):
    set_helm_runner(RevHelmRunner({}))
    with pytest.raises(ValueError):
        diff_helm_revisions("r", "ns", a, b)


def test_diff_helm_revisions_rejects_bad_section():
    set_helm_runner(RevHelmRunner({1: {}, 2: {}}))
    with pytest.raises(ValueError):
        diff_helm_revisions("r", "ns", 1, 2, section="bogus")


def test_diff_helm_revisions_partial_on_fetch_failure():
    class FailRunner:
        def run(self, args, namespace=None):
            return HelmResult("", "release has no revision 9", 1, ["helm"], 0.0)
    set_helm_runner(FailRunner())
    res = diff_helm_revisions("r", "ns", 8, 9)
    assert res["diff"] is None
    assert res["errors"]


# ── investigate_helm_release ─────────────────────────────────────────────────

from k8s.helm_wrappers import (  # noqa: E402
    investigate_helm_release, _extract_manifest_resources,
)

INV_MANIFEST = (
    "---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: jenkins\n"
    "spec:\n  replicas: 1\n"
    "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: jenkins-svc\n"
    "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: jenkins-admin\n"
    "data:\n  password: c2VjcmV0\n"
)


def test_extract_manifest_resources_kind_name_pairs():
    res = _extract_manifest_resources(INV_MANIFEST)
    pairs = {(r["kind"], r["name"]) for r in res}
    assert ("Deployment", "jenkins") in pairs
    assert ("Service", "jenkins-svc") in pairs
    assert ("Secret", "jenkins-admin") in pairs


def test_investigate_helm_release_composite(monkeypatch):
    set_helm_runner(FakeHelmRunner(_full_responses({("get", "manifest"): (INV_MANIFEST, 0, "")})))
    import k8s.wrappers as wrappers
    monkeypatch.setattr(wrappers, "get_pods", lambda ns: {
        "pod_count": 2,
        "pods": [
            {"name": "jenkins-0", "status": "Running", "ready": True, "restarts": 0},
            {"name": "jenkins-1", "status": "CrashLoopBackOff", "ready": False, "restarts": 9},
        ],
    })
    monkeypatch.setattr(wrappers, "get_events", lambda ns, field_selector=None: {
        "events": [
            {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting", "count": 5,
             "object": {"kind": "Pod", "name": "jenkins-1"}, "last_timestamp": "now"},
        ],
    })

    res = investigate_helm_release("jenkins-legacy", "jenkins-legacy")
    assert res["found"] is True
    assert res["status"]["chart"] == "jenkins"
    assert {w["kind"] for w in res["workloads"]} == {"Deployment"}
    assert res["pod_health"]["unhealthy_count"] == 1
    assert res["recent_warnings"]["count"] == 1
    assert res["release_healthy"] is False     # one pod CrashLoopBackOff


def test_investigate_helm_release_not_found_stops_early():
    # status fails -> release likely doesn't exist; return gracefully.
    set_helm_runner(FakeHelmRunner({}))  # status returns rc=1
    res = investigate_helm_release("missing", "ns")
    assert res["found"] is False
    assert "status" in res.get("errors", {})


def test_investigate_helm_release_survives_kubectl_failure(monkeypatch):
    set_helm_runner(FakeHelmRunner(_full_responses({("get", "manifest"): (INV_MANIFEST, 0, "")})))
    import k8s.wrappers as wrappers
    monkeypatch.setattr(wrappers, "get_pods", lambda ns: (_ for _ in ()).throw(RuntimeError("kubectl down")))
    monkeypatch.setattr(wrappers, "get_events", lambda ns, field_selector=None: (_ for _ in ()).throw(RuntimeError("kubectl down")))
    res = investigate_helm_release("jenkins-legacy", "jenkins-legacy")
    assert res["found"] is True                 # helm parts still work
    assert "error" in res["pod_health"]
    assert "error" in res["recent_warnings"]


def test_investigate_helm_release_ignores_unrelated_namespace_noise(monkeypatch):
    # Shared namespace: an unrelated CrashLooping pod + warning must NOT make
    # this release look unhealthy.
    set_helm_runner(FakeHelmRunner(_full_responses({("get", "manifest"): (INV_MANIFEST, 0, "")})))
    import k8s.wrappers as wrappers
    monkeypatch.setattr(wrappers, "get_pods", lambda ns: {"pods": [
        {"name": "jenkins-0", "status": "Running", "ready": True, "restarts": 0},
        {"name": "other-app-7d9", "status": "CrashLoopBackOff", "ready": False, "restarts": 12},
    ]})
    monkeypatch.setattr(wrappers, "get_events", lambda ns, field_selector=None: {"events": [
        {"type": "Warning", "reason": "BackOff", "message": "x", "count": 3,
         "object": {"kind": "Pod", "name": "other-app-7d9"}},
    ]})
    res = investigate_helm_release("jenkins-legacy", "jenkins-legacy")
    assert res["pod_health"]["scoped"] is True
    assert res["pod_health"]["pod_count"] == 1
    assert res["pod_health"]["unhealthy_count"] == 0
    assert res["recent_warnings"]["count"] == 0
    assert res["release_healthy"] is True


def test_investigate_helm_release_prior_failed_revision_not_health_blocking(monkeypatch):
    failed_history = json.dumps([
        {"revision": 6, "status": "failed", "chart": "jenkins-5.8.90", "app_version": "2.450.0",
         "updated": "2026-05-01", "description": "Upgrade failed"},
        {"revision": 7, "status": "deployed", "chart": "jenkins-5.8.92", "app_version": "2.452.1",
         "updated": "2026-06-01", "description": "Upgrade complete"},
    ])
    set_helm_runner(FakeHelmRunner(_full_responses({
        ("history",): (failed_history, 0, ""),
        ("get", "manifest"): (INV_MANIFEST, 0, ""),
    })))
    import k8s.wrappers as wrappers
    monkeypatch.setattr(wrappers, "get_pods", lambda ns: {"pods": [
        {"name": "jenkins-0", "status": "Running", "ready": True, "restarts": 0},
    ]})
    monkeypatch.setattr(wrappers, "get_events", lambda ns, field_selector=None: {"events": []})
    res = investigate_helm_release("jenkins-legacy", "jenkins-legacy")
    assert res["release_healthy"] is True
    assert res["prior_failed_revisions"] is not None
    assert any(r["revision"] == 6 for r in res["prior_failed_revisions"])


def test_investigate_helm_release_unscoped_pods_do_not_block_health(monkeypatch):
    set_helm_runner(FakeHelmRunner(_full_responses({("get", "manifest"): ("", 0, "")})))
    import k8s.wrappers as wrappers
    monkeypatch.setattr(wrappers, "get_pods", lambda ns: {"pods": [
        {"name": "whatever", "status": "CrashLoopBackOff", "ready": False, "restarts": 5},
    ]})
    monkeypatch.setattr(wrappers, "get_events", lambda ns, field_selector=None: {"events": []})
    res = investigate_helm_release("jenkins-legacy", "jenkins-legacy")
    assert res["pod_health"]["scoped"] is False
    assert res["release_healthy"] is True
