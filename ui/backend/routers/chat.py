"""Chat endpoint with Gemini-powered intent router.

POST /api/chat
  Input:  { message: str, history?: list, ssh?: SSHCredentials }
  Output: { reply: str, tool_used: str, result: dict | None,
            timestamp: float, suggested_actions: list }

The router asks Gemini to classify the user's intent and extract
parameters, then calls the appropriate tool function automatically.
No tool selection required from the user.

When the request includes ssh credentials, all kubectl calls for the
duration of that request are transparently routed via SSH to the remote
cluster master node — no other code changes required.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import auth as auth_utils
import cluster_session
import db
import memory


def _maybe_capture_chat(
    *,
    question: str,
    answer: str,
    tool_used: str,
    react_steps: list,
    session_id: Optional[str] = None,
    user: Optional[str] = None,
) -> Optional[str]:
    """Best-effort Phase 1.3 capture. Returns the new point_id or None.

    Pulls cluster/namespace context from the session's per-user memory
    so captured entries are scoped to where the user was working.
    """
    try:
        cluster = namespace = None
        if session_id:
            try:
                mem = memory.db.get_user_memory(session_id) or {}
                # First entry per category is the most-recent.
                clusters = mem.get("clusters") or []
                namespaces = mem.get("namespaces") or []
                if clusters:
                    cluster = clusters[0].get("value")
                if namespaces:
                    namespace = namespaces[0].get("value")
            except Exception:
                pass

        from services.rag.capture import maybe_capture
        return maybe_capture(
            question=question,
            answer=answer,
            tool_used=tool_used,
            react_steps=react_steps,
            session_id=session_id,
            user=user,
            cluster=cluster,
            namespace=namespace,
        )
    except Exception as exc:
        logger.warning("session capture raised (ignored): %s", exc)
        return None


def _make_memory_capturing_dispatch(session_id: Optional[str]):
    """Return a dispatch closure that wraps _dispatch and records
    successful tool calls into per-session memory (Phase 2.2).

    No-op for anonymous sessions (session_id None) — the wrapped function
    behaves identically to _dispatch.
    """
    if not session_id:
        return lambda tool, params: _dispatch(tool, params, surface="react")

    def _capturing(tool: str, params: dict) -> dict:
        result = _dispatch(tool, params, surface="react", session_id=session_id)
        # Only record when the tool succeeded enough to give us trustworthy
        # entities. A bare {"error": ...} response means the params may
        # have been wrong, so don't memorize them as "recent context".
        if isinstance(result, dict) and "error" in result and len(result) <= 2:
            return result
        if isinstance(result, dict) and result.get("success") is False:
            return result
        memory.record_tool_call(session_id, tool, params)
        return result

    return _capturing

logger = logging.getLogger(__name__)
router = APIRouter()


_cached_llm_providers = {}
HARDCODED_GEMINI_MODEL = "gemini-3.1-flash-lite"

_SENSITIVE_PARAM_RE = re.compile(r"(password|token|secret|key|credential|auth)", re.IGNORECASE)
_FALLBACK_SECRET_TEXT_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9_\-.=]+|"
    r"\b(token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+"
)


def _redact_log_text(message: str) -> str:
    text = str(message or "")
    try:
        from services.rag.redaction import redact
        return redact(text)
    except Exception:
        return _FALLBACK_SECRET_TEXT_RE.sub(lambda m: f"{m.group(1)}<REDACTED>" if m.group(1) else "<REDACTED>", text)


def _prompt_preview(message: str, limit: int = 160) -> str:
    text = " ".join(_redact_log_text(message).split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _compact_params(params: Optional[dict], *, limit: int = 8) -> dict:
    if not isinstance(params, dict):
        return {"_type": type(params).__name__}
    compact: dict = {}
    for key, value in list(params.items())[:limit]:
        if _SENSITIVE_PARAM_RE.search(str(key)):
            compact[key] = "<redacted>"
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value) if value is not None else None
            compact[key] = text[:120] + "..." if isinstance(text, str) and len(text) > 120 else value
        elif isinstance(value, list):
            compact[key] = f"list[{len(value)}]"
        elif isinstance(value, dict):
            compact[key] = f"dict[{len(value)}]"
        else:
            compact[key] = type(value).__name__
    return compact


def _compact_react_steps(steps: list[dict]) -> list[dict]:
    compact = []
    for step in steps or []:
        action = step.get("action")
        if action == "answer":
            continue
        compact.append({
            "tool": action,
            "params": _compact_params(step.get("params") or {}),
            "duration_ms": step.get("duration_ms"),
        })
    return compact


def _log_chat_turn(
    *,
    session_tag: str,
    route: str,
    message: str,
    tool_used: str,
    tool_params: Optional[dict] = None,
    capture_id: Optional[str] = None,
    rag_decision: Optional[dict] = None,
    error: Optional[str] = None,
    answer: str = "",
) -> None:
    payload = {
        "event": "chat_turn",
        "session": session_tag,
        "route": route,
        "prompt": _prompt_preview(message),
        "tool_used": tool_used,
        "tool_params": _compact_params(tool_params or {}),
        "capture_id": capture_id,
        "error": error,
        "answer_preview": _prompt_preview(answer, limit=220),
    }
    if rag_decision:
        payload["rag"] = {
            "mode": rag_decision.get("mode"),
            "top_score": rag_decision.get("top_score"),
            "top_collection": rag_decision.get("top_collection"),
            "ansible_detected": rag_decision.get("ansible_detected"),
        }
    logger.info("chat_turn %s", json.dumps(payload, default=str))


def _log_react_trace(
    *,
    session_tag: str,
    message: str,
    steps_meta: list[dict],
    final_tool_used: str,
    rag_decision: Optional[dict] = None,
) -> None:
    payload = {
        "event": "react_trace",
        "session": session_tag,
        "prompt": _prompt_preview(message),
        "steps": _compact_react_steps(steps_meta),
        "final_tool_used": final_tool_used,
    }
    if rag_decision:
        payload["rag_mode"] = rag_decision.get("mode")
    logger.info("react_trace %s", json.dumps(payload, default=str))


def _llm_provider(model: Optional[str] = None):
    """Lazily resolve and cache the configured LLM provider.

    Imported from `services.llm` in mcp (added to sys.path in main.py).
    Returns None on any import / config failure so callers can fall back cleanly.
    """
    try:
        from config.settings import get_settings
        settings = get_settings()
        provider_name = (settings.llm_provider or "gemini").lower()
        selected_model = HARDCODED_GEMINI_MODEL if provider_name == "gemini" else ""
        key = (provider_name, selected_model)
        if key in _cached_llm_providers:
            return _cached_llm_providers[key]
        from services.llm import get_provider
        provider = get_provider(model=selected_model or None)
        _cached_llm_providers[key] = provider
        return provider
    except Exception as e:
        logger.warning(f"LLM provider unavailable: {e}")
        return None


_LLM_UNAVAILABLE_NOTICE = (
    "⚠️ **No LLM is configured, so this answer is raw tool output.** "
    "There is no reasoning trace and no analysis — KubeAstra ran a single "
    "matching command and printed the result. Add an API key in Settings to "
    "restore multi-step investigation."
)


def _note_llm_unavailable(resp, session_tag: str):
    """Say out loud that the answer came back without an LLM.

    This degradation used to be entirely silent: tools still ran and rendered,
    so the reply looked plausible while the reasoning trace and synthesis had
    simply vanished. That is indistinguishable from a broken UI, and it cost
    days of debugging in the wrong place. Diagnose it in the answer itself.
    """
    logger.warning(
        "session %s: no LLM provider enabled — answering from a single tool "
        "call with no reasoning. Check that an API key is configured.",
        session_tag,
    )
    try:
        resp.reply = f"{_LLM_UNAVAILABLE_NOTICE}\n\n{resp.reply or ''}".rstrip()
    except Exception:  # never let the notice break the answer
        logger.debug("could not attach llm-unavailable notice", exc_info=True)
    return resp


# Words that are never a namespace. The old extraction was
# `namespace[:\s]+(\S+)|in\s+([a-z0-9-]+)`, copy-pasted to seven call sites,
# and it failed three different ways on ordinary phrasings:
#
#   "list all pods in the production namespace"  -> "the"
#   "get pods in namespace demo"                 -> "namespace"
#   "in the production namespace" (strict form)  -> None, silent fallback
#
# The second one is the subtle one: alternation picks the leftmost match, so
# `in\s+(...)` fires on "in namespace" before `namespace[:\s]+(...)` ever gets
# a chance. Every one of these then queried the wrong namespace and reported
# results as though they were right.
_NAMESPACE_STOPWORDS = frozenset({
    "the", "a", "an", "my", "our", "your", "this", "that", "these", "those",
    "all", "any", "some", "each", "every", "namespace", "namespaces", "ns",
    "cluster", "there", "here", "which", "what",
})

# "show pods in imagepullbackoff" is a status filter, not a namespace, and
# every pattern here reads it as one. The old code guarded this at exactly one
# of the eleven call sites; the rest happily queried a namespace named after a
# pod condition and reported "no pods found" as though that were an answer.
_POD_CONDITION_WORDS = frozenset({
    "crashloop", "crashloopbackoff", "crashlopp", "imagepull",
    "imagepullbackoff", "pending", "oom", "oomkilled", "evicted",
})


def _extract_namespace(message: str, default: Optional[str] = None) -> Optional[str]:
    """Pull a namespace out of free text, or return `default`.

    Tried most-explicit first, because a user who wrote `-n foo` means foo
    even if the sentence also contains the word "in".
    """
    text = (message or "").lower()

    patterns = (
        # -n foo   --namespace=foo   --namespace foo
        r"(?:^|\s)-n[=\s]+([a-z0-9][a-z0-9.-]*)",
        r"--namespace[=\s]+([a-z0-9][a-z0-9.-]*)",
        # namespace foo   namespace: foo   namespace "foo"
        r"namespace[:=\s]+[\"'`]?([a-z0-9][a-z0-9.-]*)[\"'`]?",
        # in the foo namespace   in foo namespace   foo namespace
        r"in\s+(?:the\s+)?([a-z0-9][a-z0-9.-]*)\s+namespace",
        r"([a-z0-9][a-z0-9.-]*)\s+namespace\b",
        # bare "in foo" — last resort, most likely to catch English
        r"\bin\s+(?:the\s+)?([a-z0-9][a-z0-9.-]*)",
    )

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = (match.group(1) or "").strip(".,;:!?\"'`")
            if (
                candidate
                and candidate not in _NAMESPACE_STOPWORDS
                and candidate not in _POD_CONDITION_WORDS
            ):
                return candidate

    return default


def _extract_namespace_or_all(message: str) -> str:
    """As above, but `*` when the user clearly means every namespace."""
    if re.search(r"\ball[\s-]?namespaces?\b|across (?:the )?cluster|every namespace", (message or "").lower()):
        return "*"
    namespace = _extract_namespace(message)
    if namespace in {"all", "all-namespaces", "allnamespaces"}:
        return "*"
    return namespace or "*"


def _short_session_id(session_id: Optional[str]) -> str:
    if not session_id:
        return "-"
    return session_id[:8]


_K8S_PROMPT_KEYWORDS = {
    "k8s", "kubernetes", "kubectl", "cluster", "namespace", "namespaces",
    "pod", "pods", "deployment", "deployments", "statefulset", "daemonset",
    "service", "services", "endpoint", "endpoints", "ingress", "node", "nodes",
    "cpu", "memory", "allocated", "allocatable", "capacity", "requests", "limits",
    "crashloopbackoff", "imagepullbackoff", "pending", "evicted", "restarts",
    "events", "rollout", "replicas", "argocd", "helm",
}


_K8S_LIVE_STATE_TERMS = {
    "get", "show", "list", "check", "status", "describe", "inspect", "investigate",
    "find", "current", "now", "running", "ready", "allocated", "allocatable",
    "capacity", "usage", "utilization", "cpu", "memory", "requests", "limits",
    "events", "warnings", "errors", "restarts", "rollout", "replicas",
}


_K8S_RESOURCE_TERMS = {
    "cluster", "namespace", "namespaces", "node", "nodes", "pod", "pods",
    "deployment", "deployments", "statefulset", "daemonset", "service",
    "services", "endpoint", "endpoints", "ingress", "argocd", "helm",
}


_IGNORE_EXTENSIONS = {
    "py", "md", "txt", "js", "json", "yaml", "yml", "conf", "urls", "ini", "cfg",
    "sh", "go", "java", "cpp", "c", "h", "ts", "html", "css", "xml", "toml", "lock",
    "png", "jpg", "jpeg", "gif", "svg", "tar", "gz", "zip", "class", "exe", "dll",
    "so", "append", "split", "join", "read", "write", "print"
}


def _looks_like_kubernetes_prompt(message: str) -> bool:
    """Deterministic guard so cluster questions never skip tool execution."""
    msg = (message or "").lower()
    tokens = set(re.findall(r"[a-z0-9_.-]+", msg))
    if tokens & _K8S_PROMPT_KEYWORDS:
        return True

    # Check for dotted hostnames/FQDNs conservatively
    for token in tokens:
        if "." in token:
            if re.fullmatch(r"v?\d+(\.\d+){1,3}([-.]?(alpha|beta|rc)\d*)?", token):
                continue
            parts = [p for p in token.split(".") if p]
            if len(parts) >= 2:
                # Extension/action denylist
                if parts[-1] in _IGNORE_EXTENSIONS:
                    continue
                # Host-like signals: contains a digit or a hyphen
                has_host_signals = any(c.isdigit() or c == "-" for c in token)
                if len(parts) >= 3:
                    # Require at least one alphabetic character and a host-like signal to look like hostname/node
                    if has_host_signals and any(any(c.isalpha() for c in part) for part in parts):
                        return True
                elif len(parts) == 2:
                    # If 2 segments, trigger only for standard internal domains
                    if parts[1] in ("corp", "local", "internal", "lan"):
                        return True
                    # Or known TLDs with host-like signals
                    if has_host_signals and parts[1] in ("com", "net", "org", "io"):
                        return True

    if re.search(r"\b[a-z0-9]+-[a-z0-9-]*k8s[a-z0-9-]*\b", msg):
        return True
    return False


def _looks_like_cluster_context_prompt(message: str) -> bool:
    """True for prompts about configured/selected kubeconfig contexts."""
    msg = (message or "").lower()
    tokens = set(re.findall(r"[a-z0-9_.-]+", msg))
    subject = {"cluster", "clusters", "context", "contexts", "kubeconfig", "kubeconfigs"}
    intent = {
        "configured", "available", "connected", "selected", "current",
        "active", "using", "have", "list", "show", "what", "which",
    }
    return bool(tokens & subject and tokens & intent)


def _looks_like_live_kubernetes_prompt(message: str) -> bool:
    """True when a Kubernetes prompt asks for current cluster state."""
    msg = (message or "").lower()
    tokens = set(re.findall(r"[a-z0-9_.-]+", msg))
    if _looks_like_cluster_context_prompt(message):
        return True
    if not _looks_like_kubernetes_prompt(message):
        return False
    if re.search(r"\b[a-z0-9]+-[a-z0-9-]*k8s[a-z0-9-]*\b", msg):
        return True
    if any("." in token and ("k8s" in token or "node" in token or "-" in token) for token in tokens):
        return True
    return bool((tokens & _K8S_RESOURCE_TERMS) and (tokens & _K8S_LIVE_STATE_TERMS))


def _should_skip_rag_for_prompt(message: str) -> bool:
    """Prompts about local/session configuration should not be KB-grounded."""
    return _looks_like_cluster_context_prompt(message)


def _looks_like_static_kb_lookup(message: str) -> bool:
    """True when the user is asking for repository/runbook knowledge, not live cluster state."""
    msg = (message or "").lower()
    if not msg.strip() or _looks_like_live_kubernetes_prompt(message):
        return False

    tokens = set(re.findall(r"[a-z0-9_.-]+", msg))
    kb_subjects = {
        "ansible", "playbook", "playbooks", "role", "roles", "runbook",
        "repo", "repository", "deployment-provisioning", "helm", "chart",
        "pipeline", "jenkinsfile", "inventory", "group_vars", "host_vars",
        "handlers", "handler", "templates", "template", "vars", "defaults",
        "meta",
    }
    lookup_intents = {
        "what", "which", "where", "show", "list", "find", "used", "use",
        "uses", "deploy", "deploys", "deployed", "deployment", "name",
        "file", "files",
    }
    return bool(tokens & kb_subjects and tokens & lookup_intents)


def _should_protect_from_fast_path(message: str) -> bool:
    """Prompts that must run deterministic routing before generic LLM fast path."""
    return _looks_like_kubernetes_prompt(message) or _looks_like_static_kb_lookup(message)


def _should_run_proactive_triage(message: str, history: list, enabled: bool) -> bool:
    """Run startup triage only when it won't obscure a direct config answer."""
    return bool(enabled and not (history or []) and not _looks_like_cluster_context_prompt(message))


