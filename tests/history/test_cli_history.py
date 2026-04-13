"""CLI integration tests for ``codevigil history``.

Tests the argparse dispatch layer end-to-end via ``codevigil.cli.main``.
Store directories are injected via monkeypatching SessionStore so tests
are hermetic and do not touch the real XDG state directory.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from codevigil.analysis.store import SessionStore, build_report
from codevigil.cli import main


def _write_session(
    store: SessionStore,
    session_id: str,
    metrics: dict[str, float] | None = None,
    **kwargs: object,
) -> None:
    defaults: dict[str, object] = {
        "project_hash": "proj-hash",
        "project_name": "test-proj",
        "model": "gpt-4.1",
        "permission_mode": "default",
        "started_at": datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        "ended_at": datetime(2026, 4, 14, 10, 30, 0, tzinfo=UTC),
        "event_count": 10,
        "parse_confidence": 0.99,
    }
    defaults.update(kwargs)
    report = build_report(
        session_id=session_id,
        metrics=metrics or {},
        **defaults,  # type: ignore[arg-type]
    )
    store.write(report)


class TestHistoryListCLI:
    def test_history_list_no_filters_exits_0(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-clitest1")

        with patch("codevigil.history.list_cmd.SessionStore") as mock_store_cls:
            mock_store_cls.return_value = store
            with patch("sys.stdout", new_callable=io.StringIO):
                code = main(["history", "list"])
        assert code == 0

    def test_history_list_bad_since_exits_2(self) -> None:
        code = main(["history", "list", "--since", "not-a-date"])
        assert code == 2

    def test_history_list_bad_until_exits_2(self) -> None:
        code = main(["history", "list", "--until", "bad"])
        assert code == 2

    def test_history_no_subcommand_no_session_id_exits_2(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["history"])
        assert code == 2

    def test_history_diff_missing_both_exits_1(self, tmp_path: Path) -> None:
        with patch("codevigil.history.diff_cmd.SessionStore") as mock_cls:
            mock_cls.return_value = SessionStore(base_dir=tmp_path)
            code = main(["history", "diff", "id-a", "id-b"])
        assert code == 1

    def test_history_detail_missing_exits_1(self, tmp_path: Path) -> None:
        with patch("codevigil.history.detail_cmd.SessionStore") as mock_cls:
            mock_cls.return_value = SessionStore(base_dir=tmp_path)
            # Use a session id that doesn't look like a subcommand name
            # The positional is dispatched via history_command=None + session_id
            # We must use a valid CLI path: no subcommand, just a positional
            # The argparse design uses session_id as a positional on history_parser
            # so we call it directly via run_detail to test the "not found" path
            import io

            from codevigil.history.detail_cmd import run_detail

            out = io.StringIO()
            code = run_detail("not-existing", store_dir=tmp_path, out=out)
        assert code == 1


class TestHistoryDiffCLI:
    def test_diff_two_real_sessions_exits_0(self, tmp_path: Path) -> None:
        store = SessionStore(base_dir=tmp_path)
        _write_session(store, "agent-diffa", metrics={"stop_phrase": 1.0})
        _write_session(store, "agent-diffb", metrics={"stop_phrase": 2.0})

        with patch("codevigil.history.diff_cmd.SessionStore") as mock_cls:
            mock_cls.return_value = store
            with patch("sys.stdout", new_callable=io.StringIO):
                code = main(["history", "diff", "agent-diffa", "agent-diffb"])
        assert code == 0


class TestHistoryHeatmapCLI:
    def test_heatmap_missing_session_exits_1(self, tmp_path: Path) -> None:
        with patch("codevigil.history.heatmap_cmd.SessionStore") as mock_cls:
            mock_cls.return_value = SessionStore(base_dir=tmp_path)
            with patch("sys.stdout", new_callable=io.StringIO):
                code = main(["history", "heatmap", "no-such-session"])
        assert code == 1
