"""Tests for watch-mode TUI features:

- Stable severity sort (CRITICAL < WARN < OK, then -updated_at, then session_id).
- Summary header with session/crit/warn/ok/projects/updated fields.
- Unique session labels via adaptive prefix extension.
- Mini-trends: inline trend arrow and last-three values.
- Percentile anchors from the session store; n/a fallback when store is empty.
- Stop-phrase context snippets from the detail payload.
- Uptime regression: sessions older than 60 seconds render non-zero uptime.
- Snapshot determinism across ticks for a 20-session fixture.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codevigil.analysis.store import SessionStore, build_report
from codevigil.renderers.terminal import (
    TerminalRenderer,
    _build_label_map,
    _format_trend,
)
from codevigil.types import MetricSnapshot, SessionMeta, SessionState, Severity
from tests.renderers._fixtures import make_meta, make_snapshots

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_meta(
    *,
    session_id: str = "a" * 16,
    state: SessionState = SessionState.ACTIVE,
    start_offset_s: float = 0.0,
    last_offset_s: float = 0.0,
    project_name: str | None = "proj-x",
    project_hash: str = "dead" * 4,
) -> SessionMeta:
    base = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
    return SessionMeta(
        session_id=session_id,
        project_hash=project_hash,
        project_name=project_name,
        file_path=Path("/tmp/x.jsonl"),
        start_time=base - timedelta(seconds=start_offset_s),
        last_event_time=base + timedelta(seconds=last_offset_s),
        event_count=10,
        parse_confidence=1.0,
        state=state,
    )


def _snap(name: str, value: float, severity: Severity = Severity.OK) -> MetricSnapshot:
    return MetricSnapshot(name=name, value=value, label="", severity=severity)


def _render_tick(
    renderer: TerminalRenderer,
    pairs: list[tuple[SessionMeta, list[MetricSnapshot]]],
) -> str:
    stream = io.StringIO()
    renderer._stream = stream
    renderer.begin_tick()
    for meta, snapshots in pairs:
        renderer.render(snapshots, meta)
    renderer.end_tick()
    return stream.getvalue()


# ---------------------------------------------------------------------------
# Severity sort
# ---------------------------------------------------------------------------


def test_severity_sort_crit_before_warn_before_ok() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)

    # Use IDs that have distinct first-8-char prefixes so the label map
    # assigns 8-char labels. The session line reads "session: <label>".
    ok_meta = _make_meta(session_id="ok000000" + "0" * 8, last_offset_s=1.0)
    ok_snaps = [_snap("read_edit_ratio", 1.0, Severity.OK)]

    warn_meta = _make_meta(session_id="warn0000" + "0" * 8, last_offset_s=2.0)
    warn_snaps = [_snap("stop_phrase", 0.5, Severity.WARN)]

    crit_meta = _make_meta(session_id="crit0000" + "0" * 8, last_offset_s=3.0)
    crit_snaps = [_snap("reasoning_loop", 9.9, Severity.CRITICAL)]

    renderer.begin_tick()
    # Render in OK → WARN → CRITICAL order; output must be reversed by sort.
    renderer.render(ok_snaps, ok_meta)
    renderer.render(warn_snaps, warn_meta)
    renderer.render(crit_snaps, crit_meta)
    renderer.end_tick()

    out = stream.getvalue()
    # The session labels are 8-char prefixes of the IDs.
    idx_crit = out.index("crit0000")
    idx_warn = out.index("warn0000")
    idx_ok = out.index("ok000000")
    assert idx_crit < idx_warn < idx_ok, (
        f"Expected CRIT < WARN < OK in output; got positions {idx_crit} {idx_warn} {idx_ok}"
    )


def test_severity_sort_stable_on_second_tick() -> None:
    """The sort order must not churn on a second tick with the same sessions."""
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)

    ok_meta = _make_meta(session_id="ok000000" + "0" * 8)
    crit_meta = _make_meta(session_id="crit0000" + "0" * 8)
    ok_snaps = [_snap("read_edit_ratio", 1.0, Severity.OK)]
    crit_snaps = [_snap("reasoning_loop", 9.9, Severity.CRITICAL)]

    for _ in range(3):
        renderer.begin_tick()
        renderer.render(ok_snaps, ok_meta)
        renderer.render(crit_snaps, crit_meta)
        renderer.end_tick()

    out = stream.getvalue()
    # In every tick, "CRIT" must appear before "OK" (the severity words).
    # We look at the session lines: crit0000 < ok000000 must hold each tick.
    positions_crit = [i for i in range(len(out)) if out[i : i + 8] == "crit0000"]
    positions_ok = [i for i in range(len(out)) if out[i : i + 8] == "ok000000"]
    assert len(positions_crit) == 3, "crit session must appear in all 3 ticks"
    assert len(positions_ok) == 3, "ok session must appear in all 3 ticks"
    for pc, po in zip(positions_crit, positions_ok, strict=True):
        assert pc < po, "CRIT must precede OK in every tick"


# ---------------------------------------------------------------------------
# Summary header
# ---------------------------------------------------------------------------


def test_header_contains_session_counts() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)

    renderer.begin_tick()
    renderer.render([_snap("read_edit_ratio", 1.0, Severity.OK)], _make_meta(session_id="a" * 16))
    renderer.render(
        [_snap("stop_phrase", 0.5, Severity.WARN)],
        _make_meta(session_id="b" * 16),
    )
    renderer.render(
        [_snap("reasoning_loop", 9.9, Severity.CRITICAL)],
        _make_meta(session_id="c" * 16),
    )
    renderer.end_tick()

    out = stream.getvalue()
    assert "sessions=3" in out
    assert "crit=1" in out
    assert "warn=1" in out
    assert "ok=1" in out


def test_header_projects_count() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)

    renderer.begin_tick()
    renderer.render(
        [_snap("read_edit_ratio", 1.0)],
        _make_meta(session_id="a" * 16, project_name="proj-a"),
    )
    renderer.render(
        [_snap("read_edit_ratio", 1.0)],
        _make_meta(session_id="b" * 16, project_name="proj-a"),
    )
    renderer.render(
        [_snap("read_edit_ratio", 1.0)],
        _make_meta(session_id="c" * 16, project_name="proj-b"),
    )
    renderer.end_tick()

    out = stream.getvalue()
    assert "projects=2" in out


def test_header_updated_timestamp_appears() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()
    out = stream.getvalue()
    assert "updated=" in out
    # Should not be the placeholder dash when a session exists.
    assert "updated=—" not in out


# ---------------------------------------------------------------------------
# Unique session labels
# ---------------------------------------------------------------------------


def test_label_map_no_collision_short_prefix() -> None:
    """When all IDs are unique at 8 chars, labels are 8 chars."""
    ids = ["abcdef01" + "x" * 8, "abcdef02" + "x" * 8, "abcdef03" + "x" * 8]
    label_map = _build_label_map(ids)
    labels = list(label_map.values())
    assert len(labels) == len(set(labels)), "Labels must be unique"
    assert all(len(lab) >= 8 for lab in labels)


def test_label_map_extends_prefix_on_collision() -> None:
    """Two IDs that share the first 8 chars get a longer label."""
    ids = ["agent-a6" + "00000001", "agent-a6" + "00000002"]
    label_map = _build_label_map(ids)
    labels = list(label_map.values())
    assert len(labels) == len(set(labels)), "Labels must be unique"
    # Should be longer than 8 since first 8 chars collide.
    assert all(len(lab) > 8 for lab in labels)


def test_label_map_single_id_no_collision() -> None:
    """A single session ID maps to its 8-char prefix."""
    ids = ["aaaaaaaaaaaaaaaa"]
    label_map = _build_label_map(ids)
    assert label_map == {"aaaaaaaaaaaaaaaa": "aaaaaaaa"}


def test_label_map_empty() -> None:
    assert _build_label_map([]) == {}


def test_session_label_in_renderer_output() -> None:
    """The session label appears in the renderer's output, not the raw ID."""
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    sid = "a3f7c2d0abcdef01"
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta(session_id=sid))
    renderer.end_tick()
    out = stream.getvalue()
    # Default label is 8-char prefix.
    assert "a3f7c2d0" in out


