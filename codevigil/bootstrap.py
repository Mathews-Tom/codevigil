"""Bootstrap threshold calibration.

Users installing codevigil for the first time have no idea whether a
read-edit ratio of 3.2 or a stop-phrase hit rate of 0.8 per hour is
normal for *their* workflow, so the built-in warn/critical thresholds
from ``docs/design.md`` §v0.1 Collectors are at best population
averages. The bootstrap phase turns those averages into personal
defaults:

1. For the first ``bootstrap.sessions`` sessions the aggregator observes
   (default 10), every user-facing collector still runs normally, but
   the aggregator pins reported severity to ``OK`` and tags each metric
   label with ``[bootstrap N/M]`` so the renderer shows "learning, not
   alerting". ``parse_health`` is excluded from this clamp — it is an
   integrity signal, not an experimental threshold.

2. Session finalisation (eviction or clean shutdown) hands the final
   per-collector snapshot to :class:`BootstrapManager`, which appends
   each metric's ``value`` to a session-distribution table persisted as
   JSON under ``bootstrap.state_path``.

3. After the ``target`` session count is reached, the manager derives
   p80 and p95 per metric and clamps them to the hard-cap boundary
   supplied at construction time (the caps come from the resolved
   config's per-collector ``warn_threshold``/``critical_threshold``
   keys, which in turn default to the literal values from §v0.1
   Collectors — so "fallback" and "calibrated" meet in the same place).

The manager is deliberately decoupled from config mutation. Completion
never rewrites the effective config — doing so would violate
``codevigil.config``'s fail-loud discipline. Users manually flip
``experimental = false`` once they are happy with the derived
thresholds, or run ``scripts/recalibrate_thresholds.py`` against a
fixture corpus to get a TOML snippet they can paste in.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource, record
from codevigil.types import MetricSnapshot

_PARSE_HEALTH_NAME: str = "parse_health"
_STATE_SCHEMA_VERSION: int = 1


@dataclass(slots=True)
class BootstrapState:
    """Serialisable calibration state.

    ``distributions`` maps ``f"{collector_name}.{metric_name}"`` to the
    list of primary ``value`` floats observed across completed sessions.
    ``derived_thresholds`` is filled in by :meth:`BootstrapManager.finalize_if_ready`
    when ``sessions_observed >= target``; entries are ``(warn, critical)``
    tuples clamped against the hard caps provided at manager construction.
    """

    sessions_observed: int = 0
    target: int = 0
    completed: bool = False
    distributions: dict[str, list[float]] = field(default_factory=dict)
    derived_thresholds: dict[str, tuple[float, float]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "sessions_observed": self.sessions_observed,
            "target": self.target,
            "completed": self.completed,
            "distributions": {k: list(v) for k, v in self.distributions.items()},
            "derived_thresholds": {
                k: [float(v[0]), float(v[1])] for k, v in self.derived_thresholds.items()
            },
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any], *, target: int) -> BootstrapState:
        if not isinstance(payload, dict):
            raise ValueError("bootstrap state root is not a dict")
        if payload.get("schema_version") != _STATE_SCHEMA_VERSION:
            raise ValueError(
                f"bootstrap state schema_version mismatch: "
                f"got {payload.get('schema_version')!r}, expected {_STATE_SCHEMA_VERSION}"
            )
        raw_dists = payload.get("distributions", {})
        if not isinstance(raw_dists, dict):
            raise ValueError("bootstrap state distributions is not a dict")
        distributions: dict[str, list[float]] = {}
        for key, values in raw_dists.items():
            if not isinstance(key, str) or not isinstance(values, list):
                raise ValueError(f"bootstrap state distributions entry {key!r} malformed")
            distributions[key] = [float(v) for v in values]
        raw_derived = payload.get("derived_thresholds", {})
        if not isinstance(raw_derived, dict):
            raise ValueError("bootstrap state derived_thresholds is not a dict")
        derived: dict[str, tuple[float, float]] = {}
        for key, pair in raw_derived.items():
            if not isinstance(key, str) or not isinstance(pair, list) or len(pair) != 2:
                raise ValueError(f"bootstrap state derived_thresholds entry {key!r} malformed")
            derived[key] = (float(pair[0]), float(pair[1]))
        return cls(
            sessions_observed=int(payload.get("sessions_observed", 0)),
            target=int(payload.get("target", target)),
            completed=bool(payload.get("completed", False)),
            distributions=distributions,
            derived_thresholds=derived,
        )


class BootstrapManager:
    """Owns bootstrap state persistence, observation, and finalisation.

    One instance is constructed per watch invocation and handed to the
    aggregator. Report and export modes do not observe — v0.1 restricts
    calibration to the live watch loop so a one-off ``report`` run against
    a historical fixture cannot accidentally finalise someone's personal
    baseline from unrelated data.
    """

    def __init__(
        self,
        state_path: Path,
        target_sessions: int,
        hard_caps: dict[str, tuple[float, float]],
    ) -> None:
        self._state_path: Path = state_path
        self._target: int = int(target_sessions)
        self._hard_caps: dict[str, tuple[float, float]] = dict(hard_caps)
        self._state: BootstrapState = BootstrapState(target=self._target)
        self._loaded: bool = False

    # --------------------------------------------------------------- state IO

    @property
    def state(self) -> BootstrapState:
        return self._state

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def target(self) -> int:
        return self._target

    def load(self) -> None:
        """Hydrate state from disk. Corrupt state resets to empty."""

        self._loaded = True
        if not self._state_path.exists():
            self._state = BootstrapState(target=self._target)
            return
        try:
            with self._state_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self._state = BootstrapState.from_json(payload, target=self._target)
            # If the on-disk target disagrees, prefer the caller's target —
            # the user may have bumped bootstrap.sessions in their config
            # between runs. The existing distribution is still useful.
            self._state.target = self._target
            if not self._state.completed and self._state.sessions_observed >= self._target:
                # Caller will normally call finalize_if_ready() after load,
                # but if an earlier session never reached finalisation, we
                # still flag the state as eligible. Leave the actual derive
                # to the public finalize_if_ready() path.
                pass
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.AGGREGATOR,
                    code="bootstrap.corrupt_state",
                    message=f"bootstrap state at {self._state_path!s} unreadable: {exc}",
                    context={"state_path": str(self._state_path)},
                )
            )
            self._state = BootstrapState(target=self._target)

    def save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._state_path.open("w", encoding="utf-8") as handle:
            json.dump(self._state.to_json(), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")

    # -------------------------------------------------------------- queries

    def is_active(self) -> bool:
        """True while severity should be pinned to OK for user collectors."""

        return not self._state.completed

    def sessions_observed(self) -> int:
        return self._state.sessions_observed

    def thresholds_for(self, metric_key: str) -> tuple[float, float] | None:
        """Return derived (warn, critical) once calibration is complete."""

        if not self._state.completed:
            return None
        return self._state.derived_thresholds.get(metric_key)

    # ------------------------------------------------------------- mutators

    def observe_session(
        self,
        session_id: str,
        collector_snapshots: dict[str, MetricSnapshot],
    ) -> None:
        """Append one session's final snapshots into the calibration window.

        ``parse_health`` snapshots are ignored — it's an integrity signal,
        not a threshold candidate. Calls after completion are a no-op so
        post-bootstrap sessions don't drag derived thresholds around.
        """

        del session_id  # reserved for future per-session dedup / provenance
        if self._state.completed:
            return
        for collector_name, snap in collector_snapshots.items():
            if collector_name == _PARSE_HEALTH_NAME:
                continue
            key = f"{collector_name}.{snap.name}"
            bucket = self._state.distributions.setdefault(key, [])
            bucket.append(float(snap.value))
        self._state.sessions_observed += 1

    def finalize_if_ready(self) -> bool:
        """Derive thresholds and persist if the window is full.

        Returns True on the single tick where completion flips from False
        to True so the aggregator can emit the INFO transition record.
        """

        if self._state.completed:
            return False
        if self._state.sessions_observed < self._target:
            self.save()
            return False
        derived: dict[str, tuple[float, float]] = {}
        for key, values in self._state.distributions.items():
            warn_p80, critical_p95 = _compute_quantiles(values)
            cap = self._hard_caps.get(key)
            if cap is not None:
                warn_cap, critical_cap = cap
                # Spec: warn clamps to <= warn_cap, critical clamps to
                # >= critical_cap. This is the high-value-is-worse
                # semantic; for low-value-is-worse collectors (e.g.
                # read_edit_ratio) the clamp direction is preserved so
                # the hard-coded fallback always wins as the boundary.
                warn_p80 = min(warn_p80, float(warn_cap))
                critical_p95 = max(critical_p95, float(critical_cap))
            derived[key] = (warn_p80, critical_p95)
        self._state.derived_thresholds = derived
        self._state.completed = True
        self.save()
        return True


def _compute_quantiles(values: list[float]) -> tuple[float, float]:
    """Return the (p80, p95) pair from ``values``.

    ``statistics.quantiles`` requires at least two data points; for
    shorter distributions we fall back to sensible scalar summaries so
    a single-sample collector never crashes finalisation. Uses
    ``n=20`` (ventiles) so index 15 is p80 and index 18 is p95.
    """

    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        only = float(values[0])
        return (only, only)
    ventiles = statistics.quantiles(values, n=20, method="inclusive")
    # ventiles has length 19: indices 0..18 for boundaries between the
    # 20 equal-sized groups. p80 sits at index 15, p95 at index 18.
    return (float(ventiles[15]), float(ventiles[18]))


__all__ = ["BootstrapManager", "BootstrapState"]
