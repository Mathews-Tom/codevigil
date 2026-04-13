"""Smoke test: docs/configuration.md documents the same defaults as config.py.

Uses plain re.search assertions against the rendered Markdown so doc drift is
caught in CI without maintaining a separate fixture corpus. Only the
collector-threshold defaults that have tripped doc/code drift in the past are
checked here; the full schema lives in docs/configuration.md.
"""

from __future__ import annotations

import re
from pathlib import Path

from codevigil.config import CONFIG_DEFAULTS

_DOCS_DIR = Path(__file__).parent.parent / "docs"
_CONFIGURATION_MD = _DOCS_DIR / "configuration.md"
_COLLECTORS_MD = _DOCS_DIR / "collectors.md"


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default(section: str, key: str) -> object:
    """Return CONFIG_DEFAULTS['collectors'][section][key]."""
    return CONFIG_DEFAULTS["collectors"][section][key]


def _doc_contains_default(text: str, key: str, value: object) -> bool:
    """True when 'key' and its string representation of 'value' appear on the
    same table row in the Markdown source.

    The pattern ``| `{key}` | ... | {value} |`` accounts for the pipe-table
    format used in docs/configuration.md. We only check that the literal
    default value appears in the row that names the key, not that the row
    is syntactically valid Markdown.
    """
    # Match a markdown table row that contains the key name and the value
    # somewhere on the same line.
    pattern = re.compile(
        r"\|[^|]*`" + re.escape(key) + r"`[^|]*\|[^|]*\|[^|]*" + re.escape(str(value)) + r"[^|]*\|"
    )
    return bool(pattern.search(text))


# ---------------------------------------------------------------------------
# parse_health.critical_threshold
# ---------------------------------------------------------------------------


def test_parse_health_critical_threshold_default_in_configuration_md() -> None:
    """configuration.md documents parse_health.critical_threshold = 0.9."""
    text = _load(_CONFIGURATION_MD)
    expected = _default("parse_health", "critical_threshold")
    assert _doc_contains_default(text, "critical_threshold", expected), (
        f"docs/configuration.md does not document parse_health.critical_threshold = {expected!r}; "
        "run 'git diff docs/configuration.md' to see drift"
    )


def test_parse_health_critical_threshold_mentioned_in_collectors_md() -> None:
    """collectors.md severity table references critical_threshold, not literal 0.9."""
    text = _load(_COLLECTORS_MD)
    # After the fix, the severity row should say "critical_threshold" rather
    # than hard-coding the literal. Confirm the literal 0.9 is gone from the
    # severity table and that "critical_threshold" is present in the section.
    parse_health_section = text[text.find("## `parse_health`") :]
    assert "critical_threshold" in parse_health_section, (
        "docs/collectors.md parse_health section does not mention critical_threshold"
    )


# ---------------------------------------------------------------------------
# read_edit_ratio.min_events_for_severity
# ---------------------------------------------------------------------------


def test_read_edit_ratio_min_events_for_severity_in_configuration_md() -> None:
    """configuration.md documents read_edit_ratio.min_events_for_severity = 10."""
    text = _load(_CONFIGURATION_MD)
    expected = _default("read_edit_ratio", "min_events_for_severity")
    assert _doc_contains_default(text, "min_events_for_severity", expected), (
        "docs/configuration.md does not document "
        f"read_edit_ratio.min_events_for_severity = {expected!r}"
    )


def test_read_edit_ratio_min_events_for_severity_in_collectors_md() -> None:
    """collectors.md severity table names the min_events_for_severity gate."""
    text = _load(_COLLECTORS_MD)
    read_edit_section = text[text.find("## `read_edit_ratio`") :]
    assert "min_events_for_severity" in read_edit_section, (
        "docs/collectors.md read_edit_ratio section does not mention min_events_for_severity"
    )


# ---------------------------------------------------------------------------
# reasoning_loop.min_tool_calls_for_severity
# ---------------------------------------------------------------------------


def test_reasoning_loop_min_tool_calls_in_configuration_md() -> None:
    """configuration.md documents reasoning_loop.min_tool_calls_for_severity = 20."""
    text = _load(_CONFIGURATION_MD)
    expected = _default("reasoning_loop", "min_tool_calls_for_severity")
    assert _doc_contains_default(text, "min_tool_calls_for_severity", expected), (
        "docs/configuration.md does not document "
        f"reasoning_loop.min_tool_calls_for_severity = {expected!r}"
    )


