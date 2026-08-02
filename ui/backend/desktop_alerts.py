"""Poll Alertmanager, so a laptop can be told about firing alerts.

Server mode receives alerts by webhook (`POST /api/alerts/webhook`). That
cannot work for desktop: Alertmanager lives in the cluster and a laptop has no
routable address it can post to, and would not be reachable when closed
anyway. So desktop inverts it and polls.

The queue this fills is drained by `GET /api/desktop/notifications`, which the
Tauri shell polls in order to raise native notifications. Nothing here knows
about notifications — it only decides what is *new*.

Reuses `normalize_alert_payload` from the webhook path, so both routes produce
the same `Alert` objects and the same fingerprints.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import desktop_config

logger = logging.getLogger(__name__)


def _http_url(base_url: str) -> str:
    """Accept an Alertmanager base URL only if it is http(s), else refuse.

    ``urllib.request.urlopen`` is not an HTTP client — it dispatches on scheme,
    and ``file:///etc/shadow`` is a URL it will happily open and hand back as a
    response body. The value arrives from the settings screen, so nothing here
    guarantees it is a URL at all, let alone a remote one; a background thread
    polling it every 30 seconds is the last place to find that out.

    Rejects rather than coerces. Somebody who typed a path where a URL belongs
    needs to see that, not have it quietly rewritten into a different mistake.
    """
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            "Alertmanager URL must start with http:// or https:// "
            f"(got {parsed.scheme or 'no'} scheme)"
        )
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )

# Cap the queue. A cluster that starts flapping must not grow this without
# bound while the shell is not draining it — the user is not going to read
# 4,000 notifications, and the newest ones are the ones that matter.
MAX_QUEUED = 50
# Fingerprints of alerts already announced. Bounded for the same reason.
MAX_REMEMBERED = 2000


def _v2_to_webhook_shape(alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Adapt `GET /api/v2/alerts` to what the webhook normalizer expects.

    The two Alertmanager formats are close but not identical: the v2 read API
    reports `status` as an object (`{"state": "active", …}`) while a webhook
    delivers the string "firing"/"resolved". Left unadapted, every alert would
    normalize with a status of the stringified dict.
    """
    adapted = []
    for item in alerts:
        if not isinstance(item, dict):
            continue
        copy = dict(item)
        status = item.get("status")
        if isinstance(status, dict):
            state = status.get("state", "active")
            copy["status"] = "resolved" if state == "resolved" else "firing"
        elif not isinstance(status, str):
            copy["status"] = "firing"
        adapted.append(copy)
    return {"alerts": adapted, "status": "firing"}


class AlertPoller:
    """Background poll of one Alertmanager, exposing newly-firing alerts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: List[Dict[str, Any]] = []
        self._seen: List[str] = []
        self._seen_set: set[str] = set()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Cuts the inter-poll sleep short. Without it, enabling notifications
        # takes effect up to a full interval later — and the priming poll
        # then treats anything that started firing in that gap as
        # pre-existing, so the first real alert after switching it on is
        # silently swallowed.
        self._wake = threading.Event()
        self._last_error: str = ""
        self._last_poll: float = 0.0
        # A first poll would otherwise announce every alert already firing in
        # the cluster, which for a laptop opened on a Monday morning is a
        # notification storm about incidents that are hours old.
        self._primed = False

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="alertmanager-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh(self) -> None:
        """Poll now instead of at the next interval.

        Called when settings change, so that enabling notifications primes
        against the cluster as it is at that moment.
        """
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            config = desktop_config.load()
            interval = max(10, int(config.get("alert_poll_seconds") or 30))
            if config.get("notifications_enabled") and config.get("alertmanager_url"):
                try:
                    self.poll_once(str(config["alertmanager_url"]))
                except Exception as error:  # never let the thread die
                    self._last_error = str(error)
                    logger.warning("desktop: alert poll failed (%s)", error)
            else:
                # Turning notifications off should also forget the primer, so
                # turning them back on does not replay the backlog.
                self._primed = False
            self._wake.wait(interval)
            self._wake.clear()

    # ── polling ───────────────────────────────────────────────────────────

    def fetch(self, base_url: str, timeout: float = 10.0) -> List[Dict[str, Any]]:
        url = _http_url(base_url) + "/api/v2/alerts?active=true&silenced=false&inhibited=false"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"Alertmanager returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise RuntimeError("Alertmanager did not return a list of alerts")
        return payload

    def poll_once(self, base_url: str) -> List[Dict[str, Any]]:
        """One poll. Returns the alerts newly queued by this call."""
        raw = self.fetch(base_url)
        self._last_poll = time.time()
        self._last_error = ""
        return self.ingest(raw)

    def ingest(self, raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize, drop anything already announced, and queue the rest."""
        from alerts.domain.normalization import normalize_alert_payload

        alerts = normalize_alert_payload(_v2_to_webhook_shape(raw))
        fresh: List[Dict[str, Any]] = []

        with self._lock:
            first_run = not self._primed
            for alert in alerts:
                if alert.status != "firing":
                    continue
                if alert.fingerprint in self._seen_set:
                    continue
                self._remember(alert.fingerprint)
                if first_run:
                    # Recorded as seen, but not announced.
                    continue
                fresh.append(_summarize(alert))

            self._primed = True
            self._queue.extend(fresh)
            if len(self._queue) > MAX_QUEUED:
                self._queue = self._queue[-MAX_QUEUED:]

        return fresh

    def _remember(self, fingerprint: str) -> None:
        self._seen_set.add(fingerprint)
        self._seen.append(fingerprint)
        if len(self._seen) > MAX_REMEMBERED:
            dropped = self._seen[: len(self._seen) - MAX_REMEMBERED]
            self._seen = self._seen[len(dropped) :]
            for item in dropped:
                self._seen_set.discard(item)

    # ── consumption ───────────────────────────────────────────────────────

    def drain(self) -> List[Dict[str, Any]]:
        """Take everything queued. Draining is destructive by design.

        The consumer is the shell, which raises an OS notification per item.
        Re-delivering on the next poll would mean duplicate notifications for
        the same alert, which is worse than losing one if the shell crashes
        between the drain and the notification.
        """
        with self._lock:
            queued, self._queue = self._queue, []
        return queued

    def status(self) -> Dict[str, Any]:
        config = desktop_config.load()
        with self._lock:
            return {
                "enabled": bool(config.get("notifications_enabled")),
                "configured": bool(config.get("alertmanager_url")),
                "queued": len(self._queue),
                "known_alerts": len(self._seen_set),
                "last_poll": self._last_poll or None,
                "last_error": self._last_error or None,
            }


def _summarize(alert: Any) -> Dict[str, Any]:
    """The fields a notification needs, and the ones a click needs."""
    labels = alert.labels or {}
    annotations = alert.annotations or {}
    return {
        "fingerprint": alert.fingerprint,
        "name": alert.name,
        "severity": alert.severity,
        "namespace": labels.get("namespace", ""),
        "pod": labels.get("pod", ""),
        "summary": annotations.get("summary") or annotations.get("description") or "",
        "starts_at": alert.starts_at.isoformat() if alert.starts_at else None,
    }


# Module-level singleton, matching how the rest of the backend exposes
# long-lived services (see mcp/services/embeddings.py).
poller = AlertPoller()
