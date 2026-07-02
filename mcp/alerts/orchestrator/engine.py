from __future__ import annotations

import asyncio
import re
import json
import os
import uuid
import logging
from typing import Any

from alerts.domain.actions import InvestigationAction, ToolExecutionRecord
from alerts.domain.alert import Alert
from alerts.domain.alert_normalizer import normalize_alert_labels
from alerts.domain.enums import InvestigationStatus
from alerts.domain.investigation import Investigation, LlmInteraction, Evidence
from alerts.domain.rca import Finding, RootCauseAnalysis
from alerts.domain.semantic import SemanticIncidentRecord
from alerts.playbooks.classifier import AlertClassifier
from alerts.playbooks.decision_engine import evaluate_decision_rules, rank_steps_by_decision_guidance
from alerts.playbooks.rca_scoring import evaluate_rca_scoring
from alerts.playbooks.registry import PlaybookRegistry
from alerts.repositories.base import InvestigationRepository, SemanticMemoryRepository
from alerts.shared.metrics import metrics
from alerts.notifications.dispatcher import NotificationDispatcher
from alerts.orchestrator.rca_context import build_rca_context

from tool_registry import DispatchContext, dispatch
from fastapi.concurrency import run_in_threadpool
from services.llm import get_provider
from pydantic import BaseModel

logger = logging.getLogger(__name__)

class LlmRecommendation(BaseModel):
    tool: str | None = None
    args: dict[str, Any] = {}
    reason: str
    stop: bool = False
    candidate_step: Any | None = None
    capability_gap: Any | None = None

class LlmAnalysis(BaseModel):
    findings: list[str]
    confidence: float
    root_causes: list[str]
    recommendations: list[str]
    supporting_evidence: list[str]
    what_was_ruled_out: list[str]
    next_checks: list[str]

# Concurrency cap on LLM calls — one semaphore per running event loop.
#
# Why a per-loop registry instead of a single module-level Semaphore: we
# want ONE shared cap across every in-flight investigation (the orchestrator
# is instantiated per webhook alert in alerts.py, so per-instance semaphores
# would lose the cap). But asyncio.Semaphore lazily binds to whichever loop
# first awaits acquire() — fine in production (single Uvicorn loop) but
# fragile under pytest-asyncio, where each test gets its own loop and a
# leftover waiter from a previous test would raise
# "Semaphore is bound to a different event loop".
#
# Solution: keyed by the currently-running loop. Production has one loop
# for the lifetime of the process, so this collapses to a single shared
# semaphore — preserving the global cap. Tests get a fresh semaphore per
# loop, so there's no cross-loop binding.
#
# Why the Gemini SDK's generate() needs to be off-loop AND throttled:
# the SDK is synchronous, so calling it inside an `async def` parks the
# entire FastAPI event loop until the model responds — the liveness probe
# stops answering and k8s restarts the pod mid-investigation. run_in_threadpool
# fixes the parking; the semaphore prevents a 35-alert burst from
# saturating both the default 40-thread pool AND the per-minute model
# quota (gemini-3.1-flash-lite is rate-limited per project, not per
# request).
_LLM_CONCURRENCY_LIMIT = int(os.environ.get("ALERTS_LLM_CONCURRENCY", "8"))
_llm_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _get_llm_semaphore() -> asyncio.Semaphore:
    """Return the LLM concurrency semaphore for the currently-running loop.
    Lazily creates one on first use per loop. Must be called from inside a
    running coroutine — there is no sensible behavior when no loop is set."""
    loop = asyncio.get_running_loop()
    sem = _llm_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_LLM_CONCURRENCY_LIMIT)
        _llm_semaphores[loop] = sem
    return sem


