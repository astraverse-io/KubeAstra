"""Tests for tool result summarization (blueprint §1).

Covers the acceptance criteria from the blueprint:
  1. Given 30 identical INFO heartbeat lines + 3 ERROR lines, summarizer collapses
     heartbeats to (xN=30) and preserves all 3 errors.
  2. Given a kubectl events list with 5 Normal/Pulling and 1 Warning/BackOff×42,
     summarizer drops the Normals and surfaces the BackOff with its count.
  3. Given a `describe pod` output, summarizer keeps Containers (with
     State/Reason/ExitCode) and drops Annotations, Tolerations, Volumes.
  4. Tool return dict still contains the original logs/raw_output field unchanged.
  5. With ENABLE_LOG_SUMMARIZATION=false, return dict has no `*_summary` fields.
  6. With LOG_SUMMARIZATION_USE_LLM=false, summary_method == "heuristic" and no
     LLM call is made.
"""

import os
import sys
from typing import Any
from unittest.mock import patch

import pytest

# Ensure mcp/ is on the path
MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


@pytest.fixture
def enable_summarization(monkeypatch):
    """Enable summarization (heuristic-only by default) for the test."""
    monkeypatch.setenv("ENABLE_LOG_SUMMARIZATION", "true")
    monkeypatch.setenv("LOG_SUMMARIZATION_USE_LLM", "false")
    # Threshold low enough that test inputs trigger summarization.
    monkeypatch.setenv("LOG_SUMMARIZATION_THRESHOLD_BYTES", "100")
    # Clear settings cache so the new env vars take effect.
    from config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def disable_summarization(monkeypatch):
    """Explicitly disable summarization."""
    monkeypatch.setenv("ENABLE_LOG_SUMMARIZATION", "false")
    from config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── Logs heuristic ───────────────────────────────────────────────────────────

class TestLogsHeuristic:
    """Acceptance criterion #1: dedupe heartbeats, preserve errors."""

    def test_collapses_duplicate_heartbeats(self, enable_summarization):
        from services.summarizer import summarize_logs
        # 30 identical heartbeats + 3 distinct errors
        heartbeats = "\n".join(["2026-05-15T10:00:00 INFO heartbeat"] * 30)
        errors = (
            "\n2026-05-15T10:00:31 FATAL container killed by oom-killer (memory limit 512Mi)"
            "\n2026-05-15T10:00:32 FATAL Java heap OutOfMemoryError"
            "\n2026-05-15T10:00:33 ERROR ConnectionRefused: dial tcp 10.0.0.1:6379"
        )
        text = heartbeats + errors

        result = summarize_logs(text)
        assert result.method == "heuristic"
        # All three error signatures must survive
        assert "oom-killer" in result.summary
        assert "OutOfMemoryError" in result.summary
        assert "ConnectionRefused" in result.summary
        # Heartbeats must be collapsed (we keep one + the count)
        assert "(xN=30)" in result.summary
        # Stats track the dedup
        assert result.stats.duplicates_collapsed >= 29

    def test_preserves_distinct_error_signatures(self, enable_summarization):
        from services.summarizer import summarize_logs
        # 5 distinct error patterns mixed with 100 noise lines
        noise = "\n".join([f"2026-05-15T10:00:{i:02d} INFO ok" for i in range(60)])
        text = (
            noise
            + "\n2026-05-15T10:01:00 ERROR ConnectionRefused to redis:6379"
            + "\n2026-05-15T10:01:01 ERROR dial tcp 10.0.0.1:6379: i/o timeout"
            + "\n2026-05-15T10:01:02 ERROR 503 upstream connect error"
            + "\n2026-05-15T10:01:03 ERROR context deadline exceeded"
            + "\n2026-05-15T10:01:04 ERROR TLS handshake timeout"
        )

        result = summarize_logs(text)
        # All 5 distinct signatures must be in the summary
        for signature in ("ConnectionRefused", "dial tcp", "503 upstream",
                          "context deadline", "TLS handshake"):
            assert signature in result.summary, f"missing signature: {signature}"

    def test_strips_ansi_codes(self, enable_summarization):
        from services.summarizer import summarize_logs
        # Build a payload larger than the threshold so summarization runs.
        text = (
            "\x1b[31mERROR\x1b[0m something failed\n"
            + "\n".join([f"INFO line {i}" for i in range(40)])
        )

        result = summarize_logs(text)
        assert "\x1b[" not in result.summary  # no ANSI escapes
        assert "ERROR" in result.summary
        assert "something failed" in result.summary

    def test_heuristic_only_when_use_llm_false(self, enable_summarization):
        """Acceptance criterion #6 — no LLM call when use_llm=false.

        The enable_summarization fixture sets LOG_SUMMARIZATION_USE_LLM=false.
        We verify both that method == "heuristic" and that the LLM polish
        function itself is never reached (mock the underlying call).
        """
        from services.summarizer import summarize_logs
        from services.summarizer import logs as logs_module

        text = "\n".join([f"line {i} ERROR something" for i in range(50)])

        # Patch the module-level _llm_polish to fail loudly if invoked.
        with patch.object(logs_module, "_llm_polish",
                          side_effect=AssertionError("LLM polish should not run")) as mock_polish:
            result = summarize_logs(text)

        assert result.method == "heuristic"
        mock_polish.assert_not_called()


