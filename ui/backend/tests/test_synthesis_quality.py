import pytest
import sys
from pathlib import Path
import json

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from services.tool_envelope import (
    ToolEnvelope,
    VerdictBand,
    InventoryEvidence,
    DiagnosticEvidence,
    StatusCheckEvidence,
    LogAnalysisEvidence,
    ConfidenceSignals,
    ToolMeta,
    make_events_envelope,
    make_investigate_pod_envelope,
)
from react import (
    _dedupe_envelopes,
    _build_causality_chain,
    _build_finalize_system,
    _build_finalize_prompt,
    build_evidence_priority_summary,
    build_envelope_retrieval_context,
    build_synthesis_breakdown,
    parse_markdown_synthesis,
    _extract_actions_from_steps,
    _sanitize_user_facing_answer,
    _with_root_cause_summary,
    ReActStep
)


def test_tool_envelope_raw_excerpt_truncation():
    """Verify that raw_excerpt is strictly capped at 2048 characters with a warning."""
    long_excerpt = "x" * 3000
    meta = ToolMeta(tool="test_tool", params={"key": "val"})
    evidence = InventoryEvidence(items=[], total_count=0)
    
    envelope = ToolEnvelope(
        verdict="Healthy",
        evidence=evidence,
        raw_excerpt=long_excerpt,
        confidence_signals=ConfidenceSignals(events_analyzed=5, data_completeness="complete"),
        _meta=meta
    )
    
    assert len(envelope.raw_excerpt) == 2048
    assert envelope.raw_excerpt.endswith("...")
    assert envelope.raw_excerpt[:2045] == "x" * 2045


def test_dedupe_envelopes_revised_flag():
    """Verify deduplication keeps the latest run and sets revised_from_step
    when evidence/verdict changed — without mutating the original envelope's
    params dict (revised_from_step lives on ToolMeta, not on params)."""
    meta1 = ToolMeta(tool="investigate_pod", params={"pod_name": "app", "namespace": "default"})
    ev1 = DiagnosticEvidence(primary_target={"pod": "app"}, failure_modes=[{"mode": "CrashLoopBackOff", "severity": "critical"}])
    env1 = ToolEnvelope(
        verdict="Unhealthy",
        evidence=ev1,
        raw_excerpt="logs...",
        _meta=meta1
    )

    # Re-run: pod is now healthy
    meta2 = ToolMeta(tool="investigate_pod", params={"pod_name": "app", "namespace": "default"})
    ev2 = DiagnosticEvidence(primary_target={"pod": "app"}, failure_modes=[])
    env2 = ToolEnvelope(
        verdict="Healthy",
        evidence=ev2,
        raw_excerpt="all clean",
        _meta=meta2
    )

    # Snapshot original params dicts for the mutation check below.
    original_env2_params = dict(env2.meta.params)

    envs_with_steps = [
        (env1, 1),
        (env2, 2)
    ]

    deduped = _dedupe_envelopes(envs_with_steps)

    assert len(deduped) == 1
    result_env = deduped[0]
    assert result_env.verdict == "Healthy"

    # I1: revised_from_step lives on meta, NOT injected into params.
    assert result_env.meta.revised_from_step == 1
    assert "revised_from_step" not in result_env.meta.params

    # I1: original envelope is not mutated by dedup (deep-copy invariant).
    assert env2.meta.revised_from_step is None
    assert env2.meta.params == original_env2_params


def test_build_finalize_prompt_empty_envelopes_fallback():
    """Verify that _build_finalize_prompt falls back correctly when envelopes list is empty."""
    empty = _build_finalize_prompt("any question", "")
    assert "No tool output gathered" in empty

    # With envelopes present, the fallback message must NOT appear.
    meta = ToolMeta(tool="get_namespaces", params={})
    env = ToolEnvelope(
        verdict="n/a",
        evidence=InventoryEvidence(items=[], total_count=0),
        _meta=meta
    )
    prompt = _build_finalize_prompt(
        "any question",
        "",
        envelopes=[env],
    )
    assert "No tool output gathered" not in prompt
    assert "get_namespaces" in prompt


def test_build_causality_chain_keeps_thought_reference_fallback():
    """Verify thought step references still work when no deterministic link exists."""
    steps = [
        ReActStep(iteration=1, thought="Let's find the pods", action="get_pods", action_params={"namespace": "default"}),
        ReActStep(iteration=2, thought="In step 1 I saw a failing pod, let's investigate", action="investigate_pod", action_params={"pod_name": "app"}),
        ReActStep(iteration=3, thought="This is unrelated, let's list nodes", action="get_nodes", action_params={}),
        ReActStep(iteration=4, thought="Now let's give the answer", action="answer", action_params={})
    ]
    
    chain = _build_causality_chain(steps)
    
    assert len(chain) == 3  # answer step excluded
    assert chain[0]["trigger"] == "user_query"
    assert chain[1]["trigger"] == "step_1"
    assert chain[2]["trigger"] == "agent_chose_next"


def test_build_causality_chain_links_pod_inventory_to_investigation():
    pod_env = ToolEnvelope(
        verdict="n/a",
        evidence=InventoryEvidence(
            items=[{"name": "my-kafka-0", "namespace": "infrastructure", "status": "CrashLoopBackOff"}],
            total_count=1,
            filter_criteria={"namespace": "infrastructure"},
        ),
        _meta=ToolMeta(tool="get_pods", params={"namespace": "infrastructure"}),
    )
    steps = [
        ReActStep(iteration=1, thought="List pods", action="get_pods", action_params={"namespace": "infrastructure"}, envelope=pod_env),
        ReActStep(
            iteration=2,
            thought="Investigate failing pod",
            action="investigate_pod",
            action_params={"namespace": "infrastructure", "pod_name": "my-kafka-0"},
        ),
    ]

    chain = _build_causality_chain(steps)

    assert chain[1]["trigger"] == "step_1"
    assert chain[1]["trigger_path"] == "evidence.items[0]"
    assert "my-kafka-0" in chain[1]["trigger_reason"]


