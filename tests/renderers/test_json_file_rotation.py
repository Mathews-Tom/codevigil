"""NDJSON renderer rotates via the shared RotatingJsonlWriter."""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.renderers.json_file import JsonFileRenderer
from tests.renderers._fixtures import make_meta, make_snapshots


def test_rotation_with_tiny_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    renderer = JsonFileRenderer(output_dir=out_dir, max_bytes=200, backups=3)

    meta = make_meta()
    snapshots = make_snapshots()
    for _ in range(30):
        renderer.render(snapshots, meta)

    active = out_dir / "snapshots.jsonl"
    assert active.exists()
    assert active.stat().st_size <= 200 + 512  # a single record can exceed cap slightly

    archives = sorted(out_dir.glob("snapshots.jsonl.*"))
    assert len(archives) >= 1
