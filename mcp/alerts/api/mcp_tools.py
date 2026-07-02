import sqlite3
import json
import os
from pydantic import BaseModel, Field

from pathlib import Path

DEFAULT_DB_PATH = str((Path(__file__).parent / "../../../ui/backend/chat_history.db").resolve())
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH)

class GetRecentAlertsParams(BaseModel):
    limit: int = Field(default=10, description="Max number of alerts to return")
    status: str | None = Field(default=None, description="Filter by status (e.g. running, completed, failed)")

class GetInvestigationDetailsParams(BaseModel):
    investigation_id: str = Field(description="The UUID of the investigation to inspect")

def handle_get_recent_alerts(params: dict, ctx) -> dict:
    limit = params.get("limit", 10)
    status = params.get("status")
    
    query = "SELECT id, namespace, severity, source, status, created_at FROM investigations"
    args = []
    if status:
        query += " WHERE status = ?"
        args.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, tuple(args)).fetchall()
            return {"alerts": [dict(r) for r in rows]}
    except Exception as e:
        return {"error": f"Failed to read alerts from DB: {e}"}

def handle_get_investigation_details(params: dict, ctx) -> dict:
    inv_id = params.get("investigation_id")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT document FROM investigations WHERE id = ?", (inv_id,)).fetchone()
            if row:
                return {"investigation": json.loads(row["document"])}
            else:
                return {"error": f"Investigation {inv_id} not found."}
    except Exception as e:
        return {"error": f"Failed to read investigation from DB: {e}"}