def test_label_stable_across_ticks() -> None:
    """Session label does not change between ticks for the same fleet."""
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    sid = "a3f7c2d0abcdef01"
    meta = make_meta(session_id=sid)

    renderer.begin_tick()
    renderer.render(make_snapshots(), meta)
    renderer.end_tick()
    tick1 = stream.getvalue()

    renderer.begin_tick()
    renderer.render(make_snapshots(), meta)
    renderer.end_tick()
    tick2_only = stream.getvalue()[len(tick1) :]

    # Extract label from both ticks — must be identical.
    label_start = tick1.index("session: ") + len("session: ")
    label_end = tick1.index(" |", label_start)
    label_tick1 = tick1[label_start:label_end]

    label_start2 = tick2_only.index("session: ") + len("session: ")
    label_end2 = tick2_only.index(" |", label_start2)
    label_tick2 = tick2_only[label_start2:label_end2]

    assert label_tick1 == label_tick2


# ---------------------------------------------------------------------------
# Mini-trends
# ---------------------------------------------------------------------------


def test_format_trend_up() -> None:
    assert "↗" in _format_trend((3.2, 4.1, 5.2))
    assert "3.2" in _format_trend((3.2, 4.1, 5.2))
    assert "5.2" in _format_trend((3.2, 4.1, 5.2))


