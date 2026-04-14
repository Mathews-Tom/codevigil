"""Tests for the two pre-v1 historical JSONL shapes in the schema-drift corpus.

Each test asserts that the corresponding fixture file reaches
parse_confidence >= 0.9 after the parser learns the additional fingerprints.
The modern 2026-03 shape (pre_v1_no_timestamp) is also verified to confirm
no regression on the already-working shape.

Fixtures live in tests/fixtures/parser_schema_drift/ and were committed
during the pre-flight corpus work (the fixture-corpus branch).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.parser import SessionParser
from codevigil.types import EventKind

_FIXTURES = Path(__file__).parent / "fixtures" / "parser_schema_drift"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(fixture_name: str) -> tuple[SessionParser, list]:
    path = _FIXTURES / fixture_name
    parser = SessionParser(session_id=path.stem)
    events = list(parser.parse(path.read_text(encoding="utf-8").splitlines()))
    return parser, events


# ---------------------------------------------------------------------------
# Shape 1: pre_v1_no_type — role/ts/session/content keys
# ---------------------------------------------------------------------------


class TestPreV1RoleTsSessionShape:
    """Lines use 'role'/'ts'/'session'/'content' instead of modern keys."""

    def test_parse_confidence_meets_threshold(self) -> None:
        parser, _ = _parse("pre_v1_no_type.jsonl")
        assert parser.stats.parse_confidence >= 0.9

    def test_all_lines_produce_events(self) -> None:
        parser, events = _parse("pre_v1_no_type.jsonl")
        # 8 lines in the fixture; every line should emit at least one event.
        assert len(events) == parser.stats.total_lines

    def test_emits_assistant_messages(self) -> None:
        _, events = _parse("pre_v1_no_type.jsonl")
        kinds = [e.kind for e in events]
        assert EventKind.ASSISTANT_MESSAGE in kinds

    def test_emits_tool_calls(self) -> None:
        _, events = _parse("pre_v1_no_type.jsonl")
        kinds = [e.kind for e in events]
        assert EventKind.TOOL_CALL in kinds

    def test_emits_tool_results(self) -> None:
        _, events = _parse("pre_v1_no_type.jsonl")
        kinds = [e.kind for e in events]
        assert EventKind.TOOL_RESULT in kinds

    def test_session_id_extracted_from_session_key(self) -> None:
        _, events = _parse("pre_v1_no_type.jsonl")
        # All events should carry the session value from the "session" field.
        assert all(e.session_id == "drift-pre-v1-no-type" for e in events)

    def test_timestamp_extracted_from_ts_key(self) -> None:
        _, events = _parse("pre_v1_no_type.jsonl")
        # The "ts" field holds a real ISO timestamp; events must not fall back
        # to datetime.now (which would have a different year).
        assert all(e.timestamp.year == 2025 for e in events)

    def test_tool_call_payload_structure(self) -> None:
        _, events = _parse("pre_v1_no_type.jsonl")
        tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
        assert tool_calls, "expected at least one TOOL_CALL event"
        for call in tool_calls:
            assert "tool_name" in call.payload
            assert "tool_use_id" in call.payload
            assert "input" in call.payload

    def test_tool_result_payload_structure(self) -> None:
        _, events = _parse("pre_v1_no_type.jsonl")
        results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
        assert results, "expected at least one TOOL_RESULT event"
        for result in results:
            assert "tool_use_id" in result.payload
            assert "is_error" in result.payload


# ---------------------------------------------------------------------------
# Shape 2: pre_v1_no_timestamp — modern keys but missing timestamp
# ---------------------------------------------------------------------------


class TestPreV1NoTimestampShape:
    """Modern type/session_id/message keys present but no timestamp field.

    This shape was already parsed correctly before this work. The test
    confirms no regression.
    """

    def test_parse_confidence_meets_threshold(self) -> None:
        parser, _ = _parse("pre_v1_no_timestamp.jsonl")
        assert parser.stats.parse_confidence >= 0.9

    def test_all_lines_produce_events(self) -> None:
        parser, events = _parse("pre_v1_no_timestamp.jsonl")
        assert len(events) == parser.stats.total_lines

    def test_emits_expected_kinds(self) -> None:
        _, events = _parse("pre_v1_no_timestamp.jsonl")
        kinds = {e.kind for e in events}
        assert EventKind.ASSISTANT_MESSAGE in kinds
        assert EventKind.TOOL_CALL in kinds
        assert EventKind.TOOL_RESULT in kinds


# ---------------------------------------------------------------------------
# Shape 3: pre_v1_flat_content — type/timestamp/session_id with flat fields
# ---------------------------------------------------------------------------


class TestPreV1FlatContentShape:
    """Lines carry type/timestamp/session_id but expose content as flat top-level
    keys (text, tool, tool_input, tool_result) with no message wrapper."""

    def test_parse_confidence_meets_threshold(self) -> None:
        parser, _ = _parse("pre_v1_flat_content.jsonl")
        assert parser.stats.parse_confidence >= 0.9

    def test_all_lines_produce_events(self) -> None:
        parser, events = _parse("pre_v1_flat_content.jsonl")
        assert len(events) == parser.stats.total_lines

    def test_emits_assistant_messages(self) -> None:
        _, events = _parse("pre_v1_flat_content.jsonl")
        kinds = [e.kind for e in events]
        assert EventKind.ASSISTANT_MESSAGE in kinds

    def test_emits_tool_calls(self) -> None:
        _, events = _parse("pre_v1_flat_content.jsonl")
        kinds = [e.kind for e in events]
        assert EventKind.TOOL_CALL in kinds

    def test_emits_tool_results(self) -> None:
        _, events = _parse("pre_v1_flat_content.jsonl")
        kinds = [e.kind for e in events]
        assert EventKind.TOOL_RESULT in kinds

    def test_session_id_extracted(self) -> None:
        _, events = _parse("pre_v1_flat_content.jsonl")
        assert all(e.session_id == "drift-flat-content" for e in events)

    def test_timestamp_extracted_from_timestamp_key(self) -> None:
        _, events = _parse("pre_v1_flat_content.jsonl")
        assert all(e.timestamp.year == 2025 for e in events)

    def test_assistant_text_payload(self) -> None:
        _, events = _parse("pre_v1_flat_content.jsonl")
        text_events = [e for e in events if e.kind is EventKind.ASSISTANT_MESSAGE]
        assert text_events, "expected at least one ASSISTANT_MESSAGE event"
        for evt in text_events:
            assert isinstance(evt.payload.get("text"), str)
            assert evt.payload["text"]

    def test_tool_call_payload_structure(self) -> None:
        _, events = _parse("pre_v1_flat_content.jsonl")
        tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
        assert tool_calls, "expected at least one TOOL_CALL event"
        for call in tool_calls:
            assert "tool_name" in call.payload
            assert "input" in call.payload

    def test_tool_result_payload_structure(self) -> None:
        _, events = _parse("pre_v1_flat_content.jsonl")
        results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
        assert results, "expected at least one TOOL_RESULT event"
        for result in results:
            assert "output" in result.payload
            assert result.payload["is_error"] is False


# ---------------------------------------------------------------------------
# Cross-fixture regression: no_type and flat_content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    [
        "pre_v1_no_type.jsonl",
        "pre_v1_no_timestamp.jsonl",
        "pre_v1_flat_content.jsonl",
    ],
)
def test_parse_confidence_above_threshold_for_all_drift_fixtures(
    fixture_name: str,
) -> None:
    """All three schema-drift fixtures must clear parse_confidence >= 0.9."""
    parser, _ = _parse(fixture_name)
    assert parser.stats.parse_confidence >= 0.9, (
        f"{fixture_name}: parse_confidence={parser.stats.parse_confidence:.2f} < 0.9"
    )
