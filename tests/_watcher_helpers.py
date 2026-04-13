"""Shared helpers for watcher tests.

Wires the process-wide error channel to a per-test JSONL file so tests can
assert the exact ``code`` values emitted by the watcher.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from codevigil.errors import (
    ErrorChannel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)


def install_error_log(path: Path) -> None:
    set_error_channel(ErrorChannel(RotatingJsonlWriter(path)))


def reset_error_log() -> None:
    reset_error_channel()


def read_error_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def read_error_codes(path: Path) -> list[str]:
    return [str(rec["code"]) for rec in read_error_records(path)]


def session_dir(home: Path, project: str = "proj") -> Path:
    """Create a ``~/.claude/projects/<project>/sessions/`` directory under home."""

    d = home / ".claude" / "projects" / project / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def iter_kinds(events: Iterator[object]) -> list[str]:
    """Return the ``kind.value`` of each event in iteration order."""

    out: list[str] = []
    for ev in events:
        kind = getattr(ev, "kind", None)
        if kind is None:
            continue
        out.append(str(kind.value))
    return out
