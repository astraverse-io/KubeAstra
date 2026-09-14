"""Core engine orchestrating evidence gathering and health rule evaluation."""

import logging
from typing import Any, Dict, List, Optional

from .collectors import collect_evidence
from .models import AnalyzerResult, Finding, Severity
from .rules import (
    check_pod_rules,
    check_scheduling_and_node_rules,
    check_service_and_rollout_rules,
)

logger = logging.getLogger(__name__)


def run_health_analyzer(
    scope_type: str = "cluster",
    namespace: Optional[str] = None,
    resource_name: Optional[str] = None,
    resource_kind: Optional[str] = None,
    runner: Optional[Any] = None,
    restart_threshold: int = 5,
) -> AnalyzerResult:
    """Run deterministic Kubernetes health rules over gathered cluster evidence.

    Args:
        scope_type: One of 'cluster', 'namespace', 'pod', 'workload', 'node'.
        namespace: Optional target namespace.
        resource_name: Optional resource name for scoped pod/node/workload checks.
        resource_kind: Optional resource kind.
        runner: Optional explicit KubectlRunner instance; defaults to get_runner().
        restart_threshold: Container restart count threshold for warnings (default 5).

    Returns:
        AnalyzerResult containing structured findings and summary metrics.
    """
    if runner is None:
        from ..kubectl_runner import get_runner
        runner = get_runner()

    scope: Dict[str, Any] = {
        "type": scope_type,
        "namespace": namespace,
        "resource_name": resource_name,
        "resource_kind": resource_kind,
    }

    # 1. Gather evidence
    evidence = collect_evidence(
        runner=runner,
        scope_type=scope_type,
        namespace=namespace,
        resource_name=resource_name,
        resource_kind=resource_kind,
    )

    # 2. Evaluate rule suites
    findings: List[Finding] = []
    findings.extend(check_pod_rules(evidence.pods, evidence.events, restart_threshold=restart_threshold))
    findings.extend(check_scheduling_and_node_rules(evidence.pods, evidence.nodes, evidence.events))
    findings.extend(check_service_and_rollout_rules(evidence.services, evidence.endpoints, evidence.deployments, evidence.pvcs))

    # 3. Summarize findings by severity
    summary = {
        Severity.CRITICAL.value: 0,
        Severity.WARNING.value: 0,
        Severity.INFO.value: 0,
    }
    for f in findings:
        if f.severity in summary:
            summary[f.severity] += 1

    return AnalyzerResult(
        scope=scope,
        finding_count=len(findings),
        findings=findings,
        summary=summary,
    )
