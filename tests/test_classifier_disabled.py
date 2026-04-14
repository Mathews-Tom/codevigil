"""Tests for classifier.enabled = false behaviour.

When the classifier is disabled via config, every turn's task_type must remain
None, SessionReport.session_task_type must be None, and classify_turn must
never be called (verified via observable state rather than mocks).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.aggregator import SessionAggregator
from codevigil.analysis.store import SessionStore
from codevigil.collectors.parse_health import ParseHealthCollector
from codevigil.errors import (
    ErrorChannel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.projects import ProjectRegistry
from codevigil.types import Collector
from codevigil.watcher import SourceEventKind
from tests._aggregator_helpers import (
    FakeClock,
    FakeSource,
    good_user_line,
    make_source_event,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


def _disabled_config(store_dir: Path | None = None) -> dict[str, object]:
    cfg: dict[str, object] = {
        "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
        "collectors": {"enabled": []},
        "classifier": {"enabled": False, "experimental": True},
    }
    if store_dir is not None:
        cfg["storage"] = {"enable_persistence": True}
    return cfg


def _registry() -> dict[str, type[Collector]]:
    return {ParseHealthCollector.name: ParseHealthCollector}


def _assistant_line(text: str = "ok") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-04-14T10:00:05+00:00",
            "session_id": "sess-1",
            "message": {
                "content": [{"type": "text", "text": text}],
                "usage": {"output_tokens": 10},
            },
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_classifier_disabled_turns_have_null_task_type(error_log: Path) -> None:
    """With classifier.enabled=false, completed turns have task_type=None."""
    clock = FakeClock(value=0.0)
    source = FakeSource()
    aggregator = SessionAggregator(
        source,
        config=_disabled_config(),
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_registry(),
    )

    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("first prompt")),
            make_source_event(SourceEventKind.APPEND, line=_assistant_line("reply")),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("second prompt")),
        ]
    )

    list(aggregator.tick())

    ctx = aggregator.sessions["sess-1"]
    assert len(ctx.completed_turns) == 1
    turn = ctx.completed_turns[0]
    # Classifier is disabled — task_type must remain None.
    assert turn.task_type is None


def test_classifier_disabled_session_report_has_null_task_fields(
    error_log: Path, tmp_path: Path
) -> None:
    """With classifier.enabled=false, SessionReport fields are None after eviction."""
    store_dir = tmp_path / "sessions"
    store_dir.mkdir()
    clock = FakeClock(value=0.0)
    source = FakeSource()
    store = SessionStore(base_dir=store_dir)
    aggregator = SessionAggregator(
        source,
        config={
            "watch": {"stale_after_seconds": 10, "evict_after_seconds": 20},
            "collectors": {"enabled": []},
            "classifier": {"enabled": False, "experimental": True},
            "storage": {"enable_persistence": True},
        },
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_registry(),
    )
    aggregator._store = store

    # Feed two complete turns.
    source.push(
        [
            make_source_event(SourceEventKind.NEW_SESSION),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("first")),
            make_source_event(SourceEventKind.APPEND, line=_assistant_line("reply 1")),
            make_source_event(SourceEventKind.APPEND, line=good_user_line("second")),
            make_source_event(SourceEventKind.APPEND, line=_assistant_line("reply 2")),
        ]
    )
    list(aggregator.tick())

    # Evict via DELETE to trigger report write.
    source.push([make_source_event(SourceEventKind.DELETE)])
    list(aggregator.tick())

    report = store.get_report("sess-1")
    assert report is not None

    # Both task-type fields must be None when classifier is disabled.
    assert report.session_task_type is None
    assert report.turn_task_types is None


def test_classifier_disabled_flag_is_read_from_config(error_log: Path) -> None:
    """The aggregator reads classifier.enabled from config, not a hard-coded default."""
    clock = FakeClock(value=0.0)
    source_off = FakeSource()
    source_on = FakeSource()

    agg_off = SessionAggregator(
        source_off,
        config=_disabled_config(),
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_registry(),
    )
    agg_on = SessionAggregator(
        source_on,
        config={
            "watch": {"stale_after_seconds": 300, "evict_after_seconds": 2100},
            "collectors": {"enabled": []},
            "classifier": {"enabled": True},
        },
        project_registry=ProjectRegistry(toml_path=Path("/nonexistent.toml")),
        clock=clock,
        registry=_registry(),
    )

    # Both aggregators get the same events.
    for source in (source_off, source_on):
        source.push(
            [
                make_source_event(SourceEventKind.NEW_SESSION),
                make_source_event(SourceEventKind.APPEND, line=good_user_line("prompt")),
                make_source_event(SourceEventKind.APPEND, line=_assistant_line("reply")),
                make_source_event(SourceEventKind.APPEND, line=good_user_line("prompt 2")),
            ]
        )

    list(agg_off.tick())
    list(agg_on.tick())

    turn_off = agg_off.sessions["sess-1"].completed_turns[0]
    turn_on = agg_on.sessions["sess-1"].completed_turns[0]

    # When disabled, task_type remains None; when enabled, it is classified.
    assert turn_off.task_type is None
    assert turn_on.task_type is not None
