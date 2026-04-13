"""NDJSON renderer: snapshot records round-trip through json.loads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codevigil.renderers.json_file import JsonFileRenderer
from codevigil.types import MetricSnapshot, Severity
from tests.renderers._fixtures import make_meta, make_snapshots


def test_snapshot_record_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out_dir = tmp_path / "codevigil-out"
    out_dir.mkdir()

    renderer = JsonFileRenderer(output_dir=out_dir)
    meta = make_meta()
    snapshots = make_snapshots()
    renderer.render(snapshots, meta)
    renderer.close()

    lines = (out_dir / "snapshots.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["kind"] == "snapshot"
    assert record["session_id"] == meta.session_id
    assert record["project_hash"] == meta.project_hash
    assert record["project_name"] == meta.project_name
    assert record["state"] == "active"
    assert record["parse_confidence"] == pytest.approx(1.0)
    assert "timestamp" in record

    entries = record["snapshots"]
    assert len(entries) == len(snapshots)
    first = entries[0]
    assert set(first) == {"name", "value", "label", "severity", "detail"}
    rebuilt = MetricSnapshot(
        name=first["name"],
        value=first["value"],
        label=first["label"],
        severity=Severity(first["severity"]),
        detail=first["detail"],
    )
    assert rebuilt == snapshots[0]
