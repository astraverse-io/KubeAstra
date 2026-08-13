from __future__ import annotations
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from gitops.locate import FieldChange  # noqa: E402
from gitops.render import render_pr, _slugify, _is_loopback  # noqa: E402

CHANGE = FieldChange(kind="Deployment", name="api-gateway", namespace="prod",
                     field_path=("spec", "replicas"), new_value=5, reason="3x traffic")
DIAG = {"title": "Replicas too low for load", "severity": "sev-1",
        "confidence": 0.9, "evidence": ["p99 142ms", "CPU throttled"]}


def test_branch_and_title_shape():
    spec = render_pr(diagnosis=DIAG, diff="- 3\n+ 5", change=CHANGE,
                     investigation_id="inv_abcdef1234", cluster="gke-prod",
                     tool_call_count=5, app_base_url=None, session_id="s1")
    assert spec.branch.startswith("kubeastra/")
    assert "inv_abc" in spec.branch  # short hash for uniqueness
    assert "api-gateway" in spec.title.lower()


def test_evidence_inline_and_no_loopback_links():
    spec = render_pr(diagnosis=DIAG, diff="- 3\n+ 5", change=CHANGE,
                     investigation_id="inv_abcdef1234", cluster="gke-prod",
                     tool_call_count=5, app_base_url="http://127.0.0.1:8765", session_id="s1")
    assert "p99 142ms" in spec.body           # evidence embedded
    assert "127.0.0.1" not in spec.body        # loopback link suppressed


def test_public_base_url_becomes_a_link():
    spec = render_pr(diagnosis=DIAG, diff="- 3\n+ 5", change=CHANGE,
                     investigation_id="inv_abcdef1234", cluster="gke-prod",
                     tool_call_count=5, app_base_url="https://kubeastra.example.com", session_id="s1")
    assert "https://kubeastra.example.com/audit?session=s1" in spec.body


def test_slugify_and_loopback_helpers():
    assert _slugify("Replicas too LOW!") == "replicas-too-low"
    assert len(_slugify("a" * 100)) == 40
    assert _is_loopback("http://127.0.0.1:8765") and _is_loopback("http://localhost:3000")
    assert not _is_loopback("https://kubeastra.example.com")
