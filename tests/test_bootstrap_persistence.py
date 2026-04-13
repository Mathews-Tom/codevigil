"""Persistence and corrupt-state handling for :class:`BootstrapManager`.

Exercises the mid-run restart case (distributions accumulated across
multiple manager lifetimes must combine before finalisation) and the
corrupt-state fallback (unreadable JSON records a WARN and boots a
fresh state rather than crashing the watch loop).
"""

from __future__ import annotations

import json
from pathlib import Path

from codevigil.bootstrap import BootstrapManager
from codevigil.errors import (
    ErrorChannel,
    RotatingJsonlWriter,
    reset_error_channel,
    set_error_channel,
)
from codevigil.types import MetricSnapshot, Severity


def _snap(name: str, value: float) -> MetricSnapshot:
    return MetricSnapshot(name=name, value=value, label="", severity=Severity.OK)


def _caps() -> dict[str, tuple[float, float]]:
    return {"stop_phrase.stop_phrase": (1.0, 3.0)}


def test_mid_bootstrap_restart_combines_distributions(tmp_path: Path) -> None:
    state_path = tmp_path / "bootstrap.json"

    first = BootstrapManager(state_path=state_path, target_sessions=4, hard_caps=_caps())
    first.load()
    first.observe_session("a", {"stop_phrase": _snap("stop_phrase", 0.1)})
    first.observe_session("b", {"stop_phrase": _snap("stop_phrase", 0.2)})
    assert first.finalize_if_ready() is False
    # finalize_if_ready persists on every call, so state is on disk now.
    assert state_path.exists()

    second = BootstrapManager(state_path=state_path, target_sessions=4, hard_caps=_caps())
    second.load()
    assert second.sessions_observed() == 2
    assert second.state.distributions["stop_phrase.stop_phrase"] == [0.1, 0.2]
    second.observe_session("c", {"stop_phrase": _snap("stop_phrase", 0.3)})
    second.observe_session("d", {"stop_phrase": _snap("stop_phrase", 0.4)})
    assert second.finalize_if_ready() is True
    # Combined distribution drove the quantiles.
    assert second.state.distributions["stop_phrase.stop_phrase"] == [0.1, 0.2, 0.3, 0.4]
    warn, critical = second.thresholds_for("stop_phrase.stop_phrase") or (None, None)
    assert warn is not None and critical is not None


def test_corrupt_state_logs_warn_and_starts_fresh(tmp_path: Path) -> None:
    state_path = tmp_path / "bootstrap.json"
    state_path.write_text("{ this is not json", encoding="utf-8")

    log_path = tmp_path / "codevigil.log"
    writer = RotatingJsonlWriter(log_path)
    set_error_channel(ErrorChannel(writer))
    try:
        mgr = BootstrapManager(state_path=state_path, target_sessions=2, hard_caps=_caps())
        mgr.load()
        assert mgr.sessions_observed() == 0
        assert mgr.is_active()
        assert log_path.exists()
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line]
        codes = {r["code"] for r in records}
        assert "bootstrap.corrupt_state" in codes
    finally:
        reset_error_channel()


def test_schema_mismatch_triggers_reset(tmp_path: Path) -> None:
    state_path = tmp_path / "bootstrap.json"
    state_path.write_text(
        json.dumps({"schema_version": 999, "sessions_observed": 5}),
        encoding="utf-8",
    )
    log_path = tmp_path / "codevigil.log"
    set_error_channel(ErrorChannel(RotatingJsonlWriter(log_path)))
    try:
        mgr = BootstrapManager(state_path=state_path, target_sessions=2, hard_caps=_caps())
        mgr.load()
        assert mgr.sessions_observed() == 0
    finally:
        reset_error_channel()
