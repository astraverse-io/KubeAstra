from __future__ import annotations
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import audit  # noqa: E402


def test_pr_opened_is_its_own_event_not_a_mutation():
    assert audit.EventType.GITOPS_PR_OPENED == "gitops.pr_opened"
    assert audit.EventType.GITOPS_PR_OPENED != audit.EventType.MUTATION_EXECUTED
