"""A settings change must reach the kubectl layer, not stop at the door.

`mcp/config/settings.py` builds a process-wide singleton at import time:

    @lru_cache()
    def get_settings() -> Settings: ...
    settings = get_settings()

Two ways to consume it, and they behave differently once anything calls
`get_settings.cache_clear()` — which the desktop settings router does on every
save, so that newly-exported env vars take effect:

  from config.settings import get_settings   ->  sees the new instance
  from config.settings import settings       ->  bound to the old one, forever

The whole of `mcp/k8s/` used the second form. Nothing was broken by it, because
the desktop Settings screen happens to write only LLM and embeddings keys and
the kubectl layer happens to read none of them. That is luck, not design: the
frozen set includes `require_destructive_confirmation` and
`enable_recovery_operations`. The day a "require confirmation for destructive
operations" toggle is added to that screen, it would appear to work, the router
would clear the cache, and the runner would keep permitting what the operator
just forbade — silently.

These tests fail against the old import style.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
for p in (str(BACKEND_DIR), str(MCP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config.settings import get_settings  # noqa: E402


@pytest.fixture
def fresh_settings(monkeypatch):
    """Simulate a settings save: change the env, drop the cache, hand back the new instance."""

    def _apply(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()
        return get_settings()

    yield _apply
    # Leave no stale instance behind for the rest of the session.
    get_settings.cache_clear()


def test_a_new_runner_picks_up_a_changed_timeout(fresh_settings):
    """Runners read config in __init__, so a later runner must see the new value."""
    from k8s.kubectl_runner import KubectlRunner

    before = KubectlRunner().timeout
    fresh_settings(KUBECTL_TIMEOUT_SECONDS=before + 17)

    assert KubectlRunner().timeout == before + 17


def test_the_helm_runner_picks_it_up_too(fresh_settings):
    from k8s.helm_runner import HelmRunner

    before = HelmRunner().timeout
    fresh_settings(KUBECTL_TIMEOUT_SECONDS=before + 23)

    assert HelmRunner().timeout == before + 23


def test_a_changed_json_cap_reaches_run_json(fresh_settings, monkeypatch):
    """`run_json` reads the cap per call, so it must not need a new runner at all."""
    from k8s.kubectl_runner import KubectlResult, KubectlRunner

    runner = KubectlRunner()
    fresh_settings(MAX_JSON_BYTES=33 * 1024 * 1024)
    seen = {}

    def capture(args, namespace=None, max_output=None, **kwargs):
        seen["max_output"] = max_output
        return KubectlResult(
            stdout='{"ok": true}',
            stderr="",
            returncode=0,
            command=["kubectl"],
            duration_seconds=0.0,
            truncated=False,
        )

    monkeypatch.setattr(runner, "run", capture)
    runner.run_json(["get", "pods"])

    assert seen["max_output"] == 33 * 1024 * 1024


def test_a_tightened_namespace_allowlist_takes_effect(fresh_settings):
    """Cluster scoping is read per call — the layer must not answer from a stale copy."""
    from k8s import validators

    fresh_settings(ALLOWED_NAMESPACES="only-this-one")

    assert validators.get_allowed_namespaces() == ["only-this-one"]


def test_the_safety_gates_are_reachable(fresh_settings):
    """The gates are the reason this matters: they must never read stale.

    Asserts against what `wrappers` itself resolves, not against the object the
    fixture hands back — the fixture's object is fresh by construction, so
    checking it would prove nothing about the module.
    """
    from k8s import wrappers

    fresh_settings(ENABLE_RECOVERY_OPERATIONS="false")
    assert wrappers.get_settings().enable_recovery_operations is False

    fresh_settings(ENABLE_RECOVERY_OPERATIONS="true")
    assert wrappers.get_settings().enable_recovery_operations is True


def test_no_module_in_the_kubectl_layer_binds_the_frozen_singleton():
    """The guard that keeps this from silently coming back.

    A future edit adding `from config.settings import settings` to this layer
    reintroduces the whole problem with no visible symptom. Catch it here.
    """
    offenders = []
    for path in sorted((MCP_DIR / "k8s").glob("*.py")) + [MCP_DIR / "mcp_server" / "runtime.py"]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("from config.settings import") and "get_settings" not in stripped:
                offenders.append(f"{path.name}:{lineno}: {stripped}")

    assert not offenders, (
        "These bind the import-time Settings instance, so a settings change "
        "cannot reach them. Import `get_settings` and call it instead:\n  "
        + "\n  ".join(offenders)
    )
