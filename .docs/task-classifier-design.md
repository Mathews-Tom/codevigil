# Task-Classifier Design Note

**Status:** Design-locked · **Date:** 2026-04-14 · **Phase:** 3 of codeburn integration plan
**Companion docs:** `.docs/codeburn-integration-plan.md`, `.docs/codeburn-integration-analysis.md`

This document locks the classification model before Phase 4 (Turn abstraction) and Phase 5 (classifier implementation) begin. Categories, turn boundary definition, cascade algorithm, layer assignment, calibration methodology, and failure handling are all decided here. Implementation must not deviate from these decisions without a revision to this doc.

---

## 1. Category Set

Five categories cover the observed distribution of Claude Code sessions. Each is defined by its tool-presence signature, keyword hints in the user message, and expected collector-metric profile. A session that does not clearly belong to any single category receives the `mixed` label (see Section 2).

### `exploration`

**Tool-presence signature.** Dominated by `Read` and `Glob` calls. `Bash` calls present but limited to read-only invocations (e.g., `ls`, `cat`, query commands). No `Edit`, `Write`, or `MultiEdit` calls, or fewer than 2. No test runner invocations.

**User-message keyword hints.** Regex: `/\b(why|investigate|explore|understand|look at|walk me through|show me|what does|how does|explain|trace|find where|where is|which file|map out)\b/i`

**Expected collector-metric profile.**
- `read_edit_ratio`: high R:E value (≥ 10.0 in typical exploration turns); no severity flag expected.
- `reasoning_loop`: low tool-call count per turn; no loop flag.
- `parse_health`: normal; no anomalies expected.
- `stop_phrase`: may be present if the user asks the assistant to stop and summarize.

---

### `mutation_heavy`

**Tool-presence signature.** Three or more `Edit`, `Write`, or `MultiEdit` calls in a turn with no preceding test-runner invocation (`Bash` with pytest/jest/cargo test pattern) in that same turn. `Read` calls present but outnumbered by write-family calls.

**User-message keyword hints.** Regex: `/\b(implement|add|create|build|write|generate|scaffold|port|migrate|convert|refactor|rename|move|delete|remove)\b/i`

**Expected collector-metric profile.**
- `read_edit_ratio`: low R:E value (< 2.0); likely triggers warn or critical severity.
- `reasoning_loop`: moderate-to-high tool-call count.
- `parse_health`: normal.
- `stop_phrase`: low occurrence.

---

### `debug_loop`

**Tool-presence signature.** Alternating pattern of `Bash` (test runner or runtime execution) and `Edit`/`Write` within the same turn or across consecutive turns. At least two Bash-then-Edit cycles observable in the session. `Read` calls interleaved. The distinguishing signal is the Bash → Edit → Bash repetition, not just the presence of either tool alone.

**User-message keyword hints.** Regex: `/\b(fix|debug|broken|failing|error|exception|crash|traceback|why is|not working|failing test|reproduce|regression)\b/i`

**Expected collector-metric profile.**
- `read_edit_ratio`: moderate; fluctuates between turns.
- `reasoning_loop`: elevated tool-call count; may trigger warn if loop count is high.
- `parse_health`: may show elevated `duplicate_count` if compaction occurred mid-debug.
- `stop_phrase`: possible if the user interrupts a failing loop.

---

### `planning`

**Tool-presence signature.** No `Edit`, `Write`, or `MultiEdit` calls. `Read` and `Glob` calls may be present but tool density is low overall. The turn is dominated by assistant text output rather than tool calls. `Bash` calls absent or limited to one invocation (e.g., `ls` for orientation).

**User-message keyword hints.** Regex: `/\b(plan|design|architect|outline|propose|think through|approach|strategy|how should|what would|should we|tradeoff|option|compare|consider)\b/i`

**Expected collector-metric profile.**
- `read_edit_ratio`: very high or undefined (zero edits).
- `reasoning_loop`: very low tool-call count; no loop flag.
- `parse_health`: normal; likely short session.
- `stop_phrase`: possible; planning sessions often end with user confirmation.

---

### `mixed`

**Definition.** The default label when no single category holds more than 50% of the turn-level tags in a session. Also applied to sessions with fewer than 2 labelled turns (insufficient signal). The floor is 2 rather than 3 because the labeled fixture corpus shows that 2-turn sessions carry deterministic structural signal (a single mutation-heavy or debug-loop turn paired with a confirming follow-up), whereas 1-turn sessions never do.

**Tool-presence signature.** No distinctive pattern; may contain turns classified as multiple different categories.

**User-message keyword hints.** No reliable keyword set; presence of any two category-signature keywords from different categories is a confirming signal.

**Expected collector-metric profile.** All metrics within normal bounds; no single metric is diagnostic. The `mixed` label is not a failure state — it accurately describes sessions that cross concerns.