# ── Events heuristic ─────────────────────────────────────────────────────────

class TestEventsHeuristic:
    """Acceptance criterion #2: drop noisy Normals, surface Warnings."""

    def _make_events(self):
        """Build 5 Normal/Pulling + 1 Warning/BackOff×42 events."""
        events = []
        # 5 routine Normal events that should be dropped
        for i in range(5):
            events.append({
                "type": "Normal",
                "reason": "Pulling",
                "message": f"Pulling image alpine:latest (attempt {i})",
                "count": 1,
                "involvedObject": {"kind": "Pod", "name": f"pod-{i}"},
                "last_timestamp": f"2026-05-15T10:0{i}:00Z",
            })
        # 1 Warning that must survive
        events.append({
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container app in pod payment-service",
            "count": 42,
            "involvedObject": {"kind": "Pod", "name": "payment-service-xyz"},
            "last_timestamp": "2026-05-15T10:10:00Z",
        })
        # Make sure total bytes_in clears the threshold so summarization runs.
        # Pad with additional Normal events.
        for i in range(20):
            events.append({
                "type": "Normal",
                "reason": "Pulled",
                "message": "Successfully pulled image",
                "count": 1,
                "involvedObject": {"kind": "Pod", "name": f"other-pod-{i}"},
                "last_timestamp": f"2026-05-15T09:0{i:02d}:00Z",
            })
        return events

    def test_drops_normal_pulling_events(self, enable_summarization):
        from services.summarizer import summarize_events
        events = self._make_events()

        result = summarize_events(events)
        assert result.method == "heuristic"
        # Normal/Pulling should be dropped — no "Pulling" line in summary
        # (the dropped-count footer mentions the number, but no per-cluster line)
        lines = result.summary.split("\n")
        cluster_lines = [l for l in lines if l.startswith("[")]
        for line in cluster_lines:
            assert "Pulling" not in line, f"Pulling cluster leaked: {line}"

    def test_surfaces_warning_with_count(self, enable_summarization):
        from services.summarizer import summarize_events
        events = self._make_events()

        result = summarize_events(events)
        # The Warning/BackOff cluster must appear with its ×42 count
        assert "BackOff" in result.summary
        assert "×42" in result.summary
        # Warning marker
        assert "[Warning]" in result.summary

    def test_warnings_sort_before_normals(self, enable_summarization):
        from services.summarizer import summarize_events
        # Mix Warning + non-noisy Normal events
        events = [
            {"type": "Normal", "reason": "RegularEvent", "message": "ok",
             "count": 1, "involvedObject": {"kind": "Pod", "name": "p1"}},
            {"type": "Warning", "reason": "BackOff", "message": "crash",
             "count": 5, "involvedObject": {"kind": "Pod", "name": "p2"}},
        ] * 20  # Multiply to clear byte threshold

        result = summarize_events(events)
        # Warning line should appear before Normal line
        lines = [l for l in result.summary.split("\n") if l.startswith("[")]
        warn_idx = next(i for i, l in enumerate(lines) if "[Warning]" in l)
        normal_idx = next(i for i, l in enumerate(lines) if "[Normal]" in l)
        assert warn_idx < normal_idx, "Warning should sort before Normal"


# ── Describe heuristic ───────────────────────────────────────────────────────

