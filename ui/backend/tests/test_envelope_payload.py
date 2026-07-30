"""The envelope summarises for the model; the UI still needs the real data.

`make_events_envelope` keeps eight deduplicated messages, not the events. That
is right for the LLM and wrong for a table someone is reading — and every
ResultCard renderer looks for `events` / `pods` / `logs`, which an envelope
does not have. Enveloped tools therefore rendered empty cards.

`payload` carries the original result through for the UI. The property worth
protecting is that it goes *only* there: putting it back in front of the model
would hand back everything the summary exists to withhold, while still paying
for the summary.
"""

import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
BACKEND_DIR = Path(__file__).resolve().parent.parent
for d in (str(MCP_DIR), str(BACKEND_DIR)):
    if d not in sys.path:
        sys.path.insert(0, d)

from services.tool_envelope import make_events_envelope, make_generic_envelope  # noqa: E402
from react import _truncate_observation  # noqa: E402


def _events(count: int) -> dict:
    return {
        "events": [
            {
                "type": "Warning",
                "reason": f"Reason{i}",
                "message": f"message {i}",
                "count": 1,
                "involved_object": {"kind": "Pod", "name": f"pod-{i}"},
            }
            for i in range(count)
        ]
    }


def test_the_full_result_survives_on_the_envelope():
    raw = _events(40)
    envelope = make_events_envelope(raw, {"namespace": "demo"}, 10.0)

    assert envelope.payload is not None
    assert len(envelope.payload["events"]) == 40, "the UI needs all of them"


def test_the_summary_really_is_lossy():
    """If this ever stops being true, `payload` has become unnecessary."""
    envelope = make_events_envelope(_events(40), {"namespace": "demo"}, 10.0)
    dumped = envelope.model_dump(by_alias=True)

    assert "events" not in dumped, "an envelope is not the raw result"


def test_payload_is_not_serialised_by_default():
    """Envelopes are dumped into LLM prompts at several places; any one of
    them forgetting to strip this would defeat the point."""
    envelope = make_events_envelope(_events(40), {"namespace": "demo"}, 10.0)

    assert "payload" not in envelope.model_dump(by_alias=True)
    assert "payload" not in envelope.model_dump()


def test_the_observation_the_model_sees_never_contains_the_payload():
    raw = _events(40)
    envelope = make_events_envelope(raw, {"namespace": "demo"}, 10.0)
    # Mirrors what react.py hands to the frontend: the dump plus the payload.
    for_frontend = envelope.model_dump(by_alias=True)
    for_frontend["payload"] = envelope.payload

    observation = _truncate_observation(for_frontend, "get_events")

    assert "payload" not in observation
    # A distinctive string only present in the deep payload, not the summary.
    assert "pod-39" not in observation


def test_generic_envelopes_carry_the_payload_too():
    raw = {"nodes": [{"name": "node-1"}, {"name": "node-2"}]}
    envelope = make_generic_envelope("get_nodes", raw, {}, 5.0)

    assert envelope.payload == raw
    assert "payload" not in envelope.model_dump(by_alias=True)