---

## 2. Turn Boundary Definition

A **turn** is the unit of classification. It consists of:

1. One user message (the prompt that opens the turn).
2. The assistant's complete response to that message: any thinking blocks, all tool calls and their results in the order they were issued, and the final assistant text message.

The turn closes when the next user message arrives, or when the session ends (whichever comes first).

Formally: `turn_i = [user_message_i, (thinking_block* | tool_call | tool_result | assistant_message)*]` where `*` denotes zero or more occurrences and the sequence is ordered by event timestamp.

**Session aggregate.** A session containing N turns is tagged with the category that holds the majority of turn-level labels (count > N/2). If no single category exceeds 50% of turns, the session receives the `mixed` label. Sessions with fewer than 2 labelled turns default to `mixed` regardless of individual turn labels; the signal is insufficient to draw a session-level conclusion.

**Tie handling.** When two categories are tied at exactly N/2 turns each (only possible when N is even), `mixed` wins. Ties are expected to be rare in practice; if they are common it signals that the category boundaries need revision.

---

## 3. Cascade Algorithm

Classification is a two-stage cascade. Stage 1 uses structural evidence (tool presence and order); Stage 2 uses lexical evidence (keyword match on the user message text). Stage 1 takes precedence; Stage 2 operates only when Stage 1 is ambiguous.

**Stage 1 — Tool-presence heuristic.**

For each turn, examine the ordered sequence of tool calls:

1. If `Edit`/`Write`/`MultiEdit` count ≥ 3 and no test-runner `Bash` call precedes them in this turn → `mutation_heavy`.
2. If Bash-test-runner calls and Edit/Write calls alternate (at least two Bash-Edit cycles) → `debug_loop`.
3. If `Edit`/`Write`/`MultiEdit` count = 0 and `Bash` count = 0 → `planning`.
4. If `Read`/`Glob` calls dominate (count > 2× the sum of all write-family calls) and write-family count < 2 → `exploration`.
5. If none of rules 1–4 match → Stage 1 is **ambiguous**; proceed to Stage 2.

A stage-1 match is deterministic and not overridden by Stage 2. A stage-1 ambiguous result always defers to Stage 2.

**Stage 2 — Keyword regex on user message.**

Apply each category's keyword regex (defined in Section 1) against the full text of the turn's user message. Matching is case-insensitive. First match wins in the order: `debug_loop`, `mutation_heavy`, `exploration`, `planning`. If no regex matches, the turn is labeled `mixed`.

The order of evaluation in Stage 2 is intentional: `debug_loop` is checked first because debug-phrased prompts often also contain mutation verbs ("fix this", "implement the fix") and the debug label is more specific. `mutation_heavy` is checked before `exploration` for the same specificity reason.

**Algorithm summary.**

```
classify_turn(turn):
    stage1_result = tool_presence_heuristic(turn)
    if stage1_result != AMBIGUOUS:
        return stage1_result
    return keyword_regex_match(turn.user_message_text)
```

---

## 4. Layer Assignment

The classifier runs in the **aggregator** (`codevigil/aggregator.py`), not in the parser (`codevigil/parser.py`).

**Rationale.** The parser is a line-level processor. It emits individual `Event` objects as it reads JSONL lines. It has no notion of turn boundaries because a turn spans multiple events across multiple lines and potentially multiple JSONL files. Threading turn-boundary logic into the parser would require the parser to maintain conversational state, which violates its single responsibility: event extraction and deduplication.

Turn boundaries are a conversational construct, not a line-level one. The aggregator already accumulates events into `_SessionContext` objects. It is the natural place where events from the same session are grouped and where temporal ordering is visible. The `TurnGrouper` (defined in Phase 4) operates as a sidecar inside `_SessionContext`, grouping incoming events into completed `Turn` objects. When `TurnGrouper` closes a turn (on the arrival of the next user message or on session eviction), the classifier is called immediately and the result is attached to the `Turn`.

This design keeps the parser stateless with respect to conversation structure and keeps classification colocated with the data structure it operates on.

---

## 5. Opt-In Surface

**Config key.** `classifier.enabled` (boolean, default `true`). When set to `false`, `classify_turn` is never called. Turn objects are created by `TurnGrouper` as normal but `task_type` remains `None` on every turn. No user-visible label appears anywhere.

**Config key.** `classifier.experimental` (boolean, default `true`). When `true`, all classifier output in user-facing surfaces is annotated with `[experimental]` (see Section 8). This flag exists so the badge can be removed in a future release when the classifier is considered stable, without touching any rendering code in the aggregator.

**In-memory attachment.** The `Turn` dataclass (defined in Phase 4) carries an optional `task_type: str | None = None` field. When the classifier is enabled, `dataclasses.replace(turn, task_type=category)` produces a new frozen instance. The aggregator stores the replaced instance in `_SessionContext.completed_turns`.

