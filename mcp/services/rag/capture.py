"""Phase 1.3 — dynamic session capture.

After a chat completes, decide via a cheap LLM call whether the session
solved a real problem. If yes, write a compact entry to the
``session_memory`` Qdrant collection (verified=False, 90-day TTL). A
later thumbs-up promotes it to ``runbook`` (handled in ``promotion.py``).

Designed to be fire-and-forget from the chat endpoint:
  - never raises into the caller; failure is logged and ignored
  - opt-in via ``settings.session_capture_enabled``
  - returns the new entry's ID on success, ``None`` otherwise
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config.settings import get_settings
from services.embeddings import embeddings
from services.rag.redaction import redact, redact_dict
from services.rag.schema import SESSION_MEMORY
from services.vector_db import vector_db

logger = logging.getLogger(__name__)


_CLASSIFIER_SYSTEM = (
    "You evaluate transcripts of a Kubernetes DevOps assistant. "
    "Decide if the session solved a *real* problem worth saving as a "
    "future-reusable runbook candidate. Be strict — chitchat, abandoned "
    "investigations, and vague answers all count as NOT worthy."
)


_CLASSIFIER_PROMPT = """Was this session a real problem-solving conversation?

A worthy session must satisfy ALL of:
  - The user described a concrete problem (not chitchat / exploration).
  - The assistant ran investigation tools (kubectl, AI analysis, etc).
  - The assistant arrived at a SPECIFIC answer (not "I don't know").

If worthy, also extract:
  - problem: ONE sentence — what was wrong.
  - resolution: ONE sentence — how the user can fix it.
  - error_signature: 3-6 word keyword phrase that would match similar future issues
                     (e.g. "CrashLoopBackOff after deploy", "ImagePullBackOff missing secret").

Output ONLY a valid JSON object, no prose, no code fences:
{{"worthy": true|false, "problem": "...", "resolution": "...", "error_signature": "..."}}

