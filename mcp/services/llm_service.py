"""LLM service for K8s/Ansible error analysis and runbook generation."""

import json
import logging
import uuid
from typing import Any, Optional

from config.settings import get_settings
from services.llm import LLMProvider, get_provider
from services.llm.base import LLMProviderError

logger = logging.getLogger(__name__)
settings = get_settings()


def _provider_failure(action: str, exc: LLMProviderError, provider_name: str) -> str:
    """Report a provider failure without quoting the provider back to the caller.

    The strings these methods return are not error responses — they are the
    *content* the caller renders: a runbook body, an executive summary, the
    ``error`` field of a 200. Whatever goes here reaches whoever asked, so
    ``f"...: {e}"`` handed them the provider's own message.

    That message is not ours and we cannot bound it. ``gemini_provider`` raises
    ``LLMProviderError(str(exc))`` around the raw SDK exception, and the SDK
    builds those from a request URL that carries the API key as a query
    parameter; the Anthropic path wraps its exception whole too. Even at its
    most benign it names the endpoint, the model and the provider's internals
    to any authenticated user.

    So: an id, and nothing derived from the exception. Not even its type — the
    ``except`` clauses here are all ``LLMProviderError``, so the type would be a
    constant dressed up as information, and leaving it out means there is no
    expression at all for a reader to have to check.
    """
    error_id = uuid.uuid4().hex[:12]
    logger.error(
        "%s %s failed id=%s", provider_name, action, error_id, exc_info=exc
    )
    return (
        f"{action} failed (error id {error_id}). The provider's message is in "
        "the server log."
    )


SYSTEM_PROMPT = """You are a senior Site Reliability Engineer and Kubernetes/Ansible expert.

You help developers diagnose and fix issues in:
- Kubernetes clusters (pods, deployments, services, ingress, RBAC, storage, networking)
- Ansible automation (playbooks, roles, inventory, Helm chart deployments)

When analyzing errors, respond ONLY with valid JSON in this exact format:
{
    "root_cause": "One sentence explanation of why this is failing",
    "solution": "Clear explanation of how to fix it",
    "steps": ["Step 1: ...", "Step 2: ...", "Step 3: ..."],
    "commands": [
        {"cmd": "kubectl get pods -n <namespace>", "description": "What this does"}
    ],
    "prevention": "How to prevent this in the future",
    "severity": "critical|high|medium|low",
    "confidence": 0.95,
    "category": "<one known error category>",
    "corrected_snippet": "...",
    "corrected_file": "..."
}

Rules:
- All kubectl/ansible/helm commands must be copy-paste ready
- Replace actual secrets/passwords with <REDACTED> in commands
- Flag destructive operations with a WARNING prefix
- Confidence = your certainty this diagnosis is correct (0.0-1.0)
- Severity = impact on cluster health

corrected_snippet and corrected_file rules:
- If the user's input contains file content (YAML, JSON, Python, shell script, Ansible playbook,
  Helm values, Kubernetes manifests, or any config file) alongside the error, you MUST include:
    * "corrected_snippet": the specific fixed lines with 3-5 lines of surrounding context.
      Use the EXACT same indentation and format as the original file.
      Do NOT add any explanation text inside the snippet — only the corrected code.
    * "corrected_file": the COMPLETE corrected file content as it should be saved.
      Include every line from the original, with only the necessary fixes applied.
      Do NOT truncate or omit any sections. Do NOT add markdown fences or explanation.
- If no file content was provided in the input, omit both fields (or set them to null)."""