def _cached_decision_as_grounding(decision) -> str:
    """Use a cached runbook as context without skipping live investigation."""
    if decision is None or decision.mode != "cached" or not decision.cached_answer:
        return ""

    citation_lines = []
    for citation in decision.citations:
        title = getattr(citation, "title", "") or "cached runbook"
        url = getattr(citation, "url", "") or ""
        section = getattr(citation, "section", "") or ""
        citation_lines.append(f"- {title}{f' ({section})' if section else ''}{f': {url}' if url else ''}")

    citations = "\n".join(citation_lines) if citation_lines else "- cached runbook"
    return (
        "[Knowledge-base context — a verified cached runbook matched this prompt. "
        "Use it as guidance, but verify current Kubernetes state with live tools before answering.]\n"
        f"Similarity: {decision.top_score:.3f}\n"
        f"Sources:\n{citations}\n\n"
        f"{decision.cached_answer[:4000]}"
    )


def _should_answer_grounded_kb_directly(message: str, decision) -> bool:
    """Bypass ReAct for static KB lookups with strong grounded retrieval."""
    return bool(
        decision is not None
        and getattr(decision, "mode", None) == "grounded"
        and getattr(decision, "grounded_chunks", None)
        and not _looks_like_live_kubernetes_prompt(message)
        and _looks_like_static_kb_lookup(message)
    )


def _format_grounded_kb_answer(message: str, decision) -> str:
    """Render grounded retrieval chunks as a deterministic answer for KB lookups."""
    chunks = list(getattr(decision, "grounded_chunks", None) or [])
    top_score = float(getattr(decision, "top_score", 0.0) or 0.0)
    seen: set[tuple[str, str, str]] = set()
    evidence: list[dict] = []

    for chunk in chunks:
        title = str(chunk.get("title") or chunk.get("url") or "(untitled)")
        section = str(chunk.get("section") or "")
        content = str(chunk.get("content") or chunk.get("solution_text") or "").strip()
        url = str(chunk.get("url") or "")
        score = float(chunk.get("similarity") or 0.0)
        if not content:
            continue
        key = (title, section, content)
        if key in seen:
            continue
        seen.add(key)
        evidence.append({
            "title": title,
            "section": section,
            "content": content,
            "url": url,
            "score": score,
        })
        if len(evidence) >= 5:
            break

    if not evidence:
        return (
            "# Diagnosis\n"
            "The knowledge base did not return a usable source snippet for this question.\n\n"
            "# Evidence\n"
            "- No grounded chunk content was available from the retrieval result.\n\n"
            "# Recommended Actions\n"
            "1. Re-run the query with a more specific repository term or resource name.\n\n"
            "# Uncertainty\n"
            "Confidence: low\n"
            "Retrieval matched metadata, but no source content was available to quote."
        )

    primary = evidence[0]
    imported_playbooks: list[str] = []
    for item in evidence:
        imported_playbooks.extend(
            match.strip().strip("'\"")
            for match in re.findall(r"(?im)^\s*import_playbook:\s*([^\s#]+)", item["content"])
        )

    playbook_names = []
    for item in evidence:
        title = item["title"]
        if title not in playbook_names:
            playbook_names.append(title)

    if imported_playbooks:
        imported = ", ".join(f"`{p}`" for p in dict.fromkeys(imported_playbooks))
        diagnosis = (
            f"The knowledge base points to `{primary['title']}` as the matching playbook source. "
            f"The retrieved playbook imports {imported} for the deployment path shown in the repo."
        )
    else:
        names = ", ".join(f"`{p}`" for p in playbook_names[:3])
        diagnosis = (
            f"The knowledge base points to {names} as the most relevant repository source"
            f"{'s' if len(playbook_names[:3]) != 1 else ''} for this question."
        )

    evidence_lines = []
    for item in evidence:
        label = f"`{item['title']}`"
        if item["section"]:
            label += f" > {item['section']}"
        content = item["content"].replace("\n", " ").strip()
        if len(content) > 260:
            content = content[:257] + "..."
        score = f" similarity={item['score']:.3f}" if item["score"] else ""
        evidence_lines.append(f"- {label}: `{content}`{score}")

    source_lines = []
    for item in evidence[:3]:
        if item["url"]:
            source_lines.append(f"   - {item['url']}")

    confidence = "high" if top_score >= 0.75 else "medium"
    uncertainty_reason = (
        f"Top retrieval similarity was {top_score:.3f} from "
        f"`{getattr(decision, 'top_collection', '') or 'knowledge base'}`. "
        "The answer is limited to the retrieved repository chunks and should be verified against the source file before making changes."
    )

    action_texts = [
        f"Review `{primary['title']}` in the deployment-provisioning repository.",
    ]
    if imported_playbooks:
        action_texts.append(
            "Follow the imported playbook path"
            f"{'s' if len(imported_playbooks) != 1 else ''}: "
            + ", ".join(f"`{p}`" for p in dict.fromkeys(imported_playbooks))
            + "."
        )
    if source_lines:
        action_texts.append("Open the cited source URL(s):\n" + "\n".join(source_lines))
    actions = [f"{idx}. {text}" for idx, text in enumerate(action_texts, start=1)]

    return "\n\n".join([
        "# Diagnosis\n" + diagnosis,
        "# Evidence\n" + "\n".join(evidence_lines),
        "# Recommended Actions\n" + "\n".join(actions),
        "# Uncertainty\n" + f"Confidence: {confidence}\n{uncertainty_reason}",
    ])


# ── Request / response models ─────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str        # "user" | "assistant"
    content: str


class SSHCredentials(BaseModel):
    host: str
    username: str
    password: str
    port: int = 22


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = Field(default_factory=list)
    ssh: Optional[SSHCredentials] = None
    session_id: Optional[str] = None   # from browser localStorage
    model: Optional[str] = None        # request-level LLM model override


class ChatResponse(BaseModel):
    reply: str
    tool_used: str
    result: Optional[dict] = None
    error: Optional[str] = None
    timestamp: float = 0.0
    suggested_actions: list = Field(default_factory=list)
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    synthesis_breakdown: Optional[dict] = None
    eval_retrieval_context: list[str] = Field(default_factory=list)
    cost_summary: Optional[dict] = None
    trace_id: Optional[str] = None


class ExecuteRequest(BaseModel):
    command: str
    ssh: Optional[SSHCredentials] = None
    session_id: Optional[str] = None
    stdin: Optional[str] = None


class ExecuteResponse(BaseModel):
    success: bool
    output: str = ""
    error: str = ""


# ── Legacy routing code removed (Phase 2) ────────────────────────────────────
# ROUTER_SYSTEM, _gemini_route, and _normalize_route were dead code.
# The chat endpoint uses _keyword_route (no-LLM) or ReAct (LLM enabled).
# See docs/ROUTING_ARCHITECTURE_PROPOSAL_FIXES.md Phase 2.


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def _dispatch(
    tool: str,
    params: dict,
    *,
    surface: str = "chat",
    session_id: Optional[str] = None,
) -> dict:
    """Dispatch through the unified tool registry.

    SSH and session-selected kubeconfig runners are installed around the
    request before this function is called, so registry handlers still target
    the correct cluster.
    """
    try:
        from tool_registry import DispatchContext, dispatch as registry_dispatch
        result = registry_dispatch(
            tool,
            params or {},
            DispatchContext(surface=surface, session_id=session_id),
        )
        if tool in {"list_contexts", "list_kubeconfig_contexts"}:
            return _augment_context_listing(result, session_id=session_id)
        return result
    except Exception as e:
        err_msg = str(e)
        logger.exception("registry dispatch failed for %s", tool)
        return {"error": err_msg, "tool": tool}


def _augment_context_listing(result: dict, *, session_id: Optional[str]) -> dict:
    """Add session-selected or in-cluster context metadata to context listings.

    ``kubectl config get-contexts`` can be empty in Kubernetes because the
    backend often runs with only an in-cluster ServiceAccount. The chat UI,
    however, also supports session-scoped selected clusters stored in SQLite.
    """
    if not isinstance(result, dict):
        return result

    enriched = dict(result)
    contexts = []
    for ctx in enriched.get("contexts") or []:
        contexts.append(ctx if isinstance(ctx, dict) else {"name": str(ctx)})

    session_conn = None
    if session_id:
        try:
            session_conn = db.get_cluster_connection(session_id)
        except Exception as exc:
            logger.debug("context listing session lookup failed: %s", exc)

    if session_conn and session_conn.get("context_name"):
        selected = {
            "name": session_conn["context_name"],
            "cluster": session_conn.get("cluster_name") or session_conn["context_name"],
            "server": session_conn.get("server_url") or "",
            "namespace": session_conn.get("namespace") or "default",
            "mode": session_conn.get("mode") or "",
            "source": "session_connection",
        }
        if not any(c.get("name") == selected["name"] for c in contexts):
            contexts.insert(0, selected)
        enriched["session_connection"] = selected
        enriched["current_context"] = enriched.get("current_context") or selected["name"]
    elif not contexts and os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"):
        in_cluster = {
            "name": "in-cluster",
            "cluster": "Kubernetes ServiceAccount",
            "server": "",
            "namespace": os.environ.get("POD_NAMESPACE", "default"),
            "mode": "in-cluster",
            "source": "serviceaccount",
        }
        contexts.append(in_cluster)
        enriched["in_cluster"] = True
        enriched["current_context"] = "in-cluster"
        enriched["message"] = (
            "No kubeconfig contexts are mounted; the backend is running in-cluster "
            "with its Kubernetes ServiceAccount."
        )

    enriched["contexts"] = contexts
    enriched["total_contexts"] = len(contexts)
    return enriched


def _resolve_pod_ns(params: dict, pod_name: str) -> str:
    """Return the best namespace for a pod-specific tool call (investigate_pod).

    If the router explicitly provided a non-default namespace, use it directly.
    Otherwise run find_workload to auto-discover which namespace the pod lives in.
    Falls back to "default" if discovery fails or returns no results.
    """
    explicit_ns = params.get("namespace")
    if explicit_ns and explicit_ns not in ("default",):
        return explicit_ns
    if not pod_name:
        return explicit_ns or "default"
    try:
        from k8s.wrappers import find_workload
        fw = find_workload(pod_name)
        # find_workload returns {"pods": [...], "deployments": [...], "services": [...]}
        for pod in fw.get("pods", []):
            if pod.get("namespace"):
                return pod["namespace"]
        for dep in fw.get("deployments", []):
            if dep.get("namespace"):
                return dep["namespace"]
    except Exception:
        pass
    return explicit_ns or "default"