def test_build_causality_chain_links_pod_dependency_to_service_lookup():
    pod_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "my-kafka-0", "namespace": "infrastructure"},
            failure_modes=[],
            contributing_factors=[
                {
                    "service": "zookeeper-kube-upd-cs",
                    "namespace": "infrastructure",
                    "service_exists": False,
                    "evidence_priority": "dependency_check",
                }
            ],
        ),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "infrastructure", "pod_name": "my-kafka-0"}),
    )
    steps = [
        ReActStep(iteration=1, thought="Investigate pod", action="investigate_pod", action_params={"namespace": "infrastructure", "pod_name": "my-kafka-0"}, envelope=pod_env),
        ReActStep(iteration=2, thought="Check service", action="get_service", action_params={"namespace": "infrastructure", "service_name": "zookeeper-kube-upd-cs"}),
    ]

    chain = _build_causality_chain(steps)

    assert chain[1]["trigger"] == "step_1"
    assert chain[1]["trigger_path"] == "evidence.contributing_factors[0]"
    assert "zookeeper-kube-upd-cs" in chain[1]["trigger_reason"]


def test_build_causality_chain_links_pod_dependency_to_endpoints_lookup():
    pod_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[],
            contributing_factors=[
                {
                    "target": "backend-svc:8080",
                    "namespace": "apps",
                    "service_exists": True,
                    "evidence_priority": "dependency_check",
                }
            ],
        ),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "apps", "pod_name": "api-0"}),
    )
    steps = [
        ReActStep(iteration=1, thought="Investigate pod", action="investigate_pod", action_params={"namespace": "apps", "pod_name": "api-0"}, envelope=pod_env),
        ReActStep(iteration=2, thought="Check endpoints", action="get_endpoints", action_params={"namespace": "apps", "service_name": "backend-svc"}),
    ]

    chain = _build_causality_chain(steps)

    assert chain[1]["trigger"] == "step_1"
    assert chain[1]["trigger_path"] == "evidence.contributing_factors[0]"
    assert "backend-svc" in chain[1]["trigger_reason"]


def test_build_causality_chain_links_fqdn_service_dependency():
    pod_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "my-kafka-0", "namespace": "infrastructure"},
            failure_modes=[],
            contributing_factors=[
                {
                    "target": "zookeeper-kube-upd-cs.infrastructure.svc.cluster.local:2181",
                    "namespace": "infrastructure",
                    "service_exists": False,
                    "evidence_priority": "dependency_check",
                }
            ],
        ),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "infrastructure", "pod_name": "my-kafka-0"}),
    )
    steps = [
        ReActStep(iteration=1, thought="Investigate pod", action="investigate_pod", action_params={"namespace": "infrastructure", "pod_name": "my-kafka-0"}, envelope=pod_env),
        ReActStep(iteration=2, thought="Check service", action="get_service", action_params={"namespace": "infrastructure", "service_name": "zookeeper-kube-upd-cs"}),
    ]

    chain = _build_causality_chain(steps)

    assert chain[1]["trigger"] == "step_1"
    assert chain[1]["trigger_path"] == "evidence.contributing_factors[0]"


def test_build_causality_chain_links_raw_observation_fallbacks():
    steps = [
        ReActStep(
            iteration=1,
            thought="List pods",
            action="get_pods",
            action_params={"namespace": "apps"},
            observation=json.dumps({"pods": [{"name": "api-0", "namespace": "apps", "status": "CrashLoopBackOff"}]}),
        ),
        ReActStep(
            iteration=2,
            thought="Investigate pod",
            action="investigate_pod",
            action_params={"namespace": "apps", "pod_name": "api-0"},
            observation=json.dumps({
                "evidence_summary": {
                    "dependency_checks": [
                        {"target": "backend-svc.apps.svc.cluster.local:8080", "namespace": "apps"}
                    ]
                }
            }),
        ),
        ReActStep(iteration=3, thought="Check endpoints", action="get_endpoints", action_params={"namespace": "apps", "service_name": "backend-svc"}),
    ]

    chain = _build_causality_chain(steps)

    assert chain[1]["trigger"] == "step_1"
    assert chain[1]["trigger_path"] == "pods[0]"
    assert chain[2]["trigger"] == "step_2"
    assert chain[2]["trigger_path"] == "evidence_summary.dependency_checks[0]"


def test_build_causality_chain_links_node_event_to_node_investigation():
    events_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=LogAnalysisEvidence(
            source={"namespace": "apps"},
            severity_counts={"warning": 1},
            top_messages=[
                {
                    "reason": "NodeHasDiskPressure",
                    "message": "Node gke-dev-node-1 has disk pressure",
                    "object": "Node/gke-dev-node-1",
                    "evidence_priority": "primary_failure",
                }
            ],
            most_recent_critical={
                "reason": "NodeHasDiskPressure",
                "message": "Node gke-dev-node-1 has disk pressure",
                "involved_object": {"kind": "Node", "name": "gke-dev-node-1"},
                "evidence_priority": "primary_failure",
            },
        ),
        _meta=ToolMeta(tool="get_events", params={"namespace": "apps"}),
    )
    steps = [
        ReActStep(iteration=1, thought="Check events", action="get_events", action_params={"namespace": "apps"}, envelope=events_env),
        ReActStep(iteration=2, thought="Investigate node", action="investigate_node", action_params={"node_name": "gke-dev-node-1"}),
    ]

    chain = _build_causality_chain(steps)

    assert chain[1]["trigger"] == "step_1"
    assert chain[1]["trigger_path"] == "evidence.most_recent_critical"
    assert "gke-dev-node-1" in chain[1]["trigger_reason"]


def test_build_causality_chain_does_not_link_partial_node_name_mentions():
    events_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=LogAnalysisEvidence(
            source={"namespace": "apps"},
            severity_counts={"warning": 1},
            top_messages=[
                {
                    "reason": "NodeHasDiskPressure",
                    "message": "Node node-10 has disk pressure",
                    "evidence_priority": "primary_failure",
                }
            ],
        ),
        _meta=ToolMeta(tool="get_events", params={"namespace": "apps"}),
    )
    steps = [
        ReActStep(iteration=1, thought="Check events", action="get_events", action_params={"namespace": "apps"}, envelope=events_env),
        ReActStep(iteration=2, thought="Investigate node", action="investigate_node", action_params={"node_name": "node-1"}),
    ]

    chain = _build_causality_chain(steps)

    assert chain[1]["trigger"] == "agent_chose_next"
    assert "trigger_path" not in chain[1]


