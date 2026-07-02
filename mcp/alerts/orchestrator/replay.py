from __future__ import annotations

from alerts.domain.investigation import Investigation


def build_replay_projection(investigation: Investigation) -> dict:
    return {
        "investigation_id": investigation.investigation_id,
        "status": investigation.status,
        "events": [event.model_dump(mode="json") for event in investigation.audit_log],
    }