class AlertsLlmClient:
    def __init__(self):
        self.provider = get_provider()

    async def _generate(self, prompt: str) -> str:
        """Call the LLM provider off the event loop, bounded by the shared
        per-loop concurrency cap. See _get_llm_semaphore for rationale."""
        async with _get_llm_semaphore():
            return await run_in_threadpool(
                self.provider.generate, prompt, temperature=0.2
            )

    async def recommend_next_step(self, payload: dict) -> LlmRecommendation:
        prompt = f"""You are a Kubernetes SRE deciding the next diagnostic tool to run.

Context:
{json.dumps(payload, indent=2)}

Respond ONLY with valid JSON matching this schema:
{{
  "tool": "tool_name_from_allowed_tools",
  "args": {{"arg_name": "arg_value"}},
  "reason": "Why you are running this tool",
  "stop": false
}}
Set "stop" to true if you have enough evidence to determine root cause, or if no tools can help.
"""
        try:
            text = await self._generate(prompt)
            text = text.replace("```json", "").replace("```", "").strip()
            data = json.loads(text)
            return LlmRecommendation(**data)
        except Exception as e:
            logger.error(f"LLM recommend_next_step error: {e}")
            return LlmRecommendation(stop=True, reason=f"LLM failure: {e}")

    async def analyze_evidence(self, context: dict) -> LlmAnalysis:
        prompt = f"""You are a Kubernetes SRE analyzing evidence from an investigation.

Context:
{json.dumps(context, indent=2)}

Respond ONLY with valid JSON matching this schema:
{{
  "findings": ["finding 1", "finding 2"],
  "confidence": 0.9,
  "root_causes": ["root cause 1"],
  "recommendations": ["fix step 1"],
  "supporting_evidence": ["evidence reference"],
  "what_was_ruled_out": ["not a network issue"],
  "next_checks": []
}}
"""
        try:
            text = await self._generate(prompt)
            text = text.replace("```json", "").replace("```", "").strip()
            data = json.loads(text)
            return LlmAnalysis(**data)
        except Exception as e:
            logger.error(f"LLM analyze_evidence error: {e}")
            return LlmAnalysis(
                findings=[f"Failed to analyze: {e}"], confidence=0.0, root_causes=[],
                recommendations=[], supporting_evidence=[], what_was_ruled_out=[], next_checks=[]
            )


