"""Retrieval router — decides whether each chat turn should:

  - ``cached``   : a verified runbook matched closely → return its
                   resolution directly, skip the LLM entirely.
  - ``grounded`` : at least one high-trust chunk matched well → call the
                   LLM with those chunks injected as context (RAG).
  - ``cold``     : nothing matched well enough → fall through to the
                   existing LLM-only / ReAct path.

This sits *before* the LLM call. Cached is the cheapest path and gives
the most consistent answers; cold is the slowest and most expensive.
Threshold tuning needs real production data; the defaults are starting
guesses backed by the AGENT_FEATURE_ROADMAP design.

The router is intentionally NOT idempotent across questions — each call
fires a fresh embedding + similarity search. That's fine: at typical
chat throughput this is microseconds compared to the LLM call we may
end up making anyway.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)


# Patterns that indicate a user is pasting an Ansible-flavored error or
# asking a static deployment-repo lookup question.
# When any of these match the question, the router force-includes the
# ``deployment_repo`` collection in the search and stamps
# ``ansible_detected=true`` in the audit log. Threshold is NOT changed
# in v1 (see plan §11.5) — we tune from data after collecting some
# real-traffic audit signal.
_ANSIBLE_ERROR_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bTASK\s*\[", re.IGNORECASE),
    re.compile(r"^PLAY RECAP", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^fatal:\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bansible-playbook\b", re.IGNORECASE),
    # Static deployment-repo lookups are not errors, but they still need
    # deployment_repo retrieval even if the configured collection list is stale.
    re.compile(r"\bansible\b", re.IGNORECASE),
    re.compile(r"\bplaybooks?\b", re.IGNORECASE),
    re.compile(r"\bdeployment-provisioning\b", re.IGNORECASE),
    re.compile(r"\b(?:group_vars|host_vars|inventory|handlers?|templates?|vars|defaults|meta)\b", re.IGNORECASE),
    re.compile(r"\b(?:which|what|where|show|find|list)\b.*\broles?\b", re.IGNORECASE),
    re.compile(r"\broles?\b.*\b(?:deploy|deploys|deployed|deployment|configure|configures|configured|install|installs|installed)\b", re.IGNORECASE),
    re.compile(r"^(?:ok|changed|failed|skipping):\s*\[", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bUNREACHABLE!", re.IGNORECASE),
    re.compile(r"Failed to import the required Python library", re.IGNORECASE),
    # FQCN module references — these appear verbatim in the deployment
    # repo's tasks, so embedding similarity will be strong when matched.
    re.compile(r"\b(?:ansible\.builtin|ansible\.windows|kubernetes\.core|community\.general|awx\.awx)\.", re.IGNORECASE),
)


def _looks_like_ansible_error(question: str) -> bool:
    """True if the question matches any Ansible/deployment-repo pattern.

    Cheap regex scan — runs on every chat turn but trivial relative to
    embed+search latency.
    """
    if not question:
        return False
    for pat in _ANSIBLE_ERROR_PATTERNS:
        if pat.search(question):
            return True
    return False


@dataclass(frozen=True)
class Citation:
    """One source citation, suitable for rendering in the UI."""
    title: str
    url: str
    section: str = ""
    similarity: float = 0.0
    collection: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "section": self.section,
            "similarity": self.similarity,
            "collection": self.collection,
        }


_CHUNK_CONTENT_MAX_CHARS = 1000


def _slim_chunk(hit: dict) -> dict:
    """Compact representation of a Qdrant search hit for persistence.

    ``vector_db.search_in`` returns FLAT dicts shaped like::

        {**payload, "id": "...", "similarity": 0.87}

    — payload fields at the top level (not nested), score named
    ``similarity`` (not ``score``), and no collection field. The collection
    is injected by ``route()`` as ``_collection`` because the per-collection
    parallel search in this module is the only place that knows which
    collection each hit came from.

    The content field differs per collection (see ``services/rag/schema.py``):

      * ``devops_doc`` / ``deployment_repo`` — ``content``
      * ``runbook`` — ``problem`` + ``resolution``
      * ``session_memory`` — ``question`` + ``resolution``
      * ``k8s_errors`` (legacy) — ``solution_text``

    The fallback chain below covers all of them.
    """
    content = (
        hit.get("content")
        or hit.get("resolution")
        or hit.get("problem")
        or hit.get("question")
        or hit.get("solution_text")
        or ""
    )
    if isinstance(content, str) and len(content) > _CHUNK_CONTENT_MAX_CHARS:
        content = content[:_CHUNK_CONTENT_MAX_CHARS] + "…"
    return {
        "id": str(hit.get("id") or ""),
        "collection": hit.get("_collection") or "",
        "score": float(hit.get("similarity") or 0.0),
        "content": content,
        "title": hit.get("title") or "",
    }


@dataclass
class RouteDecision:
    mode: str                                  # "cached" | "grounded" | "cold"
    citations: list[Citation] = field(default_factory=list)
    cached_answer: Optional[str] = None        # set only when mode == "cached"
    grounded_chunks: list[dict] = field(default_factory=list)  # raw hits, only when mode == "grounded"
    top_score: float = 0.0
    top_collection: str = ""
    reason: str = ""                           # human-readable explanation for logs / UI
    # Set by ``route()`` when the question matched an Ansible-error
    # pattern. Surfaced in audit logs so threshold tuning can be driven
    # by real data later (plan §11.5).
    ansible_detected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "citations": [c.to_dict() for c in self.citations],
            "cached_answer": self.cached_answer,
            "top_score": self.top_score,
            "top_collection": self.top_collection,
            "reason": self.reason,
            "ansible_detected": self.ansible_detected,
            # Stage 1 of the DeepEval rollout (see
            # docs/AGENT_HARNESS_IMPLEMENTATION_PLAN.md §"Post-Hoc Faithfulness
            # Pattern"): persist the actual chunks the LLM was grounded on so
            # the eval runner can compute Faithfulness without re-running
            # retrieval. Content is truncated per chunk to bound storage
            # (5 chunks × 1000 chars ≈ 5 KB per grounded chat message).
            # Stage 3 will replace this with the Phase 1 ``rag_sources_json``
            # field on ``agent_runs``; this stays only until that lands.
            "grounded_chunks": [_slim_chunk(c) for c in self.grounded_chunks],
        }


def route(
    question: str,
    entities: Optional[dict[str, str]] = None,
) -> RouteDecision:
    """Run the retrieval decision for a single chat turn.

    ``entities`` is an optional dict of equality filters (cluster, namespace,
    kind) — typically pulled from the per-user memory preamble. Empty ``{}``
    or None means "no filtering, search globally".

    Returns a ``RouteDecision``. Falls through to ``cold`` on any error so
    a broken vector DB never blocks the chat from getting an answer.
    """
    settings = get_settings()

    if not settings.rag_router_enabled or not (question or "").strip():
        return _cold("router disabled or empty question")

    collections = _collection_list(settings.rag_router_collections)
    # When the question looks like an Ansible error, force-include
    # ``deployment_repo`` in the searched collections so that source has
    # a chance to surface even if an operator forgot to add it to the
    # default collections list. The threshold stays unchanged in v1
    # (plan §11.5).
    ansible_detected = _looks_like_ansible_error(question)
    if ansible_detected and "deployment_repo" not in collections:
        collections = collections + ["deployment_repo"]

    if not collections:
        return _cold("no collections configured", ansible_detected=ansible_detected)

    try:
        from services.embeddings import embeddings
        from services.vector_db import vector_db
    except Exception as exc:
        logger.warning("router: imports failed (%s); falling back to cold", exc)
        return _cold(f"imports failed: {exc}")

    try:
        vector_db.connect()
    except Exception as exc:
        logger.warning("router: vector DB unavailable (%s); falling back to cold", exc)
        return _cold(f"vector DB unavailable: {exc}")

    try:
        qvec = embeddings.embed(question)
    except Exception as exc:
        logger.warning("router: embed failed (%s); falling back to cold", exc)
        return _cold(f"embed failed: {exc}")

    base_filters = _build_filters(entities)
    pooled: list[tuple[str, dict]] = []

    # Search each high-trust collection in parallel; pool results. Cap
    # each call by top_k so we don't pull a long tail. Parallelism shaves
    # ~2x wall time on a 3-collection setup vs. sequential (the qdrant
    # client releases the GIL during the HTTP call). Router latency is
    # dwarfed by the LLM call that follows, but it's free wins.
    def _do_search(coll: str) -> tuple[str, list[dict]]:
        try:
            return coll, vector_db.search_in(
                collection=coll,
                query_vector=qvec,
                filters=base_filters,
                limit=settings.rag_router_top_k,
                log_failures=False,
            )
        except Exception as exc:
            logger.warning("router: search in %s failed: %s", coll, exc)
            return coll, []

    if len(collections) == 1:
        # Skip the thread-pool overhead for the trivial case.
        results = [_do_search(collections[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(collections), 4)) as ex:
            results = list(ex.map(_do_search, collections))

    for coll, hits in results:
        for h in hits:
            pooled.append((coll, h))

    if not pooled:
        decision = _cold(
            "no hits across configured collections",
            ansible_detected=ansible_detected,
        )
        _audit("route", question=question, decision=decision)
        return decision

    # Sort by similarity desc, take top_k overall.
    pooled.sort(key=lambda ch: -float(ch[1].get("similarity", 0.0)))
    top = pooled[: settings.rag_router_top_k]
    top_coll, top_hit = top[0]
    top_score = float(top_hit.get("similarity", 0.0))

    # ── Cached: verified runbook with very high confidence ──────────────────
    if (
        top_score >= settings.rag_router_cached_threshold
        and top_coll == "runbook"
        and bool(top_hit.get("verified"))
    ):
        decision = RouteDecision(
            mode="cached",
            cached_answer=_format_runbook_answer(top_hit),
            citations=[_citation_from(top_coll, top_hit)],
            top_score=top_score,
            top_collection=top_coll,
            reason=f"verified runbook match (similarity={top_score:.3f})",
            ansible_detected=ansible_detected,
        )
        _audit("route", question=question, decision=decision)
        return decision

    # ── Grounded: pass top chunks to the LLM as context ─────────────────────
    if top_score >= settings.rag_router_grounded_threshold:
        # Inject ``_collection`` per chunk so downstream consumers
        # (eval runner via ``RouteDecision.to_dict``) can attribute each
        # chunk to its collection. ``build_grounded_preamble`` ignores
        # the field; existing payload schemas do not use ``_collection``.
        chunks = [{"_collection": coll, **hit} for coll, hit in top]
        decision = RouteDecision(
            mode="grounded",
            grounded_chunks=chunks,
            citations=[_citation_from(coll, hit) for coll, hit in top],
            top_score=top_score,
            top_collection=top_coll,
            reason=f"top hit in {top_coll} (similarity={top_score:.3f})",
            ansible_detected=ansible_detected,
        )
        _audit("route", question=question, decision=decision)
        return decision

    # ── Cold ────────────────────────────────────────────────────────────────
    decision = _cold(
        f"top similarity {top_score:.3f} below grounded threshold "
        f"{settings.rag_router_grounded_threshold}",
        ansible_detected=ansible_detected,
    )
    decision.top_score = top_score
    decision.top_collection = top_coll
    _audit("route", question=question, decision=decision)
    return decision


def build_grounded_preamble(decision: RouteDecision, max_chars: int = 4000) -> str:
    """Render the grounded chunks as a prompt-friendly preamble.

    Returns an empty string for non-grounded decisions. The preamble is
    intended to be prepended to ``history_context`` in ``react_loop`` so
    the LLM has factual material to cite from in its answer.
    """
    if decision.mode != "grounded" or not decision.grounded_chunks:
        return ""

    out: list[str] = [
        "[Knowledge-base context — use these chunks to ground your answer "
        "and cite the matching title/url when relevant]"
    ]
    budget = max_chars
    for i, chunk in enumerate(decision.grounded_chunks, start=1):
        title = chunk.get("title") or chunk.get("url") or f"chunk_{i}"
        section = chunk.get("section") or ""
        sim = chunk.get("similarity")
        content = (chunk.get("content") or chunk.get("solution_text") or "").strip()
        if not content:
            continue
        header = f"\n[{i}] {title}"
        if section and section != "(top)":
            header += f" > {section}"
        if sim is not None:
            header += f"  (similarity={sim})"
        body = content[: max(0, budget - len(header) - 2)]
        if not body:
            break
        out.append(header)
        out.append(body)
        budget -= len(header) + len(body) + 2
        if budget <= 0:
            break
    out.append("[end knowledge-base context]\n")
    return "\n".join(out)


# ── Internals ────────────────────────────────────────────────────────────────

def _cold(reason: str, *, ansible_detected: bool = False) -> RouteDecision:
    return RouteDecision(mode="cold", reason=reason, ansible_detected=ansible_detected)


def _collection_list(raw: str) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def _build_filters(entities: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
    if not entities:
        return None
    # Drop wildcard / empty entries — they'd needlessly tighten the search.
    cleaned = {
        k: v for k, v in entities.items()
        if v and str(v).strip() not in ("", "*", "all")
    }
    return cleaned or None


def _format_runbook_answer(hit: dict) -> str:
    """Render a cached runbook hit as a markdown answer."""
    title = hit.get("title") or "Runbook"
    problem = (hit.get("problem") or "").strip()
    resolution = (hit.get("resolution") or hit.get("content") or "").strip()
    commands = (hit.get("commands") or "").strip()
    url = hit.get("url") or ""

    parts = [f"**{title}**"]
    if problem:
        parts.append(f"\n_Problem_: {problem}")
    if resolution:
        parts.append(f"\n{resolution}")
    if commands:
        parts.append(f"\n```bash\n{commands}\n```")
    if url:
        parts.append(f"\n_Source_: {url}")
    return "\n".join(parts)


def _citation_from(collection: str, hit: dict) -> Citation:
    return Citation(
        title=str(hit.get("title") or hit.get("url") or "(untitled)"),
        url=str(hit.get("url") or ""),
        section=str(hit.get("section") or ""),
        similarity=float(hit.get("similarity") or 0.0),
        collection=collection,
    )


def _audit(event: str, *, question: str, decision: RouteDecision) -> None:
    """Append a one-line audit record. Best-effort; never raises."""
    settings = get_settings()
    if not getattr(settings, "enable_audit_log", False):
        return
    try:
        line = (
            f"{datetime.now().isoformat()} | RAG_{event.upper()} | "
            f"mode={decision.mode} | top_score={decision.top_score:.3f} | "
            f"top_collection={decision.top_collection or '-'} | "
            f"citations={len(decision.citations)} | "
            f"ansible_detected={str(decision.ansible_detected).lower()} | "
            f"reason={decision.reason} | "
            f"q={question[:120].replace(chr(10), ' ')!r}"
        )
        path = Path(settings.audit_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception as exc:
        logger.debug("router audit write failed (ignored): %s", exc)