def test_build_envelope_retrieval_context_uses_evidence_only():
    meta = ToolMeta(tool="investigate_pod", params={"namespace": "apps", "pod_name": "api-0"})
    env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"namespace": "apps", "pod_name": "api-0"},
            failure_modes=[{"type": "verified_root_cause", "root_cause": "missing service"}],
        ),
        raw_excerpt="RAW LOG LINE THAT SHOULD NOT BE USED FOR FAITHFULNESS",
        _meta=meta,
    )
    steps = [
        ReActStep(
            iteration=1,
            thought="Investigate pod",
            action="investigate_pod",
            action_params={"namespace": "apps", "pod_name": "api-0"},
            envelope=env,
        )
    ]

    context = build_envelope_retrieval_context(steps)

    assert len(context) == 1
    assert "missing service" in context[0]
    assert "primary_target" in context[0]
    assert "RAW LOG LINE" not in context[0]
    assert "raw_excerpt" not in context[0]


def test_build_finalize_system_injects_schemas():
    """Verify dynamic schema injection handles all evidence sub-types."""
    meta = ToolMeta(tool="get_pods", params={})
    env1 = ToolEnvelope(
        verdict="n/a",
        evidence=InventoryEvidence(items=[], total_count=0),
        _meta=meta
    )
    
    system_prompt = _build_finalize_system([env1])
    assert "InventoryEvidence" in system_prompt
    assert "DiagnosticEvidence" not in system_prompt


def test_build_finalize_system_keeps_evidence_user_readable():
    """The final answer should not expose internal envelope array paths."""
    system_prompt = _build_finalize_system([])

    assert "DO NOT print internal field paths" in system_prompt
    assert "envelope[i].evidence.<path>" not in system_prompt
    assert "envelope[0].evidence" not in system_prompt


def test_sanitize_user_facing_answer_removes_internal_envelope_paths():
    text = """
# Evidence
* envelope[0].evidence.items[0] - api-0 is CrashLoopBackOff
* envelope[1].evidence.timeline[2]: restart count is 42
* `envelope[i].evidence.contributing_factors[0]` — zookeeper service missing
* envelope[0] status is unhealthy
* 'envelope[0]' quoted field path should disappear
* "envelope[0]" double quoted field path should disappear
* (envelope[0]) parenthesized field path should disappear
* (see envelope[0].evidence.items[0]) parenthesized field path should disappear
* [envelope[0]] bracketed field path should disappear
"""

    cleaned = _sanitize_user_facing_answer(text)

    assert "envelope[" not in cleaned
    assert "api-0 is CrashLoopBackOff" in cleaned
    assert "restart count is 42" in cleaned
    assert "zookeeper service missing" in cleaned
    assert "status is unhealthy" in cleaned
    assert "* : restart" not in cleaned


def test_investigate_pod_envelope_promotes_verified_root_cause():
    """Verified deterministic root cause must outrank secondary/advisory details."""
    env = make_investigate_pod_envelope(
        {
            "success": True,
            "pod_name": "my-kafka-0",
            "namespace": "infrastructure",
            "classification": {
                "mode": "CrashLoopBackOff",
                "container": "kafka-broker",
            },
            "evidence_summary": {
                "suspected_root_cause": (
                    "Kafka is configured to use ZooKeeper service `zookeeper-kube-upd-cs`, "
                    "but that service does not exist in namespace `infrastructure`."
                ),
                "suggested_fix": (
                    "Restore the missing ZooKeeper service/backing pods, or update "
                    "KAFKA_ZOOKEEPER_CONNECT to the correct ZooKeeper service DNS name."
                ),
                "dependency_checks": [
                    {
                        "type": "zookeeper",
                        "target": "zookeeper-kube-upd-cs:2181",
                        "service": "zookeeper-kube-upd-cs",
                        "namespace": "infrastructure",
                        "service_exists": False,
                    }
                ],
                "secondary_issues": [
                    {
                        "container": "prometheus-jmx-exporter",
                        "reason": "CrashLoopBackOff",
                        "evidence": "Error: Unable to access jarfile jmx_prometheus_standalone.jar",
                    }
                ],
            },
            "container_log_findings": [
                {
                    "container": "kafka-broker",
                    "reason": "Error",
                    "restart_count": 4937,
                    "logs_previous": {
                        "excerpt": "Cannot connect to zookeeper-kube-upd-cs:2181",
                    },
                }
            ],
        },
        {"namespace": "infrastructure", "pod_name": "my-kafka-0", "use_ai": True},
        duration_ms=25,
    )

    root = env.evidence.failure_modes[0]
    assert root["type"] == "verified_root_cause"
    assert root["evidence_priority"] == "verified_root_cause"
    assert root["priority_label"] == "Verified root cause"
    assert root["source"] == "deterministic_investigation"
    assert "zookeeper-kube-upd-cs" in root["root_cause"]
    assert "KAFKA_ZOOKEEPER_CONNECT" in root["suggested_fix"]

    secondary = env.evidence.contributing_factors[-1]
    assert secondary["container"] == "prometheus-jmx-exporter"
    assert secondary["evidence_priority"] == "secondary_issue"

    priorities = [f["evidence_priority"] for f in env.evidence.contributing_factors]
    assert priorities == ["dependency_check", "container_log_finding", "secondary_issue"]


def test_root_cause_summary_contract_from_deterministic_pod_result():
    result = _with_root_cause_summary({
        "pod_name": "my-kafka-0",
        "namespace": "infrastructure",
        "classification": {"mode": "CrashLoopBackOff", "container": "kafka-broker"},
        "evidence_summary": {
            "suspected_root_cause": "Kafka cannot connect to ZooKeeper service.",
            "suggested_fix": "Restore the missing ZooKeeper service.",
            "dependency_checks": [
                {
                    "service": "zookeeper-kube-upd-cs",
                    "namespace": "infrastructure",
                    "service_exists": False,
                    "endpoints_exist": False,
                }
            ],
            "secondary_issues": [
                {
                    "container": "prometheus-jmx-exporter",
                    "reason": "CrashLoopBackOff",
                    "evidence": "Unable to access jarfile.",
                }
            ],
        },
        "container_log_findings": [
            {
                "container": "kafka-broker",
                "reason": "Error",
                "restart_count": 4937,
                "logs_previous": {"excerpt": "Cannot connect to zookeeper-kube-upd-cs:2181"},
            }
        ],
    })

    summary = result["root_cause_summary"]

    assert summary["schema_version"] == "root_cause_summary.v1"
    assert summary["resource_kind"] == "pod"
    assert summary["resource_name"] == "my-kafka-0"
    assert summary["namespace"] == "infrastructure"
    assert summary["root_cause"] == "Kafka cannot connect to ZooKeeper service."
    assert summary["suggested_fix"] == "Restore the missing ZooKeeper service."
    assert summary["confidence"] == 0.95
    assert summary["source_evidence"] == "verified_deterministic_investigation"
    assert summary["executable_actions"] == []
    assert summary["related_resources"][0]["name"] == "zookeeper-kube-upd-cs"
    assert summary["secondary_findings"][0]["container"] == "prometheus-jmx-exporter"


