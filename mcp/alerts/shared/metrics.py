from __future__ import annotations

from collections import Counter
from threading import Lock


class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += value

    def render_prometheus(self) -> str:
        with self._lock:
            lines = ["# TYPE intelligent_alert_manager_counter counter"]
            for name, value in sorted(self._counters.items()):
                lines.append(f"{name} {value}")
            return "\n".join(lines) + "\n"


metrics = MetricsRegistry()
