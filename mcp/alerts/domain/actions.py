from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field


class InvestigationAction(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = "playbook_step"
    source: str = "deterministic"

    @property
    def arguments_hash(self) -> str:
        payload = {"tool": self.tool, "args": self.args}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class ToolExecutionRecord(BaseModel):
    tool: str
    arguments_hash: str
    reason: str
    args: dict[str, Any] = Field(default_factory=dict)
