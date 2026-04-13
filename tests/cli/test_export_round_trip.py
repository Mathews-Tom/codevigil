"""``codevigil export`` round-trip over a 5-event fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codevigil.cli import main

from ._fixtures import write_fixture_session


def test_export_emits_ndjson_event_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))

    fixture = write_fixture_session(home / "session.jsonl")

    exit_code = main(["export", str(fixture)])
    assert exit_code == 0

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) >= 5, f"expected at least 5 events, got {len(lines)}"

    parsed = [json.loads(line) for line in lines]
    for record in parsed:
        assert set(record.keys()) == {"timestamp", "session_id", "kind", "payload"}
        assert record["session_id"] == "sess-cli-1"

    kinds = [record["kind"] for record in parsed]
    # Fixture is: system, user, assistant (text + tool_use), user (tool_result), assistant (text)
    # parser emits assistant text + tool_call separately, so expect both.
    assert "system" in kinds
    assert "user" in kinds
    assert "assistant" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
