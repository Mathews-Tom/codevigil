"""Tests for the 40-char context-snippet field in stop_phrase collector hits.

Each hit in ``recent_hits`` now carries a ``context_snippet`` key with up to
40 characters on each side of the matched span, trimmed at the string boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from codevigil.collectors.stop_phrase import _CONTEXT_WINDOW, StopPhraseCollector
from codevigil.types import Event, EventKind


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
        "warn_threshold": 1,
        "critical_threshold": 3,
        "experimental": False,
    }
    cfg.update(overrides)
    return StopPhraseCollector(cfg)


def test_context_snippet_present_on_hit() -> None:
    """Every hit in recent_hits must carry a context_snippet key."""
    c = _make()
    c.ingest(_msg("This is a well-known pre-existing problem in our codebase."))
    snap = c.snapshot()
    assert snap.detail is not None
    hits = snap.detail["recent_hits"]
    assert len(hits) >= 1
    for hit in hits:
        assert "context_snippet" in hit, f"context_snippet missing from hit: {hit}"
        assert isinstance(hit["context_snippet"], str)


def test_context_snippet_contains_matched_phrase() -> None:
    """The snippet must include the matched phrase text."""
    c = _make()
    text = "You told me to fix the bug. Should I continue? Great, let's go."
    c.ingest(_msg(text))
    snap = c.snapshot()
    assert snap.detail is not None
    hits = snap.detail["recent_hits"]
    for hit in hits:
        snippet = hit["context_snippet"]
        # The matched phrase (lowercased) must appear in the snippet.
        assert hit["phrase"].lower() in snippet.lower(), (
            f"phrase {hit['phrase']!r} not in snippet {snippet!r}"
        )


def test_context_snippet_bounded_by_window() -> None:
    """Snippet length is at most 2 * _CONTEXT_WINDOW + len(phrase)."""
    c = _make()
    phrase = "should I continue"
    # Use spaces as padding so word-boundary matching still fires.
    padding = "word " * 20  # 100 chars of non-alphanumeric-boundary padding
    text = f"{padding}{phrase} {padding}"
    c.ingest(_msg(text))
    snap = c.snapshot()
    assert snap.detail is not None
    hits = [h for h in snap.detail["recent_hits"] if h["phrase"] == phrase]
    assert hits, f"No hit for phrase {phrase!r}; all hits: {snap.detail['recent_hits']}"
    snippet = hits[0]["context_snippet"]
    max_len = 2 * _CONTEXT_WINDOW + len(phrase)
    assert len(snippet) <= max_len, (
        f"Snippet length {len(snippet)} exceeds max {max_len}; snippet: {snippet!r}"
    )


def test_context_snippet_trimmed_at_string_start() -> None:
    """Snippet is trimmed when the match is near the start of the message."""
    c = _make()
    text = "should I continue working on the task."
    c.ingest(_msg(text))
    snap = c.snapshot()
    assert snap.detail is not None
    hits = [h for h in snap.detail["recent_hits"] if "should" in h["phrase"].lower()]
    assert hits, f"No hit with 'should' in phrase; hits={snap.detail['recent_hits']}"
    snippet = hits[0]["context_snippet"]
    # The snippet should start at or near the beginning of text.
    assert text.startswith(snippet[:10]) or snippet.startswith(text[:10])


def test_context_snippet_trimmed_at_string_end() -> None:
    """Snippet is trimmed when the match is near the end of the message."""
    c = _make()
    text = "I've reviewed everything. This is a known limitation."
    c.ingest(_msg(text))
    snap = c.snapshot()
    assert snap.detail is not None
    hits = [h for h in snap.detail["recent_hits"] if "known" in h["phrase"].lower()]
    assert hits, f"No hit with 'known' in phrase; hits={snap.detail['recent_hits']}"
    snippet = hits[0]["context_snippet"]
    # The snippet must end at or near the end of text.
    assert text.endswith(snippet[-10:]) or snippet.endswith(text[-10:])


def test_context_snippet_not_empty_on_short_message() -> None:
    """Even a very short message produces a non-empty snippet."""
    c = _make()
    text = "should I continue"
    c.ingest(_msg(text))
    snap = c.snapshot()
    assert snap.detail is not None
    hits = snap.detail["recent_hits"]
    assert hits
    assert hits[0]["context_snippet"] != ""


def test_context_window_constant_is_40() -> None:
    """The context window is exactly 40 characters per spec."""
    assert _CONTEXT_WINDOW == 40
