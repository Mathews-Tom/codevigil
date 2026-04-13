# Collectors

A collector consumes parsed `Event`s, maintains a small amount of internal state, and emits a single `MetricSnapshot` per `snapshot()` call. v0.1 ships four collectors. Three are user-facing and configurable via the `enabled` list; the fourth is the always-on integrity gate.

| Collector                             | What it measures                                                               | Always on?               |
| ------------------------------------- | ------------------------------------------------------------------------------ | ------------------------ |
| [`read_edit_ratio`](#read_edit_ratio) | Reads vs. mutations, blind-edit detection, file-tracking confidence            | No (default enabled)     |
| [`stop_phrase`](#stop_phrase)         | Hits against four phrase categories that flag stalled progress                 | No (default enabled)     |
| [`reasoning_loop`](#reasoning_loop)   | Self-correction phrase rate per 1000 tool calls plus longest consecutive burst | No (default enabled)     |
| [`parse_health`](#parse_health)       | Fraction of input lines successfully parsed in a 50-line drift window          | **Yes — un-disableable** |

Each collector is documented below with: what it measures, the metric shape, threshold semantics, severity rules, and what to do when it flips to WARN or CRITICAL.

## How collectors work

Collectors honor a frozen protocol with three methods:

- `ingest(event: Event) -> None` — process one event. Must not raise. Must not block.
- `snapshot() -> MetricSnapshot` — return current state as a single scalar plus optional structured detail. Idempotent and cheap.
- `reset() -> None` — clear state. Called only at session boundaries (eviction). Never mid-session.

Every snapshot has the same shape:

```python
MetricSnapshot(
    name: str,         # collector identifier
    value: float,      # primary scalar — always a float, always exactly one
    label: str,        # human-readable summary
    severity: Severity,  # OK / WARN / CRITICAL
    detail: dict[str, Any] | None,  # optional structured breakdown
)
```

The single-scalar constraint is deliberate. Every metric must reduce to one number that can be thresholded, trended, and compared. Rich data goes in `detail`. The renderer uses `value` and `severity` for the dashboard and stuffs the rest into expandable detail rows.

## Lifecycle and reset semantics

The aggregator instantiates **one collector instance per session**. Collector state never leaks across sessions. `reset()` is called **only** when:

1. A new session file is first observed (fresh instance, no-op call for symmetry).
2. A session is evicted from the active set (silent ≥ `evict_after_seconds`).

`reset()` is **never** called on `snapshot()` or on tick cadence. Rolling windows (e.g., `read_edit_ratio`'s 50-event deque) MUST persist across snapshots. Clearing them every tick would mask the exact degradation patterns the metric is designed to detect.

The "coffee break" rule: a session that goes silent for less than `evict_after_seconds` (default 35 minutes) and then receives a new event flips back from STALE to ACTIVE without any reset. Your metric history is preserved across pauses.

---

## `read_edit_ratio`

**Signal**: how much the model is _reading_ and _researching_ before it edits, vs. how much it is _mutating_ code blindly.

### What it tracks

Tool calls are classified into four buckets based on their canonicalised `tool_name`:

| Category     | Tool names                                        |
| ------------ | ------------------------------------------------- |
| **read**     | `read`, `view`                                    |
| **research** | `grep`, `glob`, `web_search`, `web_fetch`         |
| **mutation** | `edit`, `multi_edit`, `write`, `notebook_edit`    |
| **other**    | everything else (does not count toward any ratio) |

The collector keeps a rolling deque of the last `window_size` (default 50) classified events. Each `snapshot()` recomputes the ratios from the current deque contents.

### Metrics emitted

The primary scalar is `read_edit_ratio = reads / max(mutations, 1)`. Secondary metrics live in `detail`:

- `research_mutation_ratio` — `research / max(mutations, 1)`
- `blind_edit_rate` — sub-dict with `value`, `tracking_confidence`, and an optional `label` when degraded
- `tracking_confidence` — fraction of mutation events whose `file_path` field was populated
- `experimental` — mirror of the config flag

### Severity

| Condition                                                                              | Severity                     |
| -------------------------------------------------------------------------------------- | ---------------------------- |
| Fewer than `min_events_for_severity` (default 10) classified events seen               | OK with label `"warming up"` |
| `read_edit_ratio < critical_threshold` (default 2.0)                                   | CRITICAL                     |
| `read_edit_ratio < warn_threshold` (default 4.0)                                       | WARN                         |
| Otherwise                                                                              | OK                           |

### Blind-edit detection

A "blind edit" is a mutation event whose `file_path` was not seen in a `read` or `research` event on the same path within the last `blind_edit_window` (default 20) classified events. The metric is `blind_edit_rate = blind_edits / max(mutations, 1)`.

### Tracking confidence degradation

When the fraction of mutation events with a populated `file_path` falls below `blind_edit_confidence_floor` (default 0.95), the blind-edit metric is relabeled `"insufficient data"` and the **blind-edit portion** is degraded — but the overall `read_edit_ratio` severity is **not** clamped. This avoids two failure modes at once: false WARN/CRITICAL alarms when path tracking is unreliable, and silent suppression of real read-edit drift.

### What to do on WARN / CRITICAL

- **WARN**: the model is editing more than it's reading. Sometimes legitimate (heavy refactor sprint). Sometimes a sign that it's racing past the discovery phase. Eyeball the recent assistant messages.
- **CRITICAL**: the model is mutating code with very few prior reads. This is the failure mode stellaraccident's analysis flagged. Strongly recommend pausing the session and asking the model to re-read the affected files before continuing.
- **Tracking confidence label**: if you see `"insufficient data"`, the parser is not surfacing `file_path` for most mutations. This is usually a parser drift signal — check `parse_health` first.

---

## `stop_phrase`

**Signal**: hits against phrase patterns that flag stalled or deflecting model behaviour.

### What it tracks

Scans every `ASSISTANT_MESSAGE` event's `text` payload for matches against four built-in phrase categories. Each hit is recorded with its category, the matched substring, the message index, and an optional `intent` annotation.

### Built-in categories

| Category             | What it flags                                                          | Example phrases                                           |
| -------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------- |
| `ownership_dodging`  | The model deflects the decision back to the user instead of committing | `"that's up to you"`, `"you decide"`                      |
| `permission_seeking` | The model asks for confirmation when it should just do the work        | `"should I"`, `"do you want me to"`, `"would you like"`   |
| `premature_stopping` | The model declares early victory and stops before finishing            | `"I'll leave that for now"`, `"this should work for now"` |
| `known_limitation`   | The model invokes a generic capability disclaimer                      | `"I cannot"`, `"I don't have access"`, `"as an AI"`       |

### Metrics emitted

The primary scalar is `hit_rate = hits / max(messages, 1)`. The detail dict carries:

- `hits_by_category` — dict of category → hit count
- `recent_hits` — list of the last 5 matches with `{category, phrase, matched_substring, message_index}` plus `intent` when `--explain` is set
- `experimental` — mirror of the config flag

### Severity

| Condition                                     | Severity |
| --------------------------------------------- | -------- |
| Total hits ≥ `critical_threshold` (default 3) | CRITICAL |
| Total hits ≥ `warn_threshold` (default 1)     | WARN     |
| Otherwise                                     | OK       |

### Word-boundary matching

The default match mode is **word-boundary**, not substring. This is critical: `"should I"` matches `"should I continue"` but never matches `"shoulder"`. The match is built from `re.escape(phrase)` wrapped in `(?<!\w)…(?!\w)` lookarounds so phrases starting or ending in non-word characters (like `"pre-existing"` or `"correction:"`) still anchor correctly.

For substring or full regex semantics, use the table form:

```toml
[collectors.stop_phrase]
custom_phrases = [
    { text = "as an AI", mode = "substring", category = "known_limitation" },
    { text = "\\b[Pp]erhaps\\b", mode = "regex", category = "ownership_dodging" },
]
```

### Aho–Corasick escalation

Naive multi-phrase regex matching is `O(P · L)` where P is the phrase count and L is the message length. Once `P > 32`, the collector transparently switches to a stdlib-implemented Aho–Corasick automaton with `O(L + matches)` per message. The escalation is invisible to the user — same match semantics, lower asymptotic cost.

### What to do on WARN / CRITICAL

- **WARN**: one or two stop-phrase hits is normal in long sessions. Check `recent_hits` for context.
- **CRITICAL**: 3+ hits suggests the model is stalling. Use the `--explain` flag to see the `intent` annotations and read the surrounding messages. Common patterns:
  - Multiple `permission_seeking` hits → the model wants you to drive. Either give explicit go-ahead or break the task into smaller pieces.
  - Multiple `ownership_dodging` hits → the model is deflecting. Restate the requirements concretely.
  - Multiple `premature_stopping` hits → the model thinks it's done before it is. Verify the work and push back if incomplete.

---

## `reasoning_loop`

**Signal**: how often the model corrects itself mid-stream relative to how much work it's doing.

### What it tracks

Scans every `ASSISTANT_MESSAGE` event's `text` payload for self-correction patterns: `"actually"`, `"wait, that's wrong"`, `"let me reconsider"`, `"I made an error"`, `"correction:"`, `"on second thought"`, etc. Tracks both `TOOL_CALL` events (denominator) and assistant-message hits (numerator).

### Metrics emitted

The primary scalar is `loop_rate = hits * 1000 / max(tool_calls, 1)` — self-corrections per 1000 tool calls. The detail dict carries:

- `max_burst` — the longest consecutive run of assistant messages containing at least one self-correction. Persisted across the session; intentionally not reset mid-session.
- `experimental` — mirror of the config flag

### Severity

| Condition                                                                       | Severity |
| ------------------------------------------------------------------------------- | -------- |
| Fewer than `min_tool_calls_for_severity` (default 20) tool calls seen           | OK       |
| `loop_rate ≥ critical_threshold` (default 20.0)                                 | CRITICAL |
| `loop_rate ≥ warn_threshold` (default 10.0)                                     | WARN     |
| Otherwise                                                                       | OK       |

### What to do on WARN / CRITICAL

A modest reasoning-loop rate is healthy — the model is checking its own work. A high rate is the failure mode where the model thrashes between candidate solutions without converging. Watch `max_burst`: a burst of 5+ consecutive self-correcting messages usually means the model is in a local minimum and would benefit from a step back. Stop and either restate the requirements or break the task down.

### Shared matcher implementation

`reasoning_loop` and `stop_phrase` share a single text-matching helper module (`codevigil/collectors/_text_match.py`) so both collectors get the same word-boundary semantics, the same Aho–Corasick escalation, and the same `force_mode` test hook. Phrase-matching behaviour stays consistent across both collectors by construction.

---

## `parse_health`

**Signal**: data integrity. The parser is observable; the collector watches it.

### What it tracks

`parse_health` is a built-in collector that the aggregator wires into every session via constructor injection. It receives a shared `ParseStats` reference from the parser and computes `parse_confidence = parsed_events / total_lines` over a rolling 50-line window.

### Metrics emitted

The primary scalar is `parse_confidence` — a float in `[0.0, 1.0]`. The detail dict carries:

- `missing_fields` — per-field miss count breakdown when severity is CRITICAL
- `total_lines` — the input line counter
- `parsed_events` — successfully parsed event counter

### Severity

| Condition                                                                              | Severity |
| -------------------------------------------------------------------------------------- | -------- |
| `total_lines < 50` (window not yet full)                                               | OK       |
| `parse_confidence < critical_threshold` (default `0.9`) and window is full            | CRITICAL |
| Otherwise                                                                              | OK       |

The 50-line window is hardcoded and matches the design's drift-detection rule. The CRITICAL boundary defaults to `0.9` (configurable via `collectors.parse_health.critical_threshold`): more than 10 % of input lines failing to parse is the default threshold past which derived metrics from the user-facing collectors should not be trusted. Projects with known-noisy wire formats can relax this value in `config.toml` without needing to patch the collector.

### Why it cannot be disabled

The integrity gate is required, not optional. A user who could disable `parse_health` could end up looking at a `read_edit_ratio` of 8.0 that was actually computed from a parse failure where 80% of the lines were skipped. The disable path would silently corrupt every other metric without warning. The config layer rejects any attempt to set `collectors.parse_health.enabled = false` with `ConfigError("config.parse_health_undisableable")`.

### What to do on CRITICAL

`parse_health` going CRITICAL means **the parser stopped trusting the input**. Every other metric in the same session is suspect until the cause is identified.

Common causes:

1. **Schema drift** — Anthropic shipped a new session JSONL format and the parser's `KNOWN_FINGERPRINTS` table is out of date. Update codevigil. The `parser.unknown_fingerprint` WARN in the error log is the smoking gun.
2. **Truncated session file** — the session was killed mid-write. Look at the last few lines of the affected JSONL file directly.
3. **Disk corruption** — rare but possible. Check `dmesg` and the underlying filesystem.

The CRITICAL banner appears in the watch dashboard above the affected session. In report mode, a single session's parse_health CRITICAL causes the entire `codevigil report` invocation to exit with status 2 — this is intentional, so shell scripts and CI jobs detect data integrity failures without parsing the report content.

---

## Experimental thresholds and bootstrap

Every user-facing collector ships with `experimental = true`. The watch header shows `[experimental thresholds]` until you either flip the flag in config or let bootstrap mode personalise the thresholds for your own workflow.

### Why experimental

The default v0.1 thresholds were derived from a single user's post-hoc analysis (stellaraccident's session window). One user is not a population baseline. Shipping those numbers as authoritative would produce false positives in legitimate contexts: tight debugging loops genuinely invert read/edit ratio, careful reasoning genuinely uses "actually", refactor sprints are edit-heavy by design.

### Bootstrap mode

The aggregator's `BootstrapManager` observes the first `bootstrap.sessions` (default 10) sessions with **all collector severities clamped to OK**. During the observation window:

- Each `snapshot()` still computes the real value.
- The aggregator reads it, clamps the severity to `OK`, and feeds the value back to the manager.
- The manager records per-collector value distributions on disk at `bootstrap.state_path`.

After the observation window completes:

- The manager computes p80 (WARN) and p95 (CRITICAL) of each collector's local distribution.
- Both quantiles are clamped by the literal-value hard caps from `CONFIG_DEFAULTS` so an outlier session can't produce unreasonable thresholds.
- Subsequent snapshots use the derived thresholds instead of the shipped defaults.
- The experimental badge disappears.

### Bootstrap state persistence

`bootstrap.state_path` (default `~/.local/state/codevigil/bootstrap.json`) survives process restarts. Stopping mid-bootstrap and restarting resumes from the same observation count. Deleting the file re-triggers a clean bootstrap. A corrupt file records a single WARN (`bootstrap.corrupt_state`) and re-bootstraps from scratch — never silently swallowed.

### Manual recalibration

If you want to inspect or override the calibrated thresholds before they take effect, use the offline recalibration helper against a fixture corpus:

```bash
python -m scripts.recalibrate_thresholds --fixtures-dir tests/fixtures/sessions
```

This emits a TOML snippet you can paste into `~/.config/codevigil/config.toml`. The script walks the directory, runs the three user-facing collectors against each `*.jsonl` fixture, captures the final snapshot value per collector per session, and computes p80/p95 over the resulting distributions. Output is deterministic on identical input — sorted filenames, fresh collectors per session, fixed-precision floats — so the output diffs cleanly under git.

### Disabling experimental for a single collector

If you have manually calibrated one collector and want to drop the badge for it specifically:

```toml
[collectors.read_edit_ratio]
warn_threshold = 5.0
critical_threshold = 2.5
experimental = false
```

The watch header's `[experimental thresholds]` badge only disappears once **all** enabled collectors have `experimental = false`. As long as one collector is still flagged, the badge stays visible.

---

## Complexity ceiling

Per-ingest cost per collector:

| Collector                                    | Per-ingest cost                                                                                                                      |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `read_edit_ratio`                            | `O(1)` — deque append plus counter update                                                                                            |
| `stop_phrase`                                | `O(P · L)` with P = phrase count, L = message length. Switches to Aho–Corasick automaton when `P > 32` to bound to `O(L + matches)`. |
| `reasoning_loop`                             | `O(P · L)` with the same escalation rule (shared matcher)                                                                            |
| `blind_edit_rate` (inside `read_edit_ratio`) | `O(W)` with W = lookback window size (default 20)                                                                                    |
| `parse_health`                               | `O(1)` — counter read                                                                                                                |

At `P = 50`, `L = 2000`, the naive scan is roughly 5M character compares per session of 50 assistant messages — well under a second on any modern laptop, but not `O(1)`. The Aho–Corasick escalation is the upgrade path if user phrase lists grow large. The collectors are honest about the cost rather than claiming amortised constants.