def test_root_cause_summary_contract_from_tool_envelope_result():
    env = make_investigate_pod_envelope(
        {
            "success": True,
            "pod_name": "my-kafka-0",
            "namespace": "infrastructure",
            "classification": {"mode": "CrashLoopBackOff", "container": "kafka-broker"},
            "evidence_summary": {
                "suspected_root_cause": "Kafka cannot connect to ZooKeeper service.",
                "suggested_fix": "Restore the missing ZooKeeper service.",
                "dependency_checks": [
                    {
                        "service": "zookeeper-kube-upd-cs",
                        "namespace": "infrastructure",
                        "service_exists": False,
                    }
                ],
            },
        },
        {"namespace": "infrastructure", "pod_name": "my-kafka-0", "use_ai": True},
        duration_ms=25,
    )

    result = _with_root_cause_summary(env.model_dump(by_alias=True))
    summary = result["root_cause_summary"]

    assert summary["schema_version"] == "root_cause_summary.v1"
    assert summary["resource_name"] == "my-kafka-0"
    assert summary["namespace"] == "infrastructure"
    assert summary["root_cause"] == "Kafka cannot connect to ZooKeeper service."
    assert summary["source_tool"] == "investigate_pod"
    assert summary["related_resources"][0]["name"] == "zookeeper-kube-upd-cs"


def test_root_cause_summary_uncorroborated_suspected_root_is_not_high_confidence():
    result = _with_root_cause_summary({
        "pod_name": "api-0",
        "namespace": "apps",
        "classification": {"mode": "CrashLoopBackOff", "container": "api"},
        "evidence_summary": {
            "suspected_root_cause": "Application exits during startup.",
        },
    })

    summary = result["root_cause_summary"]

    assert summary["confidence"] == 0.75
    assert summary["source_evidence"] == "deterministic_investigation"
    assert "limited" in summary["confidence_reason"]


def test_root_cause_summary_not_created_without_concrete_pod_target():
    result = _with_root_cause_summary({
        "classification": {"mode": "CrashLoopBackOff", "container": "api"},
        "evidence_summary": {
            "suspected_root_cause": "Application exits during startup.",
        },
    })

    assert "root_cause_summary" not in result


def test_evidence_priority_summary_prefers_verified_root_cause():
    env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "my-kafka-0", "namespace": "infrastructure"},
            failure_modes=[
                {
                    "mode": "CrashLoopBackOff",
                    "container": "kafka-broker",
                    "evidence_priority": "primary_failure",
                    "priority_rank": 80,
                    "priority_label": "Primary failure",
                },
                {
                    "type": "verified_root_cause",
                    "root_cause": "ZooKeeper service zookeeper-kube-upd-cs is missing.",
                    "evidence_priority": "verified_root_cause",
                    "priority_rank": 100,
                    "priority_label": "Verified root cause",
                },
            ],
            contributing_factors=[
                {
                    "container": "prometheus-jmx-exporter",
                    "reason": "Unable to access jarfile",
                    "evidence_priority": "secondary_issue",
                    "priority_rank": 40,
                    "priority_label": "Secondary issue",
                },
            ],
        ),
        raw_excerpt="",
        _meta=ToolMeta(tool="investigate_pod", params={"pod_name": "my-kafka-0"}),
    )

    summary = build_evidence_priority_summary([env])

    primary = summary["primary_root_cause"]
    assert primary["priority"] == "verified_root_cause"
    assert primary["label"] == "Verified root cause"
    assert "ZooKeeper service" in primary["summary"]


def test_evidence_priority_summary_promotes_oom_over_generic_crashloop():
    env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[
                {
                    "mode": "CrashLoopBackOff",
                    "container": "api",
                    "evidence_priority": "primary_failure",
                    "priority_rank": 80,
                    "priority_label": "Primary failure",
                },
            ],
            timeline=[
                {
                    "reason": "OOMKilled",
                    "message": "Container api was OOMKilled after exceeding memory limit.",
                    "evidence_priority": "primary_failure",
                    "priority_rank": 80,
                    "priority_label": "Primary failure",
                },
            ],
        ),
        raw_excerpt="",
        _meta=ToolMeta(tool="investigate_pod", params={"pod_name": "api-0"}),
    )

    summary = build_evidence_priority_summary([env])

    primary = summary["primary_root_cause"]
    assert primary["label"] == "Primary failure: OOMKilled"
    assert primary["evidence_path"] == "evidence.timeline[0]"
    assert primary["priority_rank"] == 90


def test_synthesis_breakdown_includes_evidence_priority():
    env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[
                {
                    "type": "verified_root_cause",
                    "root_cause": "The backend service has no ready endpoints.",
                    "evidence_priority": "verified_root_cause",
                    "priority_rank": 100,
                    "priority_label": "Verified root cause",
                }
            ],
        ),
        raw_excerpt="",
        _meta=ToolMeta(tool="investigate_pod", params={"pod_name": "api-0"}),
    )
    step = ReActStep(iteration=1, thought="investigate", action="investigate_pod", envelope=env)
    markdown = """
# Diagnosis
The pod is unhealthy because the backend service has no ready endpoints.

# Evidence
* The backend service has no ready endpoints.

# Recommended Actions
1. Restore a ready backend endpoint.

# Uncertainty
Confidence: high
The deterministic investigation identified the root cause.
"""

    breakdown = build_synthesis_breakdown(markdown, steps=[step])

    assert breakdown["evidence_priority"]["primary_root_cause"]["priority"] == "verified_root_cause"


