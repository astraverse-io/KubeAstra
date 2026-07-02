"""Tests for read-only ConfigMap inspection tools: get_configmap / search_configmaps.

These cover the live-config evidence path for source/config tracing: reading a
named ConfigMap's data, discovering which ConfigMap holds a value, redaction of
sensitive-looking values, size caps, and previews-not-values defaults.
"""
from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.kubectl_runner import set_runner  # noqa: E402
from k8s import wrappers  # noqa: E402
from k8s.wrappers import get_configmap, search_configmaps  # noqa: E402


PLUGINS_TXT = (
    "pipeline-model-api:2.2277.v00573e73ddf1\n"
    "pipeline-stage-tags-metadata:2.2277.v00573e73ddf1\n"
)

JENKINS_CM = {
    "metadata": {
        "name": "jenkins-legacy",
        "namespace": "jenkins-legacy",
        "labels": {
            "app.kubernetes.io/managed-by": "Helm",
            "helm.sh/chart": "jenkins-5.8.92",
        },
        "annotations": {
            "argocd.argoproj.io/tracking-id": "jenkins-legacy:/ConfigMap:jenkins-legacy/jenkins-legacy",
        },
    },
    "data": {
        "plugins.txt": PLUGINS_TXT,
        "jenkins.yaml": "log_level: INFO\n",
    },
}


class FakeRunner:
    """Dispatches kubectl get calls to canned ConfigMap JSON."""

    def __init__(self, named=None, items=None):
        self.named = named or {}
        self.items = items or []

    def run_json(self, args, namespace=None):
        if args[:2] == ["get", "configmap"]:
            name = args[2]
            cm = self.named.get(name)
            if cm is None:
                from k8s.kubectl_runner import KubectlError
                raise KubectlError(f"configmaps \"{name}\" not found")
            return cm
        if args[:2] == ["get", "configmaps"]:
            return {"items": self.items}
        raise AssertionError(f"unexpected args {args}")


def _use(named=None, items=None):
    set_runner(FakeRunner(named=named, items=items))


def teardown_function():
    set_runner(None)


# ── get_configmap ────────────────────────────────────────────────────────────

def test_get_configmap_returns_named_key_value():
    _use(named={"jenkins-legacy": JENKINS_CM})
    res = get_configmap("jenkins-legacy", "jenkins-legacy", key="plugins.txt")
    assert res["found"] is True
    assert "2.2277.v00573e73ddf1" in res["value"]
    assert res["key"] == "plugins.txt"
    assert res["labels_hint"]["helm.sh/chart"] == "jenkins-5.8.92"
    assert "argocd.argoproj.io/tracking-id" in res["labels_hint"]


def test_get_configmap_without_key_returns_previews_not_full_values():
    _use(named={"jenkins-legacy": JENKINS_CM})
    res = get_configmap("jenkins-legacy", "jenkins-legacy")
    assert res["found"] is True
    assert set(res["keys"]) == {"plugins.txt", "jenkins.yaml"}
    assert "previews" in res and "value" not in res
    # Preview is capped well under the full value length.
    assert len(res["previews"]["plugins.txt"]) <= wrappers._CM_PREVIEW_CAP + 60


def test_get_configmap_caps_large_value():
    big = {"metadata": {"name": "big", "namespace": "ns"}, "data": {"blob": "x" * 10000}}
    _use(named={"big": big})
    res = get_configmap("ns", "big", key="blob")
    assert "truncated" in res["value"]
    assert len(res["value"]) < 10000


def test_get_configmap_redacts_sensitive_keys():
    cm = {"metadata": {"name": "creds", "namespace": "ns"},
          "data": {"db_password": "hunter2", "log_level": "INFO"}}
    _use(named={"creds": cm})
    # Sensitive key name -> whole value redacted.
    assert get_configmap("ns", "creds", key="db_password")["value"] == "***redacted***"
    assert get_configmap("ns", "creds", key="log_level")["value"] == "INFO"


def test_get_configmap_does_not_redact_plugin_list_with_credentials_names():
    # Regression: a Jenkins plugins.txt contains plugin names like
    # "plain-credentials"/"ssh-credentials". The whole list must NOT be redacted
    # just because the word "credentials" appears.
    plugins = (
        "pipeline-model-api:2.2277.v00573e73ddf1\n"
        "plain-credentials:1.8\n"
        "ssh-credentials:308.ve4497b_ccd8f4\n"
    )
    cm = {"metadata": {"name": "jenkins-legacy", "namespace": "ci"},
          "data": {"plugins.txt": plugins}}
    _use(named={"jenkins-legacy": cm})
    value = get_configmap("ci", "jenkins-legacy", key="plugins.txt")["value"]
    assert "2.2277.v00573e73ddf1" in value
    assert "plain-credentials:1.8" in value
    assert "***redacted***" not in value


