import json

from ai_tools import analyze
from services.error_parser import (
    classify_error,
    extract_context,
    reconcile_category,
)
from services.llm_service import LLMService


def _ansible_k8s_timeout_payload(*, reason: str = "ImagePullBackOff") -> dict:
    return {
        "changed": False,
        "msg": "Deployment update timed out waiting for the condition",
        "result": {
            "apiVersion": "apps/v1",
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
                        "type": "Progressing",
                        "status": "False",
                        "reason": reason,
                        "message": "Workload did not become ready",
                    }
                ]
            },
        },
    }


def test_manifest_unknown_backoff_is_pod_image():
    error = (
        "Back-off pulling image "
        "registry.example.invalid/payments:v404: manifest unknown"
    )
    assert classify_error(error, "kubernetes") == "pod_image"


def test_existing_crashloop_classification_is_unchanged():
    assert (
        classify_error("Back-off restarting failed container api", "kubernetes")
        == "pod_crashloop"
    )


def test_deterministic_category_wins_over_llm_disagreement():
    assert reconcile_category("pod_image", "pod_crashloop") == (
        "pod_image",
        "deterministic",
    )


def test_known_llm_category_can_classify_general_failure():
    assert reconcile_category("general_failure", "networking") == (
        "networking",
        "llm",
    )


def test_unknown_llm_category_is_rejected():
    assert reconcile_category("general_failure", "made_up") == (
        "general_failure",
        "deterministic",
    )


def test_ansible_k8s_payload_uses_structured_imagepull_signal():
    payload = _ansible_k8s_timeout_payload()
    context = extract_context(
        payload["msg"],
        "kubernetes",
        structured_payload=payload,
    )
    assert context["category"] == "pod_image"
    assert context["category_source"] == "structured"
    assert context["request_evidence"]["images"] == [
        "registry.internal/payments-api:v404"
    ]
    assert context["request_evidence"]["condition_reasons"] == [
        "ImagePullBackOff"
    ]
    assert context["request_evidence"]["resource"] == {
        "kind": "Deployment",
        "name": "payments-api",
        "namespace": "payments",
    }


def test_structured_deployment_timeout_uses_coarse_fallback_not_unknown():
    payload = _ansible_k8s_timeout_payload(reason="WaitingForRollout")
    context = extract_context(
        payload["msg"],
        "kubernetes",
        structured_payload=payload,
    )
    assert context["category"] == "deployment_timeout_generic"
    assert context["category_source"] == "structured_fallback"


def test_error_only_system_prompt_forbids_live_observation_claims():
    captured = {}

    class _Provider:
        enabled = True
        name = "test"

        def generate(self, prompt, *, system, temperature):
            captured["system"] = system
            return json.dumps(
                {
                    "root_cause": "request-only diagnosis",
                    "category": "pod_image",
                }
            )

    service = LLMService(provider=_Provider())
    service.analyze(
        "timeout",
        {
            "tool": "kubernetes",
            "category": "pod_image",
            "diagnostic_mode": "error_only",
            "request_evidence": {"images": ["repo/app:v404"]},
        },
    )
    system = captured["system"]
    assert "You have NOT connected to or queried a live Kubernetes cluster" in system
    assert 'Do not say "I searched"' in system
    assert '"kubectl returned"' in system


def test_analyze_response_includes_classification_provenance(monkeypatch):
    monkeypatch.setattr(analyze.embeddings, "embed", lambda value: [0.0])
    monkeypatch.setattr(analyze.vector_db, "search", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        analyze.llm_service,
        "analyze",
        lambda *args, **kwargs: {
            "category": "pod_crashloop",
            "root_cause": "tag is absent",
            "solution": "publish the tag",
        },
    )
    result = json.loads(
        analyze.run(
            "Back-off pulling image repo/app:v404: manifest unknown",
            "kubernetes",
            "test",
        )
    )
    assert result["category"] == "pod_image"
    assert result["classification"] == {
        "source": "deterministic",
        "deterministic_category": "pod_image",
        "llm_category": "pod_crashloop",
    }


def test_analyze_response_surfaces_structured_request_evidence(monkeypatch):
    payload = _ansible_k8s_timeout_payload()
    monkeypatch.setattr(analyze.embeddings, "embed", lambda value: [0.0])
    monkeypatch.setattr(analyze.vector_db, "search", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        analyze.llm_service,
        "analyze",
        lambda *args, **kwargs: {
            "category": "unknown",
            "root_cause": "tag is absent",
            "solution": "publish the tag",
        },
    )
    result = json.loads(
        analyze.run(
            payload["msg"],
            "kubernetes",
            "test",
            structured_payload=payload,
            diagnostic_mode="error_only",
        )
    )
    assert result["category"] == "pod_image"
    assert result["classification"]["source"] == "structured"
    assert result["request_evidence"]["images"] == [
        "registry.internal/payments-api:v404"
    ]