def test_events_envelope_selects_most_recent_warning_by_timestamp():
    env = make_events_envelope(
        {
            "events": [
                {
                    "type": "Warning",
                    "reason": "OldFailedScheduling",
                    "message": "Older warning",
                    "last_timestamp": "2026-01-01T00:01:00Z",
                    "involved_object": {"kind": "Pod", "name": "api-0"},
                },
                {
                    "type": "Warning",
                    "reason": "NewBackOff",
                    "message": "Newest warning",
                    "last_timestamp": "2026-01-01T00:05:00Z",
                    "involved_object": {"kind": "Pod", "name": "api-0"},
                },
            ],
            "original_count": 2,
            "namespace": "apps",
        },
        {"namespace": "apps"},
        duration_ms=10,
    )

    recent = env.evidence.most_recent_critical
    assert recent["reason"] == "NewBackOff"
    assert recent["priority_label"] == "Most recent critical event"
    assert recent["evidence_priority"] == "primary_failure"


def test_evidence_priority_summary_ignores_inflated_secondary_rank():
    env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[
                {
                    "mode": "CrashLoopBackOff",
                    "evidence_priority": "primary_failure",
                    "priority_rank": 80,
                }
            ],
            contributing_factors=[
                {
                    "container": "sidecar",
                    "reason": "Jarfile missing",
                    "evidence_priority": "secondary_issue",
                    "priority_rank": 999,
                }
            ],
        ),
        raw_excerpt="",
        _meta=ToolMeta(tool="investigate_pod", params={"pod_name": "api-0"}),
    )

    primary = build_evidence_priority_summary([env])["primary_root_cause"]

    assert primary["priority"] == "primary_failure"
    assert primary["priority_rank"] == 80


def test_evidence_priority_summary_clamps_unjustified_ai_advisory_rank():
    env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[
                {
                    "mode": "CrashLoopBackOff",
                    "evidence_priority": "primary_failure",
                    "priority_rank": 80,
                }
            ],
            contributing_factors=[
                {
                    "ai": {"analysis": "Speculative config issue"},
                    "evidence_priority": "ai_advisory",
                    "priority_rank": 999,
                }
            ],
        ),
        raw_excerpt="",
        _meta=ToolMeta(tool="investigate_pod", params={"pod_name": "api-0"}),
    )

    primary = build_evidence_priority_summary([env])["primary_root_cause"]

    assert primary["priority"] == "primary_failure"


def test_evidence_priority_summary_allows_justified_high_confidence_ai_below_verified():
    env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[
                {
                    "mode": "CrashLoopBackOff",
                    "evidence_priority": "primary_failure",
                    "priority_rank": 80,
                }
            ],
            contributing_factors=[
                {
                    "ai": {"analysis": "Config map key is missing and confirmed by manifest diff."},
                    "evidence_priority": "ai_advisory",
                    "priority_rank": 95,
                    "confidence": "high",
                    "priority_justification": "AI analysis is based on a concrete manifest diff.",
                }
            ],
        ),
        raw_excerpt="",
        _meta=ToolMeta(tool="investigate_pod", params={"pod_name": "api-0"}),
    )

    primary = build_evidence_priority_summary([env])["primary_root_cause"]

    assert primary["priority"] == "ai_advisory"
    assert primary["priority_rank"] == 95


def test_extract_actions_from_synthesized_yaml_and_safe_commands():
    provider = MockProvider(response_text='{"approved": true, "reason": "supported"}')
    evidence_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[
                {
                    "type": "verified_root_cause",
                    "root_cause": "Deployment configuration is invalid.",
                    "evidence_priority": "verified_root_cause",
                }
            ],
            contributing_factors=[
                {
                    "mode": "CrashLoopBackOff",
                    "evidence_priority": "primary_failure",
                }
            ],
        ),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "apps", "pod_name": "api-0"}),
    )
    steps = [ReActStep(iteration=1, thought="Investigate", action="investigate_pod", envelope=evidence_env)]
    answer = """
# Diagnosis
Config needs an update.

# Recommended Actions
1. Apply this manifest after review.

```yaml
# patch:apply
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
  namespace: apps
data:
  key: value
```

2. Restart the deployment: `kubectl rollout restart deployment/api -n apps`
3. Check status: `kubectl rollout status deployment/api -n apps`
"""

    actions = _extract_actions_from_steps(
        steps,
        {},
        answer_text=answer,
        reviewer_provider=provider,
        question="Why is api crashing?",
    )

    assert len(actions) == 2
    assert actions[0]["action_kind"] == "apply_yaml"
    assert actions[0]["risk"] == "high"
    assert actions[0]["requires_approval"] is True
    assert actions[0]["command"] == "kubectl apply -f -"
    assert actions[0]["stdin"].startswith("apiVersion: v1")
    assert actions[0]["evidence_reference"]["priority"] == "verified_root_cause"

    assert actions[1]["action_kind"] == "write_command"
    assert actions[1]["risk"] == "medium"
    assert actions[1]["command"] == "kubectl rollout restart deployment/api -n apps"
    assert provider.calls


def test_extract_actions_does_not_offer_stdin_apply_without_yaml():
    answer = """
# Recommended Actions
Run `kubectl apply -f -` after preparing the manifest.
Then check status with `kubectl rollout status deployment/api -n apps`.
"""

    actions = _extract_actions_from_steps([], {}, answer_text=answer)

    assert actions == []


def test_extract_actions_trims_common_approval_suffixes():
    provider = MockProvider(response_text='{"approved": true, "reason": "supported"}')
    evidence_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[{"mode": "CrashLoopBackOff", "evidence_priority": "primary_failure"}],
        ),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "apps", "pod_name": "api-0"}),
    )
    answer = """
# Recommended Actions
kubectl rollout restart deployment/api -n apps (requires approval)
"""

    actions = _extract_actions_from_steps(
        [ReActStep(iteration=1, thought="Investigate", action="investigate_pod", envelope=evidence_env)],
        {},
        answer_text=answer,
        reviewer_provider=provider,
    )

    assert len(actions) == 1
    assert actions[0]["command"] == "kubectl rollout restart deployment/api -n apps"
    assert actions[0]["requires_approval"] is True


def test_extract_actions_fail_closed_without_llm_reviewer():
    evidence_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[{"mode": "CrashLoopBackOff", "evidence_priority": "primary_failure"}],
        ),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "apps", "pod_name": "api-0"}),
    )
    answer = "1. Restart: `kubectl rollout restart deployment/api -n apps`"

    actions = _extract_actions_from_steps(
        [ReActStep(iteration=1, thought="Investigate", action="investigate_pod", envelope=evidence_env)],
        {},
        answer_text=answer,
    )

    assert actions == []