def _candidate_workload_names(raw_name: str) -> list[str]:
    """Generate likely Kubernetes resource names from a natural-language phrase."""
    if not raw_name:
        return []

    cleaned = re.sub(r"[^a-z0-9\s._-]", " ", raw_name.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return []

    tokens = [t for t in cleaned.split(" ") if t]
    generic_suffixes = {"pod", "deployment", "service", "app", "application", "workload"}

    candidates = []

    def _push(name: str) -> None:
        name = re.sub(r"[-_.]{2,}", "-", name.strip("-._"))
        if name and name not in candidates:
            candidates.append(name)

    _push(cleaned.replace(" ", "-"))
    _push(cleaned.replace(" ", ""))

    if len(tokens) > 1 and tokens[-1] in generic_suffixes:
        trimmed = tokens[:-1]
        if trimmed:
            _push("-".join(trimmed))
            _push("".join(trimmed))

    return candidates


def _resolve_pod_ns_and_name(params: dict, pod_name: str) -> tuple[str, str]:
    """Return (namespace, exact_pod_name) for pod-specific tool calls.

    Always runs find_workload to resolve partial/prefix pod names to the first
    real running pod name (pods have suffixes like -10, -7d4f9b-xkj2p that
    users never type). When namespace is explicitly given, results are filtered
    to that namespace. Falls back to the original values if discovery fails.
    """
    explicit_ns = params.get("namespace")
    if not pod_name:
        return explicit_ns or "default", pod_name

    for candidate in _candidate_workload_names(pod_name) or [pod_name]:
        try:
            from k8s.wrappers import find_workload
            fw = find_workload(candidate)
            # find_workload returns {"pods": [...], "deployments": [...], "services": [...]}
            pods = fw.get("pods", [])
            deps = fw.get("deployments", [])

            # When namespace was explicitly given, prefer matches from that namespace
            if explicit_ns:
                ns_pods = [p for p in pods if p.get("namespace") == explicit_ns]
                ns_deps = [d for d in deps if d.get("namespace") == explicit_ns]
                # Fall back to any match if none in the specified namespace
                pods = ns_pods or pods
                deps = ns_deps or deps

            # Prefer an exact pod match (gives us the full name with ordinal / hash suffix)
            if pods:
                first = pods[0]
                return first.get("namespace") or explicit_ns or "default", first.get("name", candidate)

            # Fall back to deployment namespace (pod name stays normalized for later matching)
            if deps:
                return deps[0].get("namespace") or explicit_ns or "default", candidate
        except Exception:
            pass

    return explicit_ns or "default", pod_name




def _keyword_route(message: str, history: list = None) -> dict:
    """Simple keyword-based fallback router when Gemini is unavailable."""
    msg = message.lower().strip()
    history = history or []

    # ── Short follow-up questions (< 40 chars, no obvious entity) ─────────────
    # Repeat the last tool with the same params rather than misrouting
    SHORT_FOLLOWUPS = [
        "any warnings", "any errors", "any issues", "what about warnings",
        "show warnings", "show errors", "show issues", "any critical",
        "what happened", "why", "more details", "tell me more",
    ]
    if len(msg) < 50 and history:
        for phrase in SHORT_FOLLOWUPS:
            if phrase in msg:
                # Try to pick up namespace from last assistant turn; default to "*" (all)
                ns = "*"
                for m in reversed(history):
                    if m.role == "assistant":
                        ns_hit = re.search(r'"namespace"\s*:\s*"([^"]+)"', m.content)
                        if ns_hit and ns_hit.group(1) not in ("*", "all"):
                            ns = ns_hit.group(1)
                        break
                field = None
                if any(w in msg for w in ["warning", "warn"]):
                    field = "type=Warning"
                ns_label = "all namespaces" if ns == "*" else f"namespace {ns}"
                return {
                    "tool": "get_events",
                    "params": {"namespace": ns, "field_selector": field},
                    "explanation": f"Getting {'warning ' if field else ''}events across {ns_label}",
                }

    # ── "Are there any X?" → check live cluster (events or pods) ──────────────
    # These are cluster-status questions, NOT error analysis requests.
    # Pattern: short question asking if something exists in the cluster.
    cluster_check = re.search(
        r"^(are there|any|do (we|i|you) have|show me|check for|is there).{0,60}"
        r"(oom|crash|crashloop|evict|pending|imagepull|error|warning|fail|issue|problem)",
        msg,
    )
    if cluster_check and len(message) < 120 and "runbook" not in msg:
        # Default to "*" (all namespaces) when none specified.
        # Pod conditions are filtered out by _extract_namespace itself.
        ns = _extract_namespace_or_all(msg)
        ns_label = "all namespaces" if ns == "*" else f"namespace '{ns}'"
        pod_status = _pod_status_filter_for_question(msg)
        if pod_status:
            return {
                "tool": "get_pods",
                "params": {"namespace": ns, "status_filter": pod_status},
                "explanation": f"Checking pods in {pod_status} state across {ns_label}",
            }
        if re.search(r"warning|error|fail|issue|problem", msg):
            return {"tool": "get_events",
                    "params": {"namespace": ns, "field_selector": "type=Warning"},
                    "explanation": f"Checking live cluster events across {ns_label} for issues"}
        return {"tool": "get_pods",
                "params": {"namespace": ns},
                "explanation": f"Checking pod status across {ns_label}"}

    # ── Error pasted → analyze ─────────────────────────────────────────────
    error_keywords = ["crashloopbackoff", "oomkilled", "imagepullbackoff", "error:", "exception:",
                      "failed:", "traceback", "panic:", "fatal:", "evicted", "pending",
                      "backoff", "oomkill", "exitcode", "exit code", "connection refused",
                      "timeout", "permission denied", "forbidden", "responseerror", "response error",
                      "code=404", "code=403", "code=500", "not found", "does not exist", "unable to connect",
                      "could not locate", "fail to", "failed to", "error occurred"]
    if any(k in msg for k in error_keywords) and len(message) > 60:
        return {"tool": "analyze_error", "params": {"error_text": message},
                "explanation": "Detected a pasted error — analyzing with AI"}

    # ── Namespace Analysis ──────────────────────────────────────────────────
    if re.search(r"analyze|health|holistic", msg) and re.search(r"namespace", msg):
        of_match = re.search(r"(?:health\s+)?of\s+(?:the\s+)?([a-z0-9][a-z0-9.-]*)", msg)
        of_ns = of_match.group(1) if of_match else None
        if of_ns in _NAMESPACE_STOPWORDS:
            of_ns = None
        ns = _extract_namespace(msg) or of_ns or "default"
        # Avoid matching "analyze error" which is handled earlier
        if "error" not in msg:
            return {"tool": "analyze_namespace", "params": {"namespace": ns},
                    "explanation": f"Analyzing health of namespace '{ns}'"}

    # ── Workload investigation ──────────────────────────────────────────────
    if re.search(r"investigate|triage|debug|diagnose", msg) and not re.search(r"pod", msg):
        wl_match = re.search(r"(?:deployment|statefulset|daemonset|workload|app|application)[:\s]+(\S+)|investigate\s+(\S+)", msg)
        wl = (wl_match.group(1) or wl_match.group(2)) if wl_match else ""
        if wl and wl not in ["pod", "namespace"]:
            ns = _extract_namespace(msg, "default")
            return {"tool": "investigate_workload", 
                    "params": {"namespace": ns, "workload_name": wl, "workload_type": "deployment", "use_ai": True},
                    "explanation": f"Investigating workload '{wl}' in '{ns}'"}

    # ── Pod investigation ───────────────────────────────────────────────────
    targeted_failure = re.search(
        r"^(?:why is|why are|why isn't|why isnt|what is wrong with|what's wrong with|whats wrong with)\s+"
        r"(?:the\s+)?([a-z0-9][a-z0-9\s\-\.]{0,80}?)\s+"
        r"(crashing|failing|restarting|pending|unhealthy|down|not starting|not running)\b",
        msg,
    )
    if targeted_failure:
        pod = _candidate_workload_names(targeted_failure.group(1).strip("?. "))[0]
        params_inv: dict = {"pod_name": pod, "use_ai": True}
        targeted_ns = _extract_namespace(msg)
        if targeted_ns:
            params_inv["namespace"] = targeted_ns
        return {
            "tool": "investigate_pod",
            "params": params_inv,
            "explanation": f"Investigating why '{pod}' is failing",
        }

    if re.search(r"investigate|triage|debug|diagnose", msg):
        pod_match = re.search(r"pod[:\s]+(\S+)|pod\s+named?\s+(\S+)", msg)
        pod = (pod_match.group(1) or pod_match.group(2)) if pod_match else ""
        # Omit namespace when not stated so _dispatch_inner can auto-discover it
        params_inv: dict = {"pod_name": pod, "use_ai": True}
        _ns = _extract_namespace(msg)
        if _ns:
            params_inv["namespace"] = _ns
        return {"tool": "investigate_pod", "params": params_inv,
                "explanation": f"Investigating pod '{pod}'"}

    # ── Logs ───────────────────────────────────────────────────────────────
    if re.search(r"\blog\b|logs\b", msg):
        # Extract pod/workload name: handle "for one of the X pods", "for pod X", "for the X pod"
        pod_match = re.search(
            r"(?:pod[:\s]+(\S+)|"
            r"(?:for|of|from)\s+(?:one\s+of\s+the\s+|the\s+)?([a-z0-9][a-z0-9\-\.]+)\s*pods?\b)",
            msg,
        )
        # Namespace only when the user actually named one; None lets the
        # dispatcher auto-discover it.
        ns = _extract_namespace(msg)
        pod = (pod_match.group(1) or pod_match.group(2)) if pod_match else ""
        # Omit namespace when not explicitly stated — _dispatch_inner will auto-discover it
        params_log: dict = {"pod_name": pod, "previous": "previous" in msg or "crash" in msg}
        if ns:
            params_log["namespace"] = ns
        return {"tool": "get_pod_logs", "params": params_log, "explanation": "Fetching pod logs"}

    # ── Simple pod status inventory ────────────────────────────────────────
    if _simple_pod_status_inventory_prompt(msg):
        pod_status = _pod_status_filter_for_question(msg)
        ns = _extract_namespace(msg, "*")
        ns_label = "all namespaces" if ns == "*" else f"namespace '{ns}'"
        return {
            "tool": "get_pods",
            "params": {"namespace": ns, "status_filter": pod_status},
            "explanation": f"Checking pods in {pod_status} state across {ns_label}",
        }

    # ── Pods list ──────────────────────────────────────────────────────────
    pod_focus_params: dict[str, bool] = {}
    if "pod" in msg or re.search(r"\bimages?\b.*\brunning\b", msg):
        if re.search(r"\blabels?\b", msg) and not re.search(r"\bstatus|ready|restart|image|resources?|requests?|limits?|cpu|memory|where|scheduled|node\b", msg):
            pod_focus_params["labels_only"] = True
        elif re.search(r"\bimages?\b", msg):
            pod_focus_params["images_only"] = True
        elif re.search(r"\b(resources?|requests?|limits?|cpu|memory)\b", msg):
            pod_focus_params["resources_only"] = True
        elif re.search(r"\b(where|placement|scheduled|node_selector|node selector|tolerations?|affinity|which nodes?)\b", msg):
            pod_focus_params["placement_only"] = True

    if pod_focus_params and re.search(r"list|show|get|all|what|which|where", msg):
        ns = _extract_namespace_or_all(msg)
        params = {"namespace": ns, **pod_focus_params}
        return {"tool": "get_pods", "params": params,
                "explanation": f"Listing focused pod inventory in {'all namespaces' if ns == '*' else f'namespace {ns}'}"}

    if "pod" in msg and re.search(r"list|show|get|all|running|status", msg):
        ns = _extract_namespace(msg, "default")
        return {"tool": "get_pods", "params": {"namespace": ns},
                "explanation": f"Listing pods in namespace {ns}"}

    # ── Events (warnings, errors, recent activity) ─────────────────────────
    if re.search(r"event|warning|warn|recent|what.s happening|happening", msg):
        ns = _extract_namespace(msg, "*")
        field = "type=Warning" if re.search(r"warning|warn|error|issue", msg) else None
        ns_label = "all namespaces" if ns == "*" else f"namespace {ns}"
        return {"tool": "get_events",
                "params": {"namespace": ns, "field_selector": field},
                "explanation": f"Getting {'warning ' if field else ''}events across {ns_label}"}

    # ── Single-node resource/capacity checks ───────────────────────────────
    if (
        re.search(r"\b(cpu|memory|resources?|allocated|allocatable|capacity)\b", msg)
        and not re.search(r"\b(all|every|each)\b.*\bnodes?\b|\bnodes\b", msg)
    ):
        node_match = (
            re.search(r"\bnode[:\s]+([a-z0-9][a-z0-9.-]+)\b", msg)
            or re.search(r"\b(?:to|for|on)\s+([a-z0-9][a-z0-9.-]*-[a-z0-9.-]*)\b", msg)
            or re.search(r"\b([a-z0-9][a-z0-9.-]*-[a-z0-9.-]*)\b", msg)
        )
        if node_match:
            node_name = node_match.group(1).strip("?.!,")
            return {
                "tool": "investigate_node",
                "params": {"node_name": node_name},
                "explanation": f"Checking allocated resources on node '{node_name}'",
            }

    # ── Nodes list / labels ─────────────────────────────────────────────────
    if (
        (re.search(r"\bnodes\b", msg) or re.search(r"\b(all|every|each)\b.*\bnode\b", msg))
        and re.search(r"list|show|get|all|labels?|roles?|ready|status|capacity|taints?|conditions?|addresses?|unschedulable", msg)
    ):
        labels_only = (
            bool(re.search(r"\blabels?\b", msg))
            and not re.search(r"\b(status|ready|roles?|capacity|allocatable|resources?|cpu|memory|version|os)\b", msg)
        )
        node_params: dict[str, bool] = {}
        if labels_only:
            node_params["labels_only"] = True
        elif re.search(r"\btaints?\b|unschedulable", msg):
            node_params["taints_only"] = True
        elif re.search(r"\bconditions?\b", msg):
            node_params["conditions_only"] = True
        elif re.search(r"\baddresses?|internalip|externalip|hostnames?\b", msg):
            node_params["addresses_only"] = True
        return {"tool": "get_nodes", "params": node_params,
                "explanation": "Listing all nodes with status, roles, resources, and labels"}

    # ── All resources in a namespace ───────────────────────────────────────
    if re.search(r"all resources|everything|all (the\s+)?things|what.s running|what is running", msg):
        ns = _extract_namespace(msg, "default")
        return {"tool": "list_namespace_resources", "params": {"namespace": ns},
                "explanation": f"Listing all resources in namespace '{ns}'"}

    # ── Resource graph / topology visualization ─────────────────────────────
    if re.search(r"visualize|visualise|resource graph|topology|draw.*(namespace|cluster)|map the (namespace|cluster)", msg):
        ns = _extract_namespace(msg, "default")
        return {"tool": "get_resource_graph", "params": {"namespace": ns},
                "explanation": f"Building resource graph for namespace '{ns}'"}

    # ── List services (no specific name) ────────────────────────────────────
    if re.search(r"^services\??$|list services|show services|get services|what services", msg):
        # Try to pick up namespace from recent history context
        ns = _extract_namespace(msg, "default")
        return {"tool": "list_services", "params": {"namespace": ns},
                "explanation": f"Listing all services in namespace '{ns}'"}

    # ── Namespaces ─────────────────────────────────────────────────────────
    if re.search(r"namespace|namespaces", msg) and re.search(r"list|show|get|what|which|all|have|do i", msg):
        return {"tool": "get_namespaces", "params": {},
                "explanation": "Listing all namespaces in the cluster"}

    # ── Contexts / clusters ────────────────────────────────────────────────
    if any(k in msg for k in ["context", "cluster", "kubeconfig", "which cluster", "what cluster"]):
        return {"tool": "list_contexts", "params": {},
                "explanation": "Listing available cluster contexts"}

    # ── Runbook — only when explicitly requested ────────────────────────────
    if "runbook" in msg or re.search(r"(generate|write|create)\s+(a\s+)?(doc|guide|documentation)", msg):
        return {"tool": "generate_runbook", "params": {"error_text": message},
                "explanation": "Generating runbook"}

    # ── Fix commands ───────────────────────────────────────────────────────
    if re.search(r"fix|how (to|do i|can i) (fix|resolve|solve)|command", msg):
        return {"tool": "get_fix_commands", "params": {"error_text": message},
                "explanation": "Getting fix commands"}

    # ── Port / service detail query ────────────────────────────────────────────
    # "what port does grafana use?", "ports of grafana-operator", "i need the ports of X"
    port_query = re.search(r"\bport\b", msg)
    if port_query:
        # Try to extract a service/app name from the message
        svc_match = re.search(
            r"(?:port(?:s)?\s+(?:of|for)\s+|connect\s+to\s+|of\s+service\s+)"
            r"([a-z0-9][a-z0-9\-\.]{1,60})",
            msg,
        ) or re.search(
            r"([a-z0-9][a-z0-9\-\.]{2,60})\s+(?:port|ports|service)",
            msg,
        )
        svc_name = svc_match.group(1).strip("?. ") if svc_match else ""
        stopwords = {"the", "a", "an", "my", "our", "all", "any", "this", "that", "what", "which"}
        if svc_name and svc_name not in stopwords:
            params_svc: dict = {"service_name": svc_name}
            _ns = _extract_namespace(msg)
            if _ns:
                params_svc["namespace"] = _ns
            return {"tool": "get_service", "params": params_svc,
                    "explanation": f"Getting port details for service '{svc_name}'"}

    # ── Named workload lookup without namespace → search all ──────────────────
    # "check status of argocd", "is nginx running", "where is prometheus"
    workload_match = re.search(
        r"(?:status of|check|find|where is|is .+? running)\s+([a-z0-9][a-z0-9\-\.]{1,40})",
        msg,
    )
    if workload_match and not re.search(r"namespace[:\s]|in\s+\w+\s+namespace", msg):
        name = workload_match.group(1).strip("?. ")
        stopwords = {"the", "a", "an", "my", "our", "all", "any", "pods", "events", "logs"}
        if name not in stopwords:
            return {"tool": "find_workload", "params": {"name": name},
                    "explanation": f"Searching for '{name}' across all namespaces"}

    # ── Deployment status (namespace explicitly given) ─────────────────────────
    if re.search(r"deployment|deploy|rollout|replica", msg):
        dep_match = re.search(r"deployment[:\s]+(\S+)|deploy[:\s]+(\S+)", msg)
        ns = _extract_namespace(msg)
        dep = (dep_match.group(1) or dep_match.group(2)) if dep_match else ""
        focus_params: dict[str, bool] = {}
        if re.search(r"\blabels?\b", msg) and not re.search(r"\bstatus|ready|replicas?|image|resources?|requests?|limits?|cpu|memory|template\b", msg):
            focus_params["labels_only"] = True
        elif re.search(r"\bimages?\b", msg):
            focus_params["images_only"] = True
        elif re.search(r"\b(resources?|requests?|limits?|cpu|memory)\b", msg):
            focus_params["resources_only"] = True
        elif re.search(r"\b(template|pod template|service account|node selector|node_selector|tolerations?|affinity|volumes?)\b", msg):
            focus_params["template_only"] = True
        if dep and ns:
            return {"tool": "get_deployment",
                    "params": {"namespace": ns, "deployment_name": dep, **focus_params},
                    "explanation": f"Checking deployment {dep} in {ns}"}
        elif dep:
            return {"tool": "find_workload", "params": {"name": dep},
                    "explanation": f"Searching for '{dep}' across all namespaces"}

    # ── Default: if message looks like a short question, ask for more detail ───
    if re.search(r"^(what|how|why|when|is|are|can|does|do|any|show|list|tell)", msg) and len(msg) < 60:
        return {"tool": "none", "params": {},
                "explanation": (
                    "I need a bit more detail to help you. Try asking something like:\n"
                    "- \"List all pods in the production namespace\"\n"
                    "- \"Are there any warnings in the default namespace?\"\n"
                    "- \"Investigate pod my-app-xyz in namespace staging\"\n"
                    "- Or paste an error message directly."
                )}

    # ── Default → analyze ──────────────────────────────────────────────────
    return {"tool": "analyze_error", "params": {"error_text": message},
            "explanation": "Analyzing your message as an error/question"}


def _pod_status_filter_for_question(msg: str) -> Optional[str]:
    if re.search(r"crash\s*loop|crashloop|crashlopp|crashloopbackoff", msg):
        return "CrashLoopBackOff"
    if re.search(r"imagepull|image\s*pull|errimagepull", msg):
        return "ImagePullBackOff"
    if re.search(r"\bpending\b", msg):
        return "Pending"
    if re.search(r"\boomkilled|oom\b", msg):
        return "OOMKilled"
    if re.search(r"\bevicted\b", msg):
        return "Evicted"
    return None


def _simple_pod_status_inventory_prompt(message: str) -> bool:
    msg = (message or "").lower().strip()
    if not _pod_status_filter_for_question(msg):
        return False
    if not re.search(r"\bpods?\b", msg):
        return False
    if re.search(
        r"\b("
        r"why|identify|root\s*cause|debug|diagnose|investigate|troubleshoot|help\s+me"
        r"|figure\s+out|what\s+should|what\s+do\s+i|how\s+do\s+i|determine"
        r"|check"
        r")\b",
        msg,
    ):
        return False
    return bool(re.search(r"\b(any|are there|show|list|get|which|what)\b", msg))


def _friendly_summary(tool: str, result: dict, explanation: str) -> str:
    """Fallback static summary used when synthesis is unavailable."""
    if tool == "investigate_node" and isinstance(result, dict):
        node_summary = _node_resource_summary_text(result)
        if node_summary:
            return node_summary

    if tool in {"investigate_pod", "investigate_workload", "analyze_namespace"} and isinstance(result, dict):
        evidence_summary = result.get("evidence_summary", {})
        if isinstance(evidence_summary, dict) and evidence_summary.get("suspected_root_cause"):
            root = str(evidence_summary.get("suspected_root_cause", "")).strip()
            fix = str(evidence_summary.get("suggested_fix", "")).strip()
            if fix:
                return f"{root}\n\nSuggested fix: {fix}"
            return root

        ai = result.get("ai", {})
        ai_analysis = ai.get("ai_analysis", {}) if isinstance(ai, dict) else {}
        if isinstance(ai_analysis, dict) and ai_analysis.get("root_cause"):
            root = str(ai_analysis.get("root_cause", "")).strip()
            solution = str(ai_analysis.get("solution", "")).strip()
            if solution:
                return f"{root}\n\nSuggested fix: {solution}"
            return root

        if tool == "investigate_pod":
            pod_name = result.get("pod_name") or "This pod"
            classification = result.get("classification", {})
            mode = classification.get("mode") if isinstance(classification, dict) else None
            if mode == "CrashLoopBackOff":
                return f"`{pod_name}` is in **CrashLoopBackOff**. I collected describe output, logs, and events to help pinpoint the root cause."
            if mode == "ImagePullBackOff":
                return f"`{pod_name}` is failing because the image cannot be pulled. I collected describe output and events for the exact pull failure."
            if mode == "Pending":
                return f"`{pod_name}` is stuck in **Pending**. I collected describe output and scheduling events to show why it is not starting."

    summaries = {
        "analyze_error": "Here's the AI diagnosis for your error:",
        "investigate_pod": "I investigated the pod and collected the most relevant diagnostics.",
        "get_pods": "Here are the pods I found:",
        "get_pod_logs": "Here are the pod logs:",
        "get_events": "Here are the recent events:",
        "get_deployment": "Here's the deployment status:",
        "get_service": "Here's the service details:",
        "get_endpoints": "Here are the endpoints:",
        "get_fix_commands": "Here are the fix commands:",
        "generate_runbook": "Here's the generated runbook:",
        "cluster_report": "Here's the cluster health report:",
        "error_summary": "Here's the error summary:",
        "list_contexts": "Here are your configured clusters:",
        "list_kubeconfig_contexts": "Here are your configured clusters:",
        "switch_context": "Context switched:",
        "find_workload": "Here's what I found across all namespaces:",
        "get_rollout_status": "Here's the rollout status:",
        "get_namespaces": "Here are the namespaces in this cluster:",
        "get_nodes": "Here are the nodes in this cluster:",
        "investigate_node": "Here are the node details:",
        "list_namespace_resources": "Here are all resources in the namespace:",
        "list_services": "Here are the services in the namespace:",
        "get_resource_graph": "Here is the resource graph for the namespace:",
        "investigate_workload": "I investigated the workload and summarized the main issue.",
        "analyze_namespace": "I analyzed the namespace health and summarized the main issues.",
    }
    return summaries.get(tool, explanation)


def _node_resource_focus(result: dict) -> dict:
    allocated = result.get("allocated", {}) if isinstance(result.get("allocated"), dict) else {}
    capacity = result.get("capacity", {}) if isinstance(result.get("capacity"), dict) else {}
    allocatable = result.get("allocatable", {}) if isinstance(result.get("allocatable"), dict) else {}
    return {
        "name": result.get("name") or result.get("query"),
        "query": result.get("query"),
        "status": result.get("status"),
        "roles": result.get("roles"),
        "capacity": {
            "cpu": capacity.get("cpu"),
            "cpu_millicores": capacity.get("cpu_millicores"),
            "memory_gib": capacity.get("memory_gib"),
        },
        "allocatable": {
            "cpu": allocatable.get("cpu"),
            "cpu_millicores": allocatable.get("cpu_millicores"),
            "memory_gib": allocatable.get("memory_gib"),
        },
        "allocated": {
            "cpu_requests_millicores": allocated.get("cpu_requests_millicores"),
            "cpu_requests_cores": allocated.get("cpu_requests_cores"),
            "cpu_requests_percent_of_allocatable": allocated.get("cpu_requests_percent_of_allocatable"),
            "cpu_limits_millicores": allocated.get("cpu_limits_millicores"),
            "cpu_limits_cores": allocated.get("cpu_limits_cores"),
            "cpu_limits_percent_of_allocatable": allocated.get("cpu_limits_percent_of_allocatable"),
            "memory_requests_gib": allocated.get("memory_requests_gib"),
            "memory_requests_percent_of_allocatable": allocated.get("memory_requests_percent_of_allocatable"),
            "memory_limits_gib": allocated.get("memory_limits_gib"),
            "memory_limits_percent_of_allocatable": allocated.get("memory_limits_percent_of_allocatable"),
            "non_terminated_pods": allocated.get("non_terminated_pods"),
        },
        "pods": result.get("pods", [])[:20] if isinstance(result.get("pods"), list) else [],
    }


def _node_resource_summary_text(result: dict) -> str:
    focused = _node_resource_focus(result)
    allocated = focused.get("allocated", {})
    allocatable = focused.get("allocatable", {})
    if not isinstance(allocated, dict) or not allocated:
        return ""

    name = focused.get("name") or "the node"
    cpu_req = allocated.get("cpu_requests_cores")
    cpu_req_pct = allocated.get("cpu_requests_percent_of_allocatable")
    cpu_lim = allocated.get("cpu_limits_cores")
    cpu_lim_pct = allocated.get("cpu_limits_percent_of_allocatable")
    alloc_cpu = allocatable.get("cpu") if isinstance(allocatable, dict) else None
    pods = allocated.get("non_terminated_pods")

    if cpu_req is None and cpu_lim is None:
        return ""

    return (
        f"On node `{name}`, CPU allocation is **{cpu_req} cores requested** "
        f"({cpu_req_pct}% of {alloc_cpu} allocatable cores) and **{cpu_lim} cores limited** "
        f"({cpu_lim_pct}% of allocatable). This is across **{pods} non-terminated pods**."
    )


def _meaningfully_different(a: str, b: str) -> bool:
    a_words = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if len(w) > 3}
    b_words = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if len(w) > 3}
    if not a_words or not b_words:
        return bool(a.strip() and b.strip() and a.strip() != b.strip())
    overlap = len(a_words & b_words) / max(1, min(len(a_words), len(b_words)))
    return overlap < 0.45


