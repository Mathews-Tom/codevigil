# Task Classifier

The task classifier assigns a category label to each completed turn and aggregates those labels to a session-level summary. It is a pure function of the `Turn` snapshot produced by the aggregator — it has no side effects, no network access, and no new runtime dependencies (stdlib `re` only).

> **Experimental.** Category definitions and cascade rules may change between minor releases. Set `classifier.experimental = false` in your config once you have validated the labels against your own session corpus.

## Category set

| Label            | Short description                                           |
| ---------------- | ----------------------------------------------------------- |
| `exploration`    | Read-heavy investigation with minimal or no mutations.      |
| `mutation_heavy` | Three or more file-write operations with no bash calls.     |
| `debug_loop`     | Bash execution co-present with file mutations (fix/run).    |
| `planning`       | Pure text turn — zero tool calls.                           |
| `mixed`          | No single category holds a majority of the labelled turns.  |

`mixed` is both a turn-level fallback (when neither stage produces a match) and the session-level result when no category exceeds 50% of the classified turns.

## Cascade algorithm

Classification runs in two stages. Stage 1 takes precedence; Stage 2 only runs when Stage 1 is ambiguous.

### Stage 1 — tool-presence heuristics

Rules are evaluated in priority order. The first match wins.

| Priority | Category         | Rule                                                                                              |
| -------- | ---------------- | ------------------------------------------------------------------------------------------------- |
| 1        | `mutation_heavy` | `mutation_count >= 3` AND `bash_count == 0`                                                       |
| 2        | `debug_loop`     | `bash_count >= 1` AND `mutation_count >= 1`                                                       |
| 3        | `planning`       | `len(turn.tool_calls) == 0` — absolutely no tool calls in this turn                              |
| 4        | `exploration`    | `bash_count == 0` AND `mutation_count < 2` AND `read_glob_count > 2 * mutation_count`            |

**Tool families:**

- Mutation tools: `edit`, `write`, `multi_edit`
- Read/glob tools: `read`, `glob`
- `bash` is counted separately; `grep`, `ls`, and other tools are neutral (they do not contribute to any counter but their presence disqualifies the `planning` rule)

**Design rationale:**

- Rule 1 before Rule 2: pure annotation sessions (mass edits, no execution) are structurally different from debug loops. Requiring bash absence separates them cleanly.
- Rule 3 requires *zero* tool calls, not just zero read/glob. Any tool call — even `grep` — means the assistant did structured investigation, not pure text planning.
- Rule 4's dominance condition (`read_glob > 2 * mutation`) allows one incidental edit during an investigation without reclassifying the turn as mutation-heavy.

### Stage 2 — keyword regex on user message text

When Stage 1 produces no match, a regex is applied to the user's message text. Categories are checked in priority order; the first match wins.

| Priority | Category         | Sample trigger words                                                                          |
| -------- | ---------------- | --------------------------------------------------------------------------------------------- |
| 1        | `debug_loop`     | fix, debug, broken, failing, error, exception, crash, traceback, not working, regression      |
| 2        | `mutation_heavy` | implement, add, create, build, write, generate, scaffold, port, migrate, refactor, rename     |
| 3        | `exploration`    | why, investigate, explore, understand, look at, show me, what does, how does, explain, trace  |
| 4        | `planning`       | plan, design, architect, outline, propose, think through, strategy, tradeoff, option, compare |

`debug_loop` is checked before `mutation_heavy` because debug-phrased prompts often contain mutation verbs ("implement the fix") and the debug label is more specific. When neither stage matches, the turn is labelled `mixed`.

## Session aggregation

`aggregate_session_task_type` reduces turn-level labels to a single session label.

1. Turns with `task_type = None` (classifier disabled, or turns that were never classified) are excluded from the vote.
2. Sessions with fewer than two labelled turns return `"mixed"` — the signal is too weak for a confident session-level label.
3. When a single category holds more than 50% of the labelled turns, that category wins.
4. When no category exceeds 50% (including ties), the session is `"mixed"`.

## Session report fields

When `classifier.enabled = true`, the session report written by `codevigil watch` gains two additional fields:

| Field                | Type                    | Description                                                             |
| -------------------- | ----------------------- | ----------------------------------------------------------------------- |
| `session_task_type`  | `str \| null`           | Session-level majority label, or `null` when classifier is disabled.    |
| `turn_task_types`    | `list[str] \| null`     | Ordered list of per-turn labels, or `null` when classifier is disabled. |

Both fields use additive schema: pre-upgrade JSON records that do not contain these keys read back as `None` without error.

## Calibration

The calibration gate requires ≥ 85% session-level agreement between the classifier and a hand-labeled fixture corpus. The gate runs as a pytest test (`tests/test_classifier_calibration.py`) and as a standalone script:

```bash
uv run python scripts/calibrate_classifier.py
```

The script reads `tests/fixtures/task_classification/labels.json`, runs each labeled session through the full parser → TurnGrouper → classifier pipeline, and writes a confusion matrix report to `.docs/classifier-calibration.md`.

Rerun `scripts/calibrate_classifier.py` and commit the updated `.docs/classifier-calibration.md` alongside any change to `TOOL_SIGNATURES` or `KEYWORD_PATTERNS` in `codevigil/classifier.py`.

## User-visible surfaces

The classifier output appears in four places. Every surface that shows a task label is tagged `[experimental]` while `classifier.experimental = true` (the default). All four surfaces degrade cleanly when the classifier is disabled.

### `history list`

When at least one session in the result set has a `session_task_type`, a `task_type [experimental]` column appears in the table. Sessions with no task type show `—` in that cell. The column is hidden entirely — not just empty — when no session carries a task type. Use `--task-type <name>` to filter to sessions with a specific label.

### `history heatmap --axis task_type`

Cross-tabulates metric means across all stored sessions grouped by task type. Sessions with no task type appear under `(unclassified)`. The table title carries `[experimental]`. Exits 1 with a descriptive error if `classifier.enabled = false`.

### `history <SESSION_ID>` (detail view)

When the session carries per-turn task type data, a "Turn Task Types" panel is rendered below the metrics table. Each line shows `Turn N: [<label>] [experimental]`. The session-level label also appears in the header block as `task_type: <label> [experimental]`. Both surfaces are absent when the classifier was disabled at capture time.

### `codevigil watch` session header

A `[task: <label>] [experimental]` tag appears right-aligned on the session header line when a task type has been derived from the session's completed turns. The tag is suppressed when `session_task_type` is `None` (no turns classified yet, or classifier disabled).

## Disabling the classifier

```toml
[classifier]
enabled = false
```

When disabled, `classify_turn` is never called. Every turn's `task_type` remains `None`. `session_task_type` and `turn_task_types` are `null` in session reports. No CPU is spent on classification. All four user-visible surfaces degrade cleanly: no task column, no task header tag, no per-turn headings, and the `--axis task_type` heatmap exits 1 with a descriptive error.
