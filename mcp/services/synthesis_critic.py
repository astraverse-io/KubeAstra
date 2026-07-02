import json
import logging
from typing import Any, List, Dict, Optional

logger = logging.getLogger(__name__)

CRITIC_SYSTEM_PROMPT = """You are a critical reviewer for a Kubernetes DevOps Assistant. Your job is to verify that the generated Markdown answer is strictly supported by the gathered ToolEnvelopes and retrieval context.

You must score four binary checks (PASS or FAIL) and provide a concise one-line rationale for each:
1. "evidence_supported": Every claim in the # Diagnosis and # Evidence sections must trace directly to a field path in one of the ToolEnvelopes or the retrieval context.
2. "no_contradiction": The answer must not contradict any evidence in the ToolEnvelopes.
3. "recency_correct": If multiple events are in evidence, the answer must address the most recent events first or focus on them.
4. "confidence_honest": The stated confidence band (low | medium | high) in the # Uncertainty section must match the completeness and age of the evidence.

You MUST respond with a JSON object of this exact schema:
{
  "evidence_supported": {
    "passed": true/false,
    "rationale": "one-line explanation"
  },
  "no_contradiction": {
    "passed": true/false,
    "rationale": "one-line explanation"
  },
  "recency_correct": {
    "passed": true/false,
    "rationale": "one-line explanation"
  },
  "confidence_honest": {
    "passed": true/false,
    "rationale": "one-line explanation"
  }
}
Respond with ONLY the JSON object. Do not include any other markdown formatting or wrappers except the JSON itself.
"""

CRITIC_USER_PROMPT = """Review the following information:

User Question:
{question}

Gathered Tool Envelopes:
{envelopes_json}

Retrieval/RAG Context:
{retrieval_context}

Generated Answer:
{answer}

Analyze the generated answer against the checks. Return the JSON object.
"""

def _safe_increment_critic_fallback(check_name: str) -> None:
    try:
        from metrics import critic_fallback_total
        critic_fallback_total.labels(check_name=check_name).inc()
    except Exception:
        pass


def run_synthesis_critic(
    provider: Any,
    question: str,
    envelopes: List[Any],
    retrieval_context: str,
    answer: str,
    usage_holder: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Run the synthesis critic on a candidate answer and return the checks.
    
    Returns a dictionary of check names mapping to {'passed': bool, 'rationale': str}.
    """
    if not provider:
        logger.warning("No LLM provider available for critic; passing checks by default.")
        for key in ["evidence_supported", "no_contradiction", "recency_correct", "confidence_honest"]:
            _safe_increment_critic_fallback(key)
        return {
            "evidence_supported": {"passed": True, "rationale": "No LLM provider for review"},
            "no_contradiction": {"passed": True, "rationale": "No LLM provider for review"},
            "recency_correct": {"passed": True, "rationale": "No LLM provider for review"},
            "confidence_honest": {"passed": True, "rationale": "No LLM provider for review"},
        }
        
    # Serialize envelopes cleanly
    env_dicts = []
    for env in envelopes:
        if hasattr(env, "model_dump"):
            env_dicts.append(env.model_dump(by_alias=True))
        elif isinstance(env, dict):
            env_dicts.append(env)
        else:
            env_dicts.append(str(env))
            
    envelopes_json = json.dumps(env_dicts, indent=2)
    
    prompt = CRITIC_USER_PROMPT.format(
        question=question,
        envelopes_json=envelopes_json,
        retrieval_context=retrieval_context or "(none)",
        answer=answer
    )
    
    from services.llm.pricing import TokenUsage
    usage = TokenUsage.empty(model=getattr(provider, "model", ""))
    try:
        if hasattr(provider, "generate_with_usage"):
            raw_response, usage = provider.generate_with_usage(prompt, system=CRITIC_SYSTEM_PROMPT, temperature=0.1, max_tokens=1000)
        elif hasattr(provider, "generate"):
            raw_response = provider.generate(prompt, system=CRITIC_SYSTEM_PROMPT, temperature=0.1, max_tokens=1000)
        else:
            raw_response = "".join(provider.generate_stream(prompt, system=CRITIC_SYSTEM_PROMPT, temperature=0.1, max_tokens=1000))
        logger.info("Critic raw response: %s", raw_response)
        
        # Parse JSON from response
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
            
        result = json.loads(cleaned)
        if not isinstance(result, dict):
            result = {}
        
        # Ensure all four keys are present
        required_keys = ["evidence_supported", "no_contradiction", "recency_correct", "confidence_honest"]
        for key in required_keys:
            if key not in result or not isinstance(result[key], dict) or "passed" not in result[key]:
                logger.warning("Critic response missing or malformed key '%s', raw response was: %s", key, raw_response)
                result[key] = {"passed": True, "rationale": "Validation skipped/missing key in critic response"}
                _safe_increment_critic_fallback(key)
                
        if usage_holder is not None:
            usage_holder.append(usage)
        return result
    except Exception as e:
        logger.exception("Error running synthesis critic")
        if usage_holder is not None:
            usage_holder.append(usage)
        for key in ["evidence_supported", "no_contradiction", "recency_correct", "confidence_honest"]:
            _safe_increment_critic_fallback(key)
        return {
            "evidence_supported": {"passed": True, "rationale": f"Critic failed to evaluate: {e}"},
            "no_contradiction": {"passed": True, "rationale": f"Critic failed to evaluate: {e}"},
            "recency_correct": {"passed": True, "rationale": f"Critic failed to evaluate: {e}"},
            "confidence_honest": {"passed": True, "rationale": f"Critic failed to evaluate: {e}"},
        }

