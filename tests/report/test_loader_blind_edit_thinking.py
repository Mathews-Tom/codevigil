"""Loader tests for the blind-edit and thinking metric injectors.

These cover the report.loader injection paths added for issue #42796
parity:

* ``blind_edit_rate`` — pulled from the read_edit_ratio collector's
  ``detail.blind_edit_rate.value``.
* ``thinking_visible_ratio`` / ``thinking_visible_chars_median`` /
  ``thinking_signature_chars_median`` — pulled from the thinking
  collector's snapshot detail.
"""

from __future__ import annotations

import json
from pathlib import Path

from codevigil.report.loader import load_reports_from_jsonl


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _system(session: str = "s") -> str:
    return json.dumps(
        {
            "type": "system",
            "timestamp": "2026-04-14T10:00:00+00:00",
            "session_id": session,
            "subtype": "session_start",
        }
    )


def _tool_call(name: str, file_path: str, ts: str, tid: str = "t") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "session_id": "s",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tid,
                        "name": name,
                        "input": {"file_path": file_path},
                    }
                ]
            },
        }
    )


def _thinking_line(
    text: str,
    *,
    signature: str = "sig",
    ts: str = "2026-04-14T10:05:00+00:00",
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "session_id": "s",
            "message": {
                "content": [
                    {
                        "type": "thinking",
                        "thinking": text,
                        "signature": signature,
                    }
                ]
            },
        }
    )


def test_blind_edit_rate_injected_when_edits_skip_reads(tmp_path: Path) -> None:
    path = tmp_path / "blind.jsonl"
    # Two edits, neither preceded by a read on the same path.
    _write(
        path,
        [
            _system(),
            _tool_call("Edit", "/a.py", "2026-04-14T10:00:01+00:00", "t1"),
            _tool_call("Edit", "/b.py", "2026-04-14T10:00:02+00:00", "t2"),
        ],
    )
    reports = load_reports_from_jsonl([path])
    assert len(reports) == 1
    metrics = reports[0].metrics
    assert "blind_edit_rate" in metrics
    assert metrics["blind_edit_rate"] == 1.0


def test_blind_edit_rate_zero_when_reads_precede_edits(tmp_path: Path) -> None:
    path = tmp_path / "guarded.jsonl"
    _write(
        path,
        [
            _system(),
            _tool_call("Read", "/a.py", "2026-04-14T10:00:01+00:00", "t1"),
            _tool_call("Edit", "/a.py", "2026-04-14T10:00:02+00:00", "t2"),
        ],
    )
    reports = load_reports_from_jsonl([path])
    metrics = reports[0].metrics
    assert metrics.get("blind_edit_rate") == 0.0


def test_thinking_metrics_injected_when_visible_blocks_present(tmp_path: Path) -> None:
    path = tmp_path / "think.jsonl"
    _write(
        path,
        [
            _system(),
            _thinking_line("a" * 400, ts="2026-04-14T10:00:01+00:00"),
            _thinking_line("b" * 200, ts="2026-04-14T10:00:02+00:00"),
            _thinking_line("c" * 600, ts="2026-04-14T10:00:03+00:00"),
        ],
    )
    reports = load_reports_from_jsonl([path])
    metrics = reports[0].metrics
    assert metrics["thinking_visible_ratio"] == 1.0
    assert metrics["thinking_visible_chars_median"] == 400.0
    # The bare collector name must not leak into the cohort metric set.
    assert "thinking" not in metrics


def test_thinking_metrics_absent_when_no_thinking_blocks(tmp_path: Path) -> None:
    path = tmp_path / "nothink.jsonl"
    _write(
        path,
        [
            _system(),
            _tool_call("Read", "/a.py", "2026-04-14T10:00:01+00:00", "t1"),
        ],
    )
    reports = load_reports_from_jsonl([path])
    metrics = reports[0].metrics
    assert "thinking_visible_ratio" not in metrics
    assert "thinking" not in metrics