**SessionReport fields.** Two additive optional fields are added to `SessionReport` in Phase 5:
- `session_task_type: str | None` — the session-level aggregate label.
- `turn_task_types: tuple[str, ...] | None` — the per-turn labels in turn order.

Both default to `None` for sessions processed before the classifier was introduced, and for sessions processed with `classifier.enabled = false`. Existing history-store entries read back with `None` in these fields; no migration is required.

**Collector isolation.** Collectors (`read_edit_ratio`, `reasoning_loop`, `parse_health`, `stop_phrase`) do **not** consume `task_type` in v1. They continue to receive raw `Event` streams. Task-aware thresholds (e.g., "treat a `mutation_heavy` session differently in `read_edit_ratio`") are a v2 concern deferred until the classifier has sufficient trust from calibration. This is an explicit decision: adding task-aware thresholds before calibration is established would couple the two features in a way that makes it impossible to isolate regressions.

---

## 6. Calibration Protocol

**Corpus.** Approximately 100 hand-labeled turns drawn from the fixtures in `tests/fixtures/task_classification/`. These fixtures were established in Phase 0. Labels are stored in `tests/fixtures/task_classification/labels.json` as an array of objects with fields `session_id`, `turn_index`, and `task_type`.

**Procedure.**

1. Run the classifier over every labeled turn in `labels.json`.
2. Compare classifier output to the hand label for each turn.
3. Compute agreement rate: `(count of matching labels) / (total labeled turns)`.
4. Emit a confusion matrix showing, for each actual label, how many turns were classified as each possible category.

**Target.** Agreement rate ≥ 85%. This is a hard gate: `tests/test_classifier_calibration.py` asserts this threshold and fails the build if it is not met.

**Iteration.** When the first calibration run falls short of 85%, inspect the confusion matrix to identify which category pairs are being confused. Adjust the tool-presence rules (Stage 1) or the keyword regexes (Stage 2) to address the most frequent misclassification. Re-run calibration. Repeat until the target is met or the iteration budget is exhausted (see Section 7).

**Calibration report.** The confusion matrix and agreement rate from the most recent calibration run are checked in to `.docs/classifier-calibration.md`. This file is regenerated by `scripts/calibrate_classifier.py` and committed whenever rules change. It serves as an auditable record of classifier quality at each rule revision.

---

## 7. Failure Mode

When the classifier disagrees with a hand label, the **classifier loses**. The hand label is the ground truth.

**Standard response.** Examine the misclassified turn. Identify which stage produced the wrong result (Stage 1 tool heuristic or Stage 2 keyword regex). Update the relevant rule: tighten or relax the tool-count threshold, add a keyword to a pattern, or add a Stage 1 rule to catch the pattern explicitly.

**Structural failure.** If a misclassification cannot be corrected without overfitting — meaning the rule change that fixes this turn breaks three other turns — the category boundary is wrong. The correct response is not to add an exception for this turn but to **redesign the category**. The Phase 3 design note (this document) is the place to record a category revision. Phase 5 implementation must be updated to match.

**Iteration budget.** After 5 rule-tuning passes without reaching the 85% target, stop. Report the final confusion matrix and the categories responsible for the majority of disagreements. Options at that point:

1. Shrink the category set to fewer, better-separated buckets (e.g., collapse `planning` into `exploration` if they are chronically confused).
2. Abandon the feature for this release and ship with `classifier.enabled = false` in the default config.

**Do not ship a classifier below 85% agreement** by relaxing the test threshold or suppressing the calibration test. The threshold exists to protect users from misleading labels in user-facing output.

---

## 8. Experimental Badge

All classifier output in user-facing surfaces is tagged `[experimental]`.

This mirrors the threshold-honesty pattern already established in the project: collector outputs that depend on configurable thresholds are presented with explicit transparency about their assumptions. The `[experimental]` badge serves the same purpose for classifier output: users can distinguish deterministic collector metrics (tool call counts, R:E ratio) from classifier-derived labels (task type) which are inherently heuristic.

**Surfaces where the badge appears (Phase 6).**
- `history list` task-type column header: `Task [experimental]`.
- `history detail` per-turn headings: `[exploration] [experimental]`.
- `watch` session header tag: `[task: exploration] [experimental]`.

**Removal path.** When calibration evidence over a real-world corpus (post-Phase 5) demonstrates stable agreement above 90% over a sustained period, the `classifier.experimental` config key can be set to `false` in a future release. All badge-rendering code checks this flag; no other code changes are required.

**Relationship to `classifier.enabled`.** When `classifier.enabled = false`, no badge appears because no label is generated. When `classifier.enabled = true` and `classifier.experimental = true` (the default), the badge always appears. When `classifier.enabled = true` and `classifier.experimental = false` (manually set by the user), labels appear without the badge.
