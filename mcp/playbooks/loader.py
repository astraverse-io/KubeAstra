"""YAML loader for playbook definitions.

Reads .yaml/.yml files from playbooks/builtins/ and an optional
user-configured PLAYBOOK_DIR, then converts them into Playbook dataclasses.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from playbooks.schema import (
    Playbook,
    PlaybookParam,
    PlaybookStep,
    PlaybookTrigger,
)

logger = logging.getLogger(__name__)

BUILTINS_DIR = Path(__file__).parent / "builtins"


def load_all() -> list[Playbook]:
    """Load all playbooks from builtins dir + optional PLAYBOOK_DIR."""
    playbooks = []

    # Built-in playbooks
    playbooks.extend(_load_dir(BUILTINS_DIR))

    # User-defined playbooks (optional)
    custom_dir = os.getenv("PLAYBOOK_DIR")
    if custom_dir:
        custom_path = Path(custom_dir)
        if custom_path.is_dir():
            playbooks.extend(_load_dir(custom_path))
            logger.info("Loaded %d custom playbooks from %s",
                        len(playbooks), custom_dir)
        else:
            logger.warning("PLAYBOOK_DIR=%s does not exist, skipping", custom_dir)

    return playbooks


def _load_dir(directory: Path) -> list[Playbook]:
    """Load all .yaml/.yml files from a directory."""
    playbooks = []
    if not directory.is_dir():
        return playbooks

    for path in sorted(directory.glob("*.y*ml")):
        try:
            pb = load_file(path)
            playbooks.append(pb)
            logger.debug("Loaded playbook: %s from %s", pb.name, path.name)
        except Exception as e:
            logger.warning("Failed to load playbook %s: %s", path.name, e)

    return playbooks


def load_file(path: Path) -> Playbook:
    """Parse a single YAML file into a Playbook."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: expected a YAML mapping, got {type(raw).__name__}")

    return _parse_playbook(raw, source=path.name)


def _parse_playbook(data: dict[str, Any], source: str = "") -> Playbook:
    """Convert a raw YAML dict into a Playbook dataclass."""
    name = data.get("name")
    if not name:
        raise ValueError(f"{source}: playbook must have a 'name' field")

    triggers = [
        PlaybookTrigger(
            type=t.get("type", "status"),
            pattern=t.get("pattern", ""),
        )
        for t in data.get("triggers", [])
    ]

    params = [
        PlaybookParam(
            name=p.get("name", ""),
            required=p.get("required", False),
            default=p.get("default", ""),
            description=p.get("description", ""),
        )
        for p in data.get("params", [])
    ]

    steps = [
        PlaybookStep(
            tool=s.get("tool", ""),
            params=s.get("params", {}),
            store_as=s.get("store_as", ""),
            condition=s.get("condition", ""),
            on_error=s.get("on_error", "continue"),
        )
        for s in data.get("steps", [])
    ]

    synthesis = data.get("synthesis", {})
    synthesis_prompt = synthesis if isinstance(synthesis, str) else synthesis.get("prompt", "")

    return Playbook(
        name=name,
        description=data.get("description", ""),
        triggers=triggers,
        params=params,
        steps=steps,
        synthesis_prompt=synthesis_prompt,
        category=data.get("category", "general"),
        severity_hint=data.get("severity_hint", ""),
        tags=data.get("tags", []),
    )