def _pod_investigation_summary_text(result: dict) -> str:
    """Compose a concise deterministic answer from verified and advisory evidence."""
    evidence_summary = result.get("evidence_summary")
    evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
    verified_root = str(evidence_summary.get("suspected_root_cause") or "").strip()
    verified_fix = str(evidence_summary.get("suggested_fix") or "").strip()

    ai = result.get("ai") if isinstance(result.get("ai"), dict) else {}
    ai_analysis = ai.get("ai_analysis") if isinstance(ai, dict) and isinstance(ai.get("ai_analysis"), dict) else {}
    advisory_root = str(ai_analysis.get("root_cause") or "").strip() if isinstance(ai_analysis, dict) else ""
    advisory_fix = str(ai_analysis.get("solution") or "").strip() if isinstance(ai_analysis, dict) else ""

    if not verified_root:
        return ""

    pod = result.get("pod_name") or result.get("pod") or "the pod"
    namespace = result.get("namespace") or "the namespace"
    lines = [
        f"`{pod}` in namespace `{namespace}` has verified evidence: **{verified_root}**"
    ]

    if advisory_root and _meaningfully_different(verified_root, advisory_root):
        lines.append(f"Additional log/AI analysis points to another failing container issue: **{advisory_root}**")

    container_findings = result.get("container_log_findings")
    if isinstance(container_findings, list):
        issue_lines = []
        for finding in container_findings:
            if not isinstance(finding, dict):
                continue
            container = finding.get("container")
            reason = finding.get("reason") or finding.get("last_reason") or ""
            previous = finding.get("logs_previous") if isinstance(finding.get("logs_previous"), dict) else {}
            current = finding.get("logs_current") if isinstance(finding.get("logs_current"), dict) else {}
            excerpt = str(previous.get("excerpt") or current.get("excerpt") or "").strip()
            if not container or not (reason or excerpt):
                continue
            compact_excerpt = re.sub(r"\s+", " ", excerpt)[:220]
            issue = f"`{container}`"
            if reason:
                issue += f" ({reason})"
            if compact_excerpt:
                issue += f": {compact_excerpt}"
            issue_lines.append(issue)
        if issue_lines:
            lines.append("Container-level findings: " + "; ".join(issue_lines[:4]))

    fix = verified_fix or advisory_fix
    if fix:
        lines.append(f"Recommended fix: {fix}")

    return "\n\n".join(lines)


# Tools whose output Gemini should synthesise into a direct answer.
# AI tools (analyze_error, generate_runbook, etc.) already produce natural
# language — a second Gemini pass on those would be wasteful.
_SYNTHESIZE_TOOLS = {
    "get_pods", "get_events", "get_deployment", "get_service",
    "get_endpoints", "get_rollout_status", "find_workload",
    "list_namespace_resources", "list_services", "get_namespaces",
    "get_nodes", "get_pod_logs", "list_contexts", "investigate_pod",
    "investigate_node", "investigate_workload", "analyze_namespace",
}


