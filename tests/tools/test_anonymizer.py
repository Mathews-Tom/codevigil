"""Coverage for :mod:`tests.tools.anonymize_session`.

The anonymizer is the gatekeeper between raw user data and committed
fixtures, so each transform gets explicit per-feature coverage and the
top-level determinism contract is verified by hashing the output twice.
"""

from __future__ import annotations

import hashlib
import json

from tests.tools.anonymize_session import (
    BASE_TIMESTAMP,
    AnonMapping,
    anonymize,
    anonymize_session,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_anonymize_is_deterministic_across_runs() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2025-09-12T08:13:42+00:00",
            "session_id": "raw-session-id",
            "message": {
                "content": [
                    {"type": "text", "text": "/Users/alice/projects/secret.py opened"},
                ]
            },
        }
    )
    first = anonymize(line, mapping=AnonMapping())
    second = anonymize(line, mapping=AnonMapping())
    assert first == second
    assert _sha(first) == _sha(second)


def test_path_stripping_handles_macos_linux_and_tilde() -> None:
    mapping = AnonMapping()
    assert "/home/user/code" in anonymize(
        json.dumps({"type": "user", "message": {"content": "/Users/alice/code"}}),
        mapping=mapping,
    )
    assert "/home/user/code" in anonymize(
        json.dumps({"type": "user", "message": {"content": "/home/bob/code"}}),
        mapping=AnonMapping(),
    )
    assert "/home/user/projects" in anonymize(
        json.dumps({"type": "user", "message": {"content": "~/projects"}}),
        mapping=AnonMapping(),
    )


def test_secret_prefix_redaction_covers_each_provider() -> None:
    samples = {
        "openai": "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA",
        "github": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "github_oauth": "gho_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "aws": "AKIAABCDEFGHIJKLMNOP",
        "slack": "xoxb-1234567890-abcdefghijkl",
    }
    for label, secret in samples.items():
        line = json.dumps({"type": "user", "message": {"content": f"key: {secret}"}})
        result = anonymize(line, mapping=AnonMapping())
        assert "[REDACTED]" in result, f"{label} not redacted: {result}"
        assert secret not in result, f"{label} leaked: {result}"


def test_high_entropy_heuristic_allowlists_sha1_but_redacts_long_base64() -> None:
    sha1 = "a" * 40
    base64_token = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5"  # 48 chars
    line = json.dumps(
        {
            "type": "user",
            "message": {"content": f"sha={sha1} token={base64_token}"},
        }
    )
    result = anonymize(line, mapping=AnonMapping())
    assert sha1 in result
    assert base64_token not in result
    assert "[REDACTED]" in result


def test_project_hash_rewrite_is_stable_within_session() -> None:
    mapping = AnonMapping()
    hash_a = "abcdef0123456789abcdef0123456789"
    hash_b = "fedcba9876543210fedcba9876543210"
    out_a = anonymize(
        json.dumps({"type": "user", "message": {"content": f"~/.claude/projects/{hash_a}/sess"}}),
        mapping=mapping,
    )
    out_b = anonymize(
        json.dumps({"type": "user", "message": {"content": f"~/.claude/projects/{hash_b}/sess"}}),
        mapping=mapping,
    )
    out_a_again = anonymize(
        json.dumps({"type": "user", "message": {"content": f"~/.claude/projects/{hash_a}/other"}}),
        mapping=mapping,
    )
    assert "fixture-1" in out_a
    assert "fixture-2" in out_b
    assert "fixture-1" in out_a_again
    assert hash_a not in out_a
    assert hash_b not in out_b


def test_timestamp_normalization_preserves_relative_offsets() -> None:
    mapping = AnonMapping()
    first = anonymize(
        json.dumps({"type": "user", "timestamp": "2025-06-01T10:00:00+00:00", "message": {}}),
        mapping=mapping,
    )
    second = anonymize(
        json.dumps({"type": "user", "timestamp": "2025-06-01T10:00:05+00:00", "message": {}}),
        mapping=mapping,
    )
    assert BASE_TIMESTAMP.isoformat() in first
    decoded = json.loads(second)
    assert decoded["timestamp"] == "2026-01-01T00:00:05+00:00"


def test_tool_use_id_rewrite_is_stable_within_session() -> None:
    mapping = AnonMapping()
    line_one = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "toolu_01ABC", "name": "Read", "input": {}}]
            },
        }
    )
    line_two = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "toolu_01ABC", "content": "ok"}]
            },
        }
    )
    line_three = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "toolu_02XYZ", "name": "Edit", "input": {}}]
            },
        }
    )
    out_one = json.loads(anonymize(line_one, mapping=mapping))
    out_two = json.loads(anonymize(line_two, mapping=mapping))
    out_three = json.loads(anonymize(line_three, mapping=mapping))
    assert out_one["message"]["content"][0]["id"] == "tool-1"
    assert out_two["message"]["content"][0]["tool_use_id"] == "tool-1"
    assert out_three["message"]["content"][0]["id"] == "tool-2"


def test_anonymize_session_round_trips_iterable() -> None:
    lines = [
        json.dumps({"type": "user", "timestamp": "2025-06-01T10:00:00+00:00", "message": {}}),
        json.dumps({"type": "user", "timestamp": "2025-06-01T10:00:01+00:00", "message": {}}),
    ]
    out = list(anonymize_session(lines))
    assert len(out) == 2
    assert "2026-01-01T00:00:00+00:00" in out[0]
    assert "2026-01-01T00:00:01+00:00" in out[1]