def test_extract_actions_fail_closed_when_llm_reviewer_rejects():
    provider = MockProvider(response_text='{"approved": false, "reason": "not supported"}')
    evidence_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[{"mode": "CrashLoopBackOff", "evidence_priority": "primary_failure"}],
        ),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "apps", "pod_name": "api-0"}),
    )
    answer = "1. Restart: `kubectl rollout restart deployment/api -n apps`"

    actions = _extract_actions_from_steps(
        [ReActStep(iteration=1, thought="Investigate", action="investigate_pod", envelope=evidence_env)],
        {},
        answer_text=answer,
        reviewer_provider=provider,
    )

    assert actions == []


def test_extract_actions_rejects_underspecified_write_command():
    provider = MockProvider(response_text='{"approved": true, "reason": "supported"}')
    evidence_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[{"mode": "CrashLoopBackOff", "evidence_priority": "primary_failure"}],
        ),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "apps", "pod_name": "api-0"}),
    )
    answer = "1. Restart: `kubectl rollout restart deployment/api`"

    actions = _extract_actions_from_steps(
        [ReActStep(iteration=1, thought="Investigate", action="investigate_pod", envelope=evidence_env)],
        {},
        answer_text=answer,
        reviewer_provider=provider,
    )

    assert actions == []


def test_extract_actions_rejects_namespaced_yaml_without_namespace():
    provider = MockProvider(response_text='{"approved": true, "reason": "supported"}')
    evidence_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[
                {
                    "type": "verified_root_cause",
                    "root_cause": "ConfigMap data is invalid.",
                    "evidence_priority": "verified_root_cause",
                }
            ],
        ),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "apps", "pod_name": "api-0"}),
    )
    answer = """
```yaml
# patch:apply
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  key: value
```
"""

    actions = _extract_actions_from_steps(
        [ReActStep(iteration=1, thought="Investigate", action="investigate_pod", envelope=evidence_env)],
        {},
        answer_text=answer,
        reviewer_provider=provider,
    )

    assert actions == []


def test_extract_actions_allows_cluster_scoped_yaml_without_namespace():
    provider = MockProvider(response_text='{"approved": true, "reason": "supported"}')
    evidence_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"namespace": "apps"},
            failure_modes=[
                {
                    "type": "verified_root_cause",
                    "root_cause": "Namespace label is missing.",
                    "evidence_priority": "verified_root_cause",
                }
            ],
        ),
        _meta=ToolMeta(tool="get_namespaces", params={}),
    )
    answer = """
```yaml
# patch:apply
apiVersion: v1
kind: Namespace
metadata:
  name: apps
  labels:
    owner: platform
```
"""

    actions = _extract_actions_from_steps(
        [ReActStep(iteration=1, thought="Investigate", action="get_namespaces", envelope=evidence_env)],
        {},
        answer_text=answer,
        reviewer_provider=provider,
    )

    assert len(actions) == 1
    assert actions[0]["command"] == "kubectl apply -f -"


def test_execute_rejects_underspecified_write_command_before_subprocess():
    from routers.chat import ExecuteRequest, execute_command

    response = execute_command(
        ExecuteRequest(command="kubectl rollout restart deployment/api"),
        request=None,
    )

    assert response.success is False
    assert "namespace_missing" in response.error


def test_execute_rejects_flag_as_resource_name_before_subprocess():
    from routers.chat import ExecuteRequest, execute_command

    delete_response = execute_command(
        ExecuteRequest(command="kubectl delete pods --all -n apps"),
        request=None,
    )
    cordon_response = execute_command(
        ExecuteRequest(command="kubectl cordon -l role=worker"),
        request=None,
    )

    assert delete_response.success is False
    assert "command_target_missing" in delete_response.error
    assert cordon_response.success is False
    assert "command_target_missing" in cordon_response.error


def test_execute_rejects_namespaced_apply_without_namespace_before_subprocess():
    from routers.chat import ExecuteRequest, execute_command

    response = execute_command(
        ExecuteRequest(
            command="kubectl apply -f -",
            stdin="""apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  key: value
""",
        ),
        request=None,
    )

    assert response.success is False
    assert "yaml_namespace_missing" in response.error


def test_eval_synthesis_structure_check():
    from scripts.eval_agent_deepeval import _evaluate_synthesis_structure

    valid = {
        "diagnosis": "Pod is crashing.",
        "confidence_band": "medium",
        "parser_warnings": [],
    }
    invalid = {
        "diagnosis": "",
        "confidence_band": "unknown",
        "parser_warnings": ["missing_heading:evidence", "confidence_missing"],
    }

    assert _evaluate_synthesis_structure(valid)["passed"] is True
    invalid_result = _evaluate_synthesis_structure(invalid)
    assert invalid_result["passed"] is False
    assert "missing_heading:evidence" in invalid_result["blocking_warnings"]
    assert _evaluate_synthesis_structure(None)["reason"] == "missing_synthesis_breakdown"


def test_eval_remediation_answer_and_payload_assertions():
    from scripts.eval_agent_deepeval import ChatRunResult, Scenario, evaluate_deterministic

    scenario = Scenario(
        id="kafka_remediation",
        prompt="Why is kafka crashing?",
        expected_answer_contains=["zookeeper-kube-upd-cs", "infrastructure"],
        must_not_contain=["envelope[", "syntax error"],
        expected_suggested_actions_min=1,
        expect_root_cause_summary=True,
        expect_eval_retrieval_context=True,
    )
    chat = ChatRunResult(
        scenario_id=scenario.id,
        prompt=scenario.prompt,
        actual_output=(
            "Kafka cannot connect to zookeeper-kube-upd-cs in the "
            "infrastructure namespace."
        ),
        tool_used="investigate_pod",
        rag_mode="cold",
        rag_top_score=None,
        rag_top_collection=None,
        grounded_chunks=[],
        retrieval_context=["root cause: zookeeper-kube-upd-cs is missing"],
        error=None,
        duration_ms=1.0,
        retrieval_context_source="envelope",
        synthesis_breakdown={
            "diagnosis": "Kafka cannot connect to ZooKeeper.",
            "confidence_band": "high",
            "parser_warnings": [],
        },
        suggested_actions=[{"kind": "patch_manifest"}],
        root_cause_summary={"title": "Missing ZooKeeper service"},
    )

    deterministic = evaluate_deterministic(chat, scenario)

    assert deterministic["answer_assertions"]["passed"] is True
    assert deterministic["response_payload"]["passed"] is True
    assert deterministic["response_payload"]["suggested_actions_count"] == 1


