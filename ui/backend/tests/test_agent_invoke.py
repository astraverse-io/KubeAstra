import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import db
from routers import agent

_KUBE_SYSTEM_UID = "11111111-2222-3333-4444-555555555555"


def _ansible_imagepull_payload() -> dict:
    return {
        "changed": False,
        "msg": "Deployment update timed out waiting for the condition",
        "result": {
            "kind": "Deployment",
            "metadata": {"name": "payments-api", "namespace": "payments"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "payments-api",
                                "image": "registry.internal/payments-api:v404",
                            }
                        ]
                    }
                }
            },
            "status": {
                "conditions": [
                    {
                        "reason": "ImagePullBackOff",
                        "message": "Workload did not become ready",
                    }
                ]
            },
        },
    }


def _client(monkeypatch, tmp_path, **router_kwargs):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "invoke.db"))
    db.init_db()
    app = FastAPI()

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "req-test"
        return await call_next(request)

    app.include_router(
        agent.create_router(primary_token="current-token", **router_kwargs)
    )
    return TestClient(app)


def _result(**overrides):
    values = {
        "run_id": None,
        "reply": "diagnosis",
        "tool_used": "analyze_error",
        "result": {
            "root_cause": "missing image",
            "nested": {"token": "must-not-leak"},
        },
        "suggested_actions": [],
        "error": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _remote_auth_and_target():
    from caller_scopes import CallerScope, ScopeRegistry
    from target_registry import TargetConfig, TargetRegistry

    scopes = ScopeRegistry(
        scopes={
            "qa-ansible": CallerScope(
                name="qa-ansible",
                current_token="SCOPED_TOKEN",
                previous_token=None,
                allowed_target_ids=frozenset({"qa17"}),
            )
        },
        legacy=None,
    )
    targets = TargetRegistry(
        targets={
            "qa17": TargetConfig(
                target_id="qa17",
                display_name="QA 17",
                environment_group="qa",
                host="qa17-control.internal",
                port=22,
                username="diagnostic",
                known_hosts_alias="qa17-control",
                credential_path="/var/run/agent-target-credentials/qa17",
                expected_kube_system_uid=_KUBE_SYSTEM_UID,
                diagnostic_scopes_allowed=frozenset({"kubernetes"}),
                allowed_caller_scopes=frozenset({"qa-ansible"}),
            )
        }
    )
    return scopes, targets


class _FakeRemoteRunner:
    def __init__(self, *, uid: str = _KUBE_SYSTEM_UID, connect_error=None):
        self.uid = uid
        self.connect_error = connect_error
        self.timeout = 10.0
        self.connected = False
        self.closed = False
        self.operation_deadline = None

    def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def set_operation_deadline(self, deadline):
        self.operation_deadline = deadline

    def run_json(self, args, namespace=None):
        if "version" in args:
            return {"serverVersion": {"gitVersion": "v1.30.0"}}
        if "kube-system" in args:
            return {"metadata": {"uid": self.uid}}
        raise AssertionError(f"unexpected run_json args: {args!r}")

    def run(self, args, **kwargs):
        assert "current-context" in args
        return SimpleNamespace(
            stdout="kubernetes-admin@qa17\n",
            raise_for_status=lambda: None,
        )

    def close(self):
        self.closed = True


def test_invoke_requires_valid_bearer(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.post("/api/v1/agent/invoke", json={"input": {}}).status_code == 401
    assert client.post(
        "/api/v1/agent/invoke",
        json={"input": {}},
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_timeout_seconds", float("nan")),
        ("execution_timeout_seconds", float("inf")),
        ("execution_timeout_seconds", 0),
        ("connection_timeout_seconds", float("nan")),
        ("diagnostic_timeout_seconds", float("inf")),
        ("queue_timeout_seconds", float("nan")),
        ("queue_timeout_seconds", -1),
    ],
)
def test_router_rejects_unbounded_timeout_configuration(field, value):
    with pytest.raises(ValueError, match="must be finite"):
        agent.create_router(primary_token="token", **{field: value})


def test_invoke_returns_structured_redacted_response(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "_execute_agent", lambda body, deadline: _result())
    response = client.post(
        "/api/v1/agent/invoke",
        json={"input": {"error": "manifest unknown"}},
        headers={"Authorization": "Bearer current-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == "req-test"
    assert payload["status"] == "completed"
    assert payload["tool_result"]["root_cause"] == "missing image"
    assert payload["tool_result"]["nested"]["token"] == "<REDACTED>"


def test_invoke_rejects_malformed_json_without_echo(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    response = client.post(
        "/api/v1/agent/invoke",
        content='{"input": "private-value"',
        headers={
            "Authorization": "Bearer current-token",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400
    assert "private-value" not in response.text


def test_invoke_enforces_raw_byte_limit(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, max_input_bytes=32)
    response = client.post(
        "/api/v1/agent/invoke",
        json={"input": "x" * 100},
        headers={"Authorization": "Bearer current-token"},
    )
    assert response.status_code == 413


def test_previous_token_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "invoke.db"))
    db.init_db()
    app = FastAPI()
    app.include_router(
        agent.create_router(
            primary_token="new-token",
            previous_token="old-token",
        )
    )
    monkeypatch.setattr(agent, "_execute_agent", lambda body, deadline: _result())
    response = TestClient(app).post(
        "/api/v1/agent/invoke",
        json={"input": "error"},
        headers={"Authorization": "Bearer old-token"},
    )
    assert response.status_code == 200


def test_router_is_absent_without_primary_token(monkeypatch):
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    assert agent.router_from_environment() is None


def test_token_fingerprint_is_stable_and_non_plaintext():
    fingerprint = agent._token_fingerprint("secret-token")
    assert fingerprint == agent._token_fingerprint("secret-token")
    assert len(fingerprint) == 8
    assert "secret" not in fingerprint


def test_response_redaction_fails_closed_when_primary_redactor_raises(monkeypatch):
    from services.rag import redaction

    def broken_redactor(value):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(redaction, "redact", broken_redactor)
    value = agent._redact(
        {
            "accessToken": "token-value",
            "detail": "password=hunter2 Authorization: Bearer abc.def",
            "monkey": "not-secret",
            "tuple_value": ("secret=tuple-secret",),
        }
    )
    assert value["accessToken"] == "<REDACTED>"
    assert value["monkey"] == "not-secret"
    assert "hunter2" not in value["detail"]
    assert "abc.def" not in value["detail"]
    assert "tuple-secret" not in value["tuple_value"][0]


def test_kubectl_request_timeout_never_exceeds_remaining_budget(monkeypatch):
    monkeypatch.setattr(agent.time, "perf_counter", lambda: 100.0)
    timeout_arg = agent._request_timeout_arg(100.0059)
    timeout_ms = int(timeout_arg.removeprefix("--request-timeout=").removesuffix("ms"))
    assert 1 <= timeout_ms <= 5

    with pytest.raises(TimeoutError, match="insufficient time"):
        agent._request_timeout_arg(100.0009)


def test_execute_agent_installs_and_resets_llm_deadline(monkeypatch):
    from services.llm.base import effective_timeout

    observed = []

    def fake_dispatch(tool, params, **kwargs):
        observed.append(effective_timeout(60))
        assert tool == "analyze_error"
        assert params["diagnostic_mode"] == "error_only"
        return {"root_cause": "diagnosis", "solution": "fix"}

    monkeypatch.setattr(agent.chat, "_dispatch", fake_dispatch)
    monkeypatch.setattr(
        agent.chat,
        "_chat_react",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("targetless invocation must not run ReAct")
        ),
    )
    deadline = agent.time.perf_counter() + 5
    result = agent._execute_agent(
        agent.AgentInvokeRequest(input={"error": "boom"}),
        deadline,
    )
    assert result.reply == "diagnosis\n\nRecommended action: fix"
    assert 0 < observed[0] <= 5
    assert effective_timeout(60) == 60


def test_error_only_passes_structured_payload_and_surfaces_exact_image(monkeypatch):
    payload = _ansible_imagepull_payload()

    def fake_dispatch(tool, params, **kwargs):
        assert tool == "analyze_error"
        assert params["structured_payload"] == payload
        assert params["diagnostic_mode"] == "error_only"
        assert "timed out waiting for the condition" in params["error_text"]
        assert "registry.internal/payments-api:v404" not in params["error_text"]
        assert "ImagePullBackOff" not in params["error_text"]
        return {
            "root_cause": "The requested image tag may be unavailable.",
            "solution": "Verify that the tag is published.",
            "request_evidence": {
                "images": ["registry.internal/payments-api:v404"],
                "condition_reasons": ["ImagePullBackOff"],
            },
        }

    monkeypatch.setattr(agent.chat, "_dispatch", fake_dispatch)
    result = agent._error_only_result(agent.AgentInvokeRequest(input=payload))
    assert "registry.internal/payments-api:v404" in result.reply
    assert result.result["request_evidence"]["condition_reasons"] == [
        "ImagePullBackOff"
    ]


def test_error_only_hallucinated_live_claim_is_audited_not_rewritten(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    original_answer = (
        "I searched all namespaces and the pod was not found in any namespace. "
        "kubectl returned no resources."
    )
    monkeypatch.setattr(
        agent,
        "_execute_agent",
        lambda body, deadline: _result(reply=original_answer),
    )
    events = []
    monkeypatch.setattr(
        agent,
        "_emit_agent_audit_event",
        lambda event, **fields: events.append((event, fields)),
    )
    response = client.post(
        "/api/v1/agent/invoke",
        json={"input": _ansible_imagepull_payload()},
        headers={"Authorization": "Bearer current-token"},
    )
    assert response.status_code == 200
    assert response.json()["answer"] == original_answer
    assert events[0][0] == "hallucinated_evidence"
    assert set(events[0][1]["claim_categories"]) == {
        "first_person_cluster_search",
        "namespace_search_result",
        "kubectl_observation",
    }
    assert "answer" not in events[0][1]


def test_error_only_request_evidence_language_does_not_trigger_audit(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(
        agent,
        "_execute_agent",
        lambda body, deadline: _result(
            reply="The request payload shows ImagePullBackOff."
        ),
    )
    events = []
    monkeypatch.setattr(
        agent,
        "_emit_agent_audit_event",
        lambda event, **fields: events.append((event, fields)),
    )
    response = client.post(
        "/api/v1/agent/invoke",
        json={"input": _ansible_imagepull_payload()},
        headers={"Authorization": "Bearer current-token"},
    )
    assert response.status_code == 200
    assert events == []


def test_hallucinated_evidence_audit_event_uses_shared_audit_log(
    monkeypatch, tmp_path
):
    from config import settings as settings_module
    from k8s.kubectl_runner import _append_audit_entry  # noqa: F401

    audit_path = tmp_path / "audit.log"
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(
            enable_audit_log=True,
            audit_log_path=str(audit_path),
            audit_log_max_bytes=1024 * 1024,
        ),
    )
    agent._emit_agent_audit_event(
        "hallucinated_evidence",
        request_id="req-test",
        diagnostic_mode="error_only",
        claim_categories=["kubectl_observation"],
    )
    text = audit_path.read_text()
    assert "hallucinated_evidence" in text
    assert "request_id=req-test" in text
    assert "claim_categories=[\"kubectl_observation\"]" in text


def test_rate_limit_is_per_token(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, requests_per_minute=1)
    monkeypatch.setattr(agent, "_execute_agent", lambda body, deadline: _result())
    headers = {"Authorization": "Bearer current-token"}
    assert client.post("/api/v1/agent/invoke", json={"input": 1}, headers=headers).status_code == 200
    response = client.post("/api/v1/agent/invoke", json={"input": 2}, headers=headers)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_concurrency_rejection_does_not_leak_slot(monkeypatch, tmp_path):
    client = _client(
        monkeypatch,
        tmp_path,
        max_concurrency=1,
        queue_timeout_seconds=0.02,
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_execute(body, deadline):
        entered.set()
        assert release.wait(timeout=2)
        return _result()

    monkeypatch.setattr(agent, "_execute_agent", blocking_execute)
    headers = {"Authorization": "Bearer current-token"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            client.post,
            "/api/v1/agent/invoke",
            json={"input": "first"},
            headers=headers,
        )
        assert entered.wait(timeout=1)
        second = client.post(
            "/api/v1/agent/invoke",
            json={"input": "second"},
            headers=headers,
        )
        assert second.status_code == 429
        release.set()
        assert first.result(timeout=2).status_code == 200

    # The completed worker released its slot.
    monkeypatch.setattr(agent, "_execute_agent", lambda body, deadline: _result())
    assert client.post(
        "/api/v1/agent/invoke",
        json={"input": "third"},
        headers=headers,
    ).status_code == 200


def test_target_request_rejected_when_flag_off(monkeypatch, tmp_path):
    """P1.A: target requests must be rejected with the flag off."""
    client = _client(monkeypatch, tmp_path, remote_diagnostics_enabled=False)
    response = client.post(
        "/api/v1/agent/invoke",
        json={
            "input": {"error": "boom"},
            "target": {"connection_type": "ssh", "target_id": "qa17"},
        },
        headers={"Authorization": "Bearer current-token"},
    )
    assert response.status_code == 501
    assert "not enabled" in response.json()["detail"].lower()


def test_target_request_rejects_empty_diagnostic_scope(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, remote_diagnostics_enabled=True)
    response = client.post(
        "/api/v1/agent/invoke",
        json={
            "input": {"error": "boom"},
            "target": {
                "connection_type": "ssh",
                "target_id": "qa17",
                "diagnostic_scope": [],
            },
        },
        headers={"Authorization": "Bearer current-token"},
    )
    assert response.status_code == 422


def test_target_request_runs_against_verified_remote_runner(monkeypatch, tmp_path):
    from k8s.kubectl_runner import get_runner, kubectl

    scopes, targets = _remote_auth_and_target()
    fake_runner = _FakeRemoteRunner()
    monkeypatch.setattr(agent, "_create_ssh_runner", lambda remote, timeout: fake_runner)

    def single_shot(req, persist, session_tag, **kwargs):
        assert get_runner() is fake_runner
        return _result()

    monkeypatch.setattr(agent.chat, "_llm_provider", lambda model: None)
    monkeypatch.setattr(agent.chat, "_chat_single_shot", single_shot)
    client = _client(
        monkeypatch,
        tmp_path,
        scope_registry=scopes,
        target_registry=targets,
        known_hosts_path="/mounted/known_hosts",
        remote_diagnostics_enabled=True,
    )
    response = client.post(
        "/api/v1/agent/invoke",
        json={
            "input": {"error": "boom"},
            "target": {"connection_type": "ssh", "target_id": "qa17"},
        },
        headers={"Authorization": "Bearer SCOPED_TOKEN"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["diagnostic_mode"] == "live_cluster_partial"
    assert payload["connection"]["verified"] is True
    assert payload["connection"]["target_id"] == "qa17"
    assert payload["connection"]["kube_system_uid"] == _KUBE_SYSTEM_UID
    assert fake_runner.connected is True
    assert fake_runner.operation_deadline is not None
    assert fake_runner.closed is True
    assert get_runner() is kubectl


def test_identity_mismatch_uses_error_only_without_live_dispatch(monkeypatch, tmp_path):
    scopes, targets = _remote_auth_and_target()
    fake_runner = _FakeRemoteRunner(
        uid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    monkeypatch.setattr(agent, "_create_ssh_runner", lambda remote, timeout: fake_runner)

    dispatched = []

    def safe_dispatch(tool, params, **kwargs):
        dispatched.append(tool)
        return {"root_cause": "input-only diagnosis", "solution": "verify target"}

    monkeypatch.setattr(agent.chat, "_dispatch", safe_dispatch)
    client = _client(
        monkeypatch,
        tmp_path,
        scope_registry=scopes,
        target_registry=targets,
        known_hosts_path="/mounted/known_hosts",
        remote_diagnostics_enabled=True,
    )
    response = client.post(
        "/api/v1/agent/invoke",
        json={
            "input": {"error": "boom"},
            "target": {"connection_type": "ssh", "target_id": "qa17"},
        },
        headers={"Authorization": "Bearer SCOPED_TOKEN"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["diagnostic_mode"] == "error_only"
    assert payload["connection"]["verified"] is False
    assert payload["connection"]["reason"] == "identity_mismatch"
    assert payload["evidence"] == []
    assert dispatched == ["analyze_error"]
    assert fake_runner.closed is True


def test_runner_closes_even_if_context_reset_fails(monkeypatch):
    import k8s.kubectl_runner as kubectl_runner
    from remote_diagnostics_breaker import RemoteCircuitBreaker

    _, targets = _remote_auth_and_target()
    fake_runner = _FakeRemoteRunner()
    monkeypatch.setattr(agent, "_create_ssh_runner", lambda remote, timeout: fake_runner)
    monkeypatch.setattr(agent.chat, "_llm_provider", lambda model: None)
    monkeypatch.setattr(
        agent.chat,
        "_chat_single_shot",
        lambda *args, **kwargs: _result(),
    )
    monkeypatch.setattr(kubectl_runner, "set_runner", lambda runner: object())

    class _BrokenContext:
        @staticmethod
        def reset(token):
            raise RuntimeError("context reset failed")

    monkeypatch.setattr(kubectl_runner, "runner_ctx", _BrokenContext())
    remote = agent._RemoteExecution(
        target=targets.get("qa17"),
        caller_scope_name="qa-ansible",
        known_hosts_path="/mounted/known_hosts",
        connection_timeout_seconds=10,
        diagnostic_timeout_seconds=30,
        breaker=RemoteCircuitBreaker(),
    )

    with pytest.raises(RuntimeError, match="context reset failed"):
        agent._execute_remote_agent(
            agent.AgentInvokeRequest(
                input={"error": "boom"},
                target={
                    "connection_type": "ssh",
                    "target_id": "qa17",
                },
            ),
            agent.time.perf_counter() + 60,
            remote,
        )
    assert fake_runner.closed is True


def test_verified_tool_observation_becomes_live_evidence(monkeypatch, tmp_path):
    scopes, targets = _remote_auth_and_target()
    fake_runner = _FakeRemoteRunner()
    monkeypatch.setattr(agent, "_create_ssh_runner", lambda remote, timeout: fake_runner)
    monkeypatch.setattr(agent.chat, "_llm_provider", lambda model: None)

    client = _client(
        monkeypatch,
        tmp_path,
        scope_registry=scopes,
        target_registry=targets,
        known_hosts_path="/mounted/known_hosts",
        remote_diagnostics_enabled=True,
    )
    run_id = db.create_agent_run(
        session_id=None,
        user_id=None,
        route="agent_invoke_remote",
    )
    db.record_agent_step(
        run_id=run_id,
        iteration=1,
        action="get_events",
        status="ok",
        params={"namespace": "payments"},
        observation_preview="Warning: image pull failed for payments-api",
        duration_ms=12,
    )
    monkeypatch.setattr(
        agent.chat,
        "_chat_single_shot",
        lambda req, persist, session_tag, **kwargs: _result(
            run_id=run_id,
            tool_used="get_events",
        ),
    )

    response = client.post(
        "/api/v1/agent/invoke",
        json={
            "input": {"error": "image pull failed"},
            "target": {"connection_type": "ssh", "target_id": "qa17"},
        },
        headers={"Authorization": "Bearer SCOPED_TOKEN"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["diagnostic_mode"] == "live_cluster"
    assert payload["evidence"] == [
        {
            "source": "kubernetes",
            "tool": "get_events",
            "summary": "Warning: image pull failed for payments-api",
            "target_id": "qa17",
            "observed_at": payload["evidence"][0]["observed_at"],
        }
    ]


def test_ai_synthesis_step_is_not_reported_as_cluster_evidence():
    evidence = agent._evidence_from_steps(
        [
            {
                "step_kind": "tool",
                "status": "ok",
                "action": "cluster_report",
                "observation_preview": "AI-generated report",
            }
        ],
        target_id="qa17",
    )
    assert evidence == []


def test_unauthorized_target_fails_before_worker(monkeypatch, tmp_path):
    from caller_scopes import CallerScope, ScopeRegistry

    _, targets = _remote_auth_and_target()
    scopes = ScopeRegistry(
        scopes={
            "qa-ansible": CallerScope(
                name="qa-ansible",
                current_token="SCOPED_TOKEN",
                previous_token=None,
                allowed_target_ids=frozenset({"qa99"}),
            )
        },
        legacy=None,
    )
    monkeypatch.setattr(
        agent,
        "_execute_agent",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("worker must not run for unauthorized target")
        ),
    )
    client = _client(
        monkeypatch,
        tmp_path,
        scope_registry=scopes,
        target_registry=targets,
        known_hosts_path="/mounted/known_hosts",
        remote_diagnostics_enabled=True,
    )
    response = client.post(
        "/api/v1/agent/invoke",
        json={
            "input": {"error": "boom"},
            "target": {"connection_type": "ssh", "target_id": "qa17"},
        },
        headers={"Authorization": "Bearer SCOPED_TOKEN"},
    )
    assert response.status_code == 403


def test_unknown_target_uses_bounded_metric_label(monkeypatch, tmp_path):
    scopes, targets = _remote_auth_and_target()
    calls = []
    monkeypatch.setattr(
        agent,
        "_connection_metric",
        lambda target_id, status, reason="": calls.append(
            (target_id, status, reason)
        ),
    )
    client = _client(
        monkeypatch,
        tmp_path,
        scope_registry=scopes,
        target_registry=targets,
        known_hosts_path="/mounted/known_hosts",
        remote_diagnostics_enabled=True,
    )
    response = client.post(
        "/api/v1/agent/invoke",
        json={
            "input": {"error": "boom"},
            "target": {
                "connection_type": "ssh",
                "target_id": "attacker-chosen-target",
            },
        },
        headers={"Authorization": "Bearer SCOPED_TOKEN"},
    )
    assert response.status_code == 404
    assert calls == [("__unknown__", "failure", "target_disabled")]


def test_explicit_empty_registry_never_falls_back_to_global(monkeypatch, tmp_path):
    import target_registry as target_registry_module
    from target_registry import TargetRegistry

    scopes, populated_targets = _remote_auth_and_target()
    monkeypatch.setattr(target_registry_module, "_active", populated_targets)
    client = _client(
        monkeypatch,
        tmp_path,
        scope_registry=scopes,
        target_registry=TargetRegistry(targets={}),
        known_hosts_path="/mounted/known_hosts",
        remote_diagnostics_enabled=True,
    )
    response = client.post(
        "/api/v1/agent/invoke",
        json={
            "input": {"error": "boom"},
            "target": {"connection_type": "ssh", "target_id": "qa17"},
        },
        headers={"Authorization": "Bearer SCOPED_TOKEN"},
    )
    assert response.status_code == 404


def test_machine_single_shot_scope_blocks_dispatch(monkeypatch):
    monkeypatch.setattr(
        agent.chat,
        "_keyword_route",
        lambda message, history: {
            "tool": "delete_pod",
            "params": {"namespace": "default", "pod_name": "api"},
        },
    )
    monkeypatch.setattr(
        agent.chat,
        "_dispatch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("out-of-scope tool must not dispatch")
        ),
    )
    response = agent.chat._chat_single_shot(
        agent.chat.ChatRequest(message="delete it", history=[]),
        lambda *args, **kwargs: None,
        "machine",
        tool_scope_override={"get_pods", "analyze_error"},
    )
    assert response.error == "tool_out_of_scope"


def test_target_less_request_unchanged_when_flag_on(monkeypatch, tmp_path):
    """P1.A: enabling the flag does not change the existing target-less path."""
    monkeypatch.setattr(agent, "_execute_agent", lambda body, deadline: _result())
    client = _client(monkeypatch, tmp_path, remote_diagnostics_enabled=True)
    response = client.post(
        "/api/v1/agent/invoke",
        json={"input": {"error": "boom"}},
        headers={"Authorization": "Bearer current-token"},
    )
    assert response.status_code == 200


def test_scope_registry_overrides_legacy_lookup(monkeypatch, tmp_path):
    """When a ScopeRegistry is configured, a scoped token authenticates
    correctly and the legacy unscoped token still works (resolving to the
    LEGACY sentinel scope)."""
    from caller_scopes import ScopeRegistry, _legacy_scope, CallerScope

    scoped = CallerScope(
        name="qa-ansible",
        current_token="SCOPED_TOKEN",
        previous_token=None,
        allowed_target_ids=frozenset({"qa17"}),
    )
    registry = ScopeRegistry(
        scopes={"qa-ansible": scoped},
        legacy=_legacy_scope("current-token", None),
    )

    monkeypatch.setattr(agent, "_execute_agent", lambda body, deadline: _result())

    client = _client(monkeypatch, tmp_path, scope_registry=registry)

    # Scoped token authenticates.
    scoped_response = client.post(
        "/api/v1/agent/invoke",
        json={"input": "x"},
        headers={"Authorization": "Bearer SCOPED_TOKEN"},
    )
    assert scoped_response.status_code == 200

    # Legacy token still authenticates.
    legacy_response = client.post(
        "/api/v1/agent/invoke",
        json={"input": "x"},
        headers={"Authorization": "Bearer current-token"},
    )
    assert legacy_response.status_code == 200

    # Unknown token rejected.
    bad = client.post(
        "/api/v1/agent/invoke",
        json={"input": "x"},
        headers={"Authorization": "Bearer WRONG"},
    )
    assert bad.status_code == 401


def test_scoped_only_router_registers_without_legacy_token(monkeypatch, tmp_path):
    """P1 fix: a deployment that only configures scoped tokens (no legacy
    AGENT_API_TOKEN) must register the route. Legacy bearer attempts return
    401; the scoped token authenticates correctly."""
    from caller_scopes import CallerScope, ScopeRegistry

    scoped = CallerScope(
        name="qa-ansible",
        current_token="SCOPED",
        previous_token=None,
        allowed_target_ids=frozenset({"qa17"}),
    )
    registry = ScopeRegistry(scopes={"qa-ansible": scoped}, legacy=None)

    monkeypatch.setattr(agent, "_execute_agent", lambda body, deadline: _result())

    # Build a router with NO primary token (scoped-only).
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "invoke.db"))
    db.init_db()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "req-test"
        return await call_next(request)

    app.include_router(
        agent.create_router(
            primary_token="",  # scoped-only — no legacy
            scope_registry=registry,
        )
    )
    client = TestClient(app)

    # Scoped token works
    ok = client.post(
        "/api/v1/agent/invoke",
        json={"input": "x"},
        headers={"Authorization": "Bearer SCOPED"},
    )
    assert ok.status_code == 200

    # No auth header → 401
    no_auth = client.post("/api/v1/agent/invoke", json={"input": "x"})
    assert no_auth.status_code == 401

    # Empty-string bearer → 401 (cannot exploit primary_token="" parity)
    empty = client.post(
        "/api/v1/agent/invoke",
        json={"input": "x"},
        headers={"Authorization": "Bearer "},
    )
    assert empty.status_code == 401

    # Wrong token → 401
    bad = client.post(
        "/api/v1/agent/invoke",
        json={"input": "x"},
        headers={"Authorization": "Bearer WRONG"},
    )
    assert bad.status_code == 401


def test_router_from_environment_rejects_orphan_previous_token(monkeypatch):
    """P1 fix: AGENT_API_TOKEN_PREVIOUS without AGENT_API_TOKEN is a
    misconfiguration that the previous code silently accepted as a legacy
    bearer. router_from_environment must fail at startup."""
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_API_TOKEN_PREVIOUS", "ORPHAN")
    monkeypatch.delenv("AGENT_API_CALLER_SCOPES_PATH", raising=False)

    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="without AGENT_API_TOKEN"):
        agent.router_from_environment()


def test_router_from_environment_scoped_only_end_to_end(monkeypatch, tmp_path):
    """End-to-end coverage of router_from_environment in scoped-only mode:
    no AGENT_API_TOKEN, only AGENT_API_CALLER_SCOPES_PATH + a projected
    token tree. Exercises the YAML→registry→router path that the
    direct-create_router test in this file deliberately bypasses."""
    import textwrap
    import caller_scopes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Lay out the projected Secret tree the chart would create.
    tokens_root = tmp_path / "tokens"
    qa_dir = tokens_root / "qa-ansible"
    qa_dir.mkdir(parents=True)
    (qa_dir / "current").write_text("SCOPED_TOKEN_VALUE")

    scopes_yaml = tmp_path / "scopes.yaml"
    scopes_yaml.write_text(textwrap.dedent(
        f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa17]
        """
    ))

    # Reset through monkeypatch so pytest restores state even if an assertion
    # fails partway through this test.
    monkeypatch.setattr(caller_scopes, "_active", None)

    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_API_TOKEN_PREVIOUS", raising=False)
    monkeypatch.setenv("AGENT_API_CALLER_SCOPES_PATH", str(scopes_yaml))
    monkeypatch.setenv("AGENT_API_TOKEN_MOUNT_ROOT", str(tokens_root))

    # The scoped-only router must register — no legacy token needed.
    router = agent.router_from_environment()
    assert router is not None, "scoped-only env config must produce a router"

    # The active scope registry on the module must reflect what we loaded.
    active = caller_scopes.get_active()
    assert "qa-ansible" in active.scopes
    assert active.legacy is None

    # Mount and exercise the router. The scoped token authenticates;
    # nothing else does.
    monkeypatch.setattr(agent, "_execute_agent", lambda body, deadline: _result())
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "invoke.db"))
    db.init_db()
    app = FastAPI()

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "req-test"
        return await call_next(request)

    app.include_router(router)
    client = TestClient(app)

    ok = client.post(
        "/api/v1/agent/invoke",
        json={"input": "x"},
        headers={"Authorization": "Bearer SCOPED_TOKEN_VALUE"},
    )
    assert ok.status_code == 200

    # No legacy token exists — any other bearer must fail.
    bad = client.post(
        "/api/v1/agent/invoke",
        json={"input": "x"},
        headers={"Authorization": "Bearer NOT_THE_SCOPED_TOKEN"},
    )
    assert bad.status_code == 401


def test_router_from_environment_remote_enabled_loads_target_registry(
    monkeypatch, tmp_path
):
    import textwrap
    import target_registry

    credential_root = tmp_path / "credentials"
    credential_path = credential_root / "qa17"
    credential_path.mkdir(parents=True)
    targets_yaml = tmp_path / "targets.yaml"
    targets_yaml.write_text(
        textwrap.dedent(
            f"""\
            targets:
              qa17:
                display_name: QA 17
                environment_group: qa
                connection:
                  type: ssh
                  host: qa17-control.internal
                  port: 22
                  username: diagnostic
                  known_hosts_alias: qa17-control
                  credential_path: {credential_path}
                expected_kube_system_uid: {_KUBE_SYSTEM_UID}
                diagnostic_scopes_allowed: [kubernetes]
                allowed_caller_scopes: [qa-ansible]
            """
        )
    )
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("qa17-control ssh-ed25519 AAAATEST\n")

    monkeypatch.setattr(target_registry, "_active", None)
    monkeypatch.setenv("AGENT_API_TOKEN", "legacy-token")
    monkeypatch.delenv("AGENT_API_TOKEN_PREVIOUS", raising=False)
    monkeypatch.delenv("AGENT_API_CALLER_SCOPES_PATH", raising=False)
    monkeypatch.setenv("AGENT_API_REMOTE_DIAGNOSTICS_ENABLED", "true")
    monkeypatch.setenv("AGENT_API_TARGETS_PATH", str(targets_yaml))
    monkeypatch.setenv("AGENT_API_TARGET_CREDENTIAL_ROOT", str(credential_root))
    monkeypatch.setenv("SSH_KNOWN_HOSTS_PATH", str(known_hosts))

    router = agent.router_from_environment()
    assert router is not None
    assert target_registry.get_active().get("qa17") is not None


def test_router_from_environment_remote_enabled_requires_registry(monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", "legacy-token")
    monkeypatch.delenv("AGENT_API_TOKEN_PREVIOUS", raising=False)
    monkeypatch.delenv("AGENT_API_CALLER_SCOPES_PATH", raising=False)
    monkeypatch.setenv("AGENT_API_REMOTE_DIAGNOSTICS_ENABLED", "true")
    monkeypatch.delenv("AGENT_API_TARGETS_PATH", raising=False)

    import pytest

    with pytest.raises(RuntimeError, match="AGENT_API_TARGETS_PATH is required"):
        agent.router_from_environment()


def test_runner_operation_deadline_uses_monotonic_clock(monkeypatch):
    """Regression: the router must translate its perf_counter deadline into the
    monotonic clock domain that SSHKubectlRunner.set_operation_deadline expects.
    Simulate a large offset between the two clocks and verify the runner
    receives a value near now-in-monotonic, not a stale perf_counter value that
    would look like a deadline in the past.
    """
    import time as _time
    from remote_diagnostics_breaker import RemoteCircuitBreaker

    _, targets = _remote_auth_and_target()
    fake_runner = _FakeRemoteRunner()
    monkeypatch.setattr(agent, "_create_ssh_runner", lambda remote, timeout: fake_runner)
    monkeypatch.setattr(agent.chat, "_llm_provider", lambda model: None)
    monkeypatch.setattr(agent.chat, "_chat_single_shot", lambda *a, **k: _result())

    # 10000-second offset — perf_counter << monotonic. Before the fix, the
    # runner would compute `perf_counter_value - time.monotonic()` and get a
    # very negative remaining time on the first kubectl call.
    real_monotonic = _time.monotonic
    monkeypatch.setattr(
        "routers.agent.time.monotonic",
        lambda: real_monotonic() + 10000.0,
    )

    remote = agent._RemoteExecution(
        target=targets.get("qa17"),
        caller_scope_name="qa-ansible",
        known_hosts_path="/mounted/known_hosts",
        connection_timeout_seconds=10,
        diagnostic_timeout_seconds=30,
        breaker=RemoteCircuitBreaker(),
    )
    outcome = agent._execute_remote_agent(
        agent.AgentInvokeRequest(
            input={"error": "boom"},
            target={"connection_type": "ssh", "target_id": "qa17"},
        ),
        _time.perf_counter() + 60,
        remote,
    )
    # If the deadline had been left in perf_counter domain, the runner would
    # have seen a value ~10000s in its past and the code would have raised
    # TimeoutError before verification, collapsing to error_only.
    assert outcome.diagnostic_mode == "live_cluster_partial"
    assert outcome.connection.verified is True
    assert fake_runner.operation_deadline is not None
    # The installed deadline must be within a reasonable window of the mocked
    # monotonic "now" (offset + real_monotonic()). Allow the full diagnostic
    # phase budget of 30s plus slack.
    monotonic_now = real_monotonic() + 10000.0
    assert monotonic_now <= fake_runner.operation_deadline <= monotonic_now + 60


def test_post_verification_remote_error_stays_live_partial(monkeypatch):
    """Regression: a RemoteConnectionError raised after identity verification
    (e.g. from a reconnect during a ReAct tool call) must not roll the response
    back to error_only. Live/verified state is truthful once probe_resolved."""
    from k8s.remote_connection import RemoteConnectionError
    from remote_diagnostics_breaker import RemoteCircuitBreaker

    _, targets = _remote_auth_and_target()
    fake_runner = _FakeRemoteRunner()
    monkeypatch.setattr(agent, "_create_ssh_runner", lambda remote, timeout: fake_runner)
    monkeypatch.setattr(agent.chat, "_llm_provider", lambda model: None)

    def failing_single_shot(*args, **kwargs):
        raise RemoteConnectionError("auth_failed")

    monkeypatch.setattr(agent.chat, "_chat_single_shot", failing_single_shot)
    breaker = RemoteCircuitBreaker()
    remote = agent._RemoteExecution(
        target=targets.get("qa17"),
        caller_scope_name="qa-ansible",
        known_hosts_path="/mounted/known_hosts",
        connection_timeout_seconds=10,
        diagnostic_timeout_seconds=30,
        breaker=breaker,
    )
    outcome = agent._execute_remote_agent(
        agent.AgentInvokeRequest(
            input={"error": "boom"},
            target={"connection_type": "ssh", "target_id": "qa17"},
        ),
        agent.time.perf_counter() + 60,
        remote,
    )
    assert outcome.diagnostic_mode == "live_cluster_partial"
    assert outcome.connection.verified is True
    assert outcome.connection.kube_system_uid == _KUBE_SYSTEM_UID
    # Reason must be null so the response validator accepts the live shape.
    assert outcome.connection.reason is None
    # Breaker was already resolved by record_success in preflight; the
    # subsequent failure must not double-record against the target.
    state, _ = breaker.snapshot("qa17")
    assert state == "closed"
    assert fake_runner.closed is True
