"""Playbook registry — in-memory index of all loaded playbooks.

Lazily loaded on first access. Thread-safe via module-level initialization.
"""

import logging
from typing import Optional

from playbooks.schema import Playbook

logger = logging.getLogger(__name__)

# ── Internal state ──────────────────────────────────────────────────────────

_playbooks: dict[str, Playbook] = {}
_loaded = False


def _ensure_loaded() -> None:
    """Load playbooks from disk on first access."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    from playbooks.loader import load_all
    for pb in load_all():
        _playbooks[pb.name] = pb

    logger.info("Playbook registry: %d playbooks loaded", len(_playbooks))


# ── Public API ──────────────────────────────────────────────────────────────

def register_playbook(playbook: Playbook) -> None:
    """Register a playbook (for programmatic / test use)."""
    _ensure_loaded()
    _playbooks[playbook.name] = playbook


def get_playbook(name: str) -> Optional[Playbook]:
    """Get a playbook by name. Returns None if not found."""
    _ensure_loaded()
    return _playbooks.get(name)


def list_playbooks() -> list[Playbook]:
    """Return all registered playbooks, sorted by name."""
    _ensure_loaded()
    return sorted(_playbooks.values(), key=lambda p: p.name)


def match_playbooks(**kwargs) -> list[Playbook]:
    """Find playbooks whose triggers match the given criteria.

    Keyword arguments are matched against trigger types:
        match_playbooks(status="CrashLoopBackOff")
        match_playbooks(category="pod_oom")
        match_playbooks(error="ImagePullBackOff: manifest not found")
        match_playbooks(event="BackOff")

    Returns matching playbooks sorted by name.
    """
    _ensure_loaded()
    matches = [pb for pb in _playbooks.values() if pb.matches(**kwargs)]
    return sorted(matches, key=lambda p: p.name)


def reload() -> None:
    """Force-reload all playbooks from disk. Useful after adding custom playbooks."""
    global _loaded
    _playbooks.clear()
    _loaded = False
    _ensure_loaded()
