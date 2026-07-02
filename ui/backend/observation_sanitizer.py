"""Single chokepoint for sanitizing tool observations before they reach the LLM.

The LLM cannot output what it never sees. Every tool observation that flows
back into the next ReAct iteration's prompt — or into the LLM-side context
summarizer — passes through ``sanitize_observation`` first. That makes the
redaction deterministic at the input boundary instead of relying on the model
to refrain from echoing a secret it was just shown.

Two redactors are pipelined because neither alone covers both classes of leak:

1. ``redaction_kv.redact_prose`` — keyword-anchored: ``password: x``, ``token=y``,
   PEM blocks, ``kind: Secret`` data maps. Misses raw secret-shaped tokens
   without a keyword anchor.
2. ``redaction_entropy.redact`` — high-entropy / known-prefix: GCP ``AIza…``,
   AWS ``AKIA…``, GitHub ``ghp_…``, OpenAI ``sk-…``, Slack ``xox…``, JWT
   ``eyJ…``, bearer headers, YAML ``token:`` line, PEM, long base64 blobs.
   Misses custom-named credentials with non-pattern values.

Order matters: prose pass first preserves the readable
``token: ***redacted***`` form. Running entropy first would still mask the
secret but leave less consistent traces for human reviewers.
"""
from __future__ import annotations

from redaction_kv import redact_prose
from redaction_entropy import redact


def sanitize_observation(text: str, cap: int) -> str:
    """Apply the two-pass redaction pipeline.

    ``cap`` is a *soft* size target, not a hard ceiling — ``redact_prose``'s
    truncate appends a ``…[truncated, N bytes total]`` suffix that goes past
    ``cap``, and the entropy pass may slightly expand output for some patterns
    (e.g. a 20-char AWS access key → 25-char ``<REDACTED:aws_access_key>``
    marker). The output is typically within a small constant of ``cap``;
    callers that need a strict size bound should re-truncate themselves.
    """
    if not text:
        return text or ""
    return redact(redact_prose(text, cap))