Transcript:
---
{transcript}
---
"""


def maybe_capture(
    question: str,
    answer: str,
    tool_used: str,
    react_steps: list[dict],
    *,
    session_id: Optional[str] = None,
    user: Optional[str] = None,
    cluster: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Optional[str]:
    """Try to capture this chat into ``session_memory``.

    Returns the new point's UUID on success (so the chat response can ride
    it back to the UI for thumbs-up wiring), or ``None`` when the session
    was judged not worthy / disabled / errored.

    Never raises — capture is observability, not control flow.
    """
    settings = get_settings()
    if not settings.session_capture_enabled:
        _log_capture_decision("skipped", "session_capture_disabled", session_id=session_id, tool_used=tool_used)
        return None
    if not (question or "").strip() or not (answer or "").strip():
        _log_capture_decision("skipped", "empty_question_or_answer", session_id=session_id, tool_used=tool_used)
        return None

    # Quick rejection: failed sessions almost never produce reusable
    # runbooks. Saves a classifier call.
    #
    # NOTE: ``tool_used == "none"`` is deliberately NOT a rejection.
    # Originally this filter excluded any chat where no kubectl tool ran,
    # on the assumption that "no tool = no actionable runbook." But after
    # Phase 1.5 (deployment-repo as KB), many of the most useful chats —
    # Ansible playbook errors grounded from the deployment_repo collection
    # — produce great answers without ever invoking a tool. Filtering
    # them out blocked the Phase 2.3 prompt cache from ever warming up
    # for that whole workflow. The classifier prompt downstream still
    # requires "a SPECIFIC answer (not 'I don't know')", so chitchat
    # won't survive the LLM check either way — just at a small per-chat
    # Gemini Flash cost (~$0.0001).
    if tool_used == "error":
        _log_capture_decision("skipped", "tool_used_error", session_id=session_id, tool_used=tool_used)
        return None
    if "Failed to reach the backend" in answer or len(answer) < 40:
        _log_capture_decision("skipped", "answer_too_short_or_backend_unreachable", session_id=session_id, tool_used=tool_used)
        return None

    transcript = _build_transcript(
        question, answer, react_steps,
        max_chars=settings.session_capture_transcript_chars,
        redact_secrets=settings.session_capture_redact_secrets,
    )

    classification = _classify(transcript)
    if not classification or not classification.get("worthy"):
        _log_capture_decision(
            "skipped",
            "classifier_not_worthy",
            session_id=session_id,
            tool_used=tool_used,
            extra={"classifier_result": _classification_summary(classification)},
        )
        return None

    capture_id = _persist(
        classification=classification,
        question=question,
        answer=answer,
        tool_used=tool_used,
        session_id=session_id,
        user=user,
        cluster=cluster,
        namespace=namespace,
        ttl_days=settings.session_capture_ttl_days,
        redact_secrets=settings.session_capture_redact_secrets,
    )
    if not capture_id:
        _log_capture_decision("skipped", "persist_failed", session_id=session_id, tool_used=tool_used)
    return capture_id


# ── Internals ────────────────────────────────────────────────────────────────

def _classification_summary(classification: Optional[dict]) -> dict:
    if not isinstance(classification, dict):
        return {"present": False}
    return {
        "present": True,
        "worthy": bool(classification.get("worthy")),
        "has_problem": bool((classification.get("problem") or "").strip()),
        "has_resolution": bool((classification.get("resolution") or "").strip()),
        "error_signature": (classification.get("error_signature") or "")[:80],
    }


def _log_capture_decision(
    outcome: str,
    reason: str,
    *,
    session_id: Optional[str],
    tool_used: str,
    extra: Optional[dict] = None,
) -> None:
    payload = {
        "event": "session_capture",
        "outcome": outcome,
        "reason": reason,
        "session": session_id or "-",
        "tool_used": tool_used or "",
    }
    if extra:
        payload.update(extra)
    level = logging.DEBUG if reason == "session_capture_disabled" else logging.INFO
    logger.log(level, "session_capture %s", json.dumps(payload, default=str))

def _build_transcript(
    question: str,
    answer: str,
    react_steps: list[dict],
    *,
    max_chars: int,
    redact_secrets: bool,
) -> str:
    """Compact transcript fed to the classifier. Stripped + truncated."""
    parts: list[str] = [f"USER: {question.strip()}"]

    for step in (react_steps or []):
        action = step.get("action", "")
        if action == "answer":
            continue
        params = step.get("params") or {}
        # Render kubectl-ish params compactly; full args bloat the prompt.
        keys = ", ".join(f"{k}={v}" for k, v in list(params.items())[:4])
        parts.append(f"TOOL: {action}({keys})")

    parts.append(f"ASSISTANT: {answer.strip()}")

    blob = "\n".join(parts)
    if redact_secrets:
        blob = redact(blob)
    if len(blob) > max_chars:
        # Keep both ends — first 60% from the top (question + tools), then
        # the last 40% of the answer where the resolution usually sits.
        head = blob[: int(max_chars * 0.6)]
        tail = blob[-int(max_chars * 0.4):]
        blob = f"{head}\n... [transcript truncated] ...\n{tail}"
    return blob


def _classify(transcript: str) -> Optional[dict]:
    """Run the cheap classifier. Returns a parsed JSON dict or None."""
    try:
        from services.llm import get_provider
    except Exception as exc:
        logger.debug("capture: LLM import failed (%s)", exc)
        return None

    try:
        provider = get_provider()
    except Exception as exc:
        logger.debug("capture: provider init failed (%s)", exc)
        return None

    if not getattr(provider, "enabled", False):
        return None

    prompt = _CLASSIFIER_PROMPT.format(transcript=transcript)
    try:
        raw = provider.generate(
            prompt=prompt,
            system=_CLASSIFIER_SYSTEM,
            temperature=0.0,
            max_tokens=200,
        )
    except Exception as exc:
        logger.warning("capture: classifier call failed: %s", exc)
        return None

    return _parse_classifier_json(raw)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_classifier_json(raw: str) -> Optional[dict]:
    """Tolerate code-fence wrappers / leading-text from the LLM."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = _JSON_BLOCK_RE.search(raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _persist(
    *,
    classification: dict,
    question: str,
    answer: str,
    tool_used: str,
    session_id: Optional[str],
    user: Optional[str],
    cluster: Optional[str],
    namespace: Optional[str],
    ttl_days: int,
    redact_secrets: bool,
) -> Optional[str]:
    """Embed + upsert the capture into session_memory. Returns point_id."""
    try:
        vector_db.connect()
        vector_db.ensure_collection_for(SESSION_MEMORY)
    except Exception as exc:
        logger.warning("capture: vector DB unavailable (%s); skipping", exc)
        return None

    problem = (classification.get("problem") or "").strip()
    resolution = (classification.get("resolution") or "").strip()
    error_sig = (classification.get("error_signature") or "").strip()

    payload: dict[str, Any] = {
        "title": error_sig or problem[:80] or "captured session",
        # No persistent URL for captured sessions — give a synthetic one
        # so the citation UI has a stable handle.
        "url": f"session://{session_id or 'anon'}/{int(time.time())}",
        "section": tool_used or "",
        "question": question,
        "context": "",
        "resolution": resolution,
        "commands_run": tool_used,
        "expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=ttl_days)),
        "timestamp": _iso(datetime.now(timezone.utc)),
        "user": user or "anonymous",
        "cluster": cluster or "*",
        "namespace": namespace or "*",
        "kind": "*",
        "error_signature": error_sig,
        "verified": False,
        "upvotes": 0,
        "source": "session_capture",
    }
    if redact_secrets:
        payload = redact_dict(payload, fields=("question", "resolution", "context", "commands_run"))

    # Embed the strongest signal we have for future similarity search:
    # the (redacted) problem + error_signature.
    embed_text = error_sig or problem or payload["question"]
    try:
        vec = embeddings.embed(embed_text)
    except Exception as exc:
        logger.warning("capture: embed failed (%s); skipping", exc)
        return None

    point_id = str(uuid.uuid4())
    try:
        vector_db.upsert_point(
            collection=SESSION_MEMORY.name,
            point_id=point_id,
            payload=payload,
            vector=vec,
        )
    except Exception as exc:
        logger.warning("capture: upsert failed (%s)", exc)
        return None

    logger.info(
        "session_capture %s",
        json.dumps({
            "event": "session_capture",
            "outcome": "created",
            "capture_id": point_id,
            "session": session_id or "-",
            "error_signature": error_sig,
            "ttl_days": ttl_days,
            "tool_used": tool_used or "",
        }, default=str),
    )
    return point_id


def _iso(dt: datetime) -> str:
    return dt.isoformat()
