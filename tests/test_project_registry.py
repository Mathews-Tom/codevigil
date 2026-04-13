"""ProjectRegistry: three-source precedence and unresolved-fallback silence."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codevigil.errors import (
    ErrorChannel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.projects import ProjectRegistry
from codevigil.types import Event, EventKind
from tests._watcher_helpers import read_error_codes


@pytest.fixture
def error_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "errors.jsonl"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))
    yield path
    reset_error_channel()


def _system_event(cwd: str | None = "/Users/alice/work/coolproject") -> Event:
    payload: dict[str, object] = {"subkind": "init"}
    if cwd is not None:
        payload["cwd"] = cwd
    return Event(
        timestamp=datetime.now(tz=UTC),
        session_id="sess-1",
        kind=EventKind.SYSTEM,
        payload=payload,
    )


def _write_toml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_toml_override_wins_when_only_source(tmp_path: Path, error_log: Path) -> None:
    toml = tmp_path / "projects.toml"
    _write_toml(toml, '"abc123hash" = "Customer Portal"\n')
    registry = ProjectRegistry(toml_path=toml)

    assert registry.resolve("abc123hash") == "Customer Portal"
    assert read_error_codes(error_log) == []


def test_toml_override_beats_cwd(tmp_path: Path, error_log: Path) -> None:
    toml = tmp_path / "projects.toml"
    _write_toml(toml, '"abc123hash" = "Customer Portal"\n')
    registry = ProjectRegistry(toml_path=toml)
    registry.observe_system_event("abc123hash", _system_event("/Users/alice/work/coolproject"))

    assert registry.resolve("abc123hash") == "Customer Portal"


def test_cwd_beats_raw_hash_fallback(tmp_path: Path, error_log: Path) -> None:
    toml = tmp_path / "projects.toml"  # absent
    registry = ProjectRegistry(toml_path=toml)
    registry.observe_system_event("abc123hash", _system_event("/Users/alice/work/coolproject"))

    assert registry.resolve("abc123hash") == "coolproject"


def test_unresolved_falls_back_to_hash_prefix_silently(tmp_path: Path, error_log: Path) -> None:
    registry = ProjectRegistry(toml_path=tmp_path / "absent.toml")

    assert registry.resolve("abcdef0123456789") == "abcdef01"
    assert read_error_codes(error_log) == []


def test_first_observed_cwd_wins(tmp_path: Path, error_log: Path) -> None:
    registry = ProjectRegistry(toml_path=tmp_path / "absent.toml")
    registry.observe_system_event("hash", _system_event("/Users/alice/first"))
    registry.observe_system_event("hash", _system_event("/Users/alice/second"))

    assert registry.resolve("hash") == "first"


def test_malformed_toml_logs_warn_and_continues(tmp_path: Path, error_log: Path) -> None:
    toml = tmp_path / "projects.toml"
    _write_toml(toml, "not = valid = toml = at = all")

    registry = ProjectRegistry(toml_path=toml)

    assert registry.resolve("h") == "h"[:8]
    codes = read_error_codes(error_log)
    assert "projects.toml_load_failed" in codes


def test_non_string_toml_entry_skipped_with_warn(tmp_path: Path, error_log: Path) -> None:
    toml = tmp_path / "projects.toml"
    _write_toml(toml, '"good" = "Good Name"\n"bad" = 42\n')

    registry = ProjectRegistry(toml_path=toml)

    assert registry.resolve("good") == "Good Name"
    assert registry.resolve("bad") == "bad"[:8]
    codes = read_error_codes(error_log)
    assert "projects.toml_bad_entry" in codes


def test_non_system_events_ignored(tmp_path: Path, error_log: Path) -> None:
    registry = ProjectRegistry(toml_path=tmp_path / "absent.toml")
    user_event = Event(
        timestamp=datetime.now(tz=UTC),
        session_id="sess",
        kind=EventKind.USER_MESSAGE,
        payload={"text": "hello", "cwd": "/should/be/ignored"},
    )
    registry.observe_system_event("hash", user_event)

    assert registry.resolve("hash") == "hash"[:8]
