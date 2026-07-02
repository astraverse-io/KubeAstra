from __future__ import annotations

from alerts.domain.playbook import Playbook
from alerts.playbooks.loader import PlaybookLoader


class PlaybookRegistry:
    def __init__(self, loader: PlaybookLoader) -> None:
        self._loader = loader
        self._playbooks: dict[str, Playbook] = {}
        self.refresh()

    def refresh(self) -> None:
        self._playbooks = {playbook.id: playbook for playbook in self._loader.load_all()}

    def list(self) -> list[Playbook]:
        return list(self._playbooks.values())

    def get(self, playbook_id: str) -> Playbook:
        if playbook_id not in self._playbooks:
            raise KeyError(f"Unknown playbook: {playbook_id}")
        return self._playbooks[playbook_id]

    def upsert(self, playbook: Playbook) -> None:
        self._playbooks[playbook.id] = playbook
