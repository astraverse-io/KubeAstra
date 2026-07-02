"""Fixed LLM model catalog endpoint.

The chat UI no longer exposes a model selector. This endpoint remains for
backwards compatibility with older clients, but returns only the hardcoded
Gemini model used by the backend.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

HARDCODED_GEMINI_MODEL = "gemini-3.1-flash-lite"


class ModelOption(BaseModel):
    id: str
    label: str


class ModelsResponse(BaseModel):
    provider: str
    current_model: str
    models: list[ModelOption]
    dynamic: bool = False
    error: Optional[str] = None


@router.get("/models", response_model=ModelsResponse)
def list_models():
    """Return the single hardcoded model for backwards compatibility."""
    return _response("gemini", HARDCODED_GEMINI_MODEL, [HARDCODED_GEMINI_MODEL], dynamic=False)


def _response(
    provider: str,
    current: str,
    models: list[str],
    *,
    dynamic: bool,
    error: str | None = None,
) -> ModelsResponse:
    clean = _dedupe(models)
    return ModelsResponse(
        provider=provider,
        current_model=current,
        models=[ModelOption(id=m, label=m) for m in clean],
        dynamic=dynamic,
        error=error,
    )

def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        value = (value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
