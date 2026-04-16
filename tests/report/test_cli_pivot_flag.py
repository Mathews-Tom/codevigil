"""End-to-end CLI tests for --pivot-date flag."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codevigil.cli import main


def _setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_REPORT_OUTPUT_DIR", str(home / "reports"))
    return home


def _write_session(path: Path, ts_date: str, sid: str) -> None:
    lines = [
        json.dumps(
            {
                "type": "system",
                "timestamp": f"{ts_date}T09:00:00+00:00",
                "session_id": sid,
                "subtype": "session_start",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": f"{ts_date}T09:01:00+00:00",
                "session_id": sid,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tr",
                            "name": "Read",
                            "input": {"file_path": "/x.py"},
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": f"{ts_date}T09:02:00+00:00",
                "session_id": sid,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tw",
                            "name": "Write",
                            "input": {"file_path": "/x.py", "content": "x=1"},
                        }
                    ]
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def corpus_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = _setup_home(tmp_path, monkeypatch)
    sessions = home / "sessions"
    sessions.mkdir()
    before_dates = ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
    after_dates = ["2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13", "2026-03-14"]
    for i, d in enumerate(before_dates):
        _write_session(sessions / f"a-{i}.jsonl", d, f"a{i}")
    for i, d in enumerate(after_dates):
        _write_session(sessions / f"b-{i}.jsonl", d, f"b{i}")
    return sessions


def test_pivot_returns_zero(corpus_dir: Path) -> None:
    rc = main(["report", str(corpus_dir), "--pivot-date", "2026-03-08"])
    assert rc == 0


def test_pivot_writes_named_markdown(corpus_dir: Path, tmp_path: Path) -> None:
    main(["report", str(corpus_dir), "--pivot-date", "2026-03-08"])
    out = tmp_path / "home" / "reports" / "pivot_2026-03-08.md"
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "# Period Comparison:" in body
    assert "2026-03-01..2026-03-07" in body
    assert "2026-03-08..2026-03-14" in body


def test_pivot_rejects_bad_date(corpus_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["report", str(corpus_dir), "--pivot-date", "not-a-date"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "bad_pivot_date" in err


def test_pivot_rejects_out_of_range(corpus_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["report", str(corpus_dir), "--pivot-date", "2027-01-01"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "pivot_out_of_range" in err


def test_pivot_mutually_exclusive_with_group_by(
    corpus_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "report",
            str(corpus_dir),
            "--pivot-date",
            "2026-03-08",
            "--group-by",
            "week",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "mutually exclusive" in err