class LLMService:
    def __init__(self, provider: Optional[LLMProvider] = None):
        self._provider = provider or get_provider()

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def analyze(self, error_text: str, context: dict, similar: list[dict] = None) -> dict:
        """Analyze an error and return structured diagnosis with fix commands."""
        if not self._provider.enabled:
            return self._no_llm_response(context)

        similar_block = ""
        if similar:
            parts = [f"- {s['error_text'][:120]} → {s['solution_text'][:200]}" for s in similar[:3]]
            similar_block = "\nSimilar resolved issues:\n" + "\n".join(parts)

        request_evidence = context.get("request_evidence") or {}
        request_evidence_block = ""
        if request_evidence:
            request_evidence_block = (
                "\nCaller-supplied structured Kubernetes evidence "
                "(from the Ansible request payload; NOT live-cluster data):\n"
                f"{json.dumps(request_evidence, indent=2)[:8000]}\n"
            )

        evidence_boundary = ""
        system_prompt = SYSTEM_PROMPT
        if context.get("diagnostic_mode") == "error_only":
            evidence_boundary = """
EVIDENCE BOUNDARY — ERROR_ONLY MODE:
- You have NOT connected to or queried a live Kubernetes cluster.
- Base the diagnosis only on the caller-supplied error and structured request evidence below.
- Never claim or imply live observation. Do not say "I searched", "I checked the cluster",
  "not found in any namespace", "kubectl returned", "pod events show", or equivalent.
- Refer to structured fields as "the request payload shows", not as cluster observations.
- State uncertainty when the supplied payload does not prove the root cause.
"""
            system_prompt = SYSTEM_PROMPT + evidence_boundary

        prompt = f"""Analyze this Kubernetes/Ansible error:

Tool: {context.get('tool', 'kubernetes')}
Category detected: {context.get('category', 'unknown')}
{f"Pod: {context['pod']}" if 'pod' in context else ''}
{f"Namespace: {context['namespace']}" if 'namespace' in context else ''}
{f"Deployment: {context['deployment']}" if 'deployment' in context else ''}
{f"Node: {context['node']}" if 'node' in context else ''}
{f"Ansible task: {context['task']}" if 'task' in context else ''}
{f"Ansible host: {context['host']}" if 'host' in context else ''}
{similar_block}
{request_evidence_block}

Error:
```
{error_text[:6000]}
```

Respond ONLY with valid JSON."""

        try:
            text = self._provider.generate(
                prompt,
                system=system_prompt,
                temperature=0.2,
            )
            return self._parse(text)
        except LLMProviderError as e:
            logger.error("%s error: %s", self._provider.name, e)
            return self._no_llm_response(context)

    def analyze_live_investigation(self, pod_name: str, namespace: str,
                                   investigation_data: dict) -> dict:
        """Analyze live kubectl investigation data and provide AI diagnosis.
        
        This is called after investigate_pod gathers live cluster data.
        """
        if not self._provider.enabled:
            return {"ai_analysis": None, "ai_enabled": False,
                    "message": self._not_configured_message()}

        mode = investigation_data.get("classification", {}).get("mode", "unknown")

        # Prefer pre-computed summaries when the summarizer is enabled —
        # they're tighter and already deduped. Fall back to raw + truncation.
        describe_block = investigation_data.get("describe", {})
        describe_raw = (
            describe_block.get("describe_summary")
            or describe_block.get("raw_output", "")[:3000]
        )

        logs = ""
        logs_current = investigation_data.get("logs_current") or {}
        logs_previous = investigation_data.get("logs_previous") or {}
        if logs_current:
            logs = logs_current.get("logs_summary") or logs_current.get("logs", "")[:2000]
        if not logs and logs_previous:
            logs = logs_previous.get("logs_summary") or logs_previous.get("logs", "")[:2000]

        events_block = investigation_data.get("events", {}) or {}
        events_summary_text = events_block.get("events_summary")
        if events_summary_text:
            events_text = events_summary_text
        else:
            events = events_block.get("events", [])
            events_text = "\n".join([
                f"[{e.get('type','')}] {e.get('reason','')} - {e.get('message','')}"
                for e in events[:20]
            ])
        evidence_summary = investigation_data.get("evidence_summary") or {}
        deterministic_evidence = json.dumps(evidence_summary, indent=2) if evidence_summary else "None"
        container_log_findings = investigation_data.get("container_log_findings") or []
        container_findings_text = (
            json.dumps(container_log_findings, indent=2) if container_log_findings else "None"
        )

        prompt = f"""You are investigating a Kubernetes pod failure. Here is the live cluster data:

Pod: {pod_name}
Namespace: {namespace}
Failure mode detected: {mode}

--- kubectl describe pod (truncated) ---
{describe_raw}

--- Pod logs (truncated) ---
{logs if logs else "No logs available"}

--- Per-container status/log findings ---
{container_findings_text}

--- Deterministic dependency/evidence checks ---
{deterministic_evidence}

--- Recent events ---
{events_text if events_text else "No events"}

Based on this LIVE cluster data, provide:
1. Root cause diagnosis
2. Step-by-step fix commands (use actual pod name and namespace)
3. Prevention recommendations

Respond ONLY with valid JSON matching this schema:
{{
    "root_cause": "...",
    "solution": "...",
    "steps": ["..."],
    "commands": [{{"cmd": "...", "description": "..."}}],
    "prevention": "...",
    "severity": "critical|high|medium|low",
    "confidence": 0.9,
    "category": "..."
}}"""

        prompt += """

Important evidence rules:
- Prefer deterministic dependency checks and explicit failing log checkpoints over suspicious-looking configuration values.
- If the logs stop at a dependency health check and the dependency check says the service/endpoints are missing, make that the root cause.
- If logs say "Multiple plugin prerequisites not met" or "older version defined on the top level", diagnose application/plugin dependency version mismatch, not timeout, infinite loop, or resource exhaustion.
- Mention suspicious secondary issues only as follow-up items unless logs/events directly prove they caused the exit.
"""

        try:
            text = self._provider.generate(prompt, system=SYSTEM_PROMPT, temperature=0.2)
            parsed = self._parse(text)
            return {"ai_analysis": parsed, "ai_enabled": True}
        except LLMProviderError as e:
            return {"ai_analysis": None, "ai_enabled": True,
                    "error": _provider_failure("AI analysis", e, self._provider.name)}

    def analyze_workload_investigation(
        self,
        workload_name: str,
        namespace: str,
        investigation_data: dict[str, Any],
    ) -> dict:
        """Analyze workload-level investigation data and return structured AI output."""
        if not self._provider.enabled:
            return {
                "ai_analysis": None,
                "ai_enabled": False,
                "message": self._not_configured_message(),
            }

        workload_type = investigation_data.get("workload_type", "deployment")
        describe_block = investigation_data.get("describe", "")
        if isinstance(describe_block, dict):
            describe_raw = (
                describe_block.get("describe_summary")
                or str(describe_block.get("raw_output", ""))[:5000]
            )
        else:
            describe_raw = str(describe_block)[:5000]
        pods = investigation_data.get("pods", [])
        pod_summaries = []
        for pod in pods[:10]:
            metadata = pod.get("metadata", {})
            status = pod.get("status", {})
            pod_summaries.append(
                {
                    "name": metadata.get("name", ""),
                    "phase": status.get("phase", "Unknown"),
                    "restarts": sum(
                        cs.get("restartCount", 0)
                        for cs in status.get("containerStatuses", [])
                    ),
                }
            )

        events_block = investigation_data.get("events", {}) or {}
        events_summary_text = events_block.get("events_summary")
        if events_summary_text:
            event_lines = [events_summary_text]
        else:
            events = events_block.get("items", []) or events_block.get("events", [])
            event_lines = [
                f"[{e.get('type', '')}] {e.get('reason', '')} - {e.get('message', '')}"
                for e in events[:20]
            ]

        prompt = f"""You are investigating a Kubernetes workload issue from live cluster data.

Workload type: {workload_type}
Workload name: {workload_name}
Namespace: {namespace}

--- Workload definition (truncated) ---
{describe_raw or "No workload definition available"}

--- Related pod summary ---
{json.dumps(pod_summaries, indent=2) if pod_summaries else "No related pods found"}

--- Recent related events ---
{chr(10).join(event_lines) if event_lines else "No related events found"}

Provide:
1. Most likely root cause
2. Fix steps
3. Copy-paste kubectl commands
4. Prevention guidance

Respond ONLY with valid JSON matching this schema:
{{
  "root_cause": "...",
  "solution": "...",
  "steps": ["..."],
  "commands": [{{"cmd": "...", "description": "..."}}],
  "prevention": "...",
  "severity": "critical|high|medium|low",
  "confidence": 0.9,
  "category": "..."
}}"""

        try:
            text = self._provider.generate(prompt, system=SYSTEM_PROMPT, temperature=0.2)
            parsed = self._parse(text)
            return {"ai_analysis": parsed, "ai_enabled": True}
        except LLMProviderError as e:
            return {
                "ai_analysis": None,
                "ai_enabled": True,
                "error": _provider_failure("AI analysis", e, self._provider.name),
            }

    def analyze_namespace_health(
        self,
        namespace: str,
        resources: dict[str, Any],
        events: dict[str, Any],
    ) -> dict:
        """Analyze namespace-wide health from aggregate resources and warning events."""
        if not self._provider.enabled:
            return {
                "ai_analysis": None,
                "ai_enabled": False,
                "message": self._not_configured_message(),
            }

        summary = resources.get("summary", {})
        events_summary_text = events.get("events_summary") if isinstance(events, dict) else None
        if events_summary_text:
            event_lines = [events_summary_text]
        else:
            warning_events = events.get("events", []) if isinstance(events, dict) else []
            event_lines = [
                f"[{e.get('type', '')}] {e.get('reason', '')} - {e.get('message', '')}"
                for e in warning_events[:25]
            ]

        prompt = f"""You are analyzing the overall health of a Kubernetes namespace.

Namespace: {namespace}

--- Resource summary ---
{json.dumps(summary, indent=2)}

--- Warning events ---
{chr(10).join(event_lines) if event_lines else "No warning events found"}

Determine whether the namespace appears healthy, whether there are systemic or cascading issues,
and what the top remediation priorities should be.

Respond ONLY with valid JSON matching this schema:
{{
  "root_cause": "...",
  "solution": "...",
  "steps": ["..."],
  "commands": [{{"cmd": "...", "description": "..."}}],
  "prevention": "...",
  "severity": "critical|high|medium|low",
  "confidence": 0.9,
  "category": "..."
}}"""

        try:
            text = self._provider.generate(prompt, system=SYSTEM_PROMPT, temperature=0.2)
            parsed = self._parse(text)
            return {"ai_analysis": parsed, "ai_enabled": True}
        except LLMProviderError as e:
            return {
                "ai_analysis": None,
                "ai_enabled": True,
                "error": _provider_failure("AI analysis", e, self._provider.name),
            }

    def summarize_cluster_issues(self, issues: list[dict]) -> str:
        """Summarize multiple cluster issues into an executive report."""
        if not self._provider.enabled:
            return self._not_configured_message()

        prompt = f"""Summarize these Kubernetes/Ansible issues for a DevOps report:

{json.dumps(issues, indent=2)}

Provide:
1. Executive summary (2-3 sentences)
2. Critical issues requiring immediate attention
3. Recurring patterns you notice
4. Top 3 recommended actions

Keep it concise and actionable."""

        try:
            return self._provider.generate(prompt, temperature=0.3)
        except LLMProviderError as e:
            return _provider_failure("Summary generation", e, self._provider.name)

    def generate_runbook(self, error_category: str, examples: list[str]) -> str:
        """Generate a markdown runbook for a recurring error category."""
        if not self._provider.enabled:
            return self._not_configured_message()

        prompt = f"""Generate a runbook for handling '{error_category}' errors in Kubernetes/Ansible.

Example occurrences:
{chr(10).join(f'- {e[:200]}' for e in examples[:5])}

Format the runbook as:
## Overview
## Symptoms
## Diagnosis Steps (with kubectl/ansible commands)
## Fix Procedures
## Prevention
## Escalation Path"""

        try:
            return self._provider.generate(prompt, temperature=0.2)
        except LLMProviderError as e:
            return _provider_failure("Runbook generation", e, self._provider.name)

    def _parse(self, text: str) -> dict:
        cleaned = (text or "").strip()
        if "```" in cleaned:
            for part in cleaned.split("```"):
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    cleaned = part
                    break
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {
                "root_cause": "Could not parse LLM response",
                "solution": text,
                "steps": [],
                "commands": [],
                "prevention": "",
                "severity": "unknown",
                "confidence": 0.3,
                "category": "unknown",
            }

    def _not_configured_message(self) -> str:
        if self._provider.name == "ollama":
            return (
                "Ollama is not reachable. Set OLLAMA_BASE_URL and OLLAMA_MODEL, "
                "and ensure the Ollama server is running."
            )
        return "LLM not configured. Add GEMINI_API_KEY to .env or set LLM_PROVIDER=ollama."

    def _no_llm_response(self, context: dict) -> dict:
        return {
            "root_cause": f"LLM not configured. Error category: {context.get('category', 'unknown')}",
            "solution": self._not_configured_message(),
            "steps": [
                "1. Copy .env.example to .env",
                "2. Set LLM_PROVIDER=gemini (default) or LLM_PROVIDER=ollama",
                "3. For Gemini: add GEMINI_API_KEY. For Ollama: ensure the server is running.",
                "4. Restart the MCP server",
            ],
            "commands": [],
            "prevention": "",
            "severity": "unknown",
            "confidence": 0.0,
            "category": context.get("category", "unknown"),
        }


llm_service = LLMService()
