"""Regression tests for the CodeQL hardening fixes (pre-existing main alerts).

- #151 py/polynomial-redos: the PEM key-type label is bounded, killing the
  ambiguous backtracking while still redacting every real PEM private key.
- #153 py/command-line-injection: kubectl context names are confined to a safe
  allowlist with an alphanumeric first char, so a value can't be read as a
  kubectl flag when passed to the connectivity-check subprocess.
"""

import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for p in (BACKEND_DIR, MCP_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import redaction_kv  # noqa: E402
from routers.cluster import _CONTEXT_NAME_RE  # noqa: E402


class TestPemRedactionBounded:
    def test_redacts_real_pem_key_types(self):
        for label in ("RSA", "EC", "OPENSSH", "ENCRYPTED", "DSA", ""):
            body = f"-----BEGIN {label + ' ' if label else ''}PRIVATE KEY-----\nAAAABBBB\n-----END {label + ' ' if label else ''}PRIVATE KEY-----"
            out = redaction_kv._redact_pem(body)
            assert "AAAABBBB" not in out, f"{label!r} key not redacted"
            assert redaction_kv.PEM_REDACTION in out

    def test_open_ended_pem_is_redacted(self):
        body = "prefix -----BEGIN RSA PRIVATE KEY-----\nsecretmaterial"
        out = redaction_kv._redact_pem(body)
        assert "secretmaterial" not in out

    def test_no_pathological_backtracking(self):
        # Before bounding, a long run of the ambiguous class with no END marker
        # caused polynomial blow-up. Bounded, this returns effectively instantly.
        evil = "-----BEGIN " + ("A" * 60_000) + " PRIVATE KEY-----" + ("B" * 60_000)
        start = time.perf_counter()
        redaction_kv._redact_pem(evil)
        assert time.perf_counter() - start < 1.0

    def test_non_pem_text_unchanged(self):
        text = "kind: Deployment\nname: api\n"
        assert redaction_kv._redact_pem(text) == text


class TestContextNameAllowlist:
    def test_accepts_real_context_names(self):
        for name in (
            "minikube", "kind-kind", "gke_my-proj_us-central1_prod",
            "arn:aws:eks:us-east-1:123456789012:cluster/prod",
            "user@cluster.example.com", "docker-desktop",
        ):
            assert _CONTEXT_NAME_RE.match(name), f"{name!r} should be allowed"

    def test_rejects_flag_like_and_injection(self):
        for name in (
            "--kubeconfig=/etc/passwd",   # leading '-' → could be read as a flag
            "-x",
            "",
            "ctx with space",
            "ctx\nname",                  # newline
            "ctx;rm -rf /",               # shell metachar (harmless in list form, still refused)
            "ctx$(whoami)",
        ):
            assert not _CONTEXT_NAME_RE.match(name), f"{name!r} should be refused"