def _synthesize_answer(question: str, tool: str, result: dict) -> tuple[Optional[str], Optional[str]]:
    """Use the configured LLM to write a concise direct answer to the user's question.

    Takes the original question and the tool result, asks the LLM for a
    1-2 sentence summary that directly answers what was asked rather than
    just saying "here are the pods".

    Returns (answer, error) where both can be None.
    """
    if tool not in _SYNTHESIZE_TOOLS:
        return None, None

    if tool == "investigate_pod" and isinstance(result, dict):
        deterministic = _pod_investigation_summary_text(result)
        if deterministic:
            return deterministic, None

    provider = _llm_provider()
    if provider is None or not provider.enabled:
        return None, None

    import json as _json

    # For investigate_pod, prefer deterministic evidence gathered by tools.
    # AI analysis is included as advisory context, not the primary source.
    if tool == "investigate_pod":
        focused = {
            "pod": result.get("pod_name") or result.get("pod"),
            "namespace": result.get("namespace"),
            "classification": result.get("classification"),
            "pod_spec_summary": result.get("pod_spec_summary"),
            "evidence_summary": result.get("evidence_summary"),
            "container_log_findings": result.get("container_log_findings"),
            "ai_analysis_advisory": (result.get("ai") or {}).get("ai_analysis"),
            "steps_run": result.get("steps_run"),
        }
        result_text = _json.dumps(focused, default=str)[:8000]
    elif tool == "investigate_node":
        result_text = _json.dumps(_node_resource_focus(result), default=str)[:8000]
    elif tool == "investigate_workload":
        focused = {
            "workload_name": result.get("workload_name"),
            "workload_type": result.get("workload_type"),
            "namespace": result.get("namespace"),
            "workload_summary": result.get("workload_summary"),
            "related_pods_summary": result.get("related_pods_summary"),
            "events_parsed": result.get("events_parsed"),
            "ai_analysis_advisory": (result.get("ai") or {}).get("ai_analysis"),
            "steps_run": result.get("steps_run"),
        }
        result_text = _json.dumps(focused, default=str)[:8000]
    elif tool == "analyze_namespace":
        focused = {
            "namespace": result.get("namespace"),
            "issue_summary": result.get("issue_summary"),
            "resource_summary": (result.get("resources") or {}).get("summary"),
            "events_summary": result.get("events_summary") or (result.get("events") or {}).get("events_summary"),
            "ai_analysis_advisory": (result.get("ai") or {}).get("ai_analysis"),
            "steps_run": result.get("steps_run"),
        }
        result_text = _json.dumps(focused, default=str)[:8000]
    elif tool == "get_pods":
        if result.get("focused_modes"):
            focused = {
                "namespace": result.get("namespace"),
                "pod_count": result.get("pod_count"),
                "namespace_summary": result.get("namespace_summary"),
                "focused_modes": result.get("focused_modes"),
                "pods": result.get("pods", []),
            }
            result_text = _json.dumps(focused, default=str)[:8000]
        else:
            # For pod listings, always send the health summary first so the LLM
            # sees unhealthy pods even when the full list is 170+ entries.
            health = result.get("health_summary", {})
            focused = {
                "namespace": result.get("namespace"),
                "pod_count": result.get("pod_count"),
                "health_summary": health,
            }
            # If there are few enough pods, include the full list
            full_json = _json.dumps(result, default=str)
            if len(full_json) <= 3000:
                result_text = full_json
            else:
                # Health summary + first/last pods for context
                result_text = _json.dumps(focused, default=str)[:3000]
    elif tool == "get_nodes":
        focused = {
            "node_count": result.get("node_count"),
            "nodes": [
                {
                    "name": node.get("name"),
                    "status": node.get("status"),
                    "roles": node.get("roles"),
                    "labels": node.get("labels", {}),
                    "label_count": node.get("label_count", len(node.get("labels", {}) or {})),
                    "annotations": node.get("annotations"),
                    "taints": node.get("taints"),
                    "unschedulable": node.get("unschedulable"),
                    "addresses": node.get("addresses"),
                    "conditions": node.get("conditions"),
                }
                for node in result.get("nodes", [])
            ],
        }
        result_text = _json.dumps(focused, default=str)[:8000]
    elif tool == "get_deployment":
        if result.get("focused_modes"):
            result_text = _json.dumps(result, default=str)[:8000]
        else:
            focused = {
                "name": result.get("name"),
                "namespace": result.get("namespace"),
                "replicas": result.get("replicas"),
                "health_status": result.get("health_status"),
                "diagnostic_hint": result.get("diagnostic_hint"),
                "selector": result.get("selector"),
                "labels": result.get("labels", {}),
                "revision": result.get("revision"),
                "generation": result.get("generation"),
                "observed_generation": result.get("observed_generation"),
                "conditions": result.get("conditions", []),
                "pod_template": result.get("pod_template", {}),
            }
            result_text = _json.dumps(focused, default=str)[:8000]
    elif tool == "list_namespace_resources":
        focused = {
            "namespace": result.get("namespace"),
            "summary": result.get("summary"),
            "pods": result.get("pods", [])[:50],
            "services": result.get("services", [])[:50],
            "deployments": result.get("deployments", [])[:50],
            "statefulsets": result.get("statefulsets", [])[:50],
            "daemonsets": result.get("daemonsets", [])[:50],
            "configmaps": result.get("configmaps", [])[:50],
            "persistent_volume_claims": result.get("persistent_volume_claims", [])[:50],
            "ingresses": result.get("ingresses", [])[:50],
        }
        result_text = _json.dumps(focused, default=str)[:8000]
    elif tool == "get_endpoints":
        endpoint_slices = result.get("endpoint_slices", {}) or {}
        focused = {
            "name": result.get("name"),
            "namespace": result.get("namespace"),
            "has_endpoints": result.get("has_endpoints"),
            "ready_count": result.get("ready_count"),
            "not_ready_count": result.get("not_ready_count"),
            "ready_addresses": result.get("ready_addresses", [])[:50],
            "not_ready_addresses": result.get("not_ready_addresses", [])[:50],
            "ports": result.get("ports", []),
            "diagnostic_hint": result.get("diagnostic_hint"),
            "endpoint_slice_count": result.get("endpoint_slice_count"),
            "endpoint_slice_endpoint_count": result.get("endpoint_slice_endpoint_count"),
            "endpoint_slices": {
                "slice_count": endpoint_slices.get("slice_count"),
                "endpoint_count": endpoint_slices.get("endpoint_count"),
                "ready_count": endpoint_slices.get("ready_count"),
                "not_ready_count": endpoint_slices.get("not_ready_count"),
                "serving_count": endpoint_slices.get("serving_count"),
                "terminating_count": endpoint_slices.get("terminating_count"),
                "ports": endpoint_slices.get("ports", []),
                "endpoints": endpoint_slices.get("endpoints", [])[:80],
                "diagnostic_hint": endpoint_slices.get("diagnostic_hint"),
                "error": endpoint_slices.get("error"),
            },
        }
        result_text = _json.dumps(focused, default=str)[:8000]
    elif tool == "get_service":
        focused = {
            "name": result.get("name"),
            "namespace": result.get("namespace"),
            "type": result.get("type"),
            "focused_modes": result.get("focused_modes"),
            "labels": result.get("labels", {}),
            "annotations": result.get("annotations"),
            "cluster_ip": result.get("cluster_ip"),
            "cluster_ips": result.get("cluster_ips"),
            "external_ips": result.get("external_ips"),
            "external_name": result.get("external_name"),
            "selector": result.get("selector"),
            "ports": result.get("ports", []),
            "load_balancer": result.get("load_balancer"),
            "session_affinity": result.get("session_affinity"),
            "external_traffic_policy": result.get("external_traffic_policy"),
            "internal_traffic_policy": result.get("internal_traffic_policy"),
            "ip_families": result.get("ip_families"),
            "ip_family_policy": result.get("ip_family_policy"),
            "diagnostic_hint": result.get("diagnostic_hint"),
        }
        result_text = _json.dumps(focused, default=str)[:8000]
    else:
        # Compact the result to avoid inflating the prompt — 3000 chars is
        # enough to understand pod counts, statuses, restart counts etc.
        result_text = _json.dumps(result, default=str)[:3000]

    # Scale max_tokens based on tool complexity
    _COMPLEX_TOOLS = {
        "investigate_pod", "investigate_workload", "analyze_namespace",
        "list_namespace_resources", "get_pods", "get_events", "get_nodes", "investigate_node",
        "get_deployment", "get_endpoints", "get_service",
    }
    max_tok = 800 if tool in _COMPLEX_TOOLS else 400

    system = (
        "You are a Kubernetes DevOps assistant. "
        "Answer the user's question directly and concisely in 2-4 sentences using the data provided. "
        "Be specific: mention pod names, image names, counts, or error reasons where relevant. "
        "For node CPU/resource allocation questions, answer from allocated.cpu_requests_cores, "
        "allocated.cpu_limits_cores, percentages, allocatable CPU, and non_terminated_pods. "
        "When evidence_summary is present, treat it as verified tool evidence and prefer it over AI advisory analysis. "
        "Apply semantic reasoning — do not rely on exact keyword matches: "
        "  • BackOff events on pods that are pulling images = ImagePullBackOff-related issue. "
        "  • OOMKilled in pod status or events = OOM error. "
        "  • CrashLoopBackOff in pod status = crash loop issue. "
        "Only say 'none found' if the data genuinely shows no related activity whatsoever. "
        "Do not list every event — summarise the pattern (e.g. which pods, which image, how many). "
        "Use markdown formatting: **bold** for emphasis, `inline code` for pod/resource names, "
        "and bullet points for lists of 3+ items. Keep the response concise."
    )

    try:
        answer = provider.generate(
            f"User question: {question}\n\nData returned: {result_text}",
            system=system,
            temperature=0.1,
            max_tokens=max_tok,
        )
    except Exception as e:
        err_str = str(e)
        logger.warning(f"Answer synthesis failed, using static summary: {err_str}")
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "503" in err_str or "UNAVAILABLE" in err_str:
            return None, f"AI Service Unavailable (Quota/Rate Limit): {err_str}"
        return None, None

    answer = (answer or "").strip()
    return answer if answer else None, None

# ── Suggested actions extraction ──────────────────────────────────────────────

def _extract_suggested_actions(tool: str, result: dict) -> list:
    """Extract actionable commands from tool results for the frontend.

    Looks for kubectl commands in AI analysis results, fix playbooks, and
    error analysis that the user might want to execute directly.
    Returns a list of action dicts: [{type, label, command, namespace?, confirm?}]
    """
    actions = []
    if not isinstance(result, dict):
        return actions

    # From investigate_pod AI analysis
    ai = result.get("ai", {})
    if isinstance(ai, dict):
        ai_analysis = ai.get("ai_analysis", {})
        if isinstance(ai_analysis, dict):
            for cmd in ai_analysis.get("commands", []):
                c = cmd if isinstance(cmd, str) else (cmd.get("command") or cmd.get("cmd") or "")
                desc = "" if isinstance(cmd, str) else cmd.get("description", "")
                if c and c.strip().startswith("kubectl"):
                    is_write = any(w in c for w in ["delete", "apply", "patch", "scale", "rollout restart"])
                    actions.append({
                        "type": "apply" if is_write else "run",
                        "label": desc or c[:60],
                        "command": c,
                        "confirm": is_write,
                    })

    # From analyze_error / get_fix_commands
    for cmd in result.get("commands", []):
        c = cmd if isinstance(cmd, str) else (cmd.get("command") or cmd.get("cmd") or "")
        desc = "" if isinstance(cmd, str) else cmd.get("description", "")
        if c and c.strip().startswith("kubectl"):
            is_write = any(w in c for w in ["delete", "apply", "patch", "scale", "rollout restart"])
            actions.append({
                "type": "apply" if is_write else "run",
                "label": desc or c[:60],
                "command": c,
                "confirm": is_write,
            })

    # Deduplicate by command
    seen = set()
    deduped = []
    for a in actions:
        if a["command"] not in seen:
            seen.add(a["command"])
            deduped.append(a)
    return deduped[:5]  # Cap at 5 actions


# ── ReAct-powered chat path ──────────────────────────────────────────────────

def _chat_react(
    req: ChatRequest,
    provider,
    _persist,
    session_tag: str,
    user_id: Optional[str] = None,
    route: str = "react",
    capture_enabled: bool = True,
    deadline_monotonic: Optional[float] = None,
    tool_scope_override: Optional[set[str]] = None,
) -> ChatResponse:
    """Multi-step ReAct investigation path — used when an LLM provider is available."""
    from react import build_envelope_retrieval_context, react_loop
    from agent_run_recorder import AgentRunRecorder

    logger.info("chat_react session=%s ssh=%s", session_tag, bool(req.ssh))

    sid = req.session_id
    live_k8s_prompt = _looks_like_live_kubernetes_prompt(req.message)

    # Phase 1.4: ask the retrieval router whether to short-circuit (cached)
    # or pass grounding chunks to the LLM. Failures fall back to "cold"
    # automatically inside route().
    if _should_skip_rag_for_prompt(req.message):
        decision = None
        build_grounded_preamble = None
        logger.info("rag skipped for cluster-context prompt session=%s", session_tag)
    else:
        try:
            from services.rag.router import route as _rag_route, build_grounded_preamble
            decision = _rag_route(req.message)
        except Exception as exc:
            logger.warning("router failed (continuing cold): %s", exc)
            decision = None
            build_grounded_preamble = None

    # Short-circuit cached path for static knowledge. For live Kubernetes
    # state questions, use the cached runbook as grounding only; the answer
    # still needs fresh kubectl evidence.
    if decision is not None and decision.mode == "cached" and not live_k8s_prompt:
        logger.info(
            "chat_react session=%s mode=cached top_score=%.3f collection=%s",
            session_tag, decision.top_score, decision.top_collection,
        )
        decision_dict = decision.to_dict()
        _log_chat_turn(
            session_tag=session_tag,
            route="react_cached",
            message=req.message,
            tool_used="rag_cached",
            rag_decision=decision_dict,
            answer=decision.cached_answer or "",
        )
        _persist("assistant", decision.cached_answer or "",
                 tool_used="rag_cached",
                 result={"rag_decision": decision_dict})
        return ChatResponse(
            reply=decision.cached_answer or "",
            tool_used="rag_cached",
            result={"rag_decision": decision_dict},
            timestamp=time.time(),
            suggested_actions=[],
            session_id=req.session_id,
        )

    if _should_answer_grounded_kb_directly(req.message, decision):
        logger.info(
            "chat_react session=%s mode=grounded_direct top_score=%.3f collection=%s",
            session_tag, decision.top_score, decision.top_collection,
        )
        decision_dict = decision.to_dict()
        answer = _format_grounded_kb_answer(req.message, decision)
        result_payload = {"rag_decision": decision_dict}
        _log_chat_turn(
            session_tag=session_tag,
            route="react_grounded_direct",
            message=req.message,
            tool_used="rag_grounded",
            rag_decision=decision_dict,
            answer=answer,
        )
        _persist("assistant", answer, tool_used="rag_grounded", result=result_payload)
        return ChatResponse(
            reply=answer,
            tool_used="rag_grounded",
            result=result_payload,
            timestamp=time.time(),
            suggested_actions=[],
            session_id=req.session_id,
            eval_retrieval_context=[
                str(c.get("content") or c.get("solution_text") or "")
                for c in decision.grounded_chunks
                if str(c.get("content") or c.get("solution_text") or "").strip()
            ],
        )

    grounded_preamble = ""
    if decision is not None and decision.mode == "grounded":
        grounded_preamble = build_grounded_preamble(decision)
        logger.info(
            "chat_react session=%s mode=grounded top_score=%.3f chunks=%d",
            session_tag, decision.top_score, len(decision.grounded_chunks),
        )
    elif decision is not None and decision.mode == "cached" and live_k8s_prompt:
        grounded_preamble = _cached_decision_as_grounding(decision)
        logger.info(
            "chat_react session=%s mode=cached_as_grounding top_score=%.3f collection=%s",
            session_tag, decision.top_score, decision.top_collection,
        )

    # Phase 7: deterministic tool scoping (feature-flagged).
    scope_decision = None
    # Machine callers may supply a server-computed scope. It takes precedence
    # over prompt-derived scoping and is enforced by react_loop before
    # dispatch, so a model cannot opt into recovery/write tools.
    tool_scope_set: Optional[set[str]] = (
        set(tool_scope_override) if tool_scope_override is not None else None
    )
    if (
        tool_scope_override is None
        and os.environ.get("TOOL_SCOPING_ENABLED", "").lower() in ("1", "true", "yes")
    ):
        try:
            from tool_scoper import scope_for_prompt
            from tool_registry import valid_tool_names
            available = frozenset(valid_tool_names("react"))
            scope_decision = scope_for_prompt(req.message, available_tools=available)
            # 'broad' = no restriction; only pin a scope when classifier matched.
            if not scope_decision.is_broad():
                tool_scope_set = set(scope_decision.allowed_tools)
                logger.info("tool_scoping session=%s scope=%s tools=%d",
                            session_tag, scope_decision.scope_name, len(tool_scope_set))
        except Exception as exc:
            logger.warning("tool_scoping failed (continuing unscoped): %s", exc)

    from react import REACT_SYSTEM_SHA, SYSTEM_PROMPT_SHA, TOOL_REGISTRY_SHA
    recorder = AgentRunRecorder.start(
        session_id=sid,
        user_id=user_id,
        route=route,
        model=getattr(provider, "model", None) or req.model,
        rag_decision=decision.to_dict() if decision is not None else None,
        tool_scope=scope_decision.to_dict() if scope_decision is not None else None,
        system_prompt_sha=SYSTEM_PROMPT_SHA,
        react_system_sha=REACT_SYSTEM_SHA,
        tool_registry_sha=TOOL_REGISTRY_SHA,
    )
    result = react_loop(
        question=req.message,
        history=req.history,
        provider=provider,
        dispatch_fn=_make_memory_capturing_dispatch(sid),
        memory_preamble=memory.build_memory_preamble(sid),
        grounded_preamble=grounded_preamble,
        run_recorder=recorder,
        tool_scope=tool_scope_set,
        deadline_monotonic=deadline_monotonic,
    )

    logger.info(
        "chat_react_done session=%s iterations=%d tools=%s elapsed_ms=%.1f",
        session_tag,
        result.total_iterations,
        ",".join(s.action for s in result.steps if s.action != "answer"),
        result.total_duration_ms,
    )

    # Persist the react steps as metadata on the assistant message
    steps_meta = [
        {"thought": s.thought, "action": s.action, "params": s.action_params,
         "duration_ms": round(s.duration_ms)}
        for s in result.steps
    ]

    persisted_result = {
        "react_steps": steps_meta,
        "tool_result": result.result,
        "synthesis_breakdown": getattr(result, "synthesis_breakdown", None),
    }

    cost_summary = None
    if os.environ.get("SHOW_COST_TO_USERS", "true").lower() in ("1", "true", "yes", "on"):
        cost_summary = {
            "total_tokens_in": getattr(recorder, "total_tokens_in", 0),
            "total_tokens_out": getattr(recorder, "total_tokens_out", 0),
            "total_cached_tokens_in": getattr(recorder, "total_cached_tokens_in", 0),
            "total_cost_usd": getattr(recorder, "total_cost_usd", 0.0),
            "model": getattr(provider, "model", None) or req.model or "",
        }
        persisted_result["run_id"] = recorder.run_id if recorder else None
        persisted_result["cost_summary"] = cost_summary

    eval_retrieval_context = build_envelope_retrieval_context(result.steps)
    decision_dict = decision.to_dict() if decision is not None else None
    if decision is not None:
        persisted_result["rag_decision"] = decision_dict

    # Phase 1.3: opportunistic capture into session_memory. Runs in-line
    # for the sync endpoint (the response is already slow). Best-effort —
    # never raises, returns None when not worthy or disabled.
    capture_id = None
    if capture_enabled:
        capture_id = _maybe_capture_chat(
            question=req.message,
            answer=result.answer,
            tool_used=result.tool_used,
            react_steps=steps_meta,
            session_id=sid,
        )
    if capture_id:
        persisted_result["capture_id"] = capture_id

    _log_react_trace(
        session_tag=session_tag,
        message=req.message,
        steps_meta=steps_meta,
        final_tool_used=result.tool_used,
        rag_decision=decision_dict,
    )
    _log_chat_turn(
        session_tag=session_tag,
        route=route,
        message=req.message,
        tool_used=result.tool_used,
        capture_id=capture_id,
        rag_decision=decision_dict,
        error=result.error,
        answer=result.answer,
    )

    _persist("assistant", result.answer, tool_used=result.tool_used,
             result=persisted_result, error=result.error)

    response_result = result.result
    if (decision is not None and decision.mode != "cold") or capture_id:
        # Surface decision + capture_id so the UI can render
        # citations + thumbs buttons.
        response_result = dict(result.result or {})
        if decision is not None and decision.mode != "cold":
            response_result["rag_decision"] = decision_dict
        if capture_id:
            response_result["capture_id"] = capture_id

    return ChatResponse(
        reply=result.answer,
        tool_used=result.tool_used,
        result=response_result,
        error=result.error,
        timestamp=time.time(),
        suggested_actions=result.suggested_actions,
        session_id=req.session_id,
        run_id=recorder.run_id if recorder is not None else None,
        synthesis_breakdown=getattr(result, "synthesis_breakdown", None),
        eval_retrieval_context=eval_retrieval_context,
        cost_summary=cost_summary,
    )


