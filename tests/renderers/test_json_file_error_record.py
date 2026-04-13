"""NDJSON renderer: render_error produces distinct error records."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource
from codevigil.renderers.json_file import JsonFileRenderer
from tests.renderers._fixtures import make_meta


def test_error_record_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    renderer = JsonFileRenderer(output_dir=out_dir)

    err = CodevigilError(
        level=ErrorLevel.CRITICAL,
        source=ErrorSource.PARSER,
        code="parser.schema_drift",
        message="parse confidence dropped",
        context={"missing": 12},
    )
    renderer.render_error(err, make_meta())

    lines = (out_dir / "snapshots.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "error"
    assert rec["level"] == "critical"
    assert rec["source"] == "parser"
    assert rec["code"] == "parser.schema_drift"
    assert rec["message"] == "parse confidence dropped"
    assert rec["context"] == {"missing": 12}
    assert rec["session_id"] == make_meta().session_id


def test_error_record_without_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    renderer = JsonFileRenderer(output_dir=out_dir)

    err = CodevigilError(
        level=ErrorLevel.WARN,
        source=ErrorSource.WATCHER,
        code="watcher.rotated",
        message="file rotated",
    )
    renderer.render_error(err, None)

    rec = json.loads((out_dir / "snapshots.jsonl").read_text(encoding="utf-8"))
    assert rec["kind"] == "error"
    assert "session_id" not in rec
