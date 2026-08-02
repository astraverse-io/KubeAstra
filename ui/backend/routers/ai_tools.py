"""AI Analysis endpoints — no live cluster access needed.

POST /api/analyze      → analyze_error (Gemini + RAG)
POST /api/fix          → get_fix_commands (curated playbooks + Gemini fallback)
GET  /api/categories   → list_error_categories
POST /api/runbook      → generate_runbook
POST /api/report       → cluster_report (paste kubectl events output)
POST /api/summary      → error_summary (batch of CI/CD errors)
"""

import json
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import ai_tools.analyze as _analyze
import ai_tools.fix as _fix
import ai_tools.report as _report
import ai_tools.runbook as _runbook

from http_errors import internal_error

router = APIRouter()


# ── Request models ─────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    error_text: str
    tool: str = "kubernetes"
    environment: str = "production"


class FixRequest(BaseModel):
    error_text: Optional[str] = None
    category: Optional[str] = None
    tool: str = "kubernetes"
    namespace: str = "<namespace>"
    resource_name: str = "<name>"


class RunbookRequest(BaseModel):
    category: Optional[str] = None
    error_text: Optional[str] = None
    error_examples: Optional[List[str]] = None
    tool: str = "kubernetes"


class ReportRequest(BaseModel):
    events_text: str
    namespace: str = "all"


class SummaryRequest(BaseModel):
    errors: List[str]
    tool: str = "kubernetes"


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/analyze")
def analyze_error(req: AnalyzeRequest):
    try:
        raw = _analyze.run(req.error_text, req.tool, req.environment)
        return json.loads(raw)
    except Exception:
        raise internal_error(context="ai tools endpoint")


@router.post("/fix")
def get_fix_commands(req: FixRequest):
    try:
        raw = _fix.get_fix_commands(
            req.error_text, req.category, req.tool, req.namespace, req.resource_name
        )
        return json.loads(raw)
    except Exception:
        raise internal_error(context="ai tools endpoint")


@router.get("/categories")
def list_categories():
    try:
        raw = _fix.list_categories()
        return json.loads(raw)
    except Exception:
        raise internal_error(context="ai tools endpoint")


@router.post("/runbook")
def generate_runbook(req: RunbookRequest):
    try:
        raw = _runbook.generate_runbook(
            req.category, req.error_examples, req.error_text, req.tool
        )
        return json.loads(raw)
    except Exception:
        raise internal_error(context="ai tools endpoint")


@router.post("/report")
def cluster_report(req: ReportRequest):
    try:
        raw = _report.cluster_report(req.events_text, req.namespace)
        return json.loads(raw)
    except Exception:
        raise internal_error(context="ai tools endpoint")


@router.post("/summary")
def error_summary(req: SummaryRequest):
    try:
        raw = _report.error_summary(req.errors, req.tool)
        return json.loads(raw)
    except Exception:
        raise internal_error(context="ai tools endpoint")
