"""A truncated JSON document must not be reported as kubectl's fault.

`run_json` caps output so JSON is never cut mid-stream — but when the cap was
actually hit, nothing checked. The truncated text went to `json.loads`, which
failed, and the error read:

    Failed to parse kubectl JSON output: Expecting ',' delimiter ...

That says kubectl emitted malformed JSON. It did not: it emitted valid JSON
that this code then cut in half. The distinction matters because the two have
opposite fixes — one is "the cluster is broken", the other is "ask for less" —
and on a big enough cluster the wrong one sends you looking at the cluster.
"""

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.kubectl_runner import KubectlError, KubectlResult, KubectlRunner  # noqa: E402


def _result(stdout: str, truncated: bool) -> KubectlResult:
    return KubectlResult(
        stdout=stdout,
        stderr="",
        returncode=0,
        command=["kubectl", "get", "pods", "-o", "json"],
        duration_seconds=0.1,
        truncated=truncated,
    )


@pytest.fixture
def runner():
    return KubectlRunner()


def test_truncated_json_says_the_query_was_too_broad(runner, monkeypatch):
    """The bug: this used to surface as a kubectl parse failure."""
    cut_in_half = '{"items": [{"metadata": {"name": "pod-1"}}, {"metadata"'
    monkeypatch.setattr(runner, "run", lambda *a, **k: _result(cut_in_half, True))

    with pytest.raises(KubectlError) as excinfo:
        runner.run_json(["get", "pods", "-o", "json"])

    message = str(excinfo.value)
    assert "too broad" in message, message
    assert "Nothing is wrong with the cluster" in message, message
    # And it must not blame the parser for our own truncation.
    assert "Failed to parse" not in message, message


def test_the_truncation_error_says_how_to_proceed(runner, monkeypatch):
    monkeypatch.setattr(runner, "run", lambda *a, **k: _result("{trunc", True))

    with pytest.raises(KubectlError) as excinfo:
        runner.run_json(["get", "pods"])

    message = str(excinfo.value)
    assert "namespace" in message and "label selector" in message, message
    assert "MAX_JSON_BYTES" in message, "should name the setting that raises the cap"


def test_genuinely_malformed_json_still_reports_a_parse_error(runner, monkeypatch):
    """The old message is still right when kubectl really did emit junk."""
    monkeypatch.setattr(runner, "run", lambda *a, **k: _result("not json at all", False))

    with pytest.raises(KubectlError) as excinfo:
        runner.run_json(["get", "pods"])

    assert "Failed to parse kubectl JSON output" in str(excinfo.value)


def test_untruncated_json_parses_normally(runner, monkeypatch):
    monkeypatch.setattr(
        runner, "run", lambda *a, **k: _result('{"items": [{"name": "pod-1"}]}', False)
    )

    assert runner.run_json(["get", "pods"]) == {"items": [{"name": "pod-1"}]}


def test_the_cap_is_configurable(runner, monkeypatch):
    """Cluster size varies by orders of magnitude; 10 MB is a default, not a law."""
    from config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "max_json_bytes", 25 * 1024 * 1024)
    seen = {}

    def capture(args, namespace=None, max_output=None, **kwargs):
        seen["max_output"] = max_output
        return _result('{"ok": true}', False)

    monkeypatch.setattr(runner, "run", capture)
    runner.run_json(["get", "pods"])

    assert seen["max_output"] == 25 * 1024 * 1024
