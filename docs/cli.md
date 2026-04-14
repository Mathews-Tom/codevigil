# CLI reference

Exhaustive reference for every codevigil subcommand and flag. For a quick first-run walkthrough see [getting-started.md](getting-started.md).

## Top-level flags

These apply to every subcommand and must appear before the subcommand name on the command line.

| Flag            | Description                                                                                     |
| --------------- | ----------------------------------------------------------------------------------------------- |
| `-h, --help`    | Show help for the top-level command and exit.                                                   |
| `--version`     | Print `codevigil <version>` and exit.                                                           |
| `--config PATH` | Path to a TOML config file. Overrides `~/.config/codevigil/config.toml`.                        |
| `--explain`     | Surface `stop_phrase` collector `intent` annotations in `watch`, `report`, and `export` output. |

## Subcommands

| Subcommand                      | Purpose                                                                     |
| ------------------------------- | --------------------------------------------------------------------------- |
| [`config check`](#config-check) | Resolve the effective config and print each value with its source.          |
| [`watch`](#watch)               | Live tick loop over `~/.claude/projects` with a terminal dashboard.         |
| [`report`](#report)             | Batch analysis over one or more session files.                              |
| [`export`](#export)             | Stream parsed events as NDJSON on stdout.                                   |
| [`history`](#history)           | Retrospective view of stored session reports (list, detail, diff, heatmap). |

---

## `config check`

```text
codevigil config check
```

Resolves the effective configuration through the precedence chain (defaults → config file → environment → CLI flags) and prints every leaf key with its provenance. Useful for debugging "why is this value what it is".

### Output format

```text
codevigil config check
  bootstrap.sessions = 10  (default)
  bootstrap.state_path = '~/.local/state/codevigil/bootstrap.json'  (default)
  collectors.enabled = ['read_edit_ratio', 'stop_phrase', 'reasoning_loop']  (default)
  ...
  watch.poll_interval = 5.0  (file:/Users/me/.config/codevigil/config.toml)
  watch.tick_interval = 1.5  (env:CODEVIGIL_WATCH_TICK_INTERVAL)
```

Each line follows the format `<dotted.key> = <value>  (<source>)`. The source is one of:

- `default` — the built-in default in `codevigil/config.py`
- `file:<path>` — set by the resolved TOML config file
- `env:<VAR>` — set by an environment variable
- `cli:--<flag>` — set by a CLI flag (not user-facing in v0.1)

### Exit codes

- `0` — success
- `1` — non-critical config error
- `2` — critical config error (unknown key, type mismatch, out of range, unknown collector / renderer name)

### Examples

```bash
codevigil config check
codevigil --config ./project-codevigil.toml config check
CODEVIGIL_WATCH_POLL_INTERVAL=0.5 codevigil config check
```

---

## `watch`

```text
codevigil watch
```

Starts the live tick loop. Polls `watch.root` at `watch.poll_interval`, parses new events, runs every enabled collector, and renders a frame to the terminal at `watch.tick_interval`.

### What it does

1. Resolves config.
2. Constructs a `PollingSource` rooted at `watch.root` (default `~/.claude/projects`). Refuses to start if the resolved root is outside `$HOME`.
3. Loads `~/.config/codevigil/projects.toml` if present, for friendly project name resolution.
4. Constructs a `SessionAggregator` with the bootstrap manager loaded from `bootstrap.state_path`.
5. Runs the tick loop:
   - `renderer.begin_tick()`
   - For each `(meta, snapshots)` pair from `aggregator.tick()`:
     - Apply `--explain` annotation rewrites if the flag is set.
     - `renderer.render(snapshots, meta)`
   - `renderer.end_tick()` — flushes the buffered frame in one write.
   - `time.sleep(watch.tick_interval)`
6. On `SIGINT` (`Ctrl-C`), the next tick exits the loop, calls `aggregator.close()` and `renderer.close()` via `try/finally`, prints `codevigil shutdown`, and returns 0.

### Output

Multi-session terminal dashboard. Most-recently-active sessions appear first. See [getting-started.md](getting-started.md) for a fully annotated example frame.

### Exit codes

- `0` — clean shutdown via `Ctrl-C`
- `1` — non-critical config or runtime error
- `2` — critical error (unknown config, path scope violation, etc.)

### Examples

```bash
codevigil watch
codevigil --explain watch
CODEVIGIL_WATCH_POLL_INTERVAL=1.0 codevigil watch
codevigil --config ./local.toml watch
```

---

## `report`

```text
codevigil report PATH [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                      [--format {json,markdown}] [--output DIR]
                      [--group-by {day,week,project,model,permission_mode}]
                      [--compare-periods A_START:A_END,B_START:B_END]
```

Batch analysis over one or more session files. With no cohort flags, walks the input, parses each file, runs every enabled collector, and writes a deterministic per-session report. With `--group-by` or `--compare-periods`, produces a Markdown cohort report instead.

### Positional argument

| Argument | Description                                                                                                                                           |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PATH`   | A file, a directory (recursively walked for `*.jsonl`), or a shell glob pattern. Globs are evaluated relative to the parent directory of the pattern. |

### Flags

| Flag                       | Description                                                                     |
| -------------------------- | ------------------------------------------------------------------------------- |
| `--from YYYY-MM-DD`        | Filter at the **event** level: discard individual events whose timestamp is strictly before this date. Sessions that straddle this boundary contribute only their in-window events; `started_at` is clamped to the first in-window event. Sessions with zero in-window events are omitted entirely. |
| `--to YYYY-MM-DD`          | Filter at the **event** level: discard individual events whose timestamp is strictly after this date. Sessions that straddle this boundary contribute only their in-window events; `ended_at` is clamped to the last in-window event. Sessions with zero in-window events are omitted entirely. |
| `--format {json,markdown}` | Output format for the per-session report. Default `json`.                       |
| `--output DIR`             | Override the report output directory. Must resolve under `$HOME`.               |
| `--group-by DIMENSION`     | Produce a cohort trend table. See below. Incompatible with `--compare-periods`. |
| `--compare-periods RANGES` | Compare two date ranges. See below. Incompatible with `--group-by`.             |

> **Note on `--from`/`--to` granularity.** These flags operate at the individual event timestamp, not at the session boundary. A session that runs from 23:50 to 00:10 across midnight will appear in both `--to 2026-01-01` (pre-midnight events only) and `--from 2026-01-02` (post-midnight events only) reports, each with a clamped `started_at`/`ended_at` that reflects the in-window portion. This behaviour differs from prior versions, which dropped or kept entire sessions based on the session's first event timestamp. Reports generated with narrow date windows over sessions that straddle those windows will show different (lower) event counts and metric values than reports with no date filter.

### JSON output shape

```json
{
  "kind": "report",
  "generated_at_session_count": 12,
  "sessions": [
    {
      "session_id": "abc123",
      "project_hash": "fixture-1",
      "project_name": "my-project",
      "metrics": [
        {
          "name": "read_edit_ratio",
          "value": 5.2,
          "label": "R:E 5.2 | research:mut 7.1",
          "severity": "ok",
          "detail": {
            "research_mutation_ratio": 7.1,
            "blind_edit_rate": 0.0,
            "tracking_confidence": 1.0,
            "experimental": true
          }
        }
      ]
    }
  ]
}
```

Sessions are sorted by `session_id`; metrics within each session are sorted by `name`. Top-level keys are emitted via `json.dumps(..., sort_keys=True)`. The output is byte-identical across runs on identical input — diffable under git.

### Markdown output shape

A short summary header followed by one section per session, each containing a metric table:

```markdown
# codevigil report

Sessions: 12

## session: abc123

project: my-project

| metric          | value | severity | label                         |
| --------------- | ----- | -------- | ----------------------------- |
| read_edit_ratio | 5.2   | OK       | R:E 5.2 \| research:mut 7.1   |
| reasoning_loop  | 6.4   | OK       | 6.4/1K tool calls \| burst: 2 |
| stop_phrase     | 0     | OK       | 0 hits                        |
```

### --group-by cohort trend report

When `--group-by DIMENSION` is provided, codevigil aggregates all sessions into cohort cells grouped by the chosen dimension and emits a Markdown trend table. The per-session `--format` and JSON output are not produced.

Valid dimensions:

| Dimension         | Groups sessions by                               |
| ----------------- | ------------------------------------------------ |
| `day`             | Calendar date of the first event (UTC).          |
| `week`            | ISO 8601 week (`YYYY-Www`).                      |
| `project`         | Project hash derived from the session file path. |
| `model`           | Model identifier from session metadata.          |
| `permission_mode` | Permission mode from session metadata.           |

Each cell in the trend table shows `mean ± stdev (n)`. Cells with fewer than 5 observations are replaced with the sentinel `n<5` and are not used in any headline reporting.

Output shape:

```markdown
# Cohort Trend Report — by week

| week     | Parse Health       | Read:Edit Ratio    | Write Precision    |
| -------- | ------------------ | ------------------ | ------------------ |
| 2026-W14 | 0.99 ± 0.01 (n=10) | 4.20 ± 1.31 (n=10) | 0.43 ± 0.12 (n=10) |
| 2026-W15 | 0.98 ± 0.02 (n=15) | 3.80 ± 0.98 (n=15) | 0.51 ± 0.09 (n=15) |

## Methodology

...

## Appendix

### Behavioral Catalog

...

### Threshold Table

...
```

The report is written to `cohort_<dimension>.md` in the output directory (e.g., `cohort_week.md`) and also printed to stdout.

### --compare-periods comparison report

When `--compare-periods A_START:A_END,B_START:B_END` is provided, codevigil filters sessions into two non-overlapping date ranges, runs Welch's t-test on each shared metric, and emits a signed delta table with a prose one-liner per metric.

Date range format: `YYYY-MM-DD:YYYY-MM-DD,YYYY-MM-DD:YYYY-MM-DD`. Both start dates are inclusive; both end dates are inclusive. Periods need not be contiguous.

Output shape:

```markdown
# Period Comparison: 2026-03-30..2026-04-05 vs 2026-04-06..2026-04-12

Sessions in period A: 10 — Sessions in period B: 10

| Metric          | Period A mean | Period B mean | Delta | Delta% | Significant |
| --------------- | ------------- | ------------- | ----- | ------ | ----------- |
| read_edit_ratio | 4.20 (n=10)   | 3.80 (n=10)   | -0.40 | -9.5%  | no          |

## Summary

- **read_edit_ratio**: fell from 4.2 to 3.8 over the 2026-04-06..2026-04-12 window; n=10, n=10

## Methodology

...

## Appendix

...
```

The report is written to `compare_periods.md` in the output directory and also printed to stdout. Metrics where either period has fewer than 5 sessions are excluded from headline one-liners; the table still shows the `n<5` sentinel.

`--group-by` and `--compare-periods` are mutually exclusive. Supplying both exits 2 immediately.

### Output directory

The default output directory is `~/.local/share/codevigil/reports/`. Override via `--output DIR` or `report.output_dir` in config. The resolved path **must** be under `$HOME` — codevigil refuses to write outside the home directory and exits 2 with `PrivacyViolationError` if you point `--output` elsewhere.

### Exit codes

- `0` — success, no integrity issues
- `2` — at least one session had `parse_confidence < 0.9` (parse_health CRITICAL), OR `--output` resolved outside `$HOME`, OR a config error, OR `--group-by` and `--compare-periods` used together, OR `--compare-periods` date format is invalid

The non-zero exit on parse_health degradation is intentional: it lets shell scripts and CI jobs detect data integrity failures without parsing the report content.

### Examples

```bash
codevigil report ~/.claude/projects
codevigil report ~/.claude/projects --format markdown
codevigil report sessions/ --from 2026-04-01 --to 2026-04-30
codevigil report 'sessions/*.jsonl' --format json --output ~/reports
codevigil --explain report sessions/ --format markdown

# Cohort trend — group by ISO week
codevigil report ~/.claude/projects --group-by week
codevigil report sessions/ --from 2026-01-01 --to 2026-03-31 --group-by week
codevigil report ~/.claude/projects --group-by project

# Period comparison — two four-week windows
codevigil report ~/.claude/projects --compare-periods 2026-03-01:2026-03-31,2026-04-01:2026-04-30
codevigil report sessions/ --compare-periods 2026-03-30:2026-04-05,2026-04-06:2026-04-12
```

---

## `export`

```text
codevigil export PATH
```

Streams the parsed event stream as NDJSON on stdout, one JSON object per line. Designed for piping into `jq`, loading into notebooks, or feeding ad-hoc analysis pipelines.

### Positional argument

| Argument | Description                                                                      |
| -------- | -------------------------------------------------------------------------------- |
| `PATH`   | A file, a directory (recursively walked for `*.jsonl`), or a shell glob pattern. |

### Output shape

```json
{
  "timestamp": "2026-04-13T10:11:23+00:00",
  "session_id": "abc123",
  "kind": "tool_call",
  "payload": {
    "tool_name": "read",
    "tool_use_id": "tool-1",
    "input": { "path": "/home/user/code.py" }
  }
}
```

Each line is one parsed `Event`. The `kind` field is one of:

- `tool_call`
- `tool_result`
- `assistant`
- `user`
- `thinking`
- `system`

The `payload` shape varies per kind. See [design.md §Payload Schemas by EventKind](design.md#payload-schemas-by-eventkind) for the authoritative table.

### Exit codes

- `0` — success
- `1` — file read error
- `2` — config error or file not found

### Examples

```bash
# Count events by kind
codevigil export session.jsonl | jq '.kind' | sort | uniq -c

# Find every tool call
codevigil export session.jsonl | jq 'select(.kind == "tool_call") | .payload.tool_name'

# Dump every assistant message that contains "actually"
codevigil export session.jsonl | jq 'select(.kind == "assistant" and (.payload.text | contains("actually")))'

# Export everything in a project to a single file
codevigil export ~/.claude/projects/abc/sessions/ > all-events.ndjson
```

The `--explain` flag is plumbed through `export` for forward compatibility but currently does not change the output — the parser does not surface `intent` annotations on raw events. A future parser change can flow into export without re-wiring the dispatcher.

---

## `history`

```text
codevigil history list [OPTIONS]
codevigil history <SESSION_ID>
codevigil history diff <SESSION_A> <SESSION_B>
codevigil history heatmap <SESSION_ID>
```

Retrospective, post-mortem view of stored session reports from the `SessionStore` (`$XDG_STATE_HOME/codevigil/sessions/`). Reads session reports written by the aggregator when `storage.enable_persistence = true`. All `history` subcommands are read-only and make no network calls.

### `history list`

Lists all stored sessions in a rich formatted table. Reads all sessions from the store in a single pass — no per-row disk reads after the initial enumeration.

**Columns:** `session_id` (short, 12-char), `project`, `started_at`, `duration`, `severity`, `model`, `permission_mode`, `metrics_summary` (top-2 metrics by absolute value).

**Flags:**

| Flag                        | Description                                                                |
| --------------------------- | -------------------------------------------------------------------------- |
| `--project NAME`            | Filter by project name or project hash.                                    |
| `--since YYYY-MM-DD`        | Include sessions whose `started_at` is on or after this date (inclusive).  |
| `--until YYYY-MM-DD`        | Include sessions whose `started_at` is on or before this date (inclusive). |
| `--severity {ok,warn,crit}` | Filter by worst-metric severity across all metrics in the session.         |
| `--model MODEL`             | Filter by model identifier (exact match).                                  |
| `--permission-mode MODE`    | Filter by permission mode (exact match).                                   |

**Severity classification** maps metric values to labels using the same thresholds as the watch-mode collectors:

| Metric            | Warn threshold | Crit threshold | Scale                     |
| ----------------- | -------------- | -------------- | ------------------------- |
| `read_edit_ratio` | < 4.0          | < 2.0          | inverted (lower is worse) |
| `stop_phrase`     | >= 1.0         | >= 3.0         | normal (higher is worse)  |
| `reasoning_loop`  | >= 10.0        | >= 20.0        | normal                    |
| `parse_health`    | < 0.9          | < 0.9          | inverted                  |

**Examples:**

```bash
# List all stored sessions
codevigil history list

# List sessions from a specific project since a date
codevigil history list --project my-project --since 2026-04-01

# List only sessions classified as critical
codevigil history list --severity crit

# Filter by model
codevigil history list --model gpt-4.1
```

**Exit codes:**

- `0` — success (even if the store is empty or no sessions match the filters)
- `2` — invalid date format for `--since` or `--until`

### `history <SESSION_ID>`

Renders a single stored session in detail using `rich.panel.Panel` and `rich.table.Table` for visual layout.

**Output sections:**

1. **Header block** — session id, project, model, permission_mode, started_at, duration, event count, parse confidence, final severity.
2. **Metrics table** — one row per metric: name, value (4 decimal places), severity label.
3. **Stop-phrase context snippets** — when present in the session detail.

**Examples:**

```bash
codevigil history agent-abc123def456ghi
```

**Exit codes:**

- `0` — session found and rendered
- `1` — session id not found in the store

### `history diff <SESSION_A> <SESSION_B>`

Renders a side-by-side comparison of two sessions using rich formatted tables. Aligns metric name sequences using `difflib.SequenceMatcher` (LCS). Output is deterministic.

**Output sections:**

1. **Header comparison** — session_id, project, model, permission_mode, started_at, duration (with signed delta), event count, severity.
2. **Metric diff table** — one row per aligned metric pair: name, value A, value B, delta (B - A) with sign. Metrics present in only one session appear with `_(absent)_`.

**Examples:**

```bash
codevigil history diff agent-abc123 agent-def456
```

**Exit codes:**

- `0` — both sessions found and diffed
- `1` — one or both sessions not found in the store
- `2` — usage error (fewer than two session ids provided)

### `history heatmap <SESSION_ID>`

Renders a metric × severity matrix for a single session using `rich.table.Table`. Each row is one metric; columns are `ok`, `warn`, and `crit`; the cell in the session's actual severity bucket shows the metric value, other cells show `—`.

**Examples:**

```bash
codevigil history heatmap agent-abc123
```

**Exit codes:**

- `0` — success
- `1` — session id not found in the store

---

## Configuration interaction

Every subcommand reads the same effective config (see [configuration.md](configuration.md)). The precedence chain is:

1. CLI flags (highest)
2. `CODEVIGIL_*` environment variables
3. TOML config file (`--config` or `~/.config/codevigil/config.toml`)
4. Built-in defaults (lowest)

`codevigil config check` shows the resolved value and source for every key. Use it as the first step when debugging "why does codevigil think X is set to Y".
