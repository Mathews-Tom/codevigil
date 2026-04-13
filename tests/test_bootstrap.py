"""Unit tests for :mod:`codevigil.bootstrap`.

Covers the state machine in isolation (no aggregator, no source): the
three-session replay happy path, the hard-cap clamp, and the active →
completed transition visible via ``is_active``. The corrupt-state and
persistence-round-trip paths live in ``test_bootstrap_persistence``.
"""

from __future__ import annotations

from pathlib import Path

from codevigil.bootstrap import BootstrapManager, _compute_quantiles
from codevigil.types import MetricSnapshot, Severity


def _snap(name: str, value: float) -> MetricSnapshot:
    return MetricSnapshot(name=name, value=value, label="", severity=Severity.OK)


def _hard_caps() -> dict[str, tuple[float, float]]:
    # Mirrors CONFIG_DEFAULTS: read_edit_ratio uses low=worse semantics
    # (warn=4, critical=2); stop_phrase and reasoning_loop use high=worse.
    return {
        "read_edit_ratio.read_edit_ratio": (4.0, 2.0),
        "stop_phrase.stop_phrase": (1.0, 3.0),
        "reasoning_loop.reasoning_loop": (10.0, 20.0),
    }


def test_bootstrap_completes_on_target_session(tmp_path: Path) -> None:
    mgr = BootstrapManager(
        state_path=tmp_path / "bootstrap.json",
        target_sessions=3,
        hard_caps=_hard_caps(),
    )
    mgr.load()

    assert mgr.is_active()
    for i in range(2):
        mgr.observe_session(
            f"sess-{i}",
            {
                "read_edit_ratio": _snap("read_edit_ratio", 3.0 + i),
                "stop_phrase": _snap("stop_phrase", 0.5 + i * 0.1),
            },
        )
        assert mgr.finalize_if_ready() is False
        assert mgr.is_active()

    mgr.observe_session(
        "sess-2",
        {
            "read_edit_ratio": _snap("read_edit_ratio", 5.0),
            "stop_phrase": _snap("stop_phrase", 0.7),
        },
    )
    assert mgr.finalize_if_ready() is True
    assert mgr.is_active() is False
    # Second call is a no-op.
    assert mgr.finalize_if_ready() is False


def test_bootstrap_derived_thresholds_respect_hard_caps(tmp_path: Path) -> None:
    mgr = BootstrapManager(
        state_path=tmp_path / "bootstrap.json",
        target_sessions=3,
        hard_caps=_hard_caps(),
    )
    mgr.load()
    # stop_phrase (high=worse). Feed values well above the warn cap of 1.0
    # so p80 would exceed the cap; the clamp should pull warn back down.
    for i, v in enumerate([5.0, 6.0, 7.0]):
        mgr.observe_session(f"s-{i}", {"stop_phrase": _snap("stop_phrase", v)})
    assert mgr.finalize_if_ready() is True
    warn, critical = mgr.thresholds_for("stop_phrase.stop_phrase") or (None, None)
    assert warn is not None and critical is not None
    assert warn <= 1.0  # clamped to warn_cap
    assert critical >= 3.0  # clamped to critical_cap


def test_parse_health_snapshots_are_ignored(tmp_path: Path) -> None:
    mgr = BootstrapManager(
        state_path=tmp_path / "bootstrap.json",
        target_sessions=1,
        hard_caps=_hard_caps(),
    )
    mgr.load()
    mgr.observe_session(
        "s0",
        {
            "parse_health": _snap("parse_health", 1.0),
            "stop_phrase": _snap("stop_phrase", 0.2),
        },
    )
    mgr.finalize_if_ready()
    assert "parse_health.parse_health" not in mgr.state.distributions
    assert "stop_phrase.stop_phrase" in mgr.state.distributions


def test_compute_quantiles_handles_short_distributions() -> None:
    assert _compute_quantiles([]) == (0.0, 0.0)
    assert _compute_quantiles([2.5]) == (2.5, 2.5)
    p80, p95 = _compute_quantiles([1.0, 2.0])
    # For a 2-point list, ventile index 15 and 18 both yield values
    # between the two; assert only that the order is preserved.
    assert 1.0 <= p80 <= p95 <= 2.0
