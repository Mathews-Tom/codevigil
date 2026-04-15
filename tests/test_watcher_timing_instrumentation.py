"""Phase A timing instrumentation: first-poll / first-tick log markers.

Verifies that PollingSource and SessionAggregator emit single-shot timing
summaries on the cold-start path, and that the payload fields carry
non-empty values.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from codevigil.aggregator import SessionAggregator
from codevigil.config import CONFIG_DEFAULTS
from codevigil.projects import ProjectRegistry
from codevigil.watcher import PollingSource


def _write_session(root: Path, name: str = "session") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.jsonl"
    path.write_text(
        '{"type":"user","message":{"id":"u1","content":[{"type":"text","text":"hi"}]}}\n',
        encoding="utf-8",
    )
    return path


def test_polling_source_emits_first_poll_info_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "projects"
    _write_session(root)

    source = PollingSource(root, interval=1.0)
    with caplog.at_level(logging.INFO, logger="codevigil.watcher"):
        list(source.poll())
        list(source.poll())

    first_poll_records = [r for r in caplog.records if "first_poll" in r.getMessage()]
    assert len(first_poll_records) == 1, "first_poll should fire exactly once"
    msg = first_poll_records[0].getMessage()
    assert "elapsed_ms=" in msg
    assert "events=" in msg
    assert "bytes_read=" in msg
    assert "files_tracked=" in msg


def test_aggregator_emits_first_tick_info_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "projects"
    _write_session(root)

    source = PollingSource(root, interval=1.0)
    aggregator = SessionAggregator(
        source,
        config=dict(CONFIG_DEFAULTS),
        project_registry=ProjectRegistry(),
    )

    with caplog.at_level(logging.INFO, logger="codevigil.aggregator"):
        list(aggregator.tick())
        list(aggregator.tick())

    first_tick_records = [r for r in caplog.records if "first_tick" in r.getMessage()]
    assert len(first_tick_records) == 1, "first_tick should fire exactly once"
    msg = first_tick_records[0].getMessage()
    for field in (
        "source_events=",
        "source_ingest_ms=",
        "lifecycle_ms=",
        "snapshot_ms=",
        "sessions_tracked=",
        "sessions_yielded=",
        "eviction_churn=",
    ):
        assert field in msg, f"missing {field!r} in {msg!r}"
    aggregator.close()


def test_timing_logger_silent_without_env_var(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``CODEVIGIL_DEBUG_TIMING`` is unset, ``_configure_timing_logger``
    installs no handlers and timing records do not reach stderr."""
    from codevigil.cli import _configure_timing_logger

    monkeypatch.delenv("CODEVIGIL_DEBUG_TIMING", raising=False)
    watcher_logger = logging.getLogger("codevigil.watcher")
    pre_handlers = list(watcher_logger.handlers)

    _configure_timing_logger()

    assert list(watcher_logger.handlers) == pre_handlers
    # Emit a record and confirm nothing reaches stderr from a fresh call.
    watcher_logger.info("watcher.first_poll elapsed_ms=1.0 events=0 bytes_read=0 files_tracked=0")
    err = capsys.readouterr().err
    assert "codevigil.timing" not in err


def test_timing_logger_installs_handler_when_env_var_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from codevigil.cli import _configure_timing_logger

    monkeypatch.setenv("CODEVIGIL_DEBUG_TIMING", "1")
    watcher_logger = logging.getLogger("codevigil.watcher")
    aggregator_logger = logging.getLogger("codevigil.aggregator")
    pre_watcher = list(watcher_logger.handlers)
    pre_aggregator = list(aggregator_logger.handlers)
    try:
        _configure_timing_logger()
        assert len(watcher_logger.handlers) > len(pre_watcher)
        assert len(aggregator_logger.handlers) > len(pre_aggregator)
        watcher_logger.info(
            "watcher.first_poll elapsed_ms=2.0 events=1 bytes_read=10 files_tracked=1"
        )
        err = capsys.readouterr().err
        assert "codevigil.timing" in err
        assert "watcher.first_poll" in err
    finally:
        watcher_logger.handlers = pre_watcher
        aggregator_logger.handlers = pre_aggregator
        watcher_logger.propagate = True
        aggregator_logger.propagate = True
