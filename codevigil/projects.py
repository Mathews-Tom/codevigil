"""Project hash → friendly name resolution.

Claude Code stores session files under
``~/.claude/projects/<project-hash>/sessions/<session-id>.jsonl``. The hash is
opaque to humans, so the aggregator threads every session through a
:class:`ProjectRegistry` that maps the hash to a display name using three
sources, highest precedence first (per ``docs/design.md`` §Project Name
Resolution):

1. A user-maintained TOML file at ``~/.config/codevigil/projects.toml`` with
   ``{hash = "name"}`` pairs at the top level. This is the manual override
   users reach for when the auto-resolved name is wrong or missing.
2. The first ``cwd`` field observed inside a SYSTEM event payload for that
   hash, stripped to the last path component via ``Path(cwd).name``. This is
   what `claude` itself records when a user runs it from a project directory.
3. The raw hash prefix (``hash[:8]``) as the always-available fallback. An
   unresolved hash is *expected state*, not an error, so this branch never
   emits a WARN.

The registry is process-local: it is constructed once by the aggregator and
mutated only via :meth:`observe_system_event`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource, record
from codevigil.types import Event, EventKind, safe_get

_DEFAULT_TOML_PATH: Path = Path("~/.config/codevigil/projects.toml").expanduser()


class ProjectRegistry:
    """Resolve Claude Code project hashes to display names.

    The constructor loads the optional TOML override file synchronously; a
    malformed or unreadable file is reported via the error channel as a WARN
    and the registry continues with an empty user map (the cwd and hash
    fallbacks still work). The aggregator instantiates one registry per
    process and shares it across every session.
    """

    def __init__(self, toml_path: Path | None = None) -> None:
        self._toml_path: Path = toml_path if toml_path is not None else _DEFAULT_TOML_PATH
        self._user_overrides: dict[str, str] = {}
        self._cwd_cache: dict[str, str] = {}
        self._load_user_overrides()

    # ------------------------------------------------------------------ loading

    def _load_user_overrides(self) -> None:
        path = self._toml_path
        if not path.exists():
            return
        try:
            with path.open("rb") as handle:
                data: dict[str, Any] = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            record(
                CodevigilError(
                    level=ErrorLevel.WARN,
                    source=ErrorSource.AGGREGATOR,
                    code="projects.toml_load_failed",
                    message=(
                        f"failed to load projects override file {str(path)!r}: {exc}; "
                        f"continuing with empty user map"
                    ),
                    context={"path": str(path)},
                )
            )
            return
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str) and value:
                self._user_overrides[key] = value
            else:
                record(
                    CodevigilError(
                        level=ErrorLevel.WARN,
                        source=ErrorSource.AGGREGATOR,
                        code="projects.toml_bad_entry",
                        message=(f"ignoring non-string entry {key!r} in projects override file"),
                        context={"path": str(self._toml_path), "key": str(key)},
                    )
                )

    # ----------------------------------------------------------------- ingestion

    def observe_system_event(self, project_hash: str, event: Event) -> None:
        """Cache the first ``cwd`` value seen on a SYSTEM event for a hash.

        The aggregator forwards every SYSTEM event here. We keep the *first*
        observation rather than the latest because the resolution policy
        wants stable display names — a session that ``cd``s mid-run should
        still show up under the directory it started in.
        """

        if event.kind is not EventKind.SYSTEM:
            return
        if project_hash in self._cwd_cache:
            return
        cwd = safe_get(
            event.payload,
            "cwd",
            default=None,
            expected=str,
            source=ErrorSource.AGGREGATOR,
            event_kind="system",
        )
        if not isinstance(cwd, str) or not cwd:
            return
        name = Path(cwd).name
        if name:
            self._cwd_cache[project_hash] = name

    # ----------------------------------------------------------------- resolution

    def resolve(self, project_hash: str) -> str:
        """Return the highest-precedence display name available for a hash."""

        override = self._user_overrides.get(project_hash)
        if override:
            return override
        cwd_name = self._cwd_cache.get(project_hash)
        if cwd_name:
            return cwd_name
        return project_hash[:8] if project_hash else ""


__all__ = ["ProjectRegistry"]
