"""Error taxonomy and JSONL rotating writer tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.errors import (
    CodevigilError,
    ErrorChannel,
    ErrorLevel,
    ErrorSource,
    RotatingJsonlWriter,
    get_error_channel,
    record,
    reset_error_channel,
    set_error_channel,
)


@pytest.fixture(autouse=True)
def _reset_channel() -> Iterator[None]:
    reset_error_channel()
    yield
    reset_error_channel()


def _make_error(code: str = "test.example") -> CodevigilError:
    return CodevigilError(
        level=ErrorLevel.WARN,
        source=ErrorSource.PARSER,
        code=code,
        message="example",
        context={"k": "v"},
    )


def test_error_is_frozen_and_serialisable(tmp_path: Path) -> None:
    err = _make_error()
    with pytest.raises(AttributeError):
        err.code = "other"  # type: ignore[misc]
    record = err.to_json_record()
    assert record["level"] == "warn"
    assert record["source"] == "parser"
    assert record["code"] == "test.example"
    assert record["context"] == {"k": "v"}
    assert "timestamp" in record


def test_writer_round_trip_and_jsonl_shape(tmp_path: Path) -> None:
    path = tmp_path / "codevigil.log"
    writer = RotatingJsonlWriter(path)
    err = _make_error()
    writer.write(err.to_json_record())
    writer.write(err.to_json_record(timestamp=None))
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)
        assert parsed["code"] == "test.example"
        assert parsed["level"] == "warn"


def test_rotation_creates_archives_at_size_boundary(tmp_path: Path) -> None:
    path = tmp_path / "codevigil.log"
    writer = RotatingJsonlWriter(path, max_bytes=200, backups=3)
    # Each record is ~50 bytes; 6 writes force at least one rotation.
    for i in range(10):
        writer.write({"i": i, "pad": "x" * 40})
    archives = sorted(tmp_path.glob("codevigil.log*"))
    assert path in archives
    # At least one archive must exist after rotation.
    assert any(p.name.startswith("codevigil.log.") for p in archives)


def test_rotation_caps_backups_at_configured_count(tmp_path: Path) -> None:
    path = tmp_path / "codevigil.log"
    writer = RotatingJsonlWriter(path, max_bytes=100, backups=3)
    for i in range(200):
        writer.write({"i": i, "pad": "y" * 40})
    archive_indices = sorted(
        int(p.suffix[1:])
        for p in tmp_path.iterdir()
        if p.name.startswith("codevigil.log.") and p.suffix[1:].isdigit()
    )
    assert archive_indices, "expected at least one archive after rotation"
    assert max(archive_indices) <= 3


def test_log_path_respects_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "custom" / "log.jsonl"
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(override))
    channel = get_error_channel()
    assert channel.writer.path == override


def test_record_writes_via_module_level_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "override.log"
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(override))
    record(_make_error("test.record_helper"))
    assert override.exists()
    line = override.read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(line)["code"] == "test.record_helper"


def test_set_error_channel_installs_explicit_instance(tmp_path: Path) -> None:
    path = tmp_path / "explicit.log"
    explicit = ErrorChannel(RotatingJsonlWriter(path))
    set_error_channel(explicit)
    assert get_error_channel() is explicit
    record(_make_error("test.explicit"))
    assert path.exists()
