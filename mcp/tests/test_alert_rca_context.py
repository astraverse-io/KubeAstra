"""Regression tests for alert RCA evidence extraction."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from alerts.domain.alert import Alert  # noqa: E402
from alerts.domain.evidence import Evidence  # noqa: E402
from alerts.domain.investigation import Investigation  # noqa: E402
from alerts.orchestrator.rca_context import build_rca_context  # noqa: E402
from alerts.orchestrator.engine import InvestigationOrchestrator  # noqa: E402


def test_rca_context_extracts_investigate_pod_deterministic_evidence():
    alert = Alert.from_parts(
        name="ManualPodInvestigation",
        source="manual",
        severity="info",
        labels={"namespace": "jenkins-legacy", "pod": "jenkins-legacy-0"},
        raw_payload={},
    )
    investigation = Investigation(alert=alert)
    investigation.evidence.append(
        Evidence(
            evidence_type="kubernetes",
            tool="investigate_pod",
            summary="Pod jenkins-legacy-0 is CrashLoopBackOff.",
            raw={
                "success": True,
                "namespace": "jenkins-legacy",
                "pod_name": "jenkins-legacy-0",
                "classification": {
                    "mode": "CrashLoopBackOff",
                    "container": "init",
                    "reason": "CrashLoopBackOff",
                },
                "describe": {
                    "highlights": {
                        "restart_count": 12,
                        "state": "Waiting",
                        "ready": "False",
                    }
                },
                "logs_previous": {
                    "logs": (
                        "Multiple plugin prerequisites not met\n"
                        "workflow depends on pipeline-model-api:2.2291, "
                        "but there is an older version defined on the top level - "
                        "pipeline-model-api:2.2277"
                    )
                },
                "container_log_findings": [
                    {
                        "container": "init",
                        "restart_count": 12,
                        "reason": "CrashLoopBackOff",
                        "logs_previous": {
                            "excerpt": "Multiple plugin prerequisites not met"
                        },
                        "diagnostic_issue": {
                            "type": "application_dependency_resolution",
                            "mismatches": [
                                {
                                    "required": "pipeline-model-api:2.2291",
                                    "pinned": "pipeline-model-api:2.2277",
                                }
                            ],
                        },
                    }
                ],
                "evidence_summary": {
                    "suspected_root_cause": (
                        "Container `init` is exiting during application dependency "
                        "resolution because a dependency is pinned too old."
                    ),
                    "suggested_fix": "Update the plugin dependency pin list.",
                },
                "events": {
                    "events": [
                        {
                            "type": "Warning",
                            "reason": "BackOff",
                            "message": "Back-off restarting failed container init",
                        }
                    ]
                },
            },
        )
    )

    context = build_rca_context(investigation)

    pod_detail = context["evidence_details"][0]["pod_investigation"]
    assert pod_detail["classification"]["mode"] == "CrashLoopBackOff"
    assert pod_detail["classification"]["container"] == "init"
    assert pod_detail["deterministic_evidence"]["suspected_root_cause"].startswith(
        "Container `init` is exiting"
    )

    signal_names = {signal["name"] for signal in context["detected_signals"]}
    assert "pod_failure_mode" in signal_names
    assert "deterministic_root_cause" in signal_names
    assert "application_dependency_resolution" in signal_names


def test_alert_orchestrator_promotes_investigate_pod_root_cause():
    alert = Alert.from_parts(
        name="ManualPodInvestigation",
        source="manual",
        severity="info",
        labels={"namespace": "jenkins-legacy", "pod": "jenkins-legacy-0"},
        raw_payload={},
    )
    investigation = Investigation(alert=alert)
    investigation.evidence.append(
        Evidence(
            evidence_id="ev_jenkins",
            evidence_type="kubernetes",
            tool="investigate_pod",
            summary="Pod jenkins-legacy-0 is CrashLoopBackOff.",
            raw={
                "success": True,
                "namespace": "jenkins-legacy",
                "pod_name": "jenkins-legacy-0",
                "classification": {
                    "mode": "CrashLoopBackOff",
                    "container": "init",
                    "reason": "CrashLoopBackOff",
                },
                "container_log_findings": [
                    {
                        "container": "init",
                        "restart_count": 2949,
                        "reason": "CrashLoopBackOff",
                        "logs_previous": {
                            "excerpt": (
                                "io.jenkins.tools.pluginmanager.impl."
                                "AggregatePluginPrerequisitesNotMetException: "
                                "Plugin workflow-aggregator depends on "
                                "pipeline-model-api:2.2291.v2934911987b_6, "
                                "but there is an older version defined on the top level - "
                                "pipeline-model-api:2.2277.v00573e73ddf1"
                            )
                        },
                        "diagnostic_issue": {
                            "type": "application_dependency_resolution",
                            "mismatches": [
                                {
                                    "required": "pipeline-model-api:2.2291.v2934911987b_6",
                                    "pinned": "pipeline-model-api:2.2277.v00573e73ddf1",
                                }
                            ],
                        },
                    }
                ],
                "evidence_summary": {
                    "suspected_root_cause": (
                        "Container `init` is exiting during application dependency "
                        "resolution: a dependency requires "
                        "`pipeline-model-api:2.2291.v2934911987b_6`, but the "
                        "chart/config pins an older top-level dependency "
                        "`pipeline-model-api:2.2277.v00573e73ddf1`."
                    ),
                    "suggested_fix": (
                        "Update the application/plugin dependency pin list or Helm "
                        "values so top-level versions satisfy the required dependencies."
                    ),
                    "evidence": [
                        {
                            "type": "verified_root_cause",
                            "summary": (
                                "workflow-aggregator requires the newer "
                                "pipeline-model-api version."
                            ),
                        }
                    ],
                },
                "events": {
                    "events": [
                        {
                            "type": "Warning",
                            "reason": "BackOff",
                            "message": "Back-off restarting failed container init",
                        }
                    ]
                },
            },
        )
    )

    orchestrator = InvestigationOrchestrator.__new__(InvestigationOrchestrator)
    rca = orchestrator._deterministic_rca_from_evidence(
        investigation,
        evidence_ids=["ev_jenkins"],
        rca_scoring={"score": 1.0},
    )

    assert rca is not None
    assert rca.confidence >= 0.95
    assert "pipeline-model-api:2.2291.v2934911987b_6" in rca.summary
    assert "pipeline-model-api:2.2277.v00573e73ddf1" in rca.summary
    assert rca.recommendations == [
        "Update the application/plugin dependency pin list or Helm values so top-level versions satisfy the required dependencies."
    ]
    assert any("workflow-aggregator" in item for item in rca.supporting_evidence)
    assert any("application_dependency_resolution" in item for item in rca.supporting_evidence)