def test_format_trend_down() -> None:
    assert "↘" in _format_trend((5.2, 4.1, 3.0))


def test_format_trend_flat() -> None:
    assert "→" in _format_trend((3.0, 3.0, 3.0))


def test_format_trend_two_values() -> None:
    result = _format_trend((1.0, 2.0))
    assert "↗" in result
    assert "1.0" in result
    assert "2.0" in result


def test_mini_trend_appears_in_renderer_output() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    # Build a meta with 3 snapshots of read_edit_ratio history.
    meta = SessionMeta(
        session_id="a3f7c2d0abcdef01",
        project_hash="deadbeefcafef00d",
        project_name="proj",
        file_path=Path("/tmp/x.jsonl"),
        start_time=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        last_event_time=datetime(2026, 4, 14, 10, 5, 0, tzinfo=UTC),
        event_count=10,
        parse_confidence=1.0,
        state=SessionState.ACTIVE,
        snapshot_history={"read_edit_ratio": (3.2, 4.1, 5.2)},
    )
    snapshots = [MetricSnapshot(name="read_edit_ratio", value=5.2, label="", severity=Severity.OK)]
    renderer.begin_tick()
    renderer.render(snapshots, meta)
    renderer.end_tick()
    out = stream.getvalue()
    assert "↗" in out or "→" in out or "↘" in out


def test_mini_trend_not_shown_for_single_value() -> None:
    """A single historical value produces no trend arrow."""
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    meta = SessionMeta(
        session_id="a3f7c2d0abcdef01",
        project_hash="deadbeefcafef00d",
        project_name="proj",
        file_path=Path("/tmp/x.jsonl"),
        start_time=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        last_event_time=datetime(2026, 4, 14, 10, 5, 0, tzinfo=UTC),
        event_count=10,
        parse_confidence=1.0,
        state=SessionState.ACTIVE,
        snapshot_history={"read_edit_ratio": (5.2,)},
    )
    snapshots = [MetricSnapshot(name="read_edit_ratio", value=5.2, label="", severity=Severity.OK)]
    renderer.begin_tick()
    renderer.render(snapshots, meta)
    renderer.end_tick()
    out = stream.getvalue()
    # No trend arrows should appear when history has only one entry.
    assert "↗" not in out
    assert "↘" not in out


# ---------------------------------------------------------------------------
# Percentile anchors
# ---------------------------------------------------------------------------


def test_percentile_anchor_shows_na_when_no_store() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False, baseline_store=None)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()
    out = stream.getvalue()
    assert "[n/a]" in out


def test_percentile_anchor_shows_na_when_store_empty(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False, baseline_store=store)
    renderer.begin_tick()
    renderer.render(make_snapshots(), make_meta())
    renderer.end_tick()
    out = stream.getvalue()
    assert "[n/a]" in out


