"""Turn abstraction for the aggregator.

A *turn* is the unit of conversational context: one user message followed by
the assistant's complete response (thinking blocks, tool calls, tool results,
and the final assistant message), up to the next user message or session close.

:class:`Turn` is an immutable snapshot of a completed turn. It is populated by
:class:`TurnGrouper` as a sidecar inside ``_SessionContext`` and exposed to
the classifier (Phase 5) via ``_SessionContext.completed_turns``. Collectors
receive raw :class:`~codevigil.types.Event` objects as before; they do not
consume :class:`Turn`.

When Phase 5 attaches a ``task_type`` to a completed turn, it will use
``dataclasses.replace(turn, task_type=...)``. The frozen constraint enforces
that no in-place mutation happens between turn creation here and classification
there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from codevigil.types import Event, EventKind


@dataclass(frozen=True, slots=True)
class Turn:
    """Immutable snapshot of a single completed conversational turn.

    Fields
    ------
    session_id
        Copied from the opening user-message event.
    started_at
        Timestamp of the opening user-message event.
    ended_at
        Timestamp of the last event in this turn (just before the next user
        message, or the final event on session close).
    user_message_text
        The text content of the user message that opened this turn. May be an
        empty string when the user message carries no text block (e.g. a
        tool-result-only message).
    tool_calls
        Canonical tool names (from ``TOOL_ALIASES``) in the order they were
        first observed inside this turn. Populated from
        ``EventKind.TOOL_CALL`` events.
    event_count
        Total number of events that belong to this turn (including the opening
        user message).
    task_type
        Classification label assigned by Phase 5. ``None`` until the classifier
        runs (or when the classifier is disabled). Use
        ``dataclasses.replace(turn, task_type=label)`` to produce a classified
        copy — never mutate in place.
    """

    session_id: str
    started_at: datetime
    ended_at: datetime
    user_message_text: str
    tool_calls: tuple[str, ...]
    event_count: int
    task_type: str | None = None


class TurnGrouper:
    """State machine that groups events into :class:`Turn` objects.

    One :class:`TurnGrouper` lives inside each ``_SessionContext``. The
    aggregator calls :meth:`ingest` for every event, in order, after fanning
    the same event to collectors. When a turn boundary is detected (arrival of
    a new ``USER_MESSAGE`` after at least one prior event, or a call to
    :meth:`finalize`), a completed :class:`Turn` is returned.

    State invariant
    ---------------
    When ``_in_turn`` is ``True`` the grouper has accumulated at least one
    event (the opening user message). When ``_in_turn`` is ``False`` no events
    have been observed yet or the previous turn was emitted and the grouper is
    waiting for the next user message.

    The grouper does NOT classify turns; that is Phase 5's responsibility.
    ``task_type`` is always ``None`` on emitted turns.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id: str = session_id
        self._in_turn: bool = False
        self._started_at: datetime | None = None
        self._last_event_time: datetime | None = None
        self._user_message_text: str = ""
        self._tool_calls: list[str] = []
        self._event_count: int = 0

    def ingest(self, event: Event) -> Turn | None:
        """Consume one event and return a completed :class:`Turn` when a boundary is crossed.

        A boundary occurs when a ``USER_MESSAGE`` event arrives while the
        grouper already has an open turn. In that case the open turn is
        finalised and returned, then the new user message opens the next turn.

        Returns ``None`` for all non-boundary events.
        """
        if event.kind is EventKind.USER_MESSAGE:
            completed: Turn | None = None
            if self._in_turn:
                # Boundary: close the current turn before opening the next.
                completed = self._close_turn(ended_at=event.timestamp)
            self._open_turn(event)
            return completed

        if not self._in_turn:
            # Non-user events before the first user message are ignored.
            return None

        # Accumulate tool call names in order.
        if event.kind is EventKind.TOOL_CALL:
            tool_name: str = event.payload.get("tool_name", "")
            if tool_name:
                self._tool_calls.append(tool_name)

        self._last_event_time = event.timestamp
        self._event_count += 1
        return None

    def finalize(self) -> Turn | None:
        """Close any in-progress turn and return it.

        Called by the aggregator on session eviction to ensure the final turn
        of a session is captured even when no subsequent user message arrives.

        Returns ``None`` when no turn was in progress.
        """
        if not self._in_turn:
            return None
        ended_at = self._last_event_time
        assert ended_at is not None  # _in_turn implies at least one event
        return self._close_turn(ended_at=ended_at)

    # ---------------------------------------------------------------------- internals

    def _open_turn(self, user_event: Event) -> None:
        """Start a new turn from a USER_MESSAGE event.

        The parser emits one ``USER_MESSAGE`` event per text block (payload
        key ``"text"``) and one per tool-result block. Only text-bearing
        events carry the user prompt; we extract from ``payload["text"]``
        directly since that is the canonical parser output shape.
        """
        self._in_turn = True
        self._started_at = user_event.timestamp
        self._last_event_time = user_event.timestamp
        self._event_count = 1
        self._tool_calls = []
        # The parser puts the raw text in payload["text"] for text-type user
        # messages. tool_result events share the USER_MESSAGE kind but carry
        # no "text" key; we treat those as empty-text openers.
        raw_text = user_event.payload.get("text", "")
        self._user_message_text = raw_text if isinstance(raw_text, str) else ""

    def _close_turn(self, *, ended_at: datetime) -> Turn:
        """Emit a completed :class:`Turn` and reset internal state."""
        assert self._started_at is not None
        turn = Turn(
            session_id=self._session_id,
            started_at=self._started_at,
            ended_at=ended_at,
            user_message_text=self._user_message_text,
            tool_calls=tuple(self._tool_calls),
            event_count=self._event_count,
            task_type=None,
        )
        self._in_turn = False
        self._started_at = None
        self._last_event_time = None
        self._user_message_text = ""
        self._tool_calls = []
        self._event_count = 0
        return turn


__all__ = ["Turn", "TurnGrouper"]
