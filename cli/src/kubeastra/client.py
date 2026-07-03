"""HTTP + SSE client for the KubeAstra backend.

Thin wrapper around httpx + httpx-sse. All errors are mapped to
``ApiError`` (retryable connectivity issues), ``AuthError`` (401/403),
or ``BackendError`` (everything else the backend returns as JSON).
This lets the command layer render friendly messages without
duplicating the mapping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import httpx
from httpx_sse import EventSource, connect_sse


class ApiError(RuntimeError):
    """Cannot reach the backend, or a network-level failure."""


class AuthError(RuntimeError):
    """Backend rejected the request with 401/403."""


class BackendError(RuntimeError):
    """Backend responded with a non-2xx status carrying a JSON error body."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"backend error {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass
class StreamEvent:
    """A single SSE event decoded from the ``/api/chat/stream`` endpoint.

    Every field is optional because the backend emits several event types
    (``start``, ``iteration_planned``, ``thought_stream``, ``step_complete``,
    ``answer_start``, ``token``, ``answer_end``, ``done``, ``error``) that
    each carry a different subset. Consumers should switch on ``type``.
    """
    type: str
    iteration: Optional[int] = None
    thought: Optional[str] = None
    action: Optional[str] = None
    params: Optional[dict[str, Any]] = None
    duration_ms: Optional[int] = None
    preview: Optional[str] = None
    text: Optional[str] = None
    fallback_used: Optional[bool] = None
    result: Optional[dict[str, Any]] = None
    message: Optional[str] = None
    session: Optional[str] = None
    timestamp: Optional[float] = None
    raw: Optional[dict[str, Any]] = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "StreamEvent":
        return cls(
            type=str(payload.get("type") or "unknown"),
            iteration=payload.get("iteration"),
            thought=payload.get("thought"),
            action=payload.get("action"),
            params=payload.get("params"),
            duration_ms=payload.get("duration_ms"),
            preview=payload.get("preview"),
            text=payload.get("text"),
            fallback_used=payload.get("fallback_used"),
            result=payload.get("result"),
            message=payload.get("message"),
            session=payload.get("session"),
            timestamp=payload.get("timestamp"),
            raw=payload,
        )


class Client:
    """HTTP client for a KubeAstra backend.

    Instantiated once per CLI invocation. The backend URL and optional
    bearer token come from ``Config`` (persisted at
    ``~/.config/kubeastra/config.toml``).
    """

    def __init__(
        self,
        backend_url: str,
        api_token: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self._base = backend_url.rstrip("/")
        self._headers = {"Accept": "application/json"}
        if api_token:
            self._headers["Authorization"] = f"Bearer {api_token}"
        self._timeout = timeout

    # ── Cluster ─────────────────────────────────────────────────────────

    def autodetect_cluster(self) -> dict[str, Any]:
        return self._get_json("/api/cluster/autodetect")

    def connect_context(
        self,
        session_id: str,
        context_name: str,
        mode: str = "autodetect",
        kubeconfig_path: Optional[str] = None,
    ) -> dict[str, Any]:
        body = {
            "session_id": session_id,
            "context_name": context_name,
            "mode": mode,
        }
        if kubeconfig_path:
            body["kubeconfig_path"] = kubeconfig_path
        return self._post_json("/api/cluster/connect/context", body)

    def cluster_status(self, session_id: str) -> dict[str, Any]:
        return self._get_json(f"/api/cluster/status/{session_id}")

    # ── Chat streaming ──────────────────────────────────────────────────

    def stream_chat(
        self,
        message: str,
        history: Optional[list[dict[str, Any]]] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        """Post to /api/chat/stream and yield decoded StreamEvent objects.

        Raises ApiError on network failure, AuthError on 401/403,
        BackendError on other non-2xx. Streams until the server sends
        a ``done`` or ``error`` event.
        """
        body: dict[str, Any] = {"message": message, "history": history or []}
        if session_id:
            body["session_id"] = session_id
        if model:
            body["model"] = model

        headers = dict(self._headers)
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"

        try:
            with httpx.Client(timeout=None) as http:
                with connect_sse(
                    http,
                    "POST",
                    f"{self._base}/api/chat/stream",
                    json=body,
                    headers=headers,
                ) as event_source:
                    self._raise_for_status(event_source)
                    for sse_event in event_source.iter_sse():
                        if not sse_event.data:
                            continue
                        try:
                            payload = json.loads(sse_event.data)
                        except json.JSONDecodeError:
                            # Ignore malformed frames — matches the web UI
                            # behavior; keep the stream alive.
                            continue
                        yield StreamEvent.from_payload(payload)
        except httpx.RequestError as exc:
            raise ApiError(
                f"cannot reach backend at {self._base}: {exc}"
            ) from exc

    # ── Internals ───────────────────────────────────────────────────────

    def _get_json(self, path: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self._timeout) as http:
                response = http.get(f"{self._base}{path}", headers=self._headers)
        except httpx.RequestError as exc:
            raise ApiError(
                f"cannot reach backend at {self._base}: {exc}"
            ) from exc
        self._raise_for_status(response)
        return response.json()

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self._timeout) as http:
                response = http.post(
                    f"{self._base}{path}",
                    json=body,
                    headers={**self._headers, "Content-Type": "application/json"},
                )
        except httpx.RequestError as exc:
            raise ApiError(
                f"cannot reach backend at {self._base}: {exc}"
            ) from exc
        self._raise_for_status(response)
        return response.json()

    @staticmethod
    def _raise_for_status(response: httpx.Response | EventSource) -> None:
        # EventSource exposes the underlying response as .response
        actual = response.response if isinstance(response, EventSource) else response
        status = actual.status_code
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise AuthError(
                "backend rejected the request — set an API token with "
                "`kubeastra config set api-token <token>`"
            )
        try:
            detail = actual.json().get("detail", actual.text)
        except Exception:
            detail = actual.text
        raise BackendError(status, str(detail or "unknown error"))