# ── Single-shot chat path (keyword fallback) ────────────────────────────────

def _chat_single_shot(
    req: ChatRequest,
    _persist,
    session_tag: str,
    tool_scope_override: Optional[set[str]] = None,
) -> ChatResponse:
    """Original single-shot route → dispatch → synthesize path.

    Used when no LLM provider is available (keyword routing only).
    """
    routing = _keyword_route(req.message, req.history)
    tool = routing.get("tool", "none")
    params = routing.get("params", {})
    explanation = routing.get("explanation", "")
    logger.info(
        "chat_single_shot session=%s tool=%s ssh=%s",
        session_tag, tool, bool(req.ssh),
    )

    # No tool needed (greeting / general question)
    if tool == "none":
        _log_chat_turn(
            session_tag=session_tag,
            route="single_shot",
            message=req.message,
            tool_used="none",
            answer=explanation,
        )
        _persist("assistant", explanation, tool_used="none")
        return ChatResponse(
            reply=explanation,
            tool_used="none",
            result=None,
            timestamp=time.time(),
            session_id=req.session_id,
        )

    if tool_scope_override is not None and tool not in tool_scope_override:
        # Server-owned scope for machine callers. This check happens before
        # registry dispatch and therefore remains effective even if a write
        # tool is accidentally exposed on the chat surface in the future.
        reply = f"Tool '{tool}' is not available for this machine invocation."
        return ChatResponse(
            reply=reply,
            tool_used=tool,
            result={"error": "tool_out_of_scope", "tool": tool},
            error="tool_out_of_scope",
            timestamp=time.time(),
            session_id=req.session_id,
        )

    # Dispatch to tool
    dispatch_started_at = time.perf_counter()
    result = _dispatch(tool, params, session_id=req.session_id)
    dispatch_elapsed_ms = (time.perf_counter() - dispatch_started_at) * 1000
    logger.info(
        "chat_dispatched session=%s tool=%s ssh=%s elapsed_ms=%.1f",
        session_tag, tool, bool(req.ssh), dispatch_elapsed_ms,
    )

    # Check if the result itself is an error
    if isinstance(result, dict) and (
        ("error" in result and len(result) <= 2) or
        (result.get("success") is False and result.get("error"))
    ):
        hint = result.get("suggestion", "")
        err = result.get("error", "Unknown error")
        reply = hint or f"I ran into an issue: {err}"
        _log_chat_turn(
            session_tag=session_tag,
            route="single_shot",
            message=req.message,
            tool_used=tool,
            tool_params=params,
            error=err,
            answer=reply,
        )
        _persist("assistant", reply, tool_used=tool, result=result, error=err)
        return ChatResponse(
            reply=reply,
            tool_used=tool,
            result=result,
            error=err,
            timestamp=time.time(),
            session_id=req.session_id,
        )

    # Build reply — static summary (no LLM available for synthesis)
    not_found_hint = result.pop("_not_found_hint", None) if isinstance(result, dict) else None
    synthesized_reply, synth_error = _synthesize_answer(req.message, tool, result)
    if synth_error:
        logger.debug("single-shot synthesis skipped: %s", synth_error)
    reply = not_found_hint or synthesized_reply or _friendly_summary(tool, result, explanation)

    # Phase 5: executable recovery actions require deterministic validation
    # plus separate LLM review. The single-shot fallback has no LLM reviewer, so
    # it intentionally fails closed and leaves recovery steps as prose only.
    actions = []

    _log_chat_turn(
        session_tag=session_tag,
        route="single_shot",
        message=req.message,
        tool_used=tool,
        tool_params=params,
        answer=reply,
    )

    _persist("assistant", reply, tool_used=tool, result=result)
    return ChatResponse(
        reply=reply,
        tool_used=tool,
        result=result,
        timestamp=time.time(),
        suggested_actions=actions,
        session_id=req.session_id,
    )


