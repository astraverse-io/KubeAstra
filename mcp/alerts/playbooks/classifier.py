from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from alerts.domain.alert import Alert, AlertClassification
from alerts.domain.playbook import Playbook


class AlertClassifier:
    def __init__(
        self,
        playbooks: list[Playbook],
        rules_path: str | Path = "playbooks/classification-rules.yaml",
    ) -> None:
        self._playbook_ids = {playbook.id for playbook in playbooks}
        self._rules = self._load_rules(Path(rules_path))

    def classify(self, alert: Alert) -> AlertClassification:
        exact_match = self._match_exact_override(alert)
        if exact_match:
            return exact_match

        regex_match = self._match_regex_group(alert)
        if regex_match:
            return regex_match

        label_match = self._match_label_hint(alert)
        if label_match:
            return label_match

        fallback = self._rules.get(
            "fallback",
            {"playbook_id": "generic", "category": "generic", "confidence": 0.3},
        )
        return self._classification(
            rule=f"fallback:{fallback['playbook_id']}",
            playbook_id=fallback["playbook_id"],
            category=fallback["category"],
            confidence=fallback["confidence"],
        )

    def _match_exact_override(self, alert: Alert) -> AlertClassification | None:
        alert_names = {alert.name, alert.labels.get("alertname", "")}
        for rule in self._rules.get("exact_overrides", []):
            configured = set(rule.get("alert_names", []))
            if alert_names & configured:
                return self._classification(
                    rule=f"exact:{rule['id']}",
                    playbook_id=rule["playbook_id"],
                    category=rule["category"],
                    confidence=rule.get("confidence", 0.99),
                )
        return None

    def _match_regex_group(self, alert: Alert) -> AlertClassification | None:
        best: AlertClassification | None = None
        for rule in self._rules.get("regex_groups", []):
            haystack = self._field_text(alert, rule.get("fields", []))
            matched_patterns = [
                pattern for pattern in rule.get("patterns", []) if re.search(pattern, haystack)
            ]
            if not matched_patterns:
                continue
            candidate = self._classification(
                rule=f"regex:{rule['id']}",
                playbook_id=rule["playbook_id"],
                category=rule["category"],
                confidence=rule.get("confidence", 0.85),
                extra=[f"pattern:{pattern}" for pattern in matched_patterns],
            )
            if best is None or candidate.confidence > best.confidence:
                best = candidate
        return best

    def _match_label_hint(self, alert: Alert) -> AlertClassification | None:
        for rule in self._rules.get("label_hints", []):
            labels = rule.get("labels", {})
            if all(
                re.search(pattern, alert.labels.get(label, "")) for label, pattern in labels.items()
            ):
                return self._classification(
                    rule=f"label:{rule['id']}",
                    playbook_id=rule["playbook_id"],
                    category=rule["category"],
                    confidence=rule.get("confidence", 0.8),
                )
        return None

    def _classification(
        self,
        *,
        rule: str,
        playbook_id: str,
        category: str,
        confidence: float,
        extra: list[str] | None = None,
    ) -> AlertClassification:
        if playbook_id not in self._playbook_ids:
            playbook_id = "generic"
            category = "generic"
            rule = f"{rule}:missing_playbook"
            confidence = min(confidence, 0.3)
        return AlertClassification(
            category=category,
            confidence=confidence,
            matched_rules=[rule, *(extra or [])],
            playbook_id=playbook_id,
        )

    def _field_text(self, alert: Alert, fields: list[str]) -> str:
        values: list[str] = []
        for field in fields:
            value = self._field_value(alert, field)
            if value:
                values.append(value)
        return " ".join(values)

    def _field_value(self, alert: Alert, field: str) -> str:
        if field == "name":
            return alert.name
        if field.startswith("labels."):
            return alert.labels.get(field.removeprefix("labels."), "")
        if field.startswith("annotations."):
            return alert.annotations.get(field.removeprefix("annotations."), "")
        return ""

    def _load_rules(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {
                "fallback": {
                    "playbook_id": "generic",
                    "category": "generic",
                    "confidence": 0.3,
                }
            }
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
