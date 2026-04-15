"""Persistent cursor cache (Phase B): round-trip and resume semantics.

Verifies that ``PollingSource`` seeded with a ``cache_path`` persists
per-file byte offsets between invocations and that a second
``PollingSource`` constructed against the same cache file resumes each
file from its saved offset instead of re-reading from byte zero.

Invalidation cases:

- Missing cache file → empty cache, full replay.
- Version mismatch → empty cache.
- Root mismatch → empty cache.
- Inode mismatch (rotation) → entry invalidated per-file; full replay
  of that file only.
- Size shrank (truncate) → entry invalidated; full replay from 0.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from codevigil.watcher import PollingSource, SourceEvent, SourceEventKind
from codevigil.watcher_cache import CachedCursor, CursorStore, default_cache_path
from tests._watcher_helpers import (
    install_error_log,
    reset_error_log,
    session_dir,
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    install_error_log(tmp_path / "errors.log")
    yield tmp_path
    reset_error_log()


def _appends(events: list[SourceEvent]) -> list[str]:
    return [ev.line for ev in events if ev.kind is SourceEventKind.APPEND and ev.line is not None]


def _kinds(events: list[SourceEvent]) -> list[SourceEventKind]:
    return [ev.kind for ev in events]


# ---------------------------------------------------------------------------
# CursorStore round-trip
# ---------------------------------------------------------------------------


def test_cursor_store_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    cache_path = tmp_path / "cache.json"

    store = CursorStore(cache_path, root)
    entry_path = root / "session.jsonl"
    entry_path.write_bytes(b"ignored\n")
    store.save(
        {
            entry_path: CachedCursor(
                inode=12345,
                size=98765,
                offset=98765,
                pending=b"partial",
                mtime=1712345678.5,
            )
        }
    )

    loaded = CursorStore(cache_path, root).load()
    assert entry_path in loaded
    got = loaded[entry_path]
    assert got.inode == 12345
    assert got.size == 98765
    assert got.offset == 98765
    assert got.pending == b"partial"
    assert got.mtime == pytest.approx(1712345678.5)


def test_cursor_store_missing_file_returns_empty(tmp_path: Path) -> None:
    store = CursorStore(tmp_path / "nope.json", tmp_path)
    assert store.load() == {}


def test_cursor_store_version_mismatch_returns_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps({"version": 999, "root": str(tmp_path), "files": []}),
        encoding="utf-8",
    )
    assert CursorStore(cache_path, tmp_path).load() == {}


def test_cursor_store_root_mismatch_returns_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    store = CursorStore(cache_path, tmp_path / "a")
    store.save({})  # writes root="/tmp/.../a"
    other = CursorStore(cache_path, tmp_path / "b").load()
    assert other == {}


def test_cursor_store_corrupt_json_returns_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("not json {", encoding="utf-8")
    assert CursorStore(cache_path, tmp_path).load() == {}


def test_default_cache_path_is_stable_per_root(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    a = default_cache_path(state_dir, tmp_path / "root_a")
    a2 = default_cache_path(state_dir, tmp_path / "root_a")
    b = default_cache_path(state_dir, tmp_path / "root_b")
    assert a == a2
    assert a != b


# ---------------------------------------------------------------------------
# PollingSource resume via cache
# ---------------------------------------------------------------------------


def test_polling_source_no_cache_backward_compat(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    (sessions / "s.jsonl").write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")

    src = PollingSource(fake_home / ".claude" / "projects")
    events = list(src.poll())
    assert _appends(events) == ['{"a":1}', '{"b":2}']
    src.close()


def test_polling_source_persists_offsets_on_close(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    path = sessions / "s.jsonl"
    path.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
    cache_path = fake_home / "cache.json"

    src = PollingSource(fake_home / ".claude" / "projects", cache_path=cache_path)
    list(src.poll())
    src.close()

    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert data["version"] == 1
    files = data["files"]
    assert len(files) == 1
    assert files[0]["path"] == str(path.resolve())
    # Both lines consumed → offset matches file size.
    assert files[0]["offset"] == path.stat().st_size
    assert files[0]["size"] == path.stat().st_size


def test_polling_source_resume_reads_only_tail(fake_home: Path) -> None:
    sessions = session_dir(fake_home)
    path = sessions / "s.jsonl"
    path.write_text('{"old":1}\n{"old":2}\n', encoding="utf-8")
    cache_path = fake_home / "cache.json"

    # First run: ingest initial two lines, flush cache.
    src = PollingSource(fake_home / ".claude" / "projects", cache_path=cache_path)
    first = list(src.poll())
    assert _appends(first) == ['{"old":1}', '{"old":2}']
    src.close()

    # Append one more line; second run should emit only the new line.
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"new":3}\n')
    # Force mtime forward so the file's identity signal (inode, size) is
    # unchanged apart from growth.
    os.utime(path, None)

    src2 = PollingSource(fake_home / ".claude" / "projects", cache_path=cache_path)
    second = list(src2.poll())
    # NEW_SESSION fires because the aggregator has no in-memory context —
    # but only the tail line is emitted as APPEND.
    kinds = _kinds(second)
    assert SourceEventKind.NEW_SESSION in kinds
    assert _appends(second) == ['{"new":3}']
    src2.close()


def test_polling_source_resume_unchanged_file_emits_no_appends(
    fake_home: Path,
) -> None:
    sessions = session_dir(fake_home)
    path = sessions / "s.jsonl"
    path.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
    cache_path = fake_home / "cache.json"

    src = PollingSource(fake_home / ".claude" / "projects", cache_path=cache_path)
    list(src.poll())
    src.close()

    src2 = PollingSource(fake_home / ".claude" / "projects", cache_path=cache_path)
    second = list(src2.poll())
    assert _appends(second) == []
    # Exactly one NEW_SESSION, zero APPEND.
    assert _kinds(second) == [SourceEventKind.NEW_SESSION]
    src2.close()


def test_polling_source_resume_invalidated_on_size_shrink(
    fake_home: Path,
) -> None:
    sessions = session_dir(fake_home)
    path = sessions / "s.jsonl"
    path.write_text('{"a":1}\n{"b":2}\n{"c":3}\n', encoding="utf-8")
    cache_path = fake_home / "cache.json"

    src = PollingSource(fake_home / ".claude" / "projects", cache_path=cache_path)
    list(src.poll())
    src.close()

    # Truncate + rewrite with shorter content.
    path.write_text('{"fresh":1}\n', encoding="utf-8")

    src2 = PollingSource(fake_home / ".claude" / "projects", cache_path=cache_path)
    events = list(src2.poll())
    # Shrink invalidates the seed → full replay from offset 0.
    assert _appends(events) == ['{"fresh":1}']
    src2.close()


def test_polling_source_resume_invalidated_on_inode_change(
    fake_home: Path,
) -> None:
    sessions = session_dir(fake_home)
    path = sessions / "s.jsonl"
    path.write_text('{"a":1}\n', encoding="utf-8")
    cache_path = fake_home / "cache.json"

    src = PollingSource(fake_home / ".claude" / "projects", cache_path=cache_path)
    list(src.poll())
    src.close()

    # Rewrite via delete+create → new inode, even if content looks similar.
    path.unlink()
    path.write_text('{"rotated":1}\n{"rotated":2}\n', encoding="utf-8")

    src2 = PollingSource(fake_home / ".claude" / "projects", cache_path=cache_path)
    events = list(src2.poll())
    assert _appends(events) == ['{"rotated":1}', '{"rotated":2}']
    src2.close()


def test_polling_source_disabled_cache_writes_nothing(fake_home: Path) -> None:
    """When no ``cache_path`` is supplied the cache is inert."""
    sessions = session_dir(fake_home)
    (sessions / "s.jsonl").write_text('{"a":1}\n', encoding="utf-8")

    src = PollingSource(fake_home / ".claude" / "projects")
    list(src.poll())
    src.close()

    # No cache file was written anywhere under HOME.
    assert not any(p.name.startswith("cursor_cache") for p in fake_home.rglob("*"))
