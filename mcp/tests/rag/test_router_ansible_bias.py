"""Unit tests for router Ansible-error detection (plan §11.5).

Verifies _looks_like_ansible_error covers the documented patterns and
doesn't false-positive on common non-Ansible questions.
"""
from __future__ import annotations

import pytest

from services.rag.router import _looks_like_ansible_error


# ── Positives ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "TASK [kubernetes/kube_check_health : Check kubernetes Nodes] *** fatal:",
    "PLAY RECAP failed: deploy AWX execution environment",
    "fatal: [host]: FAILED!",
    "Running ansible-playbook -i inventory site.yaml",
    "ok: [worker-1] => something",
    "changed: [worker-1] => something",
    "failed: [host1]: msg unreachable",
    "UNREACHABLE! ping failed on host",
    "Failed to import the required Python library (kubernetes)",
    "kubernetes.core.k8s_info module returned error",
    "ansible.builtin.shell module timeout",
    "community.general.timezone error",
    "awx.awx.execution_environment returned 401",
    "ansible.windows.win_shell failed",
    "what is the Ansible playbook that used to deploy RabbitMQ",
    "which playbook deploys rabbitmq",
    "show the deployment-provisioning source for rabbitmq",
    "which role deploys rabbitmq",
    "where is group_vars for rabbitmq",
    "show inventory settings for platform",
    "find the template used by helm_rabbitmq",
    "what defaults configure kube_keepalived",
])
def test_positives_detected(q):
    assert _looks_like_ansible_error(q), f"expected positive: {q!r}"


# ── Negatives ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "how do I get my pod logs?",
    "kubectl get pods shows CrashLoopBackOff",
    "what is the standard kubernetes deployment yaml",
    "docker container restart not working",
    "install python package via pip on Ubuntu",
    "TASK MANAGER not opening on Windows",
    "I want to learn about Helm charts",
    "show me the namespaces in my cluster",
    "",
    "   ",
])
def test_negatives_not_detected(q):
    assert not _looks_like_ansible_error(q), f"expected negative: {q!r}"
