"""Tests for codevigil.report.loader.

Covers:
- expand_to_jsonl_paths: file, directory, and glob resolution.
- load_reports_from_jsonl: builds SessionReport objects from JSONL files,
  extracts write_precision from read_edit_ratio detail, skips unreadable
  files gracefully.
- write_precision extraction from the collector detail dict.
"""

from __future__ import annotations

import json
from pathlib import Path

from codevigil.report.loader import expand_to_jsonl_paths, load_reports_from_jsonl

# ---------------------------------------------------------------------------
# Fixture: minimal valid JSONL session
# ---------------------------------------------------------------------------

_TOOL_CALL_READ = json.dumps(
    {
        "type": "assistant",
        "timestamp": "2026-04-14T10:00:00+00:00",
        "session_id": "test-session",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "t-1",
                    "name": "Read",
                    "input": {"file_path": "/home/user/code.py"},
                }
            ]
        },
    }
)

_TOOL_CALL_WRITE = json.dumps(
    {
        "type": "assistant",
        "timestamp": "2026-04-14T10:01:00+00:00",
        "session_id": "test-session",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "t-2",
                    "name": "Write",
                    "input": {"file_path": "/home/user/code.py", "content": "x = 1"},
                }
            ]
        },
    }
)

_TOOL_CALL_EDIT = json.dumps(
    {
        "type": "assistant",
        "timestamp": "2026-04-14T10:02:00+00:00",
        "session_id": "test-session",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "t-3",
                    "name": "Edit",
                    "input": {
                        "file_path": "/home/user/code.py",
                        "old_string": "x = 1",
                        "new_string": "x = 2",
                    },
                }
            ]
        },
    }
)


def _write_session(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# expand_to_jsonl_paths
# ---------------------------------------------------------------------------


class TestExpandToJsonlPaths:
    def test_resolves_file(self, tmp_path: Path) -> None:
        f = tmp_path / "s.jsonl"
        f.write_text("{}\n")
        paths = expand_to_jsonl_paths(str(f))
        assert paths == [f]

    def test_resolves_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.jsonl").write_text("{}\n")
        (tmp_path / "b.jsonl").write_text("{}\n")
        (tmp_path / "other.txt").write_text("skip\n")
        paths = expand_to_jsonl_paths(str(tmp_path))
        assert len(paths) == 2
        assert all(p.suffix == ".jsonl" for p in paths)

    def test_resolves_glob(self, tmp_path: Path) -> None:
        (tmp_path / "x.jsonl").write_text("{}\n")
        (tmp_path / "y.jsonl").write_text("{}\n")
        pattern = str(tmp_path / "*.jsonl")
        paths = expand_to_jsonl_paths(pattern)
        assert len(paths) == 2

    def test_returns_empty_for_nonexistent_path(self, tmp_path: Path) -> None:
        paths = expand_to_jsonl_paths(str(tmp_path / "missing.jsonl"))
        assert paths == []

    def test_returns_sorted_paths(self, tmp_path: Path) -> None:
        for name in ("c.jsonl", "a.jsonl", "b.jsonl"):
            (tmp_path / name).write_text("{}\n")
        paths = expand_to_jsonl_paths(str(tmp_path))
        names = [p.name for p in paths]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# load_reports_from_jsonl
# ---------------------------------------------------------------------------


class TestLoadReportsFromJsonl:
    def test_loads_single_session(self, tmp_path: Path) -> None:
        path = tmp_path / "s1.jsonl"
        _write_session(
            path,
            [
                json.dumps(
                    {
                        "type": "system",
                        "timestamp": "2026-04-14T10:00:00+00:00",
                        "session_id": "s1",
                        "subtype": "session_start",
                    }
                ),
                _TOOL_CALL_READ,
            ],
        )
        reports = load_reports_from_jsonl([path])
        assert len(reports) == 1
        assert reports[0].session_id == "s1"

    def test_skips_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        reports = load_reports_from_jsonl([path])
        assert reports == []

    def test_skips_unreadable_file(self, tmp_path: Path) -> None:
        bad_path = tmp_path / "nonexistent.jsonl"
        reports = load_reports_from_jsonl([bad_path])
        assert reports == []

    def test_returns_sorted_by_started_at(self, tmp_path: Path) -> None:
        for i, ts in enumerate(["2026-04-15", "2026-04-13", "2026-04-14"]):
            path = tmp_path / f"s{i}.jsonl"
            _write_session(
                path,
                [
                    json.dumps(
                        {
                            "type": "system",
                            "timestamp": f"{ts}T10:00:00+00:00",
                            "session_id": f"s{i}",
                            "subtype": "session_start",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": f"{ts}T10:01:00+00:00",
                            "session_id": f"s{i}",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": f"t{i}",
                                        "name": "Read",
                                        "input": {"file_path": "/f.py"},
                                    }
                                ]
                            },
                        }
                    ),
                ],
            )
        reports = load_reports_from_jsonl(list((tmp_path).glob("*.jsonl")))
        dates = [r.started_at.date().isoformat() for r in reports]
        assert dates == sorted(dates)

    def test_write_precision_extracted_when_write_and_edit_present(self, tmp_path: Path) -> None:
        """write_precision should appear in metrics when both write and edit calls exist."""
        path = tmp_path / "wp.jsonl"
        _write_session(
            path,
            [
                json.dumps(
                    {
                        "type": "system",
                        "timestamp": "2026-04-14T10:00:00+00:00",
                        "session_id": "wp",
                        "subtype": "session_start",
                    }
                ),
                _TOOL_CALL_READ,
                _TOOL_CALL_WRITE,
                _TOOL_CALL_EDIT,
            ],
        )
        reports = load_reports_from_jsonl([path])
        assert len(reports) == 1
        metrics = reports[0].metrics
        # write_precision = 1 write / (1 write + 1 edit) = 0.5
        assert "write_precision" in metrics
        assert abs(metrics["write_precision"] - 0.5) < 1e-6

    def test_write_precision_absent_when_no_mutations(self, tmp_path: Path) -> None:
        """write_precision should not appear in metrics when no mutations observed."""
        path = tmp_path / "reads_only.jsonl"
        _write_session(
            path,
            [
                json.dumps(
                    {
                        "type": "system",
                        "timestamp": "2026-04-14T10:00:00+00:00",
                        "session_id": "reads_only",
                        "subtype": "session_start",
                    }
                ),
                _TOOL_CALL_READ,
            ],
        )
        reports = load_reports_from_jsonl([path])
        assert len(reports) == 1
        # No write or edit calls, so write_precision should not be injected.
        assert "write_precision" not in reports[0].metrics

    def test_multiple_files_loaded(self, tmp_path: Path) -> None:
        paths = []
        for i in range(5):
            path = tmp_path / f"session-{i}.jsonl"
            _write_session(
                path,
                [
                    json.dumps(
                        {
                            "type": "system",
                            "timestamp": f"2026-04-{14 + i:02d}T10:00:00+00:00",
                            "session_id": f"s{i}",
                            "subtype": "session_start",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": f"2026-04-{14 + i:02d}T10:01:00+00:00",
                            "session_id": f"s{i}",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": f"t{i}",
                                        "name": "Read",
                                        "input": {"file_path": "/f.py"},
                                    }
                                ]
                            },
                        }
                    ),
                ],
            )
            paths.append(path)
        reports = load_reports_from_jsonl(paths)
        assert len(reports) == 5


