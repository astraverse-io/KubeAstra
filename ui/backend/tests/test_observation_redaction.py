"""Phase 0 — secret redaction at the observation boundary.

Verifies that ``sanitize_observation`` pipelines both redactors and that the
chokepoint is wired into ``react._truncate_observation`` so secrets never reach
the next ReAct prompt or the persisted observation preview.
"""
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from observation_sanitizer import sanitize_observation
import react


# A fake JWT body — 3 segments, each ≥10 base64-ish chars, dot-separated.
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)

# Assembled at import rather than written out, so the finished string never
# appears as a literal in the source.
#
# It was a literal, and GitHub's secret scanner flagged it as a Google API Key
# in the repository's first commit. The value is obviously synthetic — the body
# is the alphabet — but the scanner matches on prefix and shape, not on whether
# a human would be fooled, and it is right to: "it's only a test fixture" is
# indistinguishable at scan time from a real key someone pasted into a test.
#
# The cost of leaving it was not the alert but push protection, now enabled on
# this repo: it blocks a push containing a provider-shaped secret, so every
# future edit to this file risked being rejected over a string that was never
# a credential. Building it from parts keeps the test honest and the scanner
# quiet, without weakening what is being asserted — the value the redactor sees
# at runtime is byte-for-byte what it was before.
_FAKE_GCP_KEY = "AIza" + "Sy" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "_123456"


def test_jwt_in_log_line_is_redacted_by_entropy_pass():
    """A bare JWT in a log line with no keyword anchor must be caught by the
    entropy redactor (the keyword pass doesn't have an anchor to match on)."""
    log_line = f"2026-06-22T10:00:00Z error decoding session: {_FAKE_JWT}"
    out = sanitize_observation(log_line, cap=1000)
    assert _FAKE_JWT not in out, "raw JWT must not survive sanitization"
    assert "<REDACTED:jwt>" in out


def test_keyword_anchored_password_is_redacted_by_kv_pass():
    """``password: hunter2`` has no entropy-matching pattern in the value but
    the keyword pass catches it (the ``password`` token sits on a word
    boundary, so the inline-secret regex anchors on it)."""
    log_line = "starting service with password: hunter2"
    out = sanitize_observation(log_line, cap=1000)
    assert "hunter2" not in out
    assert "***redacted***" in out


def test_documented_blind_spot_underscored_keyword_and_low_entropy_value():
    """Known limitation: ``db_password: hunter2`` slips through because the
    ``\\b`` word-boundary anchor doesn't fire between ``_`` and ``password``
    (both are word characters), and the value ``hunter2`` is too short / not
    secret-shaped to match any entropy pattern.

    This test pins the blind spot so future regex changes that close the gap
    cause an intentional update here rather than silent behavior drift.
    """
    log_line = "starting service with db_password: hunter2"
    out = sanitize_observation(log_line, cap=1000)
    # If this assertion ever fails because someone broadened the keyword regex
    # to allow underscored prefixes, that's good news — update this test then.
    assert "hunter2" in out


def test_gcp_api_key_is_redacted():
    """A bare GCP API key (``AIza...``) is high-entropy with a known prefix."""
    log_line = f"fetched config with {_FAKE_GCP_KEY}"
    out = sanitize_observation(log_line, cap=1000)
    assert _FAKE_GCP_KEY not in out
    assert "<REDACTED:gcp_api_key>" in out


def test_bearer_header_value_is_redacted():
    """An ``Authorization: Bearer <token>`` header is caught by the entropy
    redactor (the prefix is preserved, the value is blotted)."""
    log_line = "request headers: Authorization: Bearer abc.def.ghi-123"
    out = sanitize_observation(log_line, cap=1000)
    assert "abc.def.ghi-123" not in out
    assert "<REDACTED:bearer_header>" in out
    # The "Authorization: Bearer " prefix is preserved by the entropy redactor.
    assert "Authorization: Bearer " in out


def test_pem_private_key_block_is_redacted():
    """A PEM private key block (BEGIN...END) is redacted whole by both passes —
    redundant coverage is fine; the output must be idempotent."""
    pem_body = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxxxxYYYYzzzz...\n"
        "MoreBase64BodyHereThatLooksReal==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    log_line = f"loaded key:\n{pem_body}\ndone"
    out = sanitize_observation(log_line, cap=10000)
    assert "MIIEowIBAAKCAQEAxxxxYYYYzzzz" not in out
    assert "MoreBase64BodyHereThatLooksReal" not in out
    # Either redaction marker is acceptable — both passes catch PEM blocks.
    assert ("***redacted (private key)***" in out) or ("<REDACTED:pem_private>" in out)


def test_plain_text_passes_through_unchanged():
    """Defense against false-positive regressions: an ordinary log line with
    no secret-shaped content must come out byte-identical."""
    log_line = "2026-06-22T10:00:00Z INFO pod nginx-abc-123 ready in 2.4s namespace=default"
    out = sanitize_observation(log_line, cap=1000)
    assert out == log_line


def test_sanitize_observation_is_idempotent():
    """Running the sanitizer twice must produce identical output. Observations
    flow through multiple boundary hooks (prompt building, compaction,
    persistence) — double-sanitization must not mangle already-redacted text."""
    log_line = (
        f"token: {_FAKE_JWT} and password: hunter2 and "
        f"key={_FAKE_GCP_KEY}"
    )
    once = sanitize_observation(log_line, cap=1000)
    twice = sanitize_observation(once, cap=1000)
    assert once == twice, "sanitizer must be idempotent"


def test_sanitize_observation_handles_empty_and_none():
    """Edge cases — must not raise."""
    assert sanitize_observation("", cap=100) == ""
    assert sanitize_observation(None, cap=100) == ""  # type: ignore[arg-type]


def test_truncate_observation_sanitizes_dict_result():
    """End-to-end: react._truncate_observation must produce sanitized output
    even when the secret is buried inside a dict tool result."""
    result = {
        "logs": f"app started\nerror: failed to decode token={_FAKE_JWT}\ndone",
        "pod_name": "my-pod",
    }
    out = react._truncate_observation(result, tool="get_pod_logs")
    assert _FAKE_JWT not in out


def test_truncate_observation_sanitizes_string_result():
    """Non-dict input path must also flow through sanitization."""
    out = react._truncate_observation(
        f"raw log output: {_FAKE_GCP_KEY}",  # type: ignore[arg-type]
        tool="get_pod_logs",
    )
    assert _FAKE_GCP_KEY not in out