def test_get_configmap_redacts_entire_pem_private_key_block():
    # Regression: the whole PEM block (BEGIN, base64 body, END) must be redacted,
    # not just the BEGIN line — ConfigMaps can hold private keys.
    pem_value = (
        "tls_ca: harmless\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDb\n"
        "c2VjcmV0LWtleS1tYXRlcmlhbC1kby1ub3QtbGVhaw==\n"
        "-----END PRIVATE KEY-----\n"
        "log_level: INFO\n"
    )
    cm = {"metadata": {"name": "tls", "namespace": "ns"}, "data": {"config": pem_value}}
    _use(named={"tls": cm})
    value = get_configmap("ns", "tls", key="config")["value"]
    assert "MIIEvQIBADANBgkqhkiG" not in value          # body line gone
    assert "c2VjcmV0LWtleS1tYXRlcmlhbA" not in value     # body line gone
    assert "END PRIVATE KEY" not in value                # end line gone
    assert "BEGIN PRIVATE KEY" not in value              # begin line gone
    assert "***redacted (private key)***" in value
    assert "tls_ca: harmless" in value                   # surrounding content kept
    assert "log_level: INFO" in value


def test_get_configmap_redacts_unterminated_pem_block():
    # BEGIN with no END (e.g. truncated) -> redact from BEGIN to end of value.
    pem_value = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw\n"
    )
    cm = {"metadata": {"name": "tls", "namespace": "ns"}, "data": {"config": pem_value}}
    _use(named={"tls": cm})
    value = get_configmap("ns", "tls", key="config")["value"]
    assert "MIIEvQIBADANBgkqhkiG" not in value
    assert "***redacted (private key)***" in value


def test_get_configmap_redacts_only_secret_lines_in_multiline_value():
    # A multi-line app.properties: secret-assignment lines redacted, rest kept.
    props = (
        "log_level=INFO\n"
        "db_password=hunter2\n"
        "client_secret: abc123xyz\n"
        "feature_flag=true\n"
    )
    cm = {"metadata": {"name": "app", "namespace": "ns"}, "data": {"app.properties": props}}
    _use(named={"app": cm})
    value = get_configmap("ns", "app", key="app.properties")["value"]
    assert "log_level=INFO" in value
    assert "feature_flag=true" in value
    assert "hunter2" not in value
    assert "abc123xyz" not in value
    assert value.count("***redacted***") == 2


def test_get_configmap_not_found():
    _use(named={})
    res = get_configmap("ns", "missing")
    assert res["found"] is False


def test_get_configmap_missing_key():
    _use(named={"jenkins-legacy": JENKINS_CM})
    res = get_configmap("jenkins-legacy", "jenkins-legacy", key="nope")
    assert res["found"] is True
    assert "error" in res


# ── search_configmaps ────────────────────────────────────────────────────────

def test_search_configmaps_finds_value_across_configmaps():
    _use(items=[
        {"metadata": {"name": "other", "namespace": "jenkins-legacy"}, "data": {"x": "noise"}},
        JENKINS_CM,
    ])
    res = search_configmaps("jenkins-legacy", "2.2277.v00573e73ddf1")
    assert res["match_count"] == 1
    m = res["matches"][0]
    assert m["configmap"] == "jenkins-legacy"
    assert m["key"] == "plugins.txt"
    assert "2.2277.v00573e73ddf1" in m["excerpt"]
    assert m["labels_hint"]["helm.sh/chart"] == "jenkins-5.8.92"


def test_search_configmaps_no_match():
    _use(items=[JENKINS_CM])
    res = search_configmaps("jenkins-legacy", "does-not-exist")
    assert res["match_count"] == 0
    assert res["searched_count"] == 1


def test_search_configmaps_matches_key_name():
    _use(items=[JENKINS_CM])
    res = search_configmaps("jenkins-legacy", "plugins.txt")
    assert res["match_count"] == 1
    assert res["matches"][0]["match"] == "key"


def test_search_configmaps_respects_max_matches():
    items = [
        {"metadata": {"name": f"cm{i}", "namespace": "ns"}, "data": {"k": "target-value"}}
        for i in range(5)
    ]
    _use(items=items)
    res = search_configmaps("ns", "target", max_matches=2)
    assert res["match_count"] == 2
    assert res["truncated"] is True


def test_search_configmaps_empty_query():
    _use(items=[JENKINS_CM])
    res = search_configmaps("jenkins-legacy", "   ")
    assert res["match_count"] == 0
    assert "error" in res


def test_search_configmaps_clamps_oversized_max_matches():
    # A caller (or the model) asking for a huge max_matches is hard-capped.
    items = [
        {"metadata": {"name": f"cm{i}", "namespace": "ns"}, "data": {"k": "target-value"}}
        for i in range(wrappers._CM_MAX_MATCHES_CAP + 30)
    ]
    _use(items=items)
    res = search_configmaps("ns", "target", max_matches=100000)
    assert res["match_count"] <= wrappers._CM_MAX_MATCHES_CAP
    assert res["truncated"] is True