class TestLoaderEdgeCases:
    def test_write_precision_not_injected_when_detail_not_dict(self, tmp_path: Path) -> None:
        """_inject_write_precision no-ops when the snapshot has no detail dict."""
        from codevigil.report.loader import _inject_write_precision
        from codevigil.types import MetricSnapshot, Severity

        metrics: dict[str, float] = {"read_edit_ratio": 5.0}
        snap_no_detail = MetricSnapshot(
            name="read_edit_ratio",
            value=5.0,
            label="R:E 5.0",
            severity=Severity.OK,
            detail=None,  # type: ignore[arg-type]  # intentionally None for this test
        )
        snapshots = {"read_edit_ratio": snap_no_detail}
        _inject_write_precision(metrics, snapshots)
        # Should not add write_precision when detail is not a dict.
        assert "write_precision" not in metrics

    def test_no_parse_health_duplicate_in_names(self, tmp_path: Path) -> None:
        """parse_health should appear only once even if listed in enabled collectors."""
        # This exercises the `continue` branch in _load_one when
        # parse_health appears in the enabled list.
        path = tmp_path / "s.jsonl"
        _write_session(
            path,
            [
                json.dumps(
                    {
                        "type": "system",
                        "timestamp": "2026-04-14T10:00:00+00:00",
                        "session_id": "s",
                        "subtype": "session_start",
                    }
                ),
                _TOOL_CALL_READ,
            ],
        )
        # Config that redundantly includes parse_health in enabled list.
        import copy

        from codevigil.config import CONFIG_DEFAULTS

        cfg = copy.deepcopy(CONFIG_DEFAULTS)
        cfg["collectors"]["enabled"] = ["parse_health", "read_edit_ratio"]

        reports = load_reports_from_jsonl([path], cfg=cfg)
        # Should still succeed: parse_health not duplicated.
        assert len(reports) == 1
