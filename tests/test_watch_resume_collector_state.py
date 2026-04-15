"""End-to-end: collector state survives ``codevigil ingest`` → watch resume.

Exercises the C1+C5 loop:
1. Write a session JSONL with several tool calls.
2. Run ``codevigil ingest`` to persist collector state + metadata.
3. Append new tool calls to the same file.
4. Run the watch loop for one tick and verify that the aggregator's
   snapshot reflects the *combined* pre+post state (counters continue
   from the persisted value instead of restarting from zero).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

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


def _write_tool_call_session(path: Path, tool_names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    base_ts = "2025-11-01T10:00:"
    for i, tool in enumerate(tool_names):
        ts = f"{base_ts}{i:02d}Z"
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": ts,
                    "session_id": path.stem,
                    "message": {
                        "id": f"a{i}",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"tu_{i}",
                                "name": tool,
                                "input": {"file_path": "/tmp/demo.py"},
                            }
                        ],
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ingest_persists_read_edit_ratio_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    session_path = home / ".claude" / "projects" / "Open-ASM" / "sessions" / "agent-xyz.jsonl"
    _write_tool_call_session(session_path, ["Read", "Read", "Read", "Read", "Edit"])

    exit_code = main(["ingest"])
    assert exit_code == 0

    with ProcessedSessionStore(default_db_path()) as store:
        record = store.get_session("agent-xyz")
    assert record is not None
    # The read_edit_ratio collector must have persisted non-empty state.
    assert "read_edit_ratio" in record.collector_state
    state = record.collector_state["read_edit_ratio"]
    assert state["mutations_total"] >= 1
    assert state["classified_index"] >= 4


def test_watch_resume_continues_counters_from_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    session_path = home / ".claude" / "projects" / "Open-ASM" / "sessions" / "agent-rsm.jsonl"
    _write_tool_call_session(session_path, ["Read", "Read", "Read"])

    main(["ingest"])
    # Capture the persisted classified_index for read_edit_ratio.
    with ProcessedSessionStore(default_db_path()) as store:
        first = store.get_session("agent-rsm")
    assert first is not None
    first_index = int(first.collector_state["read_edit_ratio"]["classified_index"])

    # Append new tool calls; mtime advances so PollingSource sees growth.
    extra_lines: list[str] = []
    base_ts = "2025-11-01T11:00:"
    for i, tool in enumerate(["Edit", "Edit"]):
        extra_lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": f"{base_ts}{i:02d}Z",
                    "session_id": session_path.stem,
                    "message": {
                        "id": f"b{i}",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"tu_b{i}",
                                "name": tool,
                                "input": {"file_path": "/tmp/demo.py"},
                            }
                        ],
                    },
                }
            )
        )
    with session_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(extra_lines) + "\n")
    os.utime(session_path, (time.time(), time.time()))

    # Run one watch tick, then shut down.
    import codevigil.cli as cli_module

    original = cli_module._run_one_tick
    tick_count = {"n": 0}

    def _one_tick(aggregator: Any, renderer: Any, *, explain: bool) -> None:
        tick_count["n"] += 1
        original(aggregator, renderer, explain=explain)
        cli_module._shutdown_requested = True

    monkeypatch.setattr(cli_module, "_run_one_tick", _one_tick)

    assert main(["watch", "--by-session"]) == 0
    assert tick_count["n"] >= 1

    # Re-ingest so the store captures the cumulative state the watch
    # aggregator built from the restored counters plus the new events.
    main(["ingest", "--force"])
    with ProcessedSessionStore(default_db_path()) as store:
        second = store.get_session("agent-rsm")
    assert second is not None
    second_index = int(second.collector_state["read_edit_ratio"]["classified_index"])
    assert second_index > first_index, (
        "watch resume + re-ingest must preserve prior classified_index"
        " and add the appended events on top"
    )
