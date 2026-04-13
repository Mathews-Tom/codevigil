"""Word-boundary regression tests for StopPhraseCollector.

The classic failure mode is ``"should I"`` matching inside ``"shoulder"``
or ``"actually"`` firing on ``"factually"``. The default phrase list uses
word-boundary mode, so these cases must stay silent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from codevigil.collectors.stop_phrase import StopPhraseCollector
from codevigil.types import Event, EventKind


def _msg(text: str) -> Event:
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s",
        kind=EventKind.ASSISTANT_MESSAGE,
        payload={"text": text},
    )


def test_should_i_does_not_match_shoulder() -> None:
    c = StopPhraseCollector(
        {
            "custom_phrases": [],
            "warn_threshold": 1.0,
            "critical_threshold": 3.0,
            "experimental": True,
        }
    )
    c.ingest(_msg("My shoulder is sore from typing all day."))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["hits"] == 0


def test_should_i_continue_matches_with_space() -> None:
    c = StopPhraseCollector(
        {
            "custom_phrases": [],
            "warn_threshold": 1.0,
            "critical_threshold": 3.0,
            "experimental": True,
        }
    )
    c.ingest(_msg("Should I continue applying these patches?"))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["hits"] == 1


def test_pre_existing_does_not_match_inside_other_words() -> None:
    c = StopPhraseCollector(
        {
            "custom_phrases": [],
            "warn_threshold": 1.0,
            "critical_threshold": 3.0,
            "experimental": True,
        }
    )
    # "pre-existing" contains a hyphen which is non-word, so it should
    # still fire when surrounded by spaces but not when glued to letters.
    c.ingest(_msg("This is a pre-existing failure."))
    c.ingest(_msg("apre-existingb glued context"))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["hits"] == 1


def test_out_of_scope_does_not_match_outscope() -> None:
    c = StopPhraseCollector(
        {
            "custom_phrases": [],
            "warn_threshold": 1.0,
            "critical_threshold": 3.0,
            "experimental": True,
        }
    )
    c.ingest(_msg("This is outscope of the refactor."))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["hits"] == 0
