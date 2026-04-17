"""ProcessedSessionStore (Phase C1): schema, CRUD, round-trip."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from codevigil.analysis.processed_store import (
    ProcessedMetric,
    ProcessedSession,
    ProcessedSessionStore,
    ProcessedStoreError,
    default_db_path,
)
from codevigil.watch_roots import LEGACY_ROOT_ID, legacy_session_key


def _make_record(session_id: str = "agent-abc123") -> ProcessedSession:
    now = datetime.now(tz=UTC)
    return ProcessedSession(
        session_key=legacy_session_key(session_id),
        root_id=LEGACY_ROOT_ID,
        session_id=session_id,
        path=Path(f"/tmp/proj/{session_id}.jsonl"),
        inode=12345,
        size=1024,
        offset=1024,
        pending=b"",
        mtime=1712345678.5,
        project_hash="abc123",
        project_name="Open-ASM",
        first_event_time=now,
        last_event_time=now,
        event_count=10,
        session_task_type="exploration",
        collector_state={"read_edit_ratio": {"window": [1.0, 2.0]}},
        metrics=[
            ProcessedMetric(
                collector_name="parse_health",
                metric_name="parse_health",
                value=1.0,
                severity="ok",
                label="parse healthy",
                detail={"confidence": 1.0},
            ),
            ProcessedMetric(
                collector_name="read_edit_ratio",
                metric_name="read_edit_ratio",
                value=3.5,
                severity="warn",
                label="3.5 reads per edit",
            ),
        ],
    )


def test_store_creates_schema_on_first_open(tmp_path: Path) -> None:
    db = tmp_path / "codevigil.db"
    store = ProcessedSessionStore(db)
    store.open()
    try:
        assert db.exists()
        assert store.count() == 0
    finally:
        store.close()


def test_store_roundtrip_full_record(tmp_path: Path) -> None:
    db = tmp_path / "codevigil.db"
    record = _make_record()
    with ProcessedSessionStore(db) as store:
        store.upsert_session(record)
        assert store.count() == 1

        got = store.get_session(record.session_id)
        assert got is not None
        assert got.session_key == record.session_key
        assert got.root_id == record.root_id
        assert got.session_id == record.session_id
        assert got.path == record.path
        assert got.inode == record.inode
        assert got.size == record.size
        assert got.offset == record.offset
        assert got.pending == b""
        assert got.mtime == pytest.approx(record.mtime)
        assert got.project_hash == record.project_hash
        assert got.project_name == record.project_name
        assert got.event_count == record.event_count
        assert got.session_task_type == record.session_task_type
        assert got.collector_state == record.collector_state

        assert len(got.metrics) == 2
        parse_metric = next(m for m in got.metrics if m.collector_name == "parse_health")
        assert parse_metric.value == pytest.approx(1.0)
        assert parse_metric.severity == "ok"
        assert parse_metric.detail == {"confidence": 1.0}


def test_store_roundtrip_pending_bytes(tmp_path: Path) -> None:
    """``pending`` bytes must round-trip through base64 exactly."""
    db = tmp_path / "codevigil.db"
    record = _make_record()
    record.pending = b"\x00\xff\x01partial\n\xfe"
    with ProcessedSessionStore(db) as store:
        store.upsert_session(record)
        got = store.get_session(record.session_id)
    assert got is not None
    assert got.pending == record.pending


def test_store_get_by_path(tmp_path: Path) -> None:
    db = tmp_path / "codevigil.db"
    record = _make_record()
    with ProcessedSessionStore(db) as store:
        store.upsert_session(record)
        got = store.get_by_path(record.path)
    assert got is not None
    assert got.session_id == record.session_id


def test_store_upsert_replaces_existing(tmp_path: Path) -> None:
    db = tmp_path / "codevigil.db"
    record = _make_record()
    with ProcessedSessionStore(db) as store:
        store.upsert_session(record)
        record.size = 2048
        record.offset = 2048
        record.event_count = 25
        record.metrics = []  # drop all metrics
        store.upsert_session(record)
        assert store.count() == 1
        got = store.get_session(record.session_id)
    assert got is not None
    assert got.size == 2048
    assert got.event_count == 25
    assert got.metrics == []


def test_store_delete_cascade(tmp_path: Path) -> None:
    db = tmp_path / "codevigil.db"
    record = _make_record()
    with ProcessedSessionStore(db) as store:
        store.upsert_session(record)
        store.delete_session(record.session_id)
        assert store.count() == 0
        assert store.get_session(record.session_id) is None


def test_store_iter_all_ordered_by_last_event_desc(tmp_path: Path) -> None:
    db = tmp_path / "codevigil.db"
    with ProcessedSessionStore(db) as store:
        older = _make_record("agent-older")
        newer = _make_record("agent-newer")
        older.last_event_time = datetime(2026, 1, 1, tzinfo=UTC)
        newer.last_event_time = datetime(2026, 4, 1, tzinfo=UTC)
        store.upsert_session(older)
        store.upsert_session(newer)

        ids = [s.session_id for s in store.iter_all()]
    assert ids == ["agent-newer", "agent-older"]


def test_store_missing_session_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "codevigil.db"
    with ProcessedSessionStore(db) as store:
        assert store.get_session("nonexistent") is None
        assert store.get_by_path(Path("/does/not/exist.jsonl")) is None


def test_store_schema_mismatch_raises(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "codevigil.db"
    # Pre-create a DB with a mismatched schema version.
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_version (version) VALUES (999)")
    conn.commit()
    conn.close()

    store = ProcessedSessionStore(db)
    with pytest.raises(ProcessedStoreError) as exc_info:
        store.open()
    assert "schema_mismatch" in exc_info.value.code


def test_store_migrates_v1_database(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "codevigil.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.execute(
        """
        CREATE TABLE processed_sessions (
            session_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            inode INTEGER NOT NULL,
            size INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            pending_b64 TEXT NOT NULL DEFAULT '',
            mtime REAL NOT NULL,
            project_hash TEXT NOT NULL,
            project_name TEXT,
            first_event_time TEXT NOT NULL,
            last_event_time TEXT NOT NULL,
            event_count INTEGER NOT NULL,
            session_task_type TEXT,
            collector_state_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE processed_metrics (
            session_id TEXT NOT NULL,
            collector_name TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            value REAL NOT NULL,
            severity TEXT NOT NULL,
            label TEXT NOT NULL,
            detail_json TEXT,
            PRIMARY KEY (session_id, collector_name, metric_name)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO processed_sessions (
            session_id, path, inode, size, offset, pending_b64, mtime,
            project_hash, project_name, first_event_time, last_event_time,
            event_count, session_task_type, collector_state_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "agent-v1",
            "/tmp/proj/agent-v1.jsonl",
            1,
            100,
            100,
            "",
            1712345678.5,
            "abc123",
            "Open-ASM",
            datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
            datetime(2026, 1, 2, tzinfo=UTC).isoformat(),
            2,
            "exploration",
            "{}",
            datetime(2026, 1, 2, tzinfo=UTC).isoformat(),
        ),
    )
    conn.execute(
        """
        INSERT INTO processed_metrics (
            session_id, collector_name, metric_name, value, severity, label, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("agent-v1", "parse_health", "parse_health", 1.0, "ok", "healthy", None),
    )
    conn.commit()
    conn.close()

    with ProcessedSessionStore(db) as store:
        got = store.get_session("agent-v1")
        assert got is not None
        assert got.root_id == LEGACY_ROOT_ID
        assert got.session_key == legacy_session_key("agent-v1")
        assert len(got.metrics) == 1


def test_iter_recent_project_aggregates(tmp_path: Path) -> None:
    """Top-N recent projects are grouped by project_name with session
    counts and the latest session's metrics."""
    db = tmp_path / "codevigil.db"
    with ProcessedSessionStore(db) as store:
        # Two sessions in Open-ASM, one in codevigil.
        a = _make_record("agent-a")
        a.project_name = "Open-ASM"
        a.last_event_time = datetime(2026, 3, 1, tzinfo=UTC)
        a2 = _make_record("agent-a2")
        a2.project_name = "Open-ASM"
        a2.last_event_time = datetime(2026, 4, 1, tzinfo=UTC)
        b = _make_record("agent-b")
        b.project_name = "codevigil"
        b.last_event_time = datetime(2026, 2, 1, tzinfo=UTC)
        store.upsert_session(a)
        store.upsert_session(a2)
        store.upsert_session(b)

        aggregates = store.iter_recent_project_aggregates(10)

    assert [agg.project_key for agg in aggregates] == ["Open-ASM", "codevigil"]
    open_asm = aggregates[0]
    assert open_asm.session_count == 2
    assert open_asm.last_event_time == datetime(2026, 4, 1, tzinfo=UTC)
    assert len(open_asm.metrics) == 2
    codevigil = aggregates[1]
    assert codevigil.session_count == 1
    assert codevigil.last_event_time == datetime(2026, 2, 1, tzinfo=UTC)


def test_iter_recent_project_aggregates_limit(tmp_path: Path) -> None:
    db = tmp_path / "codevigil.db"
    with ProcessedSessionStore(db) as store:
        for i in range(5):
            r = _make_record(f"agent-{i}")
            r.project_name = f"proj-{i}"
            r.last_event_time = datetime(2026, 4, i + 1, tzinfo=UTC)
            store.upsert_session(r)
        aggregates = store.iter_recent_project_aggregates(3)
    assert len(aggregates) == 3
    assert aggregates[0].project_key == "proj-4"
    assert aggregates[1].project_key == "proj-3"
    assert aggregates[2].project_key == "proj-2"


def test_default_db_path_is_under_home() -> None:
    path = default_db_path()
    assert path.is_absolute()
    assert "codevigil" in str(path)
    assert path.name == "processed_sessions.db"