def test_parse_markdown_synthesis_nominal():
    """Verify parsing a perfectly formatted Markdown response."""
    md_text = """
# Diagnosis
The kafka-0 pod is crashing because of OOM.

# Evidence
* envelope[0].evidence.timeline[0] — OOMKilled event detected
* envelope[1].evidence.source — memory limit reached

# Recommended Actions
1. Increase memory requests
2. Restart deployment

# Uncertainty
Confidence: high
Everything seems clear from the logs.
"""
    
    result = parse_markdown_synthesis(md_text)
    
    assert result["diagnosis"] == "The kafka-0 pod is crashing because of OOM."
    assert result["evidence_count"] == 2
    assert result["recommended_actions"] == ["Increase memory requests", "Restart deployment"]
    assert result["confidence_band"] == "high"
    assert result["uncertainty_text"] == "Everything seems clear from the logs."
    assert result["parser_warnings"] == []


def test_parse_markdown_synthesis_drifted():
    """Verify post-parser tolerates heading level, colon, case, and synonym drift with warnings."""
    md_text = """
## Diagnosis:
The pod is healthy.

### Evidence
- envelope[0].evidence.items[0] - Running status

# Next Steps
* Cordon node-a
* Check endpoints

# Caveats
Confidence: medium
We have incomplete logs.
"""
    
    result = parse_markdown_synthesis(md_text)
    
    assert result["diagnosis"] == "The pod is healthy."
    assert result["evidence_count"] == 1
    assert result["recommended_actions"] == ["Cordon node-a", "Check endpoints"]
    assert result["confidence_band"] == "medium"
    assert result["uncertainty_text"] == "We have incomplete logs."
    
    warnings = result["parser_warnings"]
    assert "heading_level_drift:diagnosis" in warnings
    assert "trailing_colon:diagnosis" in warnings
    assert "heading_level_drift:evidence" in warnings
    assert "synonym_used:recommended_actions" in warnings
    assert "synonym_used:uncertainty" in warnings


class MockProvider:
    def __init__(self, response_text: str = "", stream_chunks: list = None):
        self.response_text = response_text
        self.stream_chunks = stream_chunks or []
        self.calls = []

    def generate(self, prompt: str, system=None, temperature=0.2, max_tokens=None) -> str:
        self.calls.append({"prompt": prompt, "system": system, "type": "generate"})
        return self.response_text

    def generate_stream(self, prompt: str, system=None, temperature=0.2, max_tokens=None):
        self.calls.append({"prompt": prompt, "system": system, "type": "generate_stream"})
        for chunk in self.stream_chunks:
            yield chunk


def test_compute_confidence_band():
    """Verify confidence band logic is evidence-relevance aware."""
    from react import compute_confidence_band, compute_confidence_report
    
    # 1. Budget exhausted -> low
    assert compute_confidence_band([], budget_exhausted=True) == "low"
    
    # 2. No envelopes -> low
    assert compute_confidence_band([], budget_exhausted=False) == "low"
    
    inventory_env_1 = ToolEnvelope(
        verdict="n/a",
        evidence=InventoryEvidence(items=[{"name": "api-0"}], total_count=1),
        confidence_signals=ConfidenceSignals(data_completeness="complete"),
        _meta=ToolMeta(tool="get_pods", params={}),
    )
    inventory_env_2 = ToolEnvelope(
        verdict="n/a",
        evidence=InventoryEvidence(items=[{"name": "api-1"}], total_count=1),
        confidence_signals=ConfidenceSignals(data_completeness="complete"),
        _meta=ToolMeta(tool="get_namespaces", params={}),
    )
    assert compute_confidence_band([inventory_env_1, inventory_env_2], budget_exhausted=False) == "low"
    
    stale_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0"},
            failure_modes=[{"mode": "CrashLoopBackOff", "evidence_priority": "primary_failure"}],
        ),
        confidence_signals=ConfidenceSignals(data_completeness="stale"),
        _meta=ToolMeta(tool="investigate_pod", params={}),
    )
    assert compute_confidence_band([stale_env], budget_exhausted=False) == "low"
    
    partial_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0"},
            failure_modes=[{"mode": "CrashLoopBackOff", "evidence_priority": "primary_failure"}],
        ),
        confidence_signals=ConfidenceSignals(data_completeness="partial"),
        _meta=ToolMeta(tool="investigate_pod", params={}),
    )
    assert compute_confidence_band([partial_env], budget_exhausted=False) == "low"
    
    diagnostic_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0"},
            failure_modes=[{"mode": "CrashLoopBackOff", "evidence_priority": "primary_failure"}],
        ),
        confidence_signals=ConfidenceSignals(data_completeness="complete"),
        _meta=ToolMeta(tool="investigate_pod", params={}),
    )
    assert compute_confidence_band([diagnostic_env], budget_exhausted=False) == "medium"

    verified_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "my-kafka-0", "namespace": "infrastructure"},
            failure_modes=[
                {
                    "type": "verified_root_cause",
                    "root_cause": "ZooKeeper service is missing.",
                    "evidence_priority": "verified_root_cause",
                }
            ],
            contributing_factors=[
                {
                    "service": "zookeeper-kube-upd-cs",
                    "service_exists": False,
                    "evidence_priority": "dependency_check",
                }
            ],
        ),
        confidence_signals=ConfidenceSignals(data_completeness="complete"),
        _meta=ToolMeta(tool="investigate_pod", params={}),
    )
    report = compute_confidence_report([verified_env], budget_exhausted=False)
    assert report["band"] == "high"
    assert "corroborated" in report["reasons"][0]

    healthy_env = ToolEnvelope(
        verdict="Healthy",
        evidence=DiagnosticEvidence(primary_target={"pod_name": "api-0"}, failure_modes=[]),
        confidence_signals=ConfidenceSignals(data_completeness="complete"),
        _meta=ToolMeta(tool="investigate_pod", params={}),
    )
    assert compute_confidence_band([healthy_env, diagnostic_env], budget_exhausted=False) == "low"

    other_healthy_env = ToolEnvelope(
        verdict="Healthy",
        evidence=DiagnosticEvidence(primary_target={"pod_name": "api-1"}, failure_modes=[]),
        confidence_signals=ConfidenceSignals(data_completeness="complete"),
        _meta=ToolMeta(tool="investigate_pod", params={}),
    )
    assert compute_confidence_band([other_healthy_env, diagnostic_env], budget_exhausted=False) == "medium"

    other_verified_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-1"},
            failure_modes=[
                {
                    "type": "verified_root_cause",
                    "root_cause": "ConfigMap key is missing.",
                    "evidence_priority": "verified_root_cause",
                }
            ],
        ),
        confidence_signals=ConfidenceSignals(data_completeness="complete"),
        _meta=ToolMeta(tool="investigate_pod", params={}),
    )
    assert compute_confidence_band([verified_env, other_verified_env], budget_exhausted=False) == "high"


