"""parse_session is O(1) memory in line count: confirm streaming semantics."""

from __future__ import annotations

import json
from collections.abc import Iterator

from codevigil.parser import SessionParser


def _line_generator(count: int) -> Iterator[str]:
    for i in range(count):
        yield json.dumps(
            {
                "type": "user",
                "timestamp": "2026-04-13T12:00:00+00:00",
                "session_id": "sess-1",
                "message": {"content": [{"type": "text", "text": f"msg-{i}"}]},
            }
        )


def test_parse_returns_lazy_iterator() -> None:
    parser = SessionParser(session_id="sess-1")
    result = parser.parse(_line_generator(10_000))
    assert isinstance(result, Iterator)
    # The source is a generator that has not been exhausted yet — if the
    # parser had eagerly materialised the input, stats.total_lines would
    # already be 10000. It must be 0 until we start pulling from result.
    assert parser.stats.total_lines == 0

    consumed = 0
    for _event in result:
        consumed += 1
        if consumed >= 10_000:
            break
    assert consumed == 10_000
    assert parser.stats.total_lines == 10_000


def test_parser_does_not_materialise_full_input() -> None:
    """Sentinel: feed an infinite generator and break after a fixed count.

    A non-streaming implementation would hang forever or OOM here. The
    fact that the test completes proves the parser pulls lines lazily.
    """

    def infinite() -> Iterator[str]:
        i = 0
        while True:
            yield json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-04-13T12:00:00+00:00",
                    "session_id": "sess-1",
                    "message": {"content": [{"type": "text", "text": f"msg-{i}"}]},
                }
            )
            i += 1

    parser = SessionParser(session_id="sess-1")
    pulled = 0
    for _event in parser.parse(infinite()):
        pulled += 1
        if pulled >= 100:
            break
    assert pulled == 100
