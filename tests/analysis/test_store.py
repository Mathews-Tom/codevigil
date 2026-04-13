"""Integration tests for codevigil.analysis.store.

Covers the round-trip path (build_report → SessionStore.write →
SessionStore.list_reports / get_report), migration mechanics, validation,
and the XDG_STATE_HOME resolution logic.

These are integration tests because they exercise real filesystem I/O via
temporary directories. Each test uses a fresh tmp dir so tests cannot
interfere with each other or with any pre-existing ~/.local/state/codevigil/
content on the host machine.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codevigil.analysis.store import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    SessionReport,
    SessionStore,
    StoreError,
    _default_sessions_dir,
    build_report,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(minutes=30)


def _make_report(
    session_id: str = "agent-abc123",
    *,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    metrics: dict[str, float] | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    project_hash: str = "abc12345",
    event_count: int = 120,
    parse_confidence: float = 0.98,
) -> SessionReport:
    return build_report(
        session_id=session_id,
        project_hash=project_hash,
        project_name=None,
        model=model,
        permission_mode=permission_mode,
        started_at=started_at or _T0,
        ended_at=ended_at or _T1,
        event_count=event_count,
        parse_confidence=parse_confidence,
        metrics=metrics or {"read_edit_ratio": 5.2, "reasoning_loop": 8.3},
    )


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


def test_build_report_duration_calculated() -> None:
    r = _make_report(started_at=_T0, ended_at=_T1)
    assert r.duration_seconds == 1800.0


def test_build_report_schema_version_current() -> None:
    r = _make_report()
    assert r.schema_version == CURRENT_SCHEMA_VERSION


def test_build_report_metrics_stored_as_float() -> None:
    r = _make_report(metrics={"ratio": 3})
    assert isinstance(r.metrics["ratio"], float)


def test_build_report_null_model_and_permission_mode() -> None:
    r = _make_report()
    assert r.model is None
    assert r.permission_mode is None


def test_build_report_with_model_and_permission_mode() -> None:
    r = _make_report(model="gpt-5", permission_mode="default")
    assert r.model == "gpt-5"
    assert r.permission_mode == "default"


def test_build_report_as_dict_roundtrip() -> None:
    r = _make_report()
    d = r.as_dict()
    assert d["session_id"] == "agent-abc123"
    assert d["schema_version"] == CURRENT_SCHEMA_VERSION
    assert isinstance(d["metrics"], dict)


def test_build_report_missing_metrics_raises() -> None:
    # Manually construct a broken dict
    with pytest.raises(StoreError, match="missing required field"):
        SessionReport.from_dict({"schema_version": 1, "session_id": "x"})


# ---------------------------------------------------------------------------
# SessionReport.from_dict — validation and migration
# ---------------------------------------------------------------------------


def test_from_dict_valid_record() -> None:
    data = _make_report().as_dict()
    r = SessionReport.from_dict(data)
    assert r.session_id == "agent-abc123"


def test_from_dict_missing_schema_version_raises() -> None:
    data = _make_report().as_dict()
    del data["schema_version"]
    with pytest.raises(MigrationError, match="missing schema_version"):
        SessionReport.from_dict(data)


def test_from_dict_non_int_schema_version_raises() -> None:
    data = _make_report().as_dict()
    data["schema_version"] = "1"
    with pytest.raises(MigrationError, match="missing schema_version"):
        SessionReport.from_dict(data)


def test_from_dict_future_schema_version_raises() -> None:
    data = _make_report().as_dict()
    data["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    with pytest.raises(MigrationError, match="newer than supported"):
        SessionReport.from_dict(data)


def test_from_dict_missing_required_field_raises() -> None:
    data = _make_report().as_dict()
    del data["started_at"]
    with pytest.raises(StoreError, match="missing required field"):
        SessionReport.from_dict(data)


def test_from_dict_metrics_not_dict_raises() -> None:
    data = _make_report().as_dict()
    data["metrics"] = [1, 2, 3]
    with pytest.raises(StoreError, match="must be a dict"):
        SessionReport.from_dict(data)


# ---------------------------------------------------------------------------
# SessionStore — round-trip write / list_reports
# ---------------------------------------------------------------------------


def test_store_write_creates_file(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    report = _make_report()
    path = store.write(report)
    assert path.exists()
    assert path.suffix == ".json"
    assert path.name == "agent-abc123.json"


def test_store_write_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    store = SessionStore(base_dir=target)
    report = _make_report()
    store.write(report)
    assert target.exists()


def test_store_write_content_valid_json(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    report = _make_report()
    path = store.write(report)
    loaded = json.loads(path.read_text())
    assert loaded["session_id"] == "agent-abc123"
    assert loaded["schema_version"] == CURRENT_SCHEMA_VERSION


def test_store_write_atomic_overwrites_existing(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    r1 = build_report(
        session_id="s1",
        project_hash="p1",
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=_T0,
        ended_at=_T1,
        event_count=10,
        parse_confidence=0.9,
        metrics={"m": 1.0},
    )
    r2 = build_report(
        session_id="s1",
        project_hash="p1",
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=_T0,
        ended_at=_T1,
        event_count=20,
        parse_confidence=0.9,
        metrics={"m": 2.0},
    )
    store.write(r1)
    store.write(r2)
    reloaded = store.get_report("s1")
    assert reloaded is not None
    assert reloaded.event_count == 20
    assert reloaded.metrics["m"] == 2.0


def test_store_list_reports_empty_dir(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    assert store.list_reports() == []


def test_store_list_reports_nonexistent_dir(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "does-not-exist")
    assert store.list_reports() == []


def test_store_list_reports_returns_all(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    store.write(_make_report("s1"))
    store.write(_make_report("s2"))
    store.write(_make_report("s3"))
    reports = store.list_reports()
    assert len(reports) == 3
    ids = {r.session_id for r in reports}
    assert ids == {"s1", "s2", "s3"}


def test_store_list_reports_sorted_by_started_at(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    t_base = _T0
    for i in range(5):
        store.write(
            build_report(
                session_id=f"s{i}",
                project_hash="p",
                project_name=None,
                model=None,
                permission_mode=None,
                started_at=t_base + timedelta(hours=i),
                ended_at=t_base + timedelta(hours=i, minutes=30),
                event_count=10,
                parse_confidence=0.9,
                metrics={"m": float(i)},
            )
        )
    reports = store.list_reports()
    times = [r.started_at for r in reports]
    assert times == sorted(times)


def test_store_list_reports_since_filter(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    t_old = _T0 - timedelta(days=2)
    store.write(
        build_report(
            session_id="old",
            project_hash="p",
            project_name=None,
            model=None,
            permission_mode=None,
            started_at=t_old,
            ended_at=t_old + timedelta(minutes=10),
            event_count=5,
            parse_confidence=0.9,
            metrics={},
        )
    )
    store.write(_make_report("new", started_at=_T0, ended_at=_T1))
    result = store.list_reports(since=_T0)
    assert len(result) == 1
    assert result[0].session_id == "new"


def test_store_list_reports_until_filter(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    t_future = _T0 + timedelta(days=7)
    store.write(_make_report("past", started_at=_T0, ended_at=_T1))
    store.write(
        build_report(
            session_id="future",
            project_hash="p",
            project_name=None,
            model=None,
            permission_mode=None,
            started_at=t_future,
            ended_at=t_future + timedelta(minutes=10),
            event_count=5,
            parse_confidence=0.9,
            metrics={},
        )
    )
    result = store.list_reports(until=_T0)
    assert len(result) == 1
    assert result[0].session_id == "past"


def test_store_list_reports_skips_non_json(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    # Plant a non-.json file
    (tmp_path / "ignore.txt").write_text("not json")
    store.write(_make_report())
    reports = store.list_reports()
    assert len(reports) == 1


def test_store_list_reports_skips_corrupt_json(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    store.write(_make_report("good"))
    (tmp_path / "corrupt.json").write_text("{bad json}")
    reports = store.list_reports()
    assert len(reports) == 1
    assert reports[0].session_id == "good"


def test_store_list_reports_skips_tmp_files(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    store.write(_make_report("real"))
    (tmp_path / ".real.tmp").write_text('{"schema_version":1}')
    reports = store.list_reports()
    assert len(reports) == 1


def test_store_get_report_existing(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    store.write(_make_report("target"))
    r = store.get_report("target")
    assert r is not None
    assert r.session_id == "target"


def test_store_get_report_missing_returns_none(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    assert store.get_report("no-such-session") is None


def test_store_activation_logged_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = SessionStore(base_dir=tmp_path)
    import logging

    with caplog.at_level(logging.INFO, logger="codevigil.analysis.store"):
        store.write(_make_report("s1"))
        store.write(_make_report("s2"))
    activation_messages = [r for r in caplog.records if "persistence enabled" in r.message]
    assert len(activation_messages) == 1


# ---------------------------------------------------------------------------
# XDG_STATE_HOME resolution
# ---------------------------------------------------------------------------


def test_default_sessions_dir_uses_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    result = _default_sessions_dir()
    assert result == tmp_path / "codevigil" / "sessions"


def test_default_sessions_dir_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    result = _default_sessions_dir()
    expected = Path.home() / ".local" / "state" / "codevigil" / "sessions"
    assert result == expected


def test_default_sessions_dir_empty_xdg_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "   ")
    result = _default_sessions_dir()
    expected = Path.home() / ".local" / "state" / "codevigil" / "sessions"
    assert result == expected


# ---------------------------------------------------------------------------
# SessionReport property coverage
# ---------------------------------------------------------------------------


def test_session_report_all_properties() -> None:
    r = _make_report(
        session_id="prop-test",
        project_hash="phash",
        model="gpt-5",
        permission_mode="default",
        event_count=42,
        parse_confidence=0.95,
        metrics={"r": 3.1, "s": 0.5},
    )
    assert r.session_id == "prop-test"
    assert r.project_hash == "phash"
    assert r.project_name is None
    assert r.model == "gpt-5"
    assert r.permission_mode == "default"
    assert r.started_at == _T0
    assert r.ended_at == _T1
    assert r.duration_seconds == 1800.0
    assert r.event_count == 42
    assert r.parse_confidence == 0.95
    assert r.metrics == {"r": 3.1, "s": 0.5}
    assert r.eviction_churn == 0
    assert r.cohort_size == 0


def test_session_report_eviction_churn_and_cohort_size() -> None:
    r = build_report(
        session_id="x",
        project_hash="p",
        project_name=None,
        model=None,
        permission_mode=None,
        started_at=_T0,
        ended_at=_T1,
        event_count=5,
        parse_confidence=0.9,
        metrics={},
        eviction_churn=3,
        cohort_size=12,
    )
    assert r.eviction_churn == 3
    assert r.cohort_size == 12


# ---------------------------------------------------------------------------
# store.list_reports skips unmigrateable records
# ---------------------------------------------------------------------------


def test_store_list_skips_future_schema_version(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    store.write(_make_report("good"))
    future_data = _make_report("bad").as_dict()
    future_data["schema_version"] = CURRENT_SCHEMA_VERSION + 999
    (tmp_path / "bad.json").write_text(json.dumps(future_data))
    reports = store.list_reports()
    assert len(reports) == 1
    assert reports[0].session_id == "good"


# ---------------------------------------------------------------------------
# Additional coverage: uncovered branches
# ---------------------------------------------------------------------------


def test_from_dict_below_minimum_version_raises() -> None:
    data = _make_report().as_dict()
    data["schema_version"] = 0  # below _MINIMUM_SUPPORTED_VERSION (1)
    with pytest.raises(MigrationError, match="below the minimum"):
        SessionReport.from_dict(data)


def test_store_base_dir_property(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    assert store.base_dir == tmp_path


def test_store_get_report_corrupt_returns_none_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = SessionStore(base_dir=tmp_path)
    (tmp_path / "bad-report.json").write_text("{corrupted json")
    import logging

    with caplog.at_level(logging.WARNING, logger="codevigil.analysis.store"):
        result = store.get_report("bad-report")
    assert result is None
    assert any("bad-report" in r.message for r in caplog.records)


def test_parse_dt_accepts_datetime() -> None:
    from codevigil.analysis.store import _parse_dt

    dt = datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_dt(dt) == dt


def test_parse_dt_accepts_string() -> None:
    from codevigil.analysis.store import _parse_dt

    dt = _parse_dt("2026-04-14T10:00:00+00:00")
    assert dt.year == 2026


def test_parse_dt_invalid_type_raises() -> None:
    from codevigil.analysis.store import _parse_dt

    with pytest.raises(StoreError, match="cannot parse timestamp"):
        _parse_dt(12345)


def test_store_write_exception_cleans_up_tmp_file(tmp_path: Path) -> None:
    """Verify no orphaned .tmp files remain when write is interrupted.

    We simulate a failure by making the destination path a directory,
    which causes the rename to fail on macOS.
    """
    store = SessionStore(base_dir=tmp_path)
    report = _make_report("atomic-test")
    # Create a directory where the output file would go — rename fails
    dest = tmp_path / "atomic-test.json"
    dest.mkdir()  # destination is now a dir, rename will fail on macOS
    with contextlib.suppress(Exception):
        store.write(report)
    # No .tmp files should remain
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []
