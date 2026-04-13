"""Streaming JSONL parser for Claude Code session files.

Public API: ``SessionParser``. Construct one per session file, feed an
iterable of raw lines through ``parse(lines)``, and consume the yielded
``Event`` objects lazily. After iteration completes the ``stats`` property
exposes the per-session :class:`ParseStats` snapshot the
``ParseHealthCollector`` reads to compute drift severity.

Design choices

* The parser is implemented as a class rather than a free function so the
  per-session bookkeeping (parse_confidence counters, schema fingerprint
  sampler de-dup, unknown-tool de-dup) has an obvious home and the
  collector wiring is a constructor argument instead of module-global
  state.
* ``parse(lines)`` is a generator: memory is O(1) in the number of input
  lines, verified by ``tests/test_parser_streaming.py``.
* Drift signals reach the ``ParseHealthCollector`` via the shared
  :class:`ParseStats` instance, not via sentinel values stuffed into event
  payloads. The collector receives the same instance through its
  constructor and reads ``parse_confidence`` on every snapshot.
* ``safe_get`` is intentionally NOT used for the per-line drift counters:
  it routes drift through the error channel which the collector cannot
  observe directly. Instead the parser maintains its own counter on
  :class:`ParseStats` for every required field it pulls.

The Event payload schemas produced by this parser are the authoritative
implementation of the table in ``docs/design.md`` §Payload Schemas by
EventKind. The schema is frozen after this PR — additive changes only.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from codevigil.errors import (
    CodevigilError,
    ErrorLevel,
    ErrorSource,
    record,
)
from codevigil.types import Event, EventKind

# ---------------------------------------------------------------------------
# Tool name canonicalisation
# ---------------------------------------------------------------------------

#: Canonicalisation table from raw Claude Code tool names to the lowercase
#: snake_case identifiers the rest of the pipeline uses. Unknown raw names are
#: passed through verbatim and trigger a one-time INFO via the error channel.
TOOL_ALIASES: dict[str, str] = {
    "Bash": "bash",
    "bash_tool": "bash",
    "BashTool": "bash",
    "Read": "read",
    "View": "read",
    "ReadFile": "read",
    "Edit": "edit",
    "EditFile": "edit",
    "MultiEdit": "multi_edit",
    "Write": "write",
    "WriteFile": "write",
    "Glob": "glob",
    "Grep": "grep",
    "GrepTool": "grep",
    "LS": "ls",
    "ListDirectory": "ls",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "TodoWrite": "todo_write",
    "Task": "task",
    "NotebookEdit": "notebook_edit",
}


def canonicalise_tool_name(raw: str) -> str:
    """Return the canonical snake_case form of a tool name.

    Unknown names fall through unchanged. Callers wanting the
    "unknown tool, log once" behaviour use :class:`SessionParser` which
    consults this table and de-duplicates the warnings per parse run.
    """

    return TOOL_ALIASES.get(raw, raw)


# ---------------------------------------------------------------------------
# Schema fingerprinting
# ---------------------------------------------------------------------------

#: Known schema epochs keyed by fingerprint hash. Seeded with the shape of the
#: synthetic happy-path session used by the tests so the v0.1 happy path stays
#: silent. New entries are committed as Claude Code's wire format evolves.
KNOWN_FINGERPRINTS: dict[str, str] = {}


_Fingerprint = tuple[tuple[str, ...], tuple[tuple[str, str], ...]]


def _line_fingerprint(parsed: dict[str, Any]) -> _Fingerprint:
    """Return the structural fingerprint tuple for one parsed JSON line."""

    keys = tuple(sorted(parsed.keys()))
    typed = tuple(sorted((k, type(v).__name__) for k, v in parsed.items()))
    return keys, typed


def _fingerprint_hash(fp: _Fingerprint) -> str:
    payload = json.dumps(fp, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# Seed KNOWN_FINGERPRINTS with realistic shapes. These mirror the synthetic
# happy-path fixture used in tests/test_parser_happy_path.py and the modern
# Claude Code session format observed in the wild.
def _seed_known_fingerprints() -> None:
    samples: list[tuple[dict[str, Any], str]] = [
        (
            {"type": "assistant", "timestamp": "", "session_id": "", "message": {}},
            "2026-03-claude-code",
        ),
        (
            {"type": "user", "timestamp": "", "session_id": "", "message": {}},
            "2026-03-claude-code",
        ),
        (
            {"type": "system", "timestamp": "", "session_id": "", "subtype": ""},
            "2026-03-claude-code",
        ),
    ]
    for sample, epoch in samples:
        KNOWN_FINGERPRINTS[_fingerprint_hash(_line_fingerprint(sample))] = epoch


_seed_known_fingerprints()

# Number of leading lines to fingerprint back-to-back at the start of
# every session, so the common case (Claude Code schema change between
# releases) fires within the first handful of lines. After this initial
# burst the parser continues to sample every ``_FINGERPRINT_RESAMPLE_STRIDE``
# lines forever so mid-session schema drift — a hypothetical rolling
# deploy that swaps the writer halfway through a session — is still
# caught. The unknown-fingerprint warning fires once per unique
# fingerprint, not once per session, so each distinct drift shape is
# reported exactly once.
_FINGERPRINT_INITIAL_BURST: int = 10
_FINGERPRINT_RESAMPLE_STRIDE: int = 500


# ---------------------------------------------------------------------------
# Parse statistics shared with ParseHealthCollector
# ---------------------------------------------------------------------------


# Rolling-window size for ``ParseStats.parse_confidence``. The window
# holds one bool per observed line: True if at least one event was
# emitted from that line, False if the line was malformed or produced
# no events. 200 is roughly 4x the parse_health collector's own
# ``_WINDOW_SIZE`` (50), so the rolling confidence stabilises quickly
# but still reflects late-session drift that session-wide ratios hide.
_ROLLING_CONFIDENCE_WINDOW: int = 200


@dataclass
class ParseStats:
    """Mutable per-session counters the parser updates on every line.

    The :class:`ParseHealthCollector` receives the same instance via its
    constructor and reads :attr:`parse_confidence` on every snapshot, which
    is how drift detection bridges from the parser to the collector without
    routing through the global error channel.

    ``parse_confidence`` is a *rolling* ratio over the last
    ``_ROLLING_CONFIDENCE_WINDOW`` observed lines. An earlier
    implementation used a session-wide ratio and masked late-session
    drift: a session with 1000 clean leading lines and a final 100
    corrupt lines still reported 0.91 confidence, above the 0.9
    CRITICAL threshold. The rolling window flips CRITICAL within the
    window as soon as the drift rate inside it crosses the threshold.
    A session-wide ratio is still exposed as :attr:`session_confidence`
    for callers that want the historical semantic.
    """

    total_lines: int = 0
    parsed_events: int = 0
    missing_fields: dict[str, int] = field(default_factory=dict)
    _line_outcomes: deque[bool] = field(
        default_factory=lambda: deque(maxlen=_ROLLING_CONFIDENCE_WINDOW)
    )

    def record_missing(self, field_name: str) -> None:
        self.missing_fields[field_name] = self.missing_fields.get(field_name, 0) + 1

    def record_line_outcome(self, *, parsed: bool) -> None:
        """Append a parseable/unparseable outcome to the rolling window.

        The parser calls this once per observed line — True when the
        line produced at least one event, False when it was malformed
        or silently dropped.
        """

        self._line_outcomes.append(parsed)

    @property
    def parse_confidence(self) -> float:
        """Rolling ratio of parseable lines in the trailing window.

        Returns ``1.0`` for an empty session so a freshly-constructed
        collector reports OK rather than flapping CRITICAL on zero data.
        """

        if not self._line_outcomes:
            return 1.0
        hits = sum(1 for outcome in self._line_outcomes if outcome)
        return hits / len(self._line_outcomes)

    @property
    def session_confidence(self) -> float:
        """Session-wide ratio of parsed events to total lines.

        Kept as a separate read-only signal for callers (reports,
        tests) that want the historical cumulative value.
        """

        if self.total_lines == 0:
            return 1.0
        return min(1.0, self.parsed_events / self.total_lines)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class SessionParser:
    """Streaming Claude Code session parser.

    One instance per session file. ``parse(lines)`` is a generator and may
    be consumed once. After iteration, :attr:`stats` holds the per-session
    counters and :attr:`fingerprint_warned` records whether an
    unknown-fingerprint WARN has already been emitted for this run (the
    parser emits exactly one such warning per session).
    """

    def __init__(self, *, session_id: str = "unknown") -> None:
        self._session_id: str = session_id
        self._stats: ParseStats = ParseStats()
        self._unknown_tools_seen: set[str] = set()
        # Tracks whether the parser has ever emitted an unknown
        # fingerprint WARN for this session. Exposed on the property of
        # the same name for backwards compatibility with tests that
        # asserted one-warning-per-session; the actual dedup key is the
        # per-fingerprint set below.
        self._fingerprint_warned: bool = False
        self._fingerprints_warned: set[str] = set()
        self._lines_fingerprinted: int = 0

    @property
    def stats(self) -> ParseStats:
        return self._stats

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def fingerprint_warned(self) -> bool:
        return self._fingerprint_warned

    def parse(self, lines: Iterable[str]) -> Iterator[Event]:
        """Yield :class:`Event` objects for every parseable line.

        Malformed JSON, JSON without a ``type`` field, and JSON with an
        unknown ``type`` value are logged via the error channel and
        skipped. The parser never raises on per-line errors. Every
        observed line (including skipped ones) is recorded in the
        rolling parse-confidence window so late-session drift is not
        masked by a clean early prefix.
        """

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            self._stats.total_lines += 1

            parsed = self._decode_line(line)
            if parsed is None:
                self._stats.record_line_outcome(parsed=False)
                continue

            self._sample_fingerprint(parsed)

            kind_field = parsed.get("type")
            if not isinstance(kind_field, str):
                self._stats.record_missing("type")
                self._stats.record_line_outcome(parsed=False)
                record(
                    CodevigilError(
                        level=ErrorLevel.WARN,
                        source=ErrorSource.PARSER,
                        code="parser.missing_type",
                        message="line missing top-level 'type' field",
                        context={"session_id": self._session_id},
                    )
                )
                continue

            before = self._stats.parsed_events
            yield from self._dispatch(parsed, kind_field)
            # ``_dispatch`` increments ``parsed_events`` for every event
            # it emits. If the counter moved forward we treat the line
            # as parseable; otherwise it silently failed (unknown type,
            # missing message body, etc.) and contributes negatively to
            # the rolling confidence window.
            self._stats.record_line_outcome(parsed=self._stats.parsed_events > before)

    # ------------------------------------------------------------------
    # Line-level helpers
    # ------------------------------------------------------------------

    def _decode_line(self, line: str) -> dict[str, Any] | None:
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.PARSER,
                    code="parser.malformed_line",
                    message=f"failed to decode JSONL line: {exc.msg}",
                    context={
                        "session_id": self._session_id,
                        "position": exc.pos,
                    },
                )
            )
            self._stats.record_missing("__json__")
            return None
        if not isinstance(decoded, dict):
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.PARSER,
                    code="parser.malformed_line",
                    message=(
                        f"top-level JSONL value is not an object; got {type(decoded).__name__}"
                    ),
                    context={"session_id": self._session_id},
                )
            )
            self._stats.record_missing("__object__")
            return None
        return decoded

    def _sample_fingerprint(self, parsed: dict[str, Any]) -> None:
        # Sampling cadence: fingerprint every line during the initial
        # burst, then every ``_FINGERPRINT_RESAMPLE_STRIDE`` lines for
        # the rest of the session. The line counter is monotonic across
        # the whole session so the stride check is stable regardless of
        # how many lines we've already fingerprinted.
        line_number = self._stats.total_lines
        in_initial_burst = self._lines_fingerprinted < _FINGERPRINT_INITIAL_BURST
        on_resample_tick = line_number > 0 and line_number % _FINGERPRINT_RESAMPLE_STRIDE == 0
        if not in_initial_burst and not on_resample_tick:
            return
        self._lines_fingerprinted += 1
        fp = _line_fingerprint(parsed)
        digest = _fingerprint_hash(fp)
        if digest in KNOWN_FINGERPRINTS:
            return
        if digest in self._fingerprints_warned:
            # We've already reported this exact shape for this session.
            # Stay quiet rather than spamming every 500 lines.
            return
        self._fingerprints_warned.add(digest)
        self._fingerprint_warned = True
        record(
            CodevigilError(
                level=ErrorLevel.WARN,
                source=ErrorSource.PARSER,
                code="parser.unknown_fingerprint",
                message=(
                    "observed JSONL line shape not in KNOWN_FINGERPRINTS; "
                    "Claude Code session schema may have changed"
                ),
                context={
                    "session_id": self._session_id,
                    "fingerprint": digest,
                    "keys": list(fp[0]),
                    "line_number": line_number,
                },
            )
        )

    # ------------------------------------------------------------------
    # Dispatch and per-kind extraction
    # ------------------------------------------------------------------

    def _dispatch(self, parsed: dict[str, Any], kind_field: str) -> Iterator[Event]:
        timestamp = self._extract_timestamp(parsed)
        session_id = self._extract_session_id(parsed)

        if kind_field == "assistant":
            yield from self._emit_assistant(parsed, timestamp, session_id)
        elif kind_field == "user":
            yield from self._emit_user(parsed, timestamp, session_id)
        elif kind_field == "system":
            yield from self._emit_system(parsed, timestamp, session_id)
        else:
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.PARSER,
                    code="parser.unknown_type",
                    message=f"unknown top-level type {kind_field!r}",
                    context={
                        "session_id": self._session_id,
                        "type": kind_field,
                    },
                )
            )
            self._stats.record_missing("type")

    def _extract_timestamp(self, parsed: dict[str, Any]) -> datetime:
        raw = parsed.get("timestamp")
        if isinstance(raw, str) and raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                self._stats.record_missing("timestamp")
        elif raw is None:
            self._stats.record_missing("timestamp")
        return datetime.now(tz=UTC)

    def _extract_session_id(self, parsed: dict[str, Any]) -> str:
        raw = parsed.get("session_id")
        if isinstance(raw, str) and raw:
            return raw
        return self._session_id

    def _content_blocks(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        content = message.get("content")
        if isinstance(content, list):
            return [block for block in content if isinstance(block, dict)]
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        return []

    def _emit_assistant(
        self,
        parsed: dict[str, Any],
        timestamp: datetime,
        session_id: str,
    ) -> Iterator[Event]:
        message = parsed.get("message")
        if not isinstance(message, dict):
            self._stats.record_missing("message")
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.PARSER,
                    code="parser.missing_message",
                    message="assistant line missing 'message' object",
                    context={"session_id": session_id},
                )
            )
            return

        emitted = 0
        for block in self._content_blocks(message):
            block_type = block.get("type")
            if block_type == "text":
                event = self._build_assistant_text_event(block, message, timestamp, session_id)
                if event is not None:
                    emitted += 1
                    yield event
            elif block_type == "tool_use":
                event = self._build_tool_call_event(block, timestamp, session_id)
                if event is not None:
                    emitted += 1
                    yield event
            elif block_type == "thinking":
                event = self._build_thinking_event(block, timestamp, session_id)
                if event is not None:
                    emitted += 1
                    yield event
            else:
                self._stats.record_missing("content.type")

        if emitted == 0:
            # Nothing useful in the line: still count it as parsed so a
            # lone "assistant with no content" doesn't masquerade as drift.
            self._stats.parsed_events += 1
        else:
            self._stats.parsed_events += emitted

    def _build_assistant_text_event(
        self,
        block: dict[str, Any],
        message: dict[str, Any],
        timestamp: datetime,
        session_id: str,
    ) -> Event | None:
        text = block.get("text")
        if not isinstance(text, str):
            self._stats.record_missing("text")
            return None
        payload: dict[str, Any] = {"text": text}
        token_count = self._extract_token_count(message)
        if token_count is not None:
            payload["token_count"] = token_count
        return Event(
            timestamp=timestamp,
            session_id=session_id,
            kind=EventKind.ASSISTANT_MESSAGE,
            payload=payload,
        )

    def _extract_token_count(self, message: dict[str, Any]) -> int | None:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        out = usage.get("output_tokens")
        if isinstance(out, int):
            return out
        return None

    def _build_tool_call_event(
        self,
        block: dict[str, Any],
        timestamp: datetime,
        session_id: str,
    ) -> Event | None:
        raw_name = block.get("name")
        tool_use_id = block.get("id")
        tool_input = block.get("input")
        if not isinstance(raw_name, str):
            self._stats.record_missing("tool_name")
            return None
        if not isinstance(tool_use_id, str):
            self._stats.record_missing("tool_use_id")
            return None
        if not isinstance(tool_input, dict):
            self._stats.record_missing("input")
            tool_input = {}
        canonical = canonicalise_tool_name(raw_name)
        if canonical == raw_name and raw_name not in TOOL_ALIASES.values():
            self._note_unknown_tool(raw_name)
        payload: dict[str, Any] = {
            "tool_name": canonical,
            "tool_use_id": tool_use_id,
            "input": dict(tool_input),
        }
        file_path = tool_input.get("file_path")
        if isinstance(file_path, str):
            payload["file_path"] = file_path
        return Event(
            timestamp=timestamp,
            session_id=session_id,
            kind=EventKind.TOOL_CALL,
            payload=payload,
        )

    def _note_unknown_tool(self, raw_name: str) -> None:
        if raw_name in self._unknown_tools_seen:
            return
        self._unknown_tools_seen.add(raw_name)
        record(
            CodevigilError(
                level=ErrorLevel.INFO,
                source=ErrorSource.PARSER,
                code="parser.unknown_tool",
                message=f"unrecognised tool name {raw_name!r}; passing through verbatim",
                context={
                    "session_id": self._session_id,
                    "tool_name": raw_name,
                },
            )
        )

    def _build_thinking_event(
        self,
        block: dict[str, Any],
        timestamp: datetime,
        session_id: str,
    ) -> Event | None:
        raw_text = block.get("thinking")
        signature = block.get("signature")
        payload: dict[str, Any]
        if raw_text == "[redacted]" or block.get("redacted") is True:
            payload = {
                "length": 0,
                "redacted": True,
                "text": "",
            }
        elif isinstance(raw_text, str):
            payload = {
                "length": len(raw_text),
                "redacted": False,
                "text": raw_text,
            }
        else:
            self._stats.record_missing("thinking")
            return None
        if isinstance(signature, str):
            payload["signature"] = signature
        return Event(
            timestamp=timestamp,
            session_id=session_id,
            kind=EventKind.THINKING,
            payload=payload,
        )

    def _emit_user(
        self,
        parsed: dict[str, Any],
        timestamp: datetime,
        session_id: str,
    ) -> Iterator[Event]:
        message = parsed.get("message")
        if not isinstance(message, dict):
            self._stats.record_missing("message")
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.PARSER,
                    code="parser.missing_message",
                    message="user line missing 'message' object",
                    context={"session_id": session_id},
                )
            )
            return

        blocks = self._content_blocks(message)
        emitted = 0
        if not blocks:
            text = message.get("content") if isinstance(message.get("content"), str) else None
            if isinstance(text, str):
                yield Event(
                    timestamp=timestamp,
                    session_id=session_id,
                    kind=EventKind.USER_MESSAGE,
                    payload={"text": text},
                )
                emitted += 1
            else:
                self._stats.record_missing("text")

        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    self._stats.record_missing("text")
                    continue
                yield Event(
                    timestamp=timestamp,
                    session_id=session_id,
                    kind=EventKind.USER_MESSAGE,
                    payload={"text": text},
                )
                emitted += 1
            elif block_type == "tool_result":
                event = self._build_tool_result_event(block, timestamp, session_id)
                if event is not None:
                    yield event
                    emitted += 1
            else:
                self._stats.record_missing("content.type")

        if emitted == 0:
            self._stats.parsed_events += 1
        else:
            self._stats.parsed_events += emitted

    def _build_tool_result_event(
        self,
        block: dict[str, Any],
        timestamp: datetime,
        session_id: str,
    ) -> Event | None:
        tool_use_id = block.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            self._stats.record_missing("tool_use_id")
            return None
        is_error = bool(block.get("is_error", False))
        payload: dict[str, Any] = {
            "tool_use_id": tool_use_id,
            "is_error": is_error,
        }
        content = block.get("content")
        if isinstance(content, str):
            payload["output"] = content
        elif isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            payload["output"] = "\n".join(parts)
        if "truncated" in block:
            payload["truncated"] = bool(block["truncated"])
        return Event(
            timestamp=timestamp,
            session_id=session_id,
            kind=EventKind.TOOL_RESULT,
            payload=payload,
        )

    def _emit_system(
        self,
        parsed: dict[str, Any],
        timestamp: datetime,
        session_id: str,
    ) -> Iterator[Event]:
        subkind_raw = parsed.get("subtype") or parsed.get("subkind") or "unknown"
        subkind = subkind_raw if isinstance(subkind_raw, str) else "unknown"
        payload: dict[str, Any] = {"subkind": subkind}
        for key, value in parsed.items():
            if key in {"type", "timestamp", "session_id", "subtype", "subkind"}:
                continue
            payload[key] = value
        self._stats.parsed_events += 1
        yield Event(
            timestamp=timestamp,
            session_id=session_id,
            kind=EventKind.SYSTEM,
            payload=payload,
        )


def parse_session(lines: Iterable[str], *, session_id: str = "unknown") -> Iterator[Event]:
    """Convenience function for callers that don't need the stats handle.

    Wraps :class:`SessionParser` so the simple "iterate events" use case
    stays a one-liner. Callers that want :class:`ParseStats` (notably the
    aggregator wiring up :class:`ParseHealthCollector`) construct the
    parser explicitly instead.
    """

    parser = SessionParser(session_id=session_id)
    return parser.parse(lines)


__all__ = [
    "KNOWN_FINGERPRINTS",
    "TOOL_ALIASES",
    "ParseStats",
    "SessionParser",
    "canonicalise_tool_name",
    "parse_session",
]