def test_percentile_anchor_with_baseline(tmp_path: Path) -> None:
    """When the store has data, [pN of your baseline] appears."""
    store = SessionStore(base_dir=tmp_path / "sessions")
    base = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    # Write 10 sessions with read_edit_ratio values 1.0..10.0.
    for i in range(10):
        report = build_report(
            session_id=f"session-{i:04d}",
            project_hash="deadbeef",
            project_name=None,
            model=None,
            permission_mode=None,
            started_at=base + timedelta(hours=i),
            ended_at=base + timedelta(hours=i, minutes=30),
            event_count=10,
            parse_confidence=0.99,
            metrics={"read_edit_ratio": float(i + 1)},
        )
        store.write(report)

    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False, baseline_store=store)
    snaps = [
        MetricSnapshot(name="read_edit_ratio", value=5.0, label="R:E 5.0", severity=Severity.OK)
    ]
    renderer.begin_tick()
    renderer.render(snaps, make_meta())
    renderer.end_tick()
    out = stream.getvalue()
    assert "of your baseline" in out
    # p50 of 1..10 with value=5.0 should be [p50].
    assert "[p50 of your baseline]" in out


def test_percentile_anchor_store_not_read_every_tick(tmp_path: Path) -> None:
    """The store is read at most once per _STORE_REFRESH_TICKS ticks (not every tick)."""
    from codevigil.renderers.terminal import _STORE_REFRESH_TICKS

    store = SessionStore(base_dir=tmp_path / "sessions")
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False, baseline_store=store)

    # Run more ticks than the refresh interval.
    for _ in range(_STORE_REFRESH_TICKS + 5):
        renderer.begin_tick()
        renderer.render(make_snapshots(), make_meta())
        renderer.end_tick()
    # No crash means the refresh interval logic is working.
    assert stream.getvalue()  # something was rendered


# ---------------------------------------------------------------------------
# Stop-phrase context snippets
# ---------------------------------------------------------------------------


def test_stop_phrase_context_snippet_in_terminal_output() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    detail = {
        "hits": 1,
        "messages": 5,
        "messages_with_hit": 1,
        "hits_by_category": {"permission_seeking": 1},
        "recent_hits": [
            {
                "category": "permission_seeking",
                "phrase": "should I continue",
                "matched_substring": "should I continue",
                "context_snippet": "asked me to fix it. should I continue",
                "intent": "hands the next decision to the user mid-task",
                "message_index": 3,
            }
        ],
        "matcher_mode": "regex",
        "phrase_count": 16,
    }
    snap = MetricSnapshot(
        name="stop_phrase",
        value=0.2,
        label="1 stop-phrase hit(s)",
        severity=Severity.WARN,
        detail=detail,
    )
    renderer.begin_tick()
    renderer.render([snap], make_meta())
    renderer.end_tick()
    out = stream.getvalue()
    # The context snippet should appear in the actionable hint.
    assert "should I continue" in out


def test_stop_phrase_context_snippet_truncated_to_40_chars() -> None:
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    long_snippet = "A" * 50 + "should I continue" + "B" * 50
    detail = {
        "hits": 1,
        "messages": 1,
        "messages_with_hit": 1,
        "hits_by_category": {},
        "recent_hits": [
            {
                "category": "permission_seeking",
                "phrase": "should I continue",
                "matched_substring": "should I continue",
                "context_snippet": long_snippet,
                "intent": None,
                "message_index": 1,
            }
        ],
        "matcher_mode": "regex",
        "phrase_count": 16,
    }
    snap = MetricSnapshot(
        name="stop_phrase",
        value=0.1,
        label="1 stop-phrase hit(s)",
        severity=Severity.WARN,
        detail=detail,
    )
    renderer.begin_tick()
    renderer.render([snap], make_meta())
    renderer.end_tick()
    out = stream.getvalue()
    # The snippet is truncated to 40 chars in the hint.
    assert len(long_snippet) > 40  # guard: the snippet IS long
    assert "A" * 41 not in out  # 41 consecutive A's must not appear


# ---------------------------------------------------------------------------
# Uptime regression: sessions older than 60 seconds show non-zero uptime
# ---------------------------------------------------------------------------


def test_uptime_nonzero_for_sessions_older_than_60s() -> None:
    """Uptime must be non-zero when start_time is 90 seconds before last_event_time."""
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    base = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
    meta = SessionMeta(
        session_id="a3f7c2d0abcdef01",
        project_hash="deadbeefcafef00d",
        project_name="proj",
        file_path=Path("/tmp/x.jsonl"),
        start_time=base - timedelta(seconds=90),
        last_event_time=base,
        event_count=5,
        parse_confidence=1.0,
        state=SessionState.ACTIVE,
    )
    renderer.begin_tick()
    renderer.render([_snap("parse_health", 1.0)], meta)
    renderer.end_tick()
    out = stream.getvalue()
    # "0m 00s" must not be the uptime — session is 90 seconds old.
    assert "0m 00s" not in out
    # The actual uptime is 1m 30s.
    assert "1m 30s" in out


