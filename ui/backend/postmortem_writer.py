"""Postmortem writer subagent service.

Loads a completed or failed agent run trace, formats the investigation timeline,
and invokes the Gemini LLM to write a professional Markdown incident report.
"""

import logging
from typing import Optional, Any

import db

logger = logging.getLogger(__name__)

# System prompt for postmortem writer subagent
POSTMORTEM_SYSTEM_PROMPT = """You are a senior DevOps SRE and postmortem writer.
Your job is to write a highly professional, detailed incident postmortem report in Markdown based on the provided investigation log.

The report MUST include the following sections:
1. **Summary / Incident Description**: A high-level description of what occurred, what was requested, and the ultimate outcome (success or failure).
2. **Timeline of Investigation**: A chronological sequence of the agent's actions, tool calls, and observations. Be concise but include key details (e.g. pods checked, errors seen, specific commands run).
3. **Root Cause**: The underlying issue identified (e.g. image pull failure, OOMKilled, bad probe settings, missing ConfigMap, etc.).
4. **Remediation & Action Taken**: Mutating operations proposed or performed to resolve the issue (e.g. rollout restart, pod delete, deployment scaled).
5. **Verification & Post-Remediation Health**: The outcome of the verification checks indicating whether the cluster returned to a healthy state.
6. **Preventative Recommendations**: Suggested long-term actions to prevent this incident from recurring.

Format rules:
- Keep the tone professional, objective, and analytical.
- Use standard Markdown headings, tables, bullet points, and code formatting.
- Cite specific resource names, namespaces, and error messages from the log.
- Do not make up facts; if verification or remediation did not occur in the log, state that clearly in the respective section.
"""

def generate_postmortem_prompt(run: dict, steps: list[dict]) -> str:
    """Assemble the timeline and details of a run into a structured prompt."""
    user_request = run.get('memory_snapshot')
    if not user_request:
        session_id = run.get("session_id")
        if session_id:
            try:
                history_db = db.get_history(session_id)
                user_msgs = [m for m in history_db if m.get("role") == "user"]
                if user_msgs:
                    user_request = user_msgs[0].get("content")
            except Exception as e:
                logger.warning("Failed to fetch session history fallback: %s", e)
    if not user_request:
        user_request = 'Unknown request'

    prompt = f"USER REQUEST: {user_request}\n\n"
    
    # Add final answer if available
    if run.get("final_answer"):
        prompt += f"FINAL ANSWER SUMMARY: {run.get('final_answer')}\n\n"
    if run.get("error"):
        prompt += f"RUN FAILURE / ERROR: {run.get('error')}\n\n"
        
    prompt += "CHRONOLOGICAL STEP LOG:\n"
    for step in steps:
        iter_num = step.get("iteration", 0)
        action = step.get("action", "unknown")
        kind = step.get("step_kind", "tool")
        status = step.get("status", "ok")
        thought = step.get("thought", "")
        
        prompt += f"--- STEP {iter_num} ({kind}) ---\n"
        if thought:
            prompt += f"Thought: {thought}\n"
        
        if kind == "answer":
            prompt += f"Answer generated: {step.get('observation_preview')}\n"
        else:
            params = step.get("params_json")
            preview = step.get("observation_preview") or ""
            err_type = step.get("error_type")
            err_msg = step.get("error_message")
            
            if action and action != "unknown":
                prompt += f"Action: {action} with params {params}\n"
            
            if status == "error":
                prompt += f"Status: ERROR ({err_type}): {err_msg}\n"
            elif preview:
                prompt += f"Observation: {preview[:1000]}...\n"
            
    return prompt

def get_llm_provider():
    """Lazily resolve the configured LLM provider."""
    try:
        from config.settings import get_settings
        settings = get_settings()
        provider_name = (settings.llm_provider or "gemini").lower()
        selected_model = "gemini-3.1-flash-lite" if provider_name == "gemini" else ""
        from services.llm import get_provider
        return get_provider(model=selected_model or None)
    except Exception as e:
        logger.warning(f"LLM provider unavailable: {e}")
        return None

def write_postmortem(run_id: str, provider: Optional[Any] = None) -> Optional[str]:
    """Load run trace, generate a postmortem report using the LLM, and persist it to DB.
    Returns the generated Markdown report, or None on failure."""
    run = db.get_agent_run(run_id)
    if not run:
        logger.warning(f"Could not write postmortem: Run {run_id} not found")
        return None
        
    steps = db.get_agent_steps(run_id)
    if not steps:
        logger.warning(f"No steps found for run {run_id}")
        
    prompt = generate_postmortem_prompt(run, steps)
    
    # Resolve provider if not passed
    if provider is None:
        provider = get_llm_provider()
        
    if provider is None or not provider.enabled:
        logger.warning("No enabled LLM provider available for postmortem generation")
        return None
        
    try:
        logger.info(f"Generating postmortem for run {run_id}...")
        report = provider.generate(
            prompt=prompt,
            system=POSTMORTEM_SYSTEM_PROMPT,
            temperature=0.2,
        )
        if report:
            # Save back to database
            db.update_agent_run_postmortem(run_id, report)
            return report
    except Exception as exc:
        logger.error(f"Error generating postmortem: {exc}")
        
    return None
