"""Config resolution: TOML loader with layered precedence and fail-loud validation.

Precedence, lowest to highest:

1. Built-in defaults (``CONFIG_DEFAULTS`` below).
2. Config file (``~/.config/codevigil/config.toml`` or ``--config <path>``).
3. Environment variables (``CODEVIGIL_*``).
4. CLI flags.

Every resolved value carries a provenance string so ``codevigil config check``
can show where each value came from and users can audit precedence conflicts.

Validation is strict: unknown keys, unknown collector / renderer names, wrong
types, and out-of-range values all abort startup with a descriptive error
that names the offending key, source, and expected type or range.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codevigil.errors import CodevigilError, ErrorLevel, ErrorSource

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

CONFIG_DEFAULTS: dict[str, Any] = {
    "watch": {
        "root": "~/.claude/projects",
        "poll_interval": 2.0,
        "max_files": 2000,
        "large_file_warn_bytes": 10 * 1024 * 1024,
        "stale_after_seconds": 300,
        "evict_after_seconds": 2100,
        "tick_interval": 1.0,
    },
    "collectors": {
        "enabled": ["read_edit_ratio", "stop_phrase", "reasoning_loop"],
        "parse_health": {
            # parse_health is a built-in always-on integrity collector.
            # The validator refuses any layer that flips this flag to
            # false — see ``_validate_parse_health_undisableable``.
            "enabled": True,
            # Rolling parse_confidence below this threshold flips the
            # collector to CRITICAL once its internal window has
            # accumulated enough lines. Kept tunable so projects with
            # known-noisy wire formats can relax the alarm without
            # disabling the integrity signal entirely.
            "critical_threshold": 0.9,
        },
        "read_edit_ratio": {
            "window_size": 50,
            "warn_threshold": 4.0,
            "critical_threshold": 2.0,
            "blind_edit_window": 20,
            "blind_edit_confidence_floor": 0.95,
            "min_events_for_severity": 10,
            "experimental": True,
        },
        "stop_phrase": {
            "custom_phrases": [],
            "warn_threshold": 1.0,
            "critical_threshold": 3.0,
            "experimental": True,
        },
        "reasoning_loop": {
            "warn_threshold": 10.0,
            "critical_threshold": 20.0,
            "min_tool_calls_for_severity": 20,
            "experimental": True,
        },
    },
    "renderers": {
        "enabled": ["terminal"],
    },
    "report": {
        "output_format": "json",
        "output_dir": "~/.local/share/codevigil/reports",
    },
    "logging": {
        "log_path": "~/.local/state/codevigil/codevigil.log",
    },
    "bootstrap": {
        "sessions": 10,
        "state_path": "~/.local/state/codevigil/bootstrap.json",
    },
    "storage": {
        # When false (the default), codevigil watch writes nothing to disk
        # beyond the log file. Set to true to enable the session-report store
        # under ~/.local/state/codevigil/sessions/ (XDG_STATE_HOME respected).
        # The first write logs a single-line activation notice naming the path.
        "enable_persistence": False,
        # Minimum number of calendar days a period must span to be included in
        # cohort output. Periods shorter than this are dropped with a logged
        # reason. The default of 1 means single-day periods are allowed.
        "min_observation_days": 1,
    },
    "classifier": {
        # When true (the default), the turn-level task classifier runs inside
        # the aggregator at turn-close time. Each Turn receives a task_type
        # label and SessionReport gains session_task_type and turn_task_types
        # fields. Set to false to disable classification entirely; all
        # task_type fields will be None.
        "enabled": True,
        # When true (the default), classifier output is considered experimental
        # and is tagged [experimental] in Phase 6 user-facing surfaces. Flip to
        # false after the classifier has proven stable on a real-world corpus.
        "experimental": True,
    },
}

# Known collector and renderer names. These are hardcoded for Phase 2 because
# the runtime registries are empty until their phases land. Later phases may
# replace this with a registry-backed lookup, but the validator always needs
# *some* source of truth so typos in the enabled list abort at load time.
_KNOWN_COLLECTORS: frozenset[str] = frozenset(
    {"parse_health", "read_edit_ratio", "stop_phrase", "reasoning_loop"}
)
_KNOWN_RENDERERS: frozenset[str] = frozenset({"terminal", "json_file"})

_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset({"json", "markdown"})


# ---------------------------------------------------------------------------
# Environment variable bindings
# ---------------------------------------------------------------------------

# Mapping from CODEVIGIL_* env var names to dotted config paths. Only the
# keys in this mapping can be overridden via the environment; every other
# key must be set in the TOML file or on the CLI. This keeps the env surface
# small and auditable.
_ENV_BINDINGS: dict[str, tuple[str, ...]] = {
    "CODEVIGIL_LOG_PATH": ("logging", "log_path"),
    "CODEVIGIL_WATCH_ROOT": ("watch", "root"),
    "CODEVIGIL_WATCH_POLL_INTERVAL": ("watch", "poll_interval"),
    "CODEVIGIL_WATCH_TICK_INTERVAL": ("watch", "tick_interval"),
    "CODEVIGIL_REPORT_OUTPUT_DIR": ("report", "output_dir"),
    "CODEVIGIL_REPORT_OUTPUT_FORMAT": ("report", "output_format"),
    "CODEVIGIL_BOOTSTRAP_SESSIONS": ("bootstrap", "sessions"),
}

# ---------------------------------------------------------------------------
# Errors and resolved value containers
# ---------------------------------------------------------------------------


class ConfigError(CodevigilError):
    """Raised when the config layer cannot resolve or validate a value."""

    def __init__(self, *, code: str, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(
            level=ErrorLevel.CRITICAL,
            source=ErrorSource.CONFIG,
            code=code,
            message=message,
            context=context or {},
        )


@dataclass(frozen=True, slots=True)
class ResolvedValue:
    """A single config value paired with its provenance string."""

    value: Any
    source: str  # "default" | "file:<path>" | "env:CODEVIGIL_*" | "cli:--flag"


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """Fully resolved config with per-key provenance.

    ``values`` holds the effective config as a nested dict matching
    ``CONFIG_DEFAULTS``. ``sources`` maps dotted paths (``"watch.root"``) to
    the provenance string for that value. Only leaf values are tracked —
    intermediate dict nodes have no source.
    """

    values: dict[str, Any]
    sources: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(
    *,
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> ResolvedConfig:
    """Resolve the effective config from defaults → file → env → CLI.

    Parameters:
        config_path: Optional path to a TOML config file. If ``None``, the
            default ``~/.config/codevigil/config.toml`` is tried; a missing
            default file is not an error. An explicitly-passed path that
            does not exist *is* an error.
        env: Environment mapping to read ``CODEVIGIL_*`` bindings from.
            Defaults to ``os.environ``.
        cli_overrides: Dotted-path → value mapping from parsed CLI flags.

    Returns:
        ``ResolvedConfig`` with every leaf value annotated with its source.

    Raises:
        ConfigError: on unknown keys, wrong types, out-of-range values,
            unknown collector / renderer names, or file load errors.
    """

    environment = dict(os.environ) if env is None else dict(env)
    overrides = dict(cli_overrides) if cli_overrides is not None else {}

    values: dict[str, Any] = _deep_copy_defaults()
    sources: dict[str, str] = _flatten_sources(values, source="default")

    file_values, file_path_used = _load_file_layer(config_path)
    if file_values is not None:
        _validate_layer_shape(file_values, source=f"file:{file_path_used}")
        _apply_layer(
            values,
            file_values,
            sources,
            source_label=f"file:{file_path_used}",
        )

    env_values = _collect_env_overrides(environment)
    for dotted, (raw_value, env_name) in env_values.items():
        coerced = _coerce_scalar(dotted, raw_value, source=f"env:{env_name}")
        _assign_dotted(values, dotted, coerced)
        sources[dotted] = f"env:{env_name}"

    for dotted, raw_value in overrides.items():
        _check_known_path(dotted, source="cli")
        coerced = _coerce_scalar(dotted, raw_value, source=f"cli:--{dotted}")
        _assign_dotted(values, dotted, coerced)
        sources[dotted] = f"cli:--{dotted}"

    _validate_resolved(values)
    return ResolvedConfig(values=values, sources=sources)


def render_config_check(resolved: ResolvedConfig) -> str:
    """Format a resolved config for the ``codevigil config check`` command."""

    lines: list[str] = ["codevigil config check"]
    for dotted in sorted(resolved.sources):
        value = _read_dotted(resolved.values, dotted)
        source = resolved.sources[dotted]
        lines.append(f"  {dotted} = {_format_value(value)}  ({source})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Layer helpers
# ---------------------------------------------------------------------------


def _deep_copy_defaults() -> dict[str, Any]:
    copy = _deep_copy(CONFIG_DEFAULTS)
    assert isinstance(copy, dict)
    return copy


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _flatten_sources(values: dict[str, Any], *, source: str) -> dict[str, str]:
    out: dict[str, str] = {}
    _walk_leaves(values, prefix=(), accumulator=out, source=source)
    return out


def _walk_leaves(
    values: dict[str, Any],
    *,
    prefix: tuple[str, ...],
    accumulator: dict[str, str],
    source: str,
) -> None:
    for key, value in values.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            _walk_leaves(value, prefix=path, accumulator=accumulator, source=source)
        else:
            accumulator[".".join(path)] = source


def _load_file_layer(config_path: Path | None) -> tuple[dict[str, Any] | None, Path | None]:
    if config_path is None:
        default_path = Path("~/.config/codevigil/config.toml").expanduser()
        if not default_path.exists():
            return None, None
        return _read_toml(default_path), default_path
    expanded = config_path.expanduser()
    if not expanded.exists():
        raise ConfigError(
            code="config.file_not_found",
            message=f"config file does not exist: {expanded}",
            context={"path": str(expanded)},
        )
    return _read_toml(expanded), expanded


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            code="config.toml_parse_error",
            message=f"failed to parse {path}: {exc}",
            context={"path": str(path)},
        ) from exc


def _apply_layer(
    base: dict[str, Any],
    overlay: dict[str, Any],
    sources: dict[str, str],
    *,
    source_label: str,
    prefix: tuple[str, ...] = (),
) -> None:
    for key, value in overlay.items():
        path = (*prefix, key)
        dotted = ".".join(path)
        default_slot = _read_dotted_optional(CONFIG_DEFAULTS, dotted)
        if default_slot is _MISSING:
            raise ConfigError(
                code="config.unknown_key",
                message=f"unknown config key {dotted!r} in {source_label}",
                context={"key": dotted, "source": source_label},
            )
        if isinstance(default_slot, dict):
            if not isinstance(value, dict):
                raise ConfigError(
                    code="config.type_mismatch",
                    message=(
                        f"config key {dotted!r} expected a table, got "
                        f"{type(value).__name__} in {source_label}"
                    ),
                    context={
                        "key": dotted,
                        "expected": "table",
                        "actual": type(value).__name__,
                        "source": source_label,
                    },
                )
            _apply_layer(
                base,
                value,
                sources,
                source_label=source_label,
                prefix=path,
            )
            continue
        coerced = _coerce_against_default(dotted, value, default_slot, source=source_label)
        _assign_dotted(base, dotted, coerced)
        sources[dotted] = source_label


def _collect_env_overrides(environment: dict[str, str]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for env_name, path in _ENV_BINDINGS.items():
        raw = environment.get(env_name)
        if raw is None:
            continue
        dotted = ".".join(path)
        out[dotted] = (raw, env_name)
    return out


# ---------------------------------------------------------------------------
# Coercion and validation
# ---------------------------------------------------------------------------

_MISSING: object = object()


def _read_dotted(root: dict[str, Any], dotted: str) -> Any:
    node: Any = root
    for part in dotted.split("."):
        node = node[part]
    return node


def _read_dotted_optional(root: dict[str, Any], dotted: str) -> Any:
    node: Any = root
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _assign_dotted(root: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node: dict[str, Any] = root
    for part in parts[:-1]:
        next_node = node.get(part)
        if not isinstance(next_node, dict):
            next_node = {}
            node[part] = next_node
        node = next_node
    node[parts[-1]] = value


def _validate_layer_shape(layer: dict[str, Any], *, source: str) -> None:
    if not isinstance(layer, dict):  # pragma: no cover - defensive
        raise ConfigError(
            code="config.bad_layer_shape",
            message=f"{source} did not produce a table",
        )


def _check_known_path(dotted: str, *, source: str) -> None:
    if _read_dotted_optional(CONFIG_DEFAULTS, dotted) is _MISSING:
        raise ConfigError(
            code="config.unknown_key",
            message=f"unknown config key {dotted!r} from {source}",
            context={"key": dotted, "source": source},
        )


def _coerce_against_default(dotted: str, value: Any, default: Any, *, source: str) -> Any:
    expected_type = type(default)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(
                code="config.type_mismatch",
                message=(
                    f"config key {dotted!r} expected bool, got {type(value).__name__} in {source}"
                ),
                context={"key": dotted, "expected": "bool", "source": source},
            )
        return value
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(
                code="config.type_mismatch",
                message=(
                    f"config key {dotted!r} expected int, got {type(value).__name__} in {source}"
                ),
                context={"key": dotted, "expected": "int", "source": source},
            )
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                code="config.type_mismatch",
                message=(
                    f"config key {dotted!r} expected float, got {type(value).__name__} in {source}"
                ),
                context={"key": dotted, "expected": "float", "source": source},
            )
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(
                code="config.type_mismatch",
                message=(
                    f"config key {dotted!r} expected str, got {type(value).__name__} in {source}"
                ),
                context={"key": dotted, "expected": "str", "source": source},
            )
        return value
    if isinstance(default, list):
        if not isinstance(value, list):
            raise ConfigError(
                code="config.type_mismatch",
                message=(
                    f"config key {dotted!r} expected list, got {type(value).__name__} in {source}"
                ),
                context={"key": dotted, "expected": "list", "source": source},
            )
        # ``stop_phrase.custom_phrases`` accepts a mixed list of plain
        # strings and table entries with ``text``/``mode``/``category``/
        # ``intent`` keys. Every other list-valued config key is still
        # the strict ``list[str]`` form.
        if dotted == "collectors.stop_phrase.custom_phrases":
            return _coerce_custom_phrase_list(dotted, value, source=source)
        for item in value:
            if not isinstance(item, str):
                raise ConfigError(
                    code="config.type_mismatch",
                    message=(
                        f"config key {dotted!r} list item expected str, got "
                        f"{type(item).__name__} in {source}"
                    ),
                    context={
                        "key": dotted,
                        "expected": "list[str]",
                        "source": source,
                    },
                )
        return list(value)
    raise ConfigError(  # pragma: no cover - defensive for unknown default types
        code="config.unsupported_default_type",
        message=f"config key {dotted!r} has unsupported default type {expected_type.__name__}",
        context={"key": dotted, "type": expected_type.__name__},
    )


_CUSTOM_PHRASE_FIELDS: frozenset[str] = frozenset({"text", "mode", "category", "intent"})
_CUSTOM_PHRASE_MODES: frozenset[str] = frozenset({"word", "regex", "substring"})


def _coerce_custom_phrase_list(dotted: str, value: list[Any], *, source: str) -> list[Any]:
    """Validate the mixed string/table form of ``stop_phrase.custom_phrases``."""

    out: list[Any] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
            continue
        if not isinstance(item, dict):
            raise ConfigError(
                code="config.type_mismatch",
                message=(
                    f"config key {dotted!r} list item expected str or table, got "
                    f"{type(item).__name__} in {source}"
                ),
                context={"key": dotted, "source": source},
            )
        unknown = set(item.keys()) - _CUSTOM_PHRASE_FIELDS
        if unknown:
            raise ConfigError(
                code="config.unknown_key",
                message=(
                    f"config key {dotted!r} table entry has unknown field(s) "
                    f"{sorted(unknown)!r} in {source}"
                ),
                context={"key": dotted, "unknown": sorted(unknown), "source": source},
            )
        text = item.get("text")
        if not isinstance(text, str) or not text:
            raise ConfigError(
                code="config.type_mismatch",
                message=(
                    f"config key {dotted!r} table entry requires a non-empty "
                    f"'text' field in {source}"
                ),
                context={"key": dotted, "source": source},
            )
        mode = item.get("mode", "word")
        if mode not in _CUSTOM_PHRASE_MODES:
            raise ConfigError(
                code="config.out_of_range",
                message=(
                    f"config key {dotted!r} table entry has invalid mode "
                    f"{mode!r}; expected one of {sorted(_CUSTOM_PHRASE_MODES)!r} in {source}"
                ),
                context={"key": dotted, "mode": mode, "source": source},
            )
        out.append(dict(item))
    return out


def _coerce_scalar(dotted: str, raw: Any, *, source: str) -> Any:
    default = _read_dotted_optional(CONFIG_DEFAULTS, dotted)
    if default is _MISSING:
        raise ConfigError(
            code="config.unknown_key",
            message=f"unknown config key {dotted!r} from {source}",
            context={"key": dotted, "source": source},
        )
    if isinstance(default, dict):
        raise ConfigError(
            code="config.scalar_into_table",
            message=f"config key {dotted!r} expects a table, not a scalar from {source}",
            context={"key": dotted, "source": source},
        )
    if not isinstance(raw, str):
        return _coerce_against_default(dotted, raw, default, source=source)
    # Env / CLI raw values arrive as strings; parse them against the default
    # type so CODEVIGIL_WATCH_POLL_INTERVAL="0.5" becomes float 0.5.
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ConfigError(
            code="config.type_mismatch",
            message=f"config key {dotted!r} expected bool, got {raw!r} in {source}",
            context={"key": dotted, "raw": raw, "source": source},
        )
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(
                code="config.type_mismatch",
                message=f"config key {dotted!r} expected int, got {raw!r} in {source}",
                context={"key": dotted, "raw": raw, "source": source},
            ) from exc
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(
                code="config.type_mismatch",
                message=f"config key {dotted!r} expected float, got {raw!r} in {source}",
                context={"key": dotted, "raw": raw, "source": source},
            ) from exc
    if isinstance(default, list):
        # Comma-separated env / CLI form: "a,b,c".
        items = [part.strip() for part in raw.split(",") if part.strip()]
        return items
    return raw


def _validate_resolved(values: dict[str, Any]) -> None:
    _validate_range(values, "watch.poll_interval", minimum=0.05, maximum=3600.0, kind="float")
    _validate_range(values, "watch.tick_interval", minimum=0.05, maximum=3600.0, kind="float")
    _validate_range(values, "watch.max_files", minimum=1, maximum=1_000_000, kind="int")
    _validate_range(
        values,
        "watch.stale_after_seconds",
        minimum=1,
        maximum=86_400,
        kind="int",
    )
    _validate_range(
        values,
        "watch.evict_after_seconds",
        minimum=1,
        maximum=86_400,
        kind="int",
    )
    _validate_range(
        values,
        "watch.large_file_warn_bytes",
        minimum=1024,
        maximum=10**12,
        kind="int",
    )
    _validate_range(
        values,
        "collectors.read_edit_ratio.window_size",
        minimum=1,
        maximum=100_000,
        kind="int",
    )
    _validate_range(
        values,
        "collectors.read_edit_ratio.blind_edit_window",
        minimum=1,
        maximum=10_000,
        kind="int",
    )
    _validate_range(
        values,
        "collectors.read_edit_ratio.blind_edit_confidence_floor",
        minimum=0.0,
        maximum=1.0,
        kind="float",
    )
    _validate_range(
        values,
        "bootstrap.sessions",
        minimum=1,
        maximum=1_000,
        kind="int",
    )
    _validate_range(
        values,
        "collectors.parse_health.critical_threshold",
        minimum=0.0,
        maximum=1.0,
        kind="float",
    )
    _validate_range(
        values,
        "collectors.reasoning_loop.min_tool_calls_for_severity",
        minimum=0,
        maximum=100_000,
        kind="int",
    )
    _validate_range(
        values,
        "collectors.read_edit_ratio.min_events_for_severity",
        minimum=0,
        maximum=100_000,
        kind="int",
    )

    _validate_stale_vs_evict(values)
    _validate_enabled_names(
        values,
        "collectors.enabled",
        known=_KNOWN_COLLECTORS,
        kind="collector",
    )
    _validate_enabled_names(
        values,
        "renderers.enabled",
        known=_KNOWN_RENDERERS,
        kind="renderer",
    )
    _validate_output_format(values)
    _validate_parse_health_undisableable(values)
    _validate_range(
        values,
        "storage.min_observation_days",
        minimum=1,
        maximum=365,
        kind="int",
    )


def _validate_parse_health_undisableable(values: dict[str, Any]) -> None:
    """Refuse any config layer that tries to disable ``parse_health``.

    ``parse_health`` is the parser-drift integrity collector. Allowing it
    to be disabled would let a user silence the only signal that catches
    a silent Claude Code schema break, which defeats the design goal of
    treating drift as a first-class observable.
    """

    enabled = _read_dotted_optional(values, "collectors.parse_health.enabled")
    if enabled is _MISSING or enabled is True:
        return
    raise ConfigError(
        code="config.parse_health_undisableable",
        message=(
            "collectors.parse_health.enabled cannot be set to false; "
            "parse_health is a built-in always-on integrity collector"
        ),
        context={"key": "collectors.parse_health.enabled", "value": enabled},
    )


def _validate_range(
    values: dict[str, Any],
    dotted: str,
    *,
    minimum: float,
    maximum: float,
    kind: str,
) -> None:
    value = _read_dotted(values, dotted)
    if value < minimum or value > maximum:
        raise ConfigError(
            code="config.out_of_range",
            message=(
                f"config key {dotted!r} = {value!r} is out of range "
                f"[{minimum}, {maximum}] for {kind}"
            ),
            context={
                "key": dotted,
                "value": value,
                "min": minimum,
                "max": maximum,
            },
        )


def _validate_stale_vs_evict(values: dict[str, Any]) -> None:
    stale = _read_dotted(values, "watch.stale_after_seconds")
    evict = _read_dotted(values, "watch.evict_after_seconds")
    if evict <= stale:
        raise ConfigError(
            code="config.out_of_range",
            message=(
                f"watch.evict_after_seconds ({evict}) must be strictly greater "
                f"than watch.stale_after_seconds ({stale})"
            ),
            context={"stale": stale, "evict": evict},
        )


def _validate_enabled_names(
    values: dict[str, Any],
    dotted: str,
    *,
    known: frozenset[str],
    kind: str,
) -> None:
    enabled: list[str] = _read_dotted(values, dotted)
    unknown = [name for name in enabled if name not in known]
    if unknown:
        raise ConfigError(
            code=f"config.unknown_{kind}",
            message=(f"unknown {kind} name(s) in {dotted}: {unknown!r}; known: {sorted(known)!r}"),
            context={"key": dotted, "unknown": unknown, "known": sorted(known)},
        )
    if len(enabled) != len(set(enabled)):
        raise ConfigError(
            code=f"config.duplicate_{kind}",
            message=f"duplicate {kind} name(s) in {dotted}: {enabled!r}",
            context={"key": dotted, "enabled": enabled},
        )


def _validate_output_format(values: dict[str, Any]) -> None:
    fmt = _read_dotted(values, "report.output_format")
    if fmt not in _VALID_OUTPUT_FORMATS:
        raise ConfigError(
            code="config.invalid_output_format",
            message=(
                f"report.output_format = {fmt!r} is not one of {sorted(_VALID_OUTPUT_FORMATS)!r}"
            ),
            context={"value": fmt, "valid": sorted(_VALID_OUTPUT_FORMATS)},
        )


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(v) for v in value) + "]"
    return repr(value)


__all__ = [
    "CONFIG_DEFAULTS",
    "ConfigError",
    "ResolvedConfig",
    "ResolvedValue",
    "load_config",
    "render_config_check",
]
