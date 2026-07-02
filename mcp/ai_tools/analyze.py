"""AI Tool: analyze_error
Accepts raw error text, classifies it, searches similar past issues via RAG, and returns an AI solution.
Works with pasted errors from logs, CI/CD pipelines, or any source (no live cluster needed).
"""

import json

from services.error_parser import extract_context, reconcile_category
from services.llm_service import llm_service
from services.vector_db import vector_db
from services.embeddings import embeddings


def run(
    error_text: str,
    tool: str = "kubernetes",
    environment: str = "production",
    *,
    structured_payload=None,
    diagnostic_mode: str | None = None,
) -> str:
    context = extract_context(
        error_text,
        tool,
        structured_payload=structured_payload,
    )
    context["environment"] = environment
    if diagnostic_mode:
        context["diagnostic_mode"] = diagnostic_mode

    query_vector = embeddings.embed(error_text)
    similar = vector_db.search(query_vector, tool=tool, limit=5)

    result = llm_service.analyze(error_text, context, similar)
    llm_category = result.get("category")
    category, reconciled_source = reconcile_category(
        context["category"], llm_category
    )
    category_source = (
        context.get("category_source", reconciled_source)
        if category == context["category"]
        else reconciled_source
    )

    output = {
        "category":   category,
        "classification": {
            "source": category_source,
            "deterministic_category": context["category"],
            "llm_category": llm_category,
        },
        "severity":   result.get("severity", "unknown"),
        "confidence": result.get("confidence", 0.0),
        "root_cause": result.get("root_cause", ""),
        "solution":   result.get("solution", ""),
        "steps":      result.get("steps", []),
        "commands":   result.get("commands", []),
        "prevention": result.get("prevention", ""),
        "similar_cases": [
            {
                "error":        s["error_text"][:150],
                "solution":     s["solution_text"][:200],
                "similarity":   f"{round(s['similarity'] * 100)}%",
                "success_rate": f"{s['success_rate']}%",
            }
            for s in similar
        ],
        "context": {k: v for k, v in context.items() if k not in ("error_hash",)},
    }
    if context.get("request_evidence"):
        output["request_evidence"] = context["request_evidence"]

    # Pass through corrected code fields when Gemini provides them
    if result.get("corrected_snippet"):
        output["corrected_snippet"] = result["corrected_snippet"]
    if result.get("corrected_file"):
        output["corrected_file"] = result["corrected_file"]

    return json.dumps(output, indent=2)