class TestDescribeHeuristic:
    """Acceptance criterion #3: keep Containers, drop Annotations/Tolerations/Volumes."""

    def _sample_describe(self) -> str:
        return """Name:             payment-service-55f8664785-gkpzz
Namespace:        demo
Priority:         0
Service Account:  default
Node:             worker/172.19.0.3
Start Time:       Wed, 15 May 2026 13:30:19 +0000
Labels:           app=payment-service
                  pod-template-hash=55f8664785
Annotations:      kubernetes.io/created-by: controller
                  prometheus.io/scrape: "true"
                  some.other/annotation: very-long-value-that-noone-reads
Status:           Running
IP:               10.244.1.3
IPs:
  IP:           10.244.1.3
Controlled By:  ReplicaSet/payment-service-55f8664785
Containers:
  app:
    Container ID:  containerd://2e0df306533fd99bda1a77c0d90b12fa351dc388
    Image:         busybox:1.36
    State:          Waiting
      Reason:       CrashLoopBackOff
    Last State:     Terminated
      Reason:       Error
      Exit Code:    1
    Ready:          False
    Restart Count:  8
Conditions:
  Type                        Status
  Ready                       False
Volumes:
  kube-api-access-6t5hk:
    Type:                    Projected
    TokenExpirationSeconds:  3607
Tolerations:                 node.kubernetes.io/not-ready:NoExecute op=Exists for 300s
                             node.kubernetes.io/unreachable:NoExecute op=Exists for 300s
Events:
  Type     Reason            Age   From     Message
  ----     ------            ----  ----     -------
  Warning  BackOff           22s   kubelet  Back-off restarting failed container
""" * 3  # repeat so we clear the byte threshold

    def test_keeps_containers_section(self, enable_summarization):
        from services.summarizer import summarize_describe
        result = summarize_describe(self._sample_describe())
        assert result.method == "heuristic"
        # The Containers section must survive intact — State/Reason/ExitCode
        assert "Containers:" in result.summary
        assert "CrashLoopBackOff" in result.summary
        assert "Exit Code:" in result.summary
        assert "Restart Count:  8" in result.summary

    def test_drops_annotations_tolerations_volumes(self, enable_summarization):
        from services.summarizer import summarize_describe
        result = summarize_describe(self._sample_describe())

        # These section *bodies* must not appear in the summary
        assert "prometheus.io/scrape" not in result.summary
        assert "very-long-value-that-noone-reads" not in result.summary
        assert "node.kubernetes.io/not-ready:NoExecute" not in result.summary
        assert "TokenExpirationSeconds" not in result.summary

    def test_keeps_status_and_events(self, enable_summarization):
        from services.summarizer import summarize_describe
        result = summarize_describe(self._sample_describe())
        assert "Status:" in result.summary
        assert "Running" in result.summary
        # Events section signature
        assert "Events:" in result.summary
        assert "BackOff" in result.summary


# ── Additive return shape (acceptance #4, #5) ───────────────────────────────

class TestAdditiveShape:
    """Acceptance criteria #4 and #5 — original fields preserved, gated by feature flag."""

    def test_summary_off_means_no_new_fields(self, disable_summarization):
        """Acceptance criterion #5: with feature off, no `*_summary` fields appear."""
        from services.summarizer import summarize_logs

        text = "\n".join([f"line {i}" for i in range(200)])
        result = summarize_logs(text)
        # Method = "none" means caller skips adding fields entirely.
        assert result.method == "none"
        assert result.summary == ""

    def test_summary_on_preserves_original_logs(self, enable_summarization):
        """Acceptance #4: the original `logs` field stays untouched.

        Simulated here by checking that _attach_logs_summary mutates the
        response with new keys WITHOUT removing existing ones.
        """
        from k8s.wrappers import _attach_logs_summary

        # Build a response dict shaped like get_pod_logs returns
        response = {
            "namespace": "demo",
            "pod_name": "payment-service-xyz",
            "logs": "\n".join([f"line {i} ERROR something" for i in range(60)]),
            "success": True,
        }
        original_logs = response["logs"]
        original_keys = set(response.keys())

        _attach_logs_summary(response, response["logs"])

        # Original fields must all still be present
        assert original_keys <= set(response.keys())
        # The raw logs field must be byte-identical
        assert response["logs"] == original_logs
        # New summary fields must be present
        assert "logs_summary" in response
        assert "summary_method" in response
        assert "summary_stats" in response

    def test_below_threshold_no_summary(self, enable_summarization):
        """Inputs below the byte threshold skip summarization entirely."""
        from services.summarizer import summarize_logs

        # Tiny input — below LOG_SUMMARIZATION_THRESHOLD_BYTES (100 in fixture).
        result = summarize_logs("short log")
        assert result.method == "none"
