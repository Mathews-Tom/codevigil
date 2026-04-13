"""Deterministic anonymizer for Claude Code JSONL session fixtures.

Given a raw session captured from ``~/.claude/projects/<hash>/sessions/<id>.jsonl``
this module produces a transformed version safe to commit as a fixture: home
directory paths are replaced with ``/home/user``, secrets are redacted, project
hashes and tool-use ids are rewritten to stable ``fixture-<n>`` / ``tool-<n>``
tokens, and timestamps are shifted onto a fixed base date while preserving
intra-session relative offsets.

Public API
----------

* :class:`AnonMapping` — per-session bookkeeping for hash and id rewrites and
  the timestamp base. Pass the same instance across every line of one session
  so successive lines share rewrites; build a fresh instance for the next
  session.
* :func:`anonymize` — transform a single JSONL line.
* :func:`anonymize_session` — transform an iterable of lines (one session).
* ``python -m tests.tools.anonymize_session input.jsonl`` — script form that
  prints the transformed JSONL to stdout. Reads stdin when no path is given.

Determinism is the contract: ``sha256(anonymize(x)) == sha256(anonymize(x))``
for any ``x``, with a freshly-built mapping each time. The integration tests
rely on this property to keep fixture diffs stable across re-runs.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fixed base date that every session's first timestamp is mapped to. Later
#: timestamps in the same session are shifted by the same offset so the
#: relative ordering and spacing between events is preserved exactly.
BASE_TIMESTAMP: datetime = datetime.fromisoformat("2026-01-01T00:00:00+00:00")

_HOME_REPLACEMENT: str = "/home/user"

# Path stripping. Match ``/Users/<name>/`` (macOS), ``/home/<name>/`` (Linux),
# and a leading ``~/``. The trailing slash is captured back via the
# replacement so the rest of the path survives intact.
_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/Users/[^/\s\"']+"),
    re.compile(r"/home/[^/\s\"']+"),
    re.compile(r"(?<![A-Za-z0-9_])~(?=/)"),
)

# Project hash inside a ``~/.claude/projects/<hash>/`` style path. The hash
# can be hex or any alnum-ish run; we rewrite the segment that follows the
# literal ``projects/`` prefix.
_PROJECT_HASH_PATTERN: re.Pattern[str] = re.compile(
    r"(?P<prefix>(?:\.claude|claude)/projects/)(?P<hash>[A-Za-z0-9_\-]{16,})"
)

# Known secret-prefix patterns. Each captures the entire token to the next
# whitespace or quote so the redaction replaces the full credential, not just
# the prefix.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[poasu]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[bporas]-[A-Za-z0-9\-]{10,}"),
)

_REDACTED: str = "[REDACTED]"

# High-entropy fallback heuristic. We flag any 32+ char run of base64-ish
# characters as a likely secret, *after* the allowlist below filters out
# benign shapes that happen to fit the same regex.
#
# Note: the character class deliberately excludes ``/`` and ``.`` so a long
# filesystem path like ``/home/user/projects/foo/bar.py`` does not get
# swallowed wholesale. Real base64 secrets do not span path separators, and
# the stripped path tokens between separators stay well under 32 chars in
# practice.
_HIGH_ENTROPY_PATTERN: re.Pattern[str] = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")

# Allowlist for the high-entropy fallback. These shapes look like secrets to
# the entropy heuristic but are well-known false positives:
#
# * 40-character SHA-1 commit hashes (lowercase hex only).
# * UUIDs in the canonical ``8-4-4-4-12`` dashed form.
# * JWT-shaped tokens — three base64 chunks separated by literal dots — the
#   surrounding ``a.b.c`` shape catches them before they reach this regex,
#   but we keep an explicit pattern in case a JWT segment shows up bare.
#
# Any string that matches one of these allowlist entries in its entirety is
# left untouched by the high-entropy redactor.
_ALLOWLIST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\A[0-9a-f]{40}\Z"),
    re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"),
    re.compile(r"\A[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\Z"),
)

_TIMESTAMP_KEYS: frozenset[str] = frozenset({"timestamp", "start_time", "end_time", "created_at"})
_TOOL_ID_KEYS: frozenset[str] = frozenset({"tool_use_id", "tool_call_id", "id"})

# An ISO-8601 timestamp regex tight enough to avoid matching arbitrary
# digit runs but loose enough to cover the variants Claude Code emits
# (``Z``, ``+00:00``, optional fractional seconds).
_ISO_TIMESTAMP_PATTERN: re.Pattern[str] = re.compile(
    r"\A\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:?\d{2})?\Z"
)


# ---------------------------------------------------------------------------
# Mapping state
# ---------------------------------------------------------------------------


@dataclass
class AnonMapping:
    """Per-session rewrite bookkeeping.

    A single mapping accumulates state as successive lines flow through
    :func:`anonymize`, so two occurrences of the same project hash or
    tool-use id within one session collapse to the same rewritten token.
    Spin up a fresh ``AnonMapping`` per session to keep rewrites isolated
    between sessions.
    """

    project_hashes: dict[str, str] = field(default_factory=dict)
    tool_ids: dict[str, str] = field(default_factory=dict)
    timestamp_origin: datetime | None = None

    def project_token(self, raw_hash: str) -> str:
        existing = self.project_hashes.get(raw_hash)
        if existing is not None:
            return existing
        token = f"fixture-{len(self.project_hashes) + 1}"
        self.project_hashes[raw_hash] = token
        return token

    def tool_token(self, raw_id: str) -> str:
        existing = self.tool_ids.get(raw_id)
        if existing is not None:
            return existing
        token = f"tool-{len(self.tool_ids) + 1}"
        self.tool_ids[raw_id] = token
        return token

    def shift_timestamp(self, raw: str) -> str:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
        if self.timestamp_origin is None:
            self.timestamp_origin = parsed
            return _format_timestamp(BASE_TIMESTAMP)
        delta: timedelta = parsed - self.timestamp_origin
        return _format_timestamp(BASE_TIMESTAMP + delta)


def _format_timestamp(value: datetime) -> str:
    # Preserve the explicit ``+00:00`` offset rather than ``Z`` so the
    # output round-trips through :func:`datetime.fromisoformat` on every
    # supported Python version without special-casing.
    return value.isoformat()


# ---------------------------------------------------------------------------
# String-level transforms
# ---------------------------------------------------------------------------


def _strip_paths(text: str) -> str:
    out = text
    out = _PATH_PATTERNS[0].sub(_HOME_REPLACEMENT, out)
    out = _PATH_PATTERNS[1].sub(_HOME_REPLACEMENT, out)
    out = _PATH_PATTERNS[2].sub(_HOME_REPLACEMENT, out)
    return out


def _redact_known_secrets(text: str) -> str:
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


def _redact_high_entropy(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        for allowed in _ALLOWLIST_PATTERNS:
            if allowed.fullmatch(token):
                return token
        return _REDACTED

    return _HIGH_ENTROPY_PATTERN.sub(_replace, text)


def _rewrite_project_hashes(text: str, mapping: AnonMapping) -> str:
    def _replace(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{mapping.project_token(match.group('hash'))}"

    return _PROJECT_HASH_PATTERN.sub(_replace, text)


def _transform_string(text: str, mapping: AnonMapping) -> str:
    """Apply path/secret/hash rewrites to an arbitrary string.

    Order matters: project-hash rewrites run before generic path stripping
    so the literal ``projects/<hash>`` survives long enough to be matched,
    and the high-entropy fallback runs *after* known-prefix secret patterns
    so a captured ``sk-…`` becomes ``[REDACTED]`` via its dedicated rule
    rather than via the entropy heuristic.
    """

    out = _rewrite_project_hashes(text, mapping)
    out = _strip_paths(out)
    out = _redact_known_secrets(out)
    out = _redact_high_entropy(out)
    return out


# ---------------------------------------------------------------------------
# Recursive object walker
# ---------------------------------------------------------------------------


def _walk(value: Any, mapping: AnonMapping, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _walk(v, mapping, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(item, mapping, key=key) for item in value]
    if isinstance(value, str):
        if key in _TIMESTAMP_KEYS and _ISO_TIMESTAMP_PATTERN.match(value):
            return mapping.shift_timestamp(value)
        if key in _TOOL_ID_KEYS:
            return mapping.tool_token(value)
        return _transform_string(value, mapping)
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def anonymize(line: str, *, mapping: AnonMapping) -> str:
    """Anonymize a single JSONL line in place.

    Lines that fail to decode as JSON fall through the string-level
    transforms unchanged-shape: paths are stripped and secrets redacted,
    but timestamp and id rewrites are skipped (they require the structured
    key context). This mirrors what the parser does for malformed input —
    the anonymizer never raises on a bad line.
    """

    stripped = line.rstrip("\n")
    if not stripped:
        return ""
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return _transform_string(stripped, mapping)
    walked = _walk(decoded, mapping)
    return json.dumps(walked, sort_keys=True, separators=(",", ":"))


def anonymize_session(lines: Iterable[str]) -> Iterator[str]:
    """Anonymize an entire session, yielding one transformed line per input."""

    mapping = AnonMapping()
    for raw in lines:
        out = anonymize(raw, mapping=mapping)
        if out:
            yield out


# ---------------------------------------------------------------------------
# Script entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if args and args[0] in {"-h", "--help"}:
        sys.stdout.write(
            "usage: python -m tests.tools.anonymize_session [INPUT.jsonl]\n"
            "Reads stdin when no input path is given. Writes anonymized JSONL to stdout.\n"
        )
        return 0
    if args:
        with open(args[0], encoding="utf-8") as handle:
            lines = list(handle)
    else:
        lines = list(sys.stdin)
    for transformed in anonymize_session(lines):
        sys.stdout.write(transformed)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "BASE_TIMESTAMP",
    "AnonMapping",
    "anonymize",
    "anonymize_session",
    "main",
]
