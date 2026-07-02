"""Best-effort secret redaction before writing to the shared KB.

This is a tripwire, not a vault. The goal is to catch the obvious cases —
API keys pasted into the chat, bearer tokens echoed by kubectl, base64
blobs from secret describes — so they don't end up searchable in
session_memory. It will miss novel patterns; treat it as defense in
depth alongside "don't paste secrets into the assistant" team norms.

Patterns are intentionally conservative: when a token *could* be a
secret, redact it. False positives (a legitimate string replaced with
``<REDACTED>``) are far cheaper than a real key leaking into the KB.
"""

from __future__ import annotations

import re
from typing import Iterable

# Each pattern is ``(name, compiled_regex)``. Order matters: longer/more
# specific patterns first so generic catch-alls don't gobble specifics.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Google API key (AIzaSy...)
    ("gcp_api_key",
     re.compile(r"AIza[0-9A-Za-z_\-]{32,40}")),
    # AWS access key id
    ("aws_access_key",
     re.compile(r"\b(?:AKIA|ASIA|AGPA|AROA|AIPA|ANPA|ANVA|ABIA)[0-9A-Z]{16}\b")),
    # AWS secret access key (high entropy 40-char base64)
    ("aws_secret",
     re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9/+=]{40})[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    # GitHub personal access tokens / fine-grained tokens / OAuth
    ("github_token",
     re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    # OpenAI keys
    ("openai_key",
     re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    # Slack tokens
    ("slack_token",
     re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    # JWT (three base64 segments separated by dots, ≥20 chars each segment)
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    # Bearer/Token headers — replace value portion
    ("bearer_header",
     re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([A-Za-z0-9_\-.=]+)")),
    # `token: <value>` in YAML (kubeconfig, etc.)
    ("yaml_token",
     re.compile(r"(?im)^(\s*token\s*:\s*)([^\s#]+)")),
    # PEM private key blocks
    ("pem_private",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    # Long base64 blobs (likely Secret data). Conservative: ≥80 chars.
    ("base64_blob",
     re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/])")),
]

# Patterns that need a referenced-group replacement (so we keep the prefix
# but blot out the value). Names match _PATTERNS entries above.
_PRESERVES_PREFIX = {"bearer_header", "yaml_token"}


def redact(text: str) -> str:
    """Return ``text`` with likely secrets replaced by ``<REDACTED:kind>``.

    Empty / None inputs pass through unchanged. Pattern misses are
    expected; this is a coarse net, not a guarantee.
    """
    if not text:
        return text
    out = text
    for name, pat in _PATTERNS:
        if name in _PRESERVES_PREFIX:
            out = pat.sub(lambda m: f"{m.group(1)}<REDACTED:{name}>", out)
        else:
            out = pat.sub(f"<REDACTED:{name}>", out)
    return out


def redact_dict(d: dict, fields: Iterable[str] = ()) -> dict:
    """Return a shallow copy with named string fields redacted.

    Useful before persisting a payload — pass the field names that are
    free-text user input (``question``, ``resolution``, etc.). Unlisted
    fields are left as-is so we don't mangle structured metadata.
    """
    if not d:
        return d
    out = dict(d)
    for f in fields:
        v = out.get(f)
        if isinstance(v, str):
            out[f] = redact(v)
    return out