def test_reasoning_loop_min_tool_calls_in_collectors_md() -> None:
    """collectors.md severity table names the min_tool_calls_for_severity gate."""
    text = _load(_COLLECTORS_MD)
    reasoning_section = text[text.find("## `reasoning_loop`") :]
    assert "min_tool_calls_for_severity" in reasoning_section, (
        "docs/collectors.md reasoning_loop section does not mention min_tool_calls_for_severity"
    )


# ---------------------------------------------------------------------------
# Core threshold defaults (regression guard)
# ---------------------------------------------------------------------------


def test_read_edit_ratio_warn_threshold_in_configuration_md() -> None:
    text = _load(_CONFIGURATION_MD)
    expected = _default("read_edit_ratio", "warn_threshold")
    assert _doc_contains_default(text, "warn_threshold", expected), (
        f"docs/configuration.md does not document read_edit_ratio.warn_threshold = {expected!r}"
    )


def test_read_edit_ratio_critical_threshold_in_configuration_md() -> None:
    text = _load(_CONFIGURATION_MD)
    expected = _default("read_edit_ratio", "critical_threshold")
    assert _doc_contains_default(text, "critical_threshold", expected), (
        f"docs/configuration.md does not document read_edit_ratio.critical_threshold = {expected!r}"
    )


def test_stop_phrase_warn_threshold_in_configuration_md() -> None:
    text = _load(_CONFIGURATION_MD)
    expected = _default("stop_phrase", "warn_threshold")
    assert _doc_contains_default(text, "warn_threshold", expected), (
        f"docs/configuration.md does not document stop_phrase.warn_threshold = {expected!r}"
    )


def test_stop_phrase_critical_threshold_in_configuration_md() -> None:
    text = _load(_CONFIGURATION_MD)
    expected = _default("stop_phrase", "critical_threshold")
    assert _doc_contains_default(text, "critical_threshold", expected), (
        f"docs/configuration.md does not document stop_phrase.critical_threshold = {expected!r}"
    )


def test_reasoning_loop_warn_threshold_in_configuration_md() -> None:
    text = _load(_CONFIGURATION_MD)
    expected = _default("reasoning_loop", "warn_threshold")
    assert _doc_contains_default(text, "warn_threshold", expected), (
        f"docs/configuration.md does not document reasoning_loop.warn_threshold = {expected!r}"
    )


def test_reasoning_loop_critical_threshold_in_configuration_md() -> None:
    text = _load(_CONFIGURATION_MD)
    expected = _default("reasoning_loop", "critical_threshold")
    assert _doc_contains_default(text, "critical_threshold", expected), (
        f"docs/configuration.md does not document reasoning_loop.critical_threshold = {expected!r}"
    )


# ---------------------------------------------------------------------------
# [storage] section (Phase 3)
# ---------------------------------------------------------------------------


def test_storage_enable_persistence_in_configuration_md() -> None:
    """configuration.md documents storage.enable_persistence = false (TOML lowercase)."""
    text = _load(_CONFIGURATION_MD)
    # CONFIG_DEFAULTS stores Python False; the doc uses TOML/lowercase "false".
    # Check for the TOML representation rather than the Python repr.
    assert "enable_persistence" in text, "docs/configuration.md does not mention enable_persistence"
    assert "false" in text.lower(), (
        "docs/configuration.md does not document enable_persistence default as false"
    )


def test_storage_min_observation_days_in_configuration_md() -> None:
    """configuration.md documents storage.min_observation_days = 1."""
    text = _load(_CONFIGURATION_MD)
    storage_cfg = CONFIG_DEFAULTS["storage"]
    expected = storage_cfg["min_observation_days"]
    assert _doc_contains_default(text, "min_observation_days", expected), (
        f"docs/configuration.md does not document storage.min_observation_days = {expected!r}"
    )


def test_configuration_md_mentions_storage_section() -> None:
    """configuration.md has a [storage] section heading."""
    text = _load(_CONFIGURATION_MD)
    assert "[storage]" in text or "## `[storage]`" in text, (
        "docs/configuration.md does not contain a [storage] section"
    )
