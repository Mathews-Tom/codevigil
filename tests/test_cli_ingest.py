"""End-to-end smoke tests for ``codevigil ingest`` (Phase C2)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from codevigil.analysis.processed_store import ProcessedSessionStore, default_db_path
from codevigil.cli import main


def _setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_WATCH_ROOT", str(home / ".claude" / "projects"))
    return home


def _write_session_file(
    home: Path,
    *,
    project: str,
    session_id: str,
    age_seconds: float = 0.0,
) -> Path:
    sessions = home / ".claude" / "projects" / project / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{session_id}.jsonl"
    path.write_text(
        '{"type":"user","timestamp":"2025-11-01T10:00:00Z",'
        '"message":{"id":"u1","content":[{"type":"text","text":"hi"}]}}\n'
        '{"type":"assistant","timestamp":"2025-11-01T10:00:05Z",'
        '"message":{"id":"a1","content":[{"type":"text","text":"yo"}]}}\n',
        encoding="utf-8",
    )
    if age_seconds > 0:
        target = time.time() - age_seconds
        os.utime(path, (target, target))
    return path


def test_ingest_populates_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    _write_session_file(home, project="Open-ASM", session_id="agent-abc123", age_seconds=90 * 86400)
    _write_session_file(home, project="Open-ASM", session_id="agent-def456", age_seconds=5 * 86400)

    exit_code = main(["ingest"])
    assert exit_code == 0

    db_path = default_db_path()
    assert db_path.exists()
    with ProcessedSessionStore(db_path) as store:
        assert store.count() == 2
        a = store.get_session("agent-abc123")
        b = store.get_session("agent-def456")
    assert a is not None and b is not None
    assert a.project_hash
    assert a.event_count >= 2
    assert b.event_count >= 2

    out = capsys.readouterr().out
    assert "ingest complete" in out
    assert "processed=2" in out


def test_ingest_idempotent_skips_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    _write_session_file(home, project="X", session_id="agent-1", age_seconds=86400)

    main(["ingest"])
    capsys.readouterr()

    exit_code = main(["ingest"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "processed=0" in out
    assert "skipped=1" in out


def test_ingest_force_reprocesses_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    _write_session_file(home, project="X", session_id="agent-1", age_seconds=86400)

    main(["ingest"])
    capsys.readouterr()

    exit_code = main(["ingest", "--force"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "processed=1" in out


def test_ingest_detects_file_growth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    path = _write_session_file(home, project="X", session_id="agent-1")

    main(["ingest"])
    with ProcessedSessionStore(default_db_path()) as store:
        first = store.get_session("agent-1")
    assert first is not None
    first_size = first.size

    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"type":"user","timestamp":"2025-11-01T10:00:10Z",'
            '"message":{"id":"u2","content":[{"type":"text","text":"more"}]}}\n'
        )

    main(["ingest"])
    with ProcessedSessionStore(default_db_path()) as store:
        second = store.get_session("agent-1")
    assert second is not None
    assert second.size > first_size


def test_watch_auto_ingests_when_db_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``codevigil watch`` should run ingest automatically when the DB file
    does not exist on disk, then proceed into its normal tick loop."""
    home = _setup_home(tmp_path, monkeypatch)
    _write_session_file(home, project="X", session_id="agent-1", age_seconds=86400)

    import codevigil.cli as cli_module

    # Stop after one tick so the test terminates.
    original_run_one_tick = cli_module._run_one_tick
    tick_count = {"n": 0}

    def _fake_tick(aggregator: object, renderer: object, *, explain: bool) -> None:
        tick_count["n"] += 1
        original_run_one_tick(aggregator, renderer, explain=explain)  # type: ignore[arg-type]
        cli_module._shutdown_requested = True

    monkeypatch.setattr(cli_module, "_run_one_tick", _fake_tick)

    assert not default_db_path().exists()
    exit_code = main(["watch"])
    assert exit_code == 0
    assert default_db_path().exists(), "watch should have auto-invoked ingest"
    assert tick_count["n"] >= 1

    with ProcessedSessionStore(default_db_path()) as store:
        assert store.get_session("agent-1") is not None