# ── Chat endpoint ─────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    """Handle a chat turn.

    If the request includes SSH credentials, all kubectl calls in this turn
    are executed on the remote master node via SSH.  The runner is reset to
    the local default after the turn completes (success or error).

    If session_id is provided, both the user message and assistant reply are
    persisted to SQLite so history survives page reloads.
    """
    ssh_runner = None
    ctx_token = None
    sid = req.session_id  # may be None for clients that don't send it
    user_id: Optional[str] = None
    if auth_utils.auth_enabled():
        user = auth_utils.require_current_user(request)
        user_id = user["id"]
        if sid:
            auth_utils.require_owned_session(request, sid)
        else:
            sid = db.create_session(user_id=user_id)["id"]
            req.session_id = sid
    session_tag = _short_session_id(sid)

    def _persist(role: str, content: str, tool_used: str = None,
                 result: dict = None, error: str = None):
        """Save a message to DB, silently ignoring errors so DB issues never
        break the chat response."""
        if not sid:
            return
        try:
            db.save_message(sid, role, content, tool_used=tool_used,
                            result=result, error=error)
        except Exception as db_err:
            logger.warning(f"DB save failed: {db_err}")

    resp = None
    try:
        # ── Set up SSH runner if credentials were provided ───────────────────
        if req.ssh:
            from k8s.ssh_runner import SSHKubectlRunner, SSHConnectionError
            from k8s.kubectl_runner import set_runner, runner_ctx

            try:
                ssh_runner = SSHKubectlRunner(
                    host=req.ssh.host,
                    username=req.ssh.username,
                    password=req.ssh.password,
                    port=req.ssh.port,
                )
                ssh_runner.connect()
                ctx_token = set_runner(ssh_runner)
                logger.info(f"SSH runner active for {req.ssh.username}@{req.ssh.host}")
            except SSHConnectionError as e:
                logger.warning(
                    "chat_ssh_connect_failed session=%s host=%s port=%s error=%s",
                    session_tag,
                    req.ssh.host,
                    req.ssh.port,
                    str(e),
                )
                resp = ChatResponse(
                    reply=f"Could not connect to {req.ssh.host} via SSH: {e}",
                    tool_used="error",
                    result=None,
                    error=str(e),
                    session_id=sid,
                )

        # ── Set up kubeconfig runner if session has a cluster connection ─────
        elif sid and not req.ssh:
            # resolve() raises rather than returning None when a connection
            # is recorded but its kubeconfig has gone. Falling through would
            # leave get_runner() on the local kubectl and run the command
            # against whatever cluster this machine points at.
            try:
                cluster_conn = cluster_session.resolve(sid)
            except cluster_session.ClusterConnectionUnavailable as exc:
                logger.warning("session %s: %s", session_tag, exc)
                return ChatResponse(
                    reply=str(exc),
                    tool_used="error",
                    result=None,
                    error="cluster_unavailable",
                    session_id=sid,
                )
            if cluster_conn and cluster_conn.get("context_name"):
                from k8s.kubectl_runner import KubectlRunner, set_runner, runner_ctx
                kube_runner = KubectlRunner(
                    kubeconfig_path=cluster_conn.get("kubeconfig_path"),
                    context=cluster_conn["context_name"],
                )
                ctx_token = set_runner(kube_runner)
                logger.info(
                    "Kubeconfig runner active: context=%s mode=%s",
                    cluster_conn["context_name"],
                    cluster_conn["mode"],
                )

        if resp is None:
            # 1. Persist the user message
            _persist("user", req.message)

            # 2. Decide: ReAct (multi-step) or single-shot
            provider = _llm_provider(req.model)
            use_react = provider is not None and provider.enabled

            if use_react and not _simple_pod_status_inventory_prompt(req.message):
                resp = _chat_react(req, provider, _persist, session_tag, user_id=user_id)
            else:
                resp = _chat_single_shot(req, _persist, session_tag)
                if not use_react:
                    resp = _note_llm_unavailable(resp, session_tag)

    except Exception as e:
        logger.exception("Chat error")
        err_reply = f"Something went wrong: {e}"
        _persist("assistant", err_reply, tool_used="error", error=str(e))
        resp = ChatResponse(
            reply=err_reply,
            tool_used="error",
            result=None,
            error=str(e),
            session_id=sid,
        )

    finally:
        # Always close SSH and restore the runner context
        if ssh_runner is not None:
            ssh_runner.close()
        if ctx_token is not None:
            from k8s.kubectl_runner import runner_ctx
            runner_ctx.reset(ctx_token)

    if resp is not None:
        from tracing import current_trace_id
        tid = current_trace_id()
        if tid:
            resp.trace_id = tid

    return resp


# ── Streaming chat endpoint (Phase A: real ReAct step events) ────────────────

def _format_sse(event: dict) -> str:
    """Serialize an event as a single SSE message."""
    return f"data: {json.dumps(event, default=str)}\n\n"


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """Streaming variant of /chat.

    Same input as /chat. Returns an SSE stream that emits real-time events
    as the ReAct loop progresses:

      data: {"type": "start"}
      data: {"type": "iteration_planned", "iteration": 1, "action": "...", "thought": "...", "params": {...}}
      data: {"type": "step_complete",    "iteration": 1, "action": "...", "duration_ms": N, "preview": "..."}
      ... (one pair per ReAct step)
      data: {"type": "done", "result": {reply, tool_used, result, suggested_actions, ...}}

    On error: `{"type": "error", "message": "..."}` followed by close.

    The single-shot (non-ReAct) fallback path is NOT streamed in Phase A —
    it returns a single "done" event after completing. Streaming the
    final-answer tokens themselves comes in Phase B.
    """
    from opentelemetry.context import get_current
    current_context = get_current()

    sid = req.session_id
    user_id: Optional[str] = None
    if auth_utils.auth_enabled():
        user = auth_utils.require_current_user(request)
        user_id = user["id"]
        if sid:
            auth_utils.require_owned_session(request, sid)
        else:
            sid = db.create_session(user_id=user_id)["id"]
            req.session_id = sid
    session_tag = _short_session_id(sid)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    import threading
    cancelled = threading.Event()
    run_id_holder: dict = {"run_id": None}

    def _enqueue(event: dict) -> None:
        # Thread-safe push from the ReAct worker thread → async generator.
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def _persist(role: str, content: str, tool_used: str = None,
                 result: dict = None, error: str = None):
        if not sid:
            return
        try:
            db.save_message(sid, role, content, tool_used=tool_used,
                            result=result, error=error)
        except Exception as db_err:
            logger.warning(f"DB save failed: {db_err}")

    def _run_react_in_thread() -> None:
        """Body of the worker thread. Mirrors /chat's setup + teardown."""
        from opentelemetry.context import attach
        token = attach(current_context)
        ssh_runner = None
        ctx_token = None
        try:
            # ── Persist user message ─────────────────────────────────────
            _persist("user", req.message)

            # ── SSH runner setup (mirrors /chat) ─────────────────────────
            if req.ssh:
                from k8s.ssh_runner import SSHKubectlRunner, SSHConnectionError
                from k8s.kubectl_runner import set_runner
                try:
                    ssh_runner = SSHKubectlRunner(
                        host=req.ssh.host, username=req.ssh.username,
                        password=req.ssh.password, port=req.ssh.port,
                    )
                    ssh_runner.connect()
                    ctx_token = set_runner(ssh_runner)
                except SSHConnectionError as e:
                    _enqueue({"type": "error",
                              "message": f"Could not connect to {req.ssh.host} via SSH: {e}"})
                    return
            elif sid:
                # See the non-streaming path: fail closed, never retarget.
                try:
                    cluster_conn = cluster_session.resolve(sid)
                except cluster_session.ClusterConnectionUnavailable as exc:
                    logger.warning("session %s: %s", session_tag, exc)
                    _enqueue({
                        "type": "done",
                        "result": {
                            "reply": str(exc),
                            "tool_used": "error",
                            "result": {},
                            "error": "cluster_unavailable",
                            "timestamp": time.time(),
                            "suggested_actions": [],
                            "session_id": sid,
                        },
                    })
                    return
                if cluster_conn and cluster_conn.get("context_name"):
                    from k8s.kubectl_runner import KubectlRunner, set_runner
                    kube_runner = KubectlRunner(
                        kubeconfig_path=cluster_conn.get("kubeconfig_path"),
                        context=cluster_conn["context_name"],
                    )
                    ctx_token = set_runner(kube_runner)

            # ── Phase 3.0: Proactive cluster triage ──────────────────────
            # On the FIRST message of a session, if proactive triage is
            # enabled, emit a one-screen cluster health greeting BEFORE
            # we do anything with the user's actual prompt. Best-effort:
            # any failure is swallowed so a slow/broken triage never
            # blocks the chat.
            try:
                from config.settings import get_settings as _gs_triage
                _settings = _gs_triage()
                _triage_on = bool(getattr(_settings, "enable_proactive_triage", False))
                if _should_run_proactive_triage(req.message, req.history, _triage_on):
                    from triage import cluster_overview, render_greeting
                    _ns = getattr(_settings, "proactive_triage_namespaces", "*") or "*"
                    _lookback = int(getattr(_settings, "proactive_triage_event_lookback_min", 10))
                    _overview = cluster_overview(
                        namespace=_ns,
                        event_lookback_minutes=_lookback,
                    )
                    _cluster_label = None
                    if sid:
                        _cc = db.get_cluster_connection(sid) or {}
                        _cluster_label = _cc.get("context_name")
                    _greeting = render_greeting(_overview, cluster_label=_cluster_label)
                    # Emit as a synthetic assistant message so the UI
                    # renders it inline above the LLM's eventual reply.
                    # Persisted so chat history shows the greeting on reload.
                    _enqueue({"type": "triage_greet",
                              "text": _greeting})
                    _persist("assistant", _greeting)
            except Exception as _triage_exc:
                logger.debug("proactive triage skipped (%s): %s",
                             type(_triage_exc).__name__, _triage_exc)

            # ── Provider / route decision ────────────────────────────────
            provider = _llm_provider(req.model)

            if not (provider and provider.enabled):
                # No LLM — fall back to single-shot. Emit a single "done"
                # so the client sees the same shape; richer streaming for
                # this path is out of scope for Phase A.
                #
                # The fallback is still useful, but it must not look like a
                # normal answer: no trace, no synthesis, one tool. Say so.
                resp = _chat_single_shot(req, _persist, session_tag)
                resp = _note_llm_unavailable(resp, session_tag)
                _enqueue({"type": "llm_unavailable",
                          "message": _LLM_UNAVAILABLE_NOTICE})
                _enqueue({"type": "done", "result": resp.model_dump()})
                return

            if _simple_pod_status_inventory_prompt(req.message):
                resp = _chat_single_shot(req, _persist, session_tag)
                _enqueue({"type": "done", "result": resp.model_dump()})
                return

            # ── Fast-Path Bypass ─────────────────────────────────────────
            k8s_prompt = _looks_like_kubernetes_prompt(req.message)
            live_k8s_prompt = _looks_like_live_kubernetes_prompt(req.message)
            static_kb_lookup = _looks_like_static_kb_lookup(req.message)
            try:
                # If the question is obviously conversational or general knowledge,
                # we bypass the ReAct loop entirely to provide an instant streaming response.
                if _should_protect_from_fast_path(req.message):
                    logger.info(
                        "Fast-path bypass protected for routed prompt: k8s=%s static_kb=%s prompt=%s",
                        k8s_prompt,
                        static_kb_lookup,
                        _prompt_preview(req.message),
                    )
                else:
                    bypass_prompt = (
                        f"Does this user input require running kubectl commands, checking a Kubernetes cluster, "
                        f"or DevOps troubleshooting? Reply exactly 'yes' or 'no'. Input: {req.message}"
                    )
                    is_k8s = provider.generate(bypass_prompt, max_tokens=10).strip().lower()
                    if "no" not in is_k8s:
                        raise ValueError("classified as Kubernetes/DevOps")
                    logger.info("Fast-path routing activated for: %s", _prompt_preview(req.message))
                    _enqueue({"type": "answer_start", "iteration": 0})
                    
                    from react import _build_finalize_prompt, _FINALIZE_SYSTEM
                    history_context = ""
                    if req.history:
                        recent = req.history[-4:]
                        history_context = "\n".join(
                            f"{getattr(m, 'role', 'user')}: {getattr(m, 'content', str(m))[:200]}"
                            for m in recent
                        )
                        history_context = f"\nRecent conversation:\n{history_context}\n"
                        
                    finalize_prompt = _build_finalize_prompt(req.message, history_context)
                    streamed_text = ""
                    for chunk in provider.generate_stream(finalize_prompt, system=_FINALIZE_SYSTEM, temperature=0.2, max_tokens=8000):
                        if cancelled.is_set():
                            logger.info("Fast-path stream cancelled by client connection drop")
                            break
                        if chunk:
                            streamed_text += chunk
                            _enqueue({"type": "token", "text": chunk})
                    _enqueue({"type": "answer_end", "iteration": 0, "fallback_used": False})
                    
                    _log_chat_turn(
                        session_tag=session_tag,
                        route="stream_fast_path",
                        message=req.message,
                        tool_used="none",
                        answer=streamed_text,
                    )
                    _persist("assistant", streamed_text, tool_used="none", result={}, error=None)
                    _enqueue({
                        "type": "done", 
                        "result": {
                            "reply": streamed_text, 
                            "tool_used": "none", 
                            "result": {}, 
                            "error": None, 
                            "timestamp": time.time(), 
                            "suggested_actions": [],
                            "session_id": sid,
                        }
                    })
                    return
            except Exception as e:
                logger.debug("Fast-path classification skipped: %s", e)

            # ── Phase 2.3: semantic prompt cache (L2) ────────────────────
            # Before the router or any LLM call, check if a similar
            # question was answered in the last N hours by anyone on the
            # team. Strict 0.95 similarity bar minimizes false-positives.
            # On hit: short-circuit ENTIRELY — no router, no ReAct, no
            # tools, no classifier — and return the cached resolution.
            # Best-effort: any failure (collection missing, embed fail,
            # vector DB down) silently misses and falls through.
            try:
                if live_k8s_prompt or static_kb_lookup:
                    _pc_hit, _pc_sim = None, 0.0
                    logger.info(
                        "prompt_cache skipped for routed prompt: live_k8s=%s static_kb=%s prompt=%s",
                        live_k8s_prompt,
                        static_kb_lookup,
                        _prompt_preview(req.message),
                    )
                else:
                    from services.rag.prompt_cache import (
                        lookup as _pc_lookup,
                        format_cached_answer as _pc_format,
                    )
                    _pc_hit, _pc_sim = _pc_lookup(req.message)
            except Exception as _pc_exc:
                logger.debug("prompt_cache skipped (%s): %s",
                             type(_pc_exc).__name__, _pc_exc)
                _pc_hit, _pc_sim = None, 0.0

            if _pc_hit is not None:
                _cached_text = _pc_format(_pc_hit)
                _pc_meta = {
                    "similarity": _pc_sim,
                    "original_question": _pc_hit.get("question"),
                    "original_user": _pc_hit.get("user"),
                    "original_timestamp": _pc_hit.get("timestamp"),
                }
                _log_chat_turn(
                    session_tag=session_tag,
                    route="prompt_cache",
                    message=req.message,
                    tool_used="prompt_cache",
                    answer=_cached_text,
                )
                _enqueue({
                    "type": "prompt_cache_hit",
                    "meta": _pc_meta,
                })
                _persist("assistant", _cached_text, tool_used="prompt_cache",
                         result={"prompt_cache_meta": _pc_meta})
                _enqueue({
                    "type": "done",
                    "result": {
                        "reply": _cached_text,
                        "tool_used": "prompt_cache",
                        "result": {"prompt_cache_meta": _pc_meta},
                        "error": None,
                        "timestamp": time.time(),
                        "suggested_actions": [],
                        "session_id": sid,
                    },
                })
                return

            # ── Phase 1.4: retrieval router ──────────────────────────────
            if _should_skip_rag_for_prompt(req.message):
                decision = None
                build_grounded_preamble = None
                logger.info("rag skipped for cluster-context prompt session=%s", session_tag)
            else:
                try:
                    from services.rag.router import route as _rag_route, build_grounded_preamble
                    decision = _rag_route(req.message)
                except Exception as exc:
                    logger.warning("router failed (continuing cold): %s", exc)
                    decision = None
                    build_grounded_preamble = None

            # Tell the UI immediately so it can render "found 3 relevant docs"
            # before the ReAct loop / streaming finalize even starts.
            if decision is not None and decision.mode != "cold":
                _enqueue({
                    "type": "kb_route",
                    "decision": decision.to_dict(),
                })

            # Cached short-circuit: persist + emit a single done event with
            # the runbook's answer. No ReAct, no token streaming — the
            # answer is canned and authoritative.
            if decision is not None and decision.mode == "cached" and not live_k8s_prompt:
                cached_text = decision.cached_answer or ""
                decision_dict = decision.to_dict()
                _log_chat_turn(
                    session_tag=session_tag,
                    route="stream_cached",
                    message=req.message,
                    tool_used="rag_cached",
                    rag_decision=decision_dict,
                    answer=cached_text,
                )
                _persist("assistant", cached_text, tool_used="rag_cached",
                         result={"rag_decision": decision_dict})
                _enqueue({
                    "type": "done",
                    "result": {
                        "reply": cached_text,
                        "tool_used": "rag_cached",
                        "result": {"rag_decision": decision_dict},
                        "error": None,
                        "timestamp": time.time(),
                        "suggested_actions": [],
                        "session_id": sid,
                    },
                })
                return

            if _should_answer_grounded_kb_directly(req.message, decision):
                answer = _format_grounded_kb_answer(req.message, decision)
                decision_dict = decision.to_dict()
                result_payload = {"rag_decision": decision_dict}
                _log_chat_turn(
                    session_tag=session_tag,
                    route="stream_grounded_direct",
                    message=req.message,
                    tool_used="rag_grounded",
                    rag_decision=decision_dict,
                    answer=answer,
                )
                _persist("assistant", answer, tool_used="rag_grounded", result=result_payload)
                _enqueue({
                    "type": "done",
                    "result": {
                        "reply": answer,
                        "tool_used": "rag_grounded",
                        "result": result_payload,
                        "error": None,
                        "timestamp": time.time(),
                        "suggested_actions": [],
                        "session_id": sid,
                        "eval_retrieval_context": [
                            str(c.get("content") or c.get("solution_text") or "")
                            for c in decision.grounded_chunks
                            if str(c.get("content") or c.get("solution_text") or "").strip()
                        ],
                    },
                })
                return

            grounded_preamble = ""
            if decision is not None and decision.mode == "grounded":
                grounded_preamble = build_grounded_preamble(decision)
            elif decision is not None and decision.mode == "cached" and live_k8s_prompt:
                grounded_preamble = _cached_decision_as_grounding(decision)

            from react import build_envelope_retrieval_context, react_loop
            from agent_run_recorder import AgentRunRecorder

            # Phase 7: deterministic tool scoping (feature-flagged).
            scope_decision = None
            tool_scope_set: Optional[set[str]] = None
            if os.environ.get("TOOL_SCOPING_ENABLED", "").lower() in ("1", "true", "yes"):
                try:
                    from tool_scoper import scope_for_prompt
                    from tool_registry import valid_tool_names
                    available = frozenset(valid_tool_names("react"))
                    scope_decision = scope_for_prompt(req.message, available_tools=available)
                    if not scope_decision.is_broad():
                        tool_scope_set = set(scope_decision.allowed_tools)
                        logger.info("tool_scoping session=%s scope=%s tools=%d",
                                    session_tag, scope_decision.scope_name, len(tool_scope_set))
                except Exception as exc:
                    logger.warning("tool_scoping failed (continuing unscoped): %s", exc)

            from react import REACT_SYSTEM_SHA, SYSTEM_PROMPT_SHA, TOOL_REGISTRY_SHA
            recorder = AgentRunRecorder.start(
                session_id=sid,
                user_id=user_id,
                route="stream_react",
                model=getattr(provider, "model", None) or req.model,
                rag_decision=decision.to_dict() if decision is not None else None,
                tool_scope=scope_decision.to_dict() if scope_decision is not None else None,
                system_prompt_sha=SYSTEM_PROMPT_SHA,
                react_system_sha=REACT_SYSTEM_SHA,
                tool_registry_sha=TOOL_REGISTRY_SHA,
            )
            if recorder is not None:
                run_id_holder["run_id"] = recorder.run_id
            result = react_loop(
                question=req.message,
                history=req.history,
                provider=provider,
                dispatch_fn=_make_memory_capturing_dispatch(sid),
                on_event=_enqueue,
                memory_preamble=memory.build_memory_preamble(sid),
                grounded_preamble=grounded_preamble,
                is_cancelled=cancelled.is_set,
                run_recorder=recorder,
                tool_scope=tool_scope_set,
            )

            # Persist + emit final done event (mirrors _chat_react).
            steps_meta = [
                {"thought": s.thought, "action": s.action,
                 "params": s.action_params, "duration_ms": round(s.duration_ms)}
                for s in result.steps
            ]
            persisted_result = {
                "react_steps": steps_meta,
                "tool_result": result.result,
                "synthesis_breakdown": getattr(result, "synthesis_breakdown", None),
            }

            cost_summary = None
            if os.environ.get("SHOW_COST_TO_USERS", "true").lower() in ("1", "true", "yes", "on"):
                cost_summary = {
                    "total_tokens_in": getattr(recorder, "total_tokens_in", 0),
                    "total_tokens_out": getattr(recorder, "total_tokens_out", 0),
                    "total_cached_tokens_in": getattr(recorder, "total_cached_tokens_in", 0),
                    "total_cost_usd": getattr(recorder, "total_cost_usd", 0.0),
                    "model": getattr(provider, "model", None) or req.model or "",
                }
                persisted_result["run_id"] = run_id_holder["run_id"]
                persisted_result["cost_summary"] = cost_summary

            eval_retrieval_context = build_envelope_retrieval_context(result.steps)
            response_result = result.result
            decision_dict = decision.to_dict() if decision is not None else None
            if decision is not None:
                persisted_result["rag_decision"] = decision_dict
                if decision.mode != "cold":
                    response_result = dict(result.result or {})
                    response_result["rag_decision"] = decision_dict

            # Phase 1.3 capture (best-effort).
            capture_id = _maybe_capture_chat(
                question=req.message, answer=result.answer,
                tool_used=result.tool_used, react_steps=steps_meta,
                session_id=sid,
            )
            if capture_id:
                persisted_result["capture_id"] = capture_id
                response_result = dict(response_result or {})
                response_result["capture_id"] = capture_id

            _log_react_trace(
                session_tag=session_tag,
                message=req.message,
                steps_meta=steps_meta,
                final_tool_used=result.tool_used,
                rag_decision=decision_dict,
            )
            _log_chat_turn(
                session_tag=session_tag,
                route="stream_react",
                message=req.message,
                tool_used=result.tool_used,
                capture_id=capture_id,
                rag_decision=decision_dict,
                error=result.error,
                answer=result.answer,
            )

            _persist("assistant", result.answer, tool_used=result.tool_used,
                     result=persisted_result, error=result.error)

            _enqueue({
                "type": "done",
                "result": {
                    "reply": result.answer,
                    "tool_used": result.tool_used,
                    "result": response_result,
                    "error": result.error,
                    "timestamp": time.time(),
                    "suggested_actions": result.suggested_actions,
                    "session_id": sid,
                    "run_id": run_id_holder["run_id"],
                    "synthesis_breakdown": getattr(result, "synthesis_breakdown", None),
                    "eval_retrieval_context": eval_retrieval_context,
                    "cost_summary": cost_summary,
                },
            })
        except Exception as e:
            logger.exception("chat_stream error")
            _enqueue({"type": "error", "message": f"Something went wrong: {e}"})
        finally:
            if ssh_runner is not None:
                ssh_runner.close()
            if ctx_token is not None:
                from k8s.kubectl_runner import runner_ctx
                runner_ctx.reset(ctx_token)
            # Sentinel so the SSE generator knows to close.
            loop.call_soon_threadsafe(queue.put_nowait, None)
            from opentelemetry.context import detach
            detach(token)

    async def _event_generator():
        from tracing import current_trace_id
        trace_id = current_trace_id()

        # Initial event so the client knows the stream is alive.
        yield _format_sse({"type": "start", "session": session_tag,
                           "timestamp": time.time(), "trace_id": trace_id})
        # Kick off the ReAct loop in a worker thread.
        worker_task = asyncio.create_task(asyncio.to_thread(_run_react_in_thread))
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield _format_sse(event)
        finally:
            # Set the cancelled event so the ReAct loop running in the thread aborts on its next step.
            cancelled.set()
            try:
                await worker_task  # surface any unhandled thread exception
            except Exception:
                logger.exception("chat_stream worker task raised after disconnect")

    headers = {
        "Cache-Control": "no-cache, no-transform",
        # X-Accel-Buffering: no disables nginx/proxy buffering so events
        # reach the client immediately rather than batching at the proxy.
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers=headers,
    )


