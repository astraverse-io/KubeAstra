from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from alerts.domain.playbook import Playbook


class PlaybookLoader:
    def __init__(self, playbook_path: str | Path) -> None:
        self.playbook_path = Path(playbook_path)

    def load_all(self) -> list[Playbook]:
        if not self.playbook_path.exists():
            return []
        search_path = self.playbook_path / "alert_playbooks"
        if not search_path.exists():
            search_path = self.playbook_path
        playbooks = []
        for path in sorted(search_path.glob("*.yaml")):
            payload = self._read_yaml(path)
            if self._is_playbook_payload(payload):
                resolved = self._resolve_payload(payload, seen={path.name})
                if not resolved.get("abstract", False):
                    playbooks.append(Playbook.model_validate(resolved))
        return playbooks

    def load(self, path: str | Path) -> Playbook:
        path = Path(path)
        payload = self._read_yaml(path)
        return Playbook.model_validate(self._resolve_payload(payload, seen={path.name}))

    def _read_yaml(self, path: Path) -> dict:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def _is_playbook_payload(self, payload: dict) -> bool:
        if payload.get("id") and (payload.get("abstract") or payload.get("extends")):
            return True
        required = {"id", "version", "display_name", "supported_alerts", "allowed_tools", "steps"}
        return required.issubset(payload)

    def _resolve_payload(self, payload: dict[str, Any], seen: set[str]) -> dict[str, Any]:
        parent_ref = payload.get("extends")
        if parent_ref:
            parent_path = self._resolve_parent_path(str(parent_ref))
            if parent_path.name in seen:
                chain = " -> ".join([*seen, parent_path.name])
                raise ValueError(f"Circular playbook inheritance detected: {chain}")
            parent_payload = self._read_yaml(parent_path)
            resolved = self._resolve_payload(parent_payload, seen={*seen, parent_path.name})
        else:
            resolved = {}

        for include_ref in payload.get("includes", []):
            include_path = self._resolve_include_path(str(include_ref))
            if include_path.name in seen:
                chain = " -> ".join([*seen, include_path.name])
                raise ValueError(f"Circular playbook include detected: {chain}")
            include_payload = self._read_yaml(include_path)
            resolved_include = self._resolve_payload(
                include_payload,
                seen={*seen, include_path.name},
            )
            resolved = self._merge_payloads(resolved, resolved_include)

        resolved = self._merge_payloads(resolved, payload)
        if "abstract" in payload:
            resolved["abstract"] = payload["abstract"]
        resolved.pop("includes", None)
        self._validate_resolved_payload(resolved)
        return resolved

    def _resolve_parent_path(self, parent_ref: str) -> Path:
        candidates = [
            self.playbook_path / parent_ref,
            self.playbook_path / f"{parent_ref}.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"Parent playbook not found: {parent_ref}")

    def _resolve_include_path(self, include_ref: str) -> Path:
        candidates = [
            self.playbook_path / include_ref,
            self.playbook_path / f"{include_ref}.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"Included playbook branch not found: {include_ref}")

    def _merge_payloads(self, parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
        merged = {
            key: value
            for key, value in parent.items()
            if key not in {"abstract", "extends", "includes"}
        }
        for key, value in child.items():
            if key in {"abstract", "extends", "includes"}:
                continue
            if key == "allowed_tools":
                merged[key] = self._merge_unique(parent.get(key, []), value)
            elif key in {"steps", "decision_rules", "supported_alerts"}:
                merged[key] = [*parent.get(key, []), *value]
            elif key == "remediation_mappings":
                merged[key] = self._merge_mapping_lists(parent.get(key, {}), value)
            elif isinstance(value, dict) and isinstance(parent.get(key), dict):
                merged[key] = {**parent[key], **value}
            else:
                merged[key] = value
        return merged

    def _merge_unique(self, parent_values: list[Any], child_values: list[Any]) -> list[Any]:
        merged = []
        for item in [*parent_values, *child_values]:
            if item not in merged:
                merged.append(item)
        return merged

    def _merge_mapping_lists(
        self,
        parent: dict[str, list[Any]],
        child: dict[str, list[Any]],
    ) -> dict[str, list[Any]]:
        merged = {key: list(value) for key, value in parent.items()}
        for key, value in child.items():
            merged[key] = self._merge_unique(merged.get(key, []), value)
        return merged

    def _validate_resolved_payload(self, payload: dict[str, Any]) -> None:
        step_ids = [step.get("id") for step in payload.get("steps", [])]
        duplicates = {step_id for step_id in step_ids if step_ids.count(step_id) > 1}
        if duplicates:
            raise ValueError(f"Duplicate playbook step ids: {sorted(duplicates)}")
        allowed_tools = set(payload.get("allowed_tools", []))
        step_id_set = set(step_ids)
        warnings = list(payload.get("validation_warnings", []))
        for rule in payload.get("decision_rules", []):
            unknown_tools = set(rule.get("prefer_tools", [])) - allowed_tools
            if unknown_tools:
                message = (
                    f"Decision rule {rule.get('id')} references unknown tools: "
                    f"{sorted(unknown_tools)}"
                )
                raise ValueError(message)
            unknown_steps = set(rule.get("prefer_steps", [])) - step_id_set
            if unknown_steps:
                warnings.append(
                    {
                        "type": "missing_preferred_steps",
                        "rule_id": rule.get("id"),
                        "missing_steps": sorted(unknown_steps),
                        "message": (
                            f"Decision rule {rule.get('id')} prefers steps that are not "
                            "included in this playbook; review whether the branch should "
                            "add, rename, or ignore these steps."
                        ),
                        "requires_human_review": True,
                    }
                )
        payload["validation_warnings"] = warnings
