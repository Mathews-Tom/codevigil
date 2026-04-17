"""Root-identity helpers for multi-root watch support."""

from __future__ import annotations

from pathlib import Path

import pytest

from codevigil.watch_roots import (
    describe_root,
    describe_roots,
    make_root_id,
    make_session_key,
    split_session_key,
)


def test_make_root_id_is_stable_for_same_path(tmp_path: Path) -> None:
    root = tmp_path / ".claude" / "projects"
    root.mkdir(parents=True)

    first = make_root_id(root)
    second = make_root_id(root)

    assert first == second
    assert first.startswith("root-")


def test_describe_root_resolves_path(tmp_path: Path) -> None:
    root = tmp_path / ".claude" / "projects"
    root.mkdir(parents=True)

    descriptor = describe_root(root)

    assert descriptor.root_path == root.resolve()
    assert descriptor.display_name == str(root.resolve())
    assert descriptor.root_id == make_root_id(root)


def test_describe_roots_preserves_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    descriptors = describe_roots([first, second])

    assert [descriptor.root_path for descriptor in descriptors] == [
        first.resolve(),
        second.resolve(),
    ]


def test_session_key_round_trip() -> None:
    key = make_session_key("root-abc123", "session-42")

    assert split_session_key(key) == ("root-abc123", "session-42")


@pytest.mark.parametrize(
    ("root_id", "session_id"),
    [
        ("", "session-42"),
        ("root-abc123", ""),
    ],
)
def test_make_session_key_rejects_empty_parts(root_id: str, session_id: str) -> None:
    with pytest.raises(ValueError):
        make_session_key(root_id, session_id)


@pytest.mark.parametrize("session_key", ["", "root-only", ":session-only", "root:"])
def test_split_session_key_rejects_invalid_values(session_key: str) -> None:
    with pytest.raises(ValueError):
        split_session_key(session_key)
