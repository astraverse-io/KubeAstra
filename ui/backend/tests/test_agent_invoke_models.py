"""Tests for the new request/response models introduced in Phase 1.

Verifies that:
  - the target field is optional and rejected when malformed;
  - extra fields are rejected (no credential injection);
  - the response carries the new diagnostic_mode / connection / evidence fields
    with safe defaults for target-less invocations.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from routers.agent import (
    AgentInvokeRequest,
    AgentInvokeResponse,
    AgentTargetRequest,
    ConnectionInfo,
    Evidence,
)


def test_request_target_is_optional():
    req = AgentInvokeRequest(input={"error": "boom"})
    assert req.target is None


def test_request_target_round_trips():
    req = AgentInvokeRequest(
        input={"error": "boom"},
        target={"connection_type": "ssh", "target_id": "qa17"},
    )
    assert req.target is not None
    assert req.target.target_id == "qa17"
    assert req.target.diagnostic_scope == {"kubernetes"}


def test_request_rejects_extra_top_level_fields():
    """Plan §6.1: request cannot override host, port, credentials, etc."""
    with pytest.raises(ValidationError):
        AgentInvokeRequest(input={}, host="evil.example.com")


def test_target_rejects_extra_fields():
    """Plan §6.1: the target block cannot override server-managed metadata."""
    with pytest.raises(ValidationError):
        AgentTargetRequest(
            connection_type="ssh",
            target_id="qa17",
            host="evil.example.com",  # must be rejected
        )


def test_target_id_pattern_enforced():
    with pytest.raises(ValidationError):
        AgentTargetRequest(connection_type="ssh", target_id="QA17")  # uppercase
    with pytest.raises(ValidationError):
        AgentTargetRequest(connection_type="ssh", target_id="-leading-dash")
    with pytest.raises(ValidationError):
        AgentTargetRequest(connection_type="ssh", target_id="x" * 64)  # too long


def test_target_connection_type_must_be_ssh():
    with pytest.raises(ValidationError):
        AgentTargetRequest(connection_type="http", target_id="qa17")


def test_diagnostic_scope_unknown_value_rejected():
    with pytest.raises(ValidationError):
        AgentTargetRequest(
            connection_type="ssh",
            target_id="qa17",
            diagnostic_scope={"host"},  # not allowed in v1
        )


def test_response_defaults_to_error_only_mode():
    """Plan §6.2: default safe state is error_only with empty connection info."""
    resp = AgentInvokeResponse(
        request_id="req-1",
        status="completed",
        answer="...",
        tool_used="analyze_error",
        timing_ms=100,
    )
    assert resp.diagnostic_mode == "error_only"
    assert resp.connection.verified is False
    assert resp.connection.target_id is None
    assert resp.evidence == []


def test_response_can_carry_live_cluster_metadata():
    resp = AgentInvokeResponse(
        request_id="req-2",
        run_id="run-2",
        status="completed",
        diagnostic_mode="live_cluster",
        connection=ConnectionInfo(
            type="ssh",
            target_id="qa17",
            verified=True,
            connected_host="qa17-control-plane",
            kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
            duration_ms=842,
        ),
        answer="The deployment is blocked because...",
        tool_used="investigate_workload",
        evidence=[
            Evidence(
                source="kubernetes",
                tool="get_events",
                summary="ProgressDeadlineExceeded for deployment/payments-api",
                target_id="qa17",
                observed_at="2026-06-30T15:10:00Z",
            )
        ],
        timing_ms=4320,
    )
    assert resp.diagnostic_mode == "live_cluster"
    assert resp.connection.verified is True
    assert resp.connection.kube_system_uid == "1c43dff0-0000-0000-0000-000000000000"
    assert len(resp.evidence) == 1
    assert resp.evidence[0].source == "kubernetes"


def test_response_partial_mode_is_valid():
    """live_cluster_partial still requires verified identity (target_id +
    kube_system_uid); evidence may be empty because some tool calls failed."""
    resp = AgentInvokeResponse(
        request_id="req-3",
        status="completed",
        diagnostic_mode="live_cluster_partial",
        connection=ConnectionInfo(
            type="ssh",
            target_id="qa17",
            verified=True,
            kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
        ),
        answer="...",
        tool_used="investigate_workload",
        timing_ms=100,
    )
    assert resp.diagnostic_mode == "live_cluster_partial"


def test_live_cluster_requires_verified():
    with pytest.raises(ValidationError, match="verified=true"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="live_cluster",
            connection=ConnectionInfo(
                type="ssh", target_id="qa17", verified=False,
                kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
            ),
            answer="x", tool_used="t", timing_ms=1,
            evidence=[Evidence(source="kubernetes", tool="get", summary="s")],
        )


def test_live_cluster_requires_target_id():
    with pytest.raises(ValidationError, match="target_id"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="live_cluster",
            connection=ConnectionInfo(
                type="ssh", verified=True,
                kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
            ),
            answer="x", tool_used="t", timing_ms=1,
            evidence=[Evidence(source="kubernetes", tool="get", summary="s")],
        )


def test_live_cluster_requires_kube_system_uid():
    with pytest.raises(ValidationError, match="kube_system_uid"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="live_cluster",
            connection=ConnectionInfo(type="ssh", target_id="qa17", verified=True),
            answer="x", tool_used="t", timing_ms=1,
            evidence=[Evidence(source="kubernetes", tool="get", summary="s")],
        )


def test_live_cluster_requires_evidence():
    """Partial mode allows empty evidence; live_cluster does not — that mode
    explicitly means evidence was collected."""
    with pytest.raises(ValidationError, match="evidence"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="live_cluster",
            connection=ConnectionInfo(
                type="ssh", target_id="qa17", verified=True,
                kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
            ),
            answer="x", tool_used="t", timing_ms=1,
        )


def test_live_cluster_rejects_reason_field():
    """``reason`` only makes sense on failure paths; setting it together with
    a live mode is a category error."""
    with pytest.raises(ValidationError, match="reason must be null"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="live_cluster",
            connection=ConnectionInfo(
                type="ssh", target_id="qa17", verified=True,
                kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
                reason="connect_timeout",
            ),
            answer="x", tool_used="t", timing_ms=1,
            evidence=[Evidence(source="kubernetes", tool="get", summary="s")],
        )


def test_error_only_cannot_claim_verified():
    with pytest.raises(ValidationError, match="inconsistent with connection.verified"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="error_only",
            connection=ConnectionInfo(verified=True),
            answer="x", tool_used="t", timing_ms=1,
        )


def test_connection_reason_must_be_bounded_enum():
    """Free-form transport exception text must not surface in the response."""
    with pytest.raises(ValidationError):
        ConnectionInfo(reason="Connection refused: 10.40.17.10:22")  # type: ignore[arg-type]


def test_connection_reason_accepts_known_categories():
    for r in (
        "connect_timeout", "auth_failed", "host_key_mismatch", "host_key_missing",
        "identity_mismatch", "bastion_failed", "transport_error",
        "circuit_open", "target_disabled", "unauthorized",
    ):
        ConnectionInfo(reason=r)  # type: ignore[arg-type]


def test_response_unknown_diagnostic_mode_rejected():
    with pytest.raises(ValidationError):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="speculative",  # not a valid Literal value
            answer="x",
            tool_used="t",
            timing_ms=1,
        )


def test_evidence_unknown_source_rejected():
    with pytest.raises(ValidationError):
        Evidence(source="host", tool="systemctl", summary="...")  # type: ignore[arg-type]


def test_live_cluster_requires_ssh_connection_type():
    """connection.type=None or any non-ssh value contradicts a live mode."""
    with pytest.raises(ValidationError, match="connection.type='ssh'"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="live_cluster",
            connection=ConnectionInfo(
                target_id="qa17", verified=True,
                kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
            ),  # type=None — implicitly missing
            answer="x", tool_used="t", timing_ms=1,
            evidence=[Evidence(source="kubernetes", tool="get", summary="s", target_id="qa17")],
        )


def test_evidence_target_id_must_match_connection():
    """An evidence row from a different environment is a cross-target leak."""
    with pytest.raises(ValidationError, match="does not match"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="live_cluster",
            connection=ConnectionInfo(
                type="ssh", target_id="qa17", verified=True,
                kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
            ),
            answer="x", tool_used="t", timing_ms=1,
            evidence=[
                Evidence(source="kubernetes", tool="get", summary="s", target_id="qa17"),
                Evidence(source="kubernetes", tool="get", summary="s", target_id="qa18"),
            ],
        )


def test_evidence_with_null_target_id_is_allowed():
    """Older/unattributed evidence may omit target_id; only an explicit
    mismatch is a violation."""
    resp = AgentInvokeResponse(
        request_id="r",
        status="completed",
        diagnostic_mode="live_cluster",
        connection=ConnectionInfo(
            type="ssh", target_id="qa17", verified=True,
            kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
        ),
        answer="x", tool_used="t", timing_ms=1,
        evidence=[Evidence(source="kubernetes", tool="get", summary="s")],
    )
    assert resp.diagnostic_mode == "live_cluster"


def test_error_only_rejects_kube_system_uid():
    """No live identity may leak into error_only responses."""
    with pytest.raises(ValidationError, match="kube_system_uid"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="error_only",
            connection=ConnectionInfo(
                kube_system_uid="1c43dff0-0000-0000-0000-000000000000",
            ),
            answer="x", tool_used="t", timing_ms=1,
        )


def test_error_only_rejects_evidence():
    """If we collected live observations, the mode is not error_only."""
    with pytest.raises(ValidationError, match="must not carry evidence"):
        AgentInvokeResponse(
            request_id="r",
            status="completed",
            diagnostic_mode="error_only",
            answer="x", tool_used="t", timing_ms=1,
            evidence=[Evidence(source="kubernetes", tool="get", summary="s")],
        )
