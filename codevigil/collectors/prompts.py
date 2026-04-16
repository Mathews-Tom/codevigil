"""Prompts-per-session collector.

Counts user-message turns. The primary scalar is the cumulative count of
``EventKind.USER_MESSAGE`` events observed in the session. Cohort
aggregation then averages this across sessions to produce the per-week
mean used in issue #42796 ('prompts per session', 35.9 → 27.9).

Severity is always OK — there is no validated threshold for "too few
prompts" and inventing one would violate the project's claim discipline.
"""

from __future__ import annotations

from typing import Any

from codevigil.collectors import COLLECTORS, register_collector
from codevigil.config import CONFIG_DEFAULTS
from codevigil.types import Event, EventKind, MetricSnapshot, Severity


class PromptsCollector:
    name: str = "prompts"
    complexity: str = "O(1) per ingest"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else _default_config()
        self._experimental: bool = bool(cfg["experimental"])
        self._user_turns: int = 0

    def ingest(self, event: Event) -> None:
        if event.kind is EventKind.USER_MESSAGE:
            self._user_turns += 1

    def snapshot(self) -> MetricSnapshot:
        detail: dict[str, Any] = {"user_turns": self._user_turns}
        if self._experimental:
            detail["experimental"] = True
        return MetricSnapshot(
            name=self.name,
            value=float(self._user_turns),
            label=f"{self._user_turns} user turns",
            severity=Severity.OK,
            detail=detail,
        )

    def reset(self) -> None:
        self._user_turns = 0

    def serialize_state(self) -> dict[str, Any]:
        return {"user_turns": self._user_turns}

    def restore_state(self, state: dict[str, Any]) -> None:
        self._user_turns = int(state.get("user_turns", 0))


def _default_config() -> dict[str, Any]:
    return dict(CONFIG_DEFAULTS["collectors"]["prompts"])


register_collector(COLLECTORS, PromptsCollector)


__all__ = ["PromptsCollector"]