class InvestigationOrchestrator:
    def __init__(
        self,
        *,
        playbooks: PlaybookRegistry,
        classifier: AlertClassifier,
        investigations: InvestigationRepository,
        semantic_memory: SemanticMemoryRepository,
        notifications: NotificationDispatcher,
    ) -> None:
        self.playbooks = playbooks
        self.classifier = classifier
        self.investigations = investigations
        self.semantic_memory = semantic_memory
        self.notifications = notifications
        self.llm = AlertsLlmClient()

    async def investigate(self, alert: Alert, investigation_id: str | None = None) -> Investigation:
        metrics.increment("investigations_started_total")
        alert = normalize_alert_labels(alert)
        # If the caller (e.g. the webhook or /manual router) already persisted
        # an Investigation row with an id and possibly some audit events
        # (e.g. a `manual_trigger` event identifying the user), continue that
        # record instead of overwriting it with a fresh one.
        existing: Investigation | None = None
        if investigation_id:
            try:
                existing = await self.investigations.get(investigation_id)
            except Exception:
                existing = None
        if existing is not None:
            investigation = existing
            investigation.alert = alert
        else:
            investigation = Investigation(alert=alert)
            if investigation_id:
                investigation.investigation_id = investigation_id
        investigation.append_audit("alert_received", {"fingerprint": alert.fingerprint})

        classification = self.classifier.classify(alert)
        investigation.classification = classification
        investigation.selected_playbook = classification.playbook_id
        investigation.status = InvestigationStatus.CLASSIFIED
        investigation.append_audit("alert_classified", classification.model_dump(mode="json"))

        playbook = self.playbooks.get(classification.playbook_id or "generic")
        investigation.status = InvestigationStatus.RUNNING
        investigation.append_audit(
            "playbook_loaded",
            {
                "playbook_id": playbook.id,
                "validation_warnings": playbook.validation_warnings,
            },
        )
        if playbook.validation_warnings:
            investigation.context["playbook_validation_warnings"] = playbook.validation_warnings
        await self.investigations.save(investigation)

        rendered_steps = []
        for step in playbook.steps:
            args, missing = self._render_args(step.args, alert)
            if missing:
                # A required template variable (e.g. {{ alert.labels.pod }})
                # could not be resolved from the alert. Skip the step rather
                # than dispatching the tool with empty substitutions — which
                # would silently produce useless evidence (e.g. PromQL
                # `pod=''` returning no series, kubectl `--pod ''` erroring)
                # and let the LLM reach a "no signal" conclusion on data
                # that was never queried.
                investigation.append_audit(
                    "step_skipped_missing_label",
                    {"step_id": step.id, "tool": step.tool, "missing": missing},
                )
                continue
            rendered_steps.append({
                "id": step.id,
                "tool": step.tool,
                "args": args,
                "deterministic": step.deterministic,
            })

        # Pre-LLM: run any step marked `deterministic: true` in playbook order.
        # The original orchestrator dumped the whole tool list to the LLM and
        # asked it to pick — but the LLM has no signal that composite tools
        # like investigate_pod auto-select the failing container (vs. raw
        # get_pod_logs that defaults to the first container). On a Jenkins
        # pod where init crashes but main hasn't started, that defaulting
        # produces empty logs and an "unable to determine root cause" RCA.
        # Running playbook-author-marked steps deterministically up front
        # guarantees the rich evidence is always collected before the LLM
        # gets to deviate.
        for step in rendered_steps:
            if not step["deterministic"]:
                continue
            if len(investigation.executed_tools) >= playbook.safety.max_steps:
                break
            action = InvestigationAction(
                tool=step["tool"],
                args=step["args"],
                reason=f"Deterministic playbook step: {step['id']}",
                source="playbook_deterministic",
            )
            investigation.append_audit(
                "action_validated",
                {
                    "action": action.model_dump(mode="json"),
                    "allowed": True,
                    "reason": "deterministic playbook step",
                },
            )
            evidence = await self._execute_action(action)
            metrics.increment("tool_execution_total")
            investigation.evidence.append(evidence)
            investigation.executed_tools.append(
                ToolExecutionRecord(
                    tool=action.tool,
                    arguments_hash=action.arguments_hash,
                    reason=action.reason,
                    args=action.args,
                )
            )
            investigation.append_audit(
                "tool_executed",
                {
                    "tool": action.tool,
                    "evidence_id": evidence.evidence_id,
                    "summary": evidence.summary,
                    "step_id": step["id"],
                },
            )
            await self.investigations.save(investigation)

        while len(investigation.executed_tools) < playbook.safety.max_steps:
            remaining = self._remaining_steps(rendered_steps, investigation)
            evidence_context = build_rca_context(investigation)
            decision_guidance = evaluate_decision_rules(
                playbook=playbook,
                alert=alert,
                classification=classification,
                evidence=investigation.evidence,
            )
            remaining = rank_steps_by_decision_guidance(remaining, decision_guidance)
            recommendation = await self.llm.recommend_next_step(
                {
                    "alert": alert.model_dump(mode="json"),
                    "selected_playbook": playbook.id,
                    "remaining_steps": remaining,
                    "allowed_tools": playbook.allowed_tools,
                    "evidence_summaries": [e.summary for e in investigation.evidence],
                    "evidence_details": evidence_context["evidence_details"],
                    "detected_signals": evidence_context["detected_signals"],
                    "decision_guidance": decision_guidance,
                    "playbook_validation_warnings": playbook.validation_warnings,
                    "execution_policy": playbook.execution_policy.model_dump(mode="json"),
                }
            )
            investigation.llm_interactions.append(
                LlmInteraction(
                    prompt_version="recommend_next_step.v1",
                    request={"remaining_steps": remaining},
                    response=recommendation.model_dump(mode="json"),
                )
            )
            if recommendation.stop or not recommendation.tool:
                investigation.append_audit(
                    "llm_stop_recommended", recommendation.model_dump(mode="json")
                )
                break
            if recommendation.tool not in playbook.allowed_tools:
                investigation.append_audit(
                    "action_rejected",
                    {
                        "tool": recommendation.tool,
                        "reason": "tool_not_allowed_by_playbook",
                        "allowed_tools": playbook.allowed_tools,
                    },
                )
                break

            action = InvestigationAction(
                tool=recommendation.tool,
                args=recommendation.args,
                reason=recommendation.reason,
                source="llm_advisory",
            )
            
            investigation.append_audit(
                "action_validated",
                {
                    "action": action.model_dump(mode="json"),
                    "allowed": True,
                    "reason": "playbook tool",
                },
            )

            # Execute via MCP tool registry wrapper
            evidence = await self._execute_action(action)
            metrics.increment("tool_execution_total")
            investigation.evidence.append(evidence)
            investigation.executed_tools.append(
                ToolExecutionRecord(
                    tool=action.tool,
                    arguments_hash=action.arguments_hash,
                    reason=action.reason,
                    args=action.args,
                )
            )
            investigation.append_audit(
                "tool_executed",
                {
                    "tool": action.tool,
                    "evidence_id": evidence.evidence_id,
                    "summary": evidence.summary,
                },
            )
            await self.investigations.save(investigation)

            if self._has_sufficient_evidence(investigation):
                break

        final_rca_context = build_rca_context(investigation)

        # Phase 4b: recall similar past investigations from semantic memory and
        # inject them into the RCA prompt so the LLM can reuse prior diagnoses.
        # Fail-soft: a recall failure must not abort the investigation.
        similar = await self._recall_similar_incidents(investigation)
        if similar:
            final_rca_context["similar_past_incidents"] = [
                {
                    "investigation_id": r.investigation_id,
                    "alert_name": r.alert_name,
                    "category": r.category,
                    "severity": r.severity,
                    "namespace": r.namespace,
                    "workload": r.workload,
                    "rca_summary": r.rca_summary,
                    "root_causes": r.root_causes,
                    "recommendations": r.recommendations,
                }
                for r in similar
            ]
            investigation.append_audit(
                "similar_incidents_recalled",
                {"count": len(similar), "investigation_ids": [r.investigation_id for r in similar]},
            )
            metrics.increment("incident_memory_recall_total")

        evidence_ids = [e.evidence_id for e in investigation.evidence]
        try:
            rca_scoring = evaluate_rca_scoring(
                playbook=playbook,
                alert=alert,
                classification=classification,
                evidence=investigation.evidence,
                detected_signals=final_rca_context["detected_signals"],
            )
        except Exception as exc:
            rca_scoring = None
            investigation.append_audit("rca_scoring_skipped", {"reason": str(exc)})

        deterministic_rca = self._deterministic_rca_from_evidence(
            investigation,
            evidence_ids=evidence_ids,
            rca_scoring=rca_scoring,
        )
        if deterministic_rca:
            investigation.findings.append(
                Finding(
                    title="Deterministic pod investigation analysis",
                    detail=deterministic_rca.summary,
                    confidence=deterministic_rca.confidence,
                    evidence_ids=evidence_ids,
                )
            )
            investigation.rca = deterministic_rca
            investigation.append_audit(
                "deterministic_rca_promoted",
                {
                    "source_tool": "investigate_pod",
                    "confidence": deterministic_rca.confidence,
                    "summary": deterministic_rca.summary,
                },
            )
        else:
            analysis = await self.llm.analyze_evidence(final_rca_context)
            investigation.findings.append(
                Finding(
                    title="Investigation evidence analysis",
                    detail="; ".join(analysis.findings),
                    confidence=analysis.confidence,
                    evidence_ids=evidence_ids,
                )
            )
            investigation.rca = RootCauseAnalysis(
                summary=analysis.root_causes[0]
                if analysis.root_causes
                else "No definitive root cause identified",
                confidence=analysis.confidence,
                root_causes=analysis.root_causes,
                recommendations=analysis.recommendations,
                supporting_evidence=analysis.supporting_evidence,
                what_was_ruled_out=analysis.what_was_ruled_out,
                next_checks=analysis.next_checks,
                evidence_ids=evidence_ids,
                rca_scoring=rca_scoring,
            )
        investigation.status = InvestigationStatus.COMPLETED
        metrics.increment("investigations_completed_total")
        investigation.append_audit("rca_generated", investigation.rca.model_dump(mode="json"))
        await self.investigations.save(investigation)

        try:
            await self.semantic_memory.store(self._to_semantic_record(investigation))
        except Exception as e:
            logger.error(f"Failed to store semantic memory: {e}")
            
        try:
            await self.notifications.send_summary(investigation)
        except Exception as e:
            logger.error(f"Failed to send notifications: {e}")
            
        return investigation

    async def _execute_action(self, action: InvestigationAction) -> Evidence:
        ctx = DispatchContext(surface="playbook", allow_write=False)
        try:
            output = await run_in_threadpool(dispatch, action.tool, action.args, ctx)
        except Exception as e:
            output = {"error": f"Error executing {action.tool}: {e}"}
            
        from alerts.domain.evidence import Evidence
        raw = output if isinstance(output, dict) else {"output": output}
        summary_text = self._summarize_tool_output(raw)
        return Evidence(
            evidence_id=str(uuid.uuid4()),
            evidence_type=self._evidence_type_for_tool(action.tool),
            tool=action.tool,
            summary=summary_text,
            raw=raw,
        )

    def _evidence_type_for_tool(self, tool: str) -> str:
        if tool == "prom_query":
            return "prometheus"
        if tool in {"get_logs", "logql_query"}:
            return "loki"
        return "kubernetes"

    def _summarize_tool_output(self, raw: dict[str, Any]) -> str:
        summary_len = 500
        for key in (
            "summary",
            "events_summary",
            "logs_summary",
            "describe_summary",
            "error",
            "message",
        ):
            value = raw.get(key)
            if value:
                text = str(value)
                return text[:summary_len] + ("..." if len(text) > summary_len else "")
        text = json.dumps(raw, default=str)
        return text[:summary_len] + ("..." if len(text) > summary_len else "")

    def _deterministic_rca_from_evidence(
        self,
        investigation: Investigation,
        *,
        evidence_ids: list[str],
        rca_scoring: dict[str, Any] | None,
    ) -> RootCauseAnalysis | None:
        """Promote verified tool-level diagnoses before generic LLM synthesis.

        The chat path already treats investigate_pod's deterministic evidence as
        authoritative. Alert RCA needs the same behavior so strong pod findings
        are not reduced to generic guesses by the final LLM pass.
        """
        for evidence in investigation.evidence:
            if evidence.tool != "investigate_pod":
                continue
            raw = self._raw_evidence_payload(evidence)
            if not raw:
                continue

            evidence_summary = raw.get("evidence_summary")
            evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
            root_candidate = self._verified_root_candidate(raw)
            root_cause = str(evidence_summary.get("suspected_root_cause") or "").strip()
            if not root_cause and root_candidate:
                root_cause = str(
                    root_candidate.get("root_cause")
                    or root_candidate.get("summary")
                    or ""
                ).strip()
            ai_analysis = self._nested_dict(raw, "ai", "ai_analysis")
            if not root_cause and ai_analysis:
                root_cause = str(ai_analysis.get("root_cause") or "").strip()
            if not root_cause:
                continue

            supporting_evidence = self._pod_supporting_evidence(raw, evidence)
            recommendations = self._pod_recommendations(evidence_summary, ai_analysis, root_candidate)
            next_checks = self._pod_next_checks(ai_analysis)
            confidence = 0.95 if evidence_summary.get("suspected_root_cause") or root_candidate else 0.8
            if ai_analysis and isinstance(ai_analysis.get("confidence"), (int, float)):
                confidence = max(confidence, min(float(ai_analysis["confidence"]), 1.0))

            return RootCauseAnalysis(
                summary=root_cause,
                confidence=confidence,
                root_causes=[root_cause],
                recommendations=recommendations,
                supporting_evidence=supporting_evidence,
                what_was_ruled_out=self._pod_ruled_out(raw),
                next_checks=next_checks,
                evidence_ids=evidence_ids,
                rca_scoring=rca_scoring,
            )
        return None

    def _raw_evidence_payload(self, evidence: Evidence) -> dict[str, Any]:
        raw = evidence.raw if isinstance(evidence.raw, dict) else {}
        output = raw.get("output")
        if isinstance(output, dict):
            return output
        if isinstance(output, str):
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                return raw
            if isinstance(parsed, dict):
                return parsed
        return raw

    def _pod_supporting_evidence(self, raw: dict[str, Any], evidence: Evidence) -> list[str]:
        items: list[str] = []
        classification = raw.get("classification")
        if isinstance(classification, dict):
            mode = classification.get("mode") or classification.get("reason")
            container = classification.get("container")
            message = classification.get("message")
            if mode or container or message:
                items.append(
                    "Pod classification: "
                    + ", ".join(
                        part for part in [
                            f"mode={mode}" if mode else "",
                            f"container={container}" if container else "",
                            f"message={message}" if message else "",
                        ]
                        if part
                    )
                )

        evidence_summary = raw.get("evidence_summary")
        if isinstance(evidence_summary, dict):
            for item in evidence_summary.get("evidence") or []:
                formatted = self._format_evidence_item(item)
                if formatted:
                    items.append(formatted)

        structured_evidence = raw.get("evidence")
        structured_evidence = structured_evidence if isinstance(structured_evidence, dict) else {}
        for key in ("failure_modes", "contributing_factors"):
            for item in structured_evidence.get(key) or []:
                formatted = self._format_evidence_item(item)
                if formatted:
                    items.append(formatted)

        for finding in raw.get("container_log_findings") or []:
            if not isinstance(finding, dict):
                continue
            line_parts = []
            container = finding.get("container")
            reason = finding.get("reason") or finding.get("last_reason")
            restart_count = finding.get("restart_count")
            if container:
                line_parts.append(f"container={container}")
            if reason:
                line_parts.append(f"reason={reason}")
            if restart_count not in (None, ""):
                line_parts.append(f"restart_count={restart_count}")

            diagnostic_issue = finding.get("diagnostic_issue")
            if isinstance(diagnostic_issue, dict):
                issue_type = diagnostic_issue.get("type")
                if issue_type:
                    line_parts.append(f"diagnostic={issue_type}")
                for mismatch in diagnostic_issue.get("mismatches") or []:
                    if isinstance(mismatch, dict):
                        required = mismatch.get("required")
                        pinned = mismatch.get("pinned") or mismatch.get("current")
                        if required or pinned:
                            line_parts.append(f"required={required}, pinned={pinned}")

            previous = finding.get("logs_previous") if isinstance(finding.get("logs_previous"), dict) else {}
            current = finding.get("logs_current") if isinstance(finding.get("logs_current"), dict) else {}
            excerpt = str(previous.get("excerpt") or current.get("excerpt") or "").strip()
            if excerpt:
                line_parts.append(f"excerpt={self._compact_text(excerpt, 500)}")
            if line_parts:
                items.append("Container log finding: " + "; ".join(line_parts))

        events = raw.get("events")
        event_list = events.get("events") if isinstance(events, dict) else events
        if isinstance(event_list, list):
            for event in event_list[:3]:
                if not isinstance(event, dict):
                    continue
                reason = event.get("reason")
                message = event.get("message")
                if reason or message:
                    items.append(
                        "Kubernetes event: "
                        + "; ".join(
                            part for part in [
                                f"reason={reason}" if reason else "",
                                f"message={self._compact_text(str(message), 300)}" if message else "",
                            ]
                            if part
                        )
                    )

        if not items:
            items.append(f"{evidence.tool}: {evidence.summary}")
        return self._dedupe_keep_order(items)[:12]

    def _pod_recommendations(
        self,
        evidence_summary: dict[str, Any],
        ai_analysis: dict[str, Any] | None,
        root_candidate: dict[str, Any] | None,
    ) -> list[str]:
        recommendations: list[str] = []
        suggested_fix = str(evidence_summary.get("suggested_fix") or "").strip()
        if not suggested_fix and root_candidate:
            suggested_fix = str(root_candidate.get("suggested_fix") or "").strip()
        if suggested_fix:
            recommendations.append(suggested_fix)

        if ai_analysis:
            for key in ("solution", "recommendation"):
                value = str(ai_analysis.get(key) or "").strip()
                if value:
                    recommendations.append(value)
            steps = ai_analysis.get("steps")
            if isinstance(steps, list):
                recommendations.extend(str(step).strip() for step in steps if str(step).strip())

        if not recommendations:
            recommendations.append("Review and correct the failing container configuration, then redeploy the workload.")
        return self._dedupe_keep_order(recommendations)[:8]

    def _pod_next_checks(self, ai_analysis: dict[str, Any] | None) -> list[str]:
        checks: list[str] = []
        if ai_analysis:
            commands = ai_analysis.get("commands")
            if isinstance(commands, list):
                for command in commands:
                    if isinstance(command, dict):
                        cmd = command.get("command") or command.get("cmd")
                        desc = command.get("description")
                        if cmd and desc:
                            checks.append(f"{desc}: {cmd}")
                        elif cmd:
                            checks.append(str(cmd))
                    elif command:
                        checks.append(str(command))
            next_checks = ai_analysis.get("next_checks")
            if isinstance(next_checks, list):
                checks.extend(str(check).strip() for check in next_checks if str(check).strip())
        return self._dedupe_keep_order(checks)[:8]

    def _pod_ruled_out(self, raw: dict[str, Any]) -> list[str]:
        ruled_out: list[str] = []
        evidence_summary = raw.get("evidence_summary")
        if isinstance(evidence_summary, dict):
            for item in evidence_summary.get("ruled_out") or []:
                text = self._format_evidence_item(item)
                if text:
                    ruled_out.append(text)
        return self._dedupe_keep_order(ruled_out)[:8]

    def _verified_root_candidate(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        structured_evidence = raw.get("evidence")
        structured_evidence = structured_evidence if isinstance(structured_evidence, dict) else {}
        for item in structured_evidence.get("failure_modes") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "verified_root_cause" or item.get("root_cause"):
                return item
        return None

    def _format_evidence_item(self, item: Any) -> str:
        if isinstance(item, str):
            return self._compact_text(item, 500)
        if isinstance(item, dict):
            for key in ("summary", "root_cause", "evidence", "message", "excerpt"):
                value = item.get(key)
                if value:
                    return self._compact_text(str(value), 500)
            return self._compact_text(json.dumps(item, default=str, sort_keys=True), 500)
        if item:
            return self._compact_text(str(item), 500)
        return ""

    def _nested_dict(self, data: dict[str, Any], *keys: str) -> dict[str, Any] | None:
        current: Any = data
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current if isinstance(current, dict) else None

    def _compact_text(self, text: str, limit: int) -> str:
        compact = " ".join(text.split())
        return compact[:limit] + ("..." if len(compact) > limit else "")

    def _dedupe_keep_order(self, values: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    def replay(self, investigation: Investigation) -> list[dict]:
        return [event.model_dump(mode="json") for event in investigation.audit_log]

    def _remaining_steps(
        self, rendered_steps: list[dict[str, Any]], investigation: Investigation
    ) -> list[dict[str, Any]]:
        return [
            step
            for step in rendered_steps
            if not any(
                record.tool == step["tool"] and record.args == step["args"]
                for record in investigation.executed_tools
            )
        ]

    def _has_sufficient_evidence(self, investigation: Investigation) -> bool:
        return len(investigation.evidence) >= 5

    def _render_args(self, args: dict, alert: Alert) -> tuple[dict, list[str]]:
        """Render args; return (rendered_args, missing_template_vars).

        When a template expression like {{ alert.labels.pod }} resolves to
        None (label absent from the alert) we record the expression in the
        `missing` list rather than silently substituting it with "" or None.
        Callers should skip steps whose args have any missing vars — see the
        rendered_steps loop above for the audit-and-skip path.
        """
        rendered: dict = {}
        missing: list[str] = []
        for key, value in args.items():
            if not isinstance(value, str):
                rendered[key] = value
                continue

            full_expression = re.fullmatch(r"\s*\{\{\s*([^{}]+?)\s*\}\}\s*", value)
            if full_expression:
                expr = full_expression.group(1).strip()
                result = self._lookup_alert_expression(expr, alert)
                if result is None:
                    missing.append(f"{key} <- {{{{ {expr} }}}}")
                    continue
                rendered[key] = result
                continue

            local_missing: list[str] = []

            def replace(match: re.Match[str]) -> str:
                expr = match.group(1).strip()
                resolved = self._lookup_alert_expression(expr, alert)
                if resolved is None:
                    local_missing.append(expr)
                    return ""
                return str(resolved)

            rendered_value = re.sub(r"\{\{\s*([^{}]+?)\s*\}\}", replace, value)
            if local_missing:
                missing.extend(f"{key} <- {{{{ {var} }}}}" for var in local_missing)
                continue
            rendered[key] = rendered_value

        return rendered, missing

    def _lookup_alert_expression(self, expression: str, alert: Alert) -> str | None:
        parts = expression.split(".")
        if parts[:2] == ["alert", "labels"] and len(parts) == 3:
            return alert.labels.get(parts[2])
        if parts[:2] == ["alert", "annotations"] and len(parts) == 3:
            return alert.annotations.get(parts[2])
        if expression == "alert.name":
            return alert.name
        return None

    async def _recall_similar_incidents(
        self, investigation: Investigation, limit: int = 3
    ) -> list[SemanticIncidentRecord]:
        """Query semantic memory for past incidents similar to the current alert.

        Used to inject prior RCAs into the LLM context during analyze_evidence so
        the model can reuse past diagnoses. Excludes the current investigation
        itself (in case it was somehow indexed already). Fail-soft: returns []
        on any backend error so a recall failure cannot abort the investigation.
        """
        alert = investigation.alert
        # The query mirrors SemanticIncidentRecord.embedding_text() so the same
        # embedding model produces comparable vectors for store and recall.
        query = (
            f"Alert: {alert.name} | Severity: {alert.severity} | "
            f"Namespace: {alert.labels.get('namespace', '')} | "
            f"Kind: {alert.labels.get('kind', '')}"
        )
        try:
            results = await self.semantic_memory.search(query, limit=limit + 1)
        except Exception as exc:
            logger.warning(f"Fail-soft: semantic_memory recall failed: {exc}")
            return []
        return [r for r in results if r.investigation_id != investigation.investigation_id][:limit]

    def _to_semantic_record(self, investigation: Investigation) -> SemanticIncidentRecord:
        rca = investigation.rca
        classification = investigation.classification
        return SemanticIncidentRecord(
            investigation_id=investigation.investigation_id,
            alert_name=investigation.alert.name,
            category=classification.category if classification else "unknown",
            severity=investigation.alert.severity,
            cluster=investigation.alert.labels.get("cluster"),
            namespace=investigation.alert.labels.get("namespace"),
            workload=investigation.alert.labels.get("deployment")
            or investigation.alert.labels.get("pod")
            or investigation.alert.labels.get("resource"),
            rca_summary=rca.summary if rca else "No RCA generated",
            root_causes=rca.root_causes if rca else [],
            recommendations=rca.recommendations if rca else [],
            evidence_summaries=[e.summary for e in investigation.evidence],
            tags=[investigation.alert.source, investigation.alert.severity],
        )
