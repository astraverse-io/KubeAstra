"""Kubeastra Playbook Engine — codified debugging patterns.

A playbook is a YAML-defined investigation template with:
- Trigger conditions (error pattern regex, pod status, event type)
- Ordered tool steps to execute
- An LLM synthesis prompt that turns gathered data into a diagnosis

Playbooks can be:
- Built-in: shipped with Kubeastra under playbooks/builtins/
- Custom: loaded from a user-configured directory (PLAYBOOK_DIR env var)

Usage:
    from playbooks import registry, runner

    # Find matching playbooks for a failure mode
    matches = registry.match("CrashLoopBackOff")

    # Execute a playbook
    result = runner.run("crashloop", namespace="demo", pod_name="my-app")
"""

from playbooks.schema import Playbook, PlaybookStep, PlaybookTrigger
from playbooks.registry import (
    get_playbook,
    list_playbooks,
    match_playbooks,
    register_playbook,
)
from playbooks.runner import run_playbook

__all__ = [
    "Playbook",
    "PlaybookStep",
    "PlaybookTrigger",
    "get_playbook",
    "list_playbooks",
    "match_playbooks",
    "register_playbook",
    "run_playbook",
]