def test_run_synthesis_critic_pass():
    """Verify synthesis critic parses JSON output correctly and handles pass cases."""
    from services.synthesis_critic import run_synthesis_critic
    from typing import Optional
    
    mock_response = """
    {
      "evidence_supported": {"passed": true, "rationale": "Directly traces to path"},
      "no_contradiction": {"passed": true, "rationale": "Consistent"},
      "recency_correct": {"passed": true, "rationale": "Latest event cited"},
      "confidence_honest": {"passed": true, "rationale": "High match"}
    }
    """
    provider = MockProvider(response_text=mock_response)
    
    results = run_synthesis_critic(
        provider=provider,
        question="Why did the pod crash?",
        envelopes=[],
        retrieval_context="some context",
        answer="# Diagnosis\nCrashing"
    )
    
    assert all(r["passed"] for r in results.values())
    assert results["evidence_supported"]["rationale"] == "Directly traces to path"


def test_stream_finalize_with_critic_pre_gate():
    """Verify pre-gating flow: analyzes findings placeholder and streams on completion."""
    from react import stream_finalize_with_critic
    
    # Low confidence triggers pre-gating
    stale_env = {
        "confidence_signals": {"data_completeness": "stale"}
    }
    
    mock_answer = "# Diagnosis\nCrashing\n# Evidence\n* envelope[0].evidence.items[0] - CrashLoopBackOff\n# Recommended Actions\n(requires approval) delete_pod"
    mock_critic_pass = """
    {
      "evidence_supported": {"passed": true, "rationale": "ok"},
      "no_contradiction": {"passed": true, "rationale": "ok"},
      "recency_correct": {"passed": true, "rationale": "ok"},
      "confidence_honest": {"passed": true, "rationale": "ok"}
    }
    """
    
    # Setup provider: generate mock answer and then mock critic check
    class SequentialMockProvider:
        def __init__(self):
            self.calls = 0
        def generate(self, prompt, system=None, temperature=0.2, max_tokens=None):
            self.calls += 1
            if self.calls == 1:
                return mock_answer
            else:
                return mock_critic_pass
                
    provider = SequentialMockProvider()
    events = []
    
    ans = stream_finalize_with_critic(
        provider=provider,
        question="Why did it crash?",
        history_context="some context",
        envelopes=[stale_env],
        causality_chain=[],
        finalize_system="sys prompt",
        budget_exhausted=False,
        on_event=events.append,
    )
    
    # Check that events contain placeholder and then token streams
    assert any(e["type"] == "placeholder" and "Analyzing findings..." in e["text"] for e in events)
    assert any(e["type"] == "token" for e in events)
    assert "CrashLoopBackOff" in ans
    assert "envelope[" not in ans
    assert "envelope[" not in "".join(e.get("text", "") for e in events if e["type"] == "token")


def test_stream_finalize_with_critic_hybrid_gate():
    """Verify hybrid-gating: streams diagnosis, holds actions, and calls critic for destructive actions."""
    from react import stream_finalize_with_critic
    
    high_confidence_env = ToolEnvelope(
        verdict="Unhealthy",
        evidence=DiagnosticEvidence(
            primary_target={"pod_name": "api-0", "namespace": "apps"},
            failure_modes=[
                {
                    "type": "verified_root_cause",
                    "root_cause": "The deployment image is invalid.",
                    "evidence_priority": "verified_root_cause",
                }
            ],
            contributing_factors=[
                {
                    "mode": "ImagePullBackOff",
                    "evidence_priority": "primary_failure",
                }
            ],
        ),
        confidence_signals=ConfidenceSignals(data_completeness="complete"),
        _meta=ToolMeta(tool="investigate_pod", params={"namespace": "apps", "pod_name": "api-0"}),
    )
    
    stream_chunks = [
        "# Diagnosis\nAll looks ok.\n",
        "# Evidence\n* envelope[0] status is fine.\n",
        "\n# Recommended Actions\n",
        "1. delete_pod (requires approval) to clean up.\n",
        "# Uncertainty\nConfidence: medium\n"
    ]
    
    mock_critic_pass = """
    {
      "evidence_supported": {"passed": true, "rationale": "ok"},
      "no_contradiction": {"passed": true, "rationale": "ok"},
      "recency_correct": {"passed": true, "rationale": "ok"},
      "confidence_honest": {"passed": true, "rationale": "ok"}
    }
    """
    
    class HybridMockProvider:
        def generate_stream(self, prompt, system=None, temperature=0.2, max_tokens=None):
            for chunk in stream_chunks:
                yield chunk
        def generate(self, prompt, system=None, temperature=0.2, max_tokens=None):
            return mock_critic_pass
            
    provider = HybridMockProvider()
    events = []
    
    ans = stream_finalize_with_critic(
        provider=provider,
        question="What is the state?",
        history_context="some context",
        envelopes=[high_confidence_env],
        causality_chain=[],
        finalize_system="sys prompt",
        budget_exhausted=False,
        on_event=events.append,
    )
    
    # Check that the placeholder "Verifying recommendations..." was emitted
    assert any(e["type"] == "placeholder" and "Verifying recommendations..." in e["text"] for e in events)
    
    # Verify diagnosis and evidence were streamed before placeholder
    placeholder_idx = next(i for i, e in enumerate(events) if e["type"] == "placeholder")
    tokens_before = [e["text"] for e in events[:placeholder_idx] if e["type"] == "token"]
    assert any("Diagnosis" in t for t in tokens_before)
    assert not any("delete_pod" in t for t in tokens_before)
    
    # Verify actions were streamed after placeholder
    tokens_after = [e["text"] for e in events[placeholder_idx:] if e["type"] == "token"]
    assert any("delete_pod" in t for t in tokens_after)
    assert "envelope[" not in ans
    assert "envelope[" not in "".join(e.get("text", "") for e in events if e["type"] == "token")