# ── Execute endpoint ──────────────────────────────────────────────────────────

# Only these kubectl sub-commands are allowed via the execute endpoint.
_SAFE_KUBECTL_PREFIXES = [
    "kubectl patch ",
    "kubectl apply ",
    "kubectl scale ",
    "kubectl rollout restart ",
    "kubectl rollout undo ",
    "kubectl delete pod ",
    "kubectl delete pods ",
    "kubectl set image ",
    "kubectl set resources ",
    "kubectl label ",
    "kubectl annotate ",
    "kubectl cordon ",
    "kubectl uncordon ",
    "kubectl drain ",
]


@router.post("/execute", response_model=ExecuteResponse)
def execute_command(req: ExecuteRequest, request: Request):
    """Execute a kubectl command suggested by AI analysis.

    Safety guards:
    - Only kubectl commands are allowed (no shell injection).
    - Only specific kubectl sub-commands from a whitelist are permitted.
    - SSH credentials are supported for remote cluster execution.
    - Cluster connections (kubeconfig/context) are used when active.
    """
    import shlex
    import subprocess

    cmd = req.command.strip()
    logger.info("execute_request command=%s ssh=%s", cmd[:80], bool(req.ssh))
    if req.session_id:
        auth_utils.require_owned_session(request, req.session_id)

    # Safety check 1: Must start with "kubectl"
    if not cmd.startswith("kubectl"):
        return ExecuteResponse(success=False, error="Only kubectl commands are allowed.")

    # Safety check 2: Must match a safe prefix
    if not any(cmd.startswith(prefix) for prefix in _SAFE_KUBECTL_PREFIXES):
        return ExecuteResponse(
            success=False,
            error=f"Command not in allowed list. Allowed: {', '.join(p.strip() for p in _SAFE_KUBECTL_PREFIXES)}",
        )

    # Safety check 3: No shell metacharacters
    dangerous = set(";|&$`()")
    if any(c in cmd for c in dangerous):
        return ExecuteResponse(success=False, error="Command contains disallowed shell characters.")

    if cmd == "kubectl apply -f -" and not (req.stdin or "").strip():
        return ExecuteResponse(success=False, error="kubectl apply -f - requires a reviewed stdin manifest.")

    try:
        from react import _deterministic_review_recovery_action
        action_review = _deterministic_review_recovery_action(
            {"command": cmd, "stdin": req.stdin},
            {},
            require_evidence=False,
        )
    except Exception as exc:
        logger.warning("execute_action_validation_failed command=%s error=%s", cmd[:80], exc)
        return ExecuteResponse(success=False, error="Unable to validate recovery action safely.")
    if not action_review.get("approved"):
        reason = action_review.get("reason") or "not_approved"
        return ExecuteResponse(success=False, error=f"Recovery action rejected by safety validation: {reason}.")

    # Execute
    ssh_runner = None
    try:
        # ── SSH execution path ────────────────────────────────────────────
        if req.ssh:
            from k8s.ssh_runner import SSHKubectlRunner, SSHConnectionError
            try:
                ssh_runner = SSHKubectlRunner(
                    host=req.ssh.host,
                    username=req.ssh.username,
                    password=req.ssh.password,
                    port=req.ssh.port,
                )
                ssh_runner.connect()
                # Strip "kubectl " prefix — SSHKubectlRunner.run() prepends it
                args = shlex.split(cmd.replace("kubectl ", "", 1))
                result = ssh_runner.run(args, stdin_data=req.stdin)
                if result.success:
                    return ExecuteResponse(success=True, output=result.stdout.strip())
                else:
                    return ExecuteResponse(success=False, error=result.stderr.strip())
            except SSHConnectionError as e:
                return ExecuteResponse(success=False, error=f"SSH connection failed: {e}")

        # ── Build local command with cluster connection flags ──────────────
        # Parse the command into a list safely (no shell=True)
        cmd_parts = shlex.split(cmd)

        # If a session has a cluster connection, inject --kubeconfig / --context
        # after "kubectl" so the command targets the right cluster.
        if req.session_id:
            cluster_conn = db.get_cluster_connection(req.session_id)
            if cluster_conn:
                extra_flags = []
                kpath = cluster_conn.get("kubeconfig_path")
                ctx = cluster_conn.get("context_name")
                if kpath:
                    extra_flags.extend(["--kubeconfig", kpath])
                if ctx:
                    extra_flags.extend(["--context", ctx])
                if extra_flags:
                    # Insert flags right after "kubectl"
                    cmd_parts = [cmd_parts[0]] + extra_flags + cmd_parts[1:]

        result = subprocess.run(
            cmd_parts,
            input=req.stdin,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return ExecuteResponse(success=True, output=result.stdout.strip())
        else:
            return ExecuteResponse(success=False, error=result.stderr.strip() or f"Exit code {result.returncode}")

    except subprocess.TimeoutExpired:
        return ExecuteResponse(success=False, error="Command timed out after 30 seconds.")
    except Exception as e:
        logger.exception("Execute error")
        return ExecuteResponse(success=False, error=str(e))
    finally:
        if ssh_runner is not None:
            ssh_runner.close()


# ── Phase 3: Approval Endpoints ──────────────────────────────────────────────

class ApproveRequest(BaseModel):
    token: str
    ssh: Optional[SSHCredentials] = None


class RejectRequest(BaseModel):
    reason: Optional[str] = None


@router.post("/agent-runs/{run_id}/steps/{step_id}/approve")
async def approve_step(run_id: str, step_id: int, req: ApproveRequest, request: Request):
    """Approve a suspended agent step and resume the run's ReAct loop.

    Reads the prior run state, validates the token, marks the step approved,
    and streams the remaining ReAct iterations using SSE.
    """
    from fastapi import HTTPException
    from opentelemetry.context import get_current
    current_context = get_current()
    
    if not req.token:
        raise HTTPException(status_code=400, detail="Token is required")

    run_data = db.get_agent_run(run_id)
    if not run_data:
        raise HTTPException(status_code=404, detail="Agent run not found")
        
    sid = run_data.get("session_id")
    user_id = None
    is_owner = False
    is_admin = False
    if auth_utils.auth_enabled():
        user = auth_utils.require_current_user(request)
        user_id = user["id"]
        is_owner = (run_data.get("user_id") == user_id)
        is_admin = auth_utils.is_admin(user)
        if not (is_owner or is_admin):
            raise HTTPException(status_code=404, detail="Agent run not found")

    # Stale Run Freshness Check (7-day cap)
    started_at_str = run_data.get("started_at")
    if started_at_str:
        from datetime import datetime, timedelta, timezone
        try:
            started_at = datetime.fromisoformat(started_at_str)
        except ValueError:
            started_at = datetime.strptime(started_at_str.replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")
        if datetime.now(timezone.utc).replace(tzinfo=None) - started_at > timedelta(days=7):
            raise HTTPException(status_code=410, detail="Agent run is stale (older than 7 days)")

    # Emit warning log on admin approval overrides
    steps = db.get_agent_steps(run_id)
    step = next((s for s in steps if s["id"] == step_id), None)
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    pending_action = step.get("action", "")

    if auth_utils.auth_enabled() and not is_owner and is_admin:
        logger.warning(
            "admin_approval run_id=%s admin_user_id=%s owner_user_id=%s action=%s",
            run_id, user_id, run_data.get("user_id"), pending_action,
        )

    # State Transition & CAS (returns False if already running or not suspended)
    if not db.resume_agent_run(run_id):
        raise HTTPException(status_code=409, detail="Agent run is already running or not suspended")

    session_tag = _short_session_id(sid)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    import threading
    cancelled = threading.Event()

    def _enqueue(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def _persist(role: str, content: str, tool_used: str = None,
                 result: dict = None, error: str = None):
        if not sid:
            return
        try:
            db.save_message(sid, role, content, tool_used=tool_used,
                            result=result, error=error)
        except Exception as db_err:
            logger.warning(f"DB save failed: {db_err}")

    def _run_react_in_thread() -> None:
        from opentelemetry.context import attach
        token = attach(current_context)
        ssh_runner = None
        ctx_token = None
        try:
            # ── SSH runner setup (mirrors /chat) ─────────────────────────
            if req.ssh:
                from k8s.ssh_runner import SSHKubectlRunner
                from k8s.kubectl_runner import set_runner, runner_ctx

                try:
                    ssh_runner = SSHKubectlRunner(
                        host=req.ssh.host,
                        username=req.ssh.username,
                        password=req.ssh.password,
                        port=req.ssh.port,
                    )
                    ctx_token = runner_ctx.set(ssh_runner)
                    logger.info("SSH runner active for approval session=%s", session_tag)
                except Exception as exc:
                    logger.warning("Failed to establish SSH connection: %s", exc)
                    _enqueue({"type": "error", "message": f"SSH connection failed: {exc}"})
                    return

            # Retrieve provider
            from services.llm import get_provider
            model_name = run_data.get("model") or HARDCODED_GEMINI_MODEL
            provider = get_provider(model_name)

            # Retrieve tool scope
            tool_scope_set = None
            if run_data.get("tool_scope_json"):
                scope_dict = run_data["tool_scope_json"]
                if isinstance(scope_dict, dict) and "allowed_tools" in scope_dict:
                    tool_scope_set = set(scope_dict["allowed_tools"])

            # Retrieve grounded preamble
            grounded_preamble = ""
            if run_data.get("rag_decision_json"):
                from services.rag.router import RouteDecision, build_grounded_preamble
                d = run_data["rag_decision_json"]
                decision = RouteDecision(
                    mode=d.get("mode", "cold"),
                    citations=[],
                    cached_answer=d.get("cached_answer"),
                    grounded_chunks=d.get("grounded_chunks") or [],
                    top_score=d.get("top_score") or 0.0,
                    top_collection=d.get("top_collection") or "",
                    reason=d.get("reason") or "",
                    ansible_detected=d.get("ansible_detected") or False,
                )
                if decision.mode == "grounded":
                    grounded_preamble = build_grounded_preamble(decision)

            # Load history
            history_db = db.get_history(sid)
            # Find the original question (the last user message)
            user_msgs = [m for m in history_db if m["role"] == "user"]
            if not user_msgs:
                _enqueue({"type": "error", "message": "No user message found to resume from"})
                return
            question = user_msgs[-1]["content"]
            history = [ChatMessage(role=m["role"], content=m["content"]) for m in history_db[:-1]]

            from react import react_loop
            from agent_run_recorder import AgentRunRecorder

            # Reuse existing AgentRunRecorder
            recorder = AgentRunRecorder(run_id=run_id, user_id=user_id, session_id=sid)

            result = react_loop(
                question=question,
                history=history,
                provider=provider,
                dispatch_fn=_make_memory_capturing_dispatch(sid),
                on_event=_enqueue,
                memory_preamble=memory.build_memory_preamble(sid),
                grounded_preamble=grounded_preamble,
                is_cancelled=cancelled.is_set,
                run_recorder=recorder,
                tool_scope=tool_scope_set,
                resume_run_id=run_id,
                approved_token=req.token,
                approver_user_id=user_id,
            )

            if result.error == "PendingApproval":
                # Suspended again, do not finalize yet
                return

            # Otherwise, persist the final reply and complete the run
            steps_meta = [
                {"thought": s.thought, "action": s.action,
                 "params": s.action_params, "duration_ms": round(s.duration_ms)}
                for s in result.steps
            ]
            persisted_result = {
                "react_steps": steps_meta,
                "tool_result": result.result,
                "synthesis_breakdown": getattr(result, "synthesis_breakdown", None),
            }
            response_result = result.result
            decision_dict = run_data.get("rag_decision_json")
            if decision_dict:
                persisted_result["rag_decision"] = decision_dict
                if decision_dict.get("mode") != "cold":
                    response_result = dict(result.result or {})
                    response_result["rag_decision"] = decision_dict

            # Best-effort capture
            capture_id = _maybe_capture_chat(
                question=question, answer=result.answer,
                tool_used=result.tool_used, react_steps=steps_meta,
                session_id=sid,
            )
            if capture_id:
                persisted_result["capture_id"] = capture_id
                response_result = dict(response_result or {})
                response_result["capture_id"] = capture_id

            _log_react_trace(
                session_tag=session_tag,
                message=question,
                steps_meta=steps_meta,
                final_tool_used=result.tool_used,
                rag_decision=decision_dict,
            )
            _log_chat_turn(
                session_tag=session_tag,
                route="stream_react_resume",
                message=question,
                tool_used=result.tool_used,
                capture_id=capture_id,
                rag_decision=decision_dict,
                error=result.error,
                answer=result.answer,
            )

            _persist("assistant", result.answer, tool_used=result.tool_used,
                     result=persisted_result, error=result.error)

            _enqueue({
                "type": "done",
                "result": {
                    "reply": result.answer,
                    "tool_used": result.tool_used,
                    "result": response_result,
                    "error": result.error,
                    "timestamp": time.time(),
                    "suggested_actions": result.suggested_actions,
                    "session_id": sid,
                    "run_id": run_id,
                },
            })
        except Exception as e:
            logger.exception("approve_step stream error")
            _enqueue({"type": "error", "message": f"Something went wrong: {e}"})
        finally:
            if ssh_runner is not None:
                ssh_runner.close()
            if ctx_token is not None:
                from k8s.kubectl_runner import runner_ctx
                runner_ctx.reset(ctx_token)
            loop.call_soon_threadsafe(queue.put_nowait, None)
            from opentelemetry.context import detach
            detach(token)

    async def _event_generator():
        from tracing import current_trace_id
        trace_id = current_trace_id()

        yield _format_sse({"type": "start", "session": session_tag, "timestamp": time.time(), "trace_id": trace_id})
        worker_task = asyncio.create_task(asyncio.to_thread(_run_react_in_thread))
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield _format_sse(event)
        finally:
            cancelled.set()
            try:
                await worker_task
            except Exception:
                logger.exception("approve_step worker task raised after disconnect")

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers=headers,
    )


@router.post("/agent-runs/{run_id}/steps/{step_id}/reject")
def reject_step(run_id: str, step_id: int, req: RejectRequest, request: Request):
    """Reject a pending approval step. Marks step as rejected and fails the run."""
    from fastapi import HTTPException
    
    run_data = db.get_agent_run(run_id)
    if not run_data:
        raise HTTPException(status_code=404, detail="Agent run not found")
        
    sid = run_data.get("session_id")
    if auth_utils.auth_enabled():
        if sid:
            auth_utils.require_owned_session(request, sid)

    try:
        db.reject_agent_step(run_id, step_id)
        db.fail_agent_run(run_id, error="User rejected the operation.", status="aborted")
        return {"success": True, "message": "Run aborted by user."}
    except Exception as exc:
        logger.warning("Failed to reject step: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
