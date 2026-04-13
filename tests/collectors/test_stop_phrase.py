"""Behavioural tests for StopPhraseCollector."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from codevigil.collectors.stop_phrase import StopPhraseCollector
from codevigil.types import Event, EventKind, Severity


def _msg(text: str) -> Event:
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="s",
        kind=EventKind.ASSISTANT_MESSAGE,
        payload={"text": text},
    )


def _make(**overrides: Any) -> StopPhraseCollector:
    cfg: dict[str, Any] = {
        "custom_phrases": [],
        "warn_threshold": 1.0,
        "critical_threshold": 3.0,
        "experimental": True,
    }
    cfg.update(overrides)
    return StopPhraseCollector(cfg)


def test_clean_message_no_severity() -> None:
    c = _make()
    c.ingest(_msg("Refactored the parser and added tests; gate is green."))
    snap = c.snapshot()
    assert snap.severity is Severity.OK
    assert snap.detail is not None
    assert snap.detail["hits"] == 0


def test_each_default_category_can_fire() -> None:
    c = _make()
    c.ingest(_msg("That's a pre-existing failure on main."))
    c.ingest(_msg("Should I continue with the migration?"))
    c.ingest(_msg("Looks like a good stopping point for today."))
    c.ingest(_msg("Cross-database joins are a known limitation here."))
    snap = c.snapshot()
    assert snap.detail is not None
    cats = snap.detail["hits_by_category"]
    assert "ownership_dodging" in cats
    assert "permission_seeking" in cats
    assert "premature_stopping" in cats
    assert "known_limitation" in cats


def test_warn_threshold() -> None:
    c = _make(warn_threshold=1.0, critical_threshold=10.0)
    c.ingest(_msg("Should I continue?"))
    assert c.snapshot().severity is Severity.WARN


def test_critical_threshold() -> None:
    c = _make(warn_threshold=1.0, critical_threshold=2.0)
    c.ingest(_msg("Should I continue? It's a known limitation."))
    assert c.snapshot().severity is Severity.CRITICAL


def test_recent_hits_capped_at_five() -> None:
    c = _make()
    for _ in range(8):
        c.ingest(_msg("Should I continue?"))
    snap = c.snapshot()
    assert snap.detail is not None
    assert len(snap.detail["recent_hits"]) == 5


def test_custom_phrase_string_form() -> None:
    c = _make(custom_phrases=["banana"])
    c.ingest(_msg("I added a banana to the loop."))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["hits"] == 1


def test_custom_phrase_table_form_substring_mode() -> None:
    c = _make(
        custom_phrases=[
            {"text": "foo", "mode": "substring", "category": "noise"},
        ]
    )
    c.ingest(_msg("foobar overflow"))
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["hits"] == 1
    assert snap.detail["hits_by_category"]["noise"] == 1


def test_word_mode_does_not_match_substring() -> None:
    c = _make(custom_phrases=[{"text": "foo", "mode": "word"}])
    c.ingest(_msg("foobar overflow"))
    assert c.snapshot().detail == c.snapshot().detail  # idempotent
    assert c.snapshot().detail is not None
    assert c.snapshot().detail["hits"] == 0  # type: ignore[index]


def test_reset_clears_state() -> None:
    c = _make()
    c.ingest(_msg("Should I continue?"))
    c.reset()
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["hits"] == 0
    assert snap.detail["messages"] == 0


def test_ignores_non_assistant_events() -> None:
    c = _make()
    c.ingest(
        Event(
            timestamp=datetime.now(tz=UTC),
            session_id="s",
            kind=EventKind.USER_MESSAGE,
            payload={"text": "Should I continue?"},
        )
    )
    snap = c.snapshot()
    assert snap.detail is not None
    assert snap.detail["hits"] == 0