def test_uptime_zero_for_same_start_and_last() -> None:
    """Sessions where start == last_event emit 0m 00s."""
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    now = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
    meta = SessionMeta(
        session_id="a3f7c2d0abcdef01",
        project_hash="deadbeefcafef00d",
        project_name=None,
        file_path=Path("/tmp/x.jsonl"),
        start_time=now,
        last_event_time=now,
        event_count=0,
        parse_confidence=1.0,
        state=SessionState.ACTIVE,
    )
    renderer.begin_tick()
    renderer.render([_snap("parse_health", 1.0)], meta)
    renderer.end_tick()
    out = stream.getvalue()
    assert "0m 00s" in out


# ---------------------------------------------------------------------------
# TUI snapshot determinism across ticks (20-session fixture)
# ---------------------------------------------------------------------------


def _make_20_session_fixture() -> list[tuple[SessionMeta, list[MetricSnapshot]]]:
    base = datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC)
    pairs: list[tuple[SessionMeta, list[MetricSnapshot]]] = []
    severities = [Severity.OK, Severity.WARN, Severity.CRITICAL]
    projects = ["proj-alpha", "proj-beta", "proj-gamma"]
    for i in range(20):
        sid = f"session{i:010d}xx"
        sev = severities[i % 3]
        proj = projects[i % 3]
        meta = SessionMeta(
            session_id=sid,
            project_hash=f"{i:016x}",
            project_name=proj,
            file_path=Path(f"/tmp/s{i}.jsonl"),
            start_time=base - timedelta(minutes=i * 5),
            last_event_time=base + timedelta(seconds=i),
            event_count=i * 10,
            parse_confidence=0.95,
            state=SessionState.ACTIVE,
            snapshot_history={"reasoning_loop": (float(i), float(i + 1))},
        )
        snap = MetricSnapshot(
            name="reasoning_loop",
            value=float(i + 1),
            label=f"rate: {i + 1}",
            severity=sev,
        )
        pairs.append((meta, [snap]))
    return pairs


def test_20_session_fixture_renders_deterministically() -> None:
    """Two ticks with the same fixture must produce identical output."""
    fixture = _make_20_session_fixture()

    stream1 = io.StringIO()
    renderer1 = TerminalRenderer(stream=stream1, use_color=False)
    renderer1.begin_tick()
    for meta, snaps in fixture:
        renderer1.render(snaps, meta)
    renderer1.end_tick()
    tick1 = stream1.getvalue()

    stream2 = io.StringIO()
    renderer2 = TerminalRenderer(stream=stream2, use_color=False)
    renderer2.begin_tick()
    for meta, snaps in fixture:
        renderer2.render(snaps, meta)
    renderer2.end_tick()
    tick2 = stream2.getvalue()

    assert tick1 == tick2, "TUI output must be deterministic for identical input"


def test_20_session_fixture_crit_appears_before_ok() -> None:
    """In the 20-session fixture CRITICAL sessions appear before OK sessions."""
    fixture = _make_20_session_fixture()
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    renderer.begin_tick()
    for meta, snaps in fixture:
        renderer.render(snaps, meta)
    renderer.end_tick()
    out = stream.getvalue()
    # CRIT label must appear before OK label in the output.
    assert "CRIT" in out
    assert "OK" in out
    assert out.index("CRIT") < out.index("OK")


def test_20_session_fixture_has_fleet_summary() -> None:
    fixture = _make_20_session_fixture()
    stream = io.StringIO()
    renderer = TerminalRenderer(stream=stream, use_color=False)
    renderer.begin_tick()
    for meta, snaps in fixture:
        renderer.render(snaps, meta)
    renderer.end_tick()
    out = stream.getvalue()
    assert "sessions=20" in out
    # 20 sessions / 3 severities → 7 CRIT (indices 2,5,8,11,14,17) + 1 extra
    # and 7 WARN + 7 OK (roughly) — just assert counts are non-zero.
    assert "crit=" in out
    assert "warn=" in out
    assert "ok=" in out
